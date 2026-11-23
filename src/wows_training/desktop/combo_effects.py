"""Glass combo surfaces and bounded feedback over Qt's original popup and model."""
from __future__ import annotations

from PySide6.QtCore import QAbstractAnimation, QEvent, QEasingCurve, QObject, QPointF, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QComboBox, QGraphicsOpacityEffect, QWidget, QSpinBox, QDoubleSpinBox, QStyle, QStyleOptionComboBox
from shiboken6 import isValid

from . import academy_palette as tone
from .motion import motion_enabled, subscribe_motion
from .tuning_controls import SpinFeedback,field_surface,glass_cap


POPUP_MS = 220
POPUP_STYLE = '''
QAbstractItemView {
    background:rgba(246,251,255,248); color:#12263D;
    border:1px solid #8AAAC8; border-top:2px solid #609FD2;
    padding:5px; outline:0; selection-background-color:#D4EAF9;
    selection-color:#113C66;
}
QAbstractItemView::item { padding:6px 10px; min-height:22px; border:0; border-left:2px solid transparent; }
QAbstractItemView::item:hover { background:#E6F3FC; }
QAbstractItemView::item:selected { background:#CCE5F7; color:#103E6B; border-left:2px solid #D84E99; }
QAbstractItemView::item:disabled { color:#73869A; }
'''


def _belongs_to(widget, window):
    while widget is not None and isValid(widget):
        if widget.property('tacticalSurface') is True:
            return False
        if widget.objectName() == 'replayPanel' and widget.property('academyChrome') is not True:
            return False
        if widget is window:
            return True
        widget = widget.parentWidget()
    return False


def _release_native_effect(app):
    if not isValid(app):
        return
    state = getattr(app,'_glass_combo_effect_state',None)
    if state is not None:
        state['owners'] = max(0,state['owners']-1)
        if not state['owners']:
            app.setEffectEnabled(Qt.UIEffect.UI_AnimateCombo,state['previous'])


class _PopupTrim(QWidget):
    def __init__(self, popup):
        super().__init__(popup)
        self.setObjectName('comboPopupTrim')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet('background:transparent;border:none;')
        self.progress = 1.

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        painter.setPen(QPen(QColor(tone.CYAN), 2))
        painter.drawLine(QPointF(2,2), QPointF(min(width-4,32+width*.38*self.progress),2))
        painter.setPen(QPen(QColor(tone.PINK), 1.2))
        painter.drawLine(QPointF(width-21,2),QPointF(width-3,20))
        painter.drawLine(QPointF(3,self.height()-2),QPointF(min(45,width-4),self.height()-2))
        painter.end()


class _ComboFeedback(QObject):
    def __init__(self, combo, controller):
        super().__init__(combo)
        self.combo, self.controller = combo, controller
        self.hover = 0.
        self.open_progress = 0.
        self._popup = None
        self._view = None
        self._viewport = None
        self._trim = None
        self._opacity = None
        self._uses_window_opacity = False
        self._editor = None
        self._metrics_model = None
        self._minimum_width = combo.minimumWidth()
        self.hover_animation = QVariantAnimation(self)
        self.hover_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.hover_animation.valueChanged.connect(self._hover_frame)
        self.popup_animation = QVariantAnimation(self)
        self.popup_animation.setDuration(POPUP_MS)
        self.popup_animation.setStartValue(0.)
        self.popup_animation.setEndValue(1.)
        self.popup_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.popup_animation.valueChanged.connect(self._popup_frame)
        self.popup_animation.finished.connect(self._popup_finished)
        self.combo.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.combo.installEventFilter(self)
        subscribe_motion(combo, self._motion_changed)
        self.sync_metrics()
        self.ensure_popup()

    @property
    def running(self):
        return any(isValid(animation) and animation.state() == QAbstractAnimation.State.Running
                   for animation in (self.hover_animation,self.popup_animation))

    def sync_metrics(self):
        """Leave room for short filter captions without widening account candidates."""
        if not isValid(self.combo):
            return
        texts = [self.combo.itemText(i) for i in range(self.combo.count())]
        specified = bool(self.combo.minimumContentsLength())
        if not specified and (self.combo.isEditable() or len(texts) < 2):
            return
        if not texts or len(texts) > 24 or max(map(len,texts)) > (12 if specified else 7):
            return
        metrics = QFontMetrics(self.combo.font())
        width = max(metrics.horizontalAdvance(text) for text in texts)
        if width > metrics.horizontalAdvance('界') * 8:
            return
        if any(not self.combo.itemIcon(i).isNull() for i in range(self.combo.count())):
            width += 23
        width = max(self._minimum_width,self.combo.minimumWidth(),width+47)
        if self.combo.minimumWidth() != width:
            self.combo.setMinimumWidth(width)

    def ensure_popup(self):
        if not isValid(self.combo):
            return
        model = self.combo.model()
        if model is not self._metrics_model:
            if self._metrics_model is not None and isValid(self._metrics_model):
                for name in ('rowsInserted','rowsRemoved','modelReset','dataChanged'):
                    getattr(self._metrics_model,name).disconnect(self._model_changed)
            self._metrics_model = model
            for name in ('rowsInserted','rowsRemoved','modelReset','dataChanged'):
                getattr(model,name).connect(self._model_changed)
            self.sync_metrics()
        view = self.combo.view()
        popup = view.window()
        editor = self.combo.lineEdit()
        if editor is not None and editor is not self._editor:
            editor.setStyleSheet(editor.styleSheet()+'\nQLineEdit { background:transparent; border:0; padding:0; }')
            self._editor = editor
        if popup is self._popup and view is self._view:
            return
        self.stop()
        self._popup, self._view, self._viewport = popup, view, view.viewport()
        view.setStyleSheet(view.styleSheet()+'\n'+POPUP_STYLE)
        view.installEventFilter(self)
        self._viewport.installEventFilter(self)
        popup.installEventFilter(self)
        self._trim = _PopupTrim(popup)
        self._trim.hide()
        self._uses_window_opacity = QApplication.platformName() == 'windows'
        self._opacity = None
        if not self._uses_window_opacity:
            self._opacity = QGraphicsOpacityEffect(popup)
            self._opacity.setOpacity(1.)
            popup.setGraphicsEffect(self._opacity)

    def _model_changed(self,*_args):
        # Query filters are filled asynchronously with combo signals blocked.
        self.sync_metrics()

    def _hover_frame(self, value):
        self.hover = float(value)
        if isValid(self.combo):
            self.combo.update()

    def aim(self, inside):
        if not isValid(self.combo):
            return
        self.hover_animation.stop()
        destination = float(inside)
        if motion_enabled(self.combo) and self.combo.isVisible() and not self.combo.window().isMinimized():
            self.hover_animation.setDuration(250 if inside else 300)
            self.hover_animation.setStartValue(self.hover)
            self.hover_animation.setEndValue(destination)
            self.hover_animation.start()
        else:
            self.hover = destination
            self.combo.update()

    def _set_opacity(self, opacity):
        if self._popup is None or not isValid(self._popup):
            return
        if self._uses_window_opacity:
            self._popup.setWindowOpacity(opacity)
        elif self._opacity is not None and isValid(self._opacity):
            self._opacity.setOpacity(opacity)

    def _popup_frame(self, value):
        self.open_progress = float(value)
        self._set_opacity(.58+.42*self.open_progress)
        if self._trim is not None and isValid(self._trim):
            self._trim.progress = self.open_progress
            self._trim.update()
        if isValid(self.combo):
            self.combo.update()

    def _popup_finished(self):
        self.open_progress = 1.
        self._set_opacity(1.)
        if isValid(self.combo):
            self.combo.update()

    def show_popup(self):
        if not isValid(self.combo) or not isValid(self._popup) or not isValid(self._trim):
            return
        self.popup_animation.stop()
        self._trim.setGeometry(self._popup.rect())
        self._trim.show()
        self._trim.raise_()
        if motion_enabled(self.combo) and not self.combo.window().isMinimized():
            self._popup_frame(0.)
            self.popup_animation.start()
        else:
            self._popup_finished()

    def stop(self, *, reset_hover=False):
        for animation in (self.hover_animation,self.popup_animation):
            if isValid(animation):
                animation.stop()
        if reset_hover:
            self.hover = 0.
        self.open_progress = 0.
        self._set_opacity(1.)
        if self._trim is not None and isValid(self._trim):
            self._trim.hide()
        if isValid(self.combo):
            self.combo.update()

    def _motion_changed(self, enabled):
        if not isValid(self.combo):
            return
        if not enabled:
            popup_visible = self._popup is not None and isValid(self._popup) and self._popup.isVisible()
            self.stop(reset_hover=True)
            if popup_visible:
                self._popup_finished()
                if self._trim is not None and isValid(self._trim):
                    self._trim.show()

    def draw_combo(self):
        painter = QPainter(self.combo)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.combo.rect()).adjusted(1.1,1.3,-1.8,-2.3)
        enabled = self.combo.isEnabled()
        hovered = self.hover if enabled else 0.
        opened = self._popup is not None and isValid(self._popup) and self._popup.isVisible()
        emphasis = max(hovered,self.open_progress if opened else 0.)
        path=field_surface(painter,rect,emphasis,enabled,self.combo.hasFocus() or opened)
        rtl = self.combo.layoutDirection() == Qt.LayoutDirection.RightToLeft
        option=QStyleOptionComboBox()
        self.combo.initStyleOption(option)
        arrow=self.combo.style().subControlRect(QStyle.CC_ComboBox,option,QStyle.SC_ComboBoxArrow,self.combo)
        cap=QRectF(arrow)
        cap.setTop(rect.top()+1.);cap.setBottom(rect.bottom()-1.)
        if rtl: cap.setLeft(rect.left()+1.)
        else: cap.setRight(rect.right()-1.)
        glass_cap(painter,path,cap,emphasis,rtl)
        arrow_x = arrow.center().x()
        painter.setPen(QPen(QColor('#24577B' if enabled else '#899AA9'),1.55))
        painter.save()
        painter.translate(arrow_x,self.combo.height()/2)
        painter.rotate(180*self.open_progress if opened else 0)
        chevron = QPainterPath()
        chevron.moveTo(-4,-2);chevron.lineTo(0,2);chevron.lineTo(4,-2)
        painter.drawPath(chevron)
        painter.restore()
        if not self.combo.isEditable():
            text_rect = QRectF(36 if rtl else 11,3,max(1,self.combo.width()-47),max(1,self.combo.height()-7))
            icon = self.combo.itemIcon(self.combo.currentIndex())
            if not icon.isNull():
                size = min(18,self.combo.height()-10)
                icon.paint(painter,round(text_rect.left()),round(text_rect.center().y()-size/2),size,size)
                text_rect.adjust(size+5,0,0,0)
            painter.setFont(self.combo.font())
            painter.setPen(QColor(tone.INK if enabled else '#657C90'))
            caption = self.combo.currentText() if self.combo.currentIndex() >= 0 else self.combo.placeholderText()
            caption = QFontMetrics(self.combo.font()).elidedText(caption,Qt.TextElideMode.ElideRight,round(text_rect.width()))
            painter.drawText(text_rect,Qt.AlignmentFlag.AlignVCenter|(Qt.AlignmentFlag.AlignRight if rtl else Qt.AlignmentFlag.AlignLeft),caption)
        painter.end()

    def eventFilter(self, watched, event):
        # Popup views are destroyed before the combo/controller during teardown.
        # Cache the viewport instead of calling view.viewport() from late events.
        if not isValid(self.combo) or not isValid(watched):
            return False
        kind = event.type()
        if watched is self.combo:
            if kind == QEvent.Type.Paint:
                self.ensure_popup()
                self.draw_combo()
                return True
            if kind == QEvent.Type.Enter:
                self.aim(True)
            elif kind == QEvent.Type.Leave:
                self.aim(False)
            elif kind in (QEvent.Type.FocusIn,QEvent.Type.FocusOut):
                self.combo.update()
            elif kind in (QEvent.Type.Hide,QEvent.Type.EnabledChange):
                self.stop(reset_hover=True)
            elif kind in (QEvent.Type.Show,QEvent.Type.FontChange,QEvent.Type.StyleChange):
                self.sync_metrics()
        elif watched is self._popup:
            if kind == QEvent.Type.Show:
                self.show_popup()
            elif kind in (QEvent.Type.Hide,QEvent.Type.Close):
                self.stop()
            elif kind == QEvent.Type.Resize and self._trim is not None and isValid(self._trim):
                self._trim.setGeometry(self._popup.rect())
        if watched in (self._view,self._viewport,self._popup):
            if kind in (QEvent.Type.KeyPress,QEvent.Type.MouseButtonPress,QEvent.Type.Wheel):
                self.popup_animation.stop()
                self._popup_finished()
        return False


class ComboEffects(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.bindings = {}
        app = QApplication.instance()
        if app is not None:
            # Qt's Windows roll effect first hides the real list behind a
            # screenshot for 150 ms. Replace only that process-local combo
            # effect so our original, live popup is usable from its first frame.
            state = getattr(app,'_glass_combo_effect_state',None)
            if state is None:
                state = app._glass_combo_effect_state = {'owners':0,'previous':False}
            if not state['owners']:
                state['previous'] = app.isEffectEnabled(Qt.UIEffect.UI_AnimateCombo)
            state['owners'] += 1
            app.setEffectEnabled(Qt.UIEffect.UI_AnimateCombo,False)
            self.destroyed.connect(lambda _obj=None,owner=app:_release_native_effect(owner))
            app.installEventFilter(self)
        self.refresh()

    @property
    def active_count(self):
        return sum(binding.running for binding in self.bindings.values() if isValid(binding))

    def refresh(self):
        if not isValid(self.window):
            return
        for kind in (QComboBox,QSpinBox,QDoubleSpinBox):
            for control in self.window.findChildren(kind):
                self._bind(control)

    def _bind(self, combo):
        if isValid(combo) and isValid(self.window) and combo not in self.bindings and _belongs_to(combo,self.window):
            binding = _ComboFeedback(combo,self) if isinstance(combo,QComboBox) else SpinFeedback(combo)
            self.bindings[combo] = binding
            combo.destroyed.connect(lambda _obj=None, target=combo:self.bindings.pop(target,None))

    def finish_all(self):
        for binding in list(self.bindings.values()):
            if isValid(binding):
                binding.stop(reset_hover=True)

    def eventFilter(self, watched, event):
        if not isValid(self.window) or not isValid(watched):
            return False
        kind = event.type()
        if watched is self.window and (kind in (QEvent.Type.Hide,QEvent.Type.Close)
                or kind == QEvent.Type.WindowStateChange and self.window.isMinimized()):
            self.finish_all()
        if isinstance(watched,(QComboBox,QSpinBox,QDoubleSpinBox)) and kind in (QEvent.Type.Show,QEvent.Type.Paint):
            self._bind(watched)
        return False


def install_combo_effects(window):
    """Install once after UI construction. Newly shown dialog combos join automatically."""
    controller = getattr(window,'_combo_effects',None)
    if controller is None:
        controller = window._combo_effects = ComboEffects(window)
    else:
        controller.refresh()
    return controller
