"""Pinned public player and clan shortcuts for the query landing page."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QPointF, QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (QAbstractButton, QFrame, QGridLayout, QLabel, QScrollArea, QSizePolicy,
                              QVBoxLayout, QWidget)

from . import academy_palette as tone
from .dog_tag_visual import dog_tag_image, paint_dog_tag
from .layered_panel import LayeredPanel, panel_path
from .motion import motion_enabled, subscribe_motion
from .player_dossier_panel import REGIONS
from .presentation_fonts import load_presentation_fonts


class _FavoriteCard(QAbstractButton):
    def __init__(self, target, parent):
        super().__init__(parent)
        self.setObjectName('queryFavoriteCard')
        self.setText(target['name'])
        self._clan = target['kind'] == 'clan'
        self._dog_tag = dog_tag_image(None if self._clan else target.get('_dog_tag_png'))
        self._detail = f"{REGIONS.get(target['region'], target['region'])} / {'公会' if self._clan else '玩家'}"
        self._accent = QColor('#7855A2' if self._clan else '#156AC1')
        self._background_enabled = True
        self.setProperty('backgroundEffect', True)
        self.setAccessibleName(f"打开{target['name']} · {self._detail}")
        self.setToolTip(f"{target['name']} · {self._detail}\nID {target['id']}")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._hover = 0.
        self._motion = QVariantAnimation(self)
        self._motion.setDuration(190)
        self._motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._motion.valueChanged.connect(self._frame)
        self.pressed.connect(self.update)
        self.released.connect(self.update)

    def sizeHint(self):
        return QSize(275, 96)

    def minimumSizeHint(self):
        return QSize(138, 96)

    def _frame(self, value):
        self._hover = float(value)
        self.update()

    def _highlight(self, active):
        self._motion.stop()
        end = float(active)
        if not motion_enabled(self) or not self.isVisible() or self.window().isMinimized():
            self._frame(end)
        elif self._hover != end:
            self._motion.setStartValue(self._hover)
            self._motion.setEndValue(end)
            self._motion.start()

    def _motion_changed(self, enabled):
        if not enabled:
            self._highlight(self.underMouse() or self.hasFocus())

    def enterEvent(self, event):
        super().enterEvent(event)
        self._highlight(True)

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._highlight(self.hasFocus())

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._highlight(True)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._highlight(self.underMouse())

    def showEvent(self, event):
        super().showEvent(event)
        subscribe_motion(self, self._motion_changed)
        settings = getattr(self.window(), 'client_preferences', {})
        self.set_background_enabled(settings.get('background_enabled', self._background_enabled))

    def set_background_enabled(self, enabled):
        if self._background_enabled != bool(enabled):
            self._background_enabled = bool(enabled)
            self.update()

    def hideEvent(self, event):
        self._motion.stop()
        self._frame(0.)
        super().hideEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not event.isAutoRepeat():
                self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event):
        pressed = self.isDown()
        hover = self._hover
        box = QRectF(self.rect()).adjusted(1, 3, -4, -6)
        if motion_enabled(self):
            box.translate(0, 1.5 if pressed else -1.6 * hover)
        face = panel_path(box, 15)
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setOpacity(1. if self.isEnabled() else .55)
            painter.fillPath(panel_path(box.translated(3, 4), 15), QColor(34, 73, 118, 30))
            fill = QLinearGradient(box.topLeft(), box.bottomRight())
            fill.setColorAt(0, QColor('#F9FCFF' if pressed else '#FFFFFF'))
            fill.setColorAt(.56, QColor('#EDF5FC'))
            fill.setColorAt(1, QColor('#D7E6F5' if pressed else '#E2EEF9'))
            painter.fillPath(face, fill)
            glint = QColor(self._accent)
            glint.setAlpha(round(28 * hover))
            painter.fillPath(face, glint)
            painter.save()
            painter.setClipPath(face)
            self._paint_rails(painter, box, hover)
            painter.restore()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            edge = QColor(tone.PINK if self.hasFocus() else '#A0BDD8')
            painter.setPen(QPen(edge, 1.5 if self.hasFocus() else 1.))
            painter.drawPath(face)
            mark = QRectF(box.left() + 13, box.center().y() - 21, 42, 42)
            self._paint_mark(painter, mark)
            self._paint_label(painter, box, hover)

    def _paint_rails(self, painter, box, hover):
        x, y = box.right(), box.bottom()
        inset = min(92., box.width() * .37)
        points = [QPointF(x - inset, y), QPointF(x - inset * .58, y - 16),
                  QPointF(x - 19, y - 16), QPointF(x, y - 34), QPointF(x, y)]
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(96, 158, 213, 34 + round(18 * hover)))
        painter.drawPolygon(QPolygonF(points))
        painter.setPen(QPen(QColor(73, 140, 193, 100), .85))
        painter.drawPolyline(QPolygonF(points[:-1]))
        if self._background_enabled:
            # Short engraved hatch marks stay inside the folded lower gutter.
            tint = QColor(self._accent)
            tint.setAlpha(round(66 + 50 * hover))
            painter.setPen(QPen(tint, 1))
            for i in range(5):
                end = x - 13 - i * 5
                painter.drawLine(QPointF(end-4, y-7), QPointF(end, y-12))
            if hover:
                tint.setAlpha(round(185 * hover))
                painter.setPen(QPen(tint, 1.5))
                lead = points[0] + (points[1] - points[0]) * hover
                painter.drawLine(points[0], lead)
        painter.setPen(QPen(QColor(73, 140, 193, 100), .85))
        painter.drawLine(QPointF(x - inset * .60, y - 5), QPointF(x - 8, y - 5))
        painter.fillRect(QRectF(box.left() + 10, box.top() + 2, 30 + 16 * hover, 2), self._accent)
        painter.fillRect(QRectF(box.left() + 43 + 16 * hover, box.top() + 2, 8, 2), QColor(tone.PINK))

    def _paint_mark(self, painter, mark):
        base = panel_path(mark, 8)
        painter.fillPath(base.translated(1, 1.5), QColor(48, 75, 107, 39))
        tint = QColor(self._accent)
        metal = QLinearGradient(mark.topLeft(), mark.bottomRight())
        metal.setColorAt(0, self._accent.lighter(192))
        metal.setColorAt(.43, QColor('#F3F8FC'))
        metal.setColorAt(1, self._accent.lighter(161))
        painter.fillPath(base, metal)
        tint.setAlpha(117)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(tint, 1))
        painter.drawPath(base)
        if paint_dog_tag(painter, mark.adjusted(3, 3, -3, -3), self._dog_tag):
            return
        painter.save()
        painter.translate(mark.center())
        if self._background_enabled:
            dial = QRectF(-16., -16., 32., 32.)
            painter.setPen(QPen(QColor(255, 255, 255, 215), 1.25))
            painter.drawArc(dial.translated(0, .8), 0, 360*16)
            tint.setAlpha(round(68 + 48*self._hover))
            painter.setPen(QPen(tint, 1.))
            for start in (22, 202):
                painter.drawArc(dial, start*16, 143*16)
        painter.setPen(QPen(self._accent, 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self._clan:
            points = QPolygonF([QPointF(-10, 7), QPointF(0, -7), QPointF(10, 7)])
            painter.drawPolygon(points)
            painter.setBrush(QColor('#F3FAFF'))
            for point in points:
                painter.drawEllipse(point, 2.7, 2.7)
        else:
            painter.drawEllipse(QPointF(0, -4), 4.2, 4.2)
            shoulders = QPainterPath(QPointF(-9, 9))
            shoulders.cubicTo(QPointF(-8, 1), QPointF(8, 1), QPointF(9, 9))
            painter.drawPath(shoulders)
        painter.restore()

    def _paint_label(self, painter, box, hover):
        x = box.left() + 66
        center = box.center().y()
        width = max(1, int(box.right() - x - 31))
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        font = QFont(self.font())
        font.setPixelSize(18)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(tone.INK))
        name = painter.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, width)
        painter.drawText(QRectF(x, center - 26, width, 26), align, name)
        font.setPixelSize(13)
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(QColor(tone.MUTED))
        detail = painter.fontMetrics().elidedText(self._detail, Qt.TextElideMode.ElideRight, width)
        painter.drawText(QRectF(x, center + 2, width, 21), align, detail)
        arrow_x = box.right() - 13 + 2 * hover
        painter.setPen(QPen(self._accent, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawPolyline(QPolygonF([QPointF(arrow_x - 5, center - 5), QPointF(arrow_x, center),
                                        QPointF(arrow_x - 5, center + 5)]))


class QueryFavorites(QWidget):
    targetOpened = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('queryFavorites')
        self._records = []
        self._cards = []
        self._columns = 0
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 7, 4, 4)
        root.setSpacing(10)

        title = QLabel('收藏账号')
        family = load_presentation_fonts()['chinese_tech']
        title.setStyleSheet(f'font-family:"{family}"; font-size:22px; color:{tone.INK}; '
                            f'padding:3px 0 3px 10px; border-left:3px solid {tone.BLUE};')
        root.addWidget(title, alignment=Qt.AlignmentFlag.AlignLeft)

        self._empty = LayeredPanel(cut=15)
        empty_layout = QVBoxLayout(self._empty)
        empty_layout.setContentsMargins(24, 16, 58, 20)
        prompt = QLabel('打开账号后，点击「收藏」即可固定到这里。')
        prompt.setWordWrap(True)
        prompt.setStyleSheet(f'color:{tone.MUTED};')
        empty_layout.addWidget(prompt)
        self._empty.setMinimumHeight(78)
        root.addWidget(self._empty)

        self._scroll = QScrollArea()
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll.viewport().installEventFilter(self)
        self._content = QWidget()
        self._grid = QGridLayout(self._content)
        self._grid.setContentsMargins(0, 1, 2, 4)
        self._grid.setSpacing(10)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, 1)
        root.addStretch(0)
        self._scroll.hide()

    def set_records(self, records):
        records = [dict(record) for record in records]
        if records == self._records:
            return
        self._records = records
        for card in self._cards:
            self._grid.removeWidget(card)
            card.hide()
            card.deleteLater()
        self._cards = []
        for target in records:
            card = _FavoriteCard(target, self._content)
            identity = {key: value for key, value in target.items() if key != '_dog_tag_png'}
            card.clicked.connect(lambda _checked=False, record=identity: self.targetOpened.emit(dict(record)))
            self._cards.append(card)
        self._columns = 0
        self._reflow(self._scroll.viewport().width())
        self._empty.setVisible(not records)
        self._scroll.setVisible(bool(records))

    def _reflow(self, width):
        columns = min(3, max(1, (width + 10) // 278))
        if columns == self._columns:
            return
        self._columns = columns
        for column in range(3):
            self._grid.setColumnStretch(column, int(column < columns))
        for index, card in enumerate(self._cards):
            self._grid.removeWidget(card)
            self._grid.addWidget(card, index // columns, index % columns)
            card.show()

    def eventFilter(self, watched, event):
        if watched is self._scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._reflow(event.size().width())
        return super().eventFilter(watched, event)
