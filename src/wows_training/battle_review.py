"""Source-scoped single battle review, shared by the UI, report studio and export."""
from collections import defaultdict
import hashlib
from html import escape
import json
import os
from pathlib import Path
import tempfile

from .ordinary_replay import METRICS, battle_mode_label, number, quality_label
from .ship_traits import SHIP_TRAIT_KIND, equipped_traits, recorded_build, reviewed_ship_traits, ship_display_name
from .ai_analysis import SINGLE_BATTLE_CONTRACT


REPORT_EVIDENCE_REVISION = 6


def display(value):
    if value is None:return '未知'
    if type(value) in (int,float):return f'{value:,.0f}'
    return str(value)


def clock_text(value):
    return f'{int(value)//60:02d}:{int(value)%60:02d}' if number(value) else '时间未知'


def _model_clock(value):
    if not number(value) or value < 0:
        return '时间未知'
    minutes, tenths = divmod(round(value * 10), 600)
    return f'{minutes:02d}:{tenths / 10:04.1f}'


def _model_summary(decision):
    if not decision['player'] or decision['status'] in ('not_run', 'mismatch', 'unavailable'):
        return '开火选择与走位方向评分未知。' + decision['reason']
    player = decision['player']
    lines = [f"{player['name']} · {player['ship']}。"]
    confidence = decision['policy']['minimum_confidence']
    for axis, name in (('fire', '开火选择'), ('move', '走位方向')):
        metric = decision['metrics'][axis]
        score = metric['score']
        line = f"{name} {score:.1%}" if score is not None else f'{name}未知'
        line += f"（有效 {metric['qualified_count']}/{metric['sample_count']} 点"
        if metric['qualified_count'] and metric['clock_start'] is not None:
            start, end = metric['clock_start'], metric['clock_end']
            coverage = _model_clock(start) if start == end else f'{_model_clock(start)}–{_model_clock(end)}'
            line += f'，录像 {coverage}'
        line += '）'
        if metric['reason']:
            line += '，' + metric['reason'].rstrip('。')
        lines.append(line + '。')
    lines.append(f'分值为模型汇总一致度，有效时点按置信度 ≥ {confidence:g} 统计。')
    if decision['terrain_available'] is False:
        lines.append('模型缺少地图遮挡资料。')
    return ''.join(lines)


def _model_observation(point):
    event = point.get('related_event')
    if event is None:
        return point['text']
    return f"{point['text']} 附近录像 {_model_clock(event['clock'])} · {event['text']}"


def replay_context_rows(report, review, battle_id, *, focus_entity_id=None,
                        focus_team_entity_ids=None, recording_id=None):
    """Give source-derived context facts stable references on their actual clock."""
    from .replay_context_evidence import build_replay_context_evidence
    from .replay_battle_evidence import battle_state_facts
    from .map_terrain import terrain_profile_fact
    from .performance_evidence import build_performance_evidence
    sha = report['sha256']
    facts = build_replay_context_evidence(report, review, focus_entity_id,
                                          focus_team_entity_ids=focus_team_entity_ids)
    facts.extend(battle_state_facts(report))
    if terrain := terrain_profile_fact(report):
        facts.append(terrain)
    facts.extend(build_performance_evidence(report, review, focus_entity_id,
                                            focus_team_entity_ids=focus_team_entity_ids))
    from .spotting_review import spotting_facts
    if focus_team_entity_ids is None or any(p['entity_id'] in focus_team_entity_ids
            for p in review['participants'] if p['name'] == review['recorder']):
        facts.extend(spotting_facts(report, review, focus_entity_id))
    rows = []
    for fact in facts:
        identity = [sha, battle_id, review['clock_domain'], recording_id, fact]
        key = hashlib.sha256(json.dumps(identity,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()[:24]
        row = {**fact, 'id':'context:'+key, 'source_sha':sha, 'battle_id':battle_id,
               'arena_id':report.get('arena_id'), 'clock_domain':review['clock_domain']}
        if recording_id is not None:
            row['recording_id'] = recording_id
        rows.append(row)
    return rows


def _result_for(report,player):
    ordinary=report.get('ordinary') or {};account=player.get('accountDBID')
    rows=(ordinary.get('player_results') or {}).get(str(account)) or {}
    stats=report.get('statistics') or {}
    snapshots=stats.get('result_snapshots') or []
    candidates=[row for s in snapshots if str(s.get('arena_id'))==str(report.get('arena_id'))
                for row in s.get('players',[]) if account is not None and row.get('account_id')==account
                and row.get('entity_id')==player.get('shipId') and row.get('reported_name')==player.get('name')
                and row.get('ship_params_id')==player.get('shipParamsId') and row.get('team_id')==player.get('teamId')]
    values={}
    for key in METRICS:
        numbers=set(v for v in [rows.get(key),*((r.get('battle_metrics') or {}).get(key) for r in candidates)] if number(v))
        values[key]=numbers.pop() if len(numbers)==1 else None
    return values


def build_review(report,catalog=None):
    catalog=catalog or {};header=report.get('metadata') or {};roster=[]
    quality=report.get('quality');host_team=report.get('host_absolute_team');me=header.get('playerName')
    version = str(report.get('replay_version') or '').replace(',', '.')
    for row in report.get('roster') or []:
        sid=row.get('shipParamsId')
        team=row.get('teamId');relation=row.get('relation')
        if type(host_team) is int and host_team in (0,1) and type(team) is int and team in (0,1):
            side='我方' if team==host_team else '敌方'
        else:side={0:'我方',1:'我方',2:'敌方'}.get(relation,'未知')
        ship=ship_display_name(sid,version,catalog)
        roster.append({'name':row.get('name') or '未知','ship':ship,
                       'ship_id':sid,'entity_id':row.get('shipId'),'side':side,'team_id':team,'bot':row.get('_player_kind')==2,
                       'result':_result_for(report,row)})
    entities={r['entity_id']:r for r in roster if type(r.get('entity_id')) is int}
    events=[]
    for d in report.get('death_events') or []:
        if not number(d.get('clock')):continue
        victim=d.get('victim_entity_id');killer=d.get('killer_entity_id')
        events.append({'clock':d['clock'],'kind':'击沉','text':f"{entities.get(killer,{}).get('name','未知来源')} 击沉 {entities.get(victim,{}).get('name','未知舰船')}",
                       'entities':[e for e in (killer,victim) if e in entities]})
    caps={str(c.get('entity_id')):str(c.get('name') or '?') for c in report.get('capture_points') or []}
    for cid,changes in (report.get('cap_team_changes') or {}).items():
        for row in changes:
            if isinstance(row,(list,tuple)) and len(row)>=2 and number(row[0]) and row[0]>0:
                side='中立' if row[1]==-1 else '我方' if host_team in (0,1) and row[1]==host_team else '敌方' if row[1] in (0,1) and host_team in (0,1) else '归属未知'
                events.append({'clock':row[0],'kind':'占领区','text':f"{caps.get(str(cid),'?')} 区 · {side}",'entities':[]})
    observations=(report.get('ordinary') or {}).get('observed_damage') or (report.get('statistics') or {}).get('damage_events') or []
    damage=[]
    for item in observations:
        t=(item.get('source') or {}).get('packet_clock');amount=item.get('amount')
        if number(t) and number(amount) and amount>0:
            damage.append({'clock':t,'amount':amount,'actor':item.get('attacker_entity_id'),'target':item.get('victim_entity_id')})
    events.sort(key=lambda r:r['clock']);damage.sort(key=lambda r:r['clock'])
    return {'mode':battle_mode_label(header),'map':report.get('display_map_name') or header.get('mapDisplayName') or header.get('mapName') or '未知',
            'recorder':me or '未知','date':header.get('dateTime') or '未知','version':str(report.get('replay_version') or '未知').replace(',','.'),
            'outcome':{'win':'胜利','loss':'失败','draw':'平局'}.get(report.get('outcome'),'未知'),
            'quality':quality_label(quality),'full_result':bool((report.get('post_battle_common') or {}).get('arena_id')),
            'duration':(report.get('post_battle_common') or {}).get('duration_sec'), 'clock_max':(report.get('packet_clock_range') or {}).get('max'),
            'clock_domain':'battle_elapsed_seconds' if report.get('multiview') else 'packet_clock',
            'participants':roster,'events':events,'damage_events':damage,'initial_roster':(report.get('ordinary') or {}).get('initial_count')}


def _confirmed_teams(report, model, focus):
    participants = model["participants"]
    ids = [p["entity_id"] for p in participants]
    teams = {p["team_id"] for p in participants if type(p["team_id"]) is int and p["team_id"] in (0, 1)}
    if (focus is None or report.get("roster_matches_header") is not True or teams != {0, 1}
            or type(focus.get("team_id")) is not int or focus["team_id"] not in teams
            or any(type(entity) is not int or entity <= 0 for entity in ids) or len(set(ids)) != len(ids)):
        return None
    team = focus["team_id"]
    friendly = [p for p in participants if type(p["team_id"]) is int and p["team_id"] == team]
    hostile = [p for p in participants if type(p["team_id"]) is int and p["team_id"] == 1 - team]
    return team, friendly, hostile


def _related_builds(report, model, focus, by_entity, catalog):
    ships = (catalog or {}).get("ships", catalog or {})
    entity = focus["entity_id"]
    impact = defaultdict(float)
    for hit in model["damage_events"]:
        if hit["actor"] == entity:
            impact[hit["target"]] += hit["amount"]
        elif hit["target"] == entity:
            impact[hit["actor"]] += hit["amount"]
    support = []
    for other in sorted(impact, key=impact.get, reverse=True):
        p = by_entity.get(other)
        if p is None or other == entity:
            continue
        species = ships.get(str(p["ship_id"]), {}).get("species")
        build = recorded_build(report, other, p["ship_id"], species)
        if build and build["support"]:
            support.append({"entity_id": other, "ship_name": p["ship"], "team_id": p["team_id"], **build})
            if len(support) == 6:
                break
    return support


def _damage_windows(events, subject, players):
    """Group closely timed damage callbacks for the same known attacker and target."""
    groups = defaultdict(list)
    for event in events:
        actor, target = event["actor"], event["target"]
        if subject in (actor, target) and actor != target and actor in players and target in players:
            groups[actor, target].append(event)

    by_direction = {"dealt": [], "received": []}
    for (actor, target), hits in groups.items():
        direction = "dealt" if actor == subject else "received"
        candidates = by_direction[direction]
        window = []
        for hit in hits:
            if window and hit["clock"] - window[0]["clock"] > 1.25:
                if len(window) > 1:
                    candidates.append((actor, target, window))
                window = []
            window.append(hit)
        if len(window) > 1:
            candidates.append((actor, target, window))

    for direction, candidates in by_direction.items():
        largest = sorted(candidates, key=lambda item: (
            -sum(hit["amount"] for hit in item[2]), item[2][0]["clock"]))[:6]
        for actor, target, hits in largest:
            yield direction, actor, target, hits


def build_battle_evidence(report,catalog=None,*,decision=None,random_focus=False,recording_id=None,focus_entity_id=None):
    model=build_review(report,catalog);sha=report['sha256'];arena=report.get('arena_id')
    version=str(report.get('replay_version') or '').replace(',','.')
    battle=f'arena:{version}:{arena}' if arena else 'replay:'+sha
    people=[];rows=[]
    def add(kind,text,entities,t=None,*,end=None,details=None):
        clock_end = t if end is None else end
        identity = [sha,kind,text,entities,t,len(rows)]
        if clock_end != t:
            identity.append(clock_end)
        key=hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()[:20]
        row = {'id':'battle:'+key,'source_sha':sha,'arena_id':arena,'battle_id':battle,'kind':kind,'text':text,
               'entity_ids':entities,'clock_start':t,'clock_end':clock_end,'clock_domain':model['clock_domain']}
        if details is not None:
            row['details'] = details
        rows.append(row)
    by_entity={p['entity_id']:p for p in model['participants'] if type(p['entity_id']) is int}
    source_matches = (decision or {}).get('source_sha') == sha
    scored = ((decision or {}).get('player') or {}).get('entity_id') if source_matches else None
    recorded = [p for p in model['participants'] if model['recorder'] != '未知' and p['name'] == model['recorder']]
    requested = focus_entity_id if focus_entity_id is not None else scored
    focus = by_entity.get(requested) if requested is not None else (recorded[0] if len(recorded) == 1 else None)
    focus_entity = focus.get('entity_id') if focus else None
    teams = _confirmed_teams(report, model, focus)
    if random_focus:
        team_support = []
        if teams:
            own_team, friendly, hostile = teams
            for side, fleet in (('我方', friendly), ('敌方', hostile)):
                team_support.extend({'ship_name': p['ship'], 'team_id': p['team_id'], 'side': side,
                                     'entity_id': p['entity_id'], 'is_focus': p is focus} for p in fleet)
            withheld = len(model['participants']) - len(team_support)
            text = (f"以复盘舰{focus['ship']}为准，我方是队伍 {own_team}，敌方是队伍 {1-own_team}。"
                    f"已核对我方 {len(friendly)} 艘、敌方 {len(hostile)} 艘。")
            if withheld:
                text += f"另有 {withheld} 艘阵营未知。"
            details = {'status': 'verified', 'team_id': own_team, 'ship_name': focus['ship'],
                       'recorder_team_id': report.get('host_absolute_team'), 'count': withheld, 'support': team_support,
                       'source_fields': ['roster.teamId', 'roster.shipId', 'roster.shipParamsId']}
        else:
            text = '复盘舰或双方阵营未能通过录像名单核对，请仅按已知队伍标识描述。'
            details = {'status': 'unknown', 'ship_name': focus['ship'] if focus else None}
        add('replay_team_identity', text, [p['entity_id'] for p in team_support], details=details)
    # Keep a source for each participant even if only the title roster is available.
    for p in model['participants']:
        entity=p['entity_id'];ids=[entity] if type(entity) is int else []
        people.append({'name':p['name'],'battle_id':battle,'entity_id':entity,'ship_name':p['ship']})
        side = ('我方' if p in teams[1] else '敌方' if p in teams[2] else '阵营未知') if teams else p['side']
        if random_focus and not teams:
            side = f"队伍 {p['team_id']}" if p['team_id'] in (0, 1) else '阵营未知'
        add('ordinary_lineup',f"{side} {p['ship']} · {p['name']}",ids)
        known=[f'{METRICS[k]} {display(v)}' for k,v in p['result'].items() if v is not None]
        if known:add('ordinary_result',f"{p['name']} 赛后结算："+'，'.join(known),ids)
    for ev in model['events']:
        add('ordinary_event',ev['text'],ev['entities'],ev['clock'])
    large_hits = sorted(model['damage_events'],key=lambda e:e['amount'],reverse=True)[:12]
    for hit in large_hits:
        src=by_entity.get(hit['actor'],{}).get('ship','未知来源');dst=by_entity.get(hit['target'],{}).get('ship','未知舰船')
        add('ordinary_event',f'该视角观测到 {src} 对 {dst} 造成 {display(hit["amount"])} 伤害，伤害类型未知。',
            [e for e in (hit['actor'],hit['target']) if e in by_entity],hit['clock'])
    if not model['full_result']:add('ordinary_result','该视角缺少已核验的赛后结算，最终赛果和结算数据未知。',[])
    if decision is not None and decision.get('source_sha') == sha:
        player = decision['player'] or {}
        entity = player.get('entity_id')
        ids = [entity] if type(entity) is int else []
        policy = decision.get('policy') or {}
        details = {'status':decision['status'], 'model':'WoWSP-D1', 'model_sequence':decision['model_sequence'],
                   'policy':dict(policy), 'terrain_available':decision.get('terrain_available'),
                   'support':[{'axis':axis, 'score':m.get('score'), 'sample_count':m.get('sample_count'),
                               'confident_count':m.get('qualified_count'), 'clock_start':m.get('clock_start'),
                               'clock_end':m.get('clock_end')} for axis,m in (decision.get('metrics') or {}).items()]}
        add('local_model_summary', _model_summary(decision), ids, details=details)
        for point in decision['checkpoints']:
            if model['clock_domain'] != 'packet_clock':
                break
            add('local_model_checkpoint', _model_observation(point), ids, point['clock_start'], end=point['clock_end'],
                details={'axis':point['axis'], 'status':'usable', 'recommendation':point['recommendation'],
                         'sample_count':point['sample_count'], 'confidence_min':point['confidence_min'],
                         'score':point['score'], 'clock':point['clock']})
    if decision is not None or random_focus or focus_entity is not None:
        rows.extend(replay_context_rows(report, model, battle, focus_entity_id=focus_entity,
                                        recording_id=recording_id))
    if not report.get('multiview'):
        from .replay_storyboard import build_replay_storyboard_rows
        sheets = build_replay_storyboard_rows(report, focus_entity_id=focus_entity)
        rows.extend(sheets)
        if sheets:
            fleet = []
            ships = (catalog or {}).get('ships', catalog or {})
            for mark in sheets[0]['details']['support']:
                vessel = by_entity.get(mark['entity_id'], {})
                info = ships.get(str(mark.get('ship_id')), {})
                fleet.append({**mark, 'ship_name':vessel.get('ship', '未知'), 'species':info.get('species'),
                              'tier':info.get('tier'), 'nation':info.get('nation')})
            add('replay_fleet_key', '分镜标注舰船名称。按阵营、方位和时点区分同名舰，录制方与复盘方见对局范围。',
                [ship['entity_id'] for ship in fleet], details={'support':fleet})
    if random_focus:
        entity = (focus or {}).get('entity_id')
        ids = [entity] if type(entity) is int else []
        subject = f"{focus['name']} · {focus['ship']}" if focus else '录制者，舰位待确认'
        context = f"{model['mode']} · {model['map']}。复盘对象 {subject}。录制者 {model['recorder']}，录制方赛果 {model['outcome']}。"
        add('ordinary_overview', context, ids)
        if focus and (traits := reviewed_ship_traits(report, focus)):
            text, details = traits
            add(SHIP_TRAIT_KIND, text, ids, details=details)
        if ids and (builds := _related_builds(report, model, focus, by_entity, catalog)):
            text = '本局有交火记录的其他舰船实装情况。' + '。'.join(
                f"{row['ship_name']}（队伍 {row['team_id']}） {equipped_traits(row)}" for row in builds) + '。'
            add('replay_engagement_loadouts', text, [row['entity_id'] for row in builds],
                details={'support': builds, 'source_fields': ['Vehicle.shipConfig', 'GameParams.Modernization.modifiers']})
        if ids and model['clock_domain'] == 'packet_clock':
            for direction, actor, target, hits in _damage_windows(model['damage_events'], entity, by_entity):
                source, victim = by_entity[actor], by_entity[target]
                start, end = hits[0]['clock'], hits[-1]['clock']
                total = sum(hit['amount'] for hit in hits)
                add('replay_damage_window',
                    f"{source['ship']}向{victim['ship']}的 {len(hits)} 笔伤害回调相隔 "
                    f"{end-start:.2f} 秒，观测总量 {display(total)}，伤害类型及是否同一轮攻击未知。",
                    [actor, target], start, end=end,
                    details={'direction': direction, 'entity_id':actor, 'target_id':target, 'amount':round(total,2),
                             'count':len(hits), 'source_fields':['damage_events'],
                             'support':[{'clock':h['clock'], 'amount':round(h['amount'],2)} for h in hits[:24]]})
            for field, title in (('actor','输出'), ('target','承伤')):
                first = next((event for event in model['damage_events'] if event[field] == entity), None)
                if first is not None and first not in large_hits:
                    involved = [value for value in (first['actor'],first['target']) if value in by_entity]
                    add('ordinary_event', f"本录制视角首次记录该舰{title} {display(first['amount'])}，伤害类型未知。",
                        involved, first['clock'])
                    large_hits.append(first)

        def priority(row):
            kind = row['kind']
            if kind == 'ordinary_overview':
                return 0
            if kind.startswith('local_model_'):
                return 1
            if kind.startswith('replay_'):
                return 2
            if entity is not None and entity in row['entity_ids']:
                return 3
            if kind == 'ordinary_event':
                return 4
            return 5

        rows.sort(key=priority)
    return {'kind':'ordinary_battle','report_contract':SINGLE_BATTLE_CONTRACT,'focus_entity_id':focus_entity,
            'report_evidence_revision':REPORT_EVIDENCE_REVISION,
            'battle_mode':model['mode'],'participants':people,'evidence':rows,'battles':[{'battle_id':battle}],
            'counts':{'battles':1},'source_name':f"{model['recorder']} · {model['map']}",'team_selector':{}}


def export_review(report,catalog,path,*,decision=None):
    data=build_review(report,catalog);path=Path(path)
    if decision is not None and decision.get('source_sha') == report.get('sha256'):
        data['local_model'] = decision
    if path.suffix.lower()=='.json':
        content=json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)
    else:
        def table(headers,rows):
            cells=lambda tag,values:''.join(f'<{tag}>{escape(display(v))}</{tag}>' for v in values)
            return '<table><thead><tr>'+cells('th',headers)+'</tr></thead><tbody>'+''.join('<tr>'+cells('td',r)+'</tr>' for r in rows)+'</tbody></table>'
        def title(s):return escape(str(s))
        title_text=f"{data['mode']} · {data['map']}"
        rows=[[p['side'],p['name'],p['ship'],*(p['result'][k] for k in METRICS)] for p in data['participants']]
        model_html = ''
        if 'local_model' in data:
            model_html = '<h2>本地模型 · WoWSP-D1</h2><p>'+title(_model_summary(decision))+'</p>'
            if decision['checkpoints']:
                model_html += table(['录像时间','模型观察','复盘建议'], [
                    [f"{_model_clock(point['clock_start'])}–{_model_clock(point['clock_end'])}",
                     _model_observation(point), point['recommendation']] for point in decision['checkpoints']])
        content=('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>'+title(title_text)+'</title>'
          '<style>body{background:#142331;color:#e6e9df;font:15px "Microsoft YaHei",sans-serif;max-width:1150px;margin:42px auto;padding:0 24px}h1,h2{color:#e4c48b}table{width:100%;border-collapse:collapse}td,th{padding:7px;text-align:right;border-bottom:1px solid #405462}td:nth-child(-n+3),th:nth-child(-n+3){text-align:left}th{color:#b6ddd8}</style>'
          '<h1>'+title(title_text)+'</h1><p>'+title(' · '.join([data['recorder'],data['date'],data['outcome'],data['quality']]))+'</p>'
          +model_html+'<h2>双方阵容与结算</h2>'+table(['阵营','玩家','舰船',*METRICS.values()],rows)+'<h2>关键时刻</h2>'
          +table(['比赛时间' if report.get('multiview') else '录像时间','事件','记录'],[[clock_text(e['clock']),e['kind'],e['text']] for e in data['events']])
          +'<h2>局中伤害观测</h2><p>仅计该视角记录到的伤害。</p>'+table(['录像时间','攻击方','受击方','观测伤害'],
          [[clock_text(e['clock']),e['actor_name'],e['target_name'],e['amount']] for e in _named_damage(data)] )+'</html>')
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.battle-review-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf8') as stream:stream.write(content)
        os.replace(tmp,path)
    finally:Path(tmp).unlink(missing_ok=True)
    return path


def _named_damage(data):
    names={p['entity_id']:p['name'] for p in data['participants'] if p['entity_id'] is not None}
    return [e | {'actor_name':names.get(e['actor'],'未知'),'target_name':names.get(e['target'],'未知')} for e in data['damage_events']]
