"""Relief from locally pinned WoWSP model rasters, aligned with native minimaps."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import re
import sys
from typing import TYPE_CHECKING
from zipfile import BadZipFile

from .local_decision import PACK_SHA, bundle_path
from .ordinary_replay import CURRENT, HASHES, OLD, extra_map_record

if TYPE_CHECKING:
    import numpy as np


ALIGNMENT_NAME = "terrain-alignment.json"
ALIGNMENT_SHA = "cad78108baf12ffbdb7af7c514b8913efd7d7dcc39de33c9855538080f71fe24"
TERRAIN_KIND = "replay_terrain_profile"
COAST_THRESHOLD = .75
LAND_HEIGHT = .5
_BOUNDS_KEYS = ("min_x", "max_x", "min_z", "max_z")


@dataclass(frozen=True)
class TerrainRaster:
    bounds: tuple[float, float, float, float]
    heights: np.ndarray
    digest: str


@lru_cache(maxsize=1)
def _pack_hashes(root):
    blob = (root / "pack.json").read_bytes()
    if len(blob) > 65536 or hashlib.sha256(blob).hexdigest() != PACK_SHA:
        raise ValueError("Terrain data pack inventory failed its checksum")
    return json.loads(blob)["files"]


@lru_cache(maxsize=4)
def read_raster(map_id):
    """Read only the numeric arrays needed for relative relief."""
    import numpy as np

    if not isinstance(map_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,72}", map_id):
        return None
    root = bundle_path()
    name = f"los/{map_id}/terrain_los.npz"
    try:
        expected = _pack_hashes(root).get(name)
        path = root / name
        if not expected or path.stat().st_size > 2 << 20:
            return None
        blob = path.read_bytes()
        if hashlib.sha256(blob).hexdigest() != expected:
            return None
        with np.load(BytesIO(blob), allow_pickle=False) as data:
            heights = data["height"]
            bounds = data["bounds"]
            if (heights.dtype.kind != "f" or heights.ndim != 2
                    or not 16 <= heights.shape[0] == heights.shape[1] <= 512
                    or bounds.shape != (4,) or bounds.dtype.kind != "f"
                    or not np.isfinite(bounds).all() or not np.isfinite(heights).all()):
                return None
            min_x, max_x, min_z, max_z = map(float, bounds)
            if (not min_x < max_x or not min_z < max_z or heights.min() < 0
                    or heights.max() > 5000):
                return None
            heights.setflags(write=False)
            return TerrainRaster((min_x, max_x, min_z, max_z), heights, expected)
    except (OSError, ValueError, KeyError, TypeError, BadZipFile):
        return None


@lru_cache(maxsize=1)
def _alignment():
    base = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    try:
        blob = (base / "ordinary_profiles" / ALIGNMENT_NAME).read_bytes()
        if len(blob) <= 131072 and hashlib.sha256(blob).hexdigest() == ALIGNMENT_SHA:
            return json.loads(blob)["maps"]
    except (OSError, ValueError, KeyError):
        pass
    return {}


def ordered_bounds(source):
    if not isinstance(source, dict):
        return ()
    values = tuple(source.get(key) for key in _BOUNDS_KEYS)
    if all(type(n) in (int, float) and math.isfinite(n) for n in values):
        return values
    return ()


def _map_source(report):
    version = re.sub(r"\s+", "", str(report.get("replay_version", ""))).replace(",", ".")
    metadata = report.get("metadata") or {}
    map_id = str(metadata.get("mapName") or "").rsplit("/", 1)[-1]
    if version == OLD:
        from .replay_context_evidence import _geometry_verified
        if report.get("framing_error") or not _geometry_verified(report):
            return None
        p = report["geometry_analysis"]["png_projection"]
        if p.get("map_id") != map_id:
            return None
        return version, map_id, ordered_bounds(p.get("texture_bounds_engine") or {}), (p.get("source_hashes") or {}).get("map_minimap")
    if version == CURRENT and not report.get("multiview"):
        ordinary = report.get("ordinary") or {}
        pairs, error = ordinary.get("world_pairs"), ordinary.get("world_rmse")
        if (ordinary.get("map_identity") is not True or type(pairs) is not int or pairs < 10
                or type(error) not in (int, float) or not 0 <= error <= 5
                or ordinary.get("profile_sha256") != HASHES[CURRENT + ".json"]):
            return None
        record = extra_map_record(map_id, version)
        if record:
            return version, map_id, ordered_bounds(record["original_bounds_engine"]), record["source_hashes"]["map_minimap"]
    return None


def _match_for(report):
    try:
        source = _map_source(report)
    except (OSError, AttributeError, KeyError, ValueError):
        return None
    if not source:
        return None
    version, map_id, bounds, coast_hash = source
    match = _alignment().get(version, {}).get(map_id)
    if not match or tuple(match["bounds"]) != bounds or match["coast_sha256"] != coast_hash:
        return None
    return map_id, match


def terrain_available(report):
    """Check validated coverage before enabling the optional map control."""
    match = _match_for(report)
    return bool(match and match[1]["has_relief"])


def verified_terrain(report):
    """Accept only the exact raster and native-map pair tested for this build."""
    source = _match_for(report)
    if not source:
        return None
    map_id, match = source
    raster = read_raster(map_id)
    if not raster or raster.bounds != tuple(match["bounds"]) or raster.digest != match["raster_sha256"]:
        return None
    return raster, match["coast_iou"]


def terrain_profile_fact(report):
    """Describe co-registered model relief and land concentration near replay capture points."""
    import numpy as np

    verified = verified_terrain(report)
    if not verified:
        return None
    raster, overlap = verified
    heights = raster.heights
    land = heights > LAND_HEIGHT
    max_height = float(heights.max())
    side = heights.shape[0]
    min_x, max_x, min_z, max_z = raster.bounds
    x = min_x + (np.arange(side) + .5) * (max_x - min_x) / side
    z = max_z - (np.arange(side) + .5) * (max_z - min_z) / side
    context, wording = [], []
    for point in (report.get("capture_points") or [])[:8]:
        cx, cz, radius = (point.get(k) for k in ("x", "z", "radius"))
        if (not all(type(v) in (int, float) and math.isfinite(v) for v in (cx, cz, radius))
                or radius <= 0 or not min_x <= cx <= max_x or not min_z <= cz <= max_z):
            continue
        # Compare a documented neighborhood, never extrapolate past the raster bounds.
        distance = radius * 1.5
        if not (min_x + distance <= cx <= max_x - distance and min_z + distance <= cz <= max_z - distance):
            continue
        dx, dz = x[None, :] - cx, z[:, None] - cz
        neighborhood = dx**2 + dz**2 <= distance**2
        if not (total := int(np.count_nonzero(neighborhood))):
            continue
        nearby = land & neighborhood
        count = int(np.count_nonzero(nearby))
        label = str(point.get("name") or "?")[:24]
        dominant = ""
        if count > 0:
            sectors = (("东北", (dx >= 0) & (dz >= 0)), ("西北", (dx < 0) & (dz >= 0)),
                       ("东南", (dx >= 0) & (dz < 0)), ("西南", (dx < 0) & (dz < 0)))
            share, direction = max((int(np.count_nonzero(nearby & sector))/count, name) for name, sector in sectors)
            if share >= .55:
                dominant = f"，陆地主要在该区{direction}侧"
        context.append({"label": label, "count": count, "denominator": total, "share": round(count/total, 3),
                        "radius_engine": round(distance, 2), "position": [cx, cz]})
        wording.append(f"{label} 区周边陆地约 {count/total:.0%}{dominant}")
    total = int(land.size)
    summary = (f"地形参考模型已与本局原生底图配准。全图地表陆地栅格约 {np.count_nonzero(land)/total:.0%}，"
               f"最高格 {max_height:.1f} 场景单位。"
               + ("以各占领区半径 1.5 倍为邻域，" + "。".join(wording) + "。" if wording else "")
               + "模型只描述地貌。岛体遮挡、炮弹飞越和可通行路径需要其他依据。")
    return {"kind": TERRAIN_KIND, "clock_start": None, "clock_end": None, "entity_ids": [],
            "text": summary, "details": {"source": "WoWSP 地形参考栅格与原生小地图",
                "source_fields": ["terrain_los.height", "terrain_los.bounds", "capture_points"], "unit": "scene_units",
                "quality": "reference_relief", "coverage_ratio": overlap, "value": round(max_height, 2),
                "count": int(np.count_nonzero(land)), "denominator": total, "support": context}}
