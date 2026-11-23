"""Folded display surfaces with painted depth and bounded hover illumination."""
from PySide6.QtCore import QEasingCurve, QEvent, QPointF, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from . import academy_palette as colors
from .motion import motion_enabled, subscribe_motion


def _fold_extent(cut, surface_height):
    return min(cut + 34., surface_height * .63)


def fold_content_inset(cut, top, *, minimum=20):
    """Right gutter for foreground ink at a given distance from the panel top."""
    # The front sheet ends 6 px before the backing. A further 10 px along
    # either axis gives the ink 7 px of clearance normal to the diagonal.
    return max(minimum, int(cut + 34 + 6 + 1 - top + 10))


def panel_path(rect, cut=18):
    """Round the leading corner while clipping the opposite two corners."""
    rect = QRectF(rect)
    cut = min(float(cut), rect.width() * .15, rect.height() * .28)
    radius = min(7., rect.height() * .1)
    path = QPainterPath()
    path.moveTo(rect.left() + radius, rect.top())
    path.lineTo(rect.right() - cut, rect.top())
    path.lineTo(rect.right(), rect.top() + cut)
    path.lineTo(rect.right(), rect.bottom() - radius)
    path.quadTo(rect.bottomRight(), QPointF(rect.right() - radius, rect.bottom()))
    path.lineTo(rect.left() + cut, rect.bottom())
    path.lineTo(rect.left(), rect.bottom() - cut)
    path.lineTo(rect.left(), rect.top() + radius)
    path.quadTo(rect.topLeft(), QPointF(rect.left() + radius, rect.top()))
    path.closeSubpath()
    return path


def paint_layered_panel(painter, rect, *, dark=False, accent=colors.BLUE, progress=0., cut=18):
    """Paint beneath layout contents. Five pixels along the bottom/right hold the backing."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    rect = QRectF(rect).adjusted(1, 1, -6, -6)
    if rect.width() <= 1 or rect.height() <= 1:
        painter.restore()
        return
    main = panel_path(rect.translated(0, -progress * 1.2), cut)
    shadow = panel_path(rect.translated(4, 5), cut)
    painter.fillPath(shadow, QColor(17, 29, 49, 34 + round(progress * 10)))
    pink = panel_path(rect.translated(2, 2.7), cut)
    painter.fillPath(pink, QColor(colors.PINK))
    surface = QLinearGradient(rect.topLeft(), rect.bottomRight())
    surface.setColorAt(0, QColor('#274768' if dark else '#FFFFFF'))
    surface.setColorAt(.55, QColor('#162C44' if dark else '#F5F9FF'))
    surface.setColorAt(1, QColor('#0F1B2E' if dark else '#E9EFF8'))
    painter.fillPath(main, surface)
    painter.setPen(QPen(QColor('#526F91' if dark else '#AFC4D9'), 1))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(main)
    painter.save()
    painter.setClipPath(main)
    sheen = QLinearGradient(rect.left(), rect.top(), rect.right(), rect.bottom())
    sheen.setColorAt(0, QColor(255, 255, 255, 26 + round(progress * 22)))
    sheen.setColorAt(.37, QColor(255, 255, 255, 5))
    sheen.setColorAt(1, QColor(240, 87, 166, 8 + round(progress * 15)))
    painter.fillRect(rect, sheen)
    # Offset drafting lines and an exposed dark corner give the white surface depth.
    strip = QPainterPath()
    extent = _fold_extent(cut, rect.height())
    strip.moveTo(rect.right() - extent, rect.top())
    strip.lineTo(rect.right(), rect.top())
    strip.lineTo(rect.right(), rect.top() + extent)
    strip.closeSubpath()
    painter.fillPath(strip, QColor(colors.NIGHT))
    painter.setPen(QPen(QColor(colors.PINK), 2))
    # A short illuminated segment travels along the bevel during the normal
    # hover transition. Its entire stroke stays on the exposed corner strip.
    rail_span = max(0., extent - 12.)
    rail_start = QPointF(rect.right() - rail_span - 2, rect.top() + 2)
    rail_end = QPointF(rect.right() - 2, rect.top() + rail_span + 2)
    painter.drawLine(rail_start, rail_end)
    if progress > 0 and rail_span > 10:
        rail = rail_end - rail_start
        lead = .2 + .77 * progress
        painter.setPen(QPen(QColor(219, 250, 255, round(230 * progress)), 2.1))
        painter.drawLine(rail_start + rail * (lead - .18), rail_start + rail * lead)
    edge = QColor(accent)
    edge.setAlpha(255 if dark else 195)
    painter.fillRect(QRectF(rect.left(), rect.top(), 4, min(34, rect.height()*.45)), edge)
    painter.setPen(QPen(QColor(255, 255, 255, 75 if dark else 230), 1))
    painter.drawLine(QPointF(rect.left() + 9, rect.top() + 1), QPointF(rect.right() - cut - 12, rect.top() + 1))
    # File-leaf seams occupy the padding gutter, leaving the label field clear.
    seam=QColor('#D2E2F1' if dark else '#496580');seam.setAlpha(25 if dark else 20)
    painter.setPen(QPen(seam,1))
    for offset in (7,11):
        painter.drawLine(QPointF(rect.left()+cut+7,rect.bottom()-offset),
                         QPointF(rect.right()-cut-9,rect.bottom()-offset))
    painter.setPen(QPen(QColor(240,87,166,47 if dark else 35),1))
    painter.drawLine(QPointF(rect.left()+10,rect.top()+38),
                     QPointF(rect.left()+10,min(rect.bottom()-cut-6,rect.top()+74)))
    if progress > 0:
        halo = QLinearGradient(rect.left(), 0, rect.right(), 0)
        halo.setColorAt(0, QColor(68, 219, 243, round(progress*24)))
        halo.setColorAt(.7, QColor(240, 87, 166, round(progress*12)))
        halo.setColorAt(1, QColor(240, 87, 166, 0))
        painter.fillRect(rect, halo)
    painter.restore()
    painter.restore()


class LayeredPanel(QWidget):
    def __init__(self, parent=None, *, dark=False, accent=colors.BLUE, cut=18):
        super().__init__(parent)
        self.setObjectName('layeredPanel')
        self.dark, self.accent, self.cut = dark, accent, cut
        self.hover_progress = 0.
        self._bound = False
        self._observed_window = None
        self._hover = QVariantAnimation(self)
        self._hover.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hover.valueChanged.connect(self._frame)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def _frame(self, value):
        self.hover_progress = float(value)
        self.update()

    def _finish(self, *_):
        self._hover.stop()
        self.hover_progress = 0.
        self.update()

    def _preference(self, enabled):
        if not enabled:
            self._finish()

    def _aim(self, inside):
        self._hover.stop()
        if not motion_enabled(self) or self.window().isMinimized():
            self._finish()
            return
        self._hover.setStartValue(self.hover_progress)
        self._hover.setEndValue(1. if inside else 0.)
        self._hover.setDuration(280 if inside else 330)
        self._hover.start()

    def enterEvent(self, event):
        self._aim(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._aim(False)
        super().leaveEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._bound:
            self._bound = subscribe_motion(self, self._preference)
        window = self.window()
        if self._observed_window is not window:
            if self._observed_window is not None:
                self._observed_window.removeEventFilter(self)
            self._observed_window = window
            window.installEventFilter(self)

    def hideEvent(self, event):
        self._finish()
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if watched is self._observed_window and event.type() in (QEvent.Type.Close, QEvent.Type.Hide, QEvent.Type.WindowStateChange):
            if event.type() != QEvent.Type.WindowStateChange or watched.isMinimized():
                self._finish()
        return False

    def paintEvent(self, event):
        painter = QPainter(self)
        paint_layered_panel(painter, self.rect(), dark=self.dark, accent=self.accent,
                            progress=self.hover_progress, cut=self.cut)
        painter.end()


class _EmptyTableArt(QWidget):
    def __init__(self, table):
        super().__init__(table.viewport())
        self.table = table
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet('background:transparent;border:none;')
        try:
            from .naval_assets import default_naval_assets
            self.icon = default_naval_assets().icon('warehouse')
        except (OSError, ValueError):
            self.icon = None
        table.viewport().installEventFilter(self)
        for signal in (table.model().rowsInserted, table.model().rowsRemoved, table.model().modelReset):
            signal.connect(self.refresh)
        self.refresh()

    def refresh(self, *_):
        self.setGeometry(self.parentWidget().rect())
        self.setVisible(self.table.rowCount() == 0)
        if self.table.rowCount() == 0:
            self.raise_()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.refresh()
        return False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        painter.setPen(Qt.PenStyle.NoPen)
        fold = QPainterPath()
        fold.moveTo(w*.64, h)
        fold.lineTo(w*.87, h*.37)
        fold.lineTo(w, h*.25)
        fold.lineTo(w, h)
        fold.closeSubpath()
        shade = QLinearGradient(w*.65,h,w,h*.3)
        shade.setColorAt(0,QColor(240,87,166,0))
        shade.setColorAt(1,QColor(240,87,166,15))
        painter.fillPath(fold,shade)
        if self.icon is not None:
            length = min(160.,w*.18,h*.35)
            rect = QRectF(w-length-40,h-length-38,length,length)
            painter.setOpacity(.045)
            painter.drawImage(rect,self.icon)
            painter.setOpacity(1.)
        painter.setPen(QPen(QColor(17,29,49,16),1))
        painter.drawLine(QPointF(w-28,h*.17),QPointF(w-28,h*.64))
        painter.setPen(QPen(QColor(240,87,166,42),3))
        painter.drawLine(QPointF(w-28,h*.17),QPointF(w-28,h*.17+25))
        painter.end()


def install_empty_table_art(table):
    """Show original fleet imagery only while the archive has no records."""
    overlay = _EmptyTableArt(table)
    table._empty_table_art = overlay
    return overlay
