"""Single-ship observations and settlement metrics with explicit clocks and denominators."""
from __future__ import annotations

from collections import Counter, defaultdict
from functools import lru_cache
import hashlib
import json
import math

from .combat_capture import CONSUMABLE_NAMES, LABELS
from .ordinary_replay import CURRENT, HASHES, OLD, _profile, number
from .replay_grid import location_label
from .replay_context_evidence import _ReplayObservations, _distance_fields, _fact, _subject
from .statistics_analysis import CATEGORIES, category
from .statistics_capture import AIRCRAFT_TOTALS, weapon_damage_fields
from .single_battle_metrics import KINDS as SINGLE_BATTLE_KINDS, settlement_fact, single_battle_facts


PERFORMANCE_KINDS = frozenset({
    "replay_ship_phase", "replay_consumable_timing", "replay_consumable_overlap",
    "replay_damage_efficiency", "replay_damage_composition", "replay_performance_coverage",
    "replay_damage_over_time", "replay_incoming_damage",
}) | SINGLE_BATTLE_KINDS
_OLD_PROFILE_SHA = "35c32c20540160d54d5127b9b811d21473eefb833401b52bf22eede30b9686ac"
_TEAM_UTILITY = {6, 10, 12, 36, 42}
_CATEGORY_LABELS = dict(CATEGORIES, buildings="建筑来源")


@lru_cache(maxsize=2)
def _layout(version):
    if version == CURRENT:
        fields = _profile(CURRENT + ".json")["public"]
    else:
        from .runtime_bundle import default_bundle_root
        raw = (default_bundle_root() / "statistics_schema.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != _OLD_PROFILE_SHA:
            raise ValueError("承伤结算协议版本不符。")
        fields = json.loads(raw)["public_prefix"]
    return weapon_damage_fields(fields)


def _settlements(report, version, player):
    arena = report.get("arena_id")
    common = report.get("post_battle_common") or {}
    account = player.get("accountDBID")
    if not arena or str(common.get("arena_id")) != str(arena) or type(account) is not int:
        return []
    if version == CURRENT:
        row = ((report.get("ordinary") or {}).get("player_results") or {}).get(str(account))
        return [row] if isinstance(row, dict) else []
    stats = report.get("statistics") or {}
    if stats.get("status") != "result_fields_available" or stats.get("result_stream_truncated"):
        return []
    return [row for snapshot in stats.get("result_snapshots") or []
            if str(snapshot.get("arena_id")) == str(arena)
            for row in snapshot.get("players") or []
            if (row.get("account_id"), row.get("entity_id"), row.get("ship_params_id"),
                row.get("team_id"), row.get("reported_name")) ==
               (account, player.get("shipId"), player.get("shipParamsId"), player.get("teamId"), player.get("name"))]


def selected_settlement(report, review, entity):
    """Return the verified result card for exactly the selected hull."""
    version = str(report.get('replay_version') or '').replace(',', '.')
    if version not in (OLD, CURRENT) or report.get('multiview') or type(entity) is not int:
        return None
    players = [p for p in report.get('roster') or [] if p.get('shipId') == entity]
    participants = [p for p in review.get('participants') or [] if p.get('entity_id') == entity]
    if len(players) != 1 or len(participants) != 1:
        return None
    rows = _settlements(report, version, players[0])
    if not rows:
        return None
    fact = settlement_fact(report, review, participants[0], players[0], rows, entity)
    return fact['details'] if fact else None


def _unique_value(rows, field):
    values = {row[field] for row in rows if number(row.get(field))}
    return next(iter(values)) if len(values) == 1 else None


_DOT_FIELDS = {"damage_fire": "火灾", "damage_flood": "进水"}


def _side(players, observer, other):
    own, team = players[observer].get("teamId"), players.get(other, {}).get("teamId")
    if type(own) is not int or own not in (0, 1) or type(team) is not int or team not in (0, 1):
        return "阵营未知"
    return "我方" if own == team else "敌方"


def _dot_fact(focus, players, participants, all_rows, incoming_field, total):
    samples = all_rows[focus]
    source_fields = []
    support = []
    summaries = []
    for direction, key, prefix, label in (("dealt", "dealt_damage_by_field", "", "造成"),
                                           ("received", incoming_field, "received_", "受到")):
        values = [r[key] for r in samples if isinstance(r.get(key), dict)]
        types = []
        for field, name in _DOT_FIELDS.items():
            amount = _unique_value(values, prefix + field)
            share = amount / total if direction == "dealt" and number(total) and total > 0 and number(amount) and amount <= total else None
            if amount is not None:
                support.append({"label": name, "direction": direction, "amount": amount, "share": share,
                                "field": prefix + field, "scope": "本舰结算"})
            types.append(f"{name} {amount:,.0f}" if amount is not None else f"{name}未知")
        if values:
            summaries.append(label + " ".join(types))
            source_fields.append(key)

    # The 15.10 interactions table gives per-target typed settlements. Reverse
    # a matched attacker's row to identify reported sources of the focal ship's DoT.
    directions = {"dealt": [], "received": []}
    for actor, rows in all_rows.items():
        for row in rows:
            for target in row.get("target_results") or ():
                victim = target.get("entity_id")
                if actor == victim or focus not in (actor, victim) or victim not in players:
                    continue
                types = target.get("dot_by_field") or {}
                for field, name in _DOT_FIELDS.items():
                    amount = types.get(field)
                    if not number(amount) or amount == 0:
                        continue
                    direction, other = ("dealt", victim) if actor == focus else ("received", actor)
                    directions[direction].append({"label": name, "field": field, "direction": direction,
                        "amount": amount, "entity_id": actor, "target_id": victim, "side": _side(players, focus, other),
                        "ship_name": participants[other]["ship"], "scope": "逐舰结算"})
    subject = participants[focus]["ship"]
    text = f"{subject}全场持续伤害结算。"
    if summaries:
        text += "；".join(summaries) + "。"
    entities = [focus]
    for direction, title in (("dealt", "已记录的主要目标"), ("received", "已记录的主要来源")):
        top = sorted(directions[direction], key=lambda rec: -rec["amount"])[:4]
        if top:
            source_fields.append("playersPublicInfo.interactions.damage_fire/damage_flood")
            support.extend(top)
            text += title + "为" + "、".join(
                f"{r['side']}{r['ship_name']} {r['label']} {r['amount']:,.0f}" for r in top) + "。"
            entities.extend(r["target_id"] if direction == "dealt" else r["entity_id"] for r in top)
    if not support:
        return None
    text += "结算数据没有逐跳时点。承伤回调另外记录，不能直接匹配单次起火、进水或损管。"
    return _fact("replay_damage_over_time", text, None, {
        "entity_id": focus, "ship_name": subject, "scope": "赛后结算",
        "source_fields": list(dict.fromkeys(source_fields)), "support": support}, entities)


def _incoming_facts(hits, players, participants, focus, observed, source_field):
    incoming = [hit for hit in hits if hit["target"] == focus and hit["clock"] >= 0]
    if not incoming or not source_field:
        return []
    windows = []
    for hit in incoming:
        if not windows or hit["clock"] - windows[-1][0]["clock"] > 10:
            windows.append([])
        windows[-1].append(hit)
    strongest = sorted(windows, key=lambda window: -sum(h["amount"] for h in window))[:4]
    hp_samples = {row["hp_clock"]: row["hp"] for row in observed.states.get(focus, [])
                  if number(row.get("hp_clock")) and number(row.get("hp"))}
    subject = participants[focus]["ship"]
    result = []
    for window in sorted(strongest, key=lambda block: block[0]["clock"]):
        totals = defaultdict(float)
        for hit in window:
            actor = hit["actor"]
            totals[actor if actor in players else None] += hit["amount"]
        start, end = window[0]["clock"], window[-1]["clock"]
        amount = sum(totals.values())
        ranked = sorted(totals, key=totals.get, reverse=True)
        support = [{"entity_id": actor if actor in players else None, "amount": round(totals[actor], 2),
                    "ship_name": participants[actor]["ship"] if actor in players else "来源未知",
                    "side": _side(players, focus, actor)} for actor in ranked]
        sources = "、".join(f"{r['side']}{r['ship_name']} {r['amount']:,.0f}" if r["entity_id"] is not None
                           else f"未标识来源 {r['amount']:,.0f}" for r in support[:3])
        text = f"{subject}在该时段记录承伤 {amount:,.0f}，主要回调来源 {sources}。回调未标注伤害类型。"
        before = max((clock for clock in hp_samples if 0 <= start - clock <= 5 and clock < start), default=None)
        after = min((clock for clock in hp_samples if 0 <= clock - end <= 5), default=None)
        source_fields = [source_field]
        location = {}
        if (point := observed.positions(start).get(focus)) and (location := observed.location(point)):
            text += f"时段开始时位于 {location_label(location)}。"
            source_fields.append(observed.position_source)
        if before is not None and after is not None:
            text += f"相邻血量样本为 {hp_samples[before]:,.0f} → {hp_samples[after]:,.0f}。"
            source_fields.append("combat.ships.samples.hp")
            support.append({"label": "承伤前后血量样本", "clock_start": before, "clock_end": after,
                            "before_hp": hp_samples[before], "after_hp": hp_samples[after]})
        result.append(_fact("replay_incoming_damage", text, start, {
            "amount": round(amount, 2), "entity_id": focus, "ship_name": subject, "count": len(window),
            "source_fields": source_fields, "support": support, "direction": "received", **location,
        }, [focus, *(actor for actor in ranked if actor in players)], end=end))
    return result


def _damage_category(field):
    if "from_buildings_" in field:
        return "buildings"
    suffix = field.removeprefix("received_").removeprefix("damage_")
    return category("received_damage_" + suffix)


def _composition(rows, fields, field_name, direction, entity, name, result_damage, received_total=None):
    samples = [r[field_name] for r in rows if isinstance(r.get(field_name), dict)]
    if not samples:
        return None
    reconciled = [tuple(r.get('reconciled_overlapping_fields') or ()) for r in rows]
    overlap = reconciled[0] if direction == 'dealt' and len(set(reconciled)) == 1 else ()
    incoming_uncertain = False
    if direction == 'received':
        known = {f:_unique_value(samples,f) for f in fields}
        candidates = []
        for field, variants in AIRCRAFT_TOTALS.items():
            total = known.get('received_'+field)
            parts = [known.get('received_'+v) for v in variants]
            if number(total) and total > 0 and any(number(v) and v > 0 for v in parts):
                incoming_uncertain = True
                if all(number(v) for v in parts) and sum(parts) == total:
                    candidates.append('received_'+field)
        if all(number(v) for v in known.values()) and number(received_total):
            gross = sum(known.values())
            if gross == received_total:
                incoming_uncertain = False
            elif gross - sum(known[f] for f in candidates) == received_total:
                overlap, incoming_uncertain = candidates, False
    fields = [field for field in fields if field not in overlap]
    groups = defaultdict(list)
    for field in fields:
        groups[_damage_category(field)].append(_unique_value(samples, field))
    count = sum(number(v) for group in groups.values() for v in group)
    if not count:
        return None
    subtotal = sum(v for group in groups.values() for v in group if number(v))
    complete = count == len(fields)
    total_known = direction == "dealt" and number(result_damage) and result_damage > 0 and subtotal <= result_damage
    conflict = direction == 'dealt' and number(result_damage) and subtotal > result_damage
    denominator = result_damage if total_known else (subtotal if complete and subtotal > 0 and not conflict and not incoming_uncertain else None)
    basis = "settled_total_damage" if total_known else "complete_weapon_subtotal" if denominator is not None else "unknown"
    support = []
    for group, values in groups.items():
        known = [v for v in values if number(v)]
        amount = sum(known) if known else None
        support.append({"label": _CATEGORY_LABELS[group], "known_fields": len(known),
                        "expected_fields": len(values), "subtotal": amount,
                        "share": amount / denominator if denominator is not None and amount is not None else None})
    support.sort(key=lambda r: (-(r["subtotal"] or 0), r["label"]))
    label = "输出" if direction == "dealt" else "承伤"
    parts = [f"{r['label']}{'已知部分' if r['known_fields'] < r['expected_fields'] else ''} {r['subtotal']:,.0f}"
             + (f"（{r['share']:.1%}）" if r["share"] is not None else "") for r in support if r["subtotal"]]
    text = (f"{name}的赛后{label}分类有 {count}/{len(fields)} 个武器及持续伤害字段可核对，"
            f"已知小计 {subtotal:,.0f}。" + ("金额最高的类型为" + "、".join(parts[:4]) + "。" if parts else ""))
    if total_known:
        text += f"占比分母为独立结算的总输出 {result_damage:,.0f}。"
    elif denominator is not None:
        text += "占比分母为这些分类字段的合计。"
    if not total_known and direction == "dealt" and number(result_damage) and result_damage != subtotal:
        text += f"独立结算的总输出为 {result_damage:,.0f}。"
    if overlap:
        text += '航空武器总项与分项重合，按分项计一次后可与逐目标结算核对。'
    if incoming_uncertain:
        text += '航空武器总项与分项可能重叠，已知小计包含各字段列值。'
    if conflict:
        text += '分类小计超过总输出，分类间可能重叠。'
    if not complete:
        text += f"有 {len(fields)-count} 个字段缺失或冲突，以上为已知金额。"
    return _fact("replay_damage_composition", text, None, {
        "entity_id": entity, "name": name, "direction": direction, "scope": "post_battle_weapon_fields",
        "source_fields": [field_name], "known_fields": count, "expected_fields": len(fields),
        "missing_fields": len(fields)-count, "subtotal": subtotal, "denominator": denominator, "basis": basis,
        "support": support, "unit": "damage_points",
    }, [entity])


def _clock(value):
    return f"{int(value)//60:02d}:{int(value)%60:02d}"


def _window(row, cutoff):
    start = row["start_clock"]
    end = row.get("end_clock")
    if number(end) and end >= start:
        source = "observed_end"
    else:
        end = row.get("expected_end_clock")
        source = "client_reported_timer"
    if not number(end) or end < start:
        return {"clock_start": start, "clock_end": None, "end_source": "unknown"}
    if number(cutoff) and cutoff < end:
        end, source = cutoff, "clipped_at_last_observation_or_death"
    return {"clock_start": start, "clock_end": end, "end_source": source}


def _activations(combat, eligible, observed, cutoff):
    rows = {}
    for raw in combat.get("consumables") or []:
        entity, start, kind = raw.get("entity_id"), raw.get("start_clock"), raw.get("type")
        offset = (raw.get("source") or {}).get("packet_offset")
        if entity not in eligible or not number(start) or type(offset) is not int or offset < 0:
            continue
        last = min(cutoff, observed.deaths.get(entity, cutoff))
        if start > last:
            continue
        label = raw.get("label") or LABELS.get(kind)
        if label is None:
            label = CONSUMABLE_NAMES[kind] if type(kind) is int and 0 <= kind < len(CONSUMABLE_NAMES) else "类型未知"
        rows.setdefault((entity, offset), {"entity_id": entity, "type": kind, "label": label, **_window(raw, last)})
    return sorted(rows.values(), key=lambda r: (r["clock_start"], r["entity_id"]))


def _utility_rows(entity, name, activations, hits, shared_types):
    own = [row for row in activations if row["entity_id"] == entity]
    if not own:
        return []
    # Detail representative activations while retaining counts over every verified activation packet.
    examples = list(dict.fromkeys([0, len(own)-1, *(i for i, r in enumerate(own) if r["type"] in shared_types),
                                   *range(len(own))]))[:5]
    support = []
    for index in sorted(examples):
        row = own[index].copy()
        clock = row["clock_start"]
        candidates = [h for h in hits if h["target"] == entity and 0 <= clock-h["clock"] <= 15]
        if candidates:
            near = max(candidates, key=lambda h: h["clock"])
            row.update(received_amount=near["amount"], packet_clock=near["clock"])
        support.append(row)
    counts = Counter(r["label"] for r in own)
    times = []
    end_labels = {"client_reported_timer": "客户端预计至", "observed_end": "记录结束于",
                  "clipped_at_last_observation_or_death": "可核对至"}
    for row in support:
        stamp = _clock(row["clock_start"]) + " " + row["label"]
        if row["clock_end"] is not None:
            stamp += "（" + end_labels[row["end_source"]] + _clock(row["clock_end"]) + "）"
        if "received_amount" in row:
            stamp += f"，此前 {_clock(row['packet_clock'])} 记录到本舰承伤 {row['received_amount']:,.0f}"
        times.append(stamp)
    description = "、".join(f"{k} {v} 次" for k, v in counts.items())
    text = f"{name}在本视角有 {len(own)} 次消耗品启用记录（{description}）。可复看的启用时点包括" + "；".join(times) + "。"
    facts = [_fact("replay_consumable_timing", text, own[0]["clock_start"], {
        "entity_id": entity, "name": name, "activation_count": len(own), "source_fields": ["combat.consumables"],
        "observed_end_count": sum(r["end_source"] == "observed_end" for r in own),
        "timer_end_count": sum(r["end_source"] == "client_reported_timer" for r in own), "support": support,
    }, [entity], end=own[-1]["clock_start"])]
    # At a given activation the longest overlap is with the previous opposite owner
    # whose same-type window ends last. This also bounds work in dense event streams.
    latest_end = {}
    best = None
    nearest = None
    for row in activations:
        if row["type"] not in shared_types or row["clock_end"] is None:
            continue
        is_subject = row["entity_id"] == entity
        kind = row["type"]
        previous = latest_end.get((kind, not is_subject))
        if previous is not None:
            first = row["clock_start"]
            last = min(row["clock_end"], previous["clock_end"])
            a, b = (row, previous) if is_subject else (previous, row)
            if last > first and (best is None or last-first > best[0]):
                best = (last-first, first, last, a, b)
            elif first >= previous["clock_end"]:
                gap = first-previous["clock_end"]
                if nearest is None or gap < nearest[0]:
                    nearest = (gap, previous["clock_end"], first, a, b)
        prior = latest_end.get((kind, is_subject))
        if prior is None or row["clock_end"] > prior["clock_end"]:
            latest_end[kind, is_subject] = row
    if best or nearest:
        seconds, first, last, a, b = best or nearest
        both_ends = all(r["end_source"] == "observed_end" for r in (a, b))
        label = "启用至结束记录" if both_ends else "启用与客户端计时资料"
        metric = "overlap_seconds" if best else "gap_sec"
        relation = "最长的一对重合" if best else "最近的一对相隔"
        facts.append(_fact("replay_consumable_overlap",
            f"{name}与一艘友舰分别启用{a['label']}，根据两舰的{label}，本视角记录的同类支援窗口中"
            f"{relation} {seconds:.1f} 秒（{_clock(first)}–{_clock(last)}）。实际共同覆盖范围与协同意图未知。",
            first, {"entity_id": entity, "target_id": b["entity_id"], metric: round(seconds, 2),
                    "label": a["label"], "basis": "observed_ends" if both_ends else "timer_or_clipped",
                    "source_fields": ["combat.consumables"], "support": [a, b]},
            [entity, b["entity_id"]], end=last))
    return facts


def _motion(segments, first, last, scale):
    best = None
    for segment in segments:
        begin = previous = None
        for point in segment:
            t = point[0]
            if t < first:
                continue
            if t > last:
                break
            if previous is None or t - previous > 2.5:
                begin = point
            elif begin is not None and (best is None or t-begin[0] > best[1][0]-best[0][0]):
                best = begin, point
            previous = t
    if best is None:
        return None
    a, b = best
    summary, units = _distance_fields(math.hypot(b[1]-a[1], b[2]-a[2]), scale)
    return {"label": "最长连续舰位样本段的首尾直线位移", "clock_start": a[0], "clock_end": b[0],
            "text": f"连续舰位记录 {_clock(a[0])}–{_clock(b[0])} 的首尾直线位移为 {summary}", **units}


def _phase_rows(report, observed, entity, name, hits, shot_clocks, own, cutoff, damage_stream):
    segments = observed.tracks.get(entity) or []
    verified_start = ((report.get("time_analysis") or {}).get("coverage") or {}).get("first_started_packet_clock")
    clocks = [h["clock"] for h in hits] + [s[0][0] for s in segments if s] + list(shot_clocks) + [r["clock_start"] for r in own]
    first = verified_start if number(verified_start) else min(clocks, default=None)
    death = observed.deaths.get(entity)
    end = min(cutoff, death) if number(death) else cutoff
    if first is None or end <= first or not clocks:
        return []
    start_label = "已核验开局" if number(verified_start) else "本舰首次可核对的舰位或行动记录"
    end_label = "本舰击沉" if end == death else "该视角观测截止"
    facts = []

    def record(lo, hi, phase, label, include, underway):
        selected = [h for h in hits if include(h["clock"])]
        out = [h for h in selected if h["actor"] == entity]
        inc = [h for h in selected if h["target"] == entity]
        dealt = sum(h["amount"] for h in out)
        received = sum(h["amount"] for h in inc)
        details = {"entity_id": entity, "name": name, "phase": phase, "start_basis": start_label if underway else "本舰击沉",
                   "end_basis": end_label if underway else "该视角观测截止", "source_fields": []}
        parts = []
        if damage_stream:
            details.update(dealt_count=len(out), received_count=len(inc), dealt_amount=dealt, received_amount=received)
            details["source_fields"].append(damage_stream)
            parts.append(f"该视角记录输出 {len(out)} 条、共 {dealt:,.0f}，承伤 {len(inc)} 条、共 {received:,.0f}")
        if underway and observed.combat_verified:
            batches = sum(include(t) for t in shot_clocks)
            uses = sum(include(r["clock_start"]) for r in own)
            details.update(shots_observed=batches, consumables_observed=uses)
            details["source_fields"].extend(["combat.shots", "combat.consumables"])
            parts.append(f"记录到炮弹发射封包 {batches} 批、消耗品启用 {uses} 次")
        motion = _motion(segments, lo, hi, observed.scale) if underway else None
        if motion:
            parts.append(motion["text"])
            details.update(support=[motion])
            details["source_fields"].append(observed.position_source)
        if parts:
            text = f"{name}从{start_label}至{end_label}的观察区间按时间等分。{label}（{_clock(lo)}–{_clock(hi)}），" if underway else f"{name}击沉记录之后（{_clock(lo)}–{_clock(hi)}），"
            facts.append(_fact("replay_ship_phase", text + "；".join(parts) + "。", lo, details, [entity], end=hi))

    for index, (phase, label) in enumerate((("early", "前段"), ("middle", "中段"), ("late", "后段"))):
        lo = first + (end-first)*index/3
        hi = end if index == 2 else first + (end-first)*(index+1)/3
        record(lo, hi, phase, label, lambda t: lo <= t < hi or index == 2 and t == hi, True)
    if end == death and any(end < h["clock"] <= cutoff and h["actor"] == entity for h in hits):
        record(end, cutoff, "after_death", "击沉后", lambda t: end < t <= cutoff, False)
    return facts


def build_performance_evidence(report, review, focus_entity_id=None, *, focus_team_entity_ids=None):
    """Focus on one unambiguously roster-bound ship; keep the recording and its settlement distinct."""
    version = str(report.get("replay_version") or "").replace(",", ".")
    participants = {r["entity_id"]: r for r in review.get("participants") or [] if type(r.get("entity_id")) is int}
    roster = [p for p in report.get("roster") or [] if type(p.get("shipId")) is int and p["shipId"] in participants]
    ids = Counter(p["shipId"] for p in roster)
    players = {p["shipId"]: p for p in roster if ids[p["shipId"]] == 1}
    focus = _subject(review, players, focus_entity_id, focus_team_entity_ids)
    if focus is None or review.get("clock_domain") != "packet_clock" or report.get("multiview"):
        return []
    player = players[focus]
    name = participants[focus]["name"]
    ordinary, stats = report.get("ordinary") or {}, report.get("statistics") or {}
    is_current = version == CURRENT and ordinary.get("profile_sha256") == HASHES[CURRENT + ".json"]
    is_old = (version == OLD and stats.get("profile_sha256") == _OLD_PROFILE_SHA and stats.get("schema_version") == 1
              and stats.get("client_version") == OLD)
    old_combat = version == OLD and (report.get("combat") or {}).get("schema_version") == 1
    rows = []
    unknown = []
    if report.get("framing_error") or not (is_current or is_old or old_combat):
        return [_fact("replay_performance_coverage", f"{name}的当前录像缺少已核验的逐舰表现协议或完整帧。",
                      None, {"entity_id": focus, "name": name, "status": "unknown"}, [focus])]
    settlements = _settlements(report, version, player) if is_current or is_old else []
    if is_current or is_old:
        settlement = settlement_fact(report, review, participants[focus], player, settlements, focus)
        if settlement:
            rows.append(settlement)
    metrics = settlements if is_current else [row.get("battle_metrics") or {} for row in settlements]
    damage, alive = _unique_value(metrics, "damage"), _unique_value(metrics, "life_time_sec")
    duration = (report.get("post_battle_common") or {}).get("duration_sec")
    if number(damage) and number(alive) and alive > 0 and (not number(duration) or alive <= duration):
        efficiency = damage * 60 / alive
        rows.append(_fact("replay_damage_efficiency",
            f"{name}结算总输出 {damage:,.0f}，结算存活 {alive:g} 秒，折合每存活分钟 {efficiency:,.0f} 伤害。"
            "分子包含本场全部结算输出。", None,
            {"entity_id": focus, "name": name, "numerator": damage, "denominator": alive, "value": round(efficiency, 2),
             "unit": "settled_damage_per_alive_minute", "source_fields": ["damage", "life_time_sec"]}, [focus]))
    else:
        unknown.append("结算总输出与有效存活秒数对应的每分钟伤害")
    extra = []
    all_rows = {}
    if is_current or is_old:
        all_rows = {e: settlements if e == focus else _settlements(report, version, p) for e,p in players.items()}
        extra = single_battle_facts(participants, players, all_rows, focus, review)
    income = next((r['details']['value'] for r in extra if r['kind']=='replay_target_impact'
                   and r['details']['direction']=='received' and r['details']['basis']=='reconciled_target_settlement'), None)
    if settlements:
        out_fields, in_fields = _layout(version)
    else:
        out_fields = in_fields = ()
    for fields, key, direction, label in (
        (out_fields, "dealt_damage_by_field", "dealt", "赛后输出类型构成"),
        (in_fields, "received_damage_by_field" if is_current else "damage_by_field", "received", "赛后承伤类型构成"),
    ):
        row = _composition(settlements, fields, key, direction, focus, name, damage, income)
        if row:
            rows.append(row)
        else:
            unknown.append(label)
    if all_rows and (dot := _dot_fact(focus, players, participants, all_rows,
                                      "received_damage_by_field" if is_current else "damage_by_field", damage)):
        rows.append(dot)
    observed = _ReplayObservations(report, {focus: player})
    rows.extend(extra)
    packet_range = report.get("packet_clock_range") or {}
    cutoff = packet_range.get("max")
    if number(cutoff):
        combat = (report.get("combat") or {}) if observed.combat_verified else {}
        if number(combat.get("observation_end_clock")):
            cutoff = min(cutoff, combat["observation_end_clock"])
        team = player.get("teamId")
        eligible = {key for key, p in players.items() if key == focus or type(team) is int and team in (0, 1) and p.get("teamId") == team}
        activations = _activations(combat, eligible, observed, cutoff)
        hits = [h for h in review.get("damage_events") or []
                if focus in (h.get("actor"), h.get("target")) and number(h.get("clock")) and h["clock"] <= cutoff
                and number(h.get("amount")) and h["amount"] > 0] if is_current or is_old else []
        shots = {(r.get("source") or {}).get("packet_offset"): r["start_clock"] for r in combat.get("shots") or []
                 if r.get("owner_entity_id") == focus and number(r.get("start_clock")) and r["start_clock"] <= cutoff
                 and type((r.get("source") or {}).get("packet_offset")) is int}
        stream = "ordinary.observed_damage" if is_current and isinstance(ordinary.get("observed_damage"), list) else (
            "statistics.damage_events" if is_old and isinstance(stats.get("damage_events"), list) else None)
        own = [r for r in activations if r["entity_id"] == focus]
        rows.extend(_incoming_facts(hits, players, participants, focus, observed, stream))
        rows.extend(_phase_rows(report, observed, focus, name, hits, list(shots.values()), own, cutoff, stream))
        shared_types = _TEAM_UTILITY | {66, 67, 70} if is_current else _TEAM_UTILITY
        rows.extend(_utility_rows(focus, name, activations, hits, shared_types))
    available = {r["kind"] for r in rows}
    for kind, label in (("replay_ship_phase", "本舰分时段观测"),
                        ("replay_consumable_timing", "本舰消耗品启用时点"),
                        ("replay_consumable_overlap", "与友舰同类支援消耗品的窗口关系")):
        if kind not in available:
            unknown.append(label)
    if unknown:
        rows.append(_fact("replay_performance_coverage", f"{name}在当前记录中缺少可核对的" + "、".join(unknown) + "。",
                          None, {"entity_id": focus, "name": name, "unknown": unknown,
                                 "status": "partial" if rows else "unknown"}, [focus]))
    return rows
