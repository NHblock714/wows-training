"""Harbor-styled placeholder for a confirmed private public account."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QRectF, Qt, Signal, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QLabel, QVBoxLayout

from . import academy_palette as tone
from .harbor_showcase import HarborShowcase
from .layered_panel import LayeredPanel, panel_path
from .motion import motion_enabled, subscribe_motion
from .presentation_fonts import load_presentation_fonts


class _ShareLinkLock(QAbstractButton):
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName('hiddenCareerShareLink')
        self.setAccessibleName('使用官方战绩查看链接')
        self.setToolTip('使用官方战绩查看链接')
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(112, 112)
        self._hovered = 0.
        self._pressed = 0.
        self._hover = self._animation(self._hover_frame)
        self._press = self._animation(self._press_frame)
        self.pressed.connect(lambda: self._transition(self._press, self._pressed, True, 95))
        self.released.connect(lambda: self._transition(self._press, self._pressed, False, 180))

    def _animation(self, update):
        animation = QVariantAnimation(self)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.valueChanged.connect(update)
        return animation

    def _hover_frame(self, value):
        self._hovered = float(value)
        self.update()

    def _press_frame(self, value):
        self._pressed = float(value)
        self.update()

    def _transition(self, animation, current, active, duration):
        animation.stop()
        goal = float(active)
        if not self.isVisible() or self.window().isMinimized() or not motion_enabled(self):
            (self._hover_frame if animation is self._hover else self._press_frame)(goal)
            return
        animation.setStartValue(current)
        animation.setEndValue(goal)
        animation.setDuration(duration)
        animation.start()

    def _point(self, hovered):
        self._transition(self._hover, self._hovered, hovered or self.hasFocus(), 235 if hovered else 290)

    def _motion_preference(self, enabled):
        if not enabled:
            self._hover.stop()
            self._press.stop()
            self._hover_frame(self.underMouse() or self.hasFocus())
            self._press_frame(self.isDown())
        self.update()

    def enterEvent(self, event):
        super().enterEvent(event)
        self._point(True)

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._point(False)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._point(self.underMouse())

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._point(self.underMouse())

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not event.isAutoRepeat():
                if motion_enabled(self):
                    self.animateClick()
                else:
                    self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        subscribe_motion(self, self._motion_preference)

    def hideEvent(self, event):
        self._hover.stop()
        self._press.stop()
        self._hover_frame(0.)
        self._press_frame(0.)
        super().hideEvent(event)

    def paintEvent(self, event):
        del event
        hover, press = self._hovered, self._pressed
        moving = motion_enabled(self)
        rise = (2.4 * hover - 3.2 * press) if moving else 0.
        size = min(self.width(), self.height()) / 112.
        shrink = (1 + .03 * hover - .085 * press) if moving else 1.
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setOpacity(1. if self.isEnabled() else .46)
            painter.translate(self.width() / 2., self.height() / 2.)
            painter.scale(size, size)
            painter.translate(0., -rise)
            painter.scale(shrink, shrink)

            outer = QRectF(-47, -47, 94, 94)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(5, 20, 40, 46 + round(47 * hover)))
            painter.drawEllipse(outer.translated(0., 3. - 2. * press))

            bloom = QRadialGradient(0, -3, 55)
            bloom.setColorAt(0, QColor(54, 147, 183, 34 + round(75 * hover)))
            bloom.setColorAt(.84, QColor(70, 200, 244, 17 + round(39 * hover)))
            bloom.setColorAt(1, QColor(70, 200, 244, 0))
            painter.setBrush(bloom)
            painter.drawEllipse(QRectF(-53, -53, 106, 106))

            face = QLinearGradient(0, -47, 0, 47)
            face.setColorAt(0, QColor(26, 61 + round(23 * hover), 97 + round(31 * hover), 237))
            face.setColorAt(1, QColor(17, 40 + round(14 * hover), 70 + round(22 * hover), 245))
            painter.setBrush(face)
            painter.setPen(QPen(QColor(161, 217, 252, 92 + round(66 * hover)), 1))
            painter.drawEllipse(outer)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(161, 217, 252, 92 + round(68 * hover)), 1))
            painter.drawEllipse(QRectF(-40, -40, 80, 80))

            sweep = round(19 * hover) if moving else 0
            arc_width = 2.4 + .6 * hover + .4 * press
            solid, round_cap = Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap
            painter.setPen(QPen(QColor(tone.CYAN), arc_width, solid, round_cap))
            painter.drawArc(outer, (33 + sweep) * 16, 108 * 16)
            painter.setPen(QPen(QColor(tone.PINK), arc_width, solid, round_cap))
            painter.drawArc(outer, (213 + sweep) * 16, 83 * 16)

            if self.hasFocus():
                painter.setPen(QPen(QColor('#E1FCFF'), 1, Qt.PenStyle.DashLine))
                painter.drawEllipse(QRectF(-51, -51, 102, 102))

            painter.setPen(QPen(QColor('#BCF1FA'), 2.5, solid, round_cap))
            painter.drawRoundedRect(QRectF(-9, -22, 18, 26), 8, 8)
            painter.setBrush(QColor(36, 85 + round(30 * hover), 121 + round(31 * hover)))
            painter.drawRoundedRect(QRectF(-16, -6, 32, 24), 4, 4)
            painter.drawLine(0, 3, 0, 10)
        finally:
            painter.end()


class HiddenCareerCard(LayeredPanel):
    shareLinkRequested = Signal()

    def __init__(self):
        super().__init__(dark=True, accent=tone.CYAN, cut=23)
        self.setObjectName('hiddenCareerCard')
        self.setMinimumHeight(152)
        self.setProperty('backgroundEffect', True)
        self._background_enabled = True
        self._art = HarborShowcase(instrument=True)

        row = QHBoxLayout(self)
        row.setContentsMargins(26, 18, 26, 22)
        labels = QVBoxLayout()
        labels.setSpacing(7)
        fonts = load_presentation_fonts()
        self.identity = self._label('', 15, '#A4DFF2')
        self.title = self._label('我是隐藏🐴', 30, '#F1F8FF', fonts['chinese_tech'])
        for label in (self.identity, self.title, self._label('该玩家已隐藏公开战绩。有官方查看链接时，可点击锁图标。', 14, '#D3E2F0')):
            labels.addWidget(label)
        row.addLayout(labels, 1)
        self.lock_button = _ShareLinkLock(self)
        self.lock_button.clicked.connect(self.shareLinkRequested)
        row.addWidget(self.lock_button, 0, Qt.AlignmentFlag.AlignVCenter)

    @staticmethod
    def _label(text, pixels, color, family='Microsoft YaHei UI'):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        font = QFont()
        font.setFamilies(list(dict.fromkeys((family, 'Microsoft YaHei UI', 'Segoe UI Emoji'))))
        label.setFont(font)
        label.setStyleSheet(f'background:transparent;color:{color};font-size:{pixels}px;')
        return label

    def set_account(self, name, region):
        self.identity.setText(' · '.join(part for part in (name, region) if part))

    def set_background_enabled(self, enabled):
        self._background_enabled = bool(enabled)
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        settings = getattr(self.window(), 'client_preferences', {})
        self.set_background_enabled(settings.get('background_enabled', self._background_enabled))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        size = 112 if self.width() >= 440 else 72
        if self.lock_button.width() != size:
            self.lock_button.setFixedSize(size, size)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            surface = QRectF(self.rect()).adjusted(5, 2, -7, -7)
            painter.setClipPath(panel_path(surface, self.cut))
            painter.setClipRect(surface.adjusted(3, 0, -3, -1), Qt.ClipOperation.IntersectClip)
            if self._background_enabled and self._art.paint(painter, surface):
                shade = QLinearGradient(surface.topLeft(), surface.topRight())
                shade.setColorAt(0, QColor(10, 28, 52, 248))
                shade.setColorAt(.6, QColor(19, 52, 87, 232))
                shade.setColorAt(1, QColor(18, 53, 83, 179))
                painter.fillRect(surface, shade)
        finally:
            painter.end()
