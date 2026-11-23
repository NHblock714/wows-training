"""Conservative event-scoped identity resolution; naming rules are candidates."""
from __future__ import annotations

from copy import deepcopy
import re
from uuid import UUID, uuid4


def parse_kots_name(raw_name, event_key=None):
    """Preserve the entire middle fragment, including seat suffixes/underscores."""
    if not isinstance(raw_name, str):
        raise ValueError("Tournament name must be text")
    result = {"raw_name": raw_name, "status": "unmapped", "clan": None,
              "nickname_fragment": None, "suffix": None, "event_key": event_key,
              "truncated": False, "confirmed": False, "rule_version": 1}
    match = re.fullmatch(r"(?P<clan>[A-Za-z0-9]+)_(?P<name>.+)_(?P<suffix>KOTS\d+(?:AS|EU|NA))", raw_name)
    if match:
        result.update(status="candidate", clan=match["clan"], nickname_fragment=match["name"], suffix=match["suffix"])
        result["event_key"] = event_key or match["suffix"]
        result["event_matches_suffix"] = event_key is None or event_key == match["suffix"]
        return result
    parts = raw_name.split("_", 1)
    if len(parts) == 2 and parts[0] and parts[1] and (event_key or "_KOTS" in raw_name):
        result.update(status="candidate", clan=parts[0], nickname_fragment=parts[1], truncated=True)
        result["reason"] = "Incomplete naming pattern; no personal identity is inferred"
    return result


def new_player(display_name, aliases=None, accounts=None):
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValueError("Player display name is required")
    return {"player_id": str(uuid4()), "display_name": display_name,
            "aliases": list(aliases or []), "accounts": deepcopy(accounts or [])}


def _record(value):
    if not isinstance(value, dict):
        return {}
    result = deepcopy(value.get("payload", value))
    if not isinstance(result, dict):
        return {}
    result["_document_id"] = value.get("id", result.get("id"))
    revision = value.get("revision", result.get("revision", result.get("version", 1)))
    result["_revision"] = revision if type(revision) is int and revision > 0 else 0
    return result


def _current(values):
    grouped, plain = {}, []
    for value in values or []:
        row = _record(value)
        identity = row.get("_document_id")
        if not identity:
            plain.append(row)
        elif identity not in grouped or row["_revision"] > grouped[identity]["_revision"]:
            grouped[identity] = row
        elif row["_revision"] == grouped[identity]["_revision"] and row != grouped[identity]:
            plain.append(row)
    return list(grouped.values()) + plain


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def report_battle_id(report):
    explicit = report.get("battle_id") or (report.get("battle_annotation") or {}).get("battle_id")
    if explicit:
        return str(explicit)
    version = report.get("replay_version")
    if isinstance(version, str):
        parts = re.split(r"[.,_]", version)
        if len(parts) == 4 and all(part.strip().isdigit() for part in parts):
            version = ".".join(str(int(part.strip())) for part in parts)
            arena = report.get("arena_id")
            if str(arena).isdigit() and int(arena) > 0:
                return f"arena:{version}:{int(arena)}"
    return f"replay:{report.get('sha256', '')}"


def _account_id(value):
    return str(int(value)) if type(value) in (str, int) and str(value).isdigit() and int(value) > 0 else None


def _target(assignment, player):
    accounts = [row for row in player.get("accounts", []) if isinstance(row, dict) and row.get("confirmed") is not False]
    region = assignment.get("region")
    region = "na" if region == "com" else region
    account_id = _account_id(assignment.get("account_id"))
    nickname = assignment.get("personal_nickname")
    if region or account_id:
        matches = [row for row in accounts if (not region or row.get("region") == region)
                   and (not account_id or _account_id(row.get("account_id")) == account_id)]
    else:
        matches = accounts
    if not nickname and len(matches) == 1:
        chosen = matches[0]
        region = region or chosen.get("region")
        account_id = account_id or _account_id(chosen.get("account_id"))
        nickname = chosen.get("nickname") or chosen.get("personal_nickname")
    if not nickname or region not in ("asia", "eu", "na"):
        return None
    if assignment.get("account_id") not in (None, "") and account_id is None:
        return None
    return str(nickname), region, account_id


def identity_candidates(raw_name, players, event_key=None):
    """Name/alias matching supplies review candidates, including ambiguity."""
    parsed = parse_kots_name(raw_name, event_key)
    fragment = parsed.get("nickname_fragment")
    candidates = []
    if not fragment:
        return candidates
    for player in _current(players):
        identity = _uuid(player.get("player_id") or player.get("_document_id"))
        if not identity or player.get("active") is False:
            continue
        aliases = [player.get("display_name"), *player.get("aliases", [])]
        aliases += [account.get("nickname") for account in player.get("accounts", []) if isinstance(account, dict)]
        matches = [name for name in aliases if isinstance(name, str) and (
            name.casefold() == fragment.casefold() or (parsed["truncated"] and name.casefold().startswith(fragment.casefold())))]
        if matches:
            candidates.append({"player_id": identity, "player_revision": player["_revision"],
                               "matched_aliases": list(dict.fromkeys(matches)), "confirmed": False,
                               "reason": "truncated_prefix" if parsed["truncated"] else "name_or_alias"})
    return candidates


def effective_mappings(report, legacy_mappings, assignments, players):
    """Return P1-compatible rows plus explicit resolution provenance.

    Assignments match exact event and full tournament name. A nonempty battle_ids
    list restricts scope; an empty list means the whole event. An explicit scoped
    assignment takes precedence over legacy P1 mapping, while overlapping new
    assignments must agree. Only latest document revisions are considered.
    """
    event = report.get("event_key") or "unassigned"
    battle = report_battle_id(report)
    player_index = {}
    for player in _current(players):
        key = _uuid(player.get("player_id") or player.get("_document_id"))
        if key:
            player_index.setdefault(key, []).append(player)
    assignment_rows = _current(assignments)
    names = sorted({row.get("name") for row in report.get("roster", []) if isinstance(row.get("name"), str)})
    recorder = (report.get("metadata") or {}).get("playerName")
    if isinstance(recorder, str) and recorder not in names:
        names.append(recorder)
    result = []
    for name in names:
        applicable = []
        for row in assignment_rows:
            if row.get("deleted") or row.get("deleted_at") or row.get("active") is False:
                continue
            if row.get("event_key") != event or row.get("tournament_name") != name:
                continue
            scope = row.get("battle_ids", [])
            if isinstance(scope, list) and scope and battle not in scope:
                continue
            applicable.append(row)
        resolved, invalid = [], []
        for row in applicable:
            if row.get("confirmed") is not True:
                continue
            player_id = _uuid(row.get("player_id"))
            choices = player_index.get(player_id, [])
            target = _target(row, choices[0]) if (len(choices) == 1 and choices[0].get("active") is not False
                      and isinstance(row.get("battle_ids", []), list)) else None
            if target is None:
                invalid.append(row)
            else:
                resolved.append((player_id, target, row, choices[0]))
        identity_keys = {(value[0], value[1]) for value in resolved}
        metadata = {"version": 1, "event_key": event, "battle_id": battle,
                    "candidate": parse_kots_name(name, event), "player_candidates": identity_candidates(name, players, event), "assignment_versions": [
                        {"id": row.get("_document_id"), "revision": row.get("_revision")} for row in applicable]}
        output = {"event_key": event, "tournament_name": name, "personal_nickname": None,
                  "region": None, "account_id": None, "notes": "", "confirmed": False}
        if invalid or len(identity_keys) > 1:
            metadata.update(status="conflict", reason="Overlapping or invalid confirmed assignment", candidate_player_ids=sorted({item[0] for item in resolved}))
        elif len(identity_keys) == 1:
            player_id, target, row, player = resolved[0]
            output.update(personal_nickname=target[0], region=target[1], account_id=target[2], notes=row.get("notes", ""), confirmed=True)
            metadata.update(status="confirmed", player_id=player_id, player_revision=player["_revision"], source="assignment")
        elif applicable:
            metadata.update(status="candidate", reason="Unconfirmed assignment blocks legacy automatic inheritance")
        else:
            legacy = [row for row in legacy_mappings or [] if row.get("event_key") == event and row.get("tournament_name") == name
                      and row.get("confirmed") is not False and not row.get("deleted_at")]
            keys = {(row.get("personal_nickname"), row.get("region"), _account_id(row.get("account_id"))) for row in legacy if row.get("personal_nickname")}
            if len(keys) == 1:
                nickname, region, account_id = next(iter(keys))
                output.update(personal_nickname=nickname, region=region, account_id=account_id, notes=legacy[0].get("notes", ""), confirmed=True)
                metadata.update(status="confirmed", source="legacy_manual", legacy_versions=[row.get("version", 1) for row in legacy], player_id=None)
            elif len(keys) > 1:
                metadata.update(status="conflict", source="legacy_manual")
            else:
                metadata.update(status=metadata["candidate"]["status"], source="naming_candidate")
        output["resolved_identity"] = metadata
        result.append(output)
    return result
