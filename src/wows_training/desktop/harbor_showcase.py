"""Static harbor artwork behind the account instruments and exported card."""
from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter

from .naval_assets import default_naval_assets


class HarborShowcase:
    """Cache one fitted and tinted raster for the current canvas size."""

    def __init__(self, *, instrument=False):
        try:
            self.art = default_naval_assets().background('harbor-account')
        except (OSError, ValueError):
            self.art = None
        self._size = ()
        self._raster = None
        self.instrument = instrument

    def paint(self, painter, target, clip=None):
        target = QRectF(target)
        if self.art is None or self.art.isNull() or target.isEmpty():
            return False
        dpr = painter.device().devicePixelRatioF()
        scale = min(dpr, math.sqrt(1_600_000 / (target.width() * target.height())))
        size = (max(1, round(target.width() * scale)), max(1, round(target.height() * scale)))
        if size != self._size:
            self._raster = self._compose(*size)
            self._size = size
        painter.save()
        if clip is not None:
            painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
        painter.drawImage(target, self._raster)
        painter.restore()
        return True

    def _compose(self, width, height):
        raster = QImage(width, height, QImage.Format.Format_RGB32)
        raster.fill(QColor('#D6E7F5' if self.instrument else '#EAF3FA'))
        bounds = QRectF(0, 0, width, height)
        scale = max(width / self.art.width(), height / self.art.height())
        source_width, source_height = width / scale, height / scale
        crop = QRectF((self.art.width() - source_width) * .78, (self.art.height() - source_height) * .52,
                      source_width, source_height)
        painter = QPainter(raster)
        try:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(bounds, self.art, crop)
            wash = QLinearGradient(0, height * .15, width, height * .78)
            stops = ((0, (231, 243, 253, 229)), (.38, (208, 228, 245, 208)),
                     (.72, (187, 215, 237, 178)), (1, (174, 193, 220, 151))) if self.instrument else (
                     (0, (252, 254, 255, 249)), (.40, (244, 251, 255, 224)),
                     (.71, (235, 247, 255, 190)), (1, (231, 239, 253, 137)))
            for offset, color in stops:
                wash.setColorAt(offset, QColor(*color))
            painter.fillRect(bounds, wash)
            # Keep the readout and card signature clear along the lower edge.
            footer = QLinearGradient(0, height * .76, 0, height)
            hue = (211, 227, 242) if self.instrument else (250, 247, 255)
            footer.setColorAt(0, QColor(*hue, 0))
            footer.setColorAt(1, QColor(*hue, 169))
            painter.fillRect(bounds, footer)
        finally:
            painter.end()
        return raster
