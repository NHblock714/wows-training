"""Evidence-bound decoding of packet types absent from the legacy collector.

Decoded means a checked wire layout, not a complete tactical interpretation.
Known-opaque packets retain identified structure and unresolved fields. They
must not be counted as fully decoded by a caller's capability gate.
The existing 0x2c handler is deliberately outside this module.
"""

from __future__ import annotations

import hashlib
import math
import struct

from .contracts import parse_version


UPSTREAM_COMMIT = "9ab2388002f3075d0bc85c2d7c879679f7f55383"
UPSTREAM_URL = f"https://github.com/landaire/wows-toolkit/blob/{UPSTREAM_COMMIT}/crates/wows-replays/src/packet2.rs"
SUPPORTED_VERSIONS = {(15, 3, 0), (15, 9, 0)}
PACKET_SPECS = {
    0x0E: ("server_tick_interval", 8, 1084, 1087),
    0x0F: ("server_timestamp", 8, 1078, 1081),
    0x10: ("initialization_flag", 1, 1439, 1442),
    0x13: ("initialization_marker", 0, 1443, 1446),
    0x18: ("gun_marker", 52, 1117, 1132),
    0x1D: ("player_network_statistics", 4, 272, 307),
    0x20: ("own_ship", 4, 1090, 1093),
    0x25: ("camera_state", 60, 1022, 1043),
    0x26: ("base_player_create_stub", None, 1197, 1219),
    0x27: ("camera_mode", 4, 978, 981),
    0x28: ("map", None, 1374, 1386),
    0x2A: ("non_volatile_entity_position", 32, 1055, 1065),
    0x2F: ("camera_freelook", 1, 983, 986),
    0x30: ("weapon_lock", 12, 1095, 1103),
    0x32: ("cruise_state", 8, 1368, 1371),
    0x33: ("shot_tracking", 12, 351, 360),
}
AUDIT_KINDS = frozenset(PACKET_SPECS) | {0xFFFFFFFF}


def _finite(values):
    if any(not math.isfinite(x) for x in values):
        raise ValueError("Non-finite floating-point field")


def _map(payload: bytes) -> dict:
    if len(payload) < 217:
        raise ValueError("Map payload shorter than fixed fields and 65-byte tail")
    name_size = struct.unpack_from("<I", payload, 148)[0]
    if name_size > 4096 or len(payload) != 152 + name_size + 65:
        raise ValueError("Map name length or tail boundary mismatch")
    name = payload[152:152 + name_size].decode("utf-8")
    if not name or "\x00" in name:
        raise ValueError("Invalid Map name")
    tail = payload[152 + name_size:]
    matrix = list(struct.unpack_from("<16f", tail))
    _finite(matrix)
    words = struct.unpack_from("<5I", payload)
    return {
        "space_id_raw": words[0],
        # Upstream notes warn that the i64 arena binding may be misaligned.
        "unresolved_header_words_u32": list(words[1:]),
        "blob_length": 128, "blob_sha256": hashlib.sha256(payload[20:148]).hexdigest(),
        "map_name": name,
        "matrix_f32_row_sequence": matrix,
        "matrix_is_identity": matrix == [1.0 if i % 5 == 0 else 0.0 for i in range(16)],
        "tail_flag_raw": tail[64], "tail_offset": 152 + name_size,
        "tail_length": 65, "tail_hex": tail.hex(),
        "tail_sha256": hashlib.sha256(tail).hexdigest(),
        "projection_semantics_validated": False,
    }


def decode_packet(kind: int, payload: bytes, version: str = "15.9.0") -> dict:
    """Decode an entire payload or retain an explicit opaque/unknown result.

No entity or clock state is mutated. In particular, camera coordinates cannot
be inserted into ship tracks by calling this function.
"""
    result = {
        "kind": kind, "classification": "unknown", "name": "unidentified_packet",
        "fields": {}, "diagnostic": [], "sources": [], "consumed_bytes": 0,
        "payload_length": len(payload), "payload_sha256": hashlib.sha256(payload).hexdigest(),
    }
    spec = PACKET_SPECS.get(kind)
    if spec is None:
        result["diagnostic"].append("No evidence-backed decoder; retain raw payload and offset")
        return result
    name, size, start, end = spec
    result["name"] = name
    result["sources"] = [f"{UPSTREAM_URL}#L{start}-L{end}"]
    try:
        if parse_version(version)[:3] not in SUPPORTED_VERSIONS:
            raise ValueError("Packet layout has not been validated for this replay version")
        if size is not None and len(payload) != size:
            raise ValueError(f"Expected {size} bytes, received {len(payload)}")
        classification = "decoded"
        fields = {}
        if kind in (0x0E, 0x0F):
            value, = struct.unpack("<d", payload)
            _finite([value])
            if value < 0 or (kind == 0x0E and value == 0):
                raise ValueError("Negative timestamp or non-positive tick interval")
            if kind == 0x0E:
                fields = {"interval_seconds": value, "nominal_rate_hz": 1.0 / value}
                result["diagnostic"].append("Repeated timing metadata; packet count is not a unique server tick count")
            else:
                fields = {"timestamp_raw": value, "epoch": "unverified"}
                classification = "known_opaque"
                result["diagnostic"].append("Do not treat as Unix time, match start, or a cross-replay clock alignment")
        elif kind == 0x10:
            fields = {"flag_raw": payload[0]}
            if payload[0] != 0:
                classification = "known_opaque"
                result["diagnostic"].append("Nonzero initialization flag is not identified")
        elif kind == 0x13:
            fields = {"empty_marker": True}
        elif kind == 0x18:
            floats = struct.unpack("<13f", payload)
            _finite(floats)
            fields = {"target_point_engine": list(floats[:3]), "diameter_raw": floats[3],
                      "marker_position_engine": list(floats[4:7]), "marker_direction": list(floats[7:10]),
                      "arcade_marker_size_raw": floats[10], "spg_marker_params_raw": list(floats[11:13]),
                      "is_default_marker": floats == (0.0,) * 10 + (-1.0,) * 3}
            result["diagnostic"].append("Default marker values do not establish aim, target, dispersion, or hit location")
        elif kind == 0x1D:
            packed, = struct.unpack("<I", payload)
            fields = {"packed_raw": packed, "fps": packed & 255, "ping_ms": (packed >> 8) & 65535,
                      "is_lagging": bool((packed >> 24) & 1), "unknown_bits": packed >> 25}
            if fields["unknown_bits"]:
                classification = "known_opaque"
                result["diagnostic"].append("Previously unused network-stat bits are set")
        elif kind == 0x20:
            fields = {"entity_id": struct.unpack("<I", payload)[0]}
        elif kind == 0x25:
            values = struct.unpack("<8fI6f", payload)
            # Native recorder evidence binds offset 32 to uint32 tarKey.
            # Its bits must not be subjected to floating-point validity checks.
            _finite(values[:8] + values[9:])
            fields = {"rotation_quaternion_xyzw": list(values[:4]),
                      "camera_position_engine": list(values[4:7]), "fov_radians": values[7],
                      "auxiliary_word_raw_u32": values[8],
                      "reference_position_engine": list(values[9:12]), "reference_direction": list(values[12:15]),
                      "ship_track_source": False}
            classification = "known_opaque"
            result["diagnostic"].append("Uint32 auxiliary word uses the separate native semantics profile; camera/reference positions are not ship-track evidence")
        elif kind == 0x26:
            if len(payload) < 10:
                raise ValueError("Base-player stub shorter than 10-byte prefix")
            entity, entity_type, data_length = struct.unpack_from("<IHI", payload)
            if len(payload) != 10 + data_length:
                raise ValueError("Base-player stub declared data length mismatch")
            fields = {"entity_id": entity, "entity_type_id": entity_type, "data_length": data_length,
                      "data_sha256": hashlib.sha256(payload[10:]).hexdigest()}
            if data_length:
                classification = "known_opaque"
                result["diagnostic"].append("Nonempty stub property data has not been decoded")
        elif kind == 0x27:
            fields = {"mode_raw": struct.unpack("<I", payload)[0]}
            classification = "known_opaque"
            result["diagnostic"].append("Target-client camera-mode enumeration has not been verified")
        elif kind == 0x28:
            fields = _map(payload)
            classification = "known_opaque"
            result["diagnostic"].append("Header words, 128-byte blob, matrix use, and tail flag remain semantically unresolved")
        elif kind == 0x2A:
            entity, space, x, y, z, yaw, pitch, roll = struct.unpack("<II6f", payload)
            _finite([x, y, z, yaw, pitch, roll])
            fields = {"entity_id": entity, "space_id_raw": space,
                      "position_engine": [x, y, z], "rotation_radians": [yaw, pitch, roll],
                      "ship_track_source": False}
            result["diagnostic"].append("Resolve entity type and lifecycle before using as a position source; not automatically a ship")
        elif kind == 0x2F:
            fields = {"freelook_raw": payload[0]}
            if payload[0] not in (0, 1):
                classification = "known_opaque"
                result["diagnostic"].append("Freelook byte outside the observed boolean range")
        elif kind == 0x30:
            weapon, lock, target = struct.unpack("<III", payload)
            fields = {"weapon_type_raw": weapon, "lock_type_raw": lock, "target_entity_id": target}
            classification = "known_opaque"
            result["diagnostic"].append("Lock is not evidence of firing, hitting, or intent; enumeration values retained raw")
        elif kind == 0x32:
            key, value = struct.unpack("<Ii", payload)
            fields = {"key_raw": key, "value_raw": value}
            classification = "known_opaque"
            result["diagnostic"].append("Cruise key enumeration and command semantics require target-client validation")
        elif kind == 0x33:
            entity, value = struct.unpack("<Iq", payload)
            fields = {"entity_id": entity, "tracking_value_raw": value}
            classification = "known_opaque"
            result["diagnostic"].append("Tracking value meaning is unresolved; not a shot, hit, damage or target count")
        result.update(classification=classification, fields=fields, consumed_bytes=len(payload))
    except (ValueError, struct.error, UnicodeError) as exc:
        result["diagnostic"].append(f"Layout validation failed: {exc}")
    return result
