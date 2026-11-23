"""Non-mutating annotations from pinned target-client callback definitions."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .contracts import parse_version


def load_packet_semantics(path: Path, resource_manifest: Path) -> tuple[dict, dict]:
    raw = path.read_bytes()
    profile = json.loads(raw)
    manifest = json.loads(resource_manifest.read_text("utf-8"))
    version = parse_version(profile.get("client_version", ""))
    if profile.get("schema_version") != 1 or len(version) != 4 or str(version[3]) != manifest.get("build"):
        raise ValueError("Packet semantics profile version differs from resources")
    if profile.get("source_archive_sha256") != manifest["scripts_zip_audit"]["sha256"]:
        raise ValueError("Packet semantics profile uses a different client archive")
    required = {"camera_mode", "weapon_type", "weapon_lock_type", "cruise_axis", "cruise_speed_state", "cruise_angle_state"}
    if not required <= profile.get("mappings", {}).keys() or not profile.get("consumption_chains"):
        raise ValueError("Packet semantics profile lacks mappings or consumption evidence")
    return profile, {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
                     "source_archive_sha256": profile["source_archive_sha256"], "client_version": profile["client_version"]}


def annotate_packet(decoded: dict, profile: dict, *, replay_version: str | None = None) -> dict:
    """Preserve every decoded field/classification and attach semantic evidence.

An explicit matching full replay version is required. Unknown enum values are
ordinary partial annotations, not exceptions or fabricated default values.
"""
    output = deepcopy(decoded)
    semantics = {"status": "not_applicable", "client_version": profile.get("client_version"),
                 "fields": {}, "diagnostic": [], "evidence_refs": [],
                 "changes_tracks": False, "changes_clock": False}
    output["semantics"] = semantics
    kind = decoded.get("kind")
    if kind not in (0x25, 0x27, 0x30, 0x32, 0x33):
        return output
    try:
        if replay_version is None or parse_version(replay_version) != parse_version(profile["client_version"]):
            semantics["diagnostic"].append("Full replay version does not match the semantic profile")
            return output
    except (ValueError, KeyError, AttributeError):
        semantics["diagnostic"].append("Invalid or missing version evidence")
        return output
    if decoded.get("classification") == "unknown":
        semantics["status"] = "partial"
        semantics["diagnostic"].append("Wire layout is unknown; no semantic interpretation applied")
        return output
    semantics["status"] = "verified"
    mappings, fields = profile["mappings"], decoded.get("fields", {})

    def enum(field, mapping, allow_signed_u32=False):
        raw = fields.get(field)
        key = str(raw)
        if allow_signed_u32 and type(raw) is int and raw == 0xFFFFFFFF:
            key = "-1"
        label = mappings[mapping].get(key)
        semantics["fields"][field] = {"raw": raw, "enum": mapping, "label": label}
        if label is None:
            semantics["status"] = "partial"
            semantics["diagnostic"].append(f"Unknown {mapping} value: {raw!r}")
        return label

    if kind == 0x25:
        native = profile.get("native_camera_word", {})
        semantics["status"] = "partial"
        if native.get("wire_field_offset") == 32 and native.get("wire_field_size") == 4 and native.get("python_attribute") == "tarKey":
            semantics["fields"]["auxiliary_word_raw_u32"] = {
                "raw": fields.get("auxiliary_word_raw_u32"), "role": "CameraFrameParams.tarKey",
                "consumer": "current_camera_mode.setKeyState", "encoding": "unresolved",
            }
            semantics["evidence_refs"] = ["native_camera_word", "unresolved.camera_auxiliary_word.traces"]
            semantics["diagnostic"].append("tarKey wire binding is verified; mode-specific key-state encoding remains raw")
        else:
            semantics["diagnostic"].append("Profile lacks verified native camera-word binding")
        semantics["diagnostic"].append("Recorded reference position includes mode-dependent replay offset; never a ship-track source")
    elif kind == 0x27:
        enum("mode_raw", "camera_mode")
        semantics["evidence_refs"] = ["declarations.camera", "consumption_chains.camera_callback", "consumption_chains.camera_enum_consumer"]
    elif kind == 0x30:
        enum("weapon_type_raw", "weapon_type", allow_signed_u32=True)
        lock = enum("lock_type_raw", "weapon_lock_type")
        semantics["fields"]["target_entity_id"] = {
            "raw": fields.get("target_entity_id"),
            "role": "lock_target" if lock == "LOCK_TARGET" else "reset_to_zero_by_consumer" if lock is not None else "unknown",
        }
        semantics["evidence_refs"] = ["declarations.weapon", "declarations.locks", "consumption_chains.weapon_callback", "consumption_chains.weapon_command", "consumption_chains.weapon_consumer"]
        semantics["diagnostic"].append("Weapon lock state is not a firing or damage event")
    elif kind == 0x32:
        axis = enum("key_raw", "cruise_axis")
        raw = fields.get("value_raw")
        table = "cruise_speed_state" if axis == "speedCruise" else "cruise_angle_state" if axis == "angleCruise" else None
        if table:
            fraction = mappings[table].get(str(raw))
            semantics["fields"]["value_raw"] = {"raw": raw, "state_table": table, "target_fraction": fraction}
            if fraction is None:
                semantics["status"] = "partial"
                semantics["diagnostic"].append(f"Unknown {table} state: {raw!r}")
        else:
            semantics["fields"]["value_raw"] = {"raw": raw, "target_fraction": None}
            semantics["status"] = "partial"
            semantics["diagnostic"].append("Depth/unknown cruise state requires runtime state-table evidence")
        semantics["evidence_refs"] = ["declarations.cruise", "declarations.movement", "consumption_chains.cruise_consumer", "consumption_chains.movement_bindings", "consumption_chains.movement_state_lookup"]
        semantics["diagnostic"].append("Requested control fraction is not measured speed or rudder angle")
    elif kind == 0x33:
        semantics["fields"] = {"entity_id": {"raw": fields.get("entity_id"), "role": "shot_owner_entity_id"},
                               "tracking_value_raw": {"raw": fields.get("tracking_value_raw"), "role": "shot_id"}}
        semantics["tracking_lookup_key"] = [fields.get("entity_id"), fields.get("tracking_value_raw")]
        semantics["evidence_refs"] = ["consumption_chains.tracking_callback"]
        semantics["diagnostic"].append("Camera shot-tracking lookup; does not establish firing, hit, or successful lookup")
    return output
