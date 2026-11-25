"""Exercise medal-card interaction in the actual gallery layout."""

import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QAbstractAnimation, QEvent, QPoint, Qt
from PySide6.QtGui import QTextLayout
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QBoxLayout, QLabel, QVBoxLayout, QWidget

from wows_training.desktop.achievement_gallery import AchievementGallery, earned_achievements
from wows_training.desktop.achievement_rarity import rarity_style
from wows_training.desktop.achievement_wordmark import RarityWordmark
from wows_training.desktop.motion import MotionController
from wows_training.desktop.presentation_fonts import load_presentation_fonts
from wows_training.f1_ratings.challenges import CHALLENGES


class AchievementGalleryMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.window = QWidget()
        self.window.resize(820, 600)
        self.motion = MotionController(self.window, Path(self.folder.name))
        layout = QVBoxLayout(self.window)
        self.gallery = AchievementGallery()
        self.gallery.set_detail({"achievement_gallery": [
            {"name": "摧枯拉朽", "group": "record", "status": "earned", "requirement": "单场击沉至少 8 艘",
             "description": "会战兵力是11舰对4舰，优势在我"},
            {"name": "全家福", "group": "fleet", "status": "earned", "requirement": "航母与潜艇参战占比达到 60%",
             "description": "浮木刚用完，等会去打瓦进点货"},
        ]}, ("asia", "fixture"))
        layout.addWidget(self.gallery)
        self.window.show()
        self.app.processEvents()
        self.card, self.neighbor = [card for _, card in self.gallery._cards]
        self.title = next(label for label in self.card.findChildren(QLabel) if label.text() == "摧枯拉朽")
        self.card._finish()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.folder.cleanup()

    def advance(self, fraction):
        animation = self.card._hover
        animation.pause()
        animation.setCurrentTime(round(fraction * animation.duration()))
        self.app.processEvents()

    def edge(self):
        image = self.card.grab().toImage()
        return image.copy(0, 0, image.width(), round(12 * image.devicePixelRatio()))

    def title_y(self):
        return self.title.mapTo(self.card, QPoint()).y()

    def test_hover_raises_the_surface_and_fades_back_without_moving_the_grid(self):
        resting_edge, resting_y = self.edge(), self.title_y()
        positions = self.card.geometry(), self.neighbor.geometry()
        self.app.sendEvent(self.card, QEvent(QEvent.Type.Enter))
        self.advance(.4)
        self.assertGreater(self.card.hover_progress, 0)
        self.assertLess(self.card.hover_progress, 1)
        self.assertLess(self.title_y(), resting_y)
        self.assertNotEqual(self.edge(), resting_edge)
        self.assertEqual(positions, (self.card.geometry(), self.neighbor.geometry()))

        raised_progress = self.card.hover_progress
        self.app.sendEvent(self.card, QEvent(QEvent.Type.Leave))
        self.assertAlmostEqual(self.card.hover_progress, raised_progress)
        self.advance(.4)
        self.assertLess(self.card.hover_progress, raised_progress)
        self.app.sendEvent(self.card, QEvent(QEvent.Type.Enter))
        self.advance(1)
        self.app.sendEvent(self.card, QEvent(QEvent.Type.Leave))
        self.advance(.4)
        self.assertGreater(self.card.hover_progress, 0)
        self.assertLess(self.card.hover_progress, 1)
        self.advance(1)
        self.assertEqual(self.title_y(), resting_y)
        self.assertEqual(self.edge(), resting_edge)

    def test_filter_and_motion_preference_stop_the_hover_transition(self):
        self.app.sendEvent(self.card, QEvent(QEvent.Type.Enter))
        self.advance(.4)
        self.gallery.group.setCurrentIndex(self.gallery.group.findData("fleet"))
        self.assertFalse(self.card.isVisible())
        self.assertEqual(self.card._hover.state(), QAbstractAnimation.State.Stopped)
        self.assertEqual(self.card.hover_progress, 0)
        self.gallery.group.setCurrentIndex(0)
        self.app.sendEvent(self.card, QEvent(QEvent.Type.Enter))
        self.advance(.4)
        self.motion.set_enabled(False, persist=False)
        self.app.sendEvent(self.card, QEvent(QEvent.Type.Enter))
        self.assertEqual(self.card._hover.state(), QAbstractAnimation.State.Stopped)
        self.assertEqual(self.card.hover_progress, 0)

    def test_six_grades_have_progressive_hover_depth_and_stable_earned_cards(self):
        grades = ["普通", "罕见", "稀有", "史诗", "传奇", "神话"]
        english = ["COMMON", "UNCOMMON", "RARE", "EPIC", "LEGENDARY", "MYTHIC"]
        rows = [dict(name=f"测试勋章{i}", rarity=grade, value=88, requirement="3 ≤ 单场击沉 ≤ 6",
                     is_upper_bound=i % 2, is_lower_bound=1 - i % 2, status="earned")
                for i, grade in enumerate(grades)]
        rows.append(dict(name="未达成", status="pending", rarity="神话"))
        self.gallery.set_detail({"achievement_gallery": rows}, ("asia", "six"))
        self.app.processEvents()
        QTest.mouseMove(self.gallery.banner, self.gallery.banner.rect().center())
        self.app.processEvents()
        cards = [card for _, card in self.gallery._cards]
        self.assertEqual([card.rarity.label for card in cards], grades[::-1])
        self.assertEqual(len({card.rarity.accent for card in cards}), 6)
        for rank, card in enumerate(reversed(cards)):
            with self.subTest(rank=rank):
                card._finish()
                card.layout().activate()
                wordmark = card.findChild(RarityWordmark)
                self.assertEqual(wordmark.text(), english[rank])
                self.assertTrue(card.rect().contains(wordmark.mapTo(card, wordmark.rect().bottomRight())))
                title = card.findChild(QLabel, "achievementPreviewTitle")
                measure = card.findChild(QLabel, "achievementDetailedValue")
                rule = card.findChild(QLabel, "achievementRequirement")
                self.assertEqual(measure.text(), "≤ 88" if rank % 2 else "≥ 88")
                self.assertFalse(rule.isVisible() or measure.isVisible())
                for label in (rule, measure):
                    text_layout = QTextLayout(label.text(), label.font())
                    text_layout.beginLayout()
                    text_layout.createLine()
                    text_layout.endLayout()
                    self.assertTrue(text_layout.glyphRuns())
                    for run in text_layout.glyphRuns():
                        self.assertNotIn(0, run.glyphIndexes(), label.text())
                resting = card.grab().toImage()
                geometry = card.geometry()
                origin = title.mapTo(card, QPoint())
                card._frame(1.)
                self.app.processEvents()
                self.assertEqual(card.geometry(), geometry)
                self.assertEqual(origin.y() - title.mapTo(card, QPoint()).y(), rank + 1)
                self.assertNotEqual(card.grab().toImage(), resting)
                self.assertEqual(card.medal.progress, 1.)
                card._finish()
                self.app.processEvents()
                self.assertEqual(card.grab().toImage(), resting)
        self.assertEqual(rarity_style("未知").label, grades[0])

    def test_click_expands_description_and_pushes_neighbours_down(self):
        self.assertFalse(self.card.toolTip())
        self.assertFalse(self.card.description.isVisible())
        old_height = self.card.height()
        old_y = self.neighbor.y()
        QTest.mouseClick(self.card, Qt.MouseButton.LeftButton, pos=self.card.rect().center())
        self.app.processEvents()
        self.assertTrue(self.card._expanded)
        card_index = self.gallery.cards.indexOf(self.card)
        self.assertEqual(self.gallery.cards.getItemPosition(card_index)[3], self.gallery._column_count())
        self.assertEqual(self.card._detail_animation.state(), QAbstractAnimation.State.Running)
        self.card._detail_animation.setCurrentTime(self.card._detail_animation.duration())
        self.app.processEvents()
        self.assertGreater(self.card.height(), old_height)
        self.assertGreater(self.neighbor.y(), old_y)
        self.assertEqual(self.card.description.text(), "会战兵力是11舰对4舰，优势在我")
        self.assertIn(load_presentation_fonts()["chinese_display"], self.card.description.styleSheet())
        self.assertTrue(any(label.text() == "成就致辞" and label.isVisible()
                            for label in self.card.findChildren(QLabel)))
        self.assertTrue(self.card.description.isVisible())
        self.assertFalse(self.neighbor._expanded)

        QTest.keyClick(self.neighbor, Qt.Key.Key_Space)
        self.app.processEvents()
        self.assertFalse(self.card._expanded)
        self.assertFalse(self.card.description.isVisible())
        self.assertTrue(self.neighbor._expanded)
        QTest.keyClick(self.neighbor, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertFalse(self.neighbor._expanded)
        self.assertEqual(self.card.y(), self.neighbor.y())

    def test_preview_hides_requirements_and_measurements_until_detail_reflows(self):
        self.window.resize(1120, 670)
        self.motion.set_enabled(False, persist=False)
        self.gallery.set_detail({"achievement_gallery": [
            {"id": "battle_damage", "name": "创造模式", "status": "earned", "value": 403631, "unit": "点",
             "requirement": "随机战最高单场造成伤害 ≥ 400,000 点", "description": "我说终于赢了，你说真刷爽了"},
        ]}, ("asia", "compact-details"))
        self.app.processEvents()
        card = self.gallery._cards[0][1]
        rule = card.findChild(QLabel, "achievementRequirement")
        result = card.findChild(QLabel, "achievementDetailedValue")
        self.assertEqual(result.text(), "403,631点")
        self.assertFalse(rule.isVisible() or result.isVisible())
        self.assertNotIn("400,000", card.accessibleDescription())
        self.assertNotIn("403,631", card.accessibleDescription())
        QTest.keyClick(card, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertTrue(rule.isVisible() and result.isVisible() and card.description.isVisible())
        self.assertEqual(card._detail_facts.direction(), QBoxLayout.Direction.LeftToRight)
        self.assertIn("400,000", card.accessibleDescription())
        self.window.resize(420, 620)
        self.app.processEvents()
        self.assertEqual(card._detail_facts.direction(), QBoxLayout.Direction.TopToBottom)
        self.assertGreater(result.mapTo(card, QPoint()).y(), rule.mapTo(card, QPoint()).y())
        QTest.keyClick(card, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertFalse(rule.isVisible() or result.isVisible())

    def test_expansion_respects_motion_preference_and_category_changes(self):
        self.motion.set_enabled(False, persist=False)
        self.window.resize(420, 600)
        self.app.processEvents()
        self.assertEqual(self.gallery._column_count(), 1)
        QTest.keyClick(self.card, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(self.card._detail_animation.state(), QAbstractAnimation.State.Stopped)
        self.assertTrue(self.card.description.isVisible())
        self.gallery.group.setCurrentIndex(self.gallery.group.findData("fleet"))
        self.app.processEvents()
        self.assertFalse(self.card._expanded)
        self.assertFalse(self.card.isVisible())
        self.assertIsNone(self.gallery._expanded_id)

    def test_earned_ship_names_replace_the_number_in_the_same_style(self):
        row = {"id": "ship_damage_114514", "name": "野兽先辈", "status": "earned", "rarity": "传奇",
               "value": 114514, "unit": "点", "requirement": "随机战场均伤害显示为 114,514 点",
               "matched_ships": ["玛索", "得梅因"]}
        comparison = {"name": "对照成就", "status": "earned", "rarity": "普通", "value": 7, "unit": "点"}
        self.gallery.set_detail({"achievement_gallery": [row, comparison]}, ("asia", "ship-match"))
        self.app.processEvents()
        card, other = [card for _, card in self.gallery._cards]
        label = card.findChild(QLabel, "achievementDetailedValue")
        numeric = other.findChild(QLabel, "achievementDetailedValue")
        self.assertEqual(label.text(), "玛索、得梅因")
        self.assertEqual(label.font(), numeric.font())
        self.assertFalse(any(label.text().replace(" ", "") == "114,514点" for label in card.findChildren(QLabel)))
        self.assertNotIn("玛索、得梅因", card.accessibleDescription())
        QTest.keyClick(card, Qt.Key.Key_Return)
        self.assertIn("玛索、得梅因", card.accessibleDescription())
        self.assertEqual(card.rarity.label, "传奇")

    def test_upper_tier_ornament_moves_only_while_hovering_with_motion_enabled(self):
        self.gallery.set_detail({"achievement_gallery": [
            {"name": "测试神话", "rarity": "神话", "status": "earned"},
            {"name": "测试普通", "rarity": "普通", "status": "earned"},
        ]}, ("asia", "ornament"))
        self.app.processEvents()
        high, common = [card for _, card in self.gallery._cards]
        self.assertIsNone(common._ornament)
        self.app.sendEvent(high, QEvent(QEvent.Type.Enter))
        self.assertEqual(high._ornament.state(), QAbstractAnimation.State.Running)
        high._hover.stop()
        high._frame(1.)
        high._ornament.setCurrentTime(250)
        first = high.grab().toImage()
        high._ornament.setCurrentTime(950)
        self.assertNotEqual(high.grab().toImage(), first)
        self.app.sendEvent(high, QEvent(QEvent.Type.Leave))
        self.assertEqual(high._ornament.state(), QAbstractAnimation.State.Stopped)
        self.app.sendEvent(high, QEvent(QEvent.Type.Enter))
        self.motion.set_enabled(False, persist=False)
        self.assertEqual(high._ornament.state(), QAbstractAnimation.State.Stopped)
        self.assertEqual(high._ornament_phase, 0.)

    def test_background_preference_removes_ornamental_traces_and_preserves_labels(self):
        self.window.client_preferences = {"background_enabled": False}
        self.gallery.set_detail({"achievement_gallery": [dict(name="测试神话", rarity="神话",
                                status="earned", value=100, is_upper_bound=True)]}, ("asia", "test"))
        self.app.processEvents()
        card = self.gallery._cards[0][1]
        self.assertFalse(card._background_enabled)
        card._frame(1.)
        self.app.processEvents()
        label = card.findChild(QLabel, "achievementPreviewTitle")
        plain, text = card.grab().toImage(), label.grab().toImage()
        card.set_background_enabled(True)
        self.assertNotEqual(card.grab().toImage(), plain)
        self.assertEqual(label.grab().toImage(), text)

    def test_saved_and_legacy_rows_use_current_catalogue_grade(self):
        challenge = next(item for item in CHALLENGES if item.id == "battle_frags")
        old_grade = "普通" if challenge.rarity != "普通" else "神话"
        records = earned_achievements({"achievement_gallery": [
            dict(id=challenge.id, name=challenge.name, status="earned", rarity=old_grade),
            dict(id="other-source", name=challenge.name, status="earned", rarity=old_grade),
        ]})
        self.assertEqual([row["rarity"] for row in records], [challenge.rarity, old_grade])
        legacy = earned_achievements({"challenges": [(challenge.name, challenge.requirement)]})
        self.assertEqual((legacy[0]["id"], legacy[0]["rarity"]), (challenge.id, challenge.rarity))

    def test_texture_follows_real_pointer_moves_over_the_card_and_labels(self):
        card = self.card
        QTest.mouseMove(self.neighbor, self.neighbor.rect().center())
        card._finish()
        first = QPoint(card.width()-35, card.height()-28)
        QTest.mouseMove(card, first)
        self.advance(1)
        card._texture_motion.setCurrentTime(card._texture_motion.duration())
        self.assertEqual(card._texture_pointer.toPoint(), first)
        first_image = card.grab().toImage()

        QTest.mouseMove(self.title, self.title.rect().center())
        self.app.processEvents()
        target = self.title.mapTo(card, self.title.rect().center())
        self.assertEqual(card._texture_motion.endValue().toPoint(), target)
        card._texture_motion.pause()
        card._texture_motion.setCurrentTime(0)
        self.assertEqual(card._texture_pointer.toPoint(), first)
        card._texture_motion.setCurrentTime(card._texture_motion.duration())
        self.assertEqual(card._texture_pointer.toPoint(), target)
        self.assertEqual(card.hover_progress, 1.)
        self.assertNotEqual(card.grab().toImage(), first_image)

        QTest.mouseMove(card, first)
        self.motion.set_enabled(False, persist=False)
        self.assertIsNone(card._texture_pointer)
        self.assertEqual(card._texture_motion.state(), QAbstractAnimation.State.Stopped)
        self.motion.set_enabled(True, persist=False)
        card.set_background_enabled(False)
        QTest.mouseMove(self.title, self.title.rect().center())
        self.assertIsNone(card._texture_pointer)
        self.assertEqual(card._texture_motion.state(), QAbstractAnimation.State.Stopped)
