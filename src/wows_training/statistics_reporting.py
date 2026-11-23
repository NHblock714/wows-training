"""Readable statistics and formula-safe CSV exports; unknown is never zero."""
import csv
from datetime import datetime,timezone
from html import escape
import json
from pathlib import Path
import re
from .statistics_analysis import CATEGORIES

CLASSES={'Battleship':'战列舰','Cruiser':'巡洋舰','Destroyer':'驱逐舰','AirCarrier':'航空母舰','Submarine':'潜艇'}
IDENTITIES={'confirmed':'已确认个人','confirmed_account':'主号已确认，赛事账号分开统计','tournament_account':'赛事账号，个人未确认','conflict':'身份有冲突'}
OUTCOMES={'win':'胜','loss':'负','draw':'平','unknown':'未知'}


def percentage(value):return '未知' if value is None else f'{value:.1%}'


def shown(value):
    if value is None:return '未知'
    if type(value) is int:return f'{value:,}'
    if isinstance(value,float):return f'{value:,.1f}' if not value.is_integer() else f'{value:,.0f}'
    return str(value)


def safe(value):
    if value is None:return '未知'
    if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):return "'"+value
    return value


def html_table(headers,rows):
    return '<div class="table"><table><thead><tr>'+''.join('<th>'+escape(str(v))+'</th>' for v in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+escape(shown(v))+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table></div>'


def export_statistics(payload,directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    team=re.sub('[^A-Za-z0-9_-]','_',payload['team']) or 'team'
    prefix=f'{team}_统计_{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")}'
    tables={
      '队伍选船':(['舰船','舰种','出场席位','席位分母','出现局数','比赛分母','出现比例'],[[r['ship_name'],CLASSES.get(r['ship_class'],r['ship_class']),r['appearances'],r['slot_denominator'],r['battle_count'],r['battle_denominator'],f'{r["battle_share"]:.1%}'] for r in payload['team_ships']]),
      '选手选船':(['选手显示名','原始赛号','身份口径','舰船','出场次数','该选手参赛次数','选船比例','胜场','负场','平局','未知赛果','已确认赛果局数','胜率'],[[r['player_name'],' / '.join(r['tournament_names']),IDENTITIES.get(r['identity_status'],r['identity_status']),r['ship_name'],r['appearances'],r['player_appearances'],f'{r["share"]:.1%}',r['wins'],r['losses'],r['draws'],r['unknown_results'],r['known_results'],percentage(r['win_rate'])] for r in payload['player_ships']]),
      '选手承伤':(['选手显示名','原始赛号','参赛次数','承伤完整次数','分类承伤合计','已知部分','已确认局均值']+[name for _,name in CATEGORIES],[[r['player_name'],' / '.join(r['tournament_names']),r['battle_count'],r['confirmed_appearances'],r['total'],r['known_subtotal'],r['confirmed_match_mean']]+[r['categories'][k] for k,_ in CATEGORIES] for r in payload['players']]),
      '逐局承伤':(['比赛编号','录像日期','地图','原始赛号','舰船','分类承伤合计','已知部分','确认字段数','字段总数','局中观测合计_非整局总量','局中观测来源','本方赛果','赛果未知原因','赛果来源SHA']+ [name for _,name in CATEGORIES],[[r['battle_id'],r['date'],r['map_name'],r['tournament_name'],r['ship_name'],r['total'],r['known_subtotal'],r['known_field_count'],r['field_count'],r['observed_untyped_amount'],r['observed_untyped_source_sha'],OUTCOMES[r['outcome']],r['outcome_reason'] or '', ';'.join(s['source_sha'] for s in r['outcome_evidence'])]+[r['categories'][k] for k,_ in CATEGORIES] for r in payload['damage_matches']]),
      '承伤字段证据':(['比赛编号','原始赛号','舰船','原字段','伤害量','状态','公共结果字段索引','来源SHA'],[[r['battle_id'],r['tournament_name'],r['ship_name'],field,value,'冲突' if any(c['field']==field for c in r['conflicts']) else '未知' if value is None else '已确认',payload['public_field_indices'][field],';'.join(s['source_sha'] for s in r['result_sources'])] for r in payload['damage_matches'] for field,value in r['damage_by_field'].items()])}
    paths={}
    for title,(headers,rows) in tables.items():
        path=directory/f'{prefix}_{title}.csv'
        with path.open('w',newline='',encoding='utf-8-sig') as stream:
            writer=csv.writer(stream);writer.writerow(headers);writer.writerows([[safe(v) for v in row] for row in rows])
        paths[title]=str(path)
    path=directory/f'{prefix}.json';path.write_text(json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8');paths['json']=str(path)
    counts=payload['counts'];damage=payload['received_damage']
    body=f'<h1>{escape(payload["team"])} 队伍与选手统计</h1><p>纳入 {counts["included_battles"]} 场，{counts["appearances"]} 个参赛席位；{counts["source_views"]} 份来源。同场只计一次名单。</p>'
    body+=f'<p>分类承伤完整的参赛记录：{damage["confirmed_appearances"]}/{damage["appearances"]}；分类承伤合计：{shown(damage["total"])}。未知记录不会填零。</p>'
    body+='<p>承伤类型来自赛后汇总，没有逐次命中时间。局中回调的观测合计单列，不与结算量相加。</p>'
    body+='<p>选手用船胜率按“选手 × 舰船”分别计算：胜场 ÷（胜场 + 负场 + 平局）。未知赛果不计分母；无已确认赛果时显示未知。出场样本与已确认局数同时展示。</p>'
    for title in ('队伍选船','选手选船','选手承伤','逐局承伤'):
        headers,rows=tables[title];body+='<h2>'+title+'</h2>'+html_table(headers,rows)
    body+='<h2>统计口径</h2><ul>'+''.join('<li>'+escape(v)+'</li>' for v in payload['limitations'])+'</ul>'
    body+='<h2>未纳入或来源问题</h2><pre>'+escape(json.dumps({'excluded':payload['excluded'],'source_errors':payload.get('source_errors',[])},ensure_ascii=False,indent=2))+'</pre>'
    html='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>'+escape(payload['team'])+' 数据统计</title><style>body{margin:30px auto;max-width:1250px;padding:0 20px;background:#f5f8fa;color:#173246;font:15px/1.7 "Microsoft YaHei",sans-serif}h1,h2{color:#0b707e}.table{overflow:auto;margin:20px 0}table{width:100%;border-collapse:collapse;background:white;font-size:13px}td,th{border:1px solid #d6e1e7;padding:9px;vertical-align:top;overflow-wrap:anywhere}th{background:#e7f0f4}pre{white-space:pre-wrap}</style>'+body+'</html>'
    path=directory/f'{prefix}.html';path.write_text(html,encoding='utf8');paths['html']=str(path)
    return paths
