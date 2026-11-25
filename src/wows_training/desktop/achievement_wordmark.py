"""Small enamel cartouches with ink-fitted, vector rarity lettering."""
from math import ceil
from typing import NamedTuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import QLabel

from .achievement_rarity import RarityStyle
from .presentation_fonts import load_presentation_fonts


class _Lettering(NamedTuple):
    role: str
    weight: QFont.Weight
    tracking: float  # Applied to the source outline at 64 px.
    ink_height: int
    shoulder: int
    enamel: str
    highlight: str


_LETTERING = (
    _Lettering("tech", QFont.Weight.Medium, 1, 10, 4, "#374958", "#F1F4F7"),
    _Lettering("tech", QFont.Weight.DemiBold, 4, 10, 6, "#17493B", "#B6F6CD"),
    _Lettering("tech", QFont.Weight.Bold, 6, 12, 7, "#194875", "#C9F5FF"),
    _Lettering("grade", QFont.Weight.Bold, 3, 14, 7, "#4B356E", "#F7E4FF"),
    _Lettering("display", QFont.Weight.Bold, 1, 13, 6, "#5B3C17", "#FFF2B2"),
    _Lettering("display", QFont.Weight.Black, 3, 15, 8, "#721A32", "#FFE0CC"),
)


def _plate(rect, rank):
    left, top, right, bottom = rect.getCoords()
    middle = rect.center().y()
    if rank == 0:
        path = QPainterPath()
        path.addRoundedRect(rect, 2, 2)
        return path
    if rank == 1:
        corners = [(left, top), (right - 5, top), (right, middle), (right - 5, bottom), (left, bottom)]
    elif rank == 2:
        corners = [(left + 6, top), (right, top), (right - 6, bottom), (left, bottom)]
    elif rank == 3:
        corners = [(left, middle), (left + 6, top), (right - 6, top), (right, middle),
                   (right - 6, bottom), (left + 6, bottom)]
    elif rank == 4:
        corners = [(left, top + 4), (left + 4, top), (right - 4, top), (right, top + 4),
                   (right, bottom - 4), (right - 4, bottom), (left + 4, bottom), (left, bottom - 4)]
    else:
        corners = [(left, top + 6), (left + 8, top), (right - 7, top), (right, middle - 2),
                   (right - 4, middle + 2), (right - 2, middle + 2), (right - 8, bottom),
                   (left + 7, bottom), (left, middle + 2), (left + 4, middle - 2), (left + 2, middle - 2)]
    path = QPainterPath(QPointF(*corners[0]))
    for corner in corners[1:]:
        path.lineTo(*corner)
    path.closeSubpath()
    return path


class RarityWordmark(QLabel):
    """The QLabel retains searchable, accessible text while QPainter fits the actual glyph outlines."""

    def __init__(self, style: RarityStyle, parent=None):
        super().__init__(style.english, parent)
        self.setObjectName("achievementRarityWordmark")
        self.setAccessibleName(f"{style.english} · {style.label}")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._style = style
        design = _LETTERING[style.rank]
        font = QFont(load_presentation_fonts()[design.role])
        font.setPixelSize(64)
        font.setWeight(design.weight)
        font.setItalic(style.rank == 3)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, design.tracking)
        self._letters = QPainterPath()
        self._letters.addText(0, 0, font, style.english)
        if style.rank == 2:
            self._letters = QTransform().shear(-.22, 0).map(self._letters)
        ink = self._letters.boundingRect()
        width = ceil(ink.width() * design.ink_height / ink.height()) + 2 * design.shoulder
        self.setFixedSize(min(width, 98), design.ink_height + (7 if style.rank < 3 else 9))

    def paintEvent(self, event):
        rank = self._style.rank
        design = _LETTERING[rank]
        plate = QRectF(self.rect()).adjusted(.65, .65, -.65, -.65)
        outline = _plate(plate, rank)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        enamel = QColor(design.enamel)
        accent = QColor(self._style.accent)
        field = QLinearGradient(plate.topLeft(), plate.bottomRight())
        field.setColorAt(0, enamel if rank == 0 else enamel.lighter(130))
        field.setColorAt(.55, enamel)
        field.setColorAt(1, enamel.darker(135))
        painter.setPen(QPen(enamel.darker(150), 1))
        painter.setBrush(field)
        painter.drawPath(outline)
        painter.setClipPath(outline)
        painter.setPen(QPen(accent, 1))
        if rank == 1:
            x, y = plate.right(), plate.center().y()
            painter.drawLine(QPointF(x - 4, y - 3), QPointF(x - 2, y))
            painter.drawLine(QPointF(x - 2, y), QPointF(x - 4, y + 3))
        elif rank == 2:
            for x in (plate.left() + 2, plate.right() - 6):
                painter.drawLine(QPointF(x, plate.bottom() - 1), QPointF(x + 5, plate.top() + 1))
        elif rank >= 3:
            inset = plate.adjusted(2, 2, -2, -2)
            edge = QColor(accent)
            edge.setAlpha(115 if rank == 3 else 175)
            painter.setPen(QPen(edge, .8))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(_plate(inset, rank))

        # Measure visible ink, including serif overhangs, independently of font line metrics.
        area = plate.adjusted(design.shoulder - .65, 2, -design.shoulder + .65, -2)
        bounds = self._letters.boundingRect()
        scale = min(area.width() / bounds.width(), design.ink_height / bounds.height())
        fit = QTransform()
        fit.translate(area.center().x(), area.center().y())
        fit.scale(scale, scale)
        fit.translate(-bounds.center().x(), -bounds.center().y())
        lettering = fit.map(self._letters)
        ink = lettering.boundingRect()
        foil = QLinearGradient(ink.topLeft(), ink.bottomLeft())
        foil.setColorAt(0, QColor(design.highlight))
        foil.setColorAt(.46, QColor(design.highlight))
        foil.setColorAt(1, accent)
        if rank >= 3:
            painter.fillPath(lettering.translated(.4, 1), enamel.darker(190))
        painter.fillPath(lettering, foil)
        painter.end()
