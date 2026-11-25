"""Interactive account exhibit surfaces over the saved public ship statistics."""
from copy import deepcopy
import math

from PySide6.QtCore import QElapsedTimer, QEasingCurve, QEvent, QPointF, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF, QRegion
from PySide6.QtWidgets import QBoxLayout, QWidget

from . import academy_palette as tone
from .chart_motion import ChartReveal
from .harbor_showcase import HarborShowcase
from .layered_panel import panel_path
from .motion import motion_enabled, subscribe_motion
from .naval_assets import default_naval_assets
from .naval_widgets import _visible_image_rect, english_ship_titles
from .presentation_fonts import load_presentation_fonts
from .statistics_visuals import CLASS_COLORS, CLASS_LABELS


def face(family, pixels, bold=False):
    value = QFont(family)
    value.setPixelSize(pixels)
    value.setBold(bold)
    return value


def share_text(count,total):
    if count<=0 or total<=0:
        return '0%'
    ratio=count/total
    if ratio<.001:
        return '<0.1%'
    return f'{ratio:.1%}' if ratio<.01 else f'{ratio:.0%}'


def paint_log_surface(painter, bounds):
    rect = QRectF(bounds).adjusted(.5, .5, -1.5, -1.5)
    outline = panel_path(rect, 9)
    paper = QLinearGradient(rect.topLeft(), rect.bottomRight())
    paper.setColorAt(0, QColor('#FCFEFF'))
    paper.setColorAt(1, QColor('#E8F0F6'))
    painter.fillPath(outline, paper)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor('#90AABC'), 1))
    painter.drawPath(outline)
    return outline


class CareerInstrumentRow(QWidget):
    """Give the radar the main column; move the ship log below on narrow screens."""

    def __init__(self, radar_stage, ship_log):
        super().__init__()
        self._ship_log = ship_log
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(9)
        self._layout.addWidget(radar_stage, 7)
        self._layout.addWidget(ship_log, 3)

    def resizeEvent(self, event):
        stacked = event.size().width() < 835
        self._layout.setDirection(QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight)
        self._ship_log.set_landscape(stacked and event.size().width() >= 520)
        super().resizeEvent(event)


def constellation_marks(rect):
    return [QPointF(rect.left()+(.06+(i*137%887)/1000)*rect.width(),
                    rect.top()+(.06+(i*211%887)/1000)*rect.height()) for i in range(1,18)]


def paint_constellation(painter, rect, phase, *, accent=None):
    points=constellation_marks(rect)
    painter.save()
    painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
    for i,point in enumerate(points):
        for other in points[i+1:]:
            distance=math.hypot(point.x()-other.x(),point.y()-other.y())
            if distance<rect.width()*.23:
                line = QColor(accent or tone.BLUE)
                line.setAlpha(24 if accent is None else 45)
                painter.setPen(QPen(line,.65))
                painter.drawLine(point,other)
        pulse=.5+.5*math.sin(phase*.9+i*.73)
        tint=QColor(accent or (tone.PINK if i%4==0 else tone.BLUE))
        tint.setAlpha(round(42+65*pulse))
        painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(tint)
        painter.drawEllipse(point,1.1+pulse*.6,1.1+pulse*.6)
    painter.restore()


class ConstellationBackdrop(QWidget):
    """Sparse animated points stay behind the account instruments and stop out of view."""
    phaseChanged=Signal(float)
    def __init__(self, parent=None):
        super().__init__(parent)
        self._phase = 2.4
        self._background_enabled=True
        self.setProperty('backgroundEffect',True)
        self._window = None
        self._bound = False
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._advance)
        self._seeds = tuple(((i*137 % 997)/997, (i*211 % 701)/701, i*.73) for i in range(1, 34))

    def _points(self):
        return [QPointF(9+x*max(1,self.width()-18), 9+y*max(1,self.height()-18)) for x,y,_ in self._seeds]

    def _advance(self):
        self._phase = self._clock.elapsed()/1000
        self.phaseChanged.emit(self._phase)
        dirty = QRegion()
        for point in self._points():
            dirty |= QRegion(QRectF(point.x()-8,point.y()-8,16,16).toAlignedRect())
        self.update(dirty)

    def _sync(self, *_):
        allowed = self._background_enabled and self.isVisible() and not self.window().isMinimized() and motion_enabled(self)
        if allowed and not self._timer.isActive():
            self._clock.start()
            self._timer.start()
        elif not allowed:
            self._timer.stop()
            self._phase = 2.4
            self.phaseChanged.emit(self._phase)
            self.update()

    def set_background_enabled(self,enabled):
        self._background_enabled=bool(enabled)
        self._sync()

    def showEvent(self, event):
        super().showEvent(event)
        self._background_enabled=getattr(self.window(),'client_preferences',{}).get('background_enabled',True)
        if not self._bound:
            self._bound = subscribe_motion(self,self._sync)
        if self.window() is not self._window:
            if self._window is not None:
                self._window.removeEventFilter(self)
            self._window = self.window()
            self._window.installEventFilter(self)
        self._sync()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if watched is getattr(self,'_window',None) and event.type() in (QEvent.Type.WindowStateChange,QEvent.Type.Hide,QEvent.Type.Close):
            self._sync()
        return False

    def paintEvent(self, event):
        if not self._background_enabled:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        points = self._points()
        for i, point in enumerate(points):
            for other in points[i+1:]:
                distance = math.hypot(point.x()-other.x(),point.y()-other.y())
                if distance < min(132,self.width()*.22):
                    p.setPen(QPen(QColor(18,99,211,round(27*(1-distance/132))),.65))
                    p.drawLine(point,other)
            pulse = .5+.5*math.sin(self._phase*.9+self._seeds[i][2])
            tint = QColor(tone.PINK if i%5==0 else tone.BLUE)
            tint.setAlpha(round(50+80*pulse))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(tint)
            p.drawEllipse(point,1.2+pulse*.7,1.2+pulse*.7)
        p.end()


class InteractiveDonut(QWidget):
    """A selectable, animated distribution with a labelled legend."""
    activated = Signal(object)

    def __init__(self, kind, parent=None, *, heading=None, unit='场', colors=None, compact=False):
        super().__init__(parent)
        self.kind = kind
        self.heading = heading or ('舰种场次' if kind == 'class' else '等级场次')
        self.unit, self.colors, self.compact = unit, colors, compact
        self.rows = []
        self.total = 0
        self.selected = None
        self.hovered = None
        self._targets = []
        self._ring = (QPointF(),0.,0.)
        self.available=True
        self._observed_window=None
        self._hover_weights = {}
        self._hover_start = {}
        self._fonts = load_presentation_fonts()
        self._reveal = ChartReveal(self,430)
        self._highlight = QVariantAnimation(self)
        self._highlight.setDuration(170)
        self._highlight.setStartValue(0.)
        self._highlight.setEndValue(1.)
        self._highlight.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._highlight.valueChanged.connect(self._highlight_frame)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        minimum = (224, 142) if compact else (245, 180)
        self.setMinimumSize(*minimum)
        self.setAccessibleName(self.heading + '分布')

    def set_records(self, records, selected=None, *, available=True):
        key = 'species' if self.kind=='class' else 'tier'
        values = {}
        for record in records:
            count = record.get('battles')
            if type(count) not in (int,float) or not math.isfinite(count) or count<=0:
                continue
            identity = record.get(key)
            values[identity] = values.get(identity,0)+count
        order = sorted(values, key=lambda value: (-values[value],str(value))) if self.kind=='class' else sorted(values,key=lambda value: value or 0)
        rows = [(value, CLASS_LABELS.get(value,'未知') if self.kind=='class' else ('T'+str(value) if value else '未知'),values[value]) for value in order]
        self.set_distribution(rows, selected, available=available)

    def set_distribution(self, rows, selected=None, *, available=True):
        self.rows = list(rows)
        self.total = sum(count for _, _, count in self.rows)
        self.available=available
        self.selected = selected
        if self.hovered not in {key for key, _, _ in self.rows}:
            self._set_hover(None)
        self._reveal.set_content(self.rows if self.total else None)
        self.setAccessibleDescription('；'.join(f'{name} {count:,.0f} {self.unit}' for _,name,count in self.rows))
        self.update()

    def select(self, identity):
        self.selected = identity
        self.update()

    def _color(self, identity, index):
        if self.colors is not None:
            return QColor(self.colors.get(identity, tone.MUTED))
        if self.kind == 'class':
            return QColor(CLASS_COLORS.get(identity, tone.MUTED))
        return QColor.fromHsl(205 + index*5, 145, 130 + index%3*15)

    def _set_hover(self, identity):
        if self.hovered==identity:
            return
        self.hovered = identity
        self._highlight.stop()
        self._hover_start = dict(self._hover_weights)
        if motion_enabled(self) and self.isVisible() and not self.window().isMinimized():
            self._highlight.start()
        else:
            self._hover_weights = {key:float(key==identity) for key,_,_ in self.rows}
            self.update()

    def _highlight_frame(self, progress):
        self._hover_weights = {key:self._hover_start.get(key,0.)+(float(key==self.hovered)-self._hover_start.get(key,0.))*float(progress) for key,_,_ in self.rows}
        self.update()

    def _hit(self, position):
        for identity,rect in self._targets:
            if rect.contains(position):
                return identity
        center,inner,outer = self._ring
        delta = position-center
        radius = math.hypot(delta.x(),delta.y())
        if radius<inner:
            return 'clear'
        if inner<=radius<=outer and self.total:
            angle = math.degrees(math.atan2(delta.x(),-delta.y()))%360
            start = 0.
            for identity,_,count in self.rows:
                start += count/self.total*360
                if angle<start:
                    return identity
        return None

    def mouseMoveEvent(self, event):
        identity = self._hit(event.position())
        self._set_hover(identity)
        self.setCursor(Qt.CursorShape.PointingHandCursor if identity is not None else Qt.CursorShape.ArrowCursor)
        row = next((row for row in self.rows if row[0]==identity),None)
        self.setToolTip(f'{row[1]} · {row[2]:,.0f} {self.unit} · {share_text(row[2],self.total)}' if row and self.total else '')
        super().mouseMoveEvent(event)

    def mousePressEvent(self,event):
        identity = self._hit(event.position())
        if event.button()==Qt.MouseButton.LeftButton and identity is not None:
            self.activated.emit(None if identity=='clear' or identity==self.selected else identity)
            event.accept()
        else:
            super().mousePressEvent(event)

    def leaveEvent(self,event):
        self._set_hover(None)
        super().leaveEvent(event)

    def keyPressEvent(self,event):
        keys = [row[0] for row in self.rows]
        if keys and event.key() in (Qt.Key.Key_Left,Qt.Key.Key_Right):
            index = keys.index(self.hovered) if self.hovered in keys else -1
            self._set_hover(keys[(index+(1 if event.key()==Qt.Key.Key_Right else -1))%len(keys)])
            event.accept()
        elif event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter,Qt.Key.Key_Space) and self.hovered in keys:
            self.activated.emit(None if self.selected==self.hovered else self.hovered)
            event.accept()
        elif event.key()==Qt.Key.Key_Escape:
            self.activated.emit(None)
            event.accept()
        else:
            super().keyPressEvent(event)

    def showEvent(self,event):
        super().showEvent(event)
        subscribe_motion(self,self._motion_changed)
        if self.window() is not self._observed_window:
            if self._observed_window is not None:
                self._observed_window.removeEventFilter(self)
            self._observed_window=self.window()
            self._observed_window.installEventFilter(self)

    def _motion_changed(self,enabled):
        if not enabled:
            self._highlight.stop()
            self._hover_weights = {key:float(key==self.hovered) for key,_,_ in self.rows}
            self.update()

    def hideEvent(self,event):
        self._motion_changed(False)
        super().hideEvent(event)

    def eventFilter(self,watched,event):
        if watched is getattr(self,'_observed_window',None) and event.type()==QEvent.Type.WindowStateChange and watched.isMinimized():
            self._motion_changed(False)
        return False

    def paintEvent(self,event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.compact:
            paint_log_surface(p, self.rect())
            accent = QColor('#836BA1' if self.kind == 'tier' else '#337F9D')
            p.fillRect(QRectF(15, 1, 39, 2), accent)
        inset = 0 if self.compact else 15
        p.setFont(face(self._fonts['chinese_tech'],18))
        p.setPen(QColor(tone.INK))
        p.drawText(QRectF(inset,3 if self.compact else 10,self.width()-inset-16,24), self.heading)
        self._targets=[]
        if not self.total:
            self._ring = (QPointF(), 0., 0.)
            p.setFont(face(self._fonts['chinese_tech'],16))
            p.setPen(QColor(tone.MUTED))
            empty = '暂无成员' if self.compact else '暂无场次' if self.available else '船仓资料未知'
            p.drawText(QRectF(inset,35,self.width()-2*inset,self.height()-43),Qt.AlignmentFlag.AlignCenter,empty)
            p.end()
            return
        cols = 2 if len(self.rows) > 6 else 1
        # Give both tier columns room for a two-digit tier and its percentage.
        ring_width = min(142. if self.compact else 155., self.width() * (.35 if cols == 2 else .43))
        top = 30 if self.compact else 40
        radius = min(ring_width/2-(5 if self.compact else 14),(self.height()-(40 if self.compact else 54))/2)
        center = QPointF(ring_width/2+(0 if self.compact else 5),
                         self.height()/2+(13 if self.compact else 13.5))
        inner = radius*.61
        self._ring=(center,inner,radius+5)
        hole = QRectF(center.x()-inner, center.y()-inner, 2*inner, 2*inner)
        dial = QLinearGradient(hole.topLeft(), hole.bottomRight())
        dial.setColorAt(0, QColor('#CCDAE6'))
        dial.setColorAt(.36, QColor('#F8FCFF'))
        dial.setColorAt(1, QColor('#E0EBF1'))
        p.setBrush(dial)
        p.setPen(QPen(QColor('#B0C3D0'), .9))
        p.drawEllipse(hole.adjusted(1.7, 1.7, -1.7, -1.7))
        start=0.
        for index,(identity,_,count) in enumerate(self.rows):
            if count <= 0:
                continue
            span=count/self.total*360
            emphasis=max(float(identity==self.selected),self._hover_weights.get(identity,0.))
            r=radius+emphasis*3
            outer=QRectF(center.x()-r,center.y()-r,2*r,2*r)
            visible=span*self._reveal.progress
            path=QPainterPath()
            path.arcMoveTo(outer,90-start)
            path.arcTo(outer,90-start,-visible)
            path.lineTo(center+QPointF(math.sin(math.radians(start+visible))*inner,-math.cos(math.radians(start+visible))*inner))
            path.arcTo(hole,90-start-visible,visible)
            path.closeSubpath()
            ink = self._color(identity, index)
            finish = QLinearGradient(outer.topLeft(), outer.bottomRight())
            finish.setColorAt(0, ink.lighter(117))
            finish.setColorAt(.48, ink)
            finish.setColorAt(1, ink.darker(112))
            p.fillPath(path, finish)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(tone.PAPER),1.4));p.drawPath(path)
            if emphasis and visible > 3:
                # The short highlight follows only the active segment of the bezel.
                p.setPen(QPen(QColor(243, 254, 255, round(192*emphasis)), 1.5))
                p.drawArc(outer.adjusted(2.7, 2.7, -2.7, -2.7),
                          round((89-start)*16), -round((visible-2)*16))
            if identity==self.selected:
                p.setPen(QPen(QColor(tone.PINK),2));p.drawPath(path)
            start+=span
        active = next((row for row in self.rows if row[0] == self.hovered and self.hovered is not None), None)
        if active is None and self.selected is not None:
            active = next((row for row in self.rows if row[0] == self.selected), None)
        number=share_text(active[2],self.total) if active else f'{self.total:,.0f}'
        font=face(self._fonts['tech'], min(17, max(11, round(inner*.65))))
        p.setFont(font)
        while p.fontMetrics().horizontalAdvance(number)>inner*2-4 and font.pixelSize()>10:
            font.setPixelSize(font.pixelSize()-1);p.setFont(font)
        p.setPen(QColor(tone.BLUE))
        number_box = QRectF(center.x()-inner+1, center.y()-min(20.,inner*.87), 2*inner-2, min(24.,inner*.98))
        p.drawText(number_box,Qt.AlignmentFlag.AlignCenter,number)
        p.setFont(face(self._fonts['chinese_tech'], min(13, max(9, round(inner*.49)))));p.setPen(QColor(tone.MUTED))
        caption = active[1] if active else '成员' if self.compact else '场次'
        caption = p.fontMetrics().elidedText(caption,Qt.TextElideMode.ElideRight,max(1,int(2*inner-3)))
        caption_box = QRectF(center.x()-inner, center.y()+min(4.,inner*.06), 2*inner, min(20.,inner*.80))
        p.drawText(caption_box,Qt.AlignmentFlag.AlignCenter,caption)
        rows=math.ceil(len(self.rows)/cols)
        left=ring_width+7
        width=max(1,(self.width()-left-(2 if self.compact else 17))/cols)
        row_height=min(26.,(self.height()-top-8)/rows)
        for i,(identity,name,count) in enumerate(self.rows):
            col,row=divmod(i,rows)
            rect=QRectF(left+col*width,top+row*row_height,width-3,row_height)
            self._targets.append((identity,rect))
            ink = self._color(identity,i)
            emphasis = max(float(identity == self.selected), self._hover_weights.get(identity, 0.))
            if emphasis > .01:
                tint = QColor(ink)
                tint.setAlpha(round(38*emphasis))
                band = rect.adjusted(0, 1, 0, -1)
                p.fillRect(band, tint)
                marker = QColor(tone.PINK if identity == self.selected else ink)
                marker.setAlpha(round(210*emphasis))
                p.fillRect(QRectF(band.left(), band.top(), 2, band.height()), marker)
            value = f'{count:,.0f}' if self.compact else share_text(count,self.total)
            value_font = face(self._fonts['tech'],12)
            value_width = math.ceil(QFontMetricsF(value_font).horizontalAdvance(value))
            p.setFont(face(self._fonts['chinese_tech'] if self.kind=='class' or self.compact else self._fonts['tech'],14))
            p.setPen(QColor(tone.INK))
            swatch = self.compact or rect.width() >= 77
            if swatch:
                p.fillRect(QRectF(rect.x()+5,rect.center().y()-4,8,8),ink)
            label = rect.adjusted(17 if swatch else 4,0,-value_width-7,0)
            shown = p.fontMetrics().elidedText(name,Qt.TextElideMode.ElideRight,max(1,int(label.width())))
            p.drawText(label,Qt.AlignmentFlag.AlignVCenter|Qt.AlignmentFlag.AlignLeft,shown)
            p.setFont(value_font);p.setPen(QColor(tone.BLUE))
            p.drawText(rect.adjusted(0,0,-5,0),Qt.AlignmentFlag.AlignVCenter|Qt.AlignmentFlag.AlignRight,value)
        p.end()


class AccountExhibit(QWidget):
    aceRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fonts = load_presentation_fonts()
        self._harbor = HarborShowcase(instrument=True)
        self._background_enabled = True
        self.setProperty('backgroundEffect', True)
        self.profile = {}
        self.detail = {}
        self._ship_id = None
        self._ship_image = None
        self._ship_crop = QRectF()
        self._hero = QRectF()
        self._hero_hovered = False
        self._phase = 2.4
        self._landscape = False
        self.setMinimumSize(228, 365)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAccessibleName('本命舰与战力分析')

    def set_landscape(self, enabled):
        if self._landscape == enabled:
            return
        self._landscape = enabled
        self.setMinimumHeight(168 if enabled else 365)
        self.setMaximumHeight(168 if enabled else 16777215)
        self.update()

    def set_ambient_phase(self, phase):
        self._phase = phase
        if self._background_enabled and self._harbor.art is None:
            self.update(self._hero.toAlignedRect())

    def set_background_enabled(self, enabled):
        if self._background_enabled != bool(enabled):
            self._background_enabled = bool(enabled)
            self.update()

    def showEvent(self, event):
        super().showEvent(event)
        preferences = getattr(self.window(), 'client_preferences', {})
        self.set_background_enabled(preferences.get('background_enabled', self._background_enabled))

    def set_profile(self, profile, detail):
        self.profile = deepcopy(profile or {})
        self.detail = deepcopy(detail or {})
        ace = self.profile.get('ace_ship') or {}
        ship_id = ace.get('ship_id')
        if self._ship_id != ship_id:
            self._ship_id = ship_id
            self._ship_image = default_naval_assets().ship_image(ship_id) if ship_id else None
            self._ship_crop = _visible_image_rect(self._ship_image) if self._ship_image is not None else QRectF()
        self.setToolTip(ace.get('name') or '')
        access = ['本命舰 ' + (ace.get('name') or '未知')]
        if ace.get('battles') is not None:
            access.append(f"{ace['battles']:,} 场")
        if ace.get('win_rate') is not None:
            access.append(f"胜率 {ace['win_rate']:.1%}")
        for label, key in (('优势', 'strengths'), ('待提升', 'weaknesses')):
            readings = [f'{axis} {score:.1f}' for axis, _, score in (self.detail.get(key) or [])[:2]]
            access.append(f'{label} ' + ('、'.join(readings) or '未知'))
        if ace:
            access.append('按回车查看舰船')
        self.setAccessibleDescription('。'.join(access))
        self.update()

    def mouseMoveEvent(self, event):
        link = self._hero.contains(event.position()) and bool(self.profile.get('ace_ship'))
        if self._hero_hovered != link:
            self._hero_hovered = link
            self.update(self._hero.toAlignedRect())
        self.setCursor(Qt.CursorShape.PointingHandCursor if link else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hero_hovered = False
        self.update(self._hero.toAlignedRect())
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._hero.contains(event.position()) and self.profile.get('ace_ship'):
            self.aceRequested.emit()
            event.accept()
        else:
            super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.profile.get('ace_ship'):
            self.aceRequested.emit()
            event.accept()
        else:
            super().keyPressEvent(event)

    def _draw_ship(self, painter, box):
        if self._background_enabled:
            if not self._harbor.paint(painter, box):
                paint_constellation(painter, box, self._phase)
        wash = QLinearGradient(box.topLeft(), box.bottomLeft())
        wash.setColorAt(0, QColor(245, 250, 252, 225))
        wash.setColorAt(.52, QColor(235, 247, 252, 64))
        wash.setColorAt(1, QColor(212, 232, 242, 218))
        painter.fillRect(box, wash)
        left = box.left() + 14
        width = box.width() - 28
        painter.setPen(QColor('#39677E'))
        painter.setFont(face(self._fonts['chinese_tech'], 15))
        painter.drawText(QRectF(left, box.y()+8, width, 21), Qt.AlignmentFlag.AlignVCenter, '本命舰')
        ace = self.profile.get('ace_ship') or {}
        if ace:
            active = self._hero_hovered or self.hasFocus()
            tab = QRectF(box.right()-45, box.top()+7, 33, 20)
            tab_path = panel_path(tab, 5)
            painter.fillPath(tab_path, QColor('#316C87' if active else '#D8E9EF'))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor('#447C92' if active else '#88ACBC'), .9))
            painter.drawPath(tab_path)
            x, y = tab.center().x()+1, tab.center().y()
            painter.setPen(QPen(QColor('#FAFDFF' if active else '#34728E'), 1.6))
            painter.drawPolyline(QPolygonF([QPointF(x-3,y-4), QPointF(x+1,y), QPointF(x-3,y+4)]))
        name = english_ship_titles().get(str(self._ship_id), ace.get('name') or '未知')
        title = face(self._fonts['display'] if name.isascii() else self._fonts['chinese_tech'], 22)
        painter.setFont(title)
        metrics = QFontMetricsF(title)
        while metrics.horizontalAdvance(name) > width-4 and title.pixelSize() > 14:
            title.setPixelSize(title.pixelSize()-1)
            metrics = QFontMetricsF(title)
        painter.setFont(title)
        painter.setPen(QColor('#18394E'))
        painter.drawText(QRectF(left, box.y()+31, width, 31), Qt.AlignmentFlag.AlignVCenter,
                         metrics.elidedText(name, Qt.TextElideMode.ElideRight, width-3))
        if not self._ship_crop.isEmpty():
            target = QRectF(left+2, box.top()+64, width-4, box.height()-91)
            scale = min(target.width()/self._ship_crop.width(), target.height()/self._ship_crop.height())
            extent = self._ship_crop.size()*scale
            fitted = QRectF(QPointF(), extent)
            fitted.moveCenter(target.center())
            painter.drawImage(fitted, self._ship_image, self._ship_crop)
        battles, rate = ace.get('battles'), ace.get('win_rate')
        count = f'{battles:,} 场' if battles is not None else '场次未知'
        win = f'胜率 {rate:.1%}' if rate is not None else '胜率未知'
        footer = f'{count}   ·   {win}'
        font = face(self._fonts['chinese_tech'], 14)
        while QFontMetricsF(font).horizontalAdvance(footer) > width and font.pixelSize() > 11:
            font.setPixelSize(font.pixelSize()-1)
        painter.setFont(font)
        painter.setPen(QColor('#244D63'))
        painter.drawText(QRectF(left,box.bottom()-25,width,20), Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(footer, Qt.TextElideMode.ElideRight, int(width)))
        if self.hasFocus() and ace:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor('#AD568D'), 1.4, Qt.PenStyle.DashLine))
            painter.drawRect(box.adjusted(3,3,-3,-3))

    def _draw_scores(self, painter, box, caption, entries, ink):
        left, width = box.left(), box.width()
        painter.setPen(QColor(ink))
        painter.setFont(face(self._fonts['chinese_tech'], 16))
        heading = QRectF(left, box.top(), width, 23)
        painter.drawText(heading, Qt.AlignmentFlag.AlignVCenter, caption)
        if not entries:
            painter.setFont(face(self._fonts['chinese_tech'], 14))
            painter.setPen(QColor(tone.MUTED))
            painter.drawText(heading.translated(0, 28), Qt.AlignmentFlag.AlignVCenter, '未知')
            return
        step = min(39., (box.height()-25)/2)
        for index, (name, _, score) in enumerate(entries[:2]):
            row = QRectF(left, box.top()+25+index*step, width, step)
            label = face(self._fonts['chinese_tech'], 14)
            label.setFamilies([self._fonts['chinese_tech'], 'Microsoft YaHei UI', 'Segoe UI Emoji'])
            painter.setFont(label)
            painter.setPen(QColor(tone.INK))
            shown = painter.fontMetrics().elidedText(str(name), Qt.TextElideMode.ElideRight, int(width-44))
            painter.drawText(row.adjusted(0,0,-43,-5), Qt.AlignmentFlag.AlignVCenter, shown)
            painter.setFont(face(self._fonts['tech'], 15))
            painter.setPen(QColor(ink))
            painter.drawText(row.adjusted(0,0,0,-5), Qt.AlignmentFlag.AlignVCenter|Qt.AlignmentFlag.AlignRight, f'{score:.1f}')
            track = QRectF(left,row.bottom()-3,width,2.5)
            painter.fillRect(track, QColor('#C8D8E1'))
            amount = width*max(0,min(1,score/150))
            meter = QLinearGradient(track.topLeft(), track.topRight())
            meter.setColorAt(0, QColor(ink))
            meter.setColorAt(1, QColor(ink).lighter(145))
            painter.fillRect(QRectF(track.x(),track.y(),amount,track.height()), meter)
            if amount >= 3:
                painter.fillRect(QRectF(track.x()+amount-2,track.y()-1,2,track.height()+2), QColor(ink))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        outline = paint_log_surface(painter, self.rect())
        painter.setClipPath(outline)
        bounds = outline.boundingRect().adjusted(1,1,-1,-1)
        if self._landscape:
            split = int(bounds.width()*.35)
            self._hero = QRectF(bounds.left(), bounds.top(), split, bounds.height())
            body = bounds.adjusted(split+14, 13, -14, -12)
            w = (body.width()-18)/2
            first = QRectF(body.topLeft(), body.size())
            first.setWidth(w)
            second = first.translated(w+18, 0)
        else:
            h = min(187., max(146., bounds.height()*.44))
            self._hero = QRectF(bounds.left(), bounds.top(), bounds.width(), h)
            body = bounds.adjusted(15, h+8, -15, -11)
            first = QRectF(body.left(), body.top(), body.width(), (body.height()-8)/2)
            second = first.translated(0, first.height()+8)
        self._draw_ship(painter, self._hero)
        painter.fillRect(QRectF(self._hero.left(), self._hero.bottom()-1, self._hero.width(), 1), QColor('#A6BCCB'))
        self._draw_scores(painter, first, '优势', self.detail.get('strengths'), '#166D87')
        self._draw_scores(painter, second, '待提升', self.detail.get('weaknesses'), '#94547A')
        painter.end()

