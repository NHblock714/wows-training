"""A query-history popup with separate controls for opening and clearing each entry."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
                              QStyle, QStyleOptionToolButton, QStylePainter, QToolButton, QVBoxLayout, QWidget)

from . import academy_palette as tone
from .tuning_controls import field_surface, glass_cap


_POPUP_STYLE = """
QFrame#queryHistoryPopup {
    background:#F3F9FD; border:1px solid #8AAAC8; border-top:2px solid #609FD2;
}
QScrollArea#queryHistoryScroll, QWidget#queryHistoryRows { background:#F3F9FD; border:0; }
QFrame#queryHistoryRow { background:#F3F9FD; border:0; border-bottom:1px solid #DAE8F2; }
QToolButton#queryHistoryOpen {
    padding:4px 8px; color:#12263D; background:transparent;
    border:1px solid transparent; border-left:2px solid transparent; border-radius:2px;
}
QToolButton#queryHistoryOpen:hover, QToolButton#queryHistoryOpen:focus {
    background:#D4EAF9; color:#103E6B; border-left:2px solid #C64D8D;
}
QToolButton#queryHistoryOpen:pressed { background:#BCDDF4; }
QToolButton#queryHistoryClear {
    padding:1px; color:#40627F; background:transparent; border:1px solid transparent;
    border-radius:4px; font-weight:600;
}
QToolButton#queryHistoryClear:hover, QToolButton#queryHistoryClear:focus {
    color:#96354F; border-color:#DE9EB0; background:#FBE5ED;
}
QToolButton#queryHistoryClear:pressed { color:#76243D; background:#F4C8D8; }
QLabel#queryHistoryEmpty { padding:10px; color:#486078; background:transparent; }
"""


class _HistoryAction(QToolButton):
    def __init__(self, caption, parent, *, elide=False):
        super().__init__(parent)
        self._caption = caption
        self._elide = elide
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setText(caption.replace('&', '&&'))

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            if not event.isAutoRepeat():
                self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        option.text = ''
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ToolButton, option)
        active = self.underMouse() or self.hasFocus()
        if self._elide:
            text_rect = self.rect().adjusted(10, 0, -8, 0)
            caption = self.fontMetrics().elidedText(self._caption, Qt.TextElideMode.ElideRight, text_rect.width())
            painter.setPen(QColor('#103E6B' if active else tone.INK))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, caption)
        else:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(QColor('#96354F' if active else '#40627F'), 1.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            center = QRectF(self.rect()).center()
            painter.drawLine(center + QPointF(-3, -3), center + QPointF(3, 3))
            painter.drawLine(center + QPointF(-3, 3), center + QPointF(3, -3))
        painter.end()


class QueryHistoryPicker(QToolButton):
    """A compact history dropdown with independently focusable clear buttons."""

    targetOpened = Signal(object)
    clearRequested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('queryHistoryPicker')
        self.setText('常用与本地档案')
        self.setAccessibleName(self.text())
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.clicked.connect(self._toggle_popup)

        self._popup = QFrame(self, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self._popup.setObjectName('queryHistoryPopup')
        self._popup.setStyleSheet(_POPUP_STYLE)
        self._popup.installEventFilter(self)
        box = QVBoxLayout(self._popup)
        box.setContentsMargins(5, 6, 5, 5)
        self._scroll = QScrollArea(self._popup)
        self._scroll.setObjectName('queryHistoryScroll')
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        box.addWidget(self._scroll)
        escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), self._popup)
        escape.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        escape.activated.connect(self.hidePopup)
        self._buttons = {}
        self._row_height = 38
        self.set_records([])

    def sizeHint(self):
        return QSize(200, max(34, QFontMetrics(self.font()).height() + 14))

    def minimumSizeHint(self):
        return QSize(105, self.sizeHint().height())

    def set_records(self, records):
        focus = self._popup.focusWidget() if self._popup.isVisible() else None
        entries = list(self._buttons)
        index, clear_focused = next(((i, focus is clear) for i, (open_button, clear) in enumerate(self._buttons.values())
                                     if focus in (open_button, clear)), (0, False))
        focus_key = entries[index] if entries else None
        offset = self._scroll.verticalScrollBar().value()
        old = self._scroll.takeWidget()
        if old is not None:
            old.hide()
            old.deleteLater()

        body = QWidget()
        body.setObjectName('queryHistoryRows')
        rows = QVBoxLayout(body)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(2)
        self._buttons = {}
        self._row_height = max(36, QFontMetrics(self.font()).height() + 15)
        for target in records:
            record = dict(target)
            label = record.get('label') or record['name']
            row = QFrame(body)
            row.setObjectName('queryHistoryRow')
            row.setFixedHeight(self._row_height)
            actions = QHBoxLayout(row)
            actions.setContentsMargins(1, 1, 3, 2)
            actions.setSpacing(2)
            open_button = _HistoryAction(label, row, elide=True)
            open_button.setObjectName('queryHistoryOpen')
            open_button.setAccessibleName(f'打开 {label}')
            open_button.setToolTip(label)
            open_button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
            open_button.installEventFilter(self)
            open_button.clicked.connect(lambda _checked=False, value=record: self._open_record(value))
            clear = _HistoryAction('×', row)
            clear.setObjectName('queryHistoryClear')
            clear.setFixedSize(30, self._row_height - 5)
            clear.setAccessibleName(f'清除 {label} 的本地查询历史')
            clear.setToolTip('清除该条目的本地战绩缓存与搜索记录')
            clear.installEventFilter(self)
            clear.clicked.connect(lambda _checked=False, value=record: self.clearRequested.emit(value))
            actions.addWidget(open_button, 1)
            actions.addWidget(clear)
            rows.addWidget(row)
            self._buttons[(record['kind'], record['region'], record['id'])] = (open_button, clear)
        if not self._buttons:
            empty = QLabel('暂无查询历史', body)
            empty.setObjectName('queryHistoryEmpty')
            rows.addWidget(empty)
        rows.addStretch()
        self._scroll.setWidget(body)

        if self._popup.isVisible():
            self._position_popup()
            self._scroll.verticalScrollBar().setValue(offset)
            if self._buttons:
                pair = self._buttons.get(focus_key) or list(self._buttons.values())[min(index, len(self._buttons) - 1)]
                pair[clear_focused].setFocus(Qt.FocusReason.PopupFocusReason)

    def _position_popup(self):
        top = self.mapToGlobal(QPoint(0, 0))
        bottom = top.y() + self.height()
        available = self.screen().availableGeometry()
        width = min(available.width(), max(self.width(), 240))
        desired = min(390, max(self._row_height + 16, len(self._buttons) * (self._row_height + 2) + 12))
        below = available.bottom() + 1 - bottom
        above = top.y() - available.top()
        open_below = desired <= below or below >= above
        height = max(1, min(desired, below if open_below else above))
        left = max(available.left(), min(top.x(), available.right() - width + 1))
        self._popup.setGeometry(QRect(left, bottom if open_below else top.y() - height, width, height))

    def showPopup(self):
        if not self.isEnabled() or self._popup.isVisible():
            return
        self._position_popup()
        self._popup.show()
        if self._buttons:
            next(iter(self._buttons.values()))[0].setFocus(Qt.FocusReason.PopupFocusReason)

    def hidePopup(self):
        self._popup.hide()

    def _toggle_popup(self):
        if self._popup.isVisible():
            self.hidePopup()
        else:
            self.showPopup()

    def _open_record(self, record):
        self.hidePopup()
        self.targetOpened.emit(record)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.showPopup()
            event.accept()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event):
        self.hidePopup()
        super().hideEvent(event)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self.hidePopup()
        super().changeEvent(event)

    def eventFilter(self, watched, event):
        if watched is self._popup and event.type() in (QEvent.Type.Show, QEvent.Type.Hide):
            self.update()
        if isinstance(watched, _HistoryAction) and event.type() == QEvent.Type.KeyPress:
            step = {Qt.Key.Key_Up: -1, Qt.Key.Key_Down: 1}.get(event.key())
            if step:
                pairs = list(self._buttons.values())
                for index, pair in enumerate(pairs):
                    if watched in pair:
                        button = pairs[max(0, min(index + step, len(pairs) - 1))][watched is pair[1]]
                        button.setFocus(Qt.FocusReason.PopupFocusReason)
                        self._scroll.ensureWidgetVisible(button, 0, 4)
                        return True
        return super().eventFilter(watched, event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1.1, 1.3, -1.8, -2.3)
        enabled = self.isEnabled()
        opened = self._popup.isVisible()
        emphasis = float(enabled and (opened or self.underMouse()))
        surface = field_surface(painter, rect, emphasis, enabled, self.hasFocus() or opened)
        cap = QRectF(rect.right() - 24, rect.top() + 1., 23., rect.height() - 2.)
        glass_cap(painter, surface, cap, emphasis)
        tip = cap.center()
        direction = -1. if opened else 1.
        chevron = QPainterPath(tip + QPointF(-4., -2. * direction))
        chevron.lineTo(tip + QPointF(0., 2. * direction))
        chevron.lineTo(tip + QPointF(4., -2. * direction))
        painter.setPen(QPen(QColor('#24577B' if enabled else '#899AA9'), 1.55))
        painter.drawPath(chevron)
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, max(0, self.width() - 43))
        painter.setPen(QColor(tone.INK if enabled else '#657C90'))
        painter.drawText(rect.adjusted(10, 0, -32, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        painter.end()
