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

from PySide6.QtWidgets import QApplication,QAbstractButton,QDialog,QLineEdit,QPushButton,QToolButton,QVBoxLayout,QWidget
from PySide6.QtCore import QBuffer, QIODevice, QPoint, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from wows_training.career_service import CareerService
from wows_training.desktop.motion import MotionController
from wows_training.desktop.query_center_panel import QueryCenterPanel
from wows_training.desktop.query_visuals import RatingRadar,RadarFocusOverlay,snapshot_card
from wows_training.query_center import QueryCenter, player_target
from wows_training.shared_profile import SharedProfileError


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
        dog_tags = patch('wows_training.desktop.query_center_panel.fetch_dog_tag', return_value=None)
        self.fetch_dog_tag = dog_tags.start()
        self.addCleanup(dog_tags.stop)

    def transport(self,url,params,timeout,cancel):
        self.calls.append(url)
        if self.failure:
            raise ValueError('mock public API unavailable')
        def info(aid):
            return {'account_id':int(aid),'nickname':'Demo'+aid,'hidden_profile':False,'statistics':{'pvp':self.stats,'rank_solo':self.stats}}
        if url.endswith('/account/info/'):
            if ',' in params['account_id']:
                return {'status':'ok','data':{'101':info('101'),'102':{'account_id':102,'nickname':'Private','hidden_profile':True},'103':None}}
            aid = params['account_id']
            if aid == '102':
                record = {'account_id':102,'nickname':'Private','hidden_profile':True}
            elif aid == '103':
                record = None
            else:
                record = info(aid)
            return {'status':'ok','data':{aid:record}}
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

    def test_read_progress_tracks_search_and_respects_reduced_motion(self):
        host = QWidget()
        self.addCleanup(host.close)
        motion = MotionController(host)
        panel = QueryCenterPanel(host)
        panel._service = self.service
        QVBoxLayout(host).addWidget(panel)
        host.show()
        panel.text.setText('Demo')

        for failure in (False, True):
            with self.subTest(failure=failure):
                self.failure = failure
                release = threading.Event()

                def waiting_transport(*args):
                    if not release.wait(3):
                        raise TimeoutError('test transport held too long')
                    return self.transport(*args)

                self.career.transport = waiting_transport
                motion.set_enabled(True, persist=False)
                panel.search()
                try:
                    self.assertFalse(panel.read_progress.isHidden())
                    self.assertEqual(panel.read_progress.maximum(), 0)
                    self.assertIn('搜索公开资料', panel.read_progress.accessibleDescription())
                    motion.set_enabled(False, persist=False)
                    self.assertGreater(panel.read_progress.maximum(), 0)
                    self.assertEqual(panel.read_progress.value(), 0)
                finally:
                    release.set()
                    if panel.job is not None:
                        panel.job.wait(3000)
                    QTest.qWait(20)
                self.assertFalse(panel.busy)
                self.assertTrue(panel.read_progress.isHidden())
                self.assertIn('0 个候选' if failure else '2 个候选', panel.notice.text())

    def test_read_progress_counts_earlier_members_and_ignores_updates_after_cancel(self):
        host = QWidget()
        self.addCleanup(host.close)
        panel = QueryCenterPanel(host)
        panel._service = self.service
        panel.clan = self.service.refresh_clan('asia', '111')
        release = threading.Event()

        def waiting_member_lookup(_clan, _cancel, progress):
            progress('读取成员船仓 2/3 · 示例')
            release.wait(3)
            progress('读取成员船仓 3/3 · 示例')
            return {}

        with patch.object(self.service, 'load_member_ships', side_effect=waiting_member_lookup):
            panel.fetch_fleets()
            try:
                deadline = time.monotonic() + 2
                while panel.read_progress.maximum() != 3 and time.monotonic() < deadline:
                    QTest.qWait(10)
                self.assertEqual(panel.read_progress.maximum(), 3)
                self.assertEqual(panel.read_progress.value(), 1)
                panel.cancel()
                self.assertTrue(panel.read_progress.isHidden())
            finally:
                release.set()
                if panel.job is not None:
                    panel.job.wait(3000)
                QTest.qWait(20)
        self.assertFalse(panel.busy)
        self.assertTrue(panel.read_progress.isHidden())
        self.assertEqual(panel.notice.text(), '查询已取消')

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

    def test_dog_tag_is_cached_and_shared_between_player_favorite_and_snapshot(self):
        image = QImage(32, 32, QImage.Format.Format_ARGB32)
        image.fill(QColor('#327BAA'))
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(image.save(buffer, 'PNG'))
        png = bytes(buffer.data())
        host = QWidget()
        self.addCleanup(host.close)
        panel = QueryCenterPanel(host)
        panel._service = self.service
        target = player_target('asia', '101', 'Demo101')

        def wait_for_query():
            until = time.monotonic() + 8
            while panel.busy and time.monotonic() < until:
                QTest.qWait(12)
            self.assertFalse(panel.busy)

        self.fetch_dog_tag.return_value = [object()]
        with patch('wows_training.desktop.query_center_panel.compose_dog_tag_png', return_value=png):
            panel.open_target(target, query_missing=True)
            wait_for_query()
        self.assertEqual(self.fetch_dog_tag.call_args.args, ('asia', '101'))
        self.assertFalse(panel.player_dog_tag.pixmap().isNull())
        self.assertEqual(self.service.cached_dog_tags((('asia', '101'),)), {('asia', '101'): png})
        panel.bookmark()
        self.assertEqual(panel.favorites_page._records[0]['_dog_tag_png'], png)
        with patch('wows_training.desktop.query_center_panel.SnapshotCardDialog') as dialog:
            panel.open_card()
            self.assertEqual(dialog.call_args.kwargs['dog_tag_png'], png)

        panel.return_to_favorites()
        self.career.save_settings(False)
        self.fetch_dog_tag.reset_mock()
        panel.open_target(target, query_missing=True)
        wait_for_query()
        self.fetch_dog_tag.assert_not_called()
        self.assertFalse(panel.player_dog_tag.pixmap().isNull())

        self.service.clear_history(target)
        self.assertEqual(self.service.cached_dog_tags((('asia', '101'),)), {})
        self.assertEqual(self.service.bookmarks()[0]['id'], '101')

    def test_favorite_back_and_history_clear_use_the_selected_identity(self):
        host = QWidget()
        self.addCleanup(host.close)
        panel = QueryCenterPanel(host)
        panel._service = self.service
        QVBoxLayout(host).addWidget(panel)
        host.resize(1200, 800)
        self.career.refresh('asia', '104')
        host.show()

        def wait_for_query():
            until = time.monotonic() + 8
            while panel.busy and time.monotonic() < until:
                QTest.qWait(12)
            self.assertFalse(panel.busy)

        self.assertIs(panel.stack.currentWidget(), panel.favorites_page)
        self.assertTrue(panel.detail_actions.isHidden())
        panel.text.setText('Demo')
        panel.search()
        wait_for_query()
        player_index = next(index for index in range(panel.candidates.count())
                            if panel.candidates.itemData(index)['kind'] == 'player')
        panel.candidates.setCurrentIndex(player_index)
        panel.open_candidate()
        wait_for_query()
        self.assertEqual(panel.target['id'], '101')
        panel.mark.click()
        self.assertIn('取消收藏', panel.mark.text())
        panel.back_to_favorites.click()
        self.assertIs(panel.stack.currentWidget(), panel.favorites_page)
        self.assertIsNone(panel.target)

        card = panel.favorites_page.findChild(QAbstractButton, 'queryFavoriteCard')
        self.assertIn('Demo101', card.accessibleName())
        self.calls.clear()
        card.click()
        wait_for_query()
        self.assertEqual(panel.target['id'], '101')
        self.assertFalse(self.calls)

        panel.saved.showPopup()
        clear_buttons = panel.saved.findChildren(QToolButton, 'queryHistoryClear')
        clear = next(button for button in clear_buttons if 'Demo101' in button.accessibleName() and button.isVisible())
        clear.click()
        self.assertIs(panel.stack.currentWidget(), panel.favorites_page)
        self.assertEqual([row['id'] for row in self.service.directory()], ['104'])
        self.assertEqual([row['id'] for row in self.service.bookmarks()], ['101'])
        self.assertEqual([panel.candidates.itemData(i)['kind'] for i in range(1, panel.candidates.count())], ['clan'])
        self.assertFalse(self.calls)
        panel.saved.hidePopup()

        card.click()
        wait_for_query()
        self.assertEqual(panel.target['id'], '101')
        self.assertIn('/account/info/', ''.join(self.calls))

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

    def test_confirmed_private_account_has_its_own_placeholder(self):
        host = QWidget()
        self.addCleanup(host.close)
        panel = QueryCenterPanel(host)
        panel._service = self.service
        panel.profile_ai = Mock(busy=False)

        def open_player(account_id):
            panel.open_target(player_target('asia', account_id, 'Fixture'), query_missing=True)
            until = time.monotonic() + 8
            while panel.busy and time.monotonic() < until:
                QTest.qWait(12)
            self.assertFalse(panel.busy)

        open_player('101')
        self.assertEqual(panel.stack.currentIndex(), 1)
        self.assertTrue(panel.radar.keys)
        panel.tabs.setCurrentIndex(2)

        open_player('102')
        self.assertEqual(panel.profile['status'], 'private')
        self.assertIs(panel.stack.currentWidget(), panel.hidden_account_page)
        self.assertEqual(panel.hidden_account.title.text(), '我是隐藏🐴')
        self.assertEqual(panel.hidden_account.identity.text(), 'Private · 亚服')
        self.assertIsNone(panel.detail)
        self.assertFalse(panel.radar.keys)
        self.assertEqual(panel.ship_table.rowCount(), 0)
        self.assertEqual(panel.tabs.currentIndex(), 0)
        self.assertTrue(panel.refresh_button.isEnabled())
        panel.profile_ai.set_source.assert_called_with()

        open_player('103')
        self.assertEqual(panel.profile['status'], 'not_found')
        self.assertEqual(panel.stack.currentIndex(), 0)
        self.assertNotIn('我是隐藏', panel.notice.text())

    def test_hidden_account_lock_reads_shared_stats_into_the_current_view_only(self):
        host = QWidget()
        self.addCleanup(host.close)
        panel = QueryCenterPanel(host)
        panel._service = self.service
        link = 'https://profile.worldofwarships.asia/statistics/102/ac/fixture-code/'
        shared = {'region': 'asia', 'account_id': '102', 'nickname': 'Private', 'modes': {'pvp': self.stats},
                  'ships': [{'ship_id': self.sid, 'modes': {'pvp': self.stats}}], 'achievements': None}

        def wait_for_query():
            until = time.monotonic() + 8
            while panel.busy and time.monotonic() < until:
                QTest.qWait(12)
            self.assertFalse(panel.busy)

        def dialog_controls():
            panel.hidden_account.lock_button.click()
            dialog = panel.findChild(QDialog, 'sharedCareerLinkDialog')
            field = dialog.findChild(QLineEdit, 'sharedCareerLink')
            submit = dialog.findChild(QPushButton, 'primary')
            return field, submit

        panel.open_target(player_target('asia', '102', 'Private'), query_missing=True)
        wait_for_query()
        before = self.career.store.list_runs('career_snapshot')
        with patch('wows_training.desktop.query_center_panel.load_shared_profile', return_value=shared) as load:
            field, submit = dialog_controls()
            self.assertEqual(field.echoMode(), QLineEdit.EchoMode.Password)
            self.assertFalse(submit.isEnabled())
            field.setText(link.replace('/102/', '/101/'))
            submit.click()
            load.assert_not_called()
            field.setText(link)
            submit.click()
            wait_for_query()
            self.assertEqual(load.call_args.args, (link, 'asia', '102'))
        self.assertEqual(panel.shared_mode.currentData(), 'pvp')
        self.assertEqual(panel.shared_mode_stats.item(0, 1).text(), '5')
        self.assertEqual(panel.shared_ship_stats.rowCount(), 1)
        self.assertFalse(panel.shared_view.isHidden())
        self.assertEqual(panel.profile['status'], 'private')
        self.assertEqual(self.career.store.list_runs('career_snapshot'), before)

        with patch('wows_training.desktop.query_center_panel.load_shared_profile',
                   side_effect=SharedProfileError('官方未向此链接开放战绩。')):
            field, submit = dialog_controls()
            field.setText(link)
            submit.click()
            wait_for_query()
        self.assertIn('未向此链接开放', panel.notice.text())
        self.assertNotIn('fixture-code', panel.notice.text())
        panel.open_target(player_target('asia', '103', 'Another player'), query_missing=True)
        wait_for_query()
        self.assertIsNone(panel._shared_profile)
        self.assertTrue(panel.shared_view.isHidden())

    def test_refresh_button_fetches_only_the_current_players_regional_leaderboard(self):
        host = QWidget()
        self.addCleanup(host.close)
        panel = QueryCenterPanel(host)
        panel._service = self.service
        self.career.refresh('asia', '101')
        feed = {'schema_version': 1, 'source': 'wows-numbers', 'mode': 'pvp', 'region': 'asia',
                'order': 'pr_desc', 'observed_on': '2025-11-25', 'ships': {}}

        def wait_for_refresh():
            until = time.monotonic() + 8
            while panel.busy and time.monotonic() < until:
                QTest.qWait(12)
            self.assertFalse(panel.busy)

        with patch.object(self.career, 'numbers_transport', return_value=feed) as fetch:
            panel.open_target(player_target('asia', '101', 'Demo101'))
            wait_for_refresh()
            fetch.assert_not_called()
            panel.refresh_button.click()
            wait_for_refresh()
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.args[0], 'asia')
            self.assertIn('2025-11-25', panel.notice.text())

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
