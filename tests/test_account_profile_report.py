"""Career portrait stays snapshot-scoped and readable across window widths."""
import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from wows_training.ai_analysis import digest
from wows_training.desktop.account_profile_ai import _matching_portrait
from wows_training.desktop.account_profile_report import PortraitView, _combat_readings, portrait_content


def _current_payload():
    return {
        'request': {'report_contract':'career_profile_v2', 'evidence':[
            {'kind':'career_mode_summaries', 'details':{'support':[
                {'mode':mode, 'count':count, 'status':status} for mode, count, status in (
                    ('pvp',11658,'known'), ('pvp_solo',3000,'known'), ('rank_solo',20,'known'),
                    ('oper_solo',20,'known'), ('oper_div',8,'known'), ('pve',8,'known'), ('club',None,'unknown'),
                )
            ]}},
            {'kind':'career_ship_classes', 'details':{'mode':'pvp', 'support':[{'label':'巡洋舰','share':.75}]}},
            {'kind':'career_server_comparison', 'details':{'status':'available', 'mode':'pvp', 'support':[
                {'metric':'avg_damage','value':63100,'reference_value':53800,'count':9000,'denominator':11658}]}},
        ]},
        'interpretations': [{'text':'公开生涯以十级巡洋舰为主。'}],
        'observations': [{'text':text} for text in (
            '巡洋舰占逐舰样本场次的四分之三。得梅因是常用舰。' * 3,
            '在当前量化范围内，输出表现相对突出。' * 3,
            '<b>高等级随机战</b>构成大量实际参战记录。' * 3,
        )],
        'suggestions': [],
    }


class CareerPortraitReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_current_report_has_ordered_cards_and_only_comparable_metric_chips(self):
        payload = _current_payload()
        conclusion, sections, chips = portrait_content(payload)
        self.assertEqual(conclusion, '公开生涯以十级巡洋舰为主。')
        self.assertEqual([label for label, _ in sections], ['舰船偏好', '游玩风格', '游戏经验'])
        self.assertEqual(chips[0], ('参战模式', '4 类', '随机战 · 排位 · 行动 · 联合'))
        self.assertEqual(chips[-1], ('随机战同舰场均伤害', '63,100', '参考 53,800 · 9,000/11,658 场'))
        payload['request']['evidence'][-1]['details']['status'] = 'unknown'
        self.assertEqual(len(portrait_content(payload)[2]), 2)

    def test_combat_readings_keep_mode_and_actual_damage_distinct(self):
        payload = _current_payload()
        request = payload['request']
        metrics = [
            {'metric': 'main_battery_hit_rate', 'value': .348},
            {'metric': 'avg_potential_damage', 'value': 820000},
            {'metric': 'survival_rate', 'value': .452},
            {'metric': 'avg_scouting_damage', 'value': 26528},
        ]
        request['evidence'].append({'kind': 'career_combat_style', 'details': {'support': [
            {'mode': 'pvp_solo', 'label': '单排', 'count': 4000, 'support': metrics},
            {'mode': 'pvp', 'label': '随机战', 'count': 5000, 'support': metrics},
        ]}})
        self.assertEqual(_combat_readings(request), ('随机战', [
            ('主炮命中率', '34.8%', .348), ('存活率', '45.2%', .452), ('场均侦察伤害', '26,528', None)]))
        metrics.append({'metric': 'damage_received_to_dealt_ratio', 'value': .84})
        self.assertEqual(_combat_readings(request)[1][1], ('实际承伤 ÷ 造伤', '0.84', None))

    def test_legacy_adapter_keeps_findings_and_drops_only_fixed_displays(self):
        note = '胜率和 PR 是单排与分队的合计表现，不能直接视为单排或排位水平。'
        report = {'request':{'report_contract':'career_profile_v1'},
                  'interpretations':[{'text':f'偏好高阶巡洋舰。{note}'}, {'text':'资料边界。'}],
                  'observations':[{'text':'常用十级。'}], 'suggestions':[{'text':'可关注的方向。'}]}
        conclusion, sections, _ = portrait_content(report)
        self.assertEqual(conclusion, '偏好高阶巡洋舰。')
        self.assertEqual(sections, [('偏好与游玩特征', '常用十级。')])
        self.assertIn(note, report['interpretations'][0]['text'])

    def test_matching_old_request_requires_exact_account_snapshot_and_complete_rows(self):
        rows = [dict(id='P01', kind='career_snapshot_scope', label='资料范围', text='摘要', details={}),
                dict(id='P02', kind='career_random_summary', label='随机战生涯', text='随机战', details={})]
        identity = ['asia','101','snapshot-a']
        legacy = {'kind':'career_profile','report_contract':'career_profile_v1','source_name':'公开账号资料','evidence':rows}
        run = {'report_context':{'source_fingerprint':digest([*identity, legacy])},
               'request':{'report_contract':'career_profile_v1','evidence':rows}}
        self.assertTrue(_matching_portrait(run, identity, digest(identity)))
        for other in (['asia','102','snapshot-a'], ['asia','101','snapshot-b'], ['na','101','snapshot-a']):
            self.assertFalse(_matching_portrait(run, other, digest(other)))
        self.assertFalse(_matching_portrait(run, identity, None))
        run['request']['evidence'] = rows[:1]
        self.assertFalse(_matching_portrait(run, identity, digest(identity)))

    def test_cards_reflow_without_clipping_and_model_markup_remains_plain_text(self):
        view = PortraitView()
        self.addCleanup(view.close)
        view.show()
        view.show_report(_current_payload())
        for width, columns in ((1080, 2),(640, 1),(784, 2)):
            view.resize(width, 650)
            QTest.qWait(15)
            self.assertEqual(view._columns, columns)
            self.assertTrue(view.hero.isVisible())
            self.assertFalse(view.grab().isNull())
            for panel, _, body in view._panels:
                self.assertTrue(panel.contentsRect().contains(body.geometry()))
                self.assertGreaterEqual(body.height(), body.heightForWidth(body.width()))
        body = view._panels[2][2]
        self.assertEqual(body.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn('<b>', body.text())


if __name__ == '__main__':
    unittest.main()
