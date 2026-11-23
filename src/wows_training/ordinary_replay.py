"""Replay import capabilities are selected by the exact client build."""
from __future__ import annotations

from collections import Counter, defaultdict
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import struct
import sys

from .battle_results import decode_common_result
from .combat_capture import minimap_heading
from .container import read_replay, _json_block
from .contracts import outcome, packet_frames, parse_version
from .player_info import ClientPlayersInfo
from .safe_pickle import restricted_loads

OLD = '15.9.0.13357625'
CURRENT = '15.10.0.13533378'
PERFORMANCE_SCHEMA_VERSION = 3
HASHES = {CURRENT + '.json': 'b7db6e40db9271bc0ba3ce7c300fd89e570413d897fbb08daff9130d29bb610b',
          'interactions-15.10.json': '81d876406bbb2b6950e95d3b3cd5418719a0b33c3765d94d003a3ec4a40bb0ec',
          'settlement-ribbons.json': '6b1dd7befba17f7fe18bc4fa416906cca06b5afb1ff4e4cb9f5ada4bcb82af26',
          'maps-15.9.json': '6212c2456b24335022967d3923796db1cf34a0fd5cbc685c06d732bf1e09805d',
          'maps-15.10.json': 'eadacfe1482d8ba4eb1dfe767a246b6d40f09610a67bed9f3953e8515e1270f8'}
MODES = {'randombattle':'随机战', 'pvp':'随机战', 'cooperativebattle':'人机战', 'cooperative':'人机战',
         'rankedbattle':'排位战', 'ranked':'排位战', 'rank':'排位战', 'eventbattle':'活动战', 'event':'活动战',
         'pvebattle':'行动', 'pve':'行动', 'trainingbattle':'训练房', 'training':'训练房', 'clanbattle':'军团战',
         'clan':'军团战', 'brawlbattle':'对决', 'brawl':'对决'}
METRICS = {'damage':'输出伤害', 'ships_killed':'击沉', 'exp':'结算经验', 'scouting_damage':'侦察伤害',
           'life_time_sec':'存活秒数', 'remained_hp':'结算血量'}


def battle_mode_label(metadata):
    if isinstance(metadata, dict):
        scenario = str(metadata.get('scenario') or '').lower().replace('-','_').split('_')
        if 'tournament' in scenario or 'kots' in scenario:
            return '赛事规则'
        for key in ('gameType','matchGroup'):
            label = MODES.get(str(metadata.get(key) or '').lower())
            if label:
                return label
    return '未知模式'


def quality_label(quality):
    return {'basic_verified':'战斗解析', 'result_verified':'结算与观测', 'observations_verified':'局中观测',
            'header_only':'仅录像头'}.get(quality, '已存记录')


def _root():
    return (Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2])/'ordinary_profiles'


@lru_cache(maxsize=5)
def _profile(name):
    raw=(_root()/name).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=HASHES[name]:
        raise ValueError('战斗协议或地图资料校验失败。')
    return json.loads(raw)


def extra_map_record(map_id,version=OLD):
    if version not in (OLD,CURRENT):return None
    source=_profile('maps-15.10.json' if version==CURRENT else 'maps-15.9.json')['maps'].get(map_id)
    if source is None:
        return None
    return dict(source,source_paths={k:str(_root()/path) for k,path in source['source_paths'].items()})


def map_labels():
    labels = {}
    for name in ('maps-15.9.json', 'maps-15.10.json'):
        for map_id, row in _profile(name)['maps'].items():
            if row.get('label'):
                labels[map_id] = row['label']
    return labels


def number(value):
    return type(value) in (int,float) and math.isfinite(value) and 0 <= value <= 1e12


def verified_minimap_heading(report, raw):
    """15.10 raw north-clockwise yaw converted to east-counterclockwise.

    Position packets verify raw yaw, while the minimap renderer requires the
    world x/z convention. Convert at use time so stored wire tracks stay valid.
    """
    ordinary = report.get('ordinary') or {}
    pairs = ordinary.get('world_pairs')
    position_error = ordinary.get('world_rmse')
    heading_error = ordinary.get('heading_rmse')
    if (report.get('replay_version') != CURRENT or report.get('multiview')
            or ordinary.get('profile_sha256') != HASHES[CURRENT+'.json']
            or type(pairs) is not int or pairs < 10
            or not number(position_error) or position_error > 5
            or not number(heading_error) or heading_error > .10
            or type(raw) not in (int,float) or not -math.pi <= raw <= math.pi):
        return None
    return minimap_heading(raw)


def current_combat_verified(report):
    combat = report.get('combat') or {}
    return (report.get('replay_version') == CURRENT and not report.get('multiview')
            and combat.get('client_version') == CURRENT and combat.get('schema_version') == 1
            and (report.get('ordinary') or {}).get('profile_sha256') == HASHES[CURRENT+'.json']
            and (combat.get('provenance') or {}).get('entity_tree_sha256') == _profile(CURRENT+'.json')['entity_tree_sha256'])


def metrics(public):
    return {k:public.get(k) if number(public.get(k)) else None for k in METRICS}


def result_ribbons(row, version, prefix_length):
    layout = _profile('settlement-ribbons.json')[version]
    fields = layout['fields']
    if prefix_length != layout['prefix_length'] or len(row) != prefix_length + len(fields):
        return None
    counts = row[prefix_length:]
    if any(type(count) is not int or not number(count) for count in counts):
        return None
    return {key: count for key, count in zip(fields, counts) if count}


def _header_roster(h):
    vehicles=h.get('vehicles')
    if not isinstance(vehicles,list) or not 1 <= len(vehicles) <= 256:
        raise ValueError('缺少可读取的录像阵容。')
    rows=[]
    for item in vehicles:
        if not isinstance(item,dict) or not isinstance(item.get('name'),str) or type(item.get('shipId')) is not int:
            raise ValueError('录像头的舰船或姓名格式无效。')
        rows.append({'name':item['name'], 'avatar_id':item.get('id'), 'shipParamsId':item['shipId'],
                     'relation':item.get('relation'), 'accountDBID':None,'shipId':None,'teamId':None})
    return rows


def _blobs(data,count):
    values=[]
    offset=0
    for _ in range(count):
        if offset>=len(data):
            raise ValueError('名单数据不完整。')
        size=data[offset];offset+=1
        if size==255:
            if offset+3>len(data):raise ValueError('名单字段长度不完整。')
            size=int.from_bytes(data[offset:offset+3],'little');offset+=3
        if size>8*1024*1024 or offset+size>len(data):raise ValueError('名单字段长度异常。')
        values.append(data[offset:offset+size]);offset+=size
    if offset!=len(data):raise ValueError('名单数据含未识别字段。')
    return values


def _minimap_updates(raw):
    offset=0
    for group in range(2):
        if offset>=len(raw): raise ValueError('小地图更新不完整。')
        n=raw[offset];offset+=1
        if offset+8*n>len(raw): raise ValueError('小地图更新长度异常。')
        if group==0:
            yield from struct.iter_unpack('<II',raw[offset:offset+8*n])
        offset+=8*n
    if offset!=len(raw): raise ValueError('小地图更新格式变化。')


def _recorded_clock(frame):
    return {'clock':frame.clock,'packet_clock':frame.clock,'packet_offset':frame.offset,'packet_type':frame.type}


def _result_rows(data,profile,arena,roster,host):
    from .statistics_capture import field_values, weapon_damage_fields, performance_result
    common=decode_common_result(data,profile['common'],str(arena))
    if host is None or data.get('accountDBID')!=host.get('accountDBID'):
        raise ValueError('赛后结算的录制账号与局内记录不符。')
    rows=data.get('playersPublicInfo')
    if not isinstance(rows,dict) or len(rows)>512:raise ValueError('赛后阵容格式无效。')
    result={}
    dealt_fields, received_fields = weapon_damage_fields(profile['public'])
    for player in roster:
        key=str(player.get('accountDBID'));row=rows.get(key)
        if row is None:continue
        if not isinstance(row,list) or not len(profile['public']) <= len(row) <= 4096:continue
        public=dict(zip(profile['public'],row))
        if (public.get('account_db_id')!=player['accountDBID'] or public.get('team_id')!=player['teamId']
            or public.get('vehicle_type_id')!=player['shipParamsId'] or public.get('name')!=player['name']):continue
        targets, overlap = _target_results(public, roster, dealt_fields)
        result[key]={**metrics(public), **performance_result(public),
                     'ribbons': result_ribbons(row, CURRENT, len(profile['public'])),
                     'target_results': targets, 'reconciled_overlapping_fields': overlap,
                     'dealt_damage_by_field':field_values(public,dealt_fields),
                     'received_damage_by_field':field_values(public,received_fields)}
    return common,result


def _target_results(public, roster, weapon_fields):
    from .statistics_capture import AIRCRAFT_TOTALS, field_values
    interactions = public.get('interactions')
    if not isinstance(interactions, dict) or len(interactions) > len(roster):
        return None, []
    layout = _profile('interactions-15.10.json')['fields']
    accounts = {str(row['accountDBID']): row['shipId'] for row in roster
                if type(row.get('accountDBID')) is int and type(row.get('shipId')) is int}
    targets, vectors = [], []
    for account, values in interactions.items():
        if account not in accounts or not isinstance(values, list) or len(values) != len(layout):
            return None, []
        fields = dict(zip(layout, values))
        vectors.append(fields)
        amounts = [fields.get(key) for key in weapon_fields]
        damage = sum(amounts) if all(number(n) for n in amounts) else None
        targets.append({'entity_id': accounts[account], 'damage': damage,
                        'scouting_damage': fields['scouting_damage'] if number(fields['scouting_damage']) else None,
                        'dot_by_field': field_values(fields, ('damage_fire', 'damage_flood', 'fires', 'floods'))})
    # Aircraft stats can carry both a weapon total and its delivery variants. Only
    # remove a positive duplicate when it matches for every target and the result
    # independently reconciles to the player's settled damage.
    total = public.get('damage')
    if not number(total) or any(row['damage'] is None for row in targets):
        return targets, []
    gross = sum(row['damage'] for row in targets)
    overlap = []
    if gross > total:
        for field, variants in AIRCRAFT_TOTALS.items():
            amount = sum(v[field] for v in vectors)
            if (amount > 0 and amount == public.get(field)
                    and all(v[field] == sum(v[s] for s in variants) for v in vectors)):
                overlap.append(field)
        if sum(v[field] for v in vectors for field in overlap) != gross-total:
            overlap = []
        for row, vector in zip(targets, vectors):
            row['damage'] -= sum(vector[field] for field in overlap)
    return targets, overlap


def needs_performance_refresh(report):
    ordinary = report.get('ordinary') or {}
    if report.get('multiview') or not (report.get('post_battle_common') or {}).get('arena_id'):
        return False
    version = str(report.get('replay_version') or '').replace(',', '.')
    if version == CURRENT:
        return (ordinary.get('profile_sha256') == HASHES[CURRENT+'.json']
                and ordinary.get('performance_schema_version') != PERFORMANCE_SCHEMA_VERSION)
    stats = report.get('statistics') or {}
    return (version == OLD and report.get('winner_source') == 'verified_15.9_common_results'
            and stats.get('status') == 'result_fields_available' and stats.get('client_version') == OLD
            and stats.get('performance_schema_version') != PERFORMANCE_SCHEMA_VERSION)


def _result_document(payload):
    if len(payload)<4 or int.from_bytes(payload[:4],'little')!=len(payload)-4 or len(payload)>8*1024*1024:
        raise ValueError('结算包长度异常。')
    doc = _json_block(payload[4:], 0)
    if not isinstance(doc, dict):
        raise ValueError('结算格式无效。')
    return doc


def hydrate_performance(report, raw_path):
    """Read settlement fields and loadouts from the same replay without changing its saved revision."""
    from .replay_loadout import read_ship_loadouts, replay_loadout_pending
    refresh_results = needs_performance_refresh(report)
    refresh_loadouts = replay_loadout_pending(report)
    if not (refresh_results or refresh_loadouts):
        return report
    path = Path(raw_path)
    before = path.stat()
    raw = read_replay(path)
    version = report['replay_version'].replace(',', '.')
    if hashlib.sha256(path.read_bytes()).hexdigest() != report.get('sha256') or _header_version(path) != version:
        raise ValueError('原始录像与已保存的分析不一致。')
    roster = report.get('roster') or []
    hosts = [p for p in roster if p.get('name') == raw.engine_data.get('playerName')]
    if len(hosts) != 1 or hosts[0].get('name') != (report.get('metadata') or {}).get('playerName'):
        raise ValueError('无法核对原始录像的录制者。')
    hydrated = report
    if refresh_results and version == OLD:
        hydrated = _hydrate_old_performance(report, raw, roster)
    elif refresh_results:
        rows = _hydrate_current_results(report, raw, roster, hosts[0])
        hydrated = {**report, 'ordinary': {**report['ordinary'], 'player_results': rows,
                                           'performance_schema_version': PERFORMANCE_SCHEMA_VERSION}}
    if refresh_loadouts:
        hydrated = {**hydrated, 'ship_loadouts': read_ship_loadouts(report, packet_frames(raw.decrypted_data))}
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('无法核对录像结算。')
    return hydrated


def _hydrate_current_results(report, raw, roster, host):
    rows = None
    count = 0
    for frame in packet_frames(raw.decrypted_data):
        if frame.type != 34:
            continue
        count += 1
        if count > 8:
            raise ValueError('结算包数量异常。')
        doc = _result_document(frame.payload)
        if not doc.get('commonList'):
            continue
        common, current = _result_rows(doc, _profile(CURRENT+'.json'), report['arena_id'], roster, host)
        if common != report['post_battle_common'] or (rows is not None and current != rows):
            raise ValueError('补录的结算与已保存的对局不一致。')
        rows = current
    if rows is None:
        raise ValueError('无法核对录像结算。')
    return rows


def _hydrate_old_performance(report, raw, roster):
    from .runtime_bundle import load_bundle
    from .statistics_capture import StatisticsCapture, load_profile
    from .battle_results import load_result_schema

    bundle = load_bundle()
    profile = load_profile(bundle)
    stats = report['statistics']
    if stats.get('profile_sha256') != profile['profile_sha256'] or stats.get('result_stream_truncated'):
        raise ValueError('无法核对已保存录像的结算协议。')
    common_names, _ = load_result_schema(bundle.root / 'result_schema.json', bundle.root / 'resource_manifest.json')
    capture = StatisticsCapture(profile)
    for frame in packet_frames(raw.decrypted_data):
        if frame.type != 34:
            continue
        doc = _result_document(frame.payload)
        common = decode_common_result(doc, common_names, report['arena_id'])
        if common != report['post_battle_common']:
            raise ValueError('补录的结算与已保存的对局不一致。')
        capture.begin_packet(frame)
        capture.observe_result(doc, common)
    updated = capture.finish(roster, raw.engine_data, str(report['arena_id']))
    if updated['status'] != 'result_fields_available':
        raise ValueError('无法核对录像结算。')
    return {**report, 'statistics': {**stats, 'result_snapshots': updated['result_snapshots'],
                                    'performance_schema_version': PERFORMANCE_SCHEMA_VERSION}}


def _current(data,report):
    from .ordinary_combat import CurrentCombat
    combat = CurrentCombat()
    profile=_profile(CURRENT+'.json');header=data.engine_data
    initial=report['roster'];player_map=ClientPlayersInfo({i:{int(k):name for k,name in profile['players'][kind].items()}
        for i,kind in ((1,'player'),(2,'bot'),(3,'observer'))})
    am=profile['methods']['Avatar'];kind_ids=profile['entity_types'];avatar=None;arena=None
    vehicle_ids=set();results=[];deaths=[];observed=[];trails=defaultdict(list);last={};count=0;track_count=0
    earliest=latest=None
    map_matches=[];world={};error_count=0;error_squared=0.;heading_squared=0.
    for frame in packet_frames(data.decrypted_data):
        combat.observe(frame)
        count+=1;b=frame.payload;t=frame.clock
        earliest=t if earliest is None else min(earliest,t);latest=t if latest is None else max(latest,t)
        if frame.type==0 and len(b)>=10:
            entity,kind,n=struct.unpack_from('<IHI',b)
            if kind==kind_ids['Avatar'] and len(b)==10+n:avatar=entity
        elif frame.type==5 and len(b)>=42:
            entity,kind=struct.unpack_from('<IH',b)
            if kind==kind_ids['Vehicle']:vehicle_ids.add(entity)
        elif frame.type==10 and len(b)==45:
            eid,_,x,_,z=struct.unpack_from('<IIfff',b)
            yaw=struct.unpack_from('<f',b,32)[0]
            if eid in vehicle_ids and all(math.isfinite(v) for v in (x,z,yaw)):world[eid]=(t,x,z,yaw)
        elif frame.type==40:
            from .packets import _map
            try:
                fields=_map(b)
                map_matches.append(fields['matrix_is_identity'] and fields['map_name']==header.get('mapName'))
            except (ValueError,UnicodeError,struct.error):map_matches.append(False)
        elif frame.type==34:
            if len(results)>=8: raise ValueError('结算包数量异常。')
            results.append(_result_document(b))
        elif frame.type==8 and len(b)>=12:
            entity,method,n=struct.unpack_from('<III',b)
            if len(b)!=12+n:raise ValueError('战斗方法包长度异常。')
            raw=b[12:]
            if entity==avatar:
                if method==am['onArenaStateReceived']:
                    if len(raw)<9:raise ValueError('缺少对局编号。')
                    arena_id=struct.unpack_from('<q',raw)[0]
                    if arena_id<=0 or arena not in (None,arena_id):raise ValueError('局内对局编号冲突。')
                    arena=arena_id;records=_blobs(raw[9:],5)[1:4]
                elif method in (am['onGameRoomStateChanged'],am['onNewPlayerSpawnedInBattle']):
                    records=_blobs(raw,3)
                else:records=[]
                for i,blob in enumerate(records,1):
                    player_map.create_or_update_players(restricted_loads(blob,encoding='bytes'),i)
                if method==am['receiveVehicleDeath']:
                    victim,killer,reason=struct.unpack('<III',raw)
                    if len(deaths)>=4096:raise ValueError('击沉记录过多。')
                    deaths.append({'clock':t,'victim_entity_id':victim,'killer_entity_id':killer,'type_id':reason})
                elif method==am['updateMinimapVisionInfo']:
                    for ident,packed in _minimap_updates(raw):
                        x=packed&2047;z=(packed>>11)&2047
                        if (not x and not z) or packed>>31:
                            last.pop(ident,None);continue
                        # Compare and store the wire yaw, not the display convention.
                        heading=((packed>>22)&255)*math.tau/256-math.pi
                        position=world.get(ident)
                        if position and 0<=t-position[0]<=.2:
                            error_squared+=(position[1]-(x*5000/2047-2500))**2+(position[2]-(z*5000/2047-2500))**2
                            heading_squared+=math.remainder(position[3]-heading,math.tau)**2
                            error_count+=1
                        prev=last.get(ident)
                        if prev is not None and 0<=t-prev<.5:continue
                        if track_count>=350000:raise ValueError('录像轨迹超过容量。')
                        if prev is None or not 0<=t-prev<=3:trails[ident].append([])
                        trails[ident][-1].append([t,x,z,heading]);last[ident]=t;track_count+=1
            elif entity in vehicle_ids and method==profile['methods']['Vehicle']['receiveDamagesOnShip']:
                if not raw or len(raw)!=1+8*raw[0]:raise ValueError('承伤记录长度异常。')
                for attacker,amount in struct.iter_unpack('<If',raw[1:]):
                    if number(amount) and amount>0:
                        if len(observed)>=250000:raise ValueError('承伤事件超过容量。')
                        observed.append({'victim_entity_id':entity,'attacker_entity_id':attacker,'amount':amount,'source':_recorded_clock(frame)})
    all_players=player_map.get_info()
    if not arena or player_map.unknown_fields or not count:raise ValueError('局内名单协议校验失败。')
    if len(all_players)>1024:raise ValueError('对局实体名单过大。')
    for h in initial:
        p=all_players.get(h['avatar_id'])
        if p is None or p.get('name')!=h['name'] or p.get('shipParamsId')!=h['shipParamsId']:
            raise ValueError('录像头与局内阵容不一致。')
    roster=[{k:v.get(k) for k in ('name','shipId','shipParamsId','teamId','accountDBID','maxHealth','_player_kind')} |
            {'avatar_id':key} for key,v in all_players.items() if v.get('_player_kind')!=3 and v.get('shipParamsId')]
    self_header=[p for p in initial if p['relation']==0 and p['name']==header.get('playerName')]
    host=next((p for p in roster if len(self_header)==1 and p['avatar_id']==self_header[0]['avatar_id']),None)
    if host is None:raise ValueError('无法确认录制者与舰艇的对应关系。')
    valid_entities={p['shipId'] for p in roster if type(p['shipId']) is int}
    settled=None;common=None
    for doc in results:
        if not doc.get('commonList'):continue
        result,mapped=_result_rows(doc,profile,arena,roster,host)
        if common is not None and (result!=common or mapped!=settled):raise ValueError('同一录制的赛后数据冲突。')
        common,settled=result,mapped
    known=bool(common)
    report.update(roster=roster,arena_id=str(arena),parsed_roster_count=len(roster),roster_matches_header=True,
                  host_absolute_team=host['teamId'],quality='result_verified' if known else 'observations_verified',
                  post_battle_common=common, winner_absolute_team=common['winner_team_id'] if known else None,
                  outcome=outcome(common['winner_team_id'],host['teamId']) if known else 'unknown',
                  winner_source='verified_15.10_common_results' if known else None,
                  packet_clock_range={'min':earliest,'max':latest},death_events=deaths,
                  ordinary={'player_results':settled or {},'performance_schema_version':PERFORMANCE_SCHEMA_VERSION,'observed_damage':observed,
                            'minimap_raw_tracks':{str(i):s for i,s in trails.items() if i in valid_entities},
                            'profile_sha256':HASHES[CURRENT+'.json'],'initial_count':len(initial),
                            'world_pairs':error_count,'world_rmse':math.sqrt(error_squared/error_count) if error_count else None,
                            'heading_rmse':math.sqrt(heading_squared/error_count) if error_count else None,
                            'map_identity':map_matches==[True]})
    report.update(combat.battle_state(latest, common['duration_sec'] if known else None))
    report['combat'] = combat.finish(roster, latest)
    from .consumable_ranges import attach_consumable_ranges
    attach_consumable_ranges(report)
    return report


def _header_version(path):
    from .container import MAGIC, MAX_JSON_BLOCK_BYTES
    with path.open('rb') as stream:
        if stream.read(4)!=MAGIC:raise ValueError('不是有效的战舰世界录像。')
        count,size=struct.unpack('<II',stream.read(8))
        if not 1<=count<=64 or not 0<size<=MAX_JSON_BLOCK_BYTES:raise ValueError('无效录像头。')
        header=_json_block(stream.read(size),0)
    return '.'.join(str(n) for n in parse_version(header['clientVersionFromExe']))


def parse_for_import(path,bundle_root=None):
    from .runtime_bundle import parse_portable
    from .replay_loadout import read_ship_loadouts, replay_loadout_pending
    path=Path(path);before=path.stat();version=_header_version(path)
    if version==OLD:
        report=parse_portable(path,bundle_root)
        if (report.get('packet_errors') or report.get('framing_error') or report.get('unknown_player_fields')
                or report.get('handled_callback_exceptions') or report.get('post_battle_result_errors')
                or report.get('roster_matches_header') is not True or not report.get('roster')):
            raise ValueError('关键数据未通过校验，请保留原始回放供后续适配。')
        report['quality']='basic_verified'
    else:
        data=read_replay(path);h=data.engine_data;roster=_header_roster(h)
        if '.'.join(str(n) for n in parse_version(h['clientVersionFromExe']))!=version:raise ValueError('导入期间录像发生变化。')
        report={'status':'partial','quality':'header_only','replay_version':version,'roster':roster,
                'parsed_roster_count':len(roster),'header_roster_count':len(roster),'arena_id':None,'outcome':'unknown',
                'metadata':{k:h.get(k) for k in ('mapName','mapDisplayName','mapId','matchGroup','gameType','scenario','playersPerTeam','dateTime','playerName','playerVehicle')},
                'event_key':'KOTS19AS' if any(p['name'].endswith('_KOTS19AS') for p in roster) else 'unassigned',
                'container_diagnostics':data.diagnostics}
        if version==CURRENT:report=_current(data,report)
        else:
            # Validate the complete packet envelope before admitting an older header to the library.
            if not sum(1 for _ in packet_frames(data.decrypted_data)):raise ValueError('录像没有数据包。')
        report['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    if replay_loadout_pending(report):
        if version == OLD:
            data = read_replay(path)
        report['ship_loadouts'] = read_ship_loadouts(report, packet_frames(data.decrypted_data))
    after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('导入期间录像发生变化。')
    return report
