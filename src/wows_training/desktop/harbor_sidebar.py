"""Coastal artwork with uniform contrast behind the desktop navigation."""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import QFrame

from .naval_assets import default_naval_assets


class HarborSidebar(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('sidebar')
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setProperty('backgroundEffect', True)
        self._background_enabled = True
        try:
            self._art = default_naval_assets().background('harbor-sidebar')
        except (OSError, ValueError):
            self._art = None

    def set_background_enabled(self, enabled):
        enabled = bool(enabled)
        if self._background_enabled != enabled:
            self._background_enabled = enabled
            self.update()

    def _source_rect(self, target):
        image = self._art
        scale = max(target.width()/image.width(), target.height()/image.height())
        width, height = target.width()/scale, target.height()/scale
        # Favor the left crop so the right window jamb stays out of the menu.
        left = .20 * (image.width()-width)
        return QRectF(left, (image.height()-height)*.45, width, height)

    def paintEvent(self, _event):
        area = QRectF(self.rect())
        if area.isEmpty():
            return
        painter = QPainter(self)
        try:
            if self._background_enabled and self._art is not None:
                painter.fillRect(area, QColor('#203A56'))
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                painter.save()
                painter.setOpacity(.50)
                painter.drawImage(area, self._art, self._source_rect(area))
                painter.restore()
            else:
                painter.fillRect(area, QColor('#58718B'))
            self._paint_frame(painter, area)
        finally:
            painter.end()

    @staticmethod
    def _paint_frame(painter, area):
        height = area.height()
        right = area.right()
        seam = QLinearGradient(0, 0, 0, height)
        seam.setColorAt(0, QColor(132, 223, 253, 230))
        seam.setColorAt(.57, QColor(103, 217, 250, 82))
        seam.setColorAt(.88, QColor(124, 225, 247, 205))
        seam.setColorAt(1, QColor(238, 112, 176, 182))
        painter.fillRect(QRectF(right-1, 0, 1, height), seam)
        painter.fillRect(QRectF(0, height-58, 3, 37), QColor(101, 219, 248, 170))
        painter.fillRect(QRectF(0, height-18, 3, 6), QColor(249, 122, 185, 177))
