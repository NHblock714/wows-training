"""Local screenshots tied to the report's cited and approved replay charts."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re

from .ai_analysis import AIError, SINGLE_BATTLE_CONTRACTS, _chart_png, digest
from .replay_intel import number
from .replay_storyboard import CADENCE, STORYBOARD_KIND, TILES_PER_SHEET


MAX_REPORT_VISUALS = 3
_MAX_VISUAL_BYTES = 256 * 1024
_REPLAY_TIME = re.compile(r"(?<![\d:：])([0-9]{1,3})[:：]([0-5][0-9])(?:[.．]([0-9]{1,3}))?(?![0-9:：])")


@dataclass(frozen=True)
class _Chart:
    ref: dict
    evidence: dict
    clocks: tuple

    @property
    def identity(self):
        return self.ref["evidence_id"]

    @property
    def scope(self):
        return _scope(self.ref)

    @property
    def sequence(self):
        return "frame_clocks" in self.ref


def _scope(row):
    return row.get("source_sha"), row.get("battle_id"), row.get("clock_domain")


def _chart_context(result, request):
    if (not isinstance(request, dict) or request.get("report_contract") not in SINGLE_BATTLE_CONTRACTS
            or not isinstance(request.get("images"), list) or not request["images"]
            or not isinstance(request.get("evidence"), list)
            or not isinstance(result.get("battle_assessment"), dict)
            or result.get("prepared_hash") != digest(request)):
        return {}, {}, {}
    evidence = {row["id"]: row for row in request["evidence"]
                if isinstance(row, dict) and isinstance(row.get("id"), str)}
    aliases = {row["citation_id"]: row["id"] for row in evidence.values()
               if isinstance(row.get("citation_id"), str)}
    charts = {}
    for ref in request["images"]:
        if (not isinstance(ref, dict) or not isinstance(ref.get("evidence_id"), str)
                or not isinstance(ref.get("sha256"), str) or type(ref.get("byte_length")) is not int):
            continue
        identity = ref["evidence_id"]
        row = evidence.get(identity)
        if not row or _scope(ref) != _scope(row):
            continue
        clocks = ref.get("frame_clocks", [ref.get("clock")])
        if (not isinstance(clocks, list) or not 1 <= len(clocks) <= TILES_PER_SHEET
                or not all(number(clock) and clock >= 0 for clock in clocks)
                or ref.get("clock_domain") not in ("packet_clock", "battle_elapsed_seconds")):
            continue
        sequence = row.get("kind") == STORYBOARD_KIND
        details = row.get("details")
        if (sequence != ("frame_clocks" in ref)
                or (sequence and (not isinstance(details, dict) or clocks != details.get("frame_clocks")))):
            continue
        charts[identity] = _Chart(ref, row, tuple(clocks))
    return evidence, aliases, charts


def _statements(result):
    assessment = result["battle_assessment"]
    decisions = assessment.get("decisions")
    if isinstance(decisions, list):
        for index, row in enumerate(decisions):
            yield ("decisions", index), row
    yield ("verdict", 0), assessment.get("verdict")
    dimensions = assessment.get("dimensions")
    if isinstance(dimensions, list):
        for index, row in enumerate(dimensions):
            if isinstance(row, dict) and row.get("status") == "assessed":
                yield ("dimensions", index), row


def _references(statement, evidence, aliases):
    if not isinstance(statement, dict) or not isinstance(statement.get("text"), str):
        return []
    refs = statement.get("evidence_ids")
    if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
        return []
    identities = [aliases.get(ref, ref) for ref in refs]
    return list(dict.fromkeys(identities)) if all(ref in evidence for ref in identities) else []


def _statement_times(text):
    return list(dict.fromkeys(
        int(minutes) * 60 + int(seconds) + (int(decimal) / 10 ** len(decimal) if decimal else 0)
        for minutes, seconds, decimal in _REPLAY_TIME.findall(text)
    ))


def _related_times(chart, rows):
    clocks = []
    for row in rows:
        if _scope(row) != chart.scope or row.get("kind") == STORYBOARD_KIND:
            continue
        start = row.get("clock_start", row.get("clock"))
        end = row.get("clock_end")
        end = start if end is None else end
        # A short, cited REP window can locate the image even if the model leaves
        # the clock out of its prose. A whole phase or coverage interval cannot.
        if number(start) and number(end) and 0 <= start <= end <= start + 2 * CADENCE:
            event = (row.get("details") or {}).get("clock")
            clocks.extend([event] if number(event) and start <= event <= end else [start, end])
    return clocks


def _ranked_frames(statement, evidence, aliases, charts):
    refs = _references(statement, evidence, aliases)
    if not refs:
        return []
    cited = [charts[ref] for ref in refs if ref in charts]
    rows = [evidence[ref] for ref in refs]
    text_clocks = _statement_times(statement["text"])
    ranked = []
    for chart in cited:
        targets = text_clocks or _related_times(chart, rows)
        for order, target in enumerate(targets):
            index, clock = min(enumerate(chart.clocks), key=lambda pair: abs(pair[1] - target))
            distance = abs(clock - target)
            if distance <= CADENCE:
                ranked.append(((0, order, distance), chart, index))
        if not chart.sequence:
            ranked.append(((1, 0, 0), chart, 0))
        elif not targets:
            details = chart.evidence["details"]
            anchors = [item["clock"] for item in details.get("frame_anchors", ()) if item.get("clock") in chart.clocks]
            if details.get("observed") and anchors:
                ranked.append(((1, 0, 0), chart, chart.clocks.index(anchors[0])))
            elif details.get("observed") == len(chart.clocks):
                ranked.append(((1, 0, 0), chart, len(chart.clocks) // 2))
    return [(chart, index) for _, chart, index in sorted(ranked, key=lambda item: item[0])]


def _verified_png(encoded, ref):
    try:
        raw = _chart_png(encoded)
    except AIError:
        return None
    return raw if (len(raw) == ref["byte_length"] and hashlib.sha256(raw).hexdigest() == ref["sha256"]) else None


def _caption(clock, domain, sequence):
    minutes, milliseconds = divmod(round(clock * 1000), 60_000)
    axis = "比赛时间" if domain == "battle_elapsed_seconds" else "录像时间"
    scope = " · 录制视角 · 金环标记复盘舰" if sequence else ""
    return f"{axis} {minutes:02d}:{milliseconds // 1000:02d}.{milliseconds % 1000:03d}{scope}"


def _attachment(section, chart, index, encoded, raw):
    return {"section": section[0], "statement_index": section[1], "evidence_id": chart.identity,
            "clock_domain": chart.ref["clock_domain"], "clock": chart.clocks[index],
            "parent_sha256": chart.ref["sha256"], "sha256": hashlib.sha256(raw).hexdigest(),
            "png_base64": encoded,
            "caption": _caption(chart.clocks[index], chart.ref["clock_domain"], chart.sequence)}


def _curate(result, request, image_for):
    evidence, aliases, charts = _chart_context(result, request)
    if not charts:
        return []
    candidates = {key: iter(_ranked_frames(row, evidence, aliases, charts)) for key, row in _statements(result)}
    if not candidates:
        return []
    originals, used, selected = {}, set(), []

    def add(section):
        for chart, index in candidates[section]:
            frame = (*chart.scope, chart.clocks[index])
            if frame in used:
                continue
            if chart.identity not in originals:
                encoded = image_for(chart)
                originals[chart.identity] = encoded, _verified_png(encoded, chart.ref)
            encoded, raw = originals[chart.identity]
            if raw is None:
                continue
            if chart.sequence:
                from .desktop.replay_evidence_frames import crop_replay_storyboard_tile

                encoded = crop_replay_storyboard_tile(raw, index, len(chart.clocks))
                if encoded is None:
                    continue
                try:
                    raw = _chart_png(encoded)
                except AIError:
                    continue
            if len(raw) <= _MAX_VISUAL_BYTES:
                used.add(frame)
                selected.append(_attachment(section, chart, index, encoded, raw))
                return

    decisions = [key for key in candidates if key[0] == "decisions"]
    # Give each critical decision its own frame before using the remaining
    # space for a before/after pair, the verdict or an assessed dimension.
    for section in decisions + decisions + [key for key in candidates if key[0] != "decisions"]:
        if len(selected) == MAX_REPORT_VISUALS:
            break
        add(section)
    return selected


def build_report_visuals(result, prepared):
    """Keep up to three local screenshots from the exact previewed image bytes."""
    request, images = prepared["payload"], prepared.get("image_data", ())
    manifest = request.get("images", ())
    if not images or len(images) != len(manifest):
        return []
    by_id = {ref["evidence_id"]: encoded for ref, encoded in zip(manifest, images)}
    return _curate(result, request, lambda chart: by_id.get(chart.identity))


def restore_report_visuals(result, report, ship_catalog=None):
    """Recover old screenshots only if a locally rendered chart matches its saved digest."""
    from .desktop.replay_evidence_frames import build_replay_evidence_frames
    from .replay_storyboard import build_replay_storyboard_rows

    sequence_rows = {}

    def render(chart):
        row = chart.evidence
        if chart.sequence:
            entities = row.get("entity_ids", ())
            focus = entities[0] if entities else -1
            legacy = "sampling_version" not in row["details"]
            battle_state = row["details"].get("battle_state_overlay") == 1
            names = row["details"].get("ship_label_mode") == "display_name"
            map_locations = row["details"].get("map_grid") == 1
            key = (focus, legacy, battle_state, names, map_locations)
            if key not in sequence_rows:
                sequence_rows[key] = {item["id"]: item for item in build_replay_storyboard_rows(
                    report, focus_entity_id=focus, legacy_sampling=legacy, include_battle_state=battle_state,
                    ship_names=names, include_map_locations=map_locations)}
            row = sequence_rows[key].get(chart.identity)
            if row is None:
                return None
        try:
            rendered = build_replay_evidence_frames(report, [row], ship_catalog=ship_catalog)
        except (KeyError, ValueError, TypeError, OSError):
            return None
        return next((item["png_base64"] for item in rendered if item["evidence_id"] == chart.identity), None)

    return _curate(result, result.get("request"), render)


def read_report_visuals(result):
    """Resolve saved screenshots by report statement and discard unusable entries."""
    visuals = result.get("report_visuals")
    if not isinstance(visuals, list) or not visuals:
        return {}
    evidence, aliases, charts = _chart_context(result, result.get("request"))
    if not charts:
        return {}
    statements = dict(_statements(result))
    grouped, used = {}, set()
    for visual in visuals[:MAX_REPORT_VISUALS]:
        if not isinstance(visual, dict) or not isinstance(visual.get("section"), str):
            continue
        index = visual.get("statement_index")
        identity = visual.get("evidence_id")
        if type(index) is not int or not isinstance(identity, str):
            continue
        key = visual["section"], index
        chart = charts.get(identity)
        if chart is None or identity not in _references(statements.get(key), evidence, aliases):
            continue
        clock = visual.get("clock")
        if (not number(clock) or clock not in chart.clocks or visual.get("clock_domain") != chart.ref["clock_domain"]
                or visual.get("parent_sha256") != chart.ref["sha256"]):
            continue
        frame = (*chart.scope, clock)
        if frame in used:
            continue
        encoded = visual.get("png_base64")
        if not isinstance(encoded, str) or len(encoded) > 4 * ((_MAX_VISUAL_BYTES + 2) // 3):
            continue
        try:
            raw = _chart_png(encoded)
        except AIError:
            continue
        image_hash = hashlib.sha256(raw).hexdigest()
        if (len(raw) > _MAX_VISUAL_BYTES or image_hash != visual.get("sha256")
                or (not chart.sequence and image_hash != chart.ref["sha256"])):
            continue
        grouped.setdefault(key, []).append(_attachment(key, chart, chart.clocks.index(clock), encoded, raw))
        used.add(frame)
    return grouped
