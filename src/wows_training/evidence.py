"""Capture only transmitted BattleLogic state changes, with packet provenance."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import hashlib
import json


class BattleLogicEvidence:
    def __init__(self):
        self.time_samples = []
        self.border_samples = []
        self._last_time = {}
        self._last_border = {}

    def observe(self, entity_id: int, properties: dict, packet_clock: float,
                packet_offset: int, packet_type: int) -> None:
        context = {"packet_clock": packet_clock, "packet_offset": packet_offset,
                   "packet_type": packet_type, "entity_id": entity_id}
        timing = {key: properties.get(key) for key in ("duration", "timeLeft", "battleStage")}
        if timing != self._last_time.get(entity_id):
            self.time_samples.append(context | timing)
            self._last_time[entity_id] = timing
        if "mapBorder" in properties:
            border = properties["mapBorder"]
            if entity_id not in self._last_border or border != self._last_border[entity_id]:
                self.border_samples.append(context | {"mapBorder": deepcopy(border)})
                self._last_border[entity_id] = deepcopy(border)


def validate_client_archive_evidence(path: Path, resource_manifest: Path) -> tuple[dict, dict]:
    """Reject a geometry/time profile from a different extracted client archive."""
    data = json.loads(path.read_text(encoding="utf-8"))
    manifest = json.loads(resource_manifest.read_text(encoding="utf-8"))
    if data.get("source_archive_sha256") != manifest["scripts_zip_audit"]["sha256"]:
        raise ValueError(f"Client archive provenance mismatch: {path.name}")
    return data, {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "source_archive_sha256": data["source_archive_sha256"]}


def validate_native_executable(path: Path, expected_sha256: str, manifest: dict) -> dict:
    """Bind static native evidence to the exact installed build, without loading it."""
    expected_path = Path(manifest["build_root"]).resolve() / "bin64/WorldOfWarships64.exe"
    path = path.resolve()
    if path != expected_path:
        raise ValueError("Native evidence points outside the pinned client executable")
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Client executable changed while validating native evidence")
    if digest.hexdigest() != expected_sha256:
        raise ValueError("Native evidence executable SHA256 no longer matches")
    return {"path": str(path), "sha256": digest.hexdigest(), "size_bytes": after.st_size,
            "build": manifest["build"], "execution": False}


def standard_map_scope(map_name: str, raw_map_count: int, arena_packets: list,
                       layouts: list, *, stream_complete: bool, map_error_count: int) -> bool:
    """A failed/extra Map record cannot disappear from the mapping scope gate."""
    return (stream_complete and map_error_count == 0 and raw_map_count == 1
            and len(arena_packets) == 1 and len(layouts) == 1
            and layouts[0]["fields"].get("matrix_is_identity") is True
            and layouts[0]["fields"].get("map_name", "").rsplit("/", 1)[-1] == map_name)

