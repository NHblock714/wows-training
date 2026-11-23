"""Plan a citable sequence on a single recorder's packet clock, without Qt."""
from __future__ import annotations

import hashlib
import json
import math
import re

from .replay_context_evidence import _ReplayObservations
from .replay_intel import number
from .replay_battle_state import ReplayBattleState


STORYBOARD_KIND = "replay_minimap_sequence"
TILES_PER_SHEET = 6
MAX_SHEETS = 18
MAX_SHEET_PNG_BYTES = 1024 * 1024
CADENCE = 12
_ANCHOR_SPACING = 2.5


def _roster(report):
    return {row["shipId"]: row for row in report.get("roster") or []
            if type(row.get("shipId")) is int and row["shipId"] > 0}


def _focus(report, players, selected):
    if selected is not None:
        return selected if type(selected) is int and selected in players else None
    name = (report.get("metadata") or {}).get("playerName")
    matches = [key for key, row in players.items() if name and row.get("name") == name]
    return matches[0] if len(matches) == 1 else None


def _legacy_event_clocks(report, focus):
    """Reproduce the selection in saved reports made with the original sampler."""
    if focus is None:
        return []
    hits = ((report.get("ordinary") or {}).get("observed_damage")
            or (report.get("statistics") or {}).get("damage_events") or [])
    anchors = []
    for side in ("victim_entity_id", "attacker_entity_id"):
        hit = max((h for h in hits if h.get(side) == focus and number(h.get("amount"))
                   and h["amount"] > 0 and number((h.get("source") or {}).get("packet_clock"))),
                  key=lambda h: h["amount"], default=None)
        if hit:
            anchors.append(hit["source"]["packet_clock"])
    anchors.extend(row["clock"] for row in report.get("death_events") or []
                   if row.get("victim_entity_id") == focus and number(row.get("clock")))
    return anchors


def _legacy_schedule(start, end, edges, anchors):
    limit = 72
    # Keep room for actual observations even if a long, sparse replay's entire
    # regular grid happens to fall in gaps.
    step = max(20, math.ceil((end - start) / (limit - 11)))
    clocks = [start + step * i for i in range(math.ceil((end - start) / step))]
    # Avoid a near-duplicate last regular sample while retaining both endpoints.
    if len(clocks) > 1 and end - clocks[-1] < 3:
        clocks.pop()
    clocks.append(end)
    for edge in edges:
        if start <= edge <= end and edge not in clocks:
            clocks = [t for t in clocks if t == start or t == end or t in edges or abs(t-edge) >= 3]
            clocks.append(edge)
    for anchor in [*anchors, *edges]:
        if not start <= anchor <= end:
            continue
        for offset in (0, -8, 8):
            clock = anchor + offset
            if (len(clocks) < limit and start <= clock <= end
                    and all(abs(clock - other) >= 3 for other in clocks)):
                clocks.append(clock)
    return sorted(clocks), step


def _cap_clocks(report, start, end):
    changes = report.get("cap_team_changes") or {}
    clocks = set()
    for cap in report.get("capture_points") or []:
        team = cap.get("initial_team")
        rows = (row for row in changes.get(str(cap.get("entity_id")), []) if isinstance(row, (list, tuple))
                and len(row) >= 2 and number(row[0]) and row[0] >= 0
                and type(row[1]) is int and row[1] in (-1, 0, 1))
        for clock, next_team, *_ in sorted(rows, key=lambda row: row[0]):
            if type(team) is int and team in (-1, 0, 1) and team != next_team and start <= clock <= end and clock > 0:
                clocks.add(clock)
            team = next_team
    ordered = sorted(clocks)
    if len(ordered) > 6:
        return [ordered[round(i * (len(ordered) - 1) / 5)] for i in range(6)]
    return ordered


def _separated_clocks(clocks, count, spacing):
    selected = []
    for clock in clocks:
        if all(abs(clock - other) >= spacing for other in selected):
            selected.append(clock)
            if len(selected) == count:
                break
    return selected


def _tactical_anchors(report, observed, focus, start, end):
    """Select recorded ownership changes, significant hits and recent combat."""
    death = observed.deaths.get(focus, math.inf)
    anchors = [(death, "复盘舰沉没", "death_events")] if start <= death <= end else []
    anchors.extend((clock, "占领区归属变化", "cap_team_changes") for clock in _cap_clocks(report, start, end))
    if focus is None:
        return anchors

    hits = (report.get("ordinary") or {}).get("observed_damage")
    damage_field = "ordinary.observed_damage" if hits else "statistics.damage_events"
    hits = hits or (report.get("statistics") or {}).get("damage_events") or []
    recent = {}
    for side, label in (("victim_entity_id", "主要承伤"), ("attacker_entity_id", "主要输出")):
        ranked = []
        for hit in hits:
            clock = (hit.get("source") or {}).get("packet_clock")
            amount = hit.get("amount")
            if (hit.get(side) == focus and number(amount) and amount > 0 and number(clock)
                    and start <= clock <= min(end, death)):
                ranked.append((amount, clock))
                recent[clock] = damage_field
        strongest = _separated_clocks((clock for _, clock in sorted(ranked, reverse=True)), 3, 15)
        anchors.extend((clock, label, damage_field) for clock in strongest)

    # Continue sampling combat after the major exchanges when the focal ship has
    # later recorded hits or shots. A final trajectory point alone is not combat.
    if observed.combat_verified:
        for shot in observed.combat.get("shots") or []:
            clock = (shot.get("source") or {}).get("packet_clock")
            if (shot.get("owner_entity_id") == focus and number(clock) and clock == shot.get("start_clock")
                    and start <= clock <= min(end, death)):
                recent.setdefault(clock, "combat.shots")
    late_start = start + 0.65 * (min(end, death) - start)
    existing = {clock for clock, _, _ in anchors}
    late = (clock for clock in sorted(recent, reverse=True)
            if clock >= late_start and all(abs(clock - t) >= 10 for t in existing))
    anchors.extend((clock, "后段交战", recent[clock]) for clock in _separated_clocks(late, 2, 20))
    return anchors


def _schedule(start, end, edges, anchors, observed):
    limit = TILES_PER_SHEET * MAX_SHEETS
    fixed = {start, end, *(clock for clock in edges if start <= clock <= end)}
    markers = {}
    for clock, label, source in anchors:
        if clock in markers or all(abs(clock - other) >= _ANCHOR_SPACING for other in markers):
            markers.setdefault(clock, []).append((label, source))
            fixed.add(clock)

    # Add closer views only where the source still has position observations.
    neighbours = []
    for offset in (-5, 5):
        for anchor in markers:
            clock = anchor + offset
            if (start < clock < end and all(abs(clock - other) >= _ANCHOR_SPACING for other in fixed)
                    and observed.positions(clock)):
                neighbours.append(clock)

    # Preserve continuous coverage of long recordings, with space left for the
    # nearby views. Shorter battles keep the default interval.
    slots = limit - len(fixed) - min(len(neighbours), 10) + 1
    step = max(CADENCE, math.ceil((end - start) / slots))
    clocks = fixed | {start + step * i for i in range(1, math.ceil((end - start) / step))
                      if all(abs(start + step * i - other) >= _ANCHOR_SPACING for other in fixed)}
    for clock in neighbours:
        if len(clocks) >= limit:
            break
        if all(abs(clock - other) >= _ANCHOR_SPACING for other in clocks):
            clocks.add(clock)
    return sorted(clocks), step, markers


def build_replay_storyboard_rows(report, *, focus_entity_id=None, legacy_sampling=False, include_battle_state=True,
                                 ship_names=True, include_map_locations=True):
    """Build source-bound frame times and localized ship labels."""
    if (not isinstance(report, dict) or report.get("multiview") or report.get("framing_error")
            or report.get("status") == "failed"):
        return []
    sha = report.get("sha256")
    if not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{64}", sha):
        return []
    players = _roster(report)
    observed = _ReplayObservations(report, players)
    has_scoreboard = include_battle_state and not legacy_sampling and ReplayBattleState(report).has_state
    segments = [seg for groups in observed.tracks.values() for seg in groups if seg]
    if not segments:
        return []
    focus = _focus(report, players, focus_entity_id)
    arena = report.get("arena_id")
    version = str(report.get("replay_version") or "").replace(",", ".")
    battle = f"arena:{version}:{arena}" if arena else "replay:" + sha
    first, last = min(seg[0][0] for seg in segments), max(seg[-1][0] for seg in segments)
    packets = report.get("packet_clock_range") or {}
    start = (report.get("time_analysis") or {}).get("coverage", {}).get("first_started_packet_clock")
    start_basis = "核验开局的录像包时钟"
    if not number(start) or not 0 <= start <= last:
        start, start_basis = first, "首个舰位样本"
        if number(packets.get("min")) and 0 <= packets["min"] < first - 2.5:
            start, start_basis = packets["min"], "首个录像包"
    end, end_basis = last, "最后一个舰位样本"
    if number(packets.get("max")) and packets["max"] > last + 2.5:
        end, end_basis = packets["max"], "最后一个录像包"
    # Preserve the actual boundary samples when the recording has a blank tail or lead.
    if legacy_sampling:
        clocks, step = _legacy_schedule(start, end, [first, last], _legacy_event_clocks(report, focus))
        markers = {}
    else:
        anchors = _tactical_anchors(report, observed, focus, start, end)
        clocks, step, markers = _schedule(start, end, [first, last], anchors, observed)
    if ship_names:
        from .ship_traits import ship_display_name
        labels = {entity: ship_display_name(player.get("shipParamsId"), version) for entity, player in players.items()}
    else:
        labels = {entity: f"{index:02d}" for index, entity in enumerate(sorted(players), 1)}
    recorder = _focus(report, players, None)
    recorder_label = "录制舰 " + labels[recorder] if recorder is not None else "录制舰身份未知"
    ship_keys = [{"label": labels[key], "entity_id": key,
                  "team_id": row["teamId"] if type(row.get("teamId")) is int and row["teamId"] in (0, 1) else None,
                  "ship_id": row["shipParamsId"] if type(row.get("shipParamsId")) is int else None}
                 for key, row in sorted(players.items())]
    pages = [clocks[i:i + TILES_PER_SHEET] for i in range(0, len(clocks), TILES_PER_SHEET)]
    if len(pages) > 1 and len(pages[-1]) < 3:
        tail = pages[-2] + pages[-1]
        # A final page of three or four tiles stays readable in the preview.
        split = math.ceil(len(tail) / 2)
        pages[-2:] = [tail[:split], tail[split:]]
    rows = []
    for group in pages:
        samples = [observed.positions(clock) for clock in group]
        present = sum(bool(sample) for sample in samples)
        focussed = sum(focus in sample for sample in samples)
        selected = [focus] if focus is not None else []
        subject = f"复盘舰 {labels[focus]} 有近期舰位 {focussed}/{len(group)} 格" if selected else "复盘舰身份待核对"
        text = (f"同一录制视角的连续小地图，第 {len(rows) + 1} 页，按行从左至右读。"
                f"共 {len(group)} 个采样时刻，其中 {present} 格有近期舰位，{subject}。"
                + ("同名舰按阵营和位置区分。" if ship_names else "每页舰号一致；")
                + "两个采样时点之间的未记录行动未知。")
        details = {"frame_clocks": group, "count": len(group), "observed": present, "source": recorder_label,
                   "time_interval_sec": step, "start_basis": start_basis, "end_basis": end_basis,
                   "scope": "单一录制视角，覆盖录像所及区间；结束时刻不代表结算终局。",
                   "source_fields": [observed.position_source], "support": ship_keys}
        if ship_names:
            details["ship_label_mode"] = "display_name"
        if include_map_locations and not legacy_sampling and observed.grid_bounds:
            details["map_grid"] = 1
            locations = []
            for clock, sample in zip(group, samples):
                if focus in sample and (location := observed.location(sample[focus])):
                    locations.append({"clock": clock, **location})
            if locations:
                details["frame_locations"] = locations
        if not legacy_sampling:
            details["sampling_version"] = 2
            frame_anchors = [{"clock": t, "label": label, "source": source}
                             for t in group for label, source in markers.get(t, ())]
            if frame_anchors:
                details["frame_anchors"] = frame_anchors
                details["source_fields"] += list(dict.fromkeys(anchor["source"] for anchor in frame_anchors))
        if has_scoreboard:
            details['battle_state_overlay'] = 1
        identity = [f"recorder-storyboard-v{1 if legacy_sampling else 2}", sha, battle, selected, details, focussed]
        key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
        rows.append({"kind": STORYBOARD_KIND, "id": "film:" + key, "source_sha": sha, "battle_id": battle,
                     "arena_id": arena, "entity_ids": selected, "clock_domain": "packet_clock",
                     "clock_start": group[0], "clock_end": group[-1], "text": text, "details": details})
    return rows
