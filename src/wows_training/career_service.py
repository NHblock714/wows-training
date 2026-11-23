"""Explicit public queries and dated legacy imports for personal dossiers.

All percentages are ratios in [0,1]. Public identities are (region, account_id),
never tournament accountDBID values. Queries never run during profile reads.
"""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.request import Request,build_opener

from .career_rating import metrics,project_ships,summarize_ships,number,mechanical_means
from .credential_store import SecretStore, SecretStoreError
from .public_stats import PublicStatsClient,REGIONS,ACCOUNT_MODES,MODES,_positive_id,_NoRedirect
from .workbench_store import WorkbenchStore

MODE_ORDER=('pvp','pvp_solo','pvp_div2','pvp_div3','pve','rank_solo','club','oper_solo','oper_div')
MAX_FILE_BYTES=8*1024*1024
EXPECTED_URL='https://api.wows-numbers.com/personal/rating/expected/json/'
WG_PUBLIC_CONFIG = 'wg-public.json'


def _bundled_application_id():
    """Read the public Application ID included in an approved distribution."""
    root = getattr(sys, '_MEIPASS', None)
    if not getattr(sys, 'frozen', False) or not root:
        return None
    path = Path(root) / 'career_data' / WG_PUBLIC_CONFIG
    try:
        with path.open('rb') as stream:
            raw = stream.read(1025)
        if len(raw) > 1024:
            return None
        config = json.loads(raw)
    except (OSError, ValueError):
        return None
    if (not isinstance(config, dict) or set(config) != {'schema', 'application_id'}
            or type(config['schema']) is not int or config['schema'] != 1):
        return None
    value = config['application_id']
    return value if isinstance(value, str) and re.fullmatch(r'[0-9A-Fa-f]{32}', value) else None


def utc():return datetime.now(timezone.utc).isoformat()


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=True,allow_nan=False).encode()).hexdigest()


def _cancel(cancel):
    if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):raise InterruptedError('已取消查询或导入')


def _read_json(path):
    path=Path(path)
    if not path.is_file() or path.stat().st_size>MAX_FILE_BYTES:raise ValueError('资料文件不存在或超过读取限制')
    raw=path.read_bytes()
    try:value=json.loads(raw.decode('utf-8-sig'),parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    except (ValueError,UnicodeError):raise ValueError('资料不是有效的 UTF-8 JSON') from None
    return value,{'path':str(path.resolve()),'sha256':hashlib.sha256(raw).hexdigest(),
        'file_mtime_utc':datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat()}


def load_references(directory=None):
    base=Path(directory) if directory else (Path(sys._MEIPASS) if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[2])/'career_data'
    if not (base/'metadata.json').is_file():return {'expected':{},'benchmarks':{},'encyclopedia':{},'metadata':{'status':'missing'}}
    metadata,_=_read_json(base/'metadata.json');result={'metadata':metadata}
    for key in ('expected','benchmarks','encyclopedia'):
        value,source=_read_json(base/(key+'.json'))
        if source['sha256']!=metadata.get('files',{}).get(key+'.json'):raise ValueError('公开战绩参考资料校验失败')
        if not isinstance(value,dict):raise ValueError('公开战绩参考资料格式错误')
        result[key]=value
    if 'legacy_references.json' in metadata.get('files',{}):
        value,source=_read_json(base/'legacy_references.json')
        if source['sha256']!=metadata['files']['legacy_references.json']:raise ValueError('历史战绩参考资料校验失败')
        if not isinstance(value,dict) or any(not isinstance(value.get(k),dict) for k in ('expected','encyclopedia','benchmarks','metadata')):raise ValueError('历史战绩参考资料格式错误')
        result['legacy']=value
    return result


def _identity(region,account_id):
    region='na' if str(region).lower()=='com' else str(region).lower();account_id=_positive_id(account_id)
    if region not in REGIONS or account_id is None:raise ValueError('请选择有效区服与公共账号 ID')
    return region,account_id


def _mode_result(stats):
    if stats is None:return {'status':'unsupported','reason':'mode_data_not_returned','statistics':None}
    if not isinstance(stats,dict) or type(stats.get('battles')) is not int or stats['battles']<0:raise ValueError('模式场次字段无效')
    return {'status':'ok' if stats['battles'] else 'no_battles','statistics':stats}


def _expected_data(raw):
    raw=raw.get('data',raw) if isinstance(raw,dict) else None
    if not isinstance(raw,dict) or len(raw)>5000:raise ValueError('PR 参考数据格式无效')
    output={}
    for key,row in raw.items():
        sid=_positive_id(key)
        if sid is None or not isinstance(row,dict):continue
        if 'expected_damage' in row:values=[row.get(k) for k in ('expected_damage','expected_frags','expected_winrate')]
        else:
            wr=row.get('win_rate');values=[row.get('average_damage_dealt'),row.get('average_frags'),wr/100 if number(wr) else None]
        if all(number(v) and v>0 for v in values) and values[2]<=1:
            output[sid]=dict(zip(('expected_damage','expected_frags','expected_winrate'),values))
    if not output:raise ValueError('PR 参考数据没有有效舰船记录')
    return output


def _fetch_expected(cancel):
    deadline=time.monotonic()+12;request=Request(EXPECTED_URL,headers={'User-Agent':'WOWSTraining career reference'},method='GET')
    raw=bytearray()
    with build_opener(_NoRedirect()).open(request,timeout=8) as response:
        while True:
            _cancel(cancel)
            if time.monotonic()>deadline:raise TimeoutError()
            block=response.read(min(65536,MAX_FILE_BYTES+1-len(raw)))
            if not block:break
            raw.extend(block)
            if len(raw)>MAX_FILE_BYTES:raise ValueError('Reference response exceeds limit')
    return json.loads(raw.decode('utf8'),parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))


class CareerService:
    def __init__(self,root,bundle=None,*,transport=None,secrets=None,references=None,reference_transport=None):
        self.store=WorkbenchStore(root);self.root=self.store.root;self.secrets=secrets or SecretStore(root)
        self._public_application_id = _bundled_application_id()
        self.transport=transport;self.references=references if references is not None else load_references()
        self.legacy_references=deepcopy(self.references.get('legacy') or {k:self.references.get(k,{}) for k in ('expected','encyclopedia','benchmarks','metadata')})
        self.legacy_references['legacy_only']=True
        self.reference_transport=reference_transport or _fetch_expected
        self.catalog=bundle.ship_catalog() if bundle is not None else {}
        self._reference_id=None;self._encyclopedia_id=None
        self._reload_reference()

    def _reload_reference(self):
        history=self.store.list_runs('career_reference')
        if history and history[-1]['id']!=self._reference_id:
            latest=history[-1]['payload'];self.references=deepcopy(self.references)
            self.references['expected']=_expected_data(latest['expected'])
            self.references['metadata']=dict(self.references.get('metadata') or {},expected_reference=dict(latest['source'],snapshot_id=history[-1]['id']))
            self._reference_id=history[-1]['id']
        history=self.store.list_runs('career_encyclopedia')
        if history and history[-1]['id']!=self._encyclopedia_id:
            latest=history[-1]['payload'];self.references=deepcopy(self.references)
            self.references['encyclopedia']=deepcopy(latest['encyclopedia'])
            self.references['metadata']=dict(self.references.get('metadata') or {},encyclopedia_reference=dict(latest['source'],snapshot_id=history[-1]['id']))
            self._encyclopedia_id=history[-1]['id']

    def _settings_document(self):
        rows=self.store.list_documents('public_stats_settings');return rows[-1] if rows else None

    def _saved_application_id(self):
        try:
            return self.secrets.get('wg_application_id')
        except (OSError, SecretStoreError):
            return None

    def _query_enabled(self):
        doc = self._settings_document()
        return doc['payload'].get('enabled') is True if doc else bool(self._public_application_id)

    def _application_id(self):
        return self._saved_application_id() or self._public_application_id

    def settings(self):
        return {'enabled': self._query_enabled(), 'credential_present': bool(self._application_id())}

    def save_settings(self,enabled,application_id=None):
        if application_id not in (None,''):
            if not isinstance(application_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',application_id):raise ValueError('Application ID 格式无效')
            self.secrets.set('wg_application_id',application_id)
        doc=self._settings_document();value=deepcopy(doc['payload']) if doc else {}
        value.update(enabled=enabled is True,credential_name='wg_application_id')
        self.store.save_document('public_stats_settings','Wargaming public statistics',value,doc['id'] if doc else None)
        return self.settings()

    def import_credentials(self,path):
        if self._saved_application_id():return {'status':'kept_existing','credential_present':True}
        path=Path(path)
        if str(path).startswith(('\\\\','//')) or not path.is_file() or path.stat().st_size>65536:raise ValueError('请选择本地原项目配置文件')
        try:text=path.read_text(encoding='utf-8-sig')
        except (OSError,UnicodeError):raise ValueError('无法读取原项目配置文件') from None
        found=[]
        for line in text.splitlines():
            match=re.fullmatch(r'\s*(?:export\s+)?APPLICATION_ID\s*=\s*(.*?)\s*',line)
            if not match:continue
            value=match.group(1)
            if value[:1] in ('"',"'"):
                quote=value[0];end=value.find(quote,1)
                if end<0 or value[end+1:].strip() and not value[end+1:].strip().startswith('#'):raise ValueError('原配置中的 Application ID 格式无效')
                value=value[1:end]
            else:value=value.split('#',1)[0].strip()
            if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',value):raise ValueError('原配置中的 Application ID 格式无效')
            found.append(value)
        if len(set(found))!=1:raise ValueError('原配置未提供唯一有效的 APPLICATION_ID')
        self.save_settings(self.settings()['enabled'],found[0])
        return {'status':'imported','credential_present':True}

    def _client(self,cancel=None):
        enabled = self._query_enabled()
        key = self._application_id() if enabled else None
        return PublicStatsClient(key,enabled=enabled,max_results=1000,transport=self.transport,cancel=cancel)

    def search(self,region,nickname,cancel=None):return self._client(cancel).search(region,nickname)
    def search_clans(self,region,query,cancel=None):return self._client(cancel).search_clans(region,query)
    def clan(self,region,clan_id,cancel=None):return self._client(cancel).clan(region,clan_id)

    def _save_reference(self,expected,source):
        record=self.store.save_run('career_reference','PR 舰船参考基准',{'schema_version':1,'expected':expected,'source':source})
        self.references=deepcopy(self.references);self.references['expected']=expected
        self.references['metadata']=dict(self.references.get('metadata') or {},expected_reference=dict(source,snapshot_id=record['id']))
        self._reference_id=record['id']
        return {'status':'ok','records':len(expected),'source':source,'snapshot_id':record['id']}

    def import_reference(self,path,cancel=None):
        _cancel(cancel);raw,proof=_read_json(path);expected=_expected_data(raw);_cancel(cancel)
        return self._save_reference(expected,dict(proof,provider='wows-numbers 本地参考文件',time_kind='file_mtime',timestamp=proof['file_mtime_utc'],imported_at_utc=utc(),fetched_at_utc=None))

    def refresh_reference(self,cancel=None,progress=None):
        if not self.settings()['enabled']:return {'status':'not_fetched','reason':'network_disabled'}
        _cancel(cancel)
        if progress:progress('读取公开 PR 参考基准…')
        try:
            raw=self.reference_transport(cancel);expected=_expected_data(raw);_cancel(cancel)
            return self._save_reference(expected,{'provider':'wows-numbers','url':EXPECTED_URL,'sha256':digest(raw),
                'time_kind':'fetched_at_utc','timestamp':utc(),'fetched_at_utc':utc()})
        except InterruptedError:raise
        except Exception:return {'status':'error','reason':'reference_request_failed'}

    def reference_status(self):
        self._reload_reference();meta=self.references.get('metadata') or {}
        return {'encyclopedia_records':len(self.references.get('encyclopedia') or {}),'expected_records':len(self.references.get('expected') or {}),
            'local_catalog_records':len(self.catalog),'encyclopedia_reference':deepcopy(meta.get('encyclopedia_reference') or {}),
            'expected_reference':deepcopy(meta.get('expected_reference') or {}),'replay_version':'15.9.0.13357625'}

    def refresh_encyclopedia(self,region='asia',cancel=None,progress=None):
        _cancel(cancel);client=self._client(cancel)
        info=client.encyclopedia_info(region);_cancel(cancel)
        if info['status']!='ok':return {'status':info['status'],'reason':info.get('reason')}
        languages=info['data'].get('languages') or {}
        language=next((v for v in ('zh-cn','zh','zh-tw','en') if v in languages),'en')
        result=client.encyclopedia(region,language,progress);_cancel(cancel)
        if result['status']!='ok':return {'status':result['status'],'reason':'encyclopedia_request_failed'}
        source={'provider':'Wargaming','url':result['source']['url'],'region':result['region'],'language':language,
            'game_version':info['data']['game_version'],'time_kind':'fetched_at_utc','timestamp':result['fetched_at_utc'],
            'fetched_at_utc':result['fetched_at_utc'],'ships_updated_at':info['data'].get('ships_updated_at'),
            'sha256':digest(result['data']['ships']),'scope':'Public metadata does not extend supported replay versions.'}
        record=self.store.save_run('career_encyclopedia','公开舰船百科',{'schema_version':1,'encyclopedia':result['data']['ships'],'source':source})
        self._reload_reference()
        return {'status':'ok','records':len(result['data']['ships']),'source':source,'snapshot_id':record['id']}

    def _snapshot_references(self,payload):
        """Pin rating inputs so refreshing ship references never changes old results."""
        refs=self.legacy_references if payload.get('source_type')=='legacy_cache' else self.references
        ids={str(row['ship_id']) for mode in payload.get('mode_results',{}).values() for row in mode.get('ships',{}).get('ships',[]) if isinstance(row,dict) and 'ship_id' in row}
        return {'expected':{sid:deepcopy(refs.get('expected',{}).get(sid)) for sid in ids if sid in refs.get('expected',{})},
            'encyclopedia':{sid:deepcopy(refs.get('encyclopedia',{}).get(sid)) for sid in ids if sid in refs.get('encyclopedia',{})},
            'benchmarks':deepcopy(refs.get('benchmarks') or {}),'metadata':deepcopy(refs.get('metadata') or {}),
            'mechanical_means':mechanical_means(refs.get('encyclopedia') or {}),'legacy_only':payload.get('source_type')=='legacy_cache',
            'local_catalog':{sid:{key:deepcopy(self.catalog[sid][key]) for key in ('display_name','internal_name','species','tier','nation','index') if key in self.catalog[sid]} for sid in ids if sid in self.catalog}}

    def _records(self,region=None,account_id=None):
        if region is None:
            rows = self.store.list_runs('career_snapshot')
        else:
            with self.store._connection() as db:
                records = db.execute("""SELECT * FROM runs WHERE kind='career_snapshot'
                    AND json_extract(payload_json,'$.region')=?
                    AND json_extract(payload_json,'$.account_id')=?""", (region,account_id))
                rows = [self.store._decode(record) for record in records]
        rows=[r for r in rows if r['payload'].get('schema_version')==1]
        return sorted(rows,key=lambda r:(r['payload'].get('source_type')=='official_query',r['payload'].get('source_updated_at') or 0 if r['payload'].get('source_type')=='legacy_cache' else 0,r['created_at'],r['id']),reverse=True)

    def _save(self,payload):
        fingerprint=payload.get('import_fingerprint')
        if fingerprint:
            old=next((r for r in self._records(payload['region'],payload['account_id']) if r['payload'].get('import_fingerprint')==fingerprint),None)
            if old:return old
        if payload.get('account_status')=='ok':payload=dict(payload,rating_references=self._snapshot_references(payload))
        return self.store.save_run('career_snapshot',(payload.get('nickname') or payload['account_id'])+' · '+payload['region'],payload)

    def refresh(self,region,account_id,mode='pvp',cancel=None,progress=None,*,include_achievements=False,
                include_all_fleets=False,record_failures=True):
        self._reload_reference()
        region,account_id=_identity(region,account_id)
        if mode not in ACCOUNT_MODES:raise ValueError('不支持该战斗模式')
        _cancel(cancel);client=self._client(cancel)
        if progress:progress('读取公开账号与模式战绩…')
        account=client.account_modes(region,account_id,MODE_ORDER);_cancel(cancel)
        source_type='official_query';state=account['status'];data=account.get('data') or {};modes={}
        if state=='ok':
            for name,row in data['modes'].items():modes[name]={'account':row,'ships':{'status':'not_fetched','reason':'mode_ship_stats_not_requested','ships':[]}}
            candidates = MODE_ORDER if include_all_fleets else (mode,)
            for name in candidates:
                _cancel(cancel)
                entry = modes.get(name)
                if entry is None:
                    continue
                if name not in MODES:
                    entry['ships'] = {'status':'unsupported','reason':'mode_ship_stats_unsupported','ships':[]}
                    continue
                status = entry['account']['status']
                if include_all_fleets and status == 'no_battles':
                    entry['ships'] = {'status':'no_battles','reason':None,'ships':[]}
                    continue
                if status not in ('ok','no_battles'):
                    continue
                if progress:
                    progress('读取各模式的公开船仓…' if include_all_fleets else '读取所选模式的完整船仓…')
                ships = client.ships(region,account_id,name)
                _cancel(cancel)
                if ships['status'] == 'private':
                    state, modes = 'private', {}
                    break
                entry['ships'] = {'status':ships['status'],'reason':ships.get('reason'),
                    'ships':(ships.get('data') or {}).get('ships',[]),'source':ships.get('source')}
        timestamp=account.get('fetched_at_utc') or utc()
        nickname=data.get('nickname') or account.get('nickname')
        payload={'schema_version':1,'source_type':source_type,'region':region,'account_id':account_id,
            'nickname':nickname if isinstance(nickname,str) and nickname else account_id,'account_status':state,'requested_mode':mode,
            'mode_results':modes,'reason':account.get('reason'),'timestamp':timestamp,
            'time_kind':'fetched_at_utc' if state in ('ok','private','no_battles','not_found') else 'request_attempt',
            'source_updated_at':data.get('source_updated_at'),'stats_updated_at':data.get('stats_updated_at'),
            'source':account.get('source'),'created_at_utc':utc()}
        if not record_failures and state in ('error', 'not_fetched'):
            return self._empty(region,account_id,mode,status=state,reason=account.get('reason'))
        if include_achievements and state == 'ok' and mode == 'pvp':
            if progress:progress('读取公开战斗成就…')
            result=client.achievements(region,account_id)
            _cancel(cancel)
            payload['achievements']={'status':result['status'], 'battle':(result.get('data') or {}).get('battle')}
        record=self._save(payload)
        return self._project(record,mode)

    def import_legacy_cache(self,path,cancel=None,progress=None):
        path=Path(path)
        if str(path).startswith(('\\\\','//')):raise ValueError('请选择本地缓存文件或目录')
        if path.is_dir() and (path/'data/cache').is_dir():path=path/'data/cache'
        files=sorted(path.glob('*.json')) if path.is_dir() else [path]
        if len(files)>500:raise ValueError('缓存文件数超过导入限制')
        groups={}
        pattern=re.compile(r'^(asia|eu|com|na)_([0-9]{1,20})_(info|ships)_([a-z0-9_-]+)\.json$',re.I)
        for index,file in enumerate(files):
            _cancel(cancel);match=pattern.fullmatch(file.name)
            if not match:continue
            region,account_id=_identity(match[1],match[2]);kind=match[3].lower()
            if progress:progress(f'读取旧缓存 {index+1}/{len(files)}')
            value,proof=_read_json(file);group=groups.setdefault((region,account_id),{'info':None,'ships':{},'files':[]})
            group['files'].append(proof)
            if kind=='info':
                if not isinstance(value,dict) or _positive_id(value.get('account_id'))!=account_id:raise ValueError('缓存账号与文件名不一致')
                if value.get('nickname') is not None and not isinstance(value['nickname'],str):raise ValueError('缓存昵称字段无效')
                if group['info'] is not None and group['info']!=value:raise ValueError('同账号存在相互冲突的账号缓存，请单独导入')
                group['info']=value
            else:
                if not isinstance(value,list) or len(value)>1000:raise ValueError('船仓缓存格式或记录数无效')
                modes=[m for m in match[4].split('-') if m in MODES]
                if not modes:raise ValueError('船仓缓存未标识支持的模式')
                for mode in modes:
                    rows=[];seen=set()
                    for row in value:
                        if not isinstance(row,dict) or (row.get('account_id') is not None and _positive_id(row['account_id'])!=account_id):raise ValueError('船仓缓存包含其他账号或无效账号 ID')
                        sid=_positive_id(row.get('ship_id'))
                        if sid is None or sid in seen:raise ValueError('船仓缓存的舰船 ID 无效或重复')
                        seen.add(sid);stats=_mode_result(row.get(mode))
                        if stats['statistics'] is None:raise ValueError('船仓缓存缺少文件名声明的模式')
                        rows.append({'ship_id':sid,'mode':mode,'statistics':stats['statistics'],'last_battle_time':row.get('last_battle_time')})
                    if mode in group['ships'] and group['ships'][mode]!=rows:raise ValueError('同账号存在相互冲突的船仓缓存，请单独导入')
                    group['ships'][mode]=rows
        if not groups:raise ValueError('没有找到原项目的账号或船仓 JSON 缓存')
        prepared=[]
        for (region,account_id),group in groups.items():
            _cancel(cancel);info=group['info'];state='ok';modes={}
            if info is not None and info.get('hidden_profile') is True:state='private'
            elif info is not None and (info.get('hidden_profile') is not False or not isinstance(info.get('statistics'),dict)):state='error'
            if state=='ok':
                for mode in MODE_ORDER:
                    raw=(info.get('statistics') or {}).get(mode) if info else None
                    if raw is not None or mode in group['ships']:
                        rows=group['ships'].get(mode);modes[mode]={'account':_mode_result(raw),
                            'ships':{'status':'ok' if rows and any(r['statistics']['battles'] for r in rows) else 'no_battles' if rows is not None else 'not_fetched',
                                     'ships':rows or [],'reason':None if rows is not None else 'ship_cache_missing'}}
            payload={'schema_version':1,'source_type':'legacy_cache','region':region,'account_id':account_id,
                'nickname':info.get('nickname') if info else account_id,'account_status':state,'mode_results':modes,
                'timestamp':max(r['file_mtime_utc'] for r in group['files']),'time_kind':'legacy_file_mtime','created_at_utc':utc(),
                'source_updated_at':info.get('updated_at') if info and number(info.get('updated_at')) else None,'stats_updated_at':info.get('stats_updated_at') if info and number(info.get('stats_updated_at')) else None,
                'source':{'provider':'原 F1 WOWS 本地缓存','files':group['files'],'fetched_at_utc':None,'imported_at_utc':utc(),
                          'scope':'historical_public_snapshot; filesystem time is not a verified fetch time'},
                'import_fingerprint':digest({'region':region,'account_id':account_id,'hashes':sorted(r['sha256'] for r in group['files'])})}
            # Validate every account before publishing any part of the selected folder.
            json.dumps(payload,ensure_ascii=False,allow_nan=False).encode('utf8')
            mode='pvp' if 'pvp' in modes or not modes else next(iter(modes))
            prepared.append((payload,mode))
        result=[]
        for payload,mode in prepared:
            _cancel(cancel)
            record=self._save(payload);result.append(self._project(record,mode))
        return result

    def _empty(self,region,account_id,mode,status='not_fetched',reason='no_snapshot'):
        return {'status':status,'reason':reason,'region':region,'account_id':account_id,'nickname':account_id,'mode':mode,'modes':[],
            'metrics':{},'rating':None,'ships':[],'class_distribution':[],'tier_distribution':[],'ace_ship':None,
            'source':{},'snapshot_id':None,'timestamp':None,'time_kind':None,'is_stale':True,
            'account_status':status,'ships_status':'not_fetched'}

    def _project(self,record,mode):
        value=record['payload'];result=self._empty(value['region'],value['account_id'],mode,value['account_status'],value.get('reason'))
        result.update(nickname=value.get('nickname') or value['account_id'],snapshot_id=record['id'],timestamp=value.get('timestamp'),
            time_kind=value.get('time_kind'),source=deepcopy(value.get('source') or {}),source_type=value['source_type'],
            created_at_utc=record['created_at'],source_updated_at=value.get('source_updated_at'),stats_updated_at=value.get('stats_updated_at'),
            modes=[m for m in MODE_ORDER if m in value.get('mode_results',{})],account_status=value['account_status'],ships_status='not_fetched')
        if value['source_type']=='official_query' and value['account_status'] in ('ok','private','no_battles','not_found') and result['timestamp']:
            try:result['is_stale']=(datetime.now(timezone.utc)-datetime.fromisoformat(result['timestamp'])).total_seconds()>300
            except (TypeError,ValueError):pass
        if value['account_status']!='ok':return result
        awards=value.get('achievements') or {}
        result['achievements']=deepcopy(awards.get('battle')) if awards.get('status')=='ok' else None
        if mode not in ACCOUNT_MODES:return dict(result,status='unsupported',reason='mode_not_supported')
        entry=value.get('mode_results',{}).get(mode)
        if entry is None:return dict(result,status='unsupported',reason='mode_not_in_snapshot')
        account=entry['account'];fleet=entry['ships'];result.update(status=account['status'],account_status=account['status'],ships_status=fleet['status'],reason=account.get('reason'))
        result['metrics']=metrics(account.get('statistics'))
        refs=value.get('rating_references') or self.legacy_references
        result['reference_context']={'pinned':True,'legacy_compatibility':not bool(value.get('rating_references')),
            'expected_reference':deepcopy((refs.get('metadata') or {}).get('expected_reference') or {}),
            'encyclopedia_reference':deepcopy((refs.get('metadata') or {}).get('encyclopedia_reference') or {})}
        catalog=refs.get('local_catalog',self.catalog)
        try:result['ships']=project_ships(fleet.get('ships',[]),mode,refs,catalog)
        except ValueError:
            return dict(result,status='partial' if account['status']=='ok' else 'error',ships_status='error',reason='invalid_ship_data')
        result.update(summarize_ships(result['ships'],mode,refs))
        if account['status']=='ok' and fleet['status']=='error':result.update(status='partial',reason=fleet.get('reason') or 'ship_query_failed')
        if account['status']=='unsupported' and fleet['status'] in ('ok','no_battles'):result.update(status='partial',reason='account_summary_missing')
        return result

    def profile(self,region,account_id,mode='pvp',snapshot_id=None):
        self._reload_reference()
        try:region,account_id=_identity(region,account_id)
        except ValueError:return self._empty(str(region),None,mode,reason='public_account_unlinked')
        if snapshot_id is not None:
            try:record=self.store.get_run(snapshot_id)
            except KeyError:record=None
            value=record['payload'] if record and record['kind']=='career_snapshot' else {}
            if value.get('schema_version')==1 and value.get('region')==region and value.get('account_id')==account_id:
                return self._project(record,mode)
            return self._empty(region,account_id,mode,reason='snapshot_not_found')
        rows=self._records(region,account_id)
        return self._project(rows[0],mode) if rows else self._empty(region,account_id,mode)

    def history(self,region,account_id):
        region,account_id=_identity(region,account_id)
        results=[]
        for row in self._records(region,account_id):
            mode=row['payload'].get('requested_mode') or ('pvp' if 'pvp' in row['payload'].get('mode_results',{}) or not row['payload'].get('mode_results') else next(iter(row['payload']['mode_results'])))
            value=self._project(row,mode)
            results.append({key:value.get(key) for key in ('region','account_id','nickname','status','mode','modes','snapshot_id','created_at_utc','time_kind','timestamp','source_type')})
        return results

    def list_profiles(self):
        self._reload_reference()
        latest={}
        for row in self._records():
            value=row['payload'];latest.setdefault((value['region'],value['account_id']),row)
        result=[]
        for row in latest.values():
            mode='pvp' if 'pvp' in row['payload'].get('mode_results',{}) or not row['payload'].get('mode_results') else next(iter(row['payload']['mode_results']))
            profile=self._project(row,mode)
            result.append({key:profile.get(key) for key in ('region','account_id','nickname','status','modes','snapshot_id','created_at_utc','time_kind','timestamp','source_type')})
        return sorted(result,key=lambda r:(r['nickname'],r['region'],r['account_id']))
