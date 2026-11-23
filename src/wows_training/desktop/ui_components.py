"""Compact disclosure and table presentation shared by desktop workflows."""
import re
from PySide6.QtCore import QEasingCurve,QEvent,QSize,Qt,QVariantAnimation
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame,QLabel,QToolButton,QVBoxLayout,QWidget
from .motion import motion_enabled,subscribe_motion


class _DisclosureViewport(QWidget):
    """Clip a full-size form instead of squeezing its input rows during reveal."""
    def __init__(self,body,parent=None):
        super().__init__(parent);self.body=body;body.setParent(self)
        self.setSizePolicy(body.sizePolicy());self.clipping=False

    def natural_height(self):
        layout=self.body.layout()
        if layout is not None:layout.activate()
        return self.heightForWidth(max(1,self.width()))

    def hasHeightForWidth(self):
        return self.body.hasHeightForWidth()

    def heightForWidth(self,width):
        height=self.body.heightForWidth(width) if self.body.hasHeightForWidth() else self.body.sizeHint().height()
        return max(self.body.minimumHeight(),min(self.body.maximumHeight(),max(1,height,self.body.minimumSizeHint().height())))

    def sizeHint(self):
        return QSize(max(0,self.body.sizeHint().width()),self.natural_height())

    def minimumSizeHint(self):
        hint=self.body.minimumSizeHint()
        return QSize(max(0,hint.width()),0 if self.clipping else max(0,hint.height()))

    def sync_body(self):
        self.body.setGeometry(0,0,self.width(),max(self.height(),self.natural_height()))

    def resizeEvent(self,event):
        self.sync_body();super().resizeEvent(event)

    def event(self,event):
        handled=super().event(event)
        if event.type()==QEvent.Type.LayoutRequest and hasattr(self,'body'):
            self.updateGeometry();self.sync_body()
        return handled


class StatusLabel(QLabel):
    """Show a feedback line only while a workflow has a message."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.hide()

    def setText(self, text):
        super().setText(text)
        self.setVisible(bool(text))


class Disclosure(QFrame):
    def __init__(self,title,content,parent=None,expanded=False):
        super().__init__(parent);self.setObjectName('disclosure')
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(6)
        self.toggle_button=QToolButton();self.toggle_button.setObjectName('disclosureToggle')
        self.toggle_button.setText(title);self.toggle_button.setCheckable(True)
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.body=QLabel(content) if isinstance(content,str) else content
        if isinstance(self.body,QLabel):self.body.setWordWrap(True);self.body.setObjectName('helpText')
        self._viewport=_DisclosureViewport(self.body)
        layout.addWidget(self.toggle_button);layout.addWidget(self._viewport)
        self._expanded=bool(expanded);self._body_bounds=None;self._motion_bound=False;self._motion_window=None
        self._height_motion=QVariantAnimation(self)
        self._height_motion.setDuration(180);self._height_motion.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._height_motion.valueChanged.connect(self._height_frame)
        self._height_motion.finished.connect(self._finish_motion)
        self.toggle_button.toggled.connect(self._toggle);self.setExpanded(expanded)

    def _toggle(self,expanded,animate=True):
        self._expanded=bool(expanded)
        self.toggle_button.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self._height_motion.stop()
        if not animate or not self.isVisible() or not motion_enabled(self) or self.window().isMinimized():
            self._finish_motion();return
        current=self._viewport.height() if self._viewport.isVisible() else 0
        if self._body_bounds is None:
            self._body_bounds=(self.body.minimumHeight(),self.body.maximumHeight())
        self._viewport.clipping=True
        self._viewport.setMinimumHeight(0)
        self._viewport.setMaximumHeight(current)
        self._viewport.setVisible(True)
        self.body.setVisible(True)
        self.layout().activate()
        target=self._expanded_height() if expanded else 0
        self._viewport.sync_body()
        if current==target:
            self._finish_motion();return
        self._height_motion.setStartValue(current);self._height_motion.setEndValue(target)
        self._height_motion.start()

    def _expanded_height(self):
        return self._viewport.natural_height()

    def _height_frame(self,value):
        self._viewport.setMaximumHeight(max(0,round(value)))

    def _finish_motion(self):
        self._height_motion.stop()
        self._body_bounds=None;self._viewport.clipping=False
        self._viewport.setMaximumHeight(self.body.maximumHeight())
        self._viewport.setMinimumHeight(self.body.minimumHeight())
        self.body.setVisible(self._expanded)
        self._viewport.setVisible(self._expanded)
        self._viewport.sync_body()

    def _motion_preference(self,enabled):
        if not enabled:self._finish_motion()

    def showEvent(self,event):
        super().showEvent(event)
        if not self._motion_bound:self._motion_bound=subscribe_motion(self,self._motion_preference)
        window=self.window()
        if self._motion_window is not window:
            if self._motion_window is not None:self._motion_window.removeEventFilter(self)
            self._motion_window=window;window.installEventFilter(self)

    def hideEvent(self,event):
        self._finish_motion();super().hideEvent(event)

    def eventFilter(self,watched,event):
        if watched is self._motion_window and event.type() in (QEvent.Type.Hide,QEvent.Type.Close,QEvent.Type.WindowStateChange):
            if event.type()!=QEvent.Type.WindowStateChange or watched.isMinimized():self._finish_motion()
        return super().eventFilter(watched,event)

    def setExpanded(self,expanded):
        # Programmatic reveals are immediate for focus, scrolling and playback flows.
        blocked=self.toggle_button.blockSignals(True)
        self.toggle_button.setChecked(expanded)
        self.toggle_button.blockSignals(blocked)
        self._toggle(expanded,animate=False)


def align_cell(item,value):
    text=item.text().strip()
    if value is None or text in ('未知','—','未确认','待确认'):
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter);item.setForeground(QColor('#50667B'))
    elif type(value) in (int,float) or re.fullmatch(r'[-+]?\d[\d,]*(?:\.\d+)?(?:%|/\d+)?',text):
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
    else:item.setTextAlignment(Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter)


def style_table(table):
    table.setShowGrid(False);table.setWordWrap(False)
    table.verticalHeader().setDefaultSectionSize(36)
    table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter)
    table.setAlternatingRowColors(True)
