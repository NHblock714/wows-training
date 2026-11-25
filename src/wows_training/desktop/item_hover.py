"""A short, pointer-transparent focus trace for archive lists and tables."""

from PySide6.QtCore import QEasingCurve, QEvent, QPointF, QRect, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QLinearGradient, QMouseEvent, QPainter
from PySide6.QtWidgets import QAbstractItemView, QWidget

from .motion import motion_enabled, subscribe_motion


class ItemHover(QWidget):
    def __init__(self, view):
        viewport = view.viewport()
        super().__init__(viewport)
        self.view = view
        self._position = None
        self._strength = 0.
        self._background_enabled = getattr(view.window(), 'client_preferences', {}).get('background_enabled', True)
        self.setProperty('backgroundEffect', True)
        self.setObjectName('archiveItemHover')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._sweep = QVariantAnimation(self)
        self._sweep.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._sweep.valueChanged.connect(self._paint_frame)
        self._sweep.finished.connect(self._hide_when_faded)
        subscribe_motion(view, self._motion_changed)
        viewport.setMouseTracking(True)
        viewport.installEventFilter(self)
        for bar in (view.verticalScrollBar(), view.horizontalScrollBar()):
            bar.valueChanged.connect(self._update_target)
        for signal in (view.model().modelReset, view.model().layoutChanged, view.model().rowsRemoved):
            signal.connect(self._reset)
        self.hide()

    def set_background_enabled(self, enabled):
        self._background_enabled = bool(enabled)
        if not enabled:
            self._reset()

    def _reset(self, *_):
        self._position = None
        self._sweep.stop()
        self._strength = 0.
        self.hide()

    def _motion_changed(self, enabled):
        if not enabled:
            self._reset()

    def _paint_frame(self, strength):
        self._strength = float(strength)
        self.update()

    def _hide_when_faded(self):
        if self._strength == 0.:
            self.hide()

    def _animate(self, end, duration, *, start=None):
        self._sweep.stop()
        self._sweep.setStartValue(self._strength if start is None else start)
        self._sweep.setEndValue(end)
        self._sweep.setDuration(duration)
        self._sweep.start()

    def _update_target(self, *_):
        if (not self._background_enabled or not self.view.isEnabled() or not motion_enabled(self.view)
                or self.view.window().isMinimized()):
            return
        viewport = self.parentWidget()
        index = self.view.indexAt(self._position) if self._position is not None else None
        if index is None or not index.isValid() or not index.flags() & Qt.ItemFlag.ItemIsEnabled:
            if self.isVisible() and self._sweep.endValue() != 0.:
                self._animate(0., 220)
            return

        box = self.view.visualRect(index)
        if self.view.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectRows:
            box = QRect(0, box.top(), viewport.width(), box.height())
        box = box.intersected(viewport.rect())
        if box.width() < 32 or box.height() < 14:
            self.hide()
            return
        if self.isVisible() and self.geometry() == box and self._sweep.endValue() == 1.:
            return
        self.setGeometry(box)
        self.raise_()
        self.show()
        self._animate(1., 245, start=min(.21, self._strength))

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QEvent.Type.MouseMove and isinstance(event,QMouseEvent):
            self._position = event.position().toPoint()
            self._update_target()
        elif kind == QEvent.Type.Leave:
            self._position = None
            self._update_target()
        elif kind == QEvent.Type.Hide:
            self._reset()
        elif kind == QEvent.Type.EnabledChange and not self.view.isEnabled():
            self._reset()
        elif kind == QEvent.Type.Resize:
            self._update_target()
        return False

    def paintEvent(self, event):
        painter = QPainter(self)
        width, height = self.width(), self.height()
        strength = self._strength
        end = min(width-4, max(26., width*(.20 + .51*strength)))
        line = QLinearGradient(QPointF(2,0),QPointF(end,0))
        line.setColorAt(0,QColor(19,110,181,round(152*strength)))
        line.setColorAt(.70,QColor(77,177,232,round(81*strength)))
        line.setColorAt(1,QColor(238,104,172,0))
        painter.fillRect(QRectF(2,height-2,end-2,1),line)
        painter.fillRect(QRectF(1,4,2,max(3,height-8)),QColor(24,114,180,round(124*strength)))
        painter.end()
