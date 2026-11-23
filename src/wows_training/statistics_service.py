"""Read current replay sources, independently cache verified statistics and export."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from .library import Library
from .runtime_bundle import load_bundle,file_hash
from .statistics_capture import load_profile
from .statistics_store import StatisticsStore,digest
from .statistics_analysis import summarize
from .multiview_store import BattleRecordingStore
from .workbench_store import WorkbenchStore
from .workbench_service import attach_identities

EXTRACTOR_VERSION='statistics-extract-1'


def cancelled(event):
    if event is not None and event.is_set():raise InterruptedError('统计已取消；已核验缓存保留。')


class StatisticsService:
    def __init__(self,root,bundle_root=None):
        self.library=Library(root);self.bundle=load_bundle(Path(bundle_root) if bundle_root else None)
        self.profile=load_profile(self.bundle);self.store=StatisticsStore(root);self.workbench=WorkbenchStore(root)
        self.recordings=BattleRecordingStore(root)

    def paired_battle_ids(self):return sorted({r['battle_id'] for r in self.recordings.list_recordings()})

    def input_snapshot(self,ids):
        battles=[]
        for key in sorted(set(ids)):
            row=self.library.get_battle(key)
            battles.append({'battle_id':key,'event_key':row['event_key'],'label':row['label'],'notes':row['notes'],
                'views':[{k:s[k] for k in ('sha256','analysis_revision','report_sha256')} for s in row['replays']]})
        return {'battles':battles,'identities':{kind:[(d['id'],d['revision']) for d in self.workbench.list_documents(kind)] for kind in ('player','assignment')},
                'legacy_mappings':self.library.list_mappings(),'profile_sha256':self.profile['profile_sha256'],'bundle_id':self.bundle.manifest['bundle_id']}

    def source_statistics(self,row,cancel=None,progress=None):
        cancelled(cancel)
        key=digest({'source_sha':row['sha256'],'profile':self.profile['profile_sha256'],'extractor':EXTRACTOR_VERSION})
        cached=self.store.get('source',key)
        if cached is not None:
            verification=deepcopy(cached);claimed=verification.pop('content_fingerprint',None)
            if (cached.get('source_sha')!=row['sha256'] or cached.get('extractor_version')!=EXTRACTOR_VERSION
                    or cached.get('statistics',{}).get('profile_sha256')!=self.profile['profile_sha256'] or digest(verification)!=claimed):
                raise ValueError('统计缓存与回放或定义不匹配，未采用缓存值。')
            return cached
        paths=[self.library.root/'raw_replays'/(row['sha256']+'.wowsreplay')]+[Path(p['path']) for p in row.get('sources',[])]
        path=next((p for p in paths if p.is_file() and file_hash(p)==row['sha256']),None)
        if path is None:raise FileNotFoundError('找不到校验一致的原回放；已有名单可统计，承伤保持未知。')
        jobs=self.library.root/'statistics/jobs';jobs.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=jobs) as folder:
            output=Path(folder)/'source.json';log=Path(folder)/'worker.log'
            command=[sys.executable,'--statistics-worker'] if getattr(sys,'frozen',False) else [sys.executable,'-B','-m','wows_training.app','--statistics-worker']
            command+=['--input',str(path),'--output',str(output),'--bundle',str(self.bundle.root)]
            env=os.environ.copy();env['PYTHONIOENCODING']='utf-8'
            if not getattr(sys,'frozen',False):env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])+os.pathsep+env.get('PYTHONPATH','')
            with log.open('wb') as stream:
                process=subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                start=time.monotonic()
                try:
                    while process.poll() is None:
                        cancelled(cancel)
                        if time.monotonic()-start>120:raise TimeoutError('单份回放统计超过120秒。')
                        time.sleep(.04)
                    cancelled(cancel)
                    if not output.is_file() or output.stat().st_size>64*1024*1024:raise ValueError('未得到有效的统计结果。')
                    data=json.loads(output.read_text(encoding='utf8'))
                    if process.returncode or data.get('status')=='failed':raise ValueError(data.get('error','统计解析失败'))
                finally:
                    if process.poll() is None:
                        process.terminate();process.wait(timeout=5)
            if data['source_sha']!=row['sha256'] or file_hash(path)!=row['sha256']:raise ValueError('统计期间原回放发生变化。')
            if data['statistics']['profile_sha256']!=self.profile['profile_sha256']:raise ValueError('统计定义版本不一致。')
            return self.store.save('source',key,data)

    def analyze(self,ids,team,cancel=None,progress=None):
        ids=sorted(set(ids));team=team.strip()
        if not ids or not team:raise ValueError('请选择比赛集合与队伍前缀。')
        before=self.input_snapshot(ids);battles=[];source_errors=[];fingerprints=[]
        total=sum(len(self.library.list_replays(key)) for key in ids);done=0
        for key in ids:
            cancelled(cancel);battle=self.library.get_battle(key);sources=[];results=[]
            for row in battle['replays']:
                done+=1
                if progress:progress(f'核对统计来源 {done}/{total}：{row["recorder"]}')
                report=self.library.get_report(row['sha256'])
                results.append({name:deepcopy(report.get(name)) for name in ('replay_version','arena_id','roster',
                    'winner_source','winner_absolute_team','post_battle_common','post_battle_result_errors')})
                results[-1].update(source_sha=row['sha256'],report_sha256=row['report_sha256'])
                try:data=self.source_statistics(row,cancel,progress)
                except InterruptedError:raise
                except Exception as exc:
                    data={'source_sha':row['sha256'],'core_verified':True,'roster':report['roster'],'metadata':report['metadata'],
                          'host_absolute_team':report.get('host_absolute_team'),'statistics':{'received_damage_fields':self.profile['received_damage_fields']}}
                    source_errors.append({'source_sha':row['sha256'],'battle_id':key,'reason':str(exc)})
                sources.append(data);fingerprints.append(data.get('content_fingerprint') or digest(data))
            reference=deepcopy(sources[0])
            reference['sha256']=reference['source_sha'];reference['arena_id']=battle['arena_id'];reference['event_key']=battle['event_key']
            attach_identities(reference,self.workbench,self.library.list_mappings())
            battles.append({'battle_id':key,'event_key':battle['event_key'],'label':battle['label'],'date':battle['date_raw'],
                'map_id':battle['map_id'],'map_name':self.bundle.json('labels.json')['maps'].get(battle['map_id'],battle['map_id']),
                'arena_id':battle['arena_id'],'roster':reference['roster'],'sources':sources,'result_sources':results})
        cancelled(cancel)
        if before!=self.input_snapshot(ids):raise ValueError('计算期间比赛来源或身份资料发生变化，请重新统计。')
        key=digest({'inputs':before,'team':team,'source_fingerprints':fingerprints,'algorithm':'statistics-summary-4'})
        prior=self.store.get('run',key)
        if prior is not None:return prior
        if progress:progress('汇总队伍、选手用船胜率与已确认承伤分类…')
        payload=summarize(battles,team,self.profile['received_damage_fields'],self.bundle.ship_catalog())
        payload.update(run_id=key,input_snapshot=before,source_errors=source_errors,
                       profile_sha256=self.profile['profile_sha256'],public_field_indices={k:self.profile['public_prefix'].index(k) for k in self.profile['received_damage_fields']})
        cancelled(cancel)
        return self.store.save('run',key,payload)
