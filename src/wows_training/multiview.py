"""Evidence-preserving multi-recording reports, never game-playable replays.

All public report clocks are a derived battle-elapsed axis. Original packet
clocks and offsets remain attached to each observation. No global offset,
position interpolation, damage attribution or nickname identity join is used.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math

from .contracts import parse_version
from .timebase import analyze_timebase


SCHEMA_VERSION = 1
ENGINE_VERSION = "wows-multiview-1"
SUPPORTED_VERSION = (15, 9, 0, 13357625)
TIME_MODEL = "multiview_aligned_battle_timer"
FRESHNESS_SECONDS = 3.0
POSITION_DISAGREEMENT_ENGINE = 10.0


class MultiviewError(ValueError):
    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _point(value):
    return isinstance(value, (list, tuple)) and len(value) == 2 and all(_number(v) for v in value)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _map_id(report):
    metadata = report.get("metadata") or {}
    value = metadata.get("mapName") or metadata.get("mapDisplayName")
    return value.replace("\\", "/").rstrip("/").split("/")[-1] if isinstance(value, str) else None


def _geometry(report):
    geometry = report.get("geometry_analysis") or {}
    projection = geometry.get("png_projection") or {}
    viewport = projection.get("viewport_bounds_engine") or {}
    texture = projection.get("texture_bounds_engine") or {}
    dims = projection.get("dimensions_pixels")
    valid = (geometry.get("png_alignment_verified") is True and not geometry.get("errors")
             and projection.get("status") == "source_chain_verified"
             and projection.get("mapping_scope") == "single_identity_mapping"
             and viewport.get("projection_convention") == "SpaceSizeUtils.native_max_min_return"
             and viewport.get("native_viewport_equality_verified") is True
             and isinstance(dims, list) and len(dims) == 2 and all(_number(v) and v > 0 for v in dims))
    for rectangle in (viewport, texture):
        valid = valid and all(_number(rectangle.get(k)) for k in ("min_x", "max_x", "min_z", "max_z"))
        if valid:
            valid = rectangle["max_x"] > rectangle["min_x"] and rectangle["max_z"] > rectangle["min_z"]
    if not valid:
        return None
    return {"viewport": {k: viewport[k] for k in ("min_x", "max_x", "min_z", "max_z")},
            "texture": {k: texture[k] for k in ("min_x", "max_x", "min_z", "max_z")},
            "dimensions": dims, "scale": geometry.get("meters_per_engine_unit"),
            "domain": geometry.get("distance_domain"), "map_id": projection.get("map_id")}


def _layout(report):
    rows = []
    for cap in report.get("capture_points") or []:
        if not isinstance(cap.get("name"), str) or not all(_number(cap.get(k)) for k in ("x", "z", "radius")):
            raise ValueError("Invalid capture-point layout")
        rows.append((cap["name"], round(cap["x"], 5), round(cap["z"], 5), round(cap["radius"], 5),
                     round(cap.get("inner_radius", 0), 5)))
    if len({row[0] for row in rows}) != len(rows):
        raise ValueError("Ambiguous capture-point names")
    return sorted(rows)


class _Clock:
    def __init__(self, report):
        observations = (report.get("time_analysis") or {}).get("observations") or []
        active = [row for row in observations if _number(row.get("battle_elapsed_sec"))]
        if (not active or any(row.get("stage_name") != "STARTED" or row.get("diagnostics") for row in active)
                or len({row.get("battle_segment_id") for row in active}) != 1
                or len({row.get("clock_segment_id") for row in active}) != 1
                or len({row.get("duration") for row in active}) != 1):
            raise ValueError("A single verified STARTED timer segment is required")
        self.duration = active[0].get("duration")
        if not _number(self.duration) or self.duration <= 0:
            raise ValueError("Invalid battle timer duration")
        self.rows = []
        self.by_tick = {}
        for row in active:
            clock, elapsed = row.get("packet_clock"), row["battle_elapsed_sec"]
            if (not _number(clock) or clock < 0 or elapsed < 0 or elapsed > self.duration
                    or not math.isclose(elapsed, round(elapsed), abs_tol=1e-8)
                    or not _number(row.get("timeLeft"))
                    or not math.isclose(self.duration - row["timeLeft"], elapsed, abs_tol=1e-8)):
                raise ValueError("Invalid or non-integer observed battle timer anchor")
            if self.rows and (clock <= self.rows[-1]["packet_clock"] or elapsed < self.rows[-1]["battle_elapsed_sec"]):
                raise ValueError("Ambiguous or regressing source timer")
            if int(elapsed) in self.by_tick:
                previous = self.by_tick[int(elapsed)]
                if clock - previous["packet_clock"] > 2:
                    raise ValueError("Repeated timer value spans an ambiguous interval")
            self.rows.append(row)
            self.by_tick[int(elapsed)] = row
        self.clocks = [row["packet_clock"] for row in self.rows]
        self.first = min(self.by_tick)
        self.last = max(self.by_tick)

    def interval(self, clock, offset=None, *, initial_state=False):
        if not _number(clock):
            return None
        if clock < self.clocks[0]:
            return {"bounds": [None, self.first], "display_clock": self.first,
                    "status": "prebattle_state_available_at_first_anchor"} if initial_state else None
        index = bisect_right(self.clocks, clock) - 1
        if index < 0:
            return None
        left = self.rows[index]
        if clock == left["packet_clock"] and (offset is None or type(left.get("packet_offset")) is not int or offset >= left["packet_offset"]):
            elapsed = left["battle_elapsed_sec"]
            return {"bounds": [elapsed, elapsed], "display_clock": elapsed, "status": "observed_anchor"}
        if clock == left["packet_clock"]:
            index -= 1
            if index < 0:
                return {"bounds": [None, self.first], "display_clock": self.first,
                        "status": "prebattle_state_available_at_first_anchor"} if initial_state else None
            left = self.rows[index]
        if index + 1 >= len(self.rows):
            return None
        right = self.rows[index + 1]
        if not left["packet_clock"] <= clock <= right["packet_clock"]:
            return None
        return {"bounds": [left["battle_elapsed_sec"], right["battle_elapsed_sec"]],
                "display_clock": right["battle_elapsed_sec"], "status": "bounded_interval"}


def analyze_pairing(reports):
    diagnostic = {"schema_version": SCHEMA_VERSION, "engine_version": ENGINE_VERSION,
                  "status": "incompatible", "can_merge": False, "source_shas": [], "battle_id": None,
                  "errors": [], "warnings": [], "checks": {}, "identity_maps": {}}
    def error(code, message, sha=None):
        diagnostic["errors"].append({"code": code, "message": message, "source_sha": sha})
    def warn(code, message, sha=None):
        diagnostic["warnings"].append({"code": code, "message": message, "source_sha": sha})
    if not isinstance(reports, (list, tuple)) or not reports:
        error("no_sources", "尚无可核对的回放报告。")
        return diagnostic
    unique = {}
    for report in reports:
        if not isinstance(report, dict) or report.get("multiview"):
            error("not_original_report", "只能合并原始单视角报告，不能再次合并派生结果。")
            continue
        sha = report.get("sha256")
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            error("invalid_source_sha", "原始回放 SHA256 缺失或无效。")
            continue
        if sha in unique:
            if _hash(unique[sha]) != _hash(report):
                error("same_sha_different_report", "同一原始 SHA 对应不同分析报告，请先明确分析版本。", sha)
            else:
                warn("duplicate_source", "重复输入已去重。", sha)
        unique[sha] = report
    ordered = sorted(unique.items())
    diagnostic["source_shas"] = [sha for sha, _ in ordered]
    if diagnostic["errors"]:
        return diagnostic
    if len(ordered) < 2:
        diagnostic["status"] = "pending_counterpart"
        warn("one_source", "已保留单侧来源，等待另一方回放。")
        return diagnostic
    reference_roster = None
    reference_map = reference_layout = reference_mode = reference_geometry = None
    reference_version = reference_arena = reference_duration = None
    canonical_winners, host_teams, clocks = set(), set(), {}
    for sha, report in ordered:
        try:
            version = parse_version(report.get("replay_version", ""))
            if version != SUPPORTED_VERSION:
                raise ValueError("Only the exact audited client build is supported")
        except (ValueError, AttributeError):
            error("unsupported_version", "回放不是已验证的完整 15.9.0.13357625 版本。", sha)
            continue
        arena = str(report.get("arena_id") or "")
        if not arena or arena == "0":
            error("missing_arena", "缺少服务器比赛 ID。", sha)
        if (report.get("packet_errors") or report.get("framing_error") or report.get("unknown_player_fields")
                or report.get("handled_callback_exceptions") or report.get("post_battle_result_errors")
                or report.get("roster_matches_header") is False
                or (report.get("combat") or {}).get("diagnostics", {}).get("errors")
                or (report.get("combat") or {}).get("diagnostics", {}).get("truncated")):
            error("source_validation_failed", "来源存在关键解析错误、名单不一致或战斗采集截断。", sha)
        roster = {}
        entity_ids = set()
        for player in report.get("roster") or []:
            account, entity, params, team = (player.get(k) for k in ("accountDBID", "shipId", "shipParamsId", "teamId"))
            if (type(account) is not int or account <= 0 or account in roster or type(entity) is not int
                    or entity <= 0 or entity in entity_ids or type(params) is not int or params <= 0
                    or type(team) is not int or team not in (0, 1)):
                error("invalid_identity", "需要完整、唯一的账号 ID、舰船实体和舰船参数；不能按昵称猜测。", sha)
                break
            roster[account] = player
            entity_ids.add(entity)
        if not roster or {p["teamId"] for p in roster.values()} != {0, 1}:
            error("incomplete_roster", "完整双方名单缺失。", sha)
        try:
            clock = _Clock(report)
            clocks[sha] = clock
        except (ValueError, TypeError, KeyError):
            error("ambiguous_timebase", "来源没有唯一、有效的比赛计时锚点，不能使用固定时差代替。", sha)
            clock = None
        try:
            layout = _layout(report)
        except (ValueError, TypeError, KeyError):
            error("invalid_layout", "占领点布局不能唯一核对。", sha)
            layout = None
        map_id = _map_id(report)
        if not map_id:
            error("missing_map", "缺少地图标识。", sha)
        mode = {k: (report.get("metadata") or {}).get(k) for k in ("matchGroup", "scenario", "gameMode", "playersPerTeam")}
        geometry = _geometry(report)
        if geometry is None:
            warn("geometry_unavailable", "此来源地图几何未验证，其位置不参与融合。", sha)
        if reference_roster is None:
            reference_roster = roster
            reference_map, reference_layout, reference_mode = map_id, layout, mode
            reference_version, reference_arena = version, arena
            reference_duration = clock.duration if clock else None
        if geometry is not None:
            if reference_geometry is not None and geometry != reference_geometry:
                error("geometry_conflict", "地图坐标、投影或单位证据冲突。", sha)
            reference_geometry = geometry
        if version != reference_version or arena != reference_arena:
            error("battle_identity_conflict", "版本或比赛 ID 不同，不能合并。", sha)
        if map_id != reference_map or layout != reference_layout or mode != reference_mode:
            error("layout_conflict", "地图、模式或占领点布局不一致。", sha)
        if not layout:
            warn("empty_capture_layout", "双方未记录占领点，缺少布局交叉验证。", sha)
        if clock and reference_duration is not None and clock.duration != reference_duration:
            error("timer_duration_conflict", "双方 BattleLogic 计时长度不一致。", sha)
        if set(roster) != set(reference_roster):
            error("account_roster_conflict", "双方完整账号名单不同。", sha)
            continue
        if any(roster[a]["shipParamsId"] != reference_roster[a]["shipParamsId"] for a in roster):
            error("ship_identity_conflict", "相同账号的舰船参数不一致。", sha)
        team_map = {}
        for team in (0, 1):
            mapped = {reference_roster[a]["teamId"] for a in roster if roster[a]["teamId"] == team}
            if len(mapped) != 1:
                error("team_membership_conflict", "账号所属队伍不能形成可靠双射。", sha)
            else:
                team_map[team] = next(iter(mapped))
        if set(team_map.values()) != {0, 1}:
            error("team_mapping_conflict", "队伍编号不能可靠映射。", sha)
        entity_map = {p["shipId"]: reference_roster[a]["shipId"] for a, p in roster.items()}
        diagnostic["identity_maps"][sha] = {"entity_to_canonical": {str(k): v for k, v in entity_map.items()},
            "team_to_canonical": {str(k): v for k, v in team_map.items()},
            "account_to_canonical": {str(a): reference_roster[a]["shipId"] for a in roster},
            "identity_basis": "accountDBID_and_shipParamsId; complete_team_membership_bijection"}
        host = report.get("host_absolute_team")
        if type(host) is not int or host not in team_map:
            error("unknown_recorder_side", "录制者所属侧未经验证。", sha)
        else:
            host_teams.add(team_map[host])
        winner = report.get("winner_absolute_team")
        if winner == -1:
            canonical_winners.add(-1)
        elif type(winner) is int and winner in team_map:
            canonical_winners.add(team_map[winner])
        else:
            warn("winner_missing", "此来源结算胜方未知；不会由录像长短推断胜负。", sha)
        if any(p.get("name") != reference_roster[a].get("name") for a, p in roster.items()):
            warn("name_variation", "同账号显示名有差异；身份仍按账号映射。", sha)
        death_ids = [d.get("victim_entity_id") for d in report.get("death_events") or []]
        if len(death_ids) != len(set(death_ids)):
            error("repeated_death_identity", "同一舰船有多条死亡记录，本合并器不支持未区分的复活轮次。", sha)
    if len(canonical_winners) > 1:
        error("winner_conflict", "可靠映射后的结算胜方冲突。")
    if reference_arena:
        diagnostic["battle_id"] = "arena:" + ".".join(map(str, reference_version)) + ":" + reference_arena
    ticks = [set(clock.by_tick) for clock in clocks.values()]
    common_ticks = set.intersection(*ticks) if len(ticks) == len(ordered) and ticks else set()
    diagnostic["checks"] = {"source_count": len(ordered), "canonical_host_teams": sorted(host_teams),
        "common_timer_ticks": len(common_ticks), "map_id": reference_map,
        "roster_count": len(reference_roster or {}), "winner_absolute_team": next(iter(canonical_winners), None)}
    if not common_ticks:
        error("no_shared_timer_anchor", "来源之间没有相同比赛秒的真实计时锚点。")
    if diagnostic["errors"]:
        return diagnostic
    if host_teams != {0, 1}:
        diagnostic["status"] = "pending_counterpart"
        warn("same_side_sources", "已识别来源，但尚无另一方的录制视角。")
        return diagnostic
    diagnostic.update(status="mergeable", can_merge=True)
    return diagnostic


class _Source:
    def __init__(self, report, identity):
        self.report, self.sha = report, report["sha256"]
        self.clock = _Clock(report)
        self.entities = {int(k): v for k, v in identity["entity_to_canonical"].items()}
        self.reverse_entities = {v: k for k, v in self.entities.items()}
        self.teams = {int(k): v for k, v in identity["team_to_canonical"].items()}
        self.host_team = self.teams[report["host_absolute_team"]]
        self.geometry = _geometry(report)
        self.ships = (report.get("combat") or {}).get("ships") or {}
        self.samples = {int(e): sorted(ship.get("samples", []), key=lambda s: s["clock"])
                        for e, ship in self.ships.items()}
        self.sample_clocks = {e: [s["clock"] for s in rows] for e, rows in self.samples.items()}

    def latest(self, canonical_entity, tick):
        anchor = self.clock.by_tick.get(tick)
        entity = self.reverse_entities.get(canonical_entity)
        if anchor is None or entity not in self.samples:
            return None
        index = bisect_right(self.sample_clocks[entity], anchor["packet_clock"]) - 1
        if index < 0:
            return None
        # Samples from later packets sharing a clock are not yet observed at the
        # timer property's final packet. A complete prior snapshot remains usable.
        while index >= 0:
            row = self.samples[entity][index]
            if (row["clock"] < anchor["packet_clock"] or type(row.get("packet_offset")) is not int
                    or type(anchor.get("packet_offset")) is not int or row["packet_offset"] <= anchor["packet_offset"]):
                break
            index -= 1
        return (self.samples[entity][index], index, anchor, entity) if index >= 0 else None

    def provenance(self, original, *, raw_clock=None, entity=None, initial_state=False):
        original = original if isinstance(original, dict) else {}
        raw_clock = raw_clock if _number(raw_clock) else original.get("packet_clock", original.get("clock"))
        interval = self.clock.interval(raw_clock, original.get("packet_offset"), initial_state=initial_state)
        if interval is None:
            return None
        return {"source_sha": self.sha, "recording_sha256": self.sha,
                "clock": interval["display_clock"], "packet_clock": interval["display_clock"],
                "clock_domain": "battle_elapsed_seconds", "packet_offset": None,
                "original_packet_clock": raw_clock, "original_packet_offset": original.get("packet_offset"),
                "original_packet_type": original.get("packet_type"), "original_entity_id": entity,
                "field": original.get("field"), "battle_time_interval": interval["bounds"],
                "alignment_status": interval["status"]}


class _Conflicts:
    def __init__(self):
        self.rows = []
        self.counts = Counter()
        self.per_tick = Counter()
    def add(self, kind, *, clock=None, **details):
        self.counts[kind] += 1
        if _number(clock):
            self.per_tick[clock] += 1
        if len(self.rows) < 1000:
            self.rows.append(dict(kind=kind, clock=clock, **details))


def _ordered_sources(sources, team=None):
    return sorted(sources, key=lambda source: (source.host_team != team, -len(source.clock.by_tick), source.sha))


def _death_events(sources, conflicts, unaligned):
    by_victim = defaultdict(list)
    for source in sources:
        normalized = source.report.get("normalized_death_times") or []
        offset_lookup = {(r.get("event", {}).get("clock"), r.get("event", {}).get("victim_entity_id")):
                         r.get("source", {}) for r in normalized}
        for index, event in enumerate(source.report.get("death_events") or []):
            victim = source.entities.get(event.get("victim_entity_id"))
            provenance = source.provenance(offset_lookup.get((event.get("clock"), event.get("victim_entity_id"))),
                                            raw_clock=event.get("clock"), entity=event.get("victim_entity_id"))
            if victim is None or provenance is None:
                unaligned["death"] += 1
                continue
            by_victim[victim].append({"victim_entity_id": victim,
                "killer_entity_id": source.entities.get(event.get("killer_entity_id")),
                "type_id": event.get("type_id"), "source": provenance,
                "original_report_pointer": f"/death_events/{index}"})
    events = []
    for victim, records in sorted(by_victim.items()):
        lower = min(r["source"]["battle_time_interval"][0] for r in records)
        upper = max(r["source"]["battle_time_interval"][1] for r in records)
        killers, types = {r["killer_entity_id"] for r in records}, {r["type_id"] for r in records}
        disjoint = max(r["source"]["battle_time_interval"][0] for r in records) > min(r["source"]["battle_time_interval"][1] for r in records)
        if len(killers) > 1 or len(types) > 1 or disjoint:
            conflicts.add("death_disagreement", clock=upper, entity_id=victim, sources=records)
        events.append({"clock": upper, "victim_entity_id": victim,
            "killer_entity_id": next(iter(killers)) if len(killers) == 1 else None,
            "type_id": next(iter(types)) if len(types) == 1 else None,
            "battle_time_interval": [lower, upper], "sources": [r["source"] for r in records],
            "source": records[0]["source"], "deduplication": "same_canonical_victim_single_life_records"})
    return sorted(events, key=lambda row: (row["clock"], row["victim_entity_id"]))


def _position_candidate(source, state):
    row, index, anchor, original_entity = state
    point = row.get("minimap_position")
    heading_clock, visibility_clock = row.get("heading_clock"), row.get("visibility_clock")
    current = anchor["packet_clock"]
    valid = (source.geometry is not None and _point(point) and row.get("visible_to_recorder") is True
             and _number(heading_clock) and _number(visibility_clock)
             and 0 <= current - heading_clock < FRESHNESS_SECONDS
             and 0 <= current - visibility_clock < FRESHNESS_SECONDS)
    if not valid:
        return None
    provenance = source.provenance(row.get("heading_source"), raw_clock=heading_clock, entity=original_entity, initial_state=True)
    if provenance is None:
        return None
    provenance["original_report_pointer"] = f"/combat/ships/{original_entity}/samples/{index}"
    provenance["original_age_at_anchor_seconds"] = current - heading_clock
    return {"source": source, "row": row, "point": point, "provenance": provenance}


def _field_candidate(source, state, field, clock_key, source_key):
    row, index, anchor, original_entity = state
    value, raw_clock = row.get(field), row.get(clock_key)
    if value is None or not _number(raw_clock) or raw_clock > anchor["packet_clock"]:
        return None
    provenance = source.provenance(row.get(source_key), raw_clock=raw_clock,
                                  entity=original_entity, initial_state=True)
    if provenance is None:
        return None
    provenance["original_report_pointer"] = f"/combat/ships/{original_entity}/samples/{index}/{field}"
    return value, provenance, row


def _png(point, geometry):
    texture, dims = geometry["texture"], geometry["dimensions"]
    x, z = point
    return [(x - texture["min_x"]) / (texture["max_x"] - texture["min_x"]) * dims[0],
            (texture["max_z"] - z) / (texture["max_z"] - texture["min_z"]) * dims[1]]


def _compact_source(proof):
    """A raw SHA + packet offset identifies the evidence without copied frames."""
    value = {key: proof.get(key) for key in ("source_sha", "original_packet_clock", "original_packet_offset", "battle_time_interval")}
    if proof.get("alignment_status") == "prebattle_state_available_at_first_anchor":
        value["alignment_status"] = proof["alignment_status"]
    return value


def _merge_ships(sources, roster, ticks, deaths, conflicts):
    ships, tracks, frames = {}, {}, []
    by_source = {s.sha: {"anchor_seconds": len(s.clock.by_tick), "available_position_samples": 0,
                        "selected_position_samples": 0} for s in sources}
    death_times = {row["victim_entity_id"]: row["clock"] for row in deaths}
    for player in roster:
        entity = player["shipId"]
        ships[str(entity)] = {"entity_id": entity, "max_hp": player.get("maxHealth"),
                              "max_hp_source": "canonical_roster", "samples": []}
        tracks[entity] = {"entity_id": entity, "columns": ["packet_clock", "x_engine", "z_engine", "png_x", "png_y"],
                          "segments": [], "clock_domain": "battle_elapsed_seconds"}
    last_track_tick = {}
    for tick in ticks:
        available = Counter()
        selected = Counter()
        unknown = []
        alive_count = 0
        for player in roster:
            entity, team = player["shipId"], player["teamId"]
            is_dead = death_times.get(entity, math.inf) <= tick
            alive_count += not is_dead
            preferences = _ordered_sources(sources, team)
            states = {source.sha: source.latest(entity, tick) for source in preferences}
            candidates = [value for source in preferences if states[source.sha] is not None
                          and (value := _position_candidate(source, states[source.sha])) is not None]
            visibility = {}
            for source in preferences:
                state = states[source.sha]
                if state is None:
                    visibility[source.sha] = {"status": "unknown", "reason": "no_exact_source_timer_anchor"}
                    continue
                row, _, anchor, original_entity = state
                stamp = row.get("visibility_clock")
                visible = row.get("visible_to_recorder")
                fresh = _number(stamp) and 0 <= anchor["packet_clock"] - stamp < FRESHNESS_SECONDS
                visibility[source.sha] = {"status": "visible" if visible is True and fresh else
                    "not_visible_recorded" if visible is False else "unknown",
                    "visible_to_recorder": visible if fresh or visible is False else None,
                    "original_packet_clock": stamp, "canonical_team_id": source.host_team}
            chosen = candidates[0] if candidates and not is_dead else None
            for candidate in candidates:
                available[candidate["source"].sha] += not is_dead
                by_source[candidate["source"].sha]["available_position_samples"] += not is_dead
            if len(candidates) > 1 and not is_dead:
                for alternative in candidates[1:]:
                    separation = math.dist(candidates[0]["point"], alternative["point"])
                    if separation > POSITION_DISAGREEMENT_ENGINE:
                        conflicts.add("position_disagreement", clock=tick, entity_id=entity,
                            distance_engine=separation, selected_source=chosen["source"].sha,
                            candidates=[{"source_sha": c["source"].sha, "point": c["point"],
                                         "source": c["provenance"]} for c in (candidates[0], alternative)])
            sample = {"clock": tick, "packet_clock": tick, "clock_domain": "battle_elapsed_seconds",
                      "global_observed": chosen is not None, "visible_to_recorder": chosen is not None,
                      "visibility_semantics": "global_union_not_any_team_vision", "visibility_clock": tick,
                      "visibility_by_source": visibility, "observed_by_sources": [c["source"].sha for c in candidates] if not is_dead else [],
                      "hp": None, "hp_clock": None, "heading_radians": None, "heading_clock": None,
                      "spotted_by_enemy": None, "spotted_clock": None, "is_alive": False if is_dead else None,
                      "quality": "multi_source_observed_state"}
            if chosen is not None:
                source = chosen["source"]
                selected[source.sha] += 1
                by_source[source.sha]["selected_position_samples"] += 1
                sample.update(minimap_position=list(chosen["point"]), source_sha=source.sha,
                    source_original_packet_clock=chosen["provenance"]["original_packet_clock"],
                    alignment_provenance={"original_anchor_packet_clock": source.clock.by_tick[tick]["packet_clock"],
                                          "original_anchor_packet_offset": source.clock.by_tick[tick].get("packet_offset")},
                    visibility_source=_compact_source(chosen["provenance"]),
                    heading_radians=chosen["row"].get("heading_radians"),
                    heading_convention=chosen["row"].get("heading_convention"), heading_clock=tick,
                    heading_source=_compact_source(chosen["provenance"]))
                track = tracks[entity]
                if last_track_tick.get(entity) != tick - 1:
                    track["segments"].append([])
                track["segments"][-1].append([tick, *chosen["point"], *_png(chosen["point"], source.geometry)])
                last_track_tick[entity] = tick
            elif not is_dead:
                unknown.append(entity)
            for field, stamp_key, source_key in (("hp", "hp_clock", "hp_source"),
                    ("spotted_by_enemy", "spotted_clock", "spotted_source"), ("is_alive", "alive_clock", "alive_source")):
                values = [(source, value) for source in preferences if states[source.sha] is not None
                          and (value := _field_candidate(source, states[source.sha], field, stamp_key, source_key)) is not None]
                if not values:
                    continue
                source, (value, provenance, original) = values[0]
                if field == "is_alive" and is_dead:
                    value = False
                sample[field], sample[stamp_key], sample[source_key] = value, provenance["clock"], _compact_source(provenance)
                if field == "spotted_by_enemy":
                    sample["visibility_flags_raw"] = original.get("visibility_flags_raw")
                if field == "hp" and any(other[1][0] != value for other in values[1:]):
                    conflicts.add("hp_disagreement", clock=tick, entity_id=entity, selected_source=source.sha,
                                  candidates=[{"source_sha": s.sha, "hp": v[0], "source": v[1]} for s, v in values])
            ships[str(entity)]["samples"].append(sample)
        frames.append({"clock": tick, "roster_count": len(roster), "expected_alive_count": alive_count,
            "position_count": alive_count - len(unknown), "unknown_entity_ids": unknown,
            "selected_source_counts": dict(selected), "available_source_counts": dict(available),
            "anchor_source_shas": [s.sha for s in sources if tick in s.clock.by_tick], "conflicts_at_tick": 0})
    return ships, list(tracks.values()), frames, by_source


def _convert_projectile(source, row, kind, index, unaligned):
    owner = source.entities.get(row.get("owner_entity_id"))
    start = source.provenance(row.get("source"), raw_clock=row.get("start_clock"), entity=row.get("owner_entity_id"))
    if owner is None or start is None:
        unaligned[kind] += 1
        return None
    record = deepcopy(row)
    record.update(id=f"{source.sha[:16]}:{kind}:{row.get('id', index)}", owner_entity_id=owner,
                  start_clock=start["clock"], source=start, source_sha=source.sha,
                  battle_time_interval=start["battle_time_interval"], clock_domain="battle_elapsed_seconds",
                  source_original_id=row.get("id"), original_start_clock=row.get("start_clock"),
                  interpolation_allowed=False)
    end = source.provenance(row.get("end_source"), raw_clock=row.get("end_clock"), entity=row.get("owner_entity_id"))
    record.update(end_clock=end["clock"] if end else None, end_source=end,
                  end_time_interval=end["battle_time_interval"] if end else None,
                  original_end_clock=row.get("end_clock"))
    if end is None:
        record["end"] = None
    record["samples"] = []
    for sample in row.get("samples") or []:
        proof = source.provenance(sample.get("source"), raw_clock=sample.get("clock"), entity=row.get("owner_entity_id"))
        if proof:
            record["samples"].append(dict(deepcopy(sample), clock=proof["clock"], source=proof,
                                           battle_time_interval=proof["battle_time_interval"]))
    if _number(row.get("armed_clock")):
        armed = source.provenance(row.get("armed_source"), raw_clock=row["armed_clock"], entity=row.get("owner_entity_id"))
        record["armed_clock"], record["armed_source"] = (armed["clock"], armed) if armed else (None, None)
    return record


def _merge_owner_events(sources, roster, kind, unaligned):
    result, policies = [], []
    for player in roster:
        entity, team = player["shipId"], player["teamId"]
        options = []
        for source in _ordered_sources(sources, team):
            raw_id = source.reverse_entities[entity]
            owner_key = "entity_id" if kind == "consumables" else "owner_entity_id"
            rows = [(i, row) for i, row in enumerate((source.report.get("combat") or {}).get(kind) or [])
                    if row.get(owner_key) == raw_id]
            options.append((source, rows))
        # Choose one stream per owner/category. Incomplete alternate streams stay
        # in the original reports/package, never added as duplicate ammunition.
        owner_options = [option for option in options if option[0].host_team == team]
        chosen, rows = max(owner_options or options, key=lambda option: (len(option[1]), len(option[0].clock.by_tick), option[0].sha))
        policies.append({"entity_id": entity, "category": kind, "selected_source_sha": chosen.sha,
                         "selection": "owner_team_stream" if owner_options else "available_source_stream",
                         "source_record_counts": {s.sha: len(records) for s, records in options},
                         "alternate_records_not_added": sum(len(records) for s, records in options if s.sha != chosen.sha)})
        for index, row in rows:
            if kind != "consumables":
                record = _convert_projectile(chosen, row, kind, index, unaligned)
            else:
                proof = chosen.provenance(row.get("source"), raw_clock=row.get("start_clock"), entity=row.get("entity_id"))
                if proof is None:
                    unaligned[kind] += 1
                    continue
                record = deepcopy(row)
                record.update(id=f"{chosen.sha[:16]}:consumable:{row.get('id', index)}", entity_id=entity,
                              start_clock=proof["clock"], source=proof, source_sha=chosen.sha,
                              battle_time_interval=proof["battle_time_interval"], original_start_clock=row.get("start_clock"),
                              clock_domain="battle_elapsed_seconds")
                for key, source_key in (("end_clock", "end_source"), ("expected_end_clock", None)):
                    interval = chosen.provenance(row.get(source_key) if source_key else {}, raw_clock=row.get(key), entity=row.get("entity_id"))
                    record["original_" + key] = row.get(key)
                    record[key] = interval["clock"] if interval else None
                    record[key + "_interval"] = interval["battle_time_interval"] if interval else None
                    if source_key:
                        record[source_key] = interval
                record["expected_end_semantics"] = "reported_timer_endpoint_mapped_only_within_observed_anchors"
            if record is not None:
                result.append(record)
    return sorted(result, key=lambda row: (row["start_clock"], row["id"])), policies


def _merge_state_stream(sources, ticks, entries, conflicts, category):
    """Resolve non-additive replicated state; disagreements remain explicitly null."""
    prepared = {}
    provenance = []
    for source in sources:
        rows = []
        for raw_clock, value, original_id in entries(source):
            proof = source.provenance({}, raw_clock=raw_clock, entity=original_id, initial_state=True)
            if proof:
                rows.append((proof["clock"], value, proof))
        prepared[source.sha] = sorted(rows, key=lambda row: row[0])
    output, previous = [], object()
    for tick in ticks:
        candidates = []
        for source in sources:
            rows = prepared[source.sha]
            index = bisect_right(rows, tick, key=lambda row: row[0]) - 1
            if index >= 0:
                candidates.append(rows[index])
        if not candidates:
            continue
        newest = max(row[0] for row in candidates)
        current = [row for row in candidates if row[0] == newest]
        values = {row[1] for row in current}
        value = next(iter(values)) if len(values) == 1 else None
        if len(values) > 1:
            conflicts.add(category + "_disagreement", clock=tick,
                          candidates=[{"value": row[1], "source": row[2]} for row in current])
        if value != previous:
            output.append([tick, value])
            provenance.append({"clock": tick, "value": value, "sources": [row[2] for row in current]})
            previous = value
    return output, provenance


def _merge_planes(sources,unaligned):
    records=[];policies=[]
    for team in (0,1):
        options=[]
        for source in sources:
            combat=source.report.get('combat') or {}
            rows=[r for r in combat.get('planes',[]) if source.teams.get(r.get('team_id'))==team] if combat.get('plane_observation_schema')==1 else []
            if rows:options.append((source,rows))
        if not options:continue
        own=[item for item in options if item[0].host_team==team]
        chosen,rows=max(own or options,key=lambda item:(sum(len(r.get('samples',[])) for r in item[1]),item[0].sha))
        policies.append({'team_id':team,'selected_source_sha':chosen.sha,'selection':'one_recorded_stream_per_team',
            'alternate_tracks_not_added':sum(len(r) for s,r in options if s.sha!=chosen.sha)})
        for row in rows:
            start=chosen.provenance(row.get('source'),raw_clock=row.get('start_clock'))
            samples=[]
            for sample in row.get('samples',[]):
                proof=chosen.provenance(sample.get('source'),raw_clock=sample.get('clock'))
                if proof and _point(sample.get('position')):samples.append(dict(deepcopy(sample),clock=proof['clock'],source=proof))
            if not samples:
                unaligned['planes']+=1;continue
            end=chosen.provenance(row.get('end_source'),raw_clock=row.get('end_clock'))
            record=deepcopy(row);record.update(id=chosen.sha[:16]+':'+str(row.get('id')),team_id=team,
                source_sha=chosen.sha,original_start_clock=row.get('start_clock'),start_clock=start['clock'] if start else samples[0]['clock'],
                source=start or samples[0]['source'],end_clock=end['clock'] if end else None,end_source=end,
                samples=samples,clock_domain='battle_elapsed_seconds',interpolation_allowed=False)
            record['deactivated_clock']=None
            records.append(record)
    return records,policies


def _merge_smokes(sources, ticks, shared_namespace, conflicts, unaligned):
    selected_sources = sources if shared_namespace else [_ordered_sources(sources)[0]]
    groups = defaultdict(list)
    for source in selected_sources:
        for index, smoke in enumerate((source.report.get("combat") or {}).get("smokes") or []):
            key = smoke.get("entity_id")
            if type(key) is int:
                groups[key].append((source, smoke, index))
    output = []
    for entity, records in sorted(groups.items()):
        episode = None
        previous_tick = None
        for tick in ticks:
            choices = []
            for source, record, index in records:
                anchor = source.clock.by_tick.get(tick)
                if anchor is None:
                    continue
                pc = anchor["packet_clock"]
                if _number(record.get("end_clock")) and pc >= record["end_clock"]:
                    continue
                samples = record.get("samples") or []
                si = bisect_right(samples, pc, key=lambda row: row["clock"]) - 1
                while si >= 0 and samples[si]["clock"] == pc:
                    offset = (samples[si].get("source") or {}).get("packet_offset")
                    if type(offset) is not int or type(anchor.get("packet_offset")) is not int or offset <= anchor["packet_offset"]:
                        break
                    si -= 1
                if si < 0:
                    continue
                sample = samples[si]
                proof = source.provenance(sample.get("source"), raw_clock=sample["clock"], entity=entity, initial_state=True)
                if proof:
                    choices.append((sample["clock"] - pc, source.sha, sample, proof))
            if not choices:
                if episode is not None:
                    episode["end_clock"] = tick
                    episode["end_reason"] = "fused_observation_gap_not_physical_expiry"
                    episode = None
                continue
            choices.sort(key=lambda row: (-row[0], row[1]))
            _, sha, selected, proof = choices[0]
            if episode is None or previous_tick != tick - 1:
                if episode is not None:
                    episode["end_clock"] = previous_tick + 1
                    episode["end_reason"] = "missing_timer_anchor_not_physical_expiry"
                episode = {"entity_id": entity, "id": f"smoke:{entity}:{tick}", "samples": [],
                           "end_clock": None, "quality": "fused_observed_geometry_lifetime_unknown"}
                output.append(episode)
            episode["samples"].append(dict(deepcopy(selected), clock=tick, source=proof, source_sha=sha,
                                           contributing_sources=[row[1] for row in choices]))
            previous_tick = tick
    return output


def merge_reports(reports):
    pairing = analyze_pairing(reports)
    if not pairing["can_merge"]:
        messages = pairing["errors"] or pairing["warnings"]
        raise MultiviewError("；".join(row["message"] for row in messages), pairing)
    unique = {report["sha256"]: report for report in reports}
    sources = [_Source(unique[sha], pairing["identity_maps"][sha]) for sha in pairing["source_shas"]]
    reference = sources[0].report
    roster = sorted(deepcopy(reference["roster"]), key=lambda row: row["shipId"])
    ticks = sorted(set().union(*(source.clock.by_tick for source in sources)))
    conflicts, unaligned = _Conflicts(), Counter()
    deaths = _death_events(sources, conflicts, unaligned)
    ships, tracks, frames, by_source = _merge_ships(sources, roster, ticks, deaths, conflicts)
    shots, shot_policy = _merge_owner_events(sources, roster, "shots", unaligned)
    torpedoes, torpedo_policy = _merge_owner_events(sources, roster, "torpedoes", unaligned)
    consumables, consumable_policy = _merge_owner_events(sources, roster, "consumables", unaligned)
    planes,plane_policy=_merge_planes(sources,unaligned)
    team_scores, score_provenance = {}, {}
    for team in (0, 1):
        def entries(source, team=team):
            original_team = next(t for t, canonical in source.teams.items() if canonical == team)
            return [(row[0], row[1], None) for row in source.report.get("team_score_samples", {}).get(str(original_team), [])
                    if len(row) >= 2 and _number(row[0]) and _number(row[1])]
        team_scores[str(team)], score_provenance[str(team)] = _merge_state_stream(sources, ticks, entries, conflicts, "team_score")
    caps = deepcopy(reference.get("capture_points") or [])
    cap_changes, cap_provenance, cap_state, cap_state_provenance = {}, {}, {}, {}
    cap_fields = ('capture_progress','capturing_team','has_invaders','contested','capture_enabled')
    shared_namespace = all(all(original == canonical for original, canonical in source.entities.items()) for source in sources)
    for cap in caps:
        def entries(source, cap=cap):
            match = next(c for c in source.report.get("capture_points", []) if c["name"] == cap["name"])
            raw_id = match["entity_id"]
            result = []
            for row in source.report.get("cap_team_changes", {}).get(str(raw_id), []):
                if len(row) >= 2 and _number(row[0]) and row[1] in (-1, 0, 1):
                    result.append((row[0], -1 if row[1] == -1 else source.teams[row[1]], raw_id))
            return result
        cap_changes[str(cap["entity_id"])], cap_provenance[str(cap["entity_id"])] = _merge_state_stream(sources, ticks, entries, conflicts, "capture_team")
        def capture_entries(source, cap=cap):
            raw_id = next(c["entity_id"] for c in source.report.get("capture_points", []) if c["name"] == cap["name"])
            for row in (source.report.get('cap_state_samples') or {}).get(str(raw_id), []):
                if isinstance(row, dict) and _number(row.get('packet_clock')):
                    yield row['packet_clock'], (row.get('capture_progress'), source.teams.get(row.get('capturing_team')),
                        row.get('has_invaders'), row.get('contested'), row.get('capture_enabled')), raw_id
        changes, proof = _merge_state_stream(sources, ticks, capture_entries, conflicts, 'capture_state')
        if changes:
            cap_state[str(cap['entity_id'])] = [
                dict(packet_clock=clock, **dict(zip(cap_fields, state or (None,)*len(cap_fields))))
                for clock,state in changes]
            cap_state_provenance[str(cap['entity_id'])] = proof
        for source in sources:
            other = next(c for c in source.report.get("capture_points", []) if c["name"] == cap["name"])
            shared_namespace = shared_namespace and other["entity_id"] == cap["entity_id"]
    smokes = _merge_smokes(sources, ticks, shared_namespace, conflicts, unaligned)
    full = 0
    by_entity = {}
    confirmed_deaths = {row["victim_entity_id"]: row["clock"] for row in deaths}
    for player in roster:
        entity = player["shipId"]
        values = ships[str(entity)]["samples"]
        expected = sum(tick < confirmed_deaths.get(entity, math.inf) for tick in ticks)
        observed = sum(row["global_observed"] for row in values)
        by_entity[str(entity)] = {"expected_alive_seconds": expected, "observed_position_seconds": observed,
            "position_coverage_ratio": observed / expected if expected else None,
            "selected_source_counts": dict(Counter(row["source_sha"] for row in values if row["global_observed"]))}
    for frame in frames:
        frame["conflicts_at_tick"] = conflicts.per_tick[frame["clock"]]
        full += frame["position_count"] == frame["expected_alive_count"]
    source_records = [{"sha256": s.sha, "recorder_name": (s.report.get("metadata") or {}).get("playerName"),
                       "recorder_account_id": s.report.get("recorder_account_id"), "canonical_team_id": s.host_team,
                       "original_packet_clock_range": s.report.get("packet_clock_range"),
                       "first_battle_elapsed_sec": s.clock.first, "last_battle_elapsed_sec": s.clock.last,
                       "analysis_schema_version": s.report.get("analysis_schema_version"),
                       "geometry_verified": s.geometry is not None} for s in sources]
    duration_results = [r.get("post_battle_common", {}).get("duration_sec") for r in unique.values()
                        if isinstance(r.get("post_battle_common"), dict)]
    durations = {_duration for _duration in duration_results if _number(_duration)}
    result_duration = next(iter(durations)) if len(durations) == 1 else None
    if len(durations) > 1:
        conflicts.add("result_duration_disagreement", values=sorted(durations))
    duration = sources[0].clock.duration
    time_analysis = analyze_timebase([{"packet_clock": tick, "packet_offset": None, "packet_type": None,
        "entity_id": -1, "duration": duration, "timeLeft": duration - tick, "battleStage": 0} for tick in ticks],
        result_duration_sec=result_duration, packet_clock_max=ticks[-1])
    time_analysis["model"] = TIME_MODEL
    time_analysis["stage_transitions"] = [row for row in time_analysis["stage_transitions"] if row["packet_clock"] == 0]
    time_analysis["clock_domains"]["packet"] = "Derived battle-elapsed display axis, NOT an original replay packet clock."
    time_analysis["conversion_policy"] = "One-second union of actually observed timer anchors; original clocks in multiview.source_time_maps; no offset fit."
    for row in time_analysis["observations"]:
        row["alignment_sources"] = [{"source_sha": s.sha, "original_packet_clock": s.clock.by_tick[row["battle_elapsed_sec"]]["packet_clock"],
            "original_packet_offset": s.clock.by_tick[row["battle_elapsed_sec"]].get("packet_offset")}
            for s in sources if row["battle_elapsed_sec"] in s.clock.by_tick]
    geometry_source = next((s for s in sources if s.geometry is not None), sources[0])
    geometry = deepcopy(geometry_source.report.get("geometry_analysis") or {})
    geometry["projected_tracks"] = tracks
    for key in ("periods", "normalized_capture_centers", "observed_minimap_samples_checked", "observed_minimap_samples_outside_configured_bounds"):
        geometry.pop(key, None)
    derived_sha = _hash({"engine": ENGINE_VERSION, "schema_version": SCHEMA_VERSION,
                        "source_shas": pairing["source_shas"],
                        "configuration": {"freshness_seconds": FRESHNESS_SECONDS,
                                          "position_conflict_engine_units": POSITION_DISAGREEMENT_ENGINE,
                                          "axis": "observed_integer_battle_elapsed"}})
    result = {"sha256": derived_sha, "status": "multiview_derived_partial", "quality": "multi_source_observations",
        "replay_version": ",".join(map(str, SUPPORTED_VERSION)), "arena_id": reference["arena_id"],
        "analysis_schema_version": 3, "metadata": {**deepcopy(reference.get("metadata") or {}),
            "playerName": "多视角复盘（派生）", "playerVehicle": None}, "roster": roster,
        "observers": [], "host_absolute_team": None, "winner_absolute_team": pairing["checks"]["winner_absolute_team"],
        "winner_source": "mapped_source_common_results", "outcome": "unknown",
        "packet_clock_range": {"min": ticks[0], "max": ticks[-1]}, "time_analysis": time_analysis,
        "geometry_analysis": geometry, "capture_points": caps, "cap_team_changes": cap_changes,
        "team_score_samples": team_scores, "cap_state_samples": cap_state, "death_events": deaths,
        "normalized_death_times": [{"event": event, "source": event["source"],
            "battle_time": {"status": "bounded_interval", "elapsed_interval_sec": event["battle_time_interval"]}} for event in deaths],
        "event_counts": {"deaths": len(deaths), "shots": len(shots), "torpedoes": len(torpedoes), "consumables": len(consumables)},
        "event_counts_semantics": "selected_owner_stream_events_and_unique_victim_deaths; not firing totals or damage",
        "combat": {"schema_version": 1, "client_version": "15.9.0.13357625", "observation_end_clock": ticks[-1],
            "coordinate_units": "engine_units", "heading_convention": "east_ccw", "clock_domain": "battle_elapsed_seconds",
            "ships": ships, "shots": shots, "torpedoes": torpedoes, "consumables": consumables,
            "consumable_states": [], "smokes": smokes, "events": [],
            "diagnostics": {"errors": [], "truncated": False, "unaligned_events": dict(unaligned)},
            "provenance": {"engine_version": ENGINE_VERSION, "source_shas": pairing["source_shas"]}}}
    if any((s.report.get('combat') or {}).get('plane_observation_schema')==1 for s in sources):
        result['combat'].update(planes=planes,plane_observation_schema=1)
        result['event_counts']['planes']=len(planes)
        result['sha256']=_hash({'base':derived_sha,'plane_stream_revision':1,'planes':planes})
    if reference.get("display_map_name"):
        result["display_map_name"] = reference["display_map_name"]
    result["post_battle_common"] = {"winner_team_id": pairing["checks"]["winner_absolute_team"], "duration_sec": result_duration}
    result["multiview"] = {"schema_version": SCHEMA_VERSION, "engine_version": ENGINE_VERSION,
        "battle_id": pairing["battle_id"], "source_shas": pairing["source_shas"], "status": "merged_partial",
        "sources": source_records, "identity_maps": pairing["identity_maps"], "pairing": pairing,
        "clock_domain": "battle_elapsed_seconds", "source_time_maps": {s.sha: [
            {"battle_elapsed_sec": tick, "original_packet_clock": row["packet_clock"],
             "original_packet_offset": row.get("packet_offset"), "duration": row["duration"], "timeLeft": row["timeLeft"]}
            for tick, row in sorted(s.clock.by_tick.items())] for s in sources},
        "coverage": {"clock_domain": "battle_elapsed_seconds", "first_elapsed_sec": ticks[0], "last_elapsed_sec": ticks[-1],
            "alive_count_basis": "merged_death_events_at_conservative_interval_upper_bound",
            "roster_count": len(roster), "observed_timer_seconds": len(ticks),
            "missing_timer_seconds": ticks[-1] - ticks[0] + 1 - len(ticks),
            "full_alive_position_seconds": full, "full_alive_position_ratio": full / len(ticks),
            "by_source": by_source, "by_entity": by_entity, "frames": frames},
        "conflicts": conflicts.rows, "conflict_counts": dict(conflicts.counts),
        "conflicts_truncated": sum(conflicts.counts.values()) > len(conflicts.rows),
        "unaligned_event_counts": dict(unaligned), "event_stream_selection": shot_policy + torpedo_policy + consumable_policy,
        "plane_stream_selection":plane_policy,
        "replicated_state_provenance": {"team_scores": score_provenance, "capture_teams": cap_provenance,
                                        "capture_state": cap_state_provenance},
        "shared_nonship_entity_namespace": shared_namespace,
        "policies": {"identity": "complete accountDBID + shipParamsId; never names", "time": "exact observed timer ticks, no global offset",
            "positions": "owner-team source preferred; alternate fresh observations fill gaps; no interpolation",
            "visibility": "global_observed is not either side's visibility; visibility_by_source and spotted_by_enemy remain separate",
            "events": "display at mapped time-interval upper bound; original uncertainty and packet evidence retained",
            "projectiles": "one owner-team source per owner/category; alternatives remain in original packaged reports, not added twice",
            "projectile_interpolation_allowed": False, "scores": "replicated values, never sums; conflicting newest states become unknown",
            "conflicts": "per-frame asynchronous recorded-value differences, not proven simultaneous contradictions or distinct tactical incidents",
            "smoke": "observed geometry only; observation gaps are not physical expiry",
            "damage": "no new damage, target or causal attribution"},
        "limitations": ["双边文件齐全不代表所有时刻与所有事件完整；请查看逐秒覆盖。", "未观测位置不插值，时间无法对齐的事件不强行加入主时间轴。",
            "全局融合观测不等于任一方当时视野；点亮属性保留各自来源。", "弹药采用所属侧来源流，其他副本保留在原始报告，主图层不重复计数。",
            "位置冲突阈值10引擎单位仅为诊断；不能消除采样延迟和量化误差。"],
        "provenance": {"source_report_canonical_sha256": {s.sha: _hash(s.report) for s in sources},
                       "source_report_hash_semantics": "sorted compact ASCII JSON of supplied report objects; original file byte hashes belong to the package manifest",
                       "derived_report_identity": derived_sha, "raw_replay_files_modified": False}}
    return result
