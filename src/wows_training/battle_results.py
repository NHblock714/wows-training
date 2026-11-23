"""Only the common post-battle fields proven by the target client schema."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def load_result_schema(path: Path, resource_manifest: Path):
    schema = json.loads(path.read_text(encoding="utf-8"))
    manifest = json.loads(resource_manifest.read_text(encoding="utf-8"))
    if schema["source_archive_sha256"] != manifest["scripts_zip_audit"]["sha256"]:
        raise ValueError("Result schema comes from a different client archive")
    names = schema["COMMON_RESULTS"]
    if len(names) != schema["common_count"] or len(set(names)) != len(names):
        raise ValueError("Invalid common result schema")
    return names, {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                   "source_archive_sha256": schema["source_archive_sha256"]}


def decode_common_result(server_data: dict, names: list[str], arena_id: str | None) -> dict:
    values = server_data.get("commonList")
    if not isinstance(values, list) or len(values) != len(names):
        raise ValueError("Common result length differs from the verified schema")
    common = dict(zip(names, values))
    identities = [str(common["arena_id"])]
    if arena_id is not None:
        identities.append(str(arena_id))
    if "arenaUniqueID" in server_data:
        identities.append(str(server_data["arenaUniqueID"]))
    if len(set(identities)) != 1:
        raise ValueError("Post-battle result belongs to a different arena")
    if type(common["winner_team_id"]) is not int or common["winner_team_id"] not in (-1, 0, 1):
        raise ValueError("Result has no supported final winner value")
    if not isinstance(common["duration_sec"], (int, float)) or common["duration_sec"] < 0:
        raise ValueError("Invalid result duration")
    common["arena_id"] = str(common["arena_id"])
    return common

