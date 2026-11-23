"""Compact archive counters with fleet symbols and a clipped tab edge."""
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout

from . import academy_palette as colors
from .fleet_shell import navigation_icon
from .layered_panel import LayeredPanel, fold_content_inset


class LibraryCounter(LayeredPanel):
    def __init__(self, title, icon_index, *, primary=False):
        super().__init__(accent=colors.CYAN if primary else colors.BLUE, cut=19)
        self.primary = primary
        self.setAccessibleName(title)
        self.setMinimumHeight(69)
        row = QHBoxLayout(self)
        row.setContentsMargins(17, 8, fold_content_inset(self.cut, 16), 17)
        column = QVBoxLayout()
        column.setSpacing(0)
        self.value = QLabel('0')
        self.value.setObjectName('metric')
        self.value.setStyleSheet('font: 700 28px "Bahnschrift", "Segoe UI"; color:' +
                                 (colors.BLUE if primary else colors.INK))
        caption = QLabel(title)
        caption.setStyleSheet('font-weight:600; color:' + colors.MUTED)
        column.addWidget(self.value)
        column.addWidget(caption)
        row.addLayout(column, 1)
        icon = self.symbol = QLabel()
        icon.setFixedSize(32, 32)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = navigation_icon(icon_index, 24).pixmap(QSize(24, 24), 2.)
        if not pixmap.isNull():
            pixmap = pixmap.copy()
            pen = QPainter(pixmap)
            pen.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            pen.fillRect(pixmap.rect(), QColor(colors.BLUE))
            pen.end()
            icon.setPixmap(pixmap)
        row.addWidget(icon)
