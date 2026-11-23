"""Portable, hash-checked copies of the P0 evidence and runtime-only resources."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
from types import SimpleNamespace
from .version import VERSION as PRODUCT_VERSION

VERSION = "15.9.0.13357625"


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def default_bundle_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "runtime_bundle"
    return Path(__file__).resolve().parents[2] / "runtime_bundle"


@dataclass
class RuntimeBundle:
    root: Path
    manifest: dict

    @property
    def version(self):
        return self.manifest["client_version"]

    def path(self, relative: str) -> Path:
        key = PurePosixPath(relative)
        if key.is_absolute() or ".." in key.parts or any(":" in part for part in key.parts):
            raise ValueError("Unsafe resource path")
        path = (self.root / Path(*key.parts)).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("Resource path escapes the bundle")
        return path

    def json(self, relative: str):
        return json.loads(self.path(relative).read_text(encoding="utf-8"))

    def provenance(self, relative: str):
        return {"path": str(self.path(relative)), "sha256": self.manifest["files"][relative],
                "bundle_id": self.manifest["bundle_id"], "verification": "portable_bundle_hashes"}

    def native_provenance(self):
        return {**self.manifest["native_evidence"], "verification": "archived_P0_source_evidence",
                "runtime_game_executable_required": False, "execution": False}

    def relocate_background(self, profile):
        for map_id, record in profile["maps"].items():
            for key, name in (("map_settings", "space.settings"), ("map_minimap", "minimap.png"),
                              ("map_water", "minimap_water.png")):
                record["source_paths"][key] = str(self.path(f"resources/maps/{map_id}/{name}"))

    def ship_catalog(self):
        ships = self.json("ship_catalog.json")["ships"]
        labels = self.json("labels.json").get("ships", {})
        for identifier, ship in ships.items():
            if identifier in labels:
                ship["display_name"] = labels[identifier]
        return ships

    def presentation_report(self, report):
        """Relocate cached map assets when the application folder moves."""
        labels = self.json("labels.json")
        metadata = report.get("metadata") or {}
        map_id = str(metadata.get("mapName") or "").rsplit("/", 1)[-1]
        report["display_map_name"] = labels.get("maps", {}).get(map_id, metadata.get("mapDisplayName", map_id))
        if map_id and map_id not in labels.get('maps',{}):
            from .ordinary_replay import extra_map_record
            version = str(report.get('replay_version','')).replace(',','.')
            record = extra_map_record(map_id,version)
            if record:report['display_map_name']=record.get('label',map_id)
        projection = (report.get("geometry_analysis") or {}).get("png_projection")
        if projection and map_id in self.manifest.get("maps", []):
            for category, name in (("map_settings", "space.settings"), ("map_minimap", "minimap.png"), ("map_water", "minimap_water.png")):
                key = f"resources/maps/{map_id}/{name}"
                if self.manifest["files"].get(key) != projection["source_hashes"].get(category):
                    raise ValueError("Cached report requires a different map resource revision")
                projection["source_paths"][category] = str(self.path(key))
        elif projection and str(report.get('replay_version','')).replace(',', '.') == VERSION:
            from .ordinary_replay import extra_map_record
            record = extra_map_record(map_id)
            if record is not None:
                if record['source_hashes'] != projection.get('source_hashes'):
                    raise ValueError('地图资源版本与已保存分析不一致。')
                projection['source_paths'] = record['source_paths']
        return report


def load_bundle(root: Path | None = None, *, verify=True) -> RuntimeBundle:
    root = (root or default_bundle_root()).resolve()
    manifest = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
    if manifest.get("format_version") != 1 or manifest.get("client_version") != VERSION:
        raise ValueError("Unsupported runtime resource bundle")
    bundle = RuntimeBundle(root, manifest)
    required = {"resources/schema/scripts/entities.xml", "player_schema.json", "background_schema.json",
                "geometry_schema.json", "units_schema.json", "time_schema.json", "packet_semantics.json",
                "ship_catalog.json", "legacy/replay/parser.py"}
    if not required <= manifest.get("files", {}).keys():
        raise ValueError("Runtime bundle is incomplete")
    if verify:
        for relative, expected in manifest["files"].items():
            if file_hash(bundle.path(relative)) != expected:
                raise ValueError(f"Bundled resource changed: {relative}")
    geometry = bundle.json("geometry_schema.json")
    for key, filename in (("background_binding", "background_schema.json"), ("units_binding", "units_schema.json")):
        if geometry.get(key, {}).get("sha256") != manifest["files"][filename]:
            raise ValueError("Portable geometry evidence bindings disagree")
    return bundle


def parse_portable(replay: Path, bundle_root: Path | None = None) -> dict:
    from .safe_pickle import install_replay_pickle_guard, pickle_guard_statistics
    install_replay_pickle_guard()
    rejected_before = pickle_guard_statistics()["rejected"]
    from .parser_engine import probe
    args = SimpleNamespace(replay=str(replay), portable_bundle=str(bundle_root or default_bundle_root()),
                           controller_version=None, fail_fast=False)
    report = probe(args)
    if pickle_guard_statistics()["rejected"] != rejected_before:
        raise ValueError("回放包含不支持的序列化对象，未导入该文件。")
    report["product_version"] = PRODUCT_VERSION
    report["event_key"] = "KOTS19AS" if any(str(p.get("name", "")).endswith("_KOTS19AS") for p in report["roster"]) else "unassigned"
    return report
