"""Scoped individual replay dossiers, independent of team-name heuristics."""
from collections import Counter,defaultdict
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from .library import Library
from .runtime_bundle import load_bundle
from .workbench_store import WorkbenchStore
from .identity_rules import effective_mappings,parse_kots_name
from .contracts import parse_version
from .statistics_store import StatisticsStore,digest
from .statistics_capture import load_profile
from .statistics_service import EXTRACTOR_VERSION,cancelled
from .statistics_analysis import received_for_player,aggregate_damage,aggregate_outcomes,team_outcome


def _participants(report):
    return [p for p in report.get('roster',[]) if type(p.get('shipId')) is int and p['shipId']>0
            and type(p.get('teamId')) is int and p['teamId'] in (0,1) and p.get('_player_kind')!=3]


def _signature(roster):
    return Counter((p.get('accountDBID'),p.get('shipParamsId')) for p in roster)


def _date_key(row):
    try:return datetime.strptime(row['date'],'%d.%m.%Y %H:%M:%S').isoformat(),row['battle_id']
    except (ValueError,KeyError,TypeError):return str(row.get('date') or ''),row['battle_id']


def _aggregate_profile(profile,catalog):
    rows=profile['matches'];profile['matches']=sorted(rows,key=_date_key,reverse=True)
    profile.update(appearances=len(rows),**aggregate_outcomes(rows))
    profile['damage']=aggregate_damage(rows);ships=defaultdict(list)
    for row in rows:ships[row['ship_id']].append(row)
    profile['ships']=[]
    for ship,appearances in ships.items():
        info=catalog.get(ship,{})
        profile['ships'].append({'ship_id':ship,'ship_name':appearances[0]['ship_name'],'ship_class':info.get('species'),
            'nation':info.get('nation'),'tier':info.get('tier'),'appearances':len(appearances),
            'player_appearances':len(rows),'share':len(appearances)/len(rows),'battle_ids':[r['battle_id'] for r in appearances],
            'damage':aggregate_damage(appearances),**aggregate_outcomes(appearances)})
    profile['ships'].sort(key=lambda r:(-r['appearances'],r['ship_name']))
    profile['distinct_ships']=len(ships);profile['top_ship_id']=profile['ships'][0]['ship_id'] if profile['ships'] else None
    classes=Counter(row.get('ship_class') for row in rows)
    profile['class_distribution']=[{'species':kind,'battles':count,'share':count/len(rows)} for kind,count in classes.items()] if rows else []
    profile['public_accounts']=list(profile.pop('_public_accounts').values())
    profile['tournament_accounts']=list(profile.pop('_tournament_accounts').values())
    profile['source_shas']=sorted({s for row in rows for s in row['source_shas']})
    profile['excluded_matches']=sorted(set(profile.get('excluded_matches',[])))
    profile['scope_label']='已确认个人' if profile['player_id'] else '昵称映射已确认' if profile['identity_status']=='confirmed_account' else '赛事账号'
    if profile['identity_status']=='conflict':profile['scope_label']='身份冲突，按原赛号保留'
    return profile


class PlayerDossierService:
    def __init__(self,root,bundle=None):
        self.library=Library(root);self.bundle=bundle or load_bundle();self.workbench=WorkbenchStore(root)
        self.statistics=StatisticsStore(root);self.profile=load_profile(self.bundle);self.catalog=self.bundle.ship_catalog()

    def battle_ids(self):return [row['id'] for row in self.library.list_battles()]

    def confirm_public_link(self,index,player_key,target,actor,scope='selected',*,confirmed=False,notes=''):
        """Persist a user-confirmed public identity, scoped to an exact REP actor.

        Public IDs never participate in replay lookup. Only the selected profile's
        explicit stable identity can be reused; names and public IDs cannot merge
        different replay profiles.
        """
        if confirmed is not True:raise ValueError('请先确认身份与适用范围')
        if index.get('cache_key')!=self.cache_key([b['battle_id'] for b in index['input_snapshot']['battles']]):
            raise ValueError('比赛或身份资料已变化，请重新选择')
        profile=index.get('profiles',{}).get(player_key)
        if not profile or profile.get('identity_status')=='conflict':raise ValueError('当前身份存在冲突，请先核对分配')
        region='na' if target.get('region')=='com' else target.get('region');identifier=target.get('id')
        nickname=target.get('name')
        if (target.get('kind')!='player' or region not in ('asia','eu','na') or type(identifier) not in (str,int)
                or not str(identifier).isdigit() or not 0<int(identifier) or len(str(identifier))>20
                or not isinstance(nickname,str) or not nickname.strip() or len(nickname)>200):
            raise ValueError('请从账号查询中选择确切的公开账号')
        nickname=nickname.strip();identifier=str(int(identifier))
        selected=next((a for a in profile['tournament_accounts'] if
            (a['event_key'],a['tournament_name'],str(a['tournament_account_id']))==tuple(actor)),None)
        if selected is None or scope not in ('selected','event'):raise ValueError('赛号或适用范围无效')
        battle_ids=sorted(set(selected['battle_ids'])) if scope=='selected' else []
        players=self.workbench.list_documents('player');assignments=self.workbench.list_documents('assignment')
        person=profile.get('player_id');player=next((p for p in players if (p['payload'].get('player_id') or p['id'])==person),None) if person else None
        document_id=player['id'] if player else str(uuid4());person=person or document_id
        same_record=None
        for doc in assignments:
            row=doc['payload']
            if row.get('active') is False or row.get('deleted') or row.get('deleted_at') or not row.get('confirmed'):continue
            if row.get('event_key')!=selected['event_key'] or row.get('tournament_name')!=selected['tournament_name']:continue
            old_scope=row.get('battle_ids') or []
            if battle_ids and old_scope and not set(battle_ids).intersection(old_scope):continue
            old_target=('na' if row.get('region')=='com' else row.get('region'),str(row.get('account_id') or ''))
            if row.get('player_id')!=person or old_target!=(region,identifier):
                raise ValueError('适用范围已有其他已确认分配，请先在身份核对中调整')
            if set(old_scope)==set(battle_ids):same_record=doc['id']
        payload=deepcopy(player['payload']) if player else {'display_name':nickname,'aliases':[],'accounts':[]}
        payload.setdefault('display_name',nickname)
        payload['accounts']=[a for a in payload.get('accounts',[]) if
            (a.get('region'),str(a.get('account_id') or ''))!=(region,identifier)]
        payload['accounts'].append({'region':region,'account_id':identifier,'nickname':nickname,'confirmed':True})
        assignment={'event_key':selected['event_key'],'tournament_name':selected['tournament_name'],
            'battle_ids':battle_ids,'player_id':person,'personal_nickname':nickname,'region':region,'account_id':identifier,
            'confirmed':True,'source':'manual_panel_confirmation','notes':notes or '用户核对账号查询中的精确公开账号后确认'}
        self.workbench.save_documents([
            {'kind':'player','name':payload['display_name'],'payload':payload,'document_id':document_id,'create':player is None},
            {'kind':'assignment','name':selected['tournament_name'],'payload':assignment,'document_id':same_record}])
        return 'player:'+person

    def snapshot(self,battle_ids=None):
        ids=sorted(set(self.battle_ids() if battle_ids is None else battle_ids));battles=[]
        with self.statistics.connection() as db:
            cache={row['cache_key']:row['content_sha'] for row in db.execute("SELECT cache_key,content_sha FROM objects WHERE kind='source'")}
        for identifier in ids:
            battle=self.library.get_battle(identifier)
            views=[]
            for row in battle['replays']:
                source_key=digest({'source_sha':row['sha256'],'profile':self.profile['profile_sha256'],'extractor':EXTRACTOR_VERSION})
                views.append({**{key:row[key] for key in ('sha256','analysis_revision','report_sha256')},'statistics_cache':cache.get(source_key)})
            battles.append({'battle_id':identifier,'event_key':battle['event_key'],'label':battle['label'],'date':battle['date_raw'],
                            'map_id':battle['map_id'],'views':views})
        return {'battles':battles,'identities':{kind:[(d['id'],d['revision']) for d in self.workbench.list_documents(kind)] for kind in ('player','assignment')},
            'legacy_mappings':self.library.list_mappings(),'profile_sha256':self.profile['profile_sha256'],
            'catalog_sha256':self.bundle.manifest['files']['ship_catalog.json'],'labels_sha256':self.bundle.manifest['files']['labels.json']}

    def cache_key(self,battle_ids=None):return digest({'algorithm':'individual-dossier-2','inputs':self.snapshot(battle_ids)})

    def _source(self,row,report):
        stats=report.get('statistics') or {}
        try:version_ok=parse_version(report.get('replay_version'))==tuple(map(int,self.bundle.version.split('.')))
        except (ValueError,TypeError,AttributeError):version_ok=False
        if not version_ok:return {'source_sha':row['sha256'],'core_verified':False,'roster':report.get('roster',[]),'statistics':{}},'unknown'
        valid=(report.get('roster_matches_header') is True and not report.get('packet_errors') and not report.get('framing_error')
               and not report.get('post_battle_result_errors') and stats.get('schema_version')==1
               and stats.get('client_version')==self.bundle.version and stats.get('profile_sha256')==self.profile['profile_sha256']
               and stats.get('received_damage_fields')==self.profile['received_damage_fields'])
        if valid:
            return {'source_sha':row['sha256'],'core_verified':True,'roster':report['roster'],
                    'host_absolute_team':report.get('host_absolute_team'),'statistics':stats},'report'
        key=digest({'source_sha':row['sha256'],'profile':self.profile['profile_sha256'],'extractor':EXTRACTOR_VERSION})
        cached=self.statistics.get('source',key)
        if cached is not None:
            value=deepcopy(cached);claimed=value.pop('content_fingerprint',None)
            if (cached.get('source_sha')!=row['sha256'] or cached.get('extractor_version')!=EXTRACTOR_VERSION
                    or cached.get('statistics',{}).get('profile_sha256')!=self.profile['profile_sha256'] or digest(value)!=claimed):
                raise ValueError('统计缓存与来源不一致')
            return cached,'cache'
        return {'source_sha':row['sha256'],'core_verified':False,'roster':report['roster'],
                'statistics':{'received_damage_fields':self.profile['received_damage_fields']}},'unknown'

    def build(self,battle_ids=None,cancel=None,progress=None):
        cancelled(cancel);before=self.snapshot(battle_ids);key=digest({'algorithm':'individual-dossier-2','inputs':before})
        cached=self.statistics.get('player_dossier',key)
        if cached is not None:return cached
        players=self.workbench.list_documents('player');assignments=self.workbench.list_documents('assignment')
        legacy=self.library.list_mappings();catalog=self.catalog;profiles={};errors=[];origins=Counter();source_shas=set();aliases=defaultdict(set)
        maps=self.bundle.json('labels.json')['maps'];battle_meta={}
        for index,entry in enumerate(before['battles']):
            cancelled(cancel)
            if progress:progress(f'整理 REP 档案 {index+1}/{len(before["battles"])}')
            battle=self.library.get_battle(entry['battle_id']);reports=[];sources=[];result_sources=[]
            for row in battle['replays']:
                cancelled(cancel);report=self.library.get_report(row['sha256']);reports.append(report);source_shas.add(row['sha256'])
                result_sources.append({**{name:report.get(name) for name in ('replay_version','arena_id','roster','winner_source',
                    'winner_absolute_team','post_battle_common','post_battle_result_errors')},'source_sha':row['sha256'],'report_sha256':row['report_sha256']})
                try:source,origin=self._source(row,report)
                except (ValueError,OSError,KeyError) as exc:
                    source={'source_sha':row['sha256'],'core_verified':False,'roster':report.get('roster',[]),'statistics':{}}
                    origin='unknown';errors.append({'battle_id':battle['id'],'source_sha':row['sha256'],'reason':str(exc)})
                sources.append(source);origins[origin]+=1
            if not reports:continue
            reference=reports[0];roster=_participants(reference);signature=_signature(roster)
            adopted=[]
            for source in sources:
                if _signature(_participants(source))==signature:adopted.append(source)
                else:errors.append({'battle_id':battle['id'],'source_sha':source['source_sha'],'reason':'来源参赛名单不一致'})
            mappings={r['tournament_name']:r for r in effective_mappings(reference,legacy,assignments,players)}
            confirmed_ids=[(mappings.get(p.get('name'),{}).get('resolved_identity') or {}).get('player_id') for p in roster
                           if (mappings.get(p.get('name'),{}).get('resolved_identity') or {}).get('status')=='confirmed']
            conflicting={identity for identity,n in Counter(confirmed_ids).items() if identity and n>1}
            outcomes={team:team_outcome({'battle_id':battle['id'],'arena_id':battle['arena_id'],'result_sources':result_sources},
                roster,[p for p in roster if p['teamId']==team]) for team in {p['teamId'] for p in roster}}
            seen=set()
            for p in roster:
                raw_name=p.get('name') or f'未知赛号 {p["shipId"]}';mapping=mappings.get(raw_name,{})
                identity=mapping.get('resolved_identity') or {};person=identity.get('player_id')
                confirmed=identity.get('status')=='confirmed' and person and person not in conflicting
                account=p.get('accountDBID');event=battle['event_key'];actor_key='player:'+person if confirmed else f'account:{event}:{account}:{raw_name}'
                aliases[f'account:{event}:{account}:{raw_name}'].add(actor_key)
                if actor_key in seen:
                    errors.append({'battle_id':battle['id'],'player_key':actor_key,'reason':'同局重复参赛身份，未纳入个人聚合'})
                    previous=profiles.get(actor_key)
                    if previous:
                        previous['matches']=[r for r in previous['matches'] if r['battle_id']!=battle['id']]
                        previous.setdefault('excluded_matches',[]).append(battle['id']);previous['identity_status']='conflict';previous['_public_accounts']={}
                    continue
                seen.add(actor_key)
                status=('confirmed' if confirmed else 'conflict' if identity.get('status')=='conflict' or person in conflicting
                        else 'confirmed_account' if mapping.get('confirmed') else 'tournament_account')
                display=mapping.get('personal_nickname') if status in ('confirmed','confirmed_account') else raw_name
                profile=profiles.setdefault(actor_key,{'player_key':actor_key,'player_id':person if confirmed else None,
                    'name':display or raw_name,'identity_status':status,'matches':[],'_public_accounts':{},'_tournament_accounts':{}})
                if status=='conflict':profile['identity_status']='conflict'
                identity_key=(event,str(account),raw_name)
                tournament=profile['_tournament_accounts'].setdefault(identity_key,{'event_key':event,'tournament_name':raw_name,
                    'tournament_account_id':account,'battle_ids':[],'clan':parse_kots_name(raw_name,event).get('clan')})
                tournament['battle_ids'].append(battle['id'])
                if status in ('confirmed','confirmed_account') and mapping.get('personal_nickname'):
                    public_id=mapping.get('account_id');region='na' if mapping.get('region')=='com' else mapping.get('region')
                    public_key=(region,str(public_id) if public_id else None,mapping['personal_nickname'])
                    profile['_public_accounts'][public_key]={'region':region,'account_id':str(public_id) if public_id else None,
                        'nickname':mapping['personal_nickname'],'confirmed':True,'source':identity.get('source'),
                        'assignment_versions':identity.get('assignment_versions',[])}
                ship=str(p.get('shipParamsId'));info=catalog.get(ship,{})
                safe_player=dict(p,accountDBID=account,shipParamsId=p.get('shipParamsId'))
                damage=received_for_player(safe_player,adopted,self.profile['received_damage_fields'])
                profile['matches'].append({'battle_id':battle['id'],'date':battle['date_raw'],'event_key':event,'label':battle['label'],
                    'map_id':battle['map_id'],'map_name':maps.get(battle['map_id'],battle['map_id']),
                    'tournament_name':raw_name,'tournament_account_id':account,'entity_id':p['shipId'],'team_id':p['teamId'],
                    'ship_id':ship,'ship_name':info.get('display_name') or info.get('internal_name') or ship,'ship_class':info.get('species'),
                    'source_shas':[s['source_sha'] for s in adopted],'source_count':len(adopted),
                    **deepcopy(outcomes[p['teamId']]),**damage})
            battle_meta[battle['id']]={k:battle[k] for k in ('id','date_raw','map_id','event_key','label')}
        cancelled(cancel)
        if before!=self.snapshot(battle_ids):raise ValueError('比赛或身份资料在整理期间变化，请重新载入档案')
        for profile in profiles.values():_aggregate_profile(profile,catalog)
        directory=[{k:p[k] for k in ('player_key','player_id','name','identity_status','scope_label','appearances','distinct_ships','top_ship_id','public_accounts','tournament_accounts')}
                   for p in sorted(profiles.values(),key=lambda p:(-p['appearances'],p['name']))]
        result={'schema_version':1,'kind':'individual_replay_dossiers','cache_key':key,'profiles':profiles,'players':directory,
            'aliases':{raw:next(iter(keys)) for raw,keys in aliases.items() if len(keys)==1},
            'ambiguous_aliases':[raw for raw,keys in aliases.items() if len(keys)>1],
            'input_snapshot':before,'battles':battle_meta,'source_errors':errors,
            'counts':{'selected_battles':len(before['battles']),'players':len(profiles),'appearances':sum(p['appearances'] for p in profiles.values()),
                      'source_views':len(source_shas),'report_statistics_sources':origins['report'],'cached_statistics_sources':origins['cache'],
                      'unknown_statistics_sources':origins['unknown']},
            'limitations':['按当前范围内已确认身份关联；未确认赛号不按昵称或公共账号 ID 自动合并。',
                '赛事 accountDBID 与公共 account_id 分属不同命名空间。',
                '承伤来自赛后分类结算，没有逐次伤害类型的发生时刻。']}
        return self.statistics.save('player_dossier',key,result)
