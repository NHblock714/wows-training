"""A paper-and-glass plotting surface shared by the analysis timelines."""
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor

from . import academy_palette as tone
from .layered_panel import paint_layered_panel

PRIMARY_SERIES = '#087DA9'
SECONDARY_SERIES = '#B33580'


def paint_analysis_surface(painter, bounds):
    painter.fillRect(QRectF(bounds),QColor(tone.CANVAS))
    box=QRectF(bounds).adjusted(1,1,-3,-3)
    paint_layered_panel(painter,box,accent=tone.PINK,cut=14)
    painter.fillRect(QRectF(box.left()+16,box.top()+1,29,3),QColor(tone.BLUE))
    painter.fillRect(QRectF(box.left()+48,box.top()+1,8,3),QColor(tone.PINK))
