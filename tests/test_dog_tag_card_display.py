"""Player insignia surfaces use the cached PNG without disturbing account identity."""

import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QWidget

from wows_training.desktop.query_favorites import QueryFavorites
from wows_training.desktop.query_visuals import SnapshotCardDialog, snapshot_card


def sample_png(color):
    image = QImage(60, 30, QImage.Format.Format_ARGB32)
    image.fill(color)
    data = QBuffer()
    data.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(data, 'PNG'):
        raise RuntimeError('Could not generate an insignia fixture')
    return bytes(data.data())


class DogTagCardDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.color = QColor('#DD2F70')
        cls.png = sample_png(cls.color)

    def test_favorite_draws_tag_without_emitting_binary_data(self):
        favorites = QueryFavorites()
        self.addCleanup(favorites.close)
        identity = {'kind': 'player', 'region': 'asia', 'id': '101', 'name': 'AA101'}
        favorites.set_records([dict(identity, _dog_tag_png=self.png)])
        favorites.resize(520, 200)
        favorites.show()
        self.app.processEvents()
        card = favorites._cards[0]
        image = card.grab().toImage()
        scale = image.devicePixelRatio()
        self.assertEqual(image.pixelColor(round(35 * scale), round(46 * scale)), self.color)
        events = []
        favorites.targetOpened.connect(events.append)
        card.click()
        self.assertEqual(events, [identity])

    def test_snapshot_preview_and_export_share_the_right_side_tag(self):
        profile = {'region': 'asia', 'nickname': 'AA101'}
        baseline = snapshot_card(profile, {}, background_enabled=False)
        self.assertEqual(snapshot_card(profile, {}, background_enabled=False, dog_tag_png=b'bad PNG'), baseline)
        rendered = snapshot_card(profile, {}, background_enabled=False, dog_tag_png=self.png)
        self.assertEqual(rendered.pixelColor(999, 86), self.color)
        self.assertEqual(rendered.copy(0, 0, 794, 145), baseline.copy(0, 0, 794, 145))
        self.assertEqual(rendered.copy(0, 146, 1100, rendered.height() - 146),
                         baseline.copy(0, 146, 1100, baseline.height() - 146))
        host = QWidget()
        self.addCleanup(host.close)
        host.client_preferences = {'background_enabled': False}
        dialog = SnapshotCardDialog(profile, {}, host, dog_tag_png=self.png)
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.image, rendered)
