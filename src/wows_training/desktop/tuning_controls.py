"""Paint numeric inputs and dropdowns as glass instruments over Qt's live editors."""
from PySide6.QtCore import QEvent,QEasingCurve,QObject,QPointF,QRectF,Qt,QVariantAnimation
from PySide6.QtGui import QColor,QLinearGradient,QPainter,QPainterPath,QPen
from PySide6.QtWidgets import QAbstractSpinBox,QStyle,QStyleOptionSpinBox
from shiboken6 import isValid

from . import academy_palette as tone
from .motion import motion_enabled,subscribe_motion


def field_surface(painter,rect,emphasis,enabled,focus):
    cut=min(7.,rect.height()*.23)
    path=QPainterPath()
    path.moveTo(rect.left()+2,rect.top())
    path.lineTo(rect.right()-cut,rect.top())
    path.lineTo(rect.right(),rect.top()+cut)
    path.lineTo(rect.right(),rect.bottom())
    path.lineTo(rect.left()+cut,rect.bottom())
    path.lineTo(rect.left(),rect.bottom()-cut)
    path.lineTo(rect.left(),rect.top()+2)
    path.closeSubpath()
    painter.fillPath(path.translated(.6,1.2),QColor(28,51,79,49))
    fill=QLinearGradient(rect.topLeft(),rect.bottomRight())
    fill.setColorAt(0,QColor(252,254,255,245))
    fill.setColorAt(.64,QColor(224,238,250,round(212+25*emphasis)))
    fill.setColorAt(1,QColor(195,215,234,226))
    painter.fillPath(path,fill)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    edge=QColor(141-round(70*emphasis),169-round(22*emphasis),193+round(8*emphasis))
    painter.setPen(QPen(edge if enabled else QColor('#B6C5D2'),1.))
    painter.drawPath(path)
    painter.setPen(QPen(QColor(255,255,255,200),.8))
    painter.drawLine(rect.topLeft()+QPointF(4,1),rect.topRight()+QPointF(-cut-1,1))
    if focus:
        painter.setPen(QPen(QColor(tone.PINK),1.65))
        painter.drawLine(QPointF(rect.left()+cut+2,rect.bottom()),QPointF(rect.left()+min(rect.width()*.55,56.),rect.bottom()))
    return path


def glass_cap(painter,surface,cap,emphasis,rtl=False):
    """Shaded control plate, clipped inside the same folded outline as the field."""
    painter.save()
    painter.setClipPath(surface)
    plate=QPainterPath()
    left,right=cap.left(),cap.right()
    offset=min(4.,cap.width()/5)
    slope=right if rtl else left
    lower=slope+offset if rtl else slope-offset
    outer=left if rtl else right
    plate.moveTo(slope,cap.top());plate.lineTo(outer,cap.top())
    plate.lineTo(outer,cap.bottom());plate.lineTo(lower,cap.bottom());plate.closeSubpath()
    wash=QLinearGradient(cap.topRight(),cap.bottomLeft())
    wash.setColorAt(0,QColor(93,166,218,round(61+62*emphasis)))
    wash.setColorAt(.52,QColor(161,206,234,round(32+40*emphasis)))
    wash.setColorAt(1,QColor(116,164,203,round(34+29*emphasis)))
    painter.fillPath(plate,wash)
    painter.setPen(QPen(QColor(68,121,161,round(96+86*emphasis)),1.))
    painter.drawLine(QPointF(slope,cap.top()),QPointF(lower,cap.bottom()))
    painter.setPen(QPen(QColor(13,128,182,round(136+77*emphasis)),1.3))
    direction=-1 if rtl else 1
    painter.drawLine(QPointF(lower+direction*1.8,cap.bottom()-1.4),
                     QPointF(lower+direction*(5+3*emphasis),cap.bottom()-1.4))
    if cap.width() >= 17. and cap.height() >= 12.:
        # A fine return edge catches the light without crossing the arrow's centre.
        painter.setPen(QPen(QColor(255,255,255,round(113+66*emphasis)),.8))
        painter.drawLine(QPointF(slope+direction*4.,cap.top()+2.),
                         QPointF(outer-direction*4.,cap.top()+2.))
    painter.restore()


class SpinFeedback(QObject):
    def __init__(self,spin):
        super().__init__(spin)
        self.spin=spin
        self.hover=0.
        self.active=QStyle.SC_None
        self.arrow_tint=0.
        self.pressed=QStyle.SC_None
        self.animation=QVariantAnimation(self)
        self.animation.setDuration(180)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.animation.valueChanged.connect(self._frame)
        self.arrow_animation=QVariantAnimation(self)
        self.arrow_animation.setDuration(135)
        self.arrow_animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.arrow_animation.valueChanged.connect(self._arrow_frame)
        spin.setAttribute(Qt.WidgetAttribute.WA_Hover)
        spin.lineEdit().setStyleSheet(spin.lineEdit().styleSheet()+
            '\nQLineEdit { background:transparent; border:0; padding:0; }')
        spin.installEventFilter(self)
        subscribe_motion(spin,self._motion_changed)

    @property
    def running(self):
        return any(item.state()==QVariantAnimation.State.Running
                   for item in (self.animation,self.arrow_animation))

    def _frame(self,value):
        self.hover=float(value)
        if isValid(self.spin): self.spin.update()

    def aim(self,target):
        self.animation.stop()
        if motion_enabled(self.spin) and self.spin.isVisible() and not self.spin.window().isMinimized():
            self.animation.setStartValue(self.hover)
            self.animation.setEndValue(target)
            self.animation.start()
        else:
            self._frame(target)

    def _motion_changed(self,enabled):
        if not enabled: self.stop()

    def _arrow_frame(self,value):
        self.arrow_tint=float(value)
        if isValid(self.spin): self.spin.update()

    def _active_arrow(self,control):
        if control==self.active:return
        self.active=control
        target={QStyle.SC_SpinBoxUp:-1.,QStyle.SC_SpinBoxDown:1.}.get(control,0.)
        self.arrow_animation.stop()
        if motion_enabled(self.spin) and self.spin.isVisible() and not self.spin.window().isMinimized():
            self.arrow_animation.setStartValue(self.arrow_tint)
            self.arrow_animation.setEndValue(target)
            self.arrow_animation.start()
        else:
            self._arrow_frame(target)

    def stop(self,*,reset_hover=True):
        self.animation.stop()
        self.arrow_animation.stop()
        self.arrow_tint=0.
        self.active=self.pressed=QStyle.SC_None
        self._frame(0.)

    def _option(self):
        option=QStyleOptionSpinBox()
        self.spin.initStyleOption(option)
        return option

    def draw(self):
        spin=self.spin
        option=self._option()
        painter=QPainter(spin)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect=QRectF(spin.rect()).adjusted(.8,1.,-1.1,-1.7)
        emphasis=self.hover if spin.isEnabled() else 0.
        surface=field_surface(painter,rect,emphasis,spin.isEnabled(),spin.hasFocus())
        if option.buttonSymbols!=QAbstractSpinBox.ButtonSymbols.NoButtons:
            style=spin.style()
            up=QRectF(style.subControlRect(QStyle.CC_SpinBox,option,QStyle.SC_SpinBoxUp,spin))
            down=QRectF(style.subControlRect(QStyle.CC_SpinBox,option,QStyle.SC_SpinBoxDown,spin))
            cap=up.united(down)
            cap.setTop(rect.top()+1.);cap.setBottom(rect.bottom()-1.)
            rtl=spin.layoutDirection()==Qt.LayoutDirection.RightToLeft
            glass_cap(painter,surface,cap,emphasis,rtl)
            painter.setClipPath(surface)
            for control,area,flag,direction in (
                    (QStyle.SC_SpinBoxUp,up,QAbstractSpinBox.StepUpEnabled,-1),
                    (QStyle.SC_SpinBoxDown,down,QAbstractSpinBox.StepDownEnabled,1)):
                available=spin.isEnabled() and not spin.isReadOnly() and bool(option.stepEnabled & flag)
                pressed=available and self.pressed==control and self.active==control
                light = max(0.,self.arrow_tint*direction) if available else 0.
                if light or pressed:
                    chip=QColor('#2388CF' if direction<0 else '#C34790')
                    chip.setAlpha(round(30*light)+32*pressed)
                    painter.fillRect(area.adjusted(1,0,-1,0),chip)
                center=area.center()+QPointF(0,1 if pressed else 0)
                ink='#155278' if available else '#94A7B8'
                painter.setPen(QPen(QColor(ink),1.4,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
                if option.buttonSymbols==QAbstractSpinBox.ButtonSymbols.PlusMinus:
                    painter.drawLine(center+QPointF(-3,0),center+QPointF(3,0))
                    if direction<0:painter.drawLine(center+QPointF(0,-3),center+QPointF(0,3))
                else:
                    mark=QPainterPath(center+QPointF(-3,-direction*1.25))
                    mark.lineTo(center+QPointF(0,direction*1.65))
                    mark.lineTo(center+QPointF(3,-direction*1.25))
                    painter.drawPath(mark)
        painter.end()

    def eventFilter(self,watched,event):
        if not isValid(self.spin): return False
        kind=event.type()
        if kind==QEvent.Type.Paint:
            self.draw()
            return True
        if kind==QEvent.Type.Enter and self.spin.isEnabled():
            self.aim(1.)
        elif kind==QEvent.Type.Leave:
            self._active_arrow(QStyle.SC_None)
            self.aim(0.)
        elif kind in (QEvent.Type.HoverMove,QEvent.Type.MouseButtonPress):
            self._active_arrow(self.spin.style().hitTestComplexControl(QStyle.CC_SpinBox,self._option(),event.position().toPoint(),self.spin))
            if kind==QEvent.Type.MouseButtonPress and event.button()==Qt.MouseButton.LeftButton:
                self.pressed=self.active
            self.spin.update()
        elif kind==QEvent.Type.MouseButtonRelease:
            self.pressed=QStyle.SC_None
            self.spin.update()
        elif kind in (QEvent.Type.Hide,QEvent.Type.EnabledChange):
            self.stop()
        return False
