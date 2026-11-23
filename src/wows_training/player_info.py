"""Arena field mapping read from the exact client's statically extracted schema."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def load_player_schema(path: Path, resource_manifest: Path) -> tuple[dict, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    resources = json.loads(resource_manifest.read_text(encoding="utf-8"))
    if data["source_archive_sha256"] != resources["scripts_zip_audit"]["sha256"]:
        raise ValueError("Player schema and entity resources come from different client archives")
    maps = {}
    for code, name in ((1, "player"), (2, "bot"), (3, "observer")):
        record = data["mappings"][name]
        mapping = {int(k): value for k, value in record["id_to_name"].items()}
        if set(mapping) != set(range(record["count"])) or len(set(mapping.values())) != len(mapping):
            raise ValueError(f"Invalid {name} field mapping")
        maps[code] = mapping
    return maps, {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "source_archive_sha256": data["source_archive_sha256"],
                  "field_counts": {name: data["mappings"][name]["count"] for name in ("player", "bot", "observer")}}


class ClientPlayersInfo:
    def __init__(self, mappings):
        self.mappings = mappings
        self._players = {}
        self.unknown_fields = []

    def create_or_update_players(self, players_info, players_type=1):
        mapping = self.mappings[players_type]
        for raw in players_info:
            record = {}
            unknown = {}
            for key, value in raw:
                name = mapping.get(key)
                if name is None:
                    unknown[str(key)] = value
                    self.unknown_fields.append({"player_type": players_type, "field_id": key})
                    continue
                if name in ("name", "realm", "clanTag") and isinstance(value, bytes):
                    value = value.decode("utf-8")
                record[name] = value
            if "id" not in record:
                raise ValueError("Arena player update has no verified id field")
            current = self._players.setdefault(record["id"], {})
            if current.get("_player_kind", players_type) != players_type:
                raise ValueError("Arena identity changes between player/bot/observer kinds")
            record["_player_kind"] = players_type
            current.update(record)
            if unknown:
                current.setdefault("_unknown_properties", {}).update(unknown)

    def get_info(self):
        return self._players

