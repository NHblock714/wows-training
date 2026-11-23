"""Compact, self-contained presentation of saved AI and offline briefings.

Presentation options never change the saved analysis or its evidence scope.
Only statements with resolvable references enter the report body.  Export does
not include the request, raw response, technical identifiers or service config.
"""
from __future__ import annotations

from collections import Counter
import base64
from functools import lru_cache
import hashlib
from html import escape
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote

from .ai_analysis import ASSESSMENT_AXES, SINGLE_BATTLE_CONTRACTS, tactical_skill


GROUPS = ("observations", "interpretations", "suggestions")
TEMPLATES = {
    "brief": {"title": "战术简报", "label": "单页简报", "order": ("interpretations", "observations", "suggestions"),
              "labels": ("关键发现", "核心判断", "下一步行动")},
    "review": {"title": "赛后复盘", "label": "赛后复盘", "order": GROUPS,
               "labels": ("比赛观察", "战术判断", "训练调整")},
    "player": {"title": "选手分析", "label": "选手分析", "order": GROUPS,
               "labels": ("表现与分工", "角色判断", "训练重点")},
}
PALETTES = {
    "navy": {"page": "#111D31", "panel": "#1A2C45", "raised": "#223A59", "ink": "#F3F7FF",
             "muted": "#BACDE1", "accent": "#6DE4F6", "action": "#FFA8D6", "line": "#4E6583", "warning": "#FFD6A3"},
    "light": {"page": "#F0F5FC", "panel": "#FFFFFF", "raised": "#E7F0FC", "ink": "#14263F",
              "muted": "#52718B", "accent": "#1263D3", "action": "#BC2E82", "line": "#B9D0E6", "warning": "#8B4B18"},
}
SETTLEMENT_PALETTES = {
    "light": {"banner":"#F8FBFF", "damage":"#DDEEFF", "metric":"#FFFFFF", "ribbon":"#EAF3FC",
              "win_bg":"#DFF5EF", "win":"#096D62", "loss_bg":"#FCE4F1", "loss":"#A32264"},
    "navy": {"banner":"#192B46", "damage":"#214360", "metric":"#1C304A", "ribbon":"#263D59",
             "win_bg":"#204947", "win":"#9EF0DF", "loss_bg":"#492D49", "loss":"#FFB7DB"},
}
DEFAULT_OPTIONS = {"title": "", "subtitle": "", "template": "brief", "accent": "light",
                   "sections": list(GROUPS), "show_evidence": True}
_UNCERTAIN = re.compile(r"未知|无法|不能|不确定|未确认|待确认|待核查|不足|缺少|缺失|未核验|未验证|不代表|不等于|不证明|不直接|仍需|尚未")
_TECHNICAL_ID = re.compile(r"(?<![\w])(?:[a-fA-F0-9]{32,64}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})(?![\w])")
_SECRET = re.compile(r"(?i)\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9._~+/-]+=*)")
_KEY_ASSIGNMENT = re.compile(r"(?i)\b(?:api[_ -]?key|access[_ -]?token|secret|authorization)\s*[:=]\s*[^\s,;，；]+")
_URL = re.compile(r"(?i)\b(?:https?|file)://[^\s<>\"']+")
_WINDOWS_PATH = re.compile(r"(?i)(?<!\w)[a-z]:[\\/][^\s<>\"'，；。]+|\\\\[^\s<>\"'，；。]+")
_INLINE_REFERENCES = re.compile(r"(?<![A-Za-z0-9])(?:(?:见|参见|引用)?\s*证据(?:编号)?\s*[:：]?\s*)?"
                                r"E\d{3}(?:\s*[,，、/·]\s*E\d{3})*(?![A-Za-z0-9])")


def _text(value, maximum=8000):
    """Use display text only; never turn strings into links or markup."""
    if not isinstance(value, str):
        return ""
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    value = _SECRET.sub("〔密钥已隐藏〕", value)
    value = _KEY_ASSIGNMENT.sub("〔密钥已隐藏〕", value)
    value = _URL.sub("〔链接已隐藏〕", value)
    value = _WINDOWS_PATH.sub("〔本地路径已隐藏〕", value)
    value = _TECHNICAL_ID.sub("〔内部标识已隐藏〕", value)
    return re.sub(r"\s+", " ", value).strip()[:maximum]


def normalize_options(options=None):
    """Return bounded presentation settings; unknown keys are not propagated."""
    options = options if isinstance(options, dict) else {}
    template = options.get("template")
    if not isinstance(template, str) or template not in TEMPLATES:
        template = "brief"
    accent = options.get("accent")
    if not isinstance(accent, str) or accent not in PALETTES:
        accent = "light"
    requested = options.get("sections", GROUPS)
    if not isinstance(requested, (list, tuple)):
        requested = GROUPS
    sections = [group for group in GROUPS if group in requested]
    return {"title": _text(options.get("title"), 80), "subtitle": _text(options.get("subtitle"), 160),
            "template": template, "accent": accent, "sections": sections,
            "show_evidence": options.get("show_evidence", True) is not False}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _battle_prose(value):
    text = _INLINE_REFERENCES.sub("", _text(value))
    return re.sub(r"[（(【\[]\s*[）)】\]]", "", text).strip()


def _clock(value, *, precise=False):
    if not _number(value):
        return "未知"
    if precise:
        minutes, tenths = divmod(round(abs(value)*10), 600)
        return ("−" if value < 0 else "") + f"{minutes:02d}:{tenths/10:04.1f}"
    seconds = int(abs(value))
    return ("−" if value < 0 else "") + f"{seconds // 60:02d}:{seconds % 60:02d}"


def _match_key(row):
    value = row.get("battle_id") or row.get("arena_id")
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) and value != "" else None


def _match_labels(evidence, context):
    keys = list(dict.fromkeys(key for row in evidence if (key := _match_key(row)) is not None))
    labels = {}
    supplied = context.get("battle_labels")
    supplied = supplied if isinstance(supplied, dict) else {}
    for index, key in enumerate(keys, 1):
        label = _text(supplied.get(key), 100)
        labels[key] = label or f"比赛 {index:02d}"
    return labels


def _location(row, labels):
    match = labels.get(_match_key(row), "比赛未知")
    if row.get("kind") == "ship_appearance":
        return match + " · 阵容"
    if row.get("kind") == "local_model_summary":
        return match + " · 模型覆盖"
    if row.get("kind") == "ordinary_overview":
        return match + " · 对局范围"
    if row.get("kind") == "replay_context_coverage":
        return match + " · 录像细节覆盖"
    if row.get("kind") == "replay_performance_coverage":
        return match + " · 选手观测覆盖"
    if row.get("kind") in ("replay_damage_efficiency", "replay_damage_composition", "replay_battle_contribution",
                           "replay_battle_settlement",
                           "replay_gunnery", "ordinary_result"):
        return match + " · 本局结算"
    if row.get("kind") == "replay_target_impact":
        return match + " · 逐舰伤害"
    if row.get("kind") == "replay_fleet_key":
        return match + " · 分镜舰名"
    if row.get("kind") == "replay_team_identity":
        return match + " · 敌我阵容"
    if row.get("kind") == "replay_damage_over_time":
        return match + " · 起火与进水结算"
    if row.get("kind") == "replay_ship_traits":
        return match + " · 舰船特性"
    if row.get("kind") == "replay_ship_benchmark":
        return match + " · 服务器基准"
    start = row.get("clock_start", row.get("clock"))
    if not _number(start):
        return match + " · 时间未知"
    end = row.get("clock_end")
    model_clock = (row.get("kind") == "local_model_checkpoint" or str(row.get("kind", "")).startswith("replay_")) and row.get("clock_domain") == "packet_clock"
    interval = _clock(start, precise=model_clock)
    if _number(end) and end != start:
        interval += "–" + _clock(end, precise=model_clock)
    if model_clock:
        basis = "录像时间"
    else:
        basis = "开局" if row.get("clock_domain") == "battle_elapsed_seconds" else "包时钟"
    return f"{match} · {basis} {interval}"


def _assessment_view(assessment, by_id, labels, visuals):
    """Resolve citations again at presentation time, including historical/exported documents."""
    def statement(row, *, allow_missing=False):
        if not isinstance(row, dict) or not (content := _battle_prose(row.get('text'))):
            return None
        refs = row.get('evidence_ids')
        if not isinstance(refs, list) or (not refs and not allow_missing):
            return None
        if any(not isinstance(ref, str) or ref not in by_id for ref in refs):
            return None
        # The single-battle heading already identifies the match. Keep its
        # timestamps and settlement references compact beneath the judgment.
        locations = list(dict.fromkeys(_location(by_id[ref], labels).split(" · ")[-1] for ref in refs))
        return {'text':content, 'location':'；'.join(locations), 'locations':locations}

    if not isinstance(assessment, dict) or not (verdict := statement(assessment.get('verdict'))):
        return None
    verdict.update(visuals=visuals.get(('verdict', 0), []), anchor='verdict/0')
    dimensions, missing = [], []
    for index, row in enumerate(assessment.get('dimensions') or []):
        if not isinstance(row, dict) or not isinstance(row.get('axis'), str) or row['axis'] not in ASSESSMENT_AXES:
            continue
        uncertain = row.get('status') == 'insufficient'
        view = statement(row, allow_missing=uncertain)
        if view:
            view['label'] = ASSESSMENT_AXES[row['axis']]
            view.update(visuals=visuals.get(('dimensions', index), []), anchor=f'dimensions/{index}')
            (missing if uncertain else dimensions).append(view)
    def rows(key):
        return [dict(view, visuals=visuals.get((key, index), []), anchor=f'{key}/{index}')
                for index, row in enumerate(assessment.get(key) or []) if (view := statement(row))]
    return {'verdict':verdict, 'dimensions':dimensions, 'missing':missing,
            'decisions':rows('decisions'), 'actions':rows('actions')}


def _team_context(evidence):
    record = next((row.get("details") for row in evidence if row.get("kind") == "replay_team_identity"), None)
    if not isinstance(record, dict):
        return None
    subject = _text(record.get("ship_name"), 72)
    team = record.get("team_id")
    if record.get("status") != "verified" or type(team) is not int or team not in (0, 1):
        return {"subject": subject, "sides": [], "unknown": 0}
    fleets = {side: Counter() for side in (team, 1 - team)}
    entries = record.get("support")
    for entry in entries[:256] if isinstance(entries, list) else ():
        if isinstance(entry, dict) and type(entry.get("team_id")) is int and entry["team_id"] in fleets:
            name = _text(entry.get("ship_name"), 72) or "未知舰船"
            fleets[entry["team_id"]][name] += 1
    sides = []
    for side, fleet in fleets.items():
        if not fleet:
            return {"subject": subject, "sides": [], "unknown": 0}
        ships = "、".join(f"{name} ×{count}" if count > 1 else name for name, count in fleet.items())
        sides.append({"label": "我方" if side == team else "敌方", "ships": ships, "count": sum(fleet.values())})
    unknown = record.get("count")
    return {"subject": subject, "sides": sides, "unknown": unknown if type(unknown) is int and 0 < unknown <= 256 else 0}


def _render_team_context(teams, palette):
    subject = escape(teams["subject"])
    lead = f'复盘舰 · {subject}' if subject else '复盘视角'
    if not teams["sides"]:
        lead += ' · 阵营待核对'
    header = f'<p style="color:{palette["muted"]};font-size:11px;margin:8px 0 4px;">{lead}</p>'
    if not teams["sides"]:
        return header
    columns = []
    for side, color in zip(teams["sides"], (palette["accent"], palette["action"])):
        columns.append(f'<td width="50%" bgcolor="{palette["raised"]}" style="border-top:2px solid {color};">'
                       f'<p style="color:{color};font-weight:bold;">{side["label"]} · {side["count"]} 艘</p>'
                       f'<p style="color:{palette["ink"]};">{escape(side["ships"])}</p></td>')
    note = f'<p style="color:{palette["muted"]};">另有 {teams["unknown"]} 艘阵营待核对</p>' if teams["unknown"] else ""
    return header + '<table width="99%" cellspacing="3" cellpadding="7"><tr>' + ''.join(columns) + '</tr></table>' + note


def _settlement_record(evidence, context):
    row = next((r for r in evidence if r.get('kind') == 'replay_battle_settlement'), None)
    details = row.get('details') if row else context.get('settlement')
    if not isinstance(details, dict):
        return None
    def format_metric(value):
        return f"{value:,.0f}" if _number(value) and 0 <= value <= 1e9 else "未知"
    damage = format_metric(details.get('damage'))
    settled_exp = format_metric(details.get('settled_exp'))
    base_xp = format_metric(details.get('base_xp'))
    metrics = [('击沉',format_metric(details.get('ships_killed'))), ('侦察伤害',format_metric(details.get('scouting_damage')))]
    if base_xp != '未知' or settled_exp == '未知':
        metrics.insert(0, ('基础经验', base_xp))
    else:
        metrics.insert(0, ('结算经验', settled_exp))
    ribbons = details.get('ribbons')
    recorded = isinstance(ribbons, list)
    if not recorded:
        ribbons = details.get('ribbon_metrics')
    ribbons = [(label, format_metric(r['count']), _text(r.get('key'), 64)) for r in (ribbons if isinstance(ribbons, list) else [])[:30]
               if isinstance(r, dict) and (label := _text(r.get('label'), 25)) and _number(r.get('count')) and r['count'] > 0]
    ribbons = [r for r in ribbons if r[1] != '未知'][:12]
    if damage == '未知' and all(v == '未知' for _,v in metrics) and not ribbons:
        return None
    raw_outcome = _text(details.get('outcome'),12)
    outcome = {'win':'胜利', 'loss':'失败', 'draw':'平局'}.get(raw_outcome, raw_outcome)
    return {'ship_name':_text(details.get('ship_name'),60), 'map_name':_text(details.get('map_name'),50),
            'outcome':outcome, 'damage':damage, 'metrics':metrics,
            'ribbons':ribbons, 'recorded_ribbons':recorded, 'version':_text(details.get('version'), 32)}


def build_report(payload=None, options=None):
    """Build immutable report data and scope metrics for preview or export.

    ``ready`` is false for an empty report.  ``summary.battle_count`` is None
    when no match can be identified.  Incomplete match identities are counted
    only as known matches and never advertised as the total sample size.
    Long historical statements retain their full qualifications, even if that
    makes a PDF span more than one page.
    """
    settings = normalize_options(options)
    payload = payload if isinstance(payload, dict) else {}
    if isinstance(payload.get("payload"), dict):
        payload = payload["payload"]
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    single_battle = request.get("report_contract") in SINGLE_BATTLE_CONTRACTS
    if single_battle:
        settings["show_evidence"] = False
    raw_evidence = request.get("evidence")
    evidence = [row for row in raw_evidence[:200] if isinstance(row, dict)] if isinstance(raw_evidence, list) else []
    identifiers = Counter(row.get("id") for row in evidence if isinstance(row.get("id"), str))
    by_id = {row["id"]: row for row in evidence if isinstance(row.get("id"), str) and row["id"] and identifiers[row["id"]] == 1}
    context = payload.get("report_context") if isinstance(payload.get("report_context"), dict) else {}
    labels = _match_labels(evidence, context)
    model_summaries = []
    for row in by_id.values():
        if row.get("kind") == "local_model_summary" and (summary := _text(row.get("text"), 440)):
            model_summaries.append(summary)
            break
    policy = tactical_skill()["policy"]
    limits = dict(zip(GROUPS, (policy["max_findings"], policy["max_interpretations"], policy["max_actions"])))
    valid = {key: [] for key in GROUPS}
    rejected = 0
    for key in GROUPS:
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows[:100]:
            if (not isinstance(row, dict) or not isinstance(row.get("text"), str)
                    or not row["text"].strip() or len(row["text"]) > 8000):
                rejected += 1
                continue
            refs = row.get("evidence_ids")
            if (not isinstance(refs, list) or not refs or len(refs) > 50
                    or any(not isinstance(ref, str) or ref not in by_id for ref in refs)
                    or row.get("reference_status") in ("invalid", "missing")):
                rejected += 1
                continue
            statement = _battle_prose(row["text"]) if single_battle else _text(row["text"])
            if key == "observations" and any(statement == "所选记录：" + summary for summary in model_summaries):
                continue  # The model coverage already has a dedicated section.
            locations = list(dict.fromkeys(_location(by_id[ref], labels) for ref in refs))
            valid[key].append({"text": statement, "locations": locations,
                               "location": "；".join(locations), "uncertain": bool(_UNCERTAIN.search(row["text"]))})
    selected = {key: rows[:limits[key]] for key, rows in valid.items()}
    omitted = sum(max(0, len(rows) - limits[key]) for key, rows in valid.items())
    review_items = payload.get("needs_review")
    errors = payload.get("validation_errors")
    review_count = rejected + (len(review_items) if isinstance(review_items, list) else 0) + (len(errors) if isinstance(errors, list) else 0)
    template = TEMPLATES[settings["template"]]
    sections = []
    hidden_unknowns = []
    shown_text = set()
    for key in template["order"]:
        rows = selected[key]
        # The second interpretation is the brief contract's merged uncertainty.
        # Keep it visible even when the interpretation section is deselected.
        if key == "interpretations" and len(rows) > 1:
            hidden_unknowns.extend(rows[1:])
            rows = rows[:1]
        if key in settings["sections"]:
            if rows:
                sections.append({"key": key, "title": template["labels"][GROUPS.index(key)], "rows": rows})
                shown_text.update(row["text"] for row in rows)
        else:
            hidden_unknowns.extend(row for row in rows if row["uncertain"])
    # Historical/manual imports may exceed today's short-brief contract.  Their
    # fourth or later item can qualify the earlier claims, so retain every such
    # qualification in the standalone export, not merely a pointer to the app.
    hidden_unknowns.extend(row for rows in valid.values() for row in rows if row["uncertain"])
    caveats = []
    for row in hidden_unknowns:
        if row["text"] not in shown_text:
            caveats.append(row)
            shown_text.add(row["text"])
    coverage_notes = 0
    for row in by_id.values():
        if coverage_notes >= 2:
            break
        if row.get("kind") != "replay_context_coverage":
            continue
        text = _text(row.get("text"), 280)
        if text and text not in shown_text:
            caveats.append({"text": text, "location": _location(row, labels)})
            shown_text.add(text)
            coverage_notes += 1
    identified = len(labels)
    complete = bool(evidence) and all(_match_key(row) is not None for row in evidence)
    expanded = sum(len(row["text"]) > policy["max_item_characters"] for section in sections for row in section["rows"])
    expanded += sum(len(row["text"]) > policy["max_item_characters"] for row in caveats)
    expanded += max(0, len(caveats) - 1)
    source = {"offline_brief": "离线规则简报", "manual_import": "导入分析"}.get(payload.get("source_type"), "AI 分析")
    review_decision = context.get("review_decision")
    review_status = {"accepted": "人工复核 · 已采纳", "rejected": "人工复核 · 已驳回", "needs_review": "人工复核 · 待核查"}.get(
        review_decision if isinstance(review_decision, str) else None, "待人工复核")
    visuals = {}
    if payload.get('report_visuals'):
        from .report_visuals import read_report_visuals
        visuals = read_report_visuals(payload)
    assessment = _assessment_view(payload.get('battle_assessment'), by_id, labels, visuals)
    settlement = _settlement_record(evidence, context)
    teams = _team_context(evidence)
    failure = ""
    if not any(valid.values()) and not assessment and payload.get('source_type') == 'live_ai':
        if (payload.get('metadata') or {}).get('finish_reason') == 'length':
            failure = "此次生成达到输出上限，正文未返回完整。请核对 AI 连接的输出设置和所选模型，再生成本局报告。"
        else:
            failure = "此次响应缺少可引用的完整评价，请从这场录像重新生成。"
    ready = (bool(assessment) or any(valid.values()) or bool(review_count) or bool(model_summaries)) and not failure
    notices = []
    if ready:
        notices.append(review_status + " · 引用存在不代表判断已获证实")
        if review_count:
            notices.append(f"{review_count} 项内容或引用待核查，未计入正文")
        if evidence and not complete:
            notices.append("部分比赛身份未知，场次仅统计已标识对局")
        if omitted:
            notices.append(f"正文精简 {omitted} 条，保留相关限定")
        if expanded:
            notices.append("保留了历史内容的完整限定；导出可能超过一页")
        if not any(selected.values()):
            notices.append("暂无可追溯的分析结论")
    source_name = _text(context.get("source_name"), 100)
    perspective = request.get("analysis_perspective")
    perspective_label = {"random_battle":"随机战视角", "competition":"比赛视角"}.get(perspective, "") if isinstance(perspective, str) else ""
    return {"ready": ready, "assessment":assessment, "settlement":settlement, "teams":teams, "failure":failure,
            "options": settings, "title": settings["title"] or template["title"],
            "subtitle": settings["subtitle"], "template_label": template["label"], "source": source,
            "source_name": source_name, "review_status": review_status, "perspective_label": perspective_label,
            "sections": sections, "unknowns": caveats, "notices": notices, "local_model": model_summaries,
            "summary": {"evidence_count": len(evidence), "battle_count": identified or None,
                        "battle_count_complete": complete, "review_count": review_count,
                        "omitted": omitted, "expanded": expanded,
                        "finding_count": len(selected["observations"]), "action_count": len(selected["suggestions"])}}


def _render_settlement(data, style):
    """Keep the scorecard legible in both the desktop rich-text view and printed output."""
    palette = PALETTES[style]
    tones = SETTLEMENT_PALETTES[style]
    ink, muted, accent = (palette[k] for k in ("ink", "muted", "accent"))
    heading = '　/　'.join(escape(part) for part in (data['ship_name'], data['map_name']) if part)
    outcome = data['outcome']
    outcome_tone = {'胜利':'win', '失败':'loss'}.get(outcome)
    status_bg = tones[outcome_tone + '_bg'] if outcome_tone else palette['raised']
    status_ink = tones[outcome_tone] if outcome_tone else ink
    outcome_cell = (f'<td width="15%" bgcolor="{status_bg}" align="center" valign="middle">'
                    f'<span style="color:{status_ink};font-weight:bold;font-size:14pt;">{escape(outcome)}</span></td>') if outcome else ''
    parts = [
        f'<table width="99%" cellspacing="0" cellpadding="10" style="margin-top:10px;"><tr>'
        f'<td width="5" bgcolor="{accent}"></td><td bgcolor="{tones["banner"]}">'
        f'<p style="margin:0 0 4px;"><span style="color:{accent};font-weight:bold;">战斗结算</span></p>'
        f'<span style="color:{ink};font-weight:bold;font-size:16pt;">{heading}</span>'
        f'</td>{outcome_cell}</tr></table>',
        '<table width="99%" cellspacing="3" cellpadding="8"><tr>',
    ]
    for index, (label, value) in enumerate([('造成伤害', data['damage']), *data['metrics']]):
        width, fill, color, size = (40, tones['damage'], accent, 28) if index == 0 else (20, tones['metric'], ink, 18)
        parts.append(f'<td width="{width}%" bgcolor="{fill}"><p style="margin:0;"><span '
                     f'style="font-weight:bold;color:{color};font-size:{size}pt;font-family:Bahnschrift,Segoe UI,sans-serif;">'
                     f'{escape(value)}</span></p><p style="color:{muted};margin:2px 0;">{escape(label)}</p></td>')
    parts.append('</tr></table>')
    if data['ribbons']:
        label = '战斗勋带' if data['recorded_ribbons'] else '可核验战绩'
        parts.append(f'<p style="margin:9px 0 3px;color:{ink};"><b>{label}</b></p>'
                     '<table width="99%" cellspacing="3" cellpadding="6">')
        entries = data['ribbons']
        for start in range(0, len(entries), 4):
            cells = []
            for name, value, key in entries[start:start + 4]:
                caption = escape(f'{name} ×{value}')
                encoded = _ribbon_png(data['version'], key) if data['recorded_ribbons'] else None
                glyph = (f'<img width="46" src="data:image/png;base64,{encoded}" alt="{caption}" title="{caption}"/>'
                         if encoded else '')
                cells.append(f'<td width="25%" bgcolor="{tones["ribbon"]}" title="{caption}">{glyph}&nbsp;'
                             f'<span style="color:{accent};font-size:14pt;font-weight:bold;">{escape(value)}</span></td>')
            parts.append('<tr>' + ''.join(cells) + '<td width="25%"></td>'*(4-len(cells)) + '</tr>')
        parts.append('</table>')
    if not data['recorded_ribbons']:
        parts.append(f'<p style="margin:3px 0;color:{muted};">完整勋带未记录</p>')
    return ''.join(parts)


@lru_cache(maxsize=128)
def _ribbon_png(version, key):
    from .ribbon_assets import ribbon_icon_path
    image = ribbon_icon_path(version, key)
    try:
        return base64.b64encode(image.read_bytes()).decode('ascii') if image else None
    except OSError:
        return None


def _render_minimaps(row, palette, interactive):
    cells = []
    for index, frame in enumerate(row.get('visuals', [])):
        caption = escape(frame['caption'])
        source = 'data:image/png;base64,' + frame['png_base64']
        image = f'<img src="{source}" width="245" alt="{caption}"/>'
        if interactive:
            image = f'<a href="report-frame:{row["anchor"]}/{index}">{image}</a>'
        cells.append(f'<td valign="top" bgcolor="{palette["raised"]}">{image}'
                     f'<p style="color:{palette["muted"]};font-size:10px;margin:4px 0 2px;">{caption}</p></td>')
    return ('<table cellspacing="4" cellpadding="5"><tr>' + ''.join(cells) + '</tr></table>') if cells else ''


@lru_cache(maxsize=2)
def _wordmark_image(dark=False):
    from PySide6.QtCore import QBuffer, QIODevice
    from .desktop.naval_assets import default_naval_assets, halo_wordmark, harbor_lettering
    try:
        mark = harbor_lettering(default_naval_assets().image('icons/harbor-wordmark.png'))
        if mark is None:
            return None
        if dark:
            mark = halo_wordmark(mark)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if mark.save(buffer, 'PNG'):
            return base64.b64encode(bytes(buffer.data())).decode('ascii')
        return None
    except (OSError, ValueError):
        return None


def _report_header_mark(label, palette, style):
    caption = escape(label)
    if image := _wordmark_image(style == 'navy'):
        return (f'<table cellspacing="0" cellpadding="4" style="margin:0 0 7px;"><tr>'
                f'<td valign="middle"><img width="104" alt="港口开发部" '
                f'src="data:image/png;base64,{image}"/></td>'
                f'<td valign="middle" style="color:{palette["accent"]};font-size:11px;">{caption}</td>'
                '</tr></table>')
    return f'<p style="margin:0 0 7px;color:{palette["accent"]};">港口开发部 / {caption}</p>'


def render_report(payload=None, options=None, *, interactive=False):
    """Render self-contained, Qt-compatible archive-paper HTML."""
    report = build_report(payload, options)
    # Empty previews are part of the light desktop, even with a saved dark layout.
    style = report["options"]["accent"] if report["ready"] else "light"
    palette = PALETTES[style]
    ink, muted, accent = (palette[key] for key in ("ink", "muted", "accent"))
    action, page, panel, raised = (palette[key] for key in ("action", "page", "panel", "raised"))
    esc = escape
    parts = [f'<table width="99%" cellspacing="0" cellpadding="0"><tr>'
             f'<td width="4" bgcolor="{accent}"></td><td bgcolor="{panel}" style="padding:14px 17px;border-top:1px solid {palette["line"]};">',
             _report_header_mark(report['template_label'], palette, style),
             f'<h1 style="color:{ink};font-size:25px;font-weight:700;margin-top:0px;margin-bottom:6px;">{esc(report["title"])}</h1>']
    if report["subtitle"]:
        parts.append(f'<p style="color:{muted};font-size:12px;margin-top:0px;margin-bottom:5px;">{esc(report["subtitle"])}</p>')
    source = report["source"] if report["ready"] else "报告预览"
    if report["ready"] and report["source_name"]:
        source += " · " + report["source_name"]
    if report["ready"] and report["perspective_label"]:
        source += " · " + report["perspective_label"]
    status = ("本场评价" if report["assessment"] else esc(report["review_status"])) if report["ready"] else "尚未生成"
    parts.append(f'<p style="color:{muted};font-size:10px;margin-top:4px;margin-bottom:0px;">{esc(source)} · {status}</p>'
                 f'</td><td width="26" valign="top" bgcolor="{page}" style="color:{action};font-size:25px;">◢</td></tr></table>')
    parts.append(f'<table width="99%" cellspacing="0" cellpadding="0"><tr><td width="70" bgcolor="{accent}" height="3" style="font-size:1px;"></td>'
                 f'<td width="22" bgcolor="{action}" style="font-size:1px;"></td><td bgcolor="{page}" style="font-size:1px;"></td></tr></table>')
    if report['ready'] and report['settlement']:
        parts.append(_render_settlement(report['settlement'], report['options']['accent']))
    if report['ready'] and report['teams']:
        parts.append(_render_team_context(report['teams'], palette))
    if report["ready"] and report["assessment"]:
        assessment = report['assessment']
        def assessment_row(row, label="", *, lead=False):
            caption = label or row.get('label', '')
            locations = " · ".join(row['locations'][:2])
            evidence = ('<p style="margin:3px 0 0;color:' + muted + ';font-size:9px;">'
                        + esc(locations) + '</p>') if report['options']['show_evidence'] and locations and not row['visuals'] else ''
            return (f'<table width="99%" cellpadding="7" cellspacing="0"><tr><td width="104" bgcolor="{raised}" '
                    f'style="color:{accent};font-weight:bold;">{esc(caption)}</td><td bgcolor="{panel}" '
                    f'style="border-bottom:1px solid {page};"><p style="color:{ink};font-size:{15 if lead else 12}px;'
                    f'margin:0;">{esc(row["text"])}</p>{evidence}'
                    + _render_minimaps(row, palette, interactive) + '</td></tr></table>')
        parts.append(assessment_row(assessment['verdict'], '本场总评', lead=True))
        visible = report['options']['sections']
        if "interpretations" in visible and assessment["decisions"]:
            parts.append(f'<h2 style="font-size:15px;color:{ink};">改变局势的选择</h2>')
            parts.extend(assessment_row(row, f'取舍 {i}') for i,row in enumerate(assessment['decisions'],1))
        if "observations" in visible:
            parts.append(f'<h2 style="font-size:15px;color:{ink};">本舰表现</h2>')
            parts.extend(assessment_row(row) for row in assessment['dimensions'])
        if "suggestions" in visible and assessment['actions']:
            parts.append(f'<h2 style="font-size:15px;color:{action};">再打一次</h2>')
            parts.extend(assessment_row(row, f'调整 {i}') for i,row in enumerate(assessment['actions'],1))
        if assessment['missing']:
            parts.append(f'<p style="font-size:11px;color:{muted};">评价边界　' +
                         '　'.join(esc(row['label'] + ' · ' + row['text']) for row in assessment['missing']) + '</p>')
    elif report["ready"]:
        summary = report["summary"]
        count = summary["battle_count"]
        battles = "未知" if count is None else str(count) if summary["battle_count_complete"] else "≥ " + str(count)
        if report["options"]["show_evidence"]:
            metrics = ((battles, "已标识对局"), (str(summary["evidence_count"]), "所选证据"),
                       (str(summary["finding_count"]), "关键发现"), (str(summary["action_count"]), "行动建议"))
            parts.append('<table width="99%" cellspacing="0" cellpadding="9"><tr>')
            for index, (value, label) in enumerate(metrics):
                colour = action if index == 3 else accent
                parts.append(f'<td width="25%" bgcolor="{raised}" style="text-align:center;border:3px solid {page};border-top:2px solid {colour};">'
                             f'<p style="font-size:22px;font-weight:700;color:{colour};margin-top:0px;margin-bottom:0px;">{esc(value)}</p>'
                             f'<p style="font-size:10px;color:{muted};margin-top:3px;margin-bottom:0px;">{esc(label)}</p></td>')
            parts.append('</tr></table>')
        for model_summary in report["local_model"]:
            parts.append(f'<table width="99%" cellspacing="0" cellpadding="8" style="margin-top:9px;"><tr>'
                         f'<td bgcolor="{raised}" style="border-left:2px solid {accent};">'
                         f'<p style="color:{accent};margin:0 0 4px;font-weight:700;">本地模型 · WoWSP-D1</p>'
                         f'<p style="color:{ink};margin:0;">{esc(model_summary)}</p></td></tr></table>')
        for section_number, section in enumerate(report["sections"], 1):
            colour = action if section["key"] == "suggestions" else accent
            parts.append(f'<table width="99%" cellspacing="0" cellpadding="0" style="margin-top:11px;margin-bottom:6px;"><tr>'
                         f'<td width="31" style="color:{colour};font-size:12px;font-weight:700;">{section_number:02d}</td>'
                         f'<td style="color:{ink};font-size:15px;font-weight:700;">{esc(section["title"])}</td>'
                         f'<td width="40" style="color:{colour};text-align:right;font-size:12px;">／</td></tr></table>')
            for index, row in enumerate(section["rows"], 1):
                parts.append(f'<table width="99%" cellspacing="0" cellpadding="8"><tr>'
                             f'<td width="40" valign="top" bgcolor="{raised}" style="color:{colour};font-size:13px;font-weight:700;border-left:2px solid {colour};">{index:02d}</td>'
                             f'<td valign="top" bgcolor="{panel}"><p style="font-size:12px;margin-top:0px;margin-bottom:0px;color:{ink};">{esc(row["text"])}</p>')
                if report["options"]["show_evidence"]:
                    parts.append(f'<p style="font-size:10px;margin-top:5px;margin-bottom:0px;color:{muted};">{esc(row["location"])}</p>')
                parts.append('</td></tr></table><p style="font-size:1px;margin-top:2px;margin-bottom:2px;"> </p>')
        if report["unknowns"]:
            parts.append(f'<h2 style="font-size:13px;color:{palette["warning"]};margin-top:12px;margin-bottom:6px;">待确认与限定</h2>')
            for row in report["unknowns"]:
                parts.append(f'<table width="99%" cellspacing="0" cellpadding="7"><tr><td bgcolor="{raised}" style="border-left:2px solid {palette["warning"]};">'
                             f'<p style="color:{muted};font-size:11px;margin-top:0px;margin-bottom:0px;">{esc(row["text"])}</p>')
                if report["options"]["show_evidence"]:
                    parts.append(f'<p style="color:{muted};font-size:10px;margin-top:4px;margin-bottom:0px;">{esc(row["location"])}</p>')
                parts.append('</td></tr></table>')
        elif not report["sections"]:
            parts.append(f'<p style="color:{muted};">未选正文模块</p>')
        parts.append(f'<hr style="color:{palette["line"]};"/><p style="font-size:10px;color:{muted};margin-top:8px;margin-bottom:8px;">'
                     + '<br/>'.join(esc(notice) for notice in report["notices"]) + '</p>')
    else:
        parts.append(f'<p align="center" style="color:{muted};font-size:12px;margin-top:26px;margin-bottom:20px;">'
                     + esc(report['failure'] or '尚未生成') + '</p>')
    style = (f'body{{background-color:{page};color:{ink};font-family:"Microsoft YaHei UI","Microsoft YaHei","Noto Sans SC",sans-serif;'
             'font-size:12px;line-height:1.35;margin:16px;} table{border-collapse:separate;} '
             'p{font-size:12px;margin-top:5px;margin-bottom:5px;} td{vertical-align:top;} '
             '@media print{body{margin:0;}tr{page-break-inside:avoid;}}')
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"/>'
            '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
            f'<title>{esc(report["title"])}</title><style>{style}</style></head>'
            f'<body bgcolor="{page}">' + ''.join(parts) + '</body></html>')


def _markdown(value):
    value = escape(value, quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", value)


def render_markdown(payload=None, options=None, *, image_paths=None):
    report = build_report(payload, options)
    text = ["# " + _markdown(report["title"])]
    if report["subtitle"]:
        text.append(_markdown(report["subtitle"]))
    if not report["ready"]:
        text.append(report["failure"] or "尚未生成报告。")
        return "\n\n".join(text) + "\n"
    if report['settlement']:
        data = report['settlement']
        text += ['## 战斗结算', _markdown(' · '.join(part for part in (
            data['ship_name'], data['map_name'], data['outcome']) if part))]
        metrics = [('造成伤害', data['damage']), *data['metrics']]
        text.append('| ' + ' | '.join(label for label, _ in metrics) + ' |\n| '
                    + ' | '.join('---:' for _ in metrics) + ' |\n| '
                    + ' | '.join(value for _, value in metrics) + ' |')
        if data['ribbons']:
            prefix = '战斗勋带' if data['recorded_ribbons'] else '可核验战绩'
            text.append(prefix + '　' + '　'.join(_markdown(f'{label} ×{value}') for label,value,_ in data['ribbons']))
        if not data['recorded_ribbons']:
            text.append('完整勋带未记录。')
    if teams := report["teams"]:
        text.append("## 敌我阵容")
        if teams["subject"]:
            text.append("复盘舰 · " + _markdown(teams["subject"]))
        for side in teams["sides"]:
            text.append(f'**{side["label"]} · {side["count"]} 艘**　' + _markdown(side["ships"]))
        if not teams["sides"]:
            text.append("阵营待核对")
        elif teams["unknown"]:
            text.append(f'另有 {teams["unknown"]} 艘阵营待核对')
    if report["assessment"]:
        assessment = report["assessment"]
        def line(row):
            images = [(frame, (image_paths or {}).get((row.get('anchor'), i))) for i, frame in enumerate(row.get('visuals', []))]
            citation = ' · '.join(row['locations'][:2]) if report['options']['show_evidence'] and not any(path for _,path in images) else ''
            written = _markdown(row['text']) + ('　' + _markdown(citation) if citation else '')
            for frame, path in images:
                if path:
                    written += f'\n\n![{_markdown(frame["caption"])}]({path})'
            return written
        text += [report["source"], "## 本场总评", line(assessment["verdict"])]
        for section, key, title in (('interpretations','decisions','改变局势的选择'),
                                    ('observations','dimensions','本舰表现'), ('suggestions','actions','再打一次')):
            if section in report['options']['sections'] and assessment[key]:
                text.append('## ' + title)
                for row in assessment[key]:
                    text.append(('- **' + row['label'] + '**　' if row.get('label') else '- ') + line(row))
        if assessment['missing']:
            text.append('## 评价边界')
            text.extend('- ' + row['label'] + '　' + line(row) for row in assessment['missing'])
        return "\n\n".join(text) + "\n"
    summary = report["summary"]
    count = summary["battle_count"]
    battle_text = "比赛数量未知" if count is None else f"{count} 场已标识对局"
    scope = f' / {summary["evidence_count"]} 条所选证据' if report["options"]["show_evidence"] else ''
    text.append(f'{report["source"]} · {report["review_status"]}\n\n{battle_text}{scope}')
    if report["perspective_label"]:
        text.append(report["perspective_label"])
    for model_summary in report["local_model"]:
        text.append("## 本地模型 · WoWSP-D1\n\n" + _markdown(model_summary))
    for section in report["sections"]:
        text.append("## " + section["title"])
        for index, row in enumerate(section["rows"], 1):
            text.append(f'{index}. {_markdown(row["text"])}'
                        + (f'\n\n   {_markdown(row["location"])}' if report["options"]["show_evidence"] else ""))
    if report["unknowns"]:
        text.append("## 待确认与限定")
        for row in report["unknowns"]:
            text.append(_markdown(row["text"]) + ("\n\n" + _markdown(row["location"]) if report["options"]["show_evidence"] else ""))
    text.append("---")
    text.extend(_markdown(notice) for notice in report["notices"])
    return "\n\n".join(text) + "\n"


def _atomic_write(target, content):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".wows-report-", suffix=".tmp", dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def export_report(payload, options, path, format="html"):
    """Publish a UTF-8 report, with standard local image links for Markdown viewers."""
    if format not in ("html", "md", "markdown"):
        raise ValueError("请选择 HTML 或 Markdown 导出格式。")
    report = build_report(payload, options)
    if not report["ready"]:
        raise ValueError("请先选择或生成一份分析报告。")
    target = Path(path).expanduser().resolve()
    if not target.name:
        raise ValueError("请选择报告文件名。")
    target.parent.mkdir(parents=True, exist_ok=True)
    image_paths = {}
    assessment = report['assessment']
    if format != "html" and assessment:
        folder = target.parent / (target.stem + '-战术图')
        keys = ('interpretations','decisions'), ('observations','dimensions'), ('suggestions','actions')
        rows = [assessment['verdict']]
        rows.extend(row for section,key in keys if section in report['options']['sections'] for row in assessment[key])
        for row in rows:
            for index, frame in enumerate(row['visuals']):
                data = base64.b64decode(frame['png_base64'], validate=True)
                name = hashlib.sha256(data).hexdigest()[:16] + '.png'
                folder.mkdir(exist_ok=True)
                _atomic_write(folder / name, data)
                image_paths[(row['anchor'], index)] = quote(f'{folder.name}/{name}')
    content = render_report(payload, options) if format == "html" else render_markdown(payload, options, image_paths=image_paths)
    _atomic_write(target, content.encode('utf-8'))
    return target
