"""Event-driven glass controls with the native tactical replay surface intact.

Overlays never intercept input or resize their targets. Finite interaction
feedback settles after use. Visible selected options share one slow stripe
clock, which stops when motion is disabled or its window is hidden/minimized.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile

from PySide6.QtCore import QAbstractAnimation, QElapsedTimer, QEvent, QEasingCurve, QObject, QPoint, QPointF, QRectF, Qt, QTimer, QVariantAnimation, Signal, Slot
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF, QRadialGradient
from PySide6.QtWidgets import QApplication, QPushButton, QStackedWidget, QStyle, QStyleOptionButton, QTabWidget, QWidget
from shiboken6 import isValid

from . import academy_palette as palette


SETTINGS_FILE = 'ui_motion.json'
HOVER_MS = 280
CLICK_MS = 440
PAGE_MS = 480
TAB_MS = 300


def _controller_for(widget):
    current = widget
    while current is not None and isValid(current):
        controller = getattr(current, '_motion_controller', None)
        if isinstance(controller, MotionController) and isValid(controller):
            return controller
        current = current.parent()
    return None


def motion_enabled(widget):
    """Read the host preference; standalone widgets retain their normal motion."""
    controller = _controller_for(widget)
    return controller.enabled if controller is not None else True


class _Subscription(QObject):
    def __init__(self, widget, callback):
        super().__init__(widget)
        self.callback = callback

    @Slot(bool)
    def dispatch(self, enabled):
        self.callback(enabled)


def subscribe_motion(widget, callback):
    """Subscribe once per callback, returning False until a host exists.

    Subscription lifetime is owned by the widget. A callback is called only
    when the preference changes, with the new boolean value.
    """
    controller = _controller_for(widget)
    if controller is None:
        return False
    subscriptions = getattr(widget, '_motion_subscriptions', None)
    if subscriptions is None:
        subscriptions = widget._motion_subscriptions = {}
    key = (controller, callback)
    if key not in subscriptions:
        subscription = _Subscription(widget, callback)
        controller.enabledChanged.connect(subscription.dispatch)
        subscriptions[key] = subscription
    return True


class _Overlay(QWidget):
    def __init__(self, target, name):
        super().__init__(target)
        self.setObjectName(name)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet('background:transparent; border:none;')
        self.setGeometry(target.rect())
        self.hide()

    def reveal(self):
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        self.update()


class _ButtonFeedback(_Overlay):
    def __init__(self, target, controller):
        super().__init__(target, 'motionButtonFeedback')
        self.controller = controller
        self.card = target.objectName() == 'homeShortcut'
        self.hover = 0.0
        self.focused = False
        self.pulse = 1.0
        self.acknowledgement = 1.0
        self.pressure = 0.0
        self.selection = float(target.isChecked())
        self.origin = QPointF(target.rect().center())
        self.pointer = QPointF(self.origin)
        self.pointer_animation = QVariantAnimation(self)
        self.pointer_animation.setDuration(105)
        self.pointer_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.pointer_animation.valueChanged.connect(self._pointer_frame)
        target.setMouseTracking(True)
        self.hover_animation = QVariantAnimation(self)
        self.hover_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.hover_animation.valueChanged.connect(self._hover_frame)
        self.hover_animation.finished.connect(self._settled)
        self.press_animation = QVariantAnimation(self)
        self.press_animation.setDuration(CLICK_MS)
        self.press_animation.setStartValue(0.0)
        self.press_animation.setEndValue(1.0)
        self.press_animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.press_animation.valueChanged.connect(self._press_frame)
        self.press_animation.finished.connect(self._settled)
        self.commit_animation = QVariantAnimation(self)
        self.commit_animation.setDuration(310)
        self.commit_animation.setStartValue(0.)
        self.commit_animation.setEndValue(1.)
        self.commit_animation.setEasingCurve(QEasingCurve.Type.OutQuad)
        self.commit_animation.valueChanged.connect(self._commit_frame)
        self.commit_animation.finished.connect(self._settled)
        target.clicked.connect(self._acknowledge)
        self.depth_animation = QVariantAnimation(self)
        self.depth_animation.valueChanged.connect(self._depth_frame)
        self.depth_animation.finished.connect(self._settled)
        self.selection_animation = QVariantAnimation(self)
        self.selection_animation.setDuration(260)
        self.selection_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.selection_animation.valueChanged.connect(self._selection_frame)
        self.selection_animation.finished.connect(self._settled)
        target.toggled.connect(self.select)

    def _selection_frame(self, value):
        self.selection = float(value)
        self.parentWidget().update()

    def select(self, checked):
        button = self.parentWidget()
        self.selection_animation.stop()
        destination = float(checked)
        if self.controller.enabled and button.isVisible() and not button.window().isMinimized():
            self.selection_animation.setStartValue(self.selection)
            self.selection_animation.setEndValue(destination)
            self.selection_animation.start()
        else:
            self.selection = destination
        self.controller._sync_selection_timer()
        button.update()

    def _hover_frame(self, value):
        self.hover = float(value)
        self.parentWidget().update()

    def _pointer_frame(self, point):
        self.pointer = QPointF(point)
        self.parentWidget().update()

    def follow_pointer(self, point):
        """Ease the reflection toward the live pointer, including fast reversals."""
        if not self.controller.enabled:
            return
        self.pointer_animation.stop()
        if not self.hover or (point-self.pointer).manhattanLength() < 1.:
            self._pointer_frame(point)
            return
        self.pointer_animation.setStartValue(self.pointer)
        self.pointer_animation.setEndValue(point)
        self.pointer_animation.start()

    def _commit_frame(self, value):
        self.acknowledgement = float(value)
        self.parentWidget().update()

    def _acknowledge(self):
        button = self.parentWidget()
        if not self.controller.enabled or not button.isVisible() or not button.isEnabled() or button.window().isMinimized():
            return
        self.commit_animation.stop()
        self.acknowledgement = 0.
        self.commit_animation.start()

    def _press_frame(self, value):
        self.pulse = float(value)
        self.parentWidget().update()

    def _depth_frame(self, value):
        self.pressure = float(value)
        self.parentWidget().update()

    def _settled(self):
        if self.hover <= .001 and not self.focused and not self.running:
            self.hide()

    @property
    def running(self):
        state = QAbstractAnimation.State.Running
        return any(animation.state() == state for animation in
                   (self.hover_animation, self.press_animation, self.depth_animation,
                    self.selection_animation, self.pointer_animation, self.commit_animation))

    def aim(self, hovered, animated=True):
        self.hover_animation.stop()
        destination = 1.0 if hovered else 0.0
        if animated and abs(destination - self.hover) > .001:
            enter, leave = (320, 390) if self.card else (HOVER_MS, 340)
            self.hover_animation.setDuration(enter if hovered else leave)
            self.hover_animation.setStartValue(self.hover)
            self.hover_animation.setEndValue(destination)
            self.reveal()
            self.hover_animation.start()
        else:
            self.hover = destination
            self.reveal() if self.hover or self.focused else self._settled()
        self.parentWidget().update()

    def focus(self, focused):
        self.focused = bool(focused)
        if focused:
            self.reveal()
        else:
            self.parentWidget().update()
            self._settled()

    def press(self, position):
        self.press_animation.stop()
        self.origin = QPointF(position)
        self.pulse = 0.0
        self.reveal()
        self.press_animation.start()
        self._depth_to(1.0, 140)

    def release(self):
        self._depth_to(0.0, 300)

    def _depth_to(self, target, duration):
        self.depth_animation.stop()
        self.depth_animation.setStartValue(self.pressure)
        self.depth_animation.setEndValue(target)
        self.depth_animation.setDuration(duration)
        self.depth_animation.setEasingCurve(QEasingCurve.Type.OutCubic if target else QEasingCurve.Type.OutBack)
        self.depth_animation.start()

    def finish(self, keep_focus=False):
        self.hover_animation.stop()
        self.pointer_animation.stop()
        self.commit_animation.stop()
        self.acknowledgement = 1.
        self.press_animation.stop()
        self.depth_animation.stop()
        self.selection_animation.stop()
        self.hover, self.pulse = 0.0, 1.0
        self.pressure = 0.0
        self.selection = float(self.parentWidget().isChecked())
        self.focused = bool(keep_focus)
        if self.focused:
            self.reveal()
        else:
            self.hide()
        self.parentWidget().update()

    def paintEvent(self, event):
        # The controller paints the surface in the button's own paint event.
        # This transparent child retains focus/input feedback ownership only.
        pass

    def paint_button(self):
        button = self.parentWidget()
        option = QStyleOptionButton()
        button.initStyleOption(option)
        option.state &= ~(QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_Sunken |
                          QStyle.StateFlag.State_HasFocus | QStyle.StateFlag.State_KeyboardFocusChange)
        painter = QPainter(button)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = button.isEnabled()
        name = button.objectName()
        is_navigation = name == 'nav'
        dark = name in ('primary', 'radarSave', 'nav', 'soundToggle') or button.isChecked()
        animated = motion_enabled(button)
        hover = (self.hover if animated else float(button.underMouse())) if active else 0.0
        pressure = self.pressure if animated else float(button.isDown())
        shift = -1.25 * hover + 1.5 * pressure
        # Leave room for both the lifted focus outline and the lower backing.
        # QWidget clips its own paint, so a contour above y=0 cannot be repaired
        # by changing sibling stacking order.
        box = QRectF(button.rect()).adjusted(1.2, 2.4, -2.1, -3.6)
        if is_navigation:
            inset = 1.0-self.selection
            box.adjust(3.0*inset, 2.0*inset, -3.0*inset, -2.0*inset)
        box.translate(0, shift)
        cut = min(13.0 if self.card else 10.0, max(4.0, box.height() * .22))
        path = QPainterPath()
        path.moveTo(box.left()+2.2, box.top())
        path.lineTo(box.right()-cut, box.top())
        path.lineTo(box.right(), box.top()+cut)
        path.lineTo(box.right(), box.bottom()-2.2)
        path.quadTo(box.right(), box.bottom(), box.right()-2.2, box.bottom())
        path.lineTo(box.left()+cut, box.bottom())
        path.lineTo(box.left(), box.bottom()-cut)
        path.lineTo(box.left(), box.top()+2.2)
        path.quadTo(box.left(), box.top(), box.left()+2.2, box.top())
        path.closeSubpath()
        shadow = QColor(palette.NIGHT)
        shadow.setAlpha((80 if dark else 36) if active else 20)
        painter.translate(1.1, 2.0-pressure)
        painter.fillPath(path, shadow)
        painter.translate(-1.1, -2.0+pressure)
        painter.save()
        painter.setClipPath(path)
        if is_navigation and active:
            painter.fillPath(path, QColor(16, 33, 52, 62))
        # Draw only the native bevel from a stable normal/selected state, then
        # blend the lighting before the text. The CSS hover state never snaps.
        if is_navigation and 0 < self.selection < 1:
            ordinary = QStyleOptionButton(option)
            ordinary.state &= ~QStyle.StateFlag.State_On
            button.style().drawControl(QStyle.ControlElement.CE_PushButtonBevel, ordinary, painter, button)
            selected = QStyleOptionButton(option)
            selected.state |= QStyle.StateFlag.State_On
            painter.save()
            painter.setOpacity(self.selection)
            button.style().drawControl(QStyle.ControlElement.CE_PushButtonBevel, selected, painter, button)
            painter.restore()
        elif self.card:
            self._paint_glass(painter, path, box, hover, active)
            painter.fillRect(QRectF(box.x()+12, box.y()+1, 30, 2), QColor(palette.BLUE))
        elif dark:
            button.style().drawControl(QStyle.ControlElement.CE_PushButtonBevel, option, painter, button)
        else:
            self._paint_glass(painter, path, box, hover, active)
            # Retain the client's fine bevel grain, without its opaque gray
            # center obscuring the work area's layered background.
            painter.save()
            painter.setOpacity(.13 if active else .08)
            button.style().drawControl(QStyle.ControlElement.CE_PushButtonBevel, option, painter, button)
            painter.restore()
        light = QLinearGradient(box.topLeft(), box.bottomRight())
        light.setColorAt(0, QColor(255, 255, 255, round(5+10*hover) if dark else round(18+43*hover)))
        light.setColorAt(.56, QColor(255, 255, 255, 0))
        light.setColorAt(1, QColor(11, 24, 41, round(10 + 22*max(0,pressure))))
        painter.fillPath(path, light)
        if hover > 0:
            glow = QRadialGradient(self.pointer, max(38., self.width() * .68))
            glow.setColorAt(0, QColor(255, 255, 255, round((15 if dark else 72)*hover)))
            glow.setColorAt(.44, QColor(69, 220, 244, round((15 if dark else 40)*hover)))
            glow.setColorAt(1, QColor(240, 87, 166, 0))
            painter.fillPath(path, glow)
            # An offset magenta layer appears along the lower diagonal edge.
            tint = QLinearGradient(box.bottomLeft(), box.topRight())
            tint.setColorAt(0, QColor(240, 87, 166, round((23 if dark else 36)*hover)))
            tint.setColorAt(.65, QColor(240, 87, 166, 0))
            painter.fillPath(path, tint)
        if active and name == 'homeImport':
            self._paint_stripes(painter, box, .56 + .44 * hover, .38 * hover)
        elif active and self.card:
            self._paint_stripes(painter, box, .26 + .62 * hover, .34 * hover)
        elif active and button.isCheckable() and button.text() and self.selection > .001:
            self._paint_stripes(painter, box, self.selection, self.controller.selection_phase)
        material = getattr(self.controller.window, 'client_preferences', {}).get('background_enabled', True)
        if material and active and not self.card and name not in ('homeImport', 'nav', 'soundToggle'):
            self._paint_folded_end(painter, box, option, hover, dark)
        if hover > .001:
            self._paint_corner_traces(painter, box, cut, hover, dark)
        if self.pulse < 1.0 and active:
            self._paint_press_feedback(painter, box, cut, dark)
        if self.acknowledgement < 1. and active:
            self._paint_commit(painter, box, cut, dark)
        painter.restore()
        edge = QColor(palette.CYAN if dark else '#42617F')
        edge.setAlpha(round(60 + 125 * hover) if active else 36)
        painter.setPen(QPen(edge, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        pink = QColor(palette.PINK)
        pink.setAlpha(round(65+160*hover) if active else 15)
        painter.setPen(QPen(pink, 1.7))
        painter.drawLine(QPointF(box.right()-cut-7,box.top()+.8), QPointF(box.right()-cut,box.top()+.8))
        painter.drawLine(QPointF(box.left()+1,box.bottom()-cut), QPointF(box.left()+cut,box.bottom()-1))
        if self.card:
            button.paint_card_content(painter, box, hover)
        else:
            painter.save()
            painter.translate(0, shift)
            label_option = QStyleOptionButton(option)
            label_option.rect = button.style().subElementRect(
                QStyle.SubElement.SE_PushButtonContents, option, button)
            button.style().drawControl(QStyle.ControlElement.CE_PushButtonLabel, label_option, painter, button)
            painter.restore()
        if self.focused and active:
            painter.setPen(QPen(QColor(palette.PINK),1.5,Qt.PenStyle.SolidLine))
            painter.drawPath(path)
        painter.end()

    @staticmethod
    def _paint_glass(painter, path, box, hover, active):
        """Thin ice/silver panes; the parent remains visible through the face."""
        face = QLinearGradient(box.topLeft(), box.bottomLeft())
        face.setColorAt(0, QColor(246, 252, 255, 174 if active else 112))
        face.setColorAt(.48, QColor(197, 221, 239, round(126+18*hover) if active else 88))
        face.setColorAt(1, QColor(214, 224, 236, 155 if active else 100))
        painter.fillPath(path, face)
        # Asymmetric laminated facets preserve the game's cut-corner language.
        facet = QPolygonF((box.topLeft(), QPointF(box.left()+box.width()*.55,box.top()),
                           QPointF(box.left()+box.width()*.38,box.bottom()), box.bottomLeft()))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255,255,255,round(31+24*hover) if active else 18))
        painter.drawPolygon(facet)
        painter.setPen(QPen(QColor(255,255,255,168 if active else 86),.8))
        painter.drawLine(box.topLeft()+QPointF(10,1.2),box.topRight()-QPointF(16,-1.2))
        painter.setPen(QPen(QColor(87,171,215,round(70+70*hover) if active else 25),1.3))
        painter.drawLine(box.bottomLeft()+QPointF(13,-1.5),box.bottomLeft()+QPointF(29,-1.5))

    def _paint_folded_end(self, painter, box, option, hover, dark):
        """Fit a folded laminate into the spare space alongside the native label."""
        if not option.text or box.height() < 18:
            return
        label = option.fontMetrics.size(Qt.TextFlag.TextShowMnemonic, option.text).width()
        if not option.icon.isNull():
            label += option.iconSize.width() + 4
        margin = 4.
        if option.features & QStyleOptionButton.ButtonFeature.HasMenu:
            button = self.parentWidget()
            margin += button.style().pixelMetric(QStyle.PixelMetric.PM_MenuButtonIndicator, option, button)
        room = min(144., box.width() * .35, (box.width() - label) / 2 - margin - 7.)
        if room < 19.:
            return

        painter.save()
        if option.direction == Qt.LayoutDirection.RightToLeft:
            painter.translate(box.left() + margin, box.top())
            painter.scale(-1., 1.)
        else:
            painter.translate(box.right() - margin, box.top())
        height = box.height()
        # The hinge opens from the outside in. Both faces stay clear of the text
        # at their maximum extension, including during a reversed hover.
        span = room * (.78 + .22 * hover)
        lean = min(span * .57, height * .82)
        top = QPointF(-span + lean, 0)
        foot = QPointF(-span, height)
        face = QLinearGradient(foot, QPointF(0, height))
        face.setColorAt(0., QColor(97, 194, 244, round(35 + 40 * hover)))
        face.setColorAt(.62, QColor(32, 114, 185, round(24 + 22 * hover)))
        face.setColorAt(1., QColor(105, 170, 220, 6))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(face)
        painter.drawPolygon(QPolygonF([top, QPointF(0, 0), QPointF(0, height), foot]))
        fold = QPointF(-span * .32, height)
        painter.setBrush(QColor(216, 237, 255, round((14 if dark else 46) + 24 * hover)))
        painter.drawPolygon(QPolygonF([top, fold, foot]))
        seam = QColor('#9BEAF7' if dark else '#397EB2')
        seam.setAlpha(round(63 + 62 * hover))
        painter.setPen(QPen(seam, .85))
        painter.drawLine(top, foot)
        if span >= 32.:
            # A recessed second seam separates the transparent leaf from its bevel.
            along = top - foot
            inset = QPointF(4., 0.)
            reflection = QColor('#DBF1FB' if dark else '#FFFFFF')
            reflection.setAlpha(round(42 + 66 * hover))
            painter.setPen(QPen(reflection, .85))
            painter.drawLine(foot + along * .17 + inset, foot + along * .87 + inset)
            plate_end = QPointF(-3., height * .26)
            keyline = QPainterPath(top + QPointF(7., 3.))
            keyline.lineTo(plate_end + QPointF(-8., 0.))
            keyline.lineTo(plate_end)
            painter.setPen(QPen(seam, .65))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(keyline)
        gleam = math.sin(math.pi * hover)
        if gleam > .01:
            along = top - foot
            start = foot + along * max(0., hover - .18)
            tip = foot + along * hover
            painter.setPen(QPen(QColor(245, 253, 255, round(215 * gleam)), 1.4))
            painter.drawLine(start, tip)

        if room >= 54.:
            outer = -box.width() + margin + 12.
            y = height * .54
            end = outer + room * (.26 + .16 * hover)
            trace = QPainterPath(QPointF(outer, y + 3.))
            trace.lineTo(end - 4., y + 3.)
            trace.lineTo(end, y - 1.)
            trace.lineTo(end + 7., y - 1.)
            painter.setPen(QPen(seam, .8))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(trace)
            painter.fillRect(QRectF(end + 9., y - 2., 2., 2.), seam)

        # Short etched marks follow the lower edge of the sheet. The last
        # magenta mark catches the light as the adjacent hinge unfolds.
        count = min(9, int(span / 7))
        for index in range(count):
            x = -5. - index * 5.
            ink = QColor(palette.PINK) if index == 0 else QColor(seam)
            ink.setAlpha(round((118 if index == 0 else 66) + 62 * hover))
            painter.setPen(QPen(ink, 1.25))
            painter.drawLine(QPointF(x - 2., height - 4.), QPointF(x, height - 7.))
        painter.restore()

    @staticmethod
    def _paint_corner_traces(painter, box, cut, hover, dark):
        """Two angled conductors enter along the bezel, away from the label."""
        length = min(28., box.width()*.19)*hover
        top = box.top()+3.
        right = box.right()-cut-4.
        left = box.left()+cut+4.
        bottom = box.bottom()-3.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for x,y,direction,ink in ((right,top,-1,palette.CYAN if dark else '#1275B0'),
                                 (left,bottom,1,palette.PINK if dark else '#B73E87')):
            trace = QPainterPath(QPointF(x-direction*2.,y-direction*2.))
            trace.lineTo(x,y)
            trace.lineTo(x+direction*length,y)
            colour = QColor(ink)
            colour.setAlpha(round(175*hover))
            painter.setPen(QPen(colour,1.15))
            painter.drawPath(trace)
            painter.fillRect(QRectF(x+direction*length-1.,y-.9,2.,1.8),colour)

    def _paint_press_feedback(self, painter, box, cut, dark):
        """Light spreads from the contact towards both edges of the glass."""
        progress = self.pulse
        envelope = math.sin(math.pi*progress)
        if envelope <= .001:
            return
        radius = max(28.,max(abs(box.left()-self.origin.x()),abs(box.right()-self.origin.x())))
        spot = QRadialGradient(self.origin, 13.+radius*progress)
        spot.setColorAt(0,QColor(214,247,255,round(64*envelope)))
        spot.setColorAt(.63,QColor(62,176,233,round((33 if dark else 46)*envelope)))
        spot.setColorAt(.82,QColor(220,249,255,round((90 if dark else 115)*envelope)))
        spot.setColorAt(.87,QColor(60,189,231,round(28*envelope)))
        spot.setColorAt(1,QColor(68,219,243,0))
        painter.fillRect(box,spot)
        span = (box.width()*.5-cut)*envelope
        center = box.center().x()
        rail = QLinearGradient(box.topLeft(),box.topRight())
        rail.setColorAt(0,QColor(68,219,243,0))
        rail.setColorAt(.3,QColor(68,219,243,round(190*envelope)))
        rail.setColorAt(.7,QColor(255,255,255,round(220*envelope)))
        rail.setColorAt(1,QColor(240,87,166,0))
        painter.setPen(QPen(rail,1.6))
        painter.drawLine(QPointF(center-span,box.top()+1.3),QPointF(center+span,box.top()+1.3))
        painter.drawLine(QPointF(center-span,box.bottom()-1.3),QPointF(center+span,box.bottom()-1.3))
        painter.setPen(QPen(QColor(240,87,166,round(145*envelope)),1.4))
        painter.drawLine(QPointF(box.right()-cut,box.top()+1),QPointF(box.right()-1,box.top()+cut))
        painter.drawLine(QPointF(box.left()+1,box.bottom()-cut),QPointF(box.left()+cut,box.bottom()-1))

    def _paint_commit(self, painter, box, cut, dark):
        """An actual activation sends a finite pair of glints along the edge."""
        phase = self.acknowledgement
        strength = math.sin(math.pi*phase)
        if strength < .001:
            return
        left = box.left()+cut+2.
        right = box.right()-cut-2.
        span = right-left
        if span < 8.:
            return
        travel = phase*span
        trail = min(24.,span*.22,travel)
        for start,direction,y,ink in ((left,1,box.bottom()-2.,'#21D8F0' if dark else '#0875B1'),
                                      (right,-1,box.top()+2.,'#F09CD4' if dark else '#B93788')):
            head = start+direction*travel
            tail = head-direction*trail
            glow = QLinearGradient(tail,y,head,y)
            colour = QColor(ink)
            colour.setAlpha(0)
            glow.setColorAt(0.,colour)
            colour.setAlpha(round(230*strength))
            glow.setColorAt(1.,colour)
            painter.setPen(QPen(glow,1.8))
            painter.drawLine(QPointF(tail,y),QPointF(head,y))
            painter.fillRect(QRectF(head-1.1,y-1.1,2.2,2.2),colour)
            if trail >= 9.:
                # The companion trace dissipates inside the rim as the brighter point advances.
                inset_y = y - 1.6 * direction
                painter.setPen(QPen(glow, .75))
                painter.drawLine(QPointF(tail, inset_y), QPointF(head - direction * 5., inset_y))

    @staticmethod
    def _paint_stripes(painter, box, strength, phase):
        spacing = 76.0
        offset = phase * spacing
        lean = box.height()*.68
        white = QColor(255,255,255,round(31*strength))
        blue = QColor(38,145,232,round(22*strength))
        painter.setPen(Qt.PenStyle.NoPen)
        x = box.left()-spacing-lean+offset
        while x < box.right()+spacing:
            stripe = QPolygonF([QPointF(x,box.top()),QPointF(x+24,box.top()),
                                QPointF(x+24-lean,box.bottom()),QPointF(x-lean,box.bottom())])
            painter.setBrush(white)
            painter.drawPolygon(stripe)
            stripe.translate(27,0)
            painter.setBrush(blue)
            painter.drawPolygon(stripe)
            x += spacing


class _PageFeedback(_Overlay):
    def __init__(self, stack):
        super().__init__(stack, 'motionPageFeedback')
        self.progress = 1.0
        self.last_index = stack.currentIndex()
        self.forward = True
        self.compact = isinstance(stack.parentWidget(), QTabWidget)
        self.outgoing = None
        self.home_transition = False
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(TAB_MS if self.compact else PAGE_MS)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.valueChanged.connect(self._frame)
        self.animation.finished.connect(self.finish)

    def start(self, *, home_transition=False):
        self.animation.stop()
        self.home_transition = home_transition
        self.progress = 0.0
        self.reveal()
        self.animation.start()

    def _frame(self, progress):
        self.progress = float(progress)
        self.update()

    def finish(self):
        self.animation.stop()
        self.progress = 1.0
        self.outgoing = None
        self.home_transition = False
        self.hide()

    @staticmethod
    def _phase(value, start, end):
        fraction = min(1., max(0., (value-start)/(end-start)))
        return fraction*fraction*(3.-2.*fraction)

    def _paint_departure(self, painter):
        if self.outgoing is not None and self.progress < .55:
            image, origin = self.outgoing
            t = self._phase(self.progress, 0., .55)
            size = image.deviceIndependentSize()
            painter.save()
            painter.setOpacity(.68*(1-t))
            # Two leaves of the outgoing heading peel away above the live page.
            for sign in (-1, 1):
                painter.save()
                painter.translate(origin.x()+sign*56*t, origin.y()+sign*13*t)
                seam = size.width()*.49
                top = seam+26
                if sign<0:
                    corners = [(0,0),(top,0),(seam,size.height()),(0,size.height())]
                else:
                    corners = [(top,0),(size.width(),0),(size.width(),size.height()),(seam,size.height())]
                clip = QPainterPath()
                clip.addPolygon(QPolygonF([QPointF(x,y) for x,y in corners]))
                painter.setClipPath(clip)
                details = QPainterPath()
                details.setFillRule(Qt.FillRule.WindingFill)
                details.addRect(QRectF(0,0,size.width(),4))
                details.addRect(QRectF(0,size.height()-5,size.width(),5))
                if size.width() >= 420:
                    art_width = min(388.,size.width()*.35)
                    details.addRect(QRectF(size.width()-art_width,0,art_width,size.height()))
                # Only frame/harbor layers leave. Old page titles never overlap
                # the destination's readable title while the transition settles.
                painter.setClipPath(details,Qt.ClipOperation.IntersectClip)
                painter.drawPixmap(QPointF(0,0),image)
                painter.restore()
            painter.restore()

    def _paint_glass_pass(self, painter, bounds, start, end, accent, reverse=False):
        transit = self._phase(self.progress,start,end)
        if transit <= 0. or transit >= 1.:
            return
        opacity = math.sin(math.pi*transit)**.7
        painter.save()
        painter.setClipRect(bounds)
        painter.translate(bounds.left(),bounds.top())
        width,height = bounds.width(),bounds.height()
        if (not self.forward) != reverse:
            painter.translate(width,0)
            painter.scale(-1,1)
        slant = height*.6
        span = min(width*.28,250.)
        edge = (width+span+slant)*transit
        sheet = QPolygonF([QPointF(edge-span,0),QPointF(edge,0),
                           QPointF(edge-slant,height),QPointF(edge-slant-span,height)])
        gradient = QLinearGradient(edge-span,0,edge,height*.25)
        gradient.setColorAt(0,QColor(228,248,255,0))
        gradient.setColorAt(.65,QColor(216,243,255,round(36*opacity)))
        gradient.setColorAt(1,QColor(106,209,243,round(72*opacity)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawPolygon(sheet)
        trace = QColor(accent)
        trace.setAlpha(round(176*opacity))
        painter.setPen(QPen(trace,1.4))
        painter.drawLine(QPointF(edge,0),QPointF(edge-slant,height))
        painter.drawLine(QPointF(edge-39,2),QPointF(edge-5,2))
        trace.setAlpha(round(86*opacity))
        painter.setPen(QPen(trace,1.))
        painter.drawLine(QPointF(edge-10,1),QPointF(edge-10-slant*.52,height*.52))
        painter.restore()

    def _paint_home_sweep(self, painter):
        width, height = self.width(), self.height()
        if not width or not height:
            return
        slant = min(width * .22, height * .32)
        painter.save()
        if not self.forward:
            painter.translate(width, 0)
            painter.scale(-1, 1)
        for start, end, fraction, tint, accent in (
            (.02, .88, .26, '#BDE6FA', '#4189CB'),
            (.14, .99, .15, '#E6DEFA', '#9A83CF'),
        ):
            progress = self._phase(self.progress, start, end)
            if not 0 < progress < 1:
                continue
            strength = math.sin(math.pi * progress)
            span = width * fraction
            leading = (width + slant + span) * progress
            sheet = QPolygonF([
                QPointF(leading - span, 0), QPointF(leading, 0),
                QPointF(leading - slant, height), QPointF(leading - slant - span, height),
            ])
            # Keep the leading edge of the angled pane consistently illuminated.
            tilt = slant / height
            run = span / (1 + tilt * tilt)
            gradient = QLinearGradient(leading - span, 0, leading - span + run, run * tilt)
            clear, luminous = QColor(tint), QColor(tint)
            clear.setAlpha(0)
            luminous.setAlpha(round(55 * strength))
            gradient.setColorAt(0, clear)
            gradient.setColorAt(1, luminous)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawPolygon(sheet)
            trace = QColor(accent)
            trace.setAlpha(round(135 * strength))
            painter.setPen(QPen(trace, 1.3))
            painter.drawLine(QPointF(leading, 0), QPointF(leading - slant, height))
        painter.restore()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.home_transition:
            self._paint_home_sweep(painter)
            painter.end()
            return
        self._paint_departure(painter)
        width,height = self.width(),self.height()
        # The live page holds still. The upper glass and counter-moving lower
        # trace arrive in sequence, keeping the centre of the page readable.
        head = min(36 if self.compact else 92,height)
        self._paint_glass_pass(painter,QRectF(0,0,width,head),.03,.96,palette.BLUE)
        if not self.compact and height > head+54:
            self._paint_glass_pass(painter,QRectF(0,height-35,width,35),.24,1.,palette.PINK,reverse=True)
        painter.end()


class MotionController(QObject):
    """Window-scoped feedback and a persistable reduced-motion preference."""
    enabledChanged = Signal(bool)

    def __init__(self, window, settings_root=None):
        super().__init__(window)
        self.window = window
        self.settings_path = Path(settings_root) / SETTINGS_FILE if settings_root is not None else None
        self._enabled = self._load_enabled()
        self._buttons = {}
        self._stacks = {}
        self._stopping = False
        self._selection_targets = []
        self.selection_phase = 0.0
        self._selection_clock = QElapsedTimer()
        self._selection_clock.start()
        self.selection_timer = QTimer(self)
        self.selection_timer.setInterval(40)
        self.selection_timer.timeout.connect(self._advance_selection)
        window._motion_controller = self
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self.refresh()

    @property
    def enabled(self):
        return self._enabled

    @property
    def active_count(self):
        """Number of finite interaction transitions, excluding selected-option flow."""
        state = QAbstractAnimation.State.Running
        return sum(v.running for v in self._buttons.values() if isValid(v)) + sum(
            v.animation.state() == state for v in self._stacks.values() if isValid(v))

    @property
    def selection_active_count(self):
        return len(self._selection_targets) if self.selection_timer.isActive() else 0

    def _sync_selection_timer(self):
        if not isValid(self.window) or not isValid(self.selection_timer):
            self._selection_targets = []
            return
        allowed = self._enabled and self.window.isVisible() and not self.window.isMinimized()
        self._selection_targets = [button for button in self._buttons if allowed and isValid(button)
            and button.isVisible() and button.isEnabled() and button.isChecked() and bool(button.text())]
        if self._selection_targets:
            if not self.selection_timer.isActive():
                self._selection_clock.restart()
                self.selection_timer.start()
        else:
            self.selection_timer.stop()

    def _advance_selection(self):
        self._sync_selection_timer()
        if not self.selection_timer.isActive():
            return
        self.selection_phase = (self.selection_phase+self._selection_clock.restart()/4400.0)%1.0
        for button in self._selection_targets:
            button.update()

    def _load_enabled(self):
        if self.settings_path is None:
            return True
        try:
            if self.settings_path.stat().st_size > 4096:
                return True
            value = json.loads(self.settings_path.read_text(encoding='utf8'))
            if isinstance(value, dict) and value.get('version') == 1 and type(value.get('enabled')) is bool:
                return value['enabled']
        except (OSError, ValueError, RecursionError):
            pass
        return True

    def _save_enabled(self, enabled):
        if self.settings_path is None:
            return
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix='.ui-motion-', suffix='.json', dir=self.settings_path.parent)
        try:
            with os.fdopen(handle, 'w', encoding='utf8') as stream:
                json.dump({'version': 1, 'enabled': enabled}, stream)
                stream.write('\n')
            os.replace(temporary, self.settings_path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def set_enabled(self, enabled, *, persist=True):
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        if persist:
            self._save_enabled(enabled)
        self._enabled = enabled
        if not enabled:
            self.finish_all(keep_focus=True)
        self._sync_selection_timer()
        self.enabledChanged.emit(enabled)

    def refresh(self):
        """Register existing tab pages after the main window finishes building."""
        for tabs in self.window.findChildren(QTabWidget):
            for stack in tabs.findChildren(QStackedWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
                self.register_stack(stack)

    def register_stack(self, stack):
        if not isinstance(stack, QStackedWidget) or stack in self._stacks:
            return
        overlay = _PageFeedback(stack)
        self._stacks[stack] = overlay
        stack.currentChanged.connect(lambda _index, item=stack: self._page_changed(item))
        stack.destroyed.connect(lambda _object=None, item=stack: self._stacks.pop(item, None))

    @staticmethod
    def _home_exchange(old, target):
        return (old is not None and target is not None
                and 'homePage' in (old.objectName(), target.objectName()))

    def switch_page(self, stack, index):
        if stack.currentIndex() == index:
            return
        old, target = stack.currentWidget(), stack.widget(index)
        echo = None
        if (self.enabled and stack.isVisible() and not self.window.isMinimized() and
                old is not None and target is not None and not old.property('suppressPageTransition')
                and not target.property('suppressPageTransition') and not self._home_exchange(old, target)):
            header = old.findChild(QWidget,'fleetPageHeader')
            if header is not None and header.isVisible():
                echo = (header.grab(), header.mapTo(stack, QPoint(0,0)))
        stack.setCurrentIndex(index)
        overlay = self._stacks.get(stack)
        if echo is not None and overlay is not None and overlay.isVisible():
            overlay.outgoing = echo
            overlay.update()

    def _page_changed(self, stack):
        overlay = self._stacks.get(stack)
        if overlay is None or not isValid(overlay):
            return
        previous = stack.widget(overlay.last_index)
        overlay.finish()
        current = stack.currentIndex()
        overlay.forward = current >= overlay.last_index
        overlay.last_index = current
        page = stack.currentWidget()
        if (page is not None and page.property('suppressPageTransition') is not True and self._enabled
                and self.window.isVisible() and not self.window.isMinimized() and stack.isVisible()):
            overlay.start(home_transition=self._home_exchange(previous, page))

    def _button(self, button):
        feedback = self._buttons.get(button)
        if feedback is None:
            feedback = _ButtonFeedback(button, self)
            self._buttons[button] = feedback
            button.destroyed.connect(lambda _object=None, item=button: self._discard_button(item))
            self._sync_selection_timer()
        return feedback

    def _discard_button(self, button):
        self._buttons.pop(button, None)
        self._sync_selection_timer()

    def finish_all(self, keep_focus=False):
        if self._stopping:
            return
        self._stopping = True
        try:
            for button, feedback in list(self._buttons.items()):
                if isValid(feedback) and isValid(button):
                    feedback.finish(keep_focus and button.isEnabled() and button.isVisible() and button.hasFocus())
            for feedback in list(self._stacks.values()):
                if isValid(feedback):
                    feedback.finish()
        finally:
            self._stopping = False

    def _within(self, widget):
        return (isValid(self.window) and isValid(widget)
                and (widget is self.window or self.window.isAncestorOf(widget)))

    @staticmethod
    def _replay_control(widget):
        current = widget
        while current is not None:
            if current.property('tacticalSurface') is True:
                return True
            if current.objectName() == 'replayPanel':
                return current.property('academyChrome') is not True
            current = current.parentWidget()
        return False

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind not in (QEvent.Type.Paint, QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.FocusIn, QEvent.Type.FocusOut,
                        QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                        QEvent.Type.KeyPress, QEvent.Type.KeyRelease, QEvent.Type.EnabledChange,
                        QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Resize, QEvent.Type.WindowStateChange, QEvent.Type.Close):
            return False
        if not isinstance(watched, QWidget) or not self._within(watched):
            return False
        if watched is self.window and (kind in (QEvent.Type.Hide, QEvent.Type.Close)
                or kind == QEvent.Type.WindowStateChange and self.window.isMinimized()):
            self.finish_all()
            self.selection_timer.stop()
            self._selection_targets = []
        elif watched is self.window and kind in (QEvent.Type.Show, QEvent.Type.WindowStateChange):
            self._sync_selection_timer()
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress):
            # Never leave transitional paint above the control being operated.
            for feedback in list(self._stacks.values()):
                if isValid(feedback):
                    feedback.finish()
        if watched in self._stacks:
            feedback = self._stacks[watched]
            if kind in (QEvent.Type.Hide, QEvent.Type.Resize):
                feedback.finish()
        if not isinstance(watched, QPushButton):
            return False
        if self._replay_control(watched):
            return False
        if kind == QEvent.Type.Paint:
            self._button(watched).paint_button()
            return True
        feedback = self._buttons.get(watched)
        if kind in (QEvent.Type.Hide, QEvent.Type.EnabledChange) and feedback is not None:
            feedback.finish()
            self._sync_selection_timer()
        elif kind == QEvent.Type.Show:
            self._sync_selection_timer()
        if not watched.isEnabled() or not watched.isVisible() or self.window.isMinimized():
            return False
        if kind == QEvent.Type.Resize and feedback is not None:
            feedback.setGeometry(watched.rect())
        elif kind == QEvent.Type.MouseMove and feedback is not None:
            feedback.follow_pointer(QPointF(event.position()))
        elif kind == QEvent.Type.FocusIn:
            self._button(watched).focus(True)
        elif kind == QEvent.Type.FocusOut and feedback is not None:
            feedback.focus(False)
        elif kind == QEvent.Type.Enter and self._enabled:
            feedback = self._button(watched)
            if hasattr(event,'position'):
                feedback.follow_pointer(QPointF(event.position()))
            feedback.aim(True)
        elif kind == QEvent.Type.Leave and feedback is not None:
            feedback.aim(False, self._enabled)
        elif kind == QEvent.Type.MouseButtonPress and self._enabled and event.button() == Qt.MouseButton.LeftButton:
            self._button(watched).press(event.position())
        elif kind == QEvent.Type.MouseButtonRelease and self._enabled and event.button() == Qt.MouseButton.LeftButton:
            self._button(watched).release()
        elif (kind == QEvent.Type.KeyPress and self._enabled and not event.isAutoRepeat()
              and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            self._button(watched).press(QPointF(watched.rect().center()))
        elif (kind == QEvent.Type.KeyRelease and self._enabled and not event.isAutoRepeat()
              and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            self._button(watched).release()
        return False
