"""Render the official player insignia layers on a transparent square."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..dog_tags import DogTagLayer


_CANVAS_SIDE = 256
_MAX_PNG_BYTES = 2 * 1024 * 1024
_MAX_IMAGE_SIDE = 512
_CLAN_TAG_FRACTION = 30 / 190
_PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
_COLOR = re.compile(r'#[0-9a-fA-F]{6}')
_ROLE_ORDER = {'background': 0, 'border': 1, 'symbol': 2}


def _bounded_image(image: QImage) -> bool:
    return not image.isNull() and max(image.width(), image.height()) <= _MAX_IMAGE_SIDE


def dog_tag_image(png_or_image: bytes | QImage | None) -> QImage:
    """Decode a bounded PNG, or return a null image when no usable image is available."""
    if isinstance(png_or_image, QImage):
        return png_or_image if _bounded_image(png_or_image) else QImage()
    if (not isinstance(png_or_image, bytes) or len(png_or_image) > _MAX_PNG_BYTES
            or not png_or_image.startswith(_PNG_SIGNATURE)):
        return QImage()
    buffer = QBuffer()
    buffer.setData(QByteArray(png_or_image))
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        return QImage()
    reader = QImageReader(buffer, b'png')
    size = reader.size()
    if size.isEmpty() or max(size.width(), size.height()) > _MAX_IMAGE_SIDE:
        return QImage()
    image = reader.read()
    return image if _bounded_image(image) else QImage()


def _contain(rect: QRectF, image: QImage) -> QRectF:
    scale = min(rect.width() / image.width(), rect.height() / image.height())
    fitted = QRectF(0, 0, image.width() * scale, image.height() * scale)
    fitted.moveCenter(rect.center())
    return fitted


def paint_dog_tag(painter: QPainter, rect: QRectF, png_or_image: bytes | QImage | None) -> bool:
    """Paint an insignia centered in rect while preserving aspect ratio and alpha."""
    rect = QRectF(rect)
    image = dog_tag_image(png_or_image)
    if rect.isEmpty() or image.isNull():
        return False
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawImage(_contain(rect, image), image)
    painter.restore()
    return True


def _canvas() -> QImage:
    image = QImage(_CANVAS_SIDE, _CANVAS_SIDE, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    return image


def _colorize(mask: QImage, color: QColor, *, background: bool) -> QImage:
    # The official profile paints the texture with overlay blending on a
    # solid color, then masks the resulting layer with the texture's alpha.
    texture = mask
    if background:
        # CSS brightness(.7) applies to the background texture, not its mask.
        texture = mask.copy()
        painter = QPainter(texture)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
        painter.fillRect(texture.rect(), QColor.fromRgbF(0, 0, 0, .3))
        painter.end()
    result = _canvas()
    result.fill(color)
    painter = QPainter(result)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Overlay)
    painter.drawImage(QPoint(), texture)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    painter.drawImage(QPoint(), mask)
    painter.end()
    return result


def compose_dog_tag_png(layers: Sequence[DogTagLayer] | None) -> bytes | None:
    """Composite the provider's ordered background, border and symbol layers."""
    if not layers or len(layers) > len(_ROLE_ORDER):
        return None
    roles = [_ROLE_ORDER.get(layer.role, -1) for layer in layers]
    if roles[0] < 0 or roles[-1] != _ROLE_ORDER['symbol'] or roles != sorted(set(roles)):
        return None

    rendered = []
    for layer in layers:
        area = QRectF(0, 0, _CANVAS_SIDE, _CANVAS_SIDE)
        if layer.role == 'symbol' and layer.position_y:
            inset = _CANVAS_SIDE * _CLAN_TAG_FRACTION
            area.adjust(0, inset if layer.position_y > 0 else 0, 0, -inset if layer.position_y < 0 else 0)
        image = dog_tag_image(layer.image)
        if image.isNull():
            return None
        texture = _canvas()
        painter = QPainter(texture)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(_contain(area, image), image)
        painter.end()
        if layer.color is not None:
            if layer.role == 'symbol' or not isinstance(layer.color, str) or not _COLOR.fullmatch(layer.color):
                return None
            texture = _colorize(texture, QColor(layer.color), background=layer.role == 'background')
        rendered.append(texture)

    result = _canvas()
    painter = QPainter(result)
    for texture in rendered:
        painter.drawImage(QPoint(), texture)
    painter.end()
    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not result.save(buffer, 'PNG'):
        return None
    return bytes(buffer.data())
