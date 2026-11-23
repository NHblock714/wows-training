"""Short presentation-only reveals shared by native data charts.

Values, labels and hit areas always use the final data. Only the painted fill
changes; report exporters continue to read the underlying analysis directly.
"""
from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QEvent, QObject, QVariantAnimation

from .motion import motion_enabled, subscribe_motion


class ChartReveal(QObject):
    def __init__(self, widget, duration=320):
        super().__init__(widget)
        self.widget = widget
        self.progress = 1.0
        self._signature = None
        self._pending = False
        self._has_content = False
        self._subscribed = False
        self._window = None
        self.animation = QVariantAnimation(self)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setDuration(duration)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self._advance)
        self.animation.finished.connect(self.finish)
        widget.installEventFilter(self)

    @property
    def running(self):
        return self.animation.state() == QAbstractAnimation.State.Running

    def set_content(self, content):
        signature = repr(content)
        if signature == self._signature:
            return
        self._signature = signature
        self._has_content = bool(content)
        self._pending = self._has_content
        self.animation.stop()
        self.progress = 1.0
        if self.widget.isVisible() and self._has_content:
            self._start()
        elif not self._has_content:
            self.finish()

    def _bind(self):
        if not self._subscribed:
            self._subscribed = subscribe_motion(self.widget, self._preference_changed)
        window = self.widget.window()
        if window is not self._window:
            if self._window is not None:
                self._window.removeEventFilter(self)
            self._window = window
            if window is not self.widget:
                window.installEventFilter(self)

    def _start(self):
        self._bind()
        if (not motion_enabled(self.widget) or not self.widget.isVisible()
                or self.widget.window().isMinimized() or not self._has_content):
            self.finish()
            return
        self._pending = False
        self.animation.stop()
        self.progress = 0.0
        self.animation.start()
        self.widget.update()

    def _advance(self, value):
        if not self.widget.isVisible() or self.widget.window().isMinimized():
            self.finish()
            return
        self.progress = float(value)
        self.widget.update()

    def finish(self):
        self.animation.stop()
        self._pending = False
        self.progress = 1.0
        self.widget.update()

    def _preference_changed(self, enabled):
        if not enabled:
            self.finish()

    def eventFilter(self, watched, event):
        if watched is self.widget:
            if event.type() == QEvent.Type.Show:
                self._bind()
                if self._pending:
                    self._start()
            elif event.type() in (QEvent.Type.Hide, QEvent.Type.Close):
                self.finish()
        if watched is self._window and event.type() == QEvent.Type.WindowStateChange:
            if self.widget.window().isMinimized():
                self.finish()
        return False
