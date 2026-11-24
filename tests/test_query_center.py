import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')

from PySide6.QtWidgets import QApplication,QWidget
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from wows_training.career_service import CareerService
from wows_training.desktop.query_center_panel import QueryCenterPanel
from wows_training.desktop.query_visuals import RatingRadar,RadarFocusOverlay,snapshot_card
from wows_training.query_center import QueryCenter, player_target


class FixtureSecrets:
    def __init__(self):
        self.values = {}
    def get(self,key):
        return self.values.get(key)
    def set(self,key,value):
        self.values[key] = value


class Queries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.calls = []
        self.failure = False
        self.sid = '4273911792'
        self.stats = {'battles':5,'wins':3,'damage_dealt':250000,'xp':8000,'frags':5,'survived_battles':2,
                      'damage_scouting':20000,'art_agro':1200000,'torpedo_agro':100000,'main_battery':{'hits':100,'shots':250},
                      'capture_points':25,'dropped_capture_points':10}
        self.career = CareerService(self.folder.name,transport=self.transport,secrets=FixtureSecrets())
        self.service = QueryCenter(self.career)
        self.career.save_settings(True,'demo-queries-test')

    def transport(self,url,params,timeout,cancel):
        self.calls.append(url)
        if self.failure:
            raise ValueError('mock public API unavailable')
        def info(aid):
            return {'account_id':int(aid),'nickname':'Demo'+aid,'hidden_profile':False,'statistics':{'pvp':self.stats,'rank_solo':self.stats}}
        if url.endswith('/account/info/'):
            if ',' in params['account_id']:
                return {'status':'ok','data':{'101':info('101'),'102':{'account_id':102,'nickname':'Private','hidden_profile':True},'103':None}}
            return {'status':'ok','data':{params['account_id']:info(params['account_id'])}}
        if url.endswith('/clans/info/'):
            return {'status':'ok','data':{'111':{'clan_id':111,'name':'Demo Fleet','tag':'DEMO','members':{
                str(i):{'account_name':'Demo'+str(i),'role':'private'} for i in (101,102,103)}}}}
        if url.endswith('/account/achievements/'):
            return {'status':'ok','data':{params['account_id']:{'battle':{'SOLO_WARRIOR':10}}}}
        if url.endswith('/ships/stats/'):
            aid=params['account_id']
            return {'status':'ok','data':{aid:[{'ship_id':int(self.sid),'account_id':int(aid),'pvp':self.stats}]}}
        if url.endswith('/account/list/'):
            return {'status':'ok','data':[{'account_id':101,'nickname':'Demo101'}]}
        if url.endswith('/clans/list/'):
            return {'status':'ok','data':[{'clan_id':111,'name':'Demo Fleet','tag':'DEMO'}]}
        raise AssertionError(url)

    def test_numeric_identity_and_player_clan_search(self):
        rows=self.service.search('com','00101')['candidates']
        self.assertEqual({(r['kind'],r['id'],r['region']) for r in rows},{('player','101','na'),('clan','101','na')})
        self.assertFalse(self.calls)
        rows=self.service.search('asia','Demo')['candidates']
        self.assertEqual([r['kind'] for r in rows],['player','clan'])

    def test_bookmark_import_is_atomic_and_preserves_regions(self):
        path=Path(self.folder.name)/'quick_queries.json'
        valid=[{'region':'美服','account_id':123,'nickname':'Demo'}, {'region':'亚服','account_id':123,'nickname':'Demo'}]
        path.write_text(json.dumps(valid+[{'region':'invalid','account_id':432,'nickname':'Bad'}]))
        with self.assertRaises(ValueError):
            self.service.import_bookmarks(path)
        self.assertEqual(self.service.bookmarks(),[])
        path.write_text(json.dumps(valid))
        self.assertEqual(self.service.import_bookmarks(path),2)
        self.assertEqual(self.service.import_bookmarks(path),0)
        self.service.toggle_bookmark(player_target('na','123','Demo'))
        self.assertEqual([r['region'] for r in self.service.bookmarks()],['asia'])

    def test_batch_privacy_coverage_and_immutable_filtered_clan(self):
        clan=self.service.refresh_clan('asia','111')
        self.assertEqual(set(clan['batch_players']),{'101','102'})
        self.assertTrue(clan['batch_players']['102']['hidden_profile'])
        self.assertEqual(clan['clan_info']['members']['103']['account_name'],'Demo103')
        refs=self.service.load_member_ships(clan,threading.Event(),lambda _:None)
        self.assertEqual(set(refs),{'101'}); self.assertEqual(refs['101']['status'],'ok')
        self.assertEqual(self.service.cached_member_ships(clan),refs)
        refreshed=self.service.refresh_clan('asia','111')
        self.assertIsNone(self.service.cached_member_ships(refreshed))
        self.assertEqual(self.service.cached_clan('asia','111')['snapshot_id'],refreshed['snapshot_id'])
        self.failure=True
        failed=self.service.load_member_ships(clan,threading.Event(),lambda _:None)
        self.assertEqual(failed['101']['snapshot_id'],refs['101']['snapshot_id'])
        self.assertTrue(failed['101']['refresh_failed'])

    def test_optional_achievements_and_failed_refresh_preserve_snapshot(self):
        profile=self.career.refresh('asia','101',include_achievements=True)
        self.assertEqual(profile['achievements'],{'SOLO_WARRIOR':10})
        self.assertEqual(profile['metrics']['avg_capture_points'],5)
        self.assertEqual(profile['metrics']['avg_dropped_capture_points'],2)
        self.failure=True
        self.assertEqual(self.career.refresh('asia','101',record_failures=False)['status'],'error')
        self.assertEqual(self.career.profile('asia','101')['snapshot_id'],profile['snapshot_id'])

    def test_panel_cached_same_ship_and_private_clan(self):
        host=QWidget()
        panel=QueryCenterPanel(host);panel._service=self.service
        self.addCleanup(host.close)
        def wait():
            until=time.monotonic()+8
            while panel.busy and time.monotonic()<until:
                QTest.qWait(12)
            self.assertFalse(panel.busy)
        panel.set_search('Demo101','asia')
        self.assertFalse(self.calls)
        panel.open_target(player_target('asia','101','Demo101'),query_missing=True);wait()
        self.assertEqual(panel.stack.currentIndex(),1)
        self.assertEqual(panel.ship_table.rowCount(),1);self.assertEqual(panel.mode_stats.rowCount(),14)
        self.assertEqual(panel.tabs.tabText(panel.tabs.indexOf(panel.achievements)), '成就展厅')
        self.assertTrue(all(row['status'] == 'earned' for row, _ in panel.achievements._cards))
        self.assertNotIn('/', panel.achievements.banner.count.text())
        self.assertTrue(any(card.accessibleName().startswith('吸鼠坝王 · 已达成')
                            for _, card in panel.achievements._cards))
        self.assertEqual(snapshot_card(panel.profile,panel.detail).width(),1100)
        self.career.refresh('asia','104')
        self.calls.clear()
        panel.peer_edit.setText('Demo104');panel.find_peer();panel.compare();wait()
        self.assertFalse(self.calls)
        self.assertTrue(panel.ship_radar.comparison);self.assertIn('Demo104',panel.ship_info.text())
        panel.min_battles.setValue(6);self.assertEqual(panel.ship_table.rowCount(),0)
        panel.open_target({'kind':'clan','region':'asia','id':'111','name':'Demo'},query_missing=True);wait()
        self.assertEqual(panel.stack.currentIndex(),2);self.assertEqual(panel.member_table.rowCount(),3)
        self.assertEqual({panel.member_table.item(i,2).text() for i in range(3)}, {'公开','隐藏','资料未知'})
        summary = panel.clan_summary
        self.assertEqual(summary.values['overall'].accessibleName(), '维度均分')
        self.assertNotEqual(summary.values['overall'].text(), '—')
        self.assertEqual(summary.coverage.total, 3)
        self.assertEqual({key: count for key, _, count in summary.coverage.rows},
                         {'ok': 1, 'private': 1, 'unknown': 1})
        panel.clan['clan_info']['motto']='Demo motto'
        panel._render_clan()
        self.assertEqual(panel.clan_heading.toolTip(),'Demo motto')
        panel.clan['clan_info']['motto']=None
        panel._render_clan()
        self.assertEqual(panel.clan_heading.toolTip(),'')
        panel.clan_tier.setCurrentIndex(panel.clan_tier.findData(10))
        self.assertFalse(summary.unloaded.isHidden())
        self.assertEqual(summary.values['mean'].text(), '—')
        self.assertEqual({key: count for key, _, count in summary.coverage.rows},
                         {'ok': 0, 'private': 1, 'unknown': 1, 'not_fetched': 1})
        panel.fetch_fleets(); wait()
        self.assertTrue(panel.frames)
        self.assertTrue(summary.unloaded.isHidden())
        self.assertEqual(summary.coverage.total, 3)
        self.assertRegex(panel.member_table.item(0,6).text(),r'^[SABCD] +\d+$')
        before=len(self.calls)
        panel.open_target(panel.target);wait()
        self.assertEqual(len(self.calls),before);self.assertTrue(panel.frames)

    def test_radar_focus_overlay_controls_lifetime_and_focus(self):
        from PySide6.QtWidgets import QPushButton
        from wows_training.desktop.motion import MotionController
        page = QWidget()
        page.resize(760,500)
        opener = QPushButton('放大雷达', page)
        radar = RatingRadar()
        radar.setParent(page)
        scores = {'胜率':(.5,70.), '场均伤害':(40000,88.), '主炮命中':(0.,0.)}
        radar.set_scores(scores, {'胜率':(.55,78.), '场均伤害':(50000,92.)})
        focus = RadarFocusOverlay(page)
        controller = MotionController(page)
        page.show()
        self.addCleanup(page.close)
        self.app.processEvents()
        focus.present(radar, '测试选手', '单舰对比', opener)
        focus._animation.pause()
        focus._animation.setCurrentTime(focus._animation.duration() // 2)
        self.assertTrue(0 < focus.progress < 1)
        focus._animation.resume()
        self.assertTrue(focus.chart.hasFocus())
        self.assertEqual(focus.chart._readout('主炮命中'),'0.0 / 未知')
        QTest.keyClick(focus.chart, Qt.Key.Key_Tab)
        self.assertTrue(focus.close_button.hasFocus())
        QTest.keyClick(focus.close_button, Qt.Key.Key_Escape)
        focus._animation.setCurrentTime(focus._animation.duration())
        self.assertTrue(focus.isHidden())
        self.assertTrue(opener.hasFocus())
        self.assertEqual(radar.scores,scores)
        controller.set_enabled(False)
        focus.present(radar,'测试选手','',opener)
        self.assertEqual(focus.progress,1.)
        page.resize(600,400);self.app.processEvents()
        self.assertTrue(page.rect().contains(focus.card.geometry()))
        QTest.mouseClick(focus,Qt.MouseButton.LeftButton,pos=QPoint(2,2))
        self.assertTrue(focus.isHidden())
        focus.present(radar,'测试选手','',opener)
        page.hide();self.app.processEvents();page.show()
        self.assertTrue(focus.isHidden())

    def test_mode_labels_distinguish_zero_from_unavailable(self):
        host=QWidget();panel=QueryCenterPanel(host);panel._service=self.service
        self.addCleanup(host.close)
        entries={'pvp':{'account':{'status':'ok','statistics':self.stats}}}
        for mode,status in (('pve','no_battles'),('club','private'),('rank_solo','error'),('oper_solo','not_fetched')):
            entries[mode]={'account':{'status':status,'statistics':{'battles':0}}}
        record=self.service.store.save_run('career_snapshot','状态测试',{'mode_results':entries})
        panel.profile={'snapshot_id':record['id']}
        for mode,label,number in (('pve','当前模式无场次','0'),('club','战绩隐藏','未知'),
                                  ('rank_solo','本次查询失败','未知'),('oper_solo','未查询','未知'),
                                  ('oper_div','当前快照未提供该模式','未知')):
            panel.mode.setCurrentIndex(panel.mode.findData(mode))
            self.assertEqual(panel.mode_note.text(),label)
            self.assertEqual(panel.mode_stats.item(0,1).text(),number)

    def test_account_picker_returns_only_explicitly_loaded_player_and_can_be_abandoned(self):
        host=QWidget();host.return_account_picker=Mock();host.show_page=Mock()
        host.dossier_panel=Mock();panel=QueryCenterPanel(host);panel._service=self.service;self.addCleanup(host.close)
        self.career.refresh('asia','101');self.calls.clear()
        context={'token':'one','region':'asia','player_key':'rep:one'}
        panel.begin_account_picker(context)
        self.assertIsNone(panel.target);self.assertFalse(panel.use_account_button.isEnabled());self.assertFalse(self.calls)
        panel.open_target(player_target('asia','101','Demo101'))
        until=time.monotonic()+8
        while panel.busy and time.monotonic()<until:QTest.qWait(12)
        self.assertTrue(panel.use_account_button.isEnabled());self.assertFalse(self.calls)
        panel.open_dossier()
        picked=host.return_account_picker.call_args.args
        self.assertEqual(picked[0],context);self.assertEqual(picked[1]['id'],'101')
        host.dossier_panel.open_public_account.assert_not_called()
        panel.begin_account_picker(context);panel.set_search('SomeoneElse','eu')
        self.assertIsNone(panel._picker_context);self.assertTrue(panel.use_account_button.isHidden())
        panel.begin_account_picker(context);panel.cancel_account_picker()
        host.return_account_picker.assert_called_with(context,None)

    def test_account_picker_rejects_returned_profile_for_another_identity(self):
        host=QWidget();panel=QueryCenterPanel(host);panel._service=self.service;self.addCleanup(host.close)
        panel.begin_account_picker({'token':'one','region':'asia'})
        with patch.object(self.career,'profile',return_value={'status':'ok','region':'asia','account_id':'102'}):
            panel.open_target(player_target('asia','101','Demo101'))
            until=time.monotonic()+8
            while panel.busy and time.monotonic()<until:QTest.qWait(12)
        self.assertIsNone(panel.target);self.assertFalse(panel.use_account_button.isEnabled())


if __name__ == '__main__':
    unittest.main()
