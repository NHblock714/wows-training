"""Explicit units, team identities, and bounded packet framing for P0."""

from __future__ import annotations

import math
import re
import struct
import hashlib
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Iterator


def parse_version(value: str) -> tuple[int, ...]:
    parts = re.split(r"[.,_]", value.strip())
    if len(parts) not in (3, 4) or any(not p.strip().isdigit() for p in parts):
        raise ValueError(f"Invalid client version: {value!r}")
    return tuple(int(p.strip()) for p in parts)


def require_exact_version(actual: str, expected: str) -> None:
    if parse_version(actual) != parse_version(expected):
        raise ValueError(f"Schema version mismatch: replay={actual}, resources={expected}")


def outcome(winner_team: int | None, host_team: int | None) -> str:
    if winner_team is None:
        return "unknown"
    if winner_team == -1:
        return "draw"
    if winner_team not in (0, 1) or host_team not in (0, 1):
        return "unknown"
    return "win" if winner_team == host_team else "loss"


def decode_minimap(packed: int) -> dict:
    """Return raw bits and engine-space candidates; never label them metres."""
    if not 0 <= packed <= 0xFFFFFFFF:
        raise ValueError("Minimap field must be UINT32")
    x = packed & 0x7FF
    z = (packed >> 11) & 0x7FF
    disappearing = bool((packed >> 31) & 1)
    sentinel = packed == 0 or (x == 0 and z == 0)
    return {
        "packed": packed, "raw_x": x, "raw_z": z,
        "x_engine": x / 2047.0 * 5000.0 - 2500.0,
        "z_engine": z / 2047.0 * 5000.0 - 2500.0,
        "heading_radians": ((packed >> 22) & 255) / 256.0 * math.tau - math.pi,
        "bit30": (packed >> 30) & 1,
        "disappearing": disappearing, "sentinel": sentinel,
        "wire_visible_candidate": not sentinel and not disappearing,
    }


def position_status(wire_visible: bool, last_update: float | None,
                    clock: float, freshness: float = 3.0) -> str:
    """Freshness is an explicit diagnostic policy, not a server visibility fact."""
    if last_update is None:
        return "unknown"
    age = clock - last_update
    if wire_visible and 0 <= age < freshness:
        return "observed"
    return "last_known"


@dataclass(frozen=True)
class PacketFrame:
    offset: int
    type: int
    clock: float
    payload: bytes


def packet_frames(data: bytes) -> Iterator[PacketFrame]:
    offset = 0
    while offset < len(data):
        if len(data) - offset < 12:
            raise ValueError(f"Truncated packet header at {offset}")
        size, kind, clock = struct.unpack_from("<IIf", data, offset)
        if not math.isfinite(clock):
            raise ValueError(f"Non-finite packet clock at {offset}")
        end = offset + 12 + size
        if end > len(data):
            raise ValueError(f"Truncated packet payload at {offset}: {size} bytes declared")
        yield PacketFrame(offset, kind, clock, data[offset + 12:end])
        offset = end


def verify_schema_manifest(schema: Path, manifest_path: Path, version: str) -> dict:
    """Bind an extracted schema to its pinned build and recorded payload hashes."""
    schema = schema.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    parts = parse_version(version)
    if len(parts) != 4 or str(parts[3]) != manifest.get("build"):
        raise ValueError("Resource manifest build does not match the replay build")
    if manifest.get("status") != "complete" or manifest.get("errors"):
        raise ValueError("Resource extraction manifest is incomplete")
    if Path(manifest["schema"]["scripts_root"]).resolve() != schema / "scripts":
        raise ValueError("Schema path does not match its extraction manifest")
    observed = [parse_version(v) for v in manifest.get("client_versions_observed", [])]
    if parts not in observed:
        raise ValueError("Resource manifest lacks this observed full client version")
    records = [r for r in manifest["resources"] if r["category"] == "entity_schema"]
    actual = {p.resolve() for p in (schema / "scripts").rglob("*") if p.is_file()}
    expected = {Path(r["output_path"]).resolve() for r in records}
    if not records or actual != expected:
        raise ValueError("Schema file set differs from the extraction manifest")
    for record in records:
        file = Path(record["output_path"]).resolve()
        if not file.is_relative_to(schema):
            raise ValueError("Schema record lies outside its root")
        if hashlib.sha256(file.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError(f"Schema hash mismatch: {file.name}")
        source_idx = Path(record["source"]["idx_path"]).resolve()
        expected_idx = Path(manifest["build_root"]).resolve() / "idx"
        if not source_idx.is_relative_to(expected_idx):
            raise ValueError("Schema index source is from a different build")
    return {"manifest": str(manifest_path.resolve()),
            "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "build": manifest["build"], "verified_schema_files": len(records)}


def classify_probe(*, packet_errors: int, callback_errors: int, framing_error: str | None,
                   roster_matches: bool, track_count: int, battle_end: bool,
                   unknown_packets: int, unread_payloads: int, stopped_early: bool,
                   opaque_packets: int = 0) -> tuple[str, list[str]]:
    reasons = []
    for condition, reason in (
        (packet_errors > 0, "packet_processing_errors"),
        (callback_errors > 0, "handled_callback_exceptions"),
        (framing_error is not None, "invalid_packet_framing"),
        (not roster_matches, "roster_mismatch"),
        (track_count == 0, "missing_position_observations"),
        (not battle_end, "battle_end_not_recorded"),
        (unknown_packets > 0, "unmapped_packet_types"),
        (opaque_packets > 0, "opaque_packet_semantics"),
        (unread_payloads > 0, "unconsumed_packet_payload"),
        (stopped_early, "stopped_before_stream_end"),
    ):
        if condition:
            reasons.append(reason)
    return ("partial" if reasons else "structurally_decoded"), reasons

