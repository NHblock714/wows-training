"""Exercise insignia composition with local PNG fixtures."""

import unittest
from types import SimpleNamespace

from PySide6.QtCore import QBuffer, QIODevice, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from wows_training.desktop.dog_tag_visual import compose_dog_tag_png, dog_tag_image, paint_dog_tag


def fixture_png(width, height, *, fill=Qt.GlobalColor.transparent, detail=None):
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(fill)
    if detail:
        painter = QPainter(image)
        painter.fillRect(*detail)
        painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    return bytes(buffer.data())


def layer(role, image, color=None, position_y=0.):
    return SimpleNamespace(role=role, image=image, color=color, position_y=position_y)


class DogTagVisualTests(unittest.TestCase):
    def test_layers_paint_background_then_border_then_symbol(self):
        layers = (
            layer('background', fixture_png(16, 16, fill=QColor('#1800cc'))),
            layer('border', fixture_png(16, 16, detail=(QRect(4, 4, 8, 8), QColor('#ee6600')))),
            layer('symbol', fixture_png(16, 16, detail=(QRect(7, 7, 2, 2), QColor('#f8ffff')))),
        )
        image = dog_tag_image(compose_dog_tag_png(layers))
        self.assertEqual(image.pixelColor(32, 32).name(), '#1800cc')
        self.assertEqual(image.pixelColor(90, 90).name(), '#ee6600')
        self.assertEqual(image.pixelColor(128, 128).name(), '#f8ffff')

    def test_tint_respects_texture_alpha_and_darkens_the_background_only(self):
        texture = fixture_png(16, 16, detail=(QRect(4, 4, 8, 8), QColor(128, 128, 128, 128)))
        symbol = layer('symbol', fixture_png(16, 16, detail=(QRect(0, 0, 2, 2), QColor('#ffffff'))))
        background = dog_tag_image(compose_dog_tag_png((layer('background', texture, '#80d0f0'), symbol)))
        border = dog_tag_image(compose_dog_tag_png((layer('border', texture, '#80d0f0'), symbol)))
        for image in (background, border):
            self.assertEqual(image.pixelColor(250, 250).alpha(), 0)
            self.assertEqual(image.pixelColor(128, 128).alpha(), 128)
            self.assertGreater(image.pixelColor(128, 128).blue(), image.pixelColor(128, 128).red())
        self.assertLess(background.pixelColor(128, 128).red(), border.pixelColor(128, 128).red())

    def test_clan_layout_shrinks_and_anchors_symbol(self):
        image = fixture_png(16, 16, fill=QColor('white'))
        top = dog_tag_image(compose_dog_tag_png((layer('symbol', image, position_y=-1.),)))
        bottom = dog_tag_image(compose_dog_tag_png((layer('symbol', image, position_y=1.),)))
        self.assertEqual(top.pixelColor(128, 20).alpha(), 255)
        self.assertEqual(top.pixelColor(128, 246).alpha(), 0)
        self.assertEqual(bottom.pixelColor(128, 20).alpha(), 0)
        self.assertEqual(bottom.pixelColor(128, 246).alpha(), 255)

    def test_paint_containment_and_bounded_png_input(self):
        source = fixture_png(16, 8, fill=QColor('red'))
        target = QImage(36, 36, QImage.Format.Format_ARGB32_Premultiplied)
        target.fill(Qt.GlobalColor.transparent)
        painter = QPainter(target)
        self.assertTrue(paint_dog_tag(painter, QRectF(4, 4, 24, 24), source))
        self.assertFalse(paint_dog_tag(painter, QRectF(0, 0, 0, 20), source))
        painter.end()
        self.assertEqual(target.pixelColor(16, 7).alpha(), 0)
        self.assertEqual(target.pixelColor(16, 16).name(), '#ff0000')
        self.assertEqual(target.pixelColor(30, 16).alpha(), 0)
        self.assertTrue(dog_tag_image(fixture_png(513, 1)).isNull())
        self.assertTrue(dog_tag_image(b'NOT PNG').isNull())
        self.assertIsNone(compose_dog_tag_png((layer('symbol', b'NOT PNG'),)))
        self.assertIsNone(compose_dog_tag_png((layer('border', source),)))
        self.assertIsNone(compose_dog_tag_png((layer('symbol', source), layer('background', source))))
