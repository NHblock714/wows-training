"""Deduplicated team/player ship and received-damage summaries with unknowns."""
from collections import Counter,defaultdict
from copy import deepcopy
import re
from .identity_rules import parse_kots_name
from .statistics_capture import number

CATEGORIES=(('main_ap','主炮 AP'),('main_he','主炮 HE'),('main_cs','主炮 CS/SAP'),('secondary','副炮'),
            ('torpedo','鱼雷'),('fire','火灾'),('flood','进水'),('ram','撞击'),('air','航空武器'),('other','其他类型'))
WIN_RATE_RULE='胜率 = 胜场 ÷（胜场 + 负场 + 平局）；未知赛果不计入分母，无已确认赛果时显示未知。'


def aggregate_outcomes(rows):
    counts=Counter(r.get('outcome','unknown') for r in rows)
    known=sum(counts[k] for k in ('win','loss','draw'))
    return {'wins':counts['win'],'losses':counts['loss'],'draws':counts['draw'],
            'unknown_results':sum(counts.values())-known,'known_results':known,
            'win_rate':counts['win']/known if known else None}


def team_outcome(battle,roster,selected):
    reference=Counter((p['accountDBID'],p['shipParamsId']) for p in roster)
    selected_ids=Counter((p['accountDBID'],p['shipParamsId']) for p in selected)
    arena=str(battle.get('arena_id') or battle['battle_id'].rsplit(':',1)[-1])
    evidence=[];rejected=[];conflict=False
    for report in battle.get('result_sources',[]):
        source={'source_sha':report['source_sha'],'report_sha256':report.get('report_sha256')}
        players=[p for p in report.get('roster',[]) if type(p.get('shipId')) is int and p['shipId']>0 and p.get('_player_kind')!=3]
        version='.'.join(s.strip() for s in re.split(r'[.,_]',report.get('replay_version') or '') if s.strip())
        common=report.get('post_battle_common') or {}
        reason=None
        if (version!='15.9.0.13357625' or str(report.get('arena_id'))!=arena
                or not isinstance(common,dict) or str(common.get('arena_id'))!=arena):
            reason='缺少目标版本同场公共结算'
        elif (Counter((p.get('accountDBID'),p.get('shipParamsId')) for p in players)!=reference
                or any(type(p.get('teamId')) is not int or p['teamId'] not in (0,1) for p in players)):
            reason='结算来源参赛名单或队伍归属不一致'
        else:
            teams={p['teamId'] for p in players if (p.get('accountDBID'),p.get('shipParamsId')) in selected_ids}
            if len(teams)!=1 or Counter((p['accountDBID'],p['shipParamsId']) for p in players if p['teamId'] in teams)!=selected_ids:
                reason='结算来源不能对应到所选队伍完整阵容'
        if reason:
            rejected.append({**source,'reason':reason});continue
        winner=common.get('winner_team_id');reported=report.get('winner_absolute_team')
        if (report.get('post_battle_result_errors') or report.get('winner_source')=='conflict'
                or (type(reported) is int and reported in (-1,0,1) and reported!=winner)):
            conflict=True;reason='来源结算存在冲突或解析错误'
        elif report.get('winner_source')!='verified_15.9_common_results' or type(winner) is not int or winner not in (-1,0,1):
            reason='胜方未取得已核验的公共结算'
        if reason:
            rejected.append({**source,'reason':reason});continue
        side=next(iter(teams));outcome='draw' if winner==-1 else 'win' if winner==side else 'loss'
        evidence.append({**source,'arena_id':arena,'winner_source':report['winner_source'],
                         'winner_absolute_team':winner,'selected_absolute_team':side,'outcome':outcome})
    outcomes={r['outcome'] for r in evidence}
    conflict=conflict or len(outcomes)>1
    value=next(iter(outcomes)) if len(outcomes)==1 and not conflict else 'unknown'
    return {'outcome':value,'outcome_conflict':conflict,'outcome_evidence':evidence,'outcome_rejections':rejected,
            'outcome_reason':'结算来源冲突，赛果未知' if conflict else '未取得可核验的同场同队赛果' if not outcomes else None}


def category(field):
    suffix=field.removeprefix('received_damage_')
    if suffix in ('main_ap','main_he','main_cs'):return suffix
    if suffix.startswith('atba_'):return 'secondary'
    if suffix.startswith('tpd_'):return 'torpedo'
    if suffix in ('fire','flood','ram'):return suffix
    if suffix.startswith(('bomb','tbomb','rocket','skip','adbomb','dbomb_airsupport')):return 'air'
    return 'other'


def known_sum(values):
    values=list(values)
    return sum(values) if values and all(number(v) for v in values) else None


def aggregate_damage(rows):
    complete=[r for r in rows if r['total'] is not None]
    parts=[r['known_subtotal'] for r in rows if r['known_subtotal'] is not None]
    categories={key:known_sum(r['categories'][key] for r in rows) for key,_ in CATEGORIES}
    return {'appearances':len(rows),'confirmed_appearances':len(complete),'unknown_appearances':len(rows)-len(complete),
            'total':known_sum(r['total'] for r in rows),'known_subtotal':sum(parts) if parts else None,
            'confirmed_match_mean':sum(r['total'] for r in complete)/len(complete) if complete else None,
            'categories':categories,
            'category_known_counts':{key:sum(r['categories'][key] is not None for r in rows) for key,_ in CATEGORIES}}


def received_for_player(player,sources,fields):
    account=player['accountDBID'];candidates=defaultdict(list);used={};untyped=[]
    for source in sources:
        if not source.get('core_verified'):continue
        stats=source.get('statistics') or {}
        if stats.get('received_damage_fields')!=fields:continue
        sha=source['source_sha']
        for snapshot in stats.get('result_snapshots',[]):
            for row in snapshot['players']:
                if row['account_id']!=account or row['ship_params_id']!=player['shipParamsId']:continue
                evidence={'source_sha':sha,**snapshot['source'],'public_account_key':row['source_public_key']}
                if any(number(v) for v in row['damage_by_field'].values()):used[sha]=evidence
                for field in fields:
                    value=row['damage_by_field'].get(field)
                    if number(value):candidates[field].append((value,evidence))
        entity=next((r['shipId'] for r in source['roster'] if r.get('accountDBID')==account),None)
        events=[r for r in stats.get('damage_events',[]) if r['victim_entity_id']==entity]
        owner=any(r.get('accountDBID')==account and r.get('teamId')==source.get('host_absolute_team') for r in source['roster'])
        if events:untyped.append((not owner,-len(events),sha,events))
    values={};conflicts=[]
    for field in fields:
        distinct={v for v,_ in candidates[field]}
        values[field]=next(iter(distinct)) if len(distinct)==1 else None
        if len(distinct)>1:conflicts.append({'field':field,'candidates':[{'value':v,'source':e} for v,e in candidates[field]]})
    categories={key:known_sum(v for name,v in values.items() if category(name)==key) for key,_ in CATEGORIES}
    known_values=[v for v in values.values() if v is not None]
    observed=sorted(untyped,key=lambda r:r[:3])[0] if untyped else None
    return {'damage_by_field':values,'total':known_sum(values.values()),'known_subtotal':sum(known_values) if known_values else None,
            'categories':categories,'known_field_count':sum(v is not None for v in values.values()),'field_count':len(fields),
            'conflicts':conflicts,'result_sources':list(used.values()),'time_scope':'post_battle_aggregate_no_per_hit_time',
            'observed_untyped_amount':sum(e['amount'] for e in observed[3]) if observed else None,
            'observed_untyped_source_sha':observed[2] if observed else None,'observed_untyped_event_count':len(observed[3]) if observed else 0}


def summarize(battles,team,fields,ship_catalog):
    output={'schema_version':1,'kind':'team_player_statistics','team':team,'categories':[{'id':k,'name':n} for k,n in CATEGORIES],
            'received_damage_fields':fields,'team_ships':[],'player_ships':[],'players':[],'damage_matches':[],
            'battles':[],'excluded':[],'limitations':['按内部比赛编号去重；出场席位与出现局数分别计数。',
            '未确认身份按赛事账号与原赛号统计；不猜测借号、截断或真实个人对应。',
            WIN_RATE_RULE,'赛果按选手所属队伍与已核验公共结算匹配；双边队伍编号转换后核对，冲突保留未知。',
            '承伤为经校验的赛后分类字段合计，没有逐次类型发生时间。缺失或跨来源冲突均为未知。',
            '局中伤害回调未携带类型且可能不完整，单独列为观测合计，不加到结算承伤中。']}
    appearances=defaultdict(list);ship_slots=Counter();ship_battles=defaultdict(set);player_ship=defaultdict(list);seen=set()
    for battle in battles:
        if battle['battle_id'] in seen:continue
        seen.add(battle['battle_id'])
        roster=[p for p in battle['roster'] if type(p.get('shipId')) is int and p['shipId']>0 and type(p.get('teamId')) is int and p.get('_player_kind')!=3]
        selected=[]
        for p in roster:
            parsed=parse_kots_name(p.get('name',''),battle['event_key'])
            if (parsed.get('clan') or '').casefold()==team.casefold() and parsed.get('event_matches_suffix'):selected.append(p)
        sides={p['teamId'] for p in selected}
        if len(sides)!=1 or len(selected)!=sum(p['teamId'] in sides for p in roster):
            output['excluded'].append({'battle_id':battle['battle_id'],'reason':'队伍前缀缺失、不完整或在双方同时出现；请核对队伍选择与原赛号。'});continue
        result=team_outcome(battle,roster,selected)
        reference={(p['accountDBID'],p['shipParamsId']) for p in roster}
        sources=[]
        for source in battle['sources']:
            signature={(p.get('accountDBID'),p.get('shipParamsId')) for p in source.get('roster',[]) if type(p.get('shipId')) is int and p['shipId']>0 and p.get('_player_kind')!=3}
            if signature==reference:sources.append(source)
            else:output.setdefault('source_warnings',[]).append({'source_sha':source.get('source_sha'),'reason':'来源名单不一致，统计未采用此源。'})
        selected_ids=[]
        ids=[(p.get('resolved_identity') or {}).get('player_id') for p in selected if (p.get('resolved_identity') or {}).get('status')=='confirmed']
        conflicting={key for key,n in Counter(ids).items() if key and n>1}
        for p in selected:
            identity=p.get('resolved_identity') or {};confirmed=identity.get('status')=='confirmed' and bool(identity.get('player_id')) and identity.get('player_id') not in conflicting
            key='player:'+identity['player_id'] if confirmed else f'account:{battle["event_key"]}:{p["accountDBID"]}:{p["name"]}'
            ship_id=str(p['shipParamsId']);ship=ship_catalog.get(ship_id,{})
            personal=(p.get('personal_account') or {}).get('personal_nickname')
            row={'battle_id':battle['battle_id'],'date':battle['date'],'map_id':battle['map_id'],'map_name':battle['map_name'],
                 'player_key':key,'player_name':personal or p['name'],'tournament_name':p['name'],'account_id':p['accountDBID'],
                 'identity_status':'confirmed' if confirmed else 'conflict' if identity.get('status')=='conflict' or identity.get('player_id') in conflicting else 'confirmed_account' if personal and identity.get('status')=='confirmed' else 'tournament_account',
                 'ship_id':ship_id,'ship_name':ship.get('display_name') or ship.get('internal_name') or ship_id,
                 'ship_class':ship.get('species'),'source_count':len(sources),**deepcopy(result),**received_for_player(p,sources,fields)}
            output['damage_matches'].append(row);appearances[key].append(row);ship_slots[ship_id]+=1;ship_battles[ship_id].add(battle['battle_id']);player_ship[(key,ship_id)].append(row);selected_ids.append(key)
        output['battles'].append({key:deepcopy(battle[key]) for key in ('battle_id','date','map_id','map_name','label')})
        output['battles'][-1].update(selected_players=len(selected),source_shas=[s['source_sha'] for s in sources],**result)
    count=len(output['battles']);slots=sum(ship_slots.values())
    for ship,n in ship_slots.items():
        data=ship_catalog.get(ship,{})
        output['team_ships'].append({'ship_id':ship,'ship_name':data.get('display_name') or data.get('internal_name') or ship,
            'ship_class':data.get('species'),'appearances':n,'slot_denominator':slots,'battle_count':len(ship_battles[ship]),
            'battle_denominator':count,'battle_share':len(ship_battles[ship])/count if count else None,'battle_ids':sorted(ship_battles[ship])})
    output['team_ships'].sort(key=lambda r:(-r['battle_count'],-r['appearances'],r['ship_name']))
    for key,rows in appearances.items():
        first=rows[0];counts=Counter(r['ship_name'] for r in rows)
        output['players'].append({'player_key':key,'player_name':first['player_name'],'tournament_names':sorted({r['tournament_name'] for r in rows}),
            'identity_status':first['identity_status'],'battle_count':len(rows),'distinct_ships':len(counts),
            'top_ships':counts.most_common(3),**aggregate_damage(rows),**aggregate_outcomes(rows)})
    output['players'].sort(key=lambda r:r['player_name'])
    for (key,ship),rows in player_ship.items():
        output['player_ships'].append({'player_key':key,'player_name':rows[0]['player_name'],'tournament_names':sorted({r['tournament_name'] for r in rows}),
            'identity_status':rows[0]['identity_status'],'ship_id':ship,'ship_name':rows[0]['ship_name'],
            'appearances':len(rows),'player_appearances':len(appearances[key]),'share':len(rows)/len(appearances[key]),
            'battle_ids':[r['battle_id'] for r in rows],**aggregate_outcomes(rows)})
    output['player_ships'].sort(key=lambda r:(r['player_name'],-r['appearances'],r['ship_name']))
    output['counts']={'requested_battles':len(seen),'included_battles':count,'excluded_battles':len(output['excluded']),'appearances':slots,
                      'players':len(appearances),'unique_ships':len(ship_slots),'source_views':len({s for b in output['battles'] for s in b['source_shas']})}
    output['received_damage']=aggregate_damage(output['damage_matches'])
    output['outcomes']=aggregate_outcomes(output['battles'])
    return output
