"""Read selected ship modules and upgrades from exact-version Vehicle creation packets."""
import struct
from collections import Counter


_PROPERTY_COUNTS = {"15.9.0.13357625": 54, "15.10.0.13533378": 55}
LOADOUT_SCHEMA_VERSION = 1


class _Reader:
    def __init__(self, data):
        self.data = memoryview(data)
        self.offset = 0

    def take(self, size):
        end = self.offset + size
        if end > len(self.data):
            raise ValueError("truncated ship configuration")
        chunk = self.data[self.offset:end]
        self.offset = end
        return chunk

    def integer(self, size):
        return int.from_bytes(self.take(size), "little")

    def count(self, size, limit):
        count = self.integer(size)
        if count > limit:
            raise ValueError("invalid ship configuration count")
        return count

    def ids(self, limit):
        count = self.count(4, limit)
        return list(struct.unpack(f"<{count}I", self.take(4 * count)))


def _vehicle_config(frame, version):
    count = _PROPERTY_COUNTS[version]
    payload = frame.payload
    if len(payload) < 43:
        return None
    entity, kind = struct.unpack_from("<IH", payload)
    if kind != 2 or not entity:
        return None
    try:
        wire = _Reader(memoryview(payload)[38:])
        if wire.integer(4) != len(payload) - 42 or wire.integer(1) != count:
            return None
        # Verified Vehicle.def order for both builds. The first 43 exposed values
        # have fixed widths, followed by two AA arrays, effects, sounds and shipConfig.
        for index, width in enumerate([1] * 19 + [2] * 8 + [4] * 15 + [8]):
            if wire.integer(1) != index:
                return None
            wire.take(width)
        if wire.integer(1) != 43:
            return None
        for _ in range(wire.count(1, 254)):
            wire.take(8 * wire.count(1, 254))
        if wire.integer(1) != 44:
            return None
        wire.take(2 * wire.count(1, 254))
        for index in (45, 46):
            if wire.integer(1) != index:
                return None
            for _ in range(wire.count(1, 254)):
                wire.take(2)
                wire.take(wire.count(1, 254))
                wire.take(wire.count(1, 254))
        if wire.integer(1) != 47:
            return None
        size = wire.integer(1)
        if size == 255:
            size = wire.integer(3)
        config = _Reader(wire.take(size))
        config.integer(4)  # format version, with layout selected by the exact replay build
        ship_id = config.integer(4)
        if size != 12 + 4 * config.integer(4):
            return None
        modules = config.ids(64)
        config.take(4)  # extra field following the modules
        if count == 54:
            upgrades = config.ids(32)
        else:
            upgrades = []
            for _ in range(config.count(4, 32)):
                selected = config.integer(4)
                options = config.ids(16)
                if selected and selected not in options:
                    return None
                upgrades.append(selected)
    except ValueError:
        return None
    if not ship_id:
        return None
    return entity, {"ship_id": ship_id, "unit_ids": [unit for unit in modules if unit],
                    "upgrade_ids": [upgrade for upgrade in upgrades if upgrade]}


def replay_loadout_pending(report):
    version = str(report.get("replay_version") or "").replace(",", ".")
    return (version in _PROPERTY_COUNTS and report.get("roster_matches_header") is True
            and bool(report.get("arena_id")) and not report.get("multiview")
            and (report.get("ship_loadouts") or {}).get("schema_version") != LOADOUT_SCHEMA_VERSION)


def read_ship_loadouts(report, frames):
    """Accept only matching roster entities and discard conflicts across reappearances."""
    version = str(report.get("replay_version") or "").replace(",", ".")
    entries = {}
    if version not in _PROPERTY_COUNTS:
        return entries
    roster = [row for row in report.get("roster") or [] if type(row.get("shipId")) is int]
    counts = Counter(row["shipId"] for row in roster)
    ships = {row["shipId"]: row.get("shipParamsId") for row in roster if counts[row["shipId"]] == 1}
    conflicts = set()
    for frame in frames:
        if frame.type != 5 or len(frame.payload) < 43:
            continue
        entity = struct.unpack_from("<I", frame.payload)[0]
        if entity not in ships or entity in conflicts:
            continue
        parsed = _vehicle_config(frame, version)
        if parsed is None:
            continue
        _, loadout = parsed
        prior = entries.get(entity, loadout)
        if loadout["ship_id"] != ships[entity] or loadout != prior:
            conflicts.add(entity)
            entries.pop(entity, None)
        else:
            entries[entity] = loadout
    return {"schema_version": LOADOUT_SCHEMA_VERSION, "version": version, "source": "Vehicle.shipConfig",
            "ships": {str(entity): loadout for entity, loadout in entries.items()}}
