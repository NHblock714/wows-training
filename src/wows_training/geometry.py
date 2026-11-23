"""Version-pinned playable-coordinate diagnostics, separate from map textures."""
from __future__ import annotations

import json
import math
import hashlib
from pathlib import Path


CLIENT_VERSION = "15.9.0.13357625"
SCHEMA_VERSION = 2
AXIS_CONVENTION = "SpaceSizeUtils.native_max_min_return"


class GeometryUnavailable(ValueError):
    """Required geometric evidence is missing or outside the verified scope."""


def number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def load_geometry_schema(path: Path | str, expected_version: str = CLIENT_VERSION) -> dict:
    schema = json.loads(Path(path).read_text(encoding="utf-8"))
    if schema.get("schema_version") != SCHEMA_VERSION or schema.get("client_version") != expected_version or expected_version != CLIENT_VERSION:
        raise GeometryUnavailable("Geometry schema does not match the verified client version")
    if schema.get("game_execution") is not False:
        raise GeometryUnavailable("Geometry evidence must come from static inspection")
    verified = schema.get("verified", {})
    if not verified.get("map_border_formula") or not verified.get("space_normalization_formula") or not verified.get("native_tuple_order"):
        raise GeometryUnavailable("Required geometry formula evidence is not verified")
    if schema.get("projection", {}).get("convention") != AXIS_CONVENTION:
        raise GeometryUnavailable("Unsupported coordinate convention")
    if schema.get("projection", {}).get("native_tuple_order") != "max_min":
        raise GeometryUnavailable("Native map tuple order has not been verified")
    if not schema.get("map_borders"):
        raise GeometryUnavailable("Map border definitions are missing")
    return schema


def resolve_playable_bounds(schema: dict, map_border: dict) -> dict:
    """Resolve the actual decoded BattleLogic.mapBorder, without guessing by map."""
    if (schema.get("schema_version") != SCHEMA_VERSION or schema.get("client_version") != CLIENT_VERSION
            or not schema.get("verified", {}).get("map_border_formula")
            or not schema.get("verified", {}).get("native_tuple_order")):
        raise GeometryUnavailable("Map border formula is not verified for this client")
    param_id = map_border.get("paramsId")
    if isinstance(param_id, bool) or not isinstance(param_id, int):
        raise ValueError("mapBorder.paramsId must be an integer")
    definition = schema["map_borders"].get(str(param_id))
    if definition is None:
        raise GeometryUnavailable(f"Unknown map border paramsId: {param_id}")
    position = map_border.get("position")
    if not isinstance(position, (list, tuple)) or len(position) != 3:
        raise ValueError("mapBorder.position must contain x, y, z")
    x, _, z = [number(value, f"position[{index}]") for index, value in enumerate(position)]
    width, height = number(definition["width_engine"], "width"), number(definition["height_engine"], "height")
    if width <= 0 or height <= 0:
        raise GeometryUnavailable("Map border has nonpositive dimensions")
    return {
        "min_x": x - width / 2, "max_x": x + width / 2,
        "min_z": z - height / 2, "max_z": z + height / 2,
        "kind": "playable", "unit": "engine", "params_id": param_id,
        "definition_name": definition["name"], "position": list(position),
        "projection_convention": AXIS_CONVENTION,
        "png_alignment_verified": False,
        "extent_source": "MapBorderDef configured boundary",
        "native_viewport_equality_verified": False,
    }


def _dimensions(bounds: dict) -> tuple[float, float, float, float]:
    if bounds.get("kind") != "playable" or bounds.get("unit") != "engine":
        raise GeometryUnavailable("Expected explicit playable bounds in engine units")
    if bounds.get("projection_convention") != AXIS_CONVENTION:
        raise GeometryUnavailable("Bounds have an unverified projection convention")
    min_x, max_x, min_z, max_z = (number(bounds[key], key) for key in ("min_x", "max_x", "min_z", "max_z"))
    if max_x <= min_x or max_z <= min_z:
        raise ValueError("Bounds must have positive width and height")
    return min_x, max_x, min_z, max_z


def normalize_world(x: float, z: float, bounds: dict) -> tuple[float, float]:
    """Apply SpaceSizeUtils' convention; this does not calibrate a PNG texture.

    Positive x moves right; positive z moves up. Values are not clamped, so
    observed positions outside the playable area remain available for diagnosis.
    """
    min_x, max_x, min_z, max_z = _dimensions(bounds)
    x, z = number(x, "x"), number(z, "z")
    return (x - min_x) / (max_x - min_x), (max_z - z) / (max_z - min_z)


def denormalize_world(u: float, v: float, bounds: dict) -> tuple[float, float]:
    min_x, max_x, min_z, max_z = _dimensions(bounds)
    u, v = number(u, "u"), number(v, "v")
    return min_x + u * (max_x - min_x), max_z - v * (max_z - min_z)


def project_to_playable_canvas(x: float, z: float, pixel_width: float, pixel_height: float, bounds: dict) -> tuple[float, float]:
    """Project to continuous canvas edges [0,width] x [0,height], not PNG bounds."""
    width, height = number(pixel_width, "pixel_width"), number(pixel_height, "pixel_height")
    if width <= 0 or height <= 0:
        raise ValueError("Canvas dimensions must be positive")
    u, v = normalize_world(x, z, bounds)
    return u * width, v * height


def contains_world(x: float, z: float, bounds: dict) -> bool:
    u, v = normalize_world(x, z, bounds)
    return 0 <= u <= 1 and 0 <= v <= 1


def _bound_profile(schema: dict, key: str):
    binding = schema.get(key)
    if not isinstance(binding, dict) or not binding.get("path") or not binding.get("sha256"):
        raise GeometryUnavailable(f"Required evidence binding is absent: {key}")
    path = Path(binding["path"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != binding["sha256"]:
        raise GeometryUnavailable(f"Evidence binding changed: {key}")
    return path


def resolve_background_bounds(schema: dict, map_id: str) -> dict:
    """Resolve a texture rectangle only from the pinned native/UI evidence."""
    from .background import background_bounds, load_background_schema
    profile = load_background_schema(_bound_profile(schema, "background_binding"))
    return background_bounds(profile, map_id)


def engine_distance_to_meters(schema: dict, distance: float) -> float:
    from .units import engine_distance_to_meters as convert, load_units_schema
    profile = load_units_schema(_bound_profile(schema, "units_binding"))
    return convert(profile, distance, coordinate_kind="world_position")
