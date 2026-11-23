"""Escaped offline reports for rules, training and reviewed AI text."""
from __future__ import annotations
import csv
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
from uuid import uuid4

LABELS = {"counts":"样本范围", "groups":"可比较分组", "battles":"比赛记录", "ship_usage":"舰船出场", "patterns":"重复模式",
    "fusion_checks":"多视角核查", "coverage":"观测覆盖", "conditions":"适用条件", "results":"计划对照", "comparisons":"训练比较",
    "plans":"对抗方案", "options":"备选方案", "metrics":"指标", "status":"状态", "basis":"依据", "actions":"建议行动",
    "resources":"所需资源", "risks":"风险", "adjustment_signals":"调整信号", "limitations":"限制", "observations":"AI 观察表述",
    "interpretations":"AI 解释", "suggestions":"AI 建议", "needs_review":"待核查", "exclusions":"排除项",
    "evidence_ids":"证据引用", "numerator":"满足规则的比赛数", "denominator":"可比较比赛数", "ratio":"比例",
    "exceptions":"例外局", "battle_ids":"比赛", "input_reports":"输入回放", "unique_views":"去重视角",
    "selected_battles":"纳入比赛", "source_sha":"来源 SHA", "clock_start":"起始包时间", "clock_end":"结束包时间"}

def cell(value):
    if value is None:
        return "未记录"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    return str(value)

def table(headers, rows):
    return '<div class="table"><table><thead><tr>' + ''.join('<th>'+escape(str(h))+'</th>' for h in headers) + '</tr></thead><tbody>' + ''.join(
        '<tr>'+''.join('<td>'+escape(cell(v))+'</td>' for v in row)+'</tr>' for row in rows) + '</tbody></table></div>'

def render_run(run, reviews=None, *, preview=False):
    payload = run.get("payload", run)
    parts = [f'<h1>{escape(run.get("name") or "赛训分析报告")}</h1>', '<p>离线规则结果与建议分开保存；证据可定位原比赛。AI 文本始终需要人工判断。</p>']
    source_table=table(["项目", "记录"], [["分析类型", run.get("kind")], ["生成时间（UTC）",run.get("created_at")],
                 ["分析对象",payload.get("team_selector") or payload.get("opponent_selector")],
                 ["规则版本",payload.get("rules_version")], ["状态",payload.get("status")], ["输入指纹", (run.get("input_fingerprint") or {}).get("hash")]])
    selector=payload.get("team_selector") or payload.get("opponent_selector") or {}
    subject=selector.get("clan_tag") or (f"绝对队伍 {selector['team_id']}" if "team_id" in selector else "按当前记录")
    counts=payload.get("counts") or {}
    if counts:
        parts.append(table(["分析对象","纳入比赛","录制来源","比较组"],[[subject,counts.get("selected_battles"),counts.get("unique_views"),counts.get("comparison_groups")]]))
    status={"partial":"部分观测可用","comparable":"存在可比较结果","not_comparable":"当前样本不可比较",
            "general_only":"仅一般准备建议","conditional_options":"条件化备选方案","insufficient_evidence":"依据不足","unknown":"尚不能判断"}.get(payload.get("status"),payload.get("status"))
    if status:parts.append('<p>'+escape(str(status))+'；请同时查看覆盖率、样本分母和例外局。</p>')
    presentation=payload.get("presentation") or {}
    if payload.get("ship_usage"):
        parts.append('<h2>所选队伍的舰船出场</h2>'+table(["舰船","出场 / 阵容总席位","出现局 / 纳入局"],[
            [(presentation.get("ships") or {}).get(str(row["ship_id"]),row["ship_id"]),
             f'{row["appearance_count"]} / {row["appearance_denominator"]}',
             f'{row["battle_count"]} / {row["battle_denominator"]}'] for row in payload["ship_usage"]]))
    if payload.get("kind")=="collection_analysis":
        parts.append('<h2>可比较的部署规律</h2>')
        for group in payload.get("groups",[]):
            conditions=group["conditions"];name=(presentation.get("maps") or {}).get(conditions["map_id"],conditions["map_id"])
            side={"N":"北","S":"南","E":"东","W":"西"}.get(conditions.get("spawn_side"),"未知")
            parts.append(f'<h3>{escape(str(name))} · 开局观测方位 {side} · {len(group["battle_ids"])} 场</h3>')
            parts.append('<p>'+escape(str(conditions.get("mode"))+'；'+str(conditions.get("version")))+('</p>' if group["comparable"] else '；缺少可比条件，独立保留。</p>'))
            parts.append(table(["规则","满足 / 可比较","例外局","判断"],[
                [row["label"],f'{row["numerator"]} / {row["denominator"]}',len(row["exception_battle_ids"]),
                 "达到样本门槛" if row["status"]=="observed_pattern" else "样本或观测不足"] for row in group["patterns"]]))
        battle_rows=[]
        for battle in payload.get("battles",[]):
            dwell={};missing=set()
            for entity in battle.get("spatial",{}).get("entities",[]):
                for zone in entity.get("zones",[]):
                    value=zone.get("inside_seconds")
                    if isinstance(value,(int,float)) and not isinstance(value,bool):
                        dwell[zone["zone_id"]]=dwell.get(zone["zone_id"],0)+value
                    else:
                        missing.add(zone["zone_id"])
            ratio=battle.get("coverage",{}).get("ratio")
            regions=[]
            for key in sorted(set(dwell)|missing):
                label=f"{dwell[key]:.1f}" if key in dwell else "未知"
                if key in dwell and key in missing:label+="（仅有记录的舰船）"
                regions.append(f"{key}: {label}")
            battle_rows.append([battle["battle_id"],"未知" if ratio is None else f"{ratio:.1%}",
                "；".join(regions) or "无有效记录",
                len(battle.get("consumable_windows",{}).get("windows",[]))])
        parts.append('<h2>逐局覆盖与空间记录</h2>'+table(["比赛","观测覆盖","区域采样驻留（船·秒）","消耗品记录"],battle_rows))
    keys = ("groups", "plans", "options", "results", "comparisons", "metrics", "observations", "interpretations", "suggestions", "needs_review", "fusion_checks", "exclusions", "limitations")
    for key in keys:
        if key=="groups" and payload.get("kind")=="collection_analysis":
            continue
        value = payload.get(key)
        if value in (None, [], {}):
            continue
        parts.append(f'<h2>{LABELS.get(key,key)}</h2>')
        if isinstance(value, list) and all(isinstance(row, dict) for row in value):
            for index, row in enumerate(value):
                parts.append(f'<h3>{escape(str(row.get("name") or row.get("title") or row.get("id") or index+1))}</h3>')
                parts.append(table(["项目","内容"], [[LABELS.get(k,k),v] for k,v in row.items() if k != "raw_response"]))
        elif isinstance(value, dict):
            parts.append(table(["项目","内容"], [[LABELS.get(k,k),v] for k,v in value.items()]))
        else:
            parts.append('<pre>'+escape(cell(value))+'</pre>')
    evidence = payload.get("evidence") or []
    parts.append('<h2>来源与分析版本</h2>'+source_table)
    if evidence:
        displayed=evidence[:100] if preview else evidence
        parts.append('<h2>证据索引</h2>'+table(["ID","比赛","起始记录秒","结束记录秒","说明"], [
            [row.get("id"),row.get("battle_id"),row.get("clock_start"),row.get("clock_end"),row.get("text") or row.get("details")] for row in displayed]))
        if preview and len(evidence)>100:
            parts.append(f'<p>报告预览展示前 100 / {len(evidence)} 条；下方证据表和导出文件保留完整索引。</p>')
    if reviews:
        parts.append('<h2>教练批注</h2>'+table(["时间","处理","批注"], [[row.get("created_at"),row.get("decision"),row.get("comment")] for row in reviews]))
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>赛训分析</title><style>body{background:#f0f4f8;color:#182b40;font:14px/1.6 "Microsoft YaHei",sans-serif;margin:0}main{max-width:1200px;margin:auto;padding:28px}table{border-collapse:collapse;width:100%;background:white}th,td{color:#182b40;background:#fff;padding:9px;border:1px solid #d6e0eb;text-align:left;vertical-align:top;white-space:pre-wrap;overflow-wrap:anywhere}th{background:#e4eef7}pre{white-space:pre-wrap;overflow-wrap:anywhere}.table{overflow:auto;margin-bottom:18px}h2{margin-top:28px}</style><main>'+''.join(parts)+'</main></html>'

def export_run(run, output_dir, reviews=None):
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    stem = f"wows_{run.get('kind','analysis')}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:8]}"
    paths = {kind: root/(stem+'.'+kind) for kind in ("html","json","csv")}
    paths["html"].write_text(render_run(run,reviews), encoding="utf-8")
    paths["json"].write_text(json.dumps({"run":run,"reviews":reviews or []},ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    with paths["csv"].open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream)
        writer.writerow(["证据ID","比赛","来源SHA256","起始记录秒","结束记录秒","类型","说明"])
        for row in run.get("payload",run).get("evidence",[]):
            values=[row.get(k) for k in ("id","battle_id","source_sha","clock_start","clock_end","kind","text")]
            writer.writerow([("'"+v) if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for v in values])
        for group in ("observations","interpretations","suggestions","needs_review"):
            for row in run.get("payload",run).get(group,[]):
                text=cell(row.get("text",row))
                values=[",".join(row.get("evidence_ids",[])),"","","","",f"AI {group}（不是观测事实）",text]
                writer.writerow([("'"+v) if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for v in values])
    return {kind:str(path) for kind,path in paths.items()}
