from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from wows_training.career_service import CareerService,load_references
from wows_training.career_rating import project_ships,summarize_ships
from wows_training.public_stats import PublicStatsClient
from wows_training.runtime_bundle import load_bundle


def ship(sid):return {'ship_id':sid,'ship_id_str':'TEST'+str(sid),'name':'测试舰'+str(sid),'tier':10,'type':'Submarine','nation':'usa'}
def page(rows,total=101,page_total=2):return {'status':'ok','meta':{'count':len(rows),'total':total,'page_total':page_total},'data':{str(i):ship(i) for i in rows}}
def statistics():return {'battles':10,'wins':6,'damage_dealt':200000,'frags':10,'xp':20000}


class Secrets:
    def get(self,key):return 'fixture-key-only'
    def set(self,key,value):pass


class CareerUpdatesTests(unittest.TestCase):
    def client(self,transport,cancel=None):return PublicStatsClient('fixture-key-only',enabled=True,transport=transport,cancel=cancel)

    def test_ship_directory_is_paged_localized_complete_and_sanitized(self):
        calls=[]
        def transport(url,params,timeout,cancel):
            calls.append(deepcopy(params));return page(range(1,101)) if params['page_no']==1 else page([101])
        result=self.client(transport).encyclopedia('asia','zh-cn')
        self.assertEqual(result['status'],'ok');self.assertEqual(len(result['data']['ships']),101)
        self.assertTrue(result['data']['complete']);self.assertEqual([v['page_no'] for v in calls],[1,2])
        self.assertEqual(calls[0]['language'],'zh-cn');self.assertNotIn('fixture-key-only',json.dumps(result))

    def test_missing_empty_truncated_and_inconsistent_pages_are_rejected(self):
        cases=[page([],101),page([101],102),page([101],101,3),{'status':'ok','meta':{'page_total':2},'data':{'101':ship(101)}},page([100])]
        for second in cases:
            with self.subTest(second=second):
                result=self.client(lambda u,p,t,c:page(range(1,101)) if p['page_no']==1 else second).encyclopedia('asia')
                self.assertEqual(result['status'],'error');self.assertIsNone(result['data'])
        result=self.client(lambda u,p,t,c:page([1],101)).encyclopedia('asia')
        self.assertEqual(result['status'],'error')

    def test_metadata_identity_and_pagination_limits_are_enforced(self):
        for invalid in (page([1],3001,31),dict(page([1],1,1),data={'1':ship(2)})):
            self.assertEqual(self.client(lambda *a:invalid).encyclopedia('asia')['status'],'error')

    def test_cancelled_between_pages_discards_partial_directory(self):
        event=threading.Event();calls=[]
        def transport(*args):calls.append(True);event.set();return page(range(1,101))
        result=self.client(transport,event).encyclopedia('asia')
        self.assertEqual(result['reason'],'cancelled');self.assertIsNone(result['data']);self.assertEqual(len(calls),1)

    def test_disabled_directory_never_requests(self):
        client=PublicStatsClient('fixture-key-only',transport=lambda *a:self.fail('Unexpected request'))
        self.assertEqual(client.encyclopedia('asia')['reason'],'network_disabled')
        self.assertEqual(client.encyclopedia_info('asia')['reason'],'network_disabled')

    def test_local_metadata_covers_current_ship_but_no_expected_remains_unknown(self):
        catalog={'1':{'display_name':'本机新潜艇','species':'Submarine','tier':10,'nation':'USA'}}
        refs={'expected':{'1':{'expected_damage':10000,'expected_frags':1,'expected_winrate':.5}},'benchmarks':{},'encyclopedia':{}}
        rows=project_ships([{'ship_id':'1','statistics':statistics()}],'pvp',refs,catalog)
        self.assertEqual(rows[0]['name'],'本机新潜艇');self.assertEqual(rows[0]['metadata_source'],'local_client_15_9')
        self.assertEqual(summarize_ships(rows,'pvp',refs)['rating']['covered_battles'],10)
        unknown=project_ships([{'ship_id':'1','statistics':statistics()}],'pvp',dict(refs,expected={}),catalog)[0]
        self.assertIsNone(unknown['pr']);self.assertEqual(unknown['pr_status'],'expected_values_missing');self.assertEqual(unknown['battles'],10)

    def test_refresh_references_pins_old_profiles_and_new_queries_use_new_values(self):
        with tempfile.TemporaryDirectory() as directory:
            refs={'encyclopedia':{'1':ship(1)},'expected':{'1':{'expected_damage':10000,'expected_frags':1,'expected_winrate':.5}},'benchmarks':{},'metadata':{}}
            def transport(url,params,timeout,cancel):
                if url.endswith('/account/info/'):return {'status':'ok','data':{'7':{'account_id':7,'nickname':'Test','hidden_profile':False,'statistics':{'pvp':statistics()}}}}
                return {'status':'ok','data':{'7':[{'ship_id':1,'pvp':statistics()}]}}
            service=CareerService(directory,transport=transport,secrets=Secrets(),references=deepcopy(refs));service.save_settings(True)
            old=service.refresh('asia',7);pr=old['rating']['pr']
            service.reference_transport=lambda c:{'1':{'expected_damage':50000,'expected_frags':2,'expected_winrate':.6}}
            self.assertEqual(service.refresh_reference()['status'],'ok')
            service.references['encyclopedia']['1']['name']='Renamed'
            saved=service.profile('asia',7,snapshot_id=old['snapshot_id'])
            self.assertEqual(saved['rating']['pr'],pr);self.assertEqual(saved['ships'][0]['name'],'测试舰1');self.assertTrue(saved['reference_context']['pinned'])
            new=service.refresh('asia',7);self.assertNotEqual(new['rating']['pr'],pr)
            restarted=CareerService(directory,references=deepcopy(refs),secrets=Secrets())
            self.assertEqual(restarted.profile('asia',7,snapshot_id=old['snapshot_id'])['rating']['pr'],pr)

    def test_encyclopedia_refresh_is_immutable_explicit_and_restartable(self):
        with tempfile.TemporaryDirectory() as directory:
            calls=[]
            def transport(url,params,timeout,cancel):
                calls.append(url)
                if url.endswith('/encyclopedia/info/'):return {'status':'ok','data':{'game_version':'15.9.0','languages':{'zh-cn':'中文'}}}
                return page([1],1,1)
            service=CareerService(directory,transport=transport,secrets=Secrets(),references={})
            self.assertEqual(service.refresh_encyclopedia()['status'],'not_fetched');self.assertEqual(calls,[])
            service.save_settings(True);updated=service.refresh_encyclopedia()
            self.assertEqual(updated['status'],'ok');self.assertEqual(updated['records'],1)
            restarted=CareerService(directory,references={},secrets=Secrets())
            self.assertEqual(restarted.reference_status()['encyclopedia_records'],1)
            self.assertEqual(restarted.reference_status()['encyclopedia_reference']['game_version'],'15.9.0')
            self.assertNotIn('fixture-key-only',json.dumps(service.store.list_runs()))
            service.transport=lambda *a:page([],1,1)
            self.assertEqual(service.refresh_encyclopedia()['status'],'error')
            self.assertEqual(len(service.store.list_runs('career_encyclopedia')),1)

    def test_current_bundle_has_new_15_9_metadata_and_preserved_legacy_reference(self):
        refs=load_references();legacy=refs['legacy'];native=load_bundle().ship_catalog()
        self.assertEqual(len(legacy['encyclopedia']),967);self.assertEqual(len(legacy['expected']),941)
        self.assertEqual(len(refs['encyclopedia']),1017);self.assertEqual(len(refs['expected']),978)
        added=set(refs['encyclopedia'])-set(legacy['encyclopedia']);self.assertEqual(len(added),50)
        rows=project_ships([{'ship_id':sid,'statistics':statistics()} for sid in sorted(added)],'pvp',refs,native)
        self.assertTrue(all(row['name']!=row['ship_id'] and row['species'] and row['tier'] for row in rows))
        self.assertEqual(refs['metadata']['encyclopedia_reference']['game_version'],'15.9.0')


if __name__=='__main__':unittest.main()
