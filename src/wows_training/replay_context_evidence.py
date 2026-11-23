"""Bounded report evidence from observations on one verified replay time axis."""
from __future__ import annotations

from bisect import bisect_right
from itertools import combinations
import math

from .contracts import parse_version
from .replay_grid import location_label, map_cell, map_location
from .replay_intel import number


_PINNED = (15, 9, 0, 13357625)
_WIRE = (15, 10, 0, 13533378)
_MAX_SAMPLE_AGE = 2.5
_TURN_STEP_DEGREES = 30
REPLAY_CONTEXT_KINDS = frozenset({
    "replay_relative_position", "replay_map_control", "replay_hull_angle",
    "replay_action_frequency", "replay_context_coverage",
})


def _version(report):
    try:
        return parse_version(report.get("replay_version") or "")
    except (TypeError, ValueError):
        return ()


def _geometry_verified(report):
    geometry = report.get("geometry_analysis") or {}
    projection = geometry.get("png_projection") or {}
    viewport = projection.get("viewport_bounds_engine") or {}
    return (geometry.get("png_alignment_verified") is True and not geometry.get("errors")
            and projection.get("status") == "source_chain_verified"
            and projection.get("mapping_scope") == "single_identity_mapping"
            and viewport.get("projection_convention") == "SpaceSizeUtils.native_max_min_return"
            and viewport.get("native_viewport_equality_verified") is True)


def _track_segments(segments, columns, transform):
    for raw in segments:
        if not isinstance(raw, list):
            continue
        segment = []
        for row in raw:
            if (not isinstance(row, (tuple, list)) or len(row) <= max(columns)
                    or not all(number(row[index]) for index in columns)):
                if segment:
                    yield segment
                    segment = []
                continue
            point = transform(row, columns)
            if point is None or point[0] < 0 or segment and point[0] <= segment[-1][0]:
                if segment:
                    yield segment
                    segment = []
                if point is None or point[0] < 0:
                    continue
            segment.append(point)
        if segment:
            yield segment


class _ReplayObservations:
    def __init__(self, report, players):
        self.report = report
        self.players = players
        self.version = _version(report)
        self.combat = report.get("combat") or {}
        from .ordinary_replay import current_combat_verified
        self.combat_verified = ((self.version == _PINNED and self.combat.get("schema_version") == 1
                                 or current_combat_verified(report)) and not report.get("framing_error"))
        self.tracks = {}
        self.position_source = None
        self.grid_bounds = None
        self.scale = None
        self.deaths = {}
        for event in report.get("death_events") or []:
            entity, clock = event.get("victim_entity_id"), event.get("clock")
            if type(entity) is int and number(clock) and clock >= 0:
                self.deaths[entity] = min(clock, self.deaths.get(entity, clock))
        self.states = {}
        if self.combat_verified:
            for entity in players:
                samples = (self.combat.get("ships") or {}).get(str(entity), {}).get("samples") or []
                self.states[entity] = sorted((sample for sample in samples if number(sample.get("clock"))),
                                             key=lambda sample: sample["clock"])
        if self.version == _PINNED and _geometry_verified(report):
            self._load_projected()
        elif self.version == _WIRE and not report.get("multiview"):
            self._load_wire()

    def _load_projected(self):
        geometry = self.report["geometry_analysis"]
        self.grid_bounds = geometry["png_projection"]["viewport_bounds_engine"]
        if (geometry.get("meters_per_engine_unit") == 30
                and geometry.get("distance_domain") == "client_displayed_world_distance"):
            self.scale = 30.0
        self.position_source = "geometry_analysis.projected_tracks"
        for track in geometry.get("projected_tracks") or []:
            entity = track.get("entity_id")
            names = track.get("columns") or []
            required = ("packet_clock", "x_engine", "z_engine")
            if entity not in self.players or any(names.count(key) != 1 for key in required):
                continue
            columns = tuple(names.index(key) for key in required)
            transform = lambda row, ix: (row[ix[0]], row[ix[1]], row[ix[2]], None)
            segments = _track_segments(track.get("segments") or [], columns, transform)
            self.tracks.setdefault(entity, []).extend(segments)

    def _load_wire(self):
        from .ordinary_replay import CURRENT, HASHES, extra_map_record

        ordinary = self.report.get("ordinary") or {}
        pairs, error = ordinary.get("world_pairs"), ordinary.get("world_rmse")
        if (ordinary.get("profile_sha256") != HASHES[CURRENT + '.json']
                or type(pairs) is not int or pairs < 10 or not number(error) or not 0 <= error <= 5):
            return
        self.position_source = "ordinary.minimap_raw_tracks"
        if ordinary.get("map_identity") is True:
            name = str((self.report.get("metadata") or {}).get("mapName") or "").rsplit("/", 1)[-1]
            record = extra_map_record(name, CURRENT)
            if record:
                self.grid_bounds = record.get("original_bounds_engine")

        def coordinates(row, _columns):
            clock, x, z = row[:3]
            if not (0 <= x <= 2047 and 0 <= z <= 2047):
                return None
            return clock, x * 5000 / 2047 - 2500, z * 5000 / 2047 - 2500, row[3] if len(row) > 3 else None

        for entity in self.players:
            raw = (ordinary.get("minimap_raw_tracks") or {}).get(str(entity), [])
            if raw:
                self.tracks[entity] = list(_track_segments(raw, (0, 1, 2), coordinates))

    def _state_at(self, entity, clock):
        rows = self.states.get(entity) or []
        index = bisect_right(rows, clock, key=lambda row: row["clock"]) - 1
        return rows[index] if index >= 0 else {}

    def positions(self, clock):
        positions = {}
        for entity, segments in self.tracks.items():
            if self.deaths.get(entity, math.inf) <= clock:
                continue
            state = self._state_at(entity, clock)
            if state.get("visible_to_recorder") is False or state.get("is_alive") is False:
                continue
            offered = []
            for segment in segments:
                if segment[0][0] <= clock <= segment[-1][0]:
                    index = bisect_right(segment, clock, key=lambda point: point[0]) - 1
                    offered.append(segment[index])
            if len(offered) == 1 and clock - offered[0][0] <= _MAX_SAMPLE_AGE:
                positions[entity] = offered[0]
        return positions

    def heading(self, entity, clock, point):
        if self.position_source == "ordinary.minimap_raw_tracks":
            from .ordinary_replay import verified_minimap_heading
            value = verified_minimap_heading(self.report, point[3])
            return (value, point[0]) if value is not None else (None, None)
        state = self._state_at(entity, clock)
        value, stamp = state.get("heading_radians"), state.get("heading_clock")
        if (state.get("heading_convention") == "east_ccw" and number(value) and number(stamp)
                and 0 <= clock - stamp <= _MAX_SAMPLE_AGE):
            return value, stamp
        return None, None

    def location(self, point, capture_points=None):
        if capture_points is None:
            capture_points = self.report.get("capture_points") or ()
        return map_location(self.grid_bounds, point[1], point[2], capture_points)


def _fact(kind, text, clock, details, entities=(), end=None):
    return {"kind": kind, "text": text, "entity_ids": list(dict.fromkeys(entities)),
            "clock_start": clock, "clock_end": clock if end is None else end, "details": details}


def _subject(review, players, entity, team_entities):
    if type(entity) is int:
        return entity if entity in players and (team_entities is None or entity in team_entities) else None
    recorder = review.get("recorder")
    if not recorder or recorder in ("未知", "录制者，舰位待确认"):
        return None
    matches = [key for key, player in players.items() if player.get("name") == recorder]
    if len(matches) != 1 or team_entities is not None and matches[0] not in team_entities:
        return None
    return matches[0]


def _anchor_windows(report, review, observed, subject, team_entities):
    """Prefer observed clocks of play start and the focal ship's damage events."""
    established_start = (report.get("time_analysis") or {}).get("coverage", {}).get("first_started_packet_clock")
    authored = set(team_entities if team_entities is not None else ([subject] if subject is not None else observed.players))
    clocks = []
    if number(established_start) and established_start >= 0:
        clocks.append((established_start, "已核验开局锚点", None))
    for field, other, label in (("target", "actor", "观测承伤节点"), ("actor", "target", "观测输出节点")):
        if subject is None:
            continue
        observed_hits = (hit for hit in review.get("damage_events", []) if hit.get(field) == subject
                         and number(hit.get("clock")) and hit["clock"] >= 0 and number(hit.get("amount")))
        for hit in sorted(observed_hits, key=lambda item: (-item["amount"], item["clock"]))[:8]:
            counterpart = hit.get(other)
            if counterpart not in observed.players:
                counterpart = None
            positions = observed.positions(hit["clock"])
            if subject in positions:
                clocks.append((hit["clock"], label, counterpart))
                break
    # Use actual trajectory samples if no verified event has a usable position.
    # Startup heatmaps can include a pre-battle position, so label this as a sample.
    if not any(authored.intersection(observed.positions(clock)) for clock, _, _ in clocks):
        candidates = [point[0] for entity in authored for segment in observed.tracks.get(entity, [])
                      for point in segment if not number(established_start) or point[0] >= established_start]
        if candidates:
            clocks.append((min(candidates), "首段可用位置记录", None))
    clocks.sort(key=lambda row: row[0])
    result = []
    for item in clocks:
        if not result or item[0] - result[-1][0] >= 1.0:
            result.append(item)
    return result[:3]


def _distance(a, b):
    return math.hypot(b[1] - a[1], b[2] - a[2])


def _distance_fields(value, scale):
    if scale is None:
        return f"{value:.1f} 场景单位", {"distance_engine": round(value, 2), "unit": "engine_units"}
    return f"{value * scale / 1000:.2f} 公里", {"distance_m": round(value * scale), "unit": "client_display_meters"}


def _team(player):
    team = player.get("teamId")
    return team if type(team) is int and team in (0, 1) else None


def _location_rows(observed, anchors, subject, team_entities):
    facts = []
    angle_count = 0
    team = _team(observed.players[subject]) if subject is not None else None
    for clock, label, counterpart in anchors:
        positions = observed.positions(clock)
        if team_entities is not None and len(team_entities) > 1 and (subject is None or clock == anchors[0][0]):
            points = {entity: positions[entity] for entity in sorted(team_entities) if entity in positions}
            if len(points) >= 2:
                length = max(_distance(a, b) for a, b in combinations(points.values(), 2))
                summary, units = _distance_fields(length, observed.scale)
                text = (f"{label}，分析所选队伍在本时点有 {len(points)}/{len(team_entities)} 艘舰有位置记录，"
                        f"有记录舰船间的最大间距为 {summary}。")
                facts.append(_fact("replay_relative_position", text, clock, {
                    **units, "entity_ids": list(points), "observed_entities": len(points),
                    "selected_entities": len(team_entities), "source_fields": [observed.position_source]}, points))
        if subject not in positions:
            continue
        focus = positions[subject]
        groups = {} if team is None else {
            False: [entity for entity in positions if entity != subject and _team(observed.players[entity]) == team],
            True: [entity for entity in positions if _team(observed.players[entity]) is not None
                   and _team(observed.players[entity]) != team],
        }
        references = []
        for opposing, members in groups.items():
            if not members:
                continue
            entity = (counterpart if opposing and counterpart in members
                      else min(members, key=lambda key: (_distance(focus, positions[key]), key)))
            role = ("同条伤害记录的对方舰" if opposing and entity == counterpart
                    else "最近有位置记录的对方舰" if opposing else "最近有位置记录的同队舰")
            target = positions[entity]
            length = _distance(focus, target)
            summary, units = _distance_fields(length, observed.scale)
            bearing = (90 - math.degrees(math.atan2(target[2] - focus[2], target[1] - focus[1]))) % 360 if length > 0 else None
            references.append({"target_id": entity, "entity_id": entity, "name": observed.players[entity]["name"],
                               "label": role, "text": summary,
                               "position": [target[1], target[2]], "clock": target[0],
                               "bearing_deg": round(bearing, 1) % 360 if bearing is not None else None,
                               **observed.location(target), **units})
            heading, head_clock = observed.heading(subject, clock, focus)
            if opposing and bearing is not None and heading is not None and angle_count < 2:
                line = math.atan2(target[2] - focus[2], target[1] - focus[1])
                angle = abs(math.degrees(math.remainder(line - heading, math.tau)))
                adjacent = observed.players[entity]["name"]
                facts.append(_fact("replay_hull_angle", f"{label}，{observed.players[subject]['name']}的舰艏朝向与{role}{adjacent}的连线夹角约 {angle:.0f}°（0° 指向对方，90° 为侧对）。这是平面几何关系。",
                    clock, {"entity_id": subject, "target_id": entity, "relative_angle_deg": round(angle, 1),
                            "source_fields": [observed.position_source, "combat.ships.samples.heading_radians"]
                            if observed.position_source != "ordinary.minimap_raw_tracks" else [observed.position_source],
                            "observed": [{"entity_id": subject, "name": observed.players[subject]["name"], "clock": focus[0]},
                                         {"entity_id": entity, "name": adjacent, "clock": target[0]},
                                         {"field": "heading_clock", "clock": head_clock}]}, (subject, entity)))
                angle_count += 1
        location = observed.location(focus)
        if references or location:
            parts = [f"与{item['label']}{observed.players[item['target_id']]['name']}相距{item['text']}"
                     + (f"，该舰位于 {location_label(item)}" if item.get("map_cell") else "")
                     + (f"，方向 {item['bearing_deg']:.0f}°（北为 0°）" if item["bearing_deg"] is not None else "")
                     for item in references]
            if location:
                parts.insert(0, f"位于 {location_label(location)}")
            facts.append(_fact("replay_relative_position", f"{label}，观测到{observed.players[subject]['name']}" + "，".join(parts) + "。",
                clock, {"entity_id": subject, "name": observed.players[subject]["name"],
                        "position": [focus[1], focus[2]], "clock": focus[0],
                        "source_fields": [observed.position_source], "support": references, **location},
                [subject, *(item["target_id"] for item in references)]))
    return facts


def _cap_facts(report, observed, subject=None):
    if (observed.version != _PINNED and not observed.combat_verified) or report.get("framing_error"):
        return []
    candidates = []
    caps = report.get("capture_points") or []
    changes = report.get("cap_team_changes") or {}
    for cap in caps:
        entity = cap.get("entity_id")
        name = str(cap.get("name") or "未命名")
        stream = changes.get(str(entity)) or []
        valid = sorted((row for row in stream if isinstance(row, (tuple, list)) and len(row) >= 2
                         and number(row[0]) and row[0] >= 0 and type(row[1]) is int and row[1] in (-1, 0, 1)),
                       key=lambda row: row[0])
        for index, row in enumerate(valid):
            if row[0] > 0 and row[1] in (0, 1) and (index == 0 or row[1] != valid[index-1][1]):
                candidates.append((row[0], row[1], name, cap))
    facts = []
    ordered = sorted(candidates, key=lambda item:item[0])
    if len(ordered) > 12:
        ordered = [ordered[round(i*(len(ordered)-1)/11)] for i in range(12)]
    for clock, team, name, cap in ordered:
        text = f"客户端记录 {name} 区归属队伍 {team}。"
        fields = {"team_id": team, "label": name, "source_fields": ["cap_team_changes"]}
        if (cap_cell := map_cell(observed.grid_bounds, cap.get("x"), cap.get("z"))) is not None:
            fields.update(cap_name=name, cap_cell=cap_cell)
            text += f"{name} 点中心位于 {cap_cell}。"
        ids = []
        geometry = _geometry_verified(report) or observed.position_source == "ordinary.minimap_raw_tracks"
        if geometry and all(number(cap.get(key)) for key in ("x", "z", "radius")) and cap["radius"] > 0:
            positions = observed.positions(clock)
            ids = [entity for entity, point in positions.items()
                   if math.hypot(point[1] - cap["x"], point[2] - cap["z"]) <= cap["radius"]]
            fields.update(observed_entities=len(positions), count=len(ids))
            text += (f"这个时点有 {len(positions)} 艘舰具备位置记录，其中 {len(ids)} 艘的记录位置在圈内。"
                     if positions else "这个时点缺少可用的圈内舰位观测。")
            if subject in positions:
                point = positions[subject]
                distance = math.hypot(point[1]-cap["x"], point[2]-cap["z"])
                location = observed.location(point, [cap])
                fields.update(entity_id=subject, distance_engine=round(distance, 1), radius_engine=cap["radius"], **location)
                text += (f"复盘舰位于 {location_label(location)}。" if location else
                         f"复盘舰距圈心 {distance:.1f} 场景单位，圈半径 {cap['radius']:g}。")
        facts.append(_fact("replay_map_control", text, clock, fields, ids))
    return facts


def _frequency(report, observed, subject):
    if subject is None or not observed.combat_verified or report.get("multiview"):
        return None
    combat = observed.combat
    diagnostics = combat.get("diagnostics") or {}
    if diagnostics.get("truncated") or diagnostics.get("errors"):
        return None
    first = (report.get("time_analysis") or {}).get("coverage", {}).get("first_started_packet_clock")
    start_label = "已核验开局"
    if not number(first) and observed.version == _WIRE:
        first = min((segment[0][0] for segment in observed.tracks.get(subject, []) if segment), default=None)
        start_label = "本舰首次舰位记录"
    end_seen = combat.get("observation_end_clock")
    if not number(first) or not number(end_seen) or first < 0:
        return None
    end = min(end_seen, observed.deaths.get(subject, end_seen))
    if end - first < 30:
        return None
    rows = []
    for stream, owner, label in (("shots", "owner_entity_id", "炮弹发射封包"),
                                  ("torpedoes", "owner_entity_id", "鱼雷发射封包"),
                                  ("consumables", "entity_id", "消耗品启用封包")):
        offsets = {(row.get("source") or {}).get("packet_offset") for row in combat.get(stream) or []
                   if row.get(owner) == subject and number(row.get("start_clock"))
                   and first <= row["start_clock"] <= end and type((row.get("source") or {}).get("packet_offset")) is int}
        if offsets:
            count = len(offsets)
            rows.append({"label": label, "count": count, "events_per_minute": round(count * 60 / (end - first), 2),
                         "source_fields": [f"combat.{stream}"]})
    if not rows:
        return None
    summary = "，".join(f"{row['label']} {row['count']} 批（约 {row['events_per_minute']:.2f} 批/分钟）" for row in rows)
    endpoint = "本舰击沉记录" if observed.deaths.get(subject, math.inf) <= end_seen else "录制观测截止"
    text = (f"{observed.players[subject]['name']}在{start_label}至{endpoint}的 {end-first:.1f} 秒里，"
            f"本视角记录到{summary}。按封包去重，频率只反映已接收的事件。")
    return _fact("replay_action_frequency", text, first, {"entity_id": subject,
        "name": observed.players[subject]["name"], "duration_sec": round(end-first, 2),
        "support": rows, "unit": "observed_packet_batches_per_minute"}, [subject], end=end)


def _recorded_turn_frequency(observed, subject):
    """Count net 30° bow direction steps within fresh, consecutive heading samples."""
    if subject is None or observed.position_source != "ordinary.minimap_raw_tracks":
        return None
    # This evidence is a geometric fallback when action packets were not
    # decoded. Small direction steps accumulate only within one visible span.
    count, covered = 0, 0.0
    first = last = None
    threshold = math.radians(_TURN_STEP_DEGREES)
    segments = sorted(observed.tracks.get(subject, []), key=lambda points: points[0][0])
    if any(next_segment[0][0] <= segment[-1][0] for segment, next_segment in zip(segments, segments[1:])):
        return None  # Repeated or reset sample times cannot share a denominator.
    for segment in segments:
        previous, reference = None, None
        for point in segment:
            clock = point[0]
            if observed.deaths.get(subject, math.inf) <= clock:
                break
            heading, _ = observed.heading(subject, clock, point)
            if heading is None:
                previous, reference = None, None
                continue
            if previous is None or clock - previous > _MAX_SAMPLE_AGE:
                reference = heading
            else:
                covered += clock - previous
                first = previous if first is None else min(first, previous)
                last = clock if last is None else max(last, clock)
                if abs(math.remainder(heading - reference, math.tau)) >= threshold - 1e-9:
                    count += 1
                    reference = heading
            previous = clock
    if covered < 30 or first is None or last is None:
        return None
    rate = count * 60 / covered
    text = (f"{observed.players[subject]['name']}在同一连续航向段内，舰首记录方向相对上个参照累计变化"
            f"达 {_TURN_STEP_DEGREES}° 共 {count} 次。连续有效样本覆盖 {covered:.1f} 秒，约 {rate:.2f} 次/分钟。"
            "只统计观察到的舰首变向，开火、鱼雷与消耗品操作次数未知。")
    return _fact("replay_action_frequency", text, first, {"entity_id": subject,
        "name": observed.players[subject]["name"], "count": count,
        "observed_seconds": round(covered, 2), "events_per_minute": round(rate, 2),
        "unit": "observed_heading_steps_per_minute", "source_fields": [observed.position_source]}, [subject], end=last)


def build_replay_context_evidence(report, review, focus_entity_id=None, *, focus_team_entity_ids=None):
    """Collect compact, source-scoped facts without treating coverage gaps as events."""
    participants = {p["entity_id"]: p for p in review.get("participants", []) if type(p.get("entity_id")) is int}
    players = {row["shipId"]: {**participants[row["shipId"]], "teamId": row.get("teamId")}
               for row in report.get("roster") or [] if type(row.get("shipId")) is int and row["shipId"] in participants}
    team_entities = (None if focus_team_entity_ids is None else
                     {entity for entity in focus_team_entity_ids if type(entity) is int and entity in players})
    subject = _subject(review, players, focus_entity_id, team_entities)
    observed = _ReplayObservations(report, players)
    anchors = _anchor_windows(report, review, observed, subject, team_entities) if observed.tracks else []
    facts = _cap_facts(report, observed, subject)
    facts.extend(_location_rows(observed, anchors, subject, team_entities))
    frequency = _frequency(report, observed, subject) or _recorded_turn_frequency(observed, subject)
    if frequency is not None:
        facts.append(frequency)
    available = {row["kind"] for row in facts}
    unknown = [label for kind, label in (("replay_relative_position", "关键节点相对舰位"),
        ("replay_map_control", "占领区归属变化"), ("replay_hull_angle", "舰艏相对夹角"),
        ("replay_action_frequency", "可归属的操作事件频率")) if kind not in available]
    if unknown:
        text = "当前录像缺少可核对的" + "、".join(unknown) + "。"
        if report.get("multiview") and "replay_action_frequency" not in available:
            text += "合并录像的操作频率需回到单方录制核对。"
        facts.append(_fact("replay_context_coverage", text, None,
                           {"status": "partial" if facts else "unknown", "unknown": unknown}))
    return facts
