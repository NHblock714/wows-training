"""Exercise medal-card interaction in the actual gallery layout."""

import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QAbstractAnimation, QEvent, QPoint
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from wows_training.desktop.achievement_gallery import AchievementGallery
from wows_training.desktop.motion import MotionController


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
            {"name": "摧枯拉朽", "group": "record", "status": "earned", "requirement": "单场击沉至少 8 艘"},
            {"name": "全家福", "group": "fleet", "status": "earned", "requirement": "航母与潜艇参战占比达到 60%"},
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
