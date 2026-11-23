"""Interactive account exhibit surfaces over the saved public ship statistics."""
from copy import deepcopy
import math

from PySide6.QtCore import QElapsedTimer, QEasingCurve, QEvent, QPointF, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF, QRegion
from PySide6.QtWidgets import QWidget

from . import academy_palette as tone
from .chart_motion import ChartReveal
from .harbor_showcase import HarborShowcase
from .layered_panel import paint_layered_panel, panel_path
from .motion import motion_enabled, subscribe_motion
from .naval_assets import default_naval_assets
from .naval_widgets import english_ship_titles
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


def constellation_marks(rect):
    return [QPointF(rect.left()+(.06+(i*137%887)/1000)*rect.width(),
                    rect.top()+(.06+(i*211%887)/1000)*rect.height()) for i in range(1,18)]


def paint_constellation(painter, rect, phase):
    points=constellation_marks(rect)
    painter.save()
    painter.setClipRect(rect)
    for i,point in enumerate(points):
        for other in points[i+1:]:
            distance=math.hypot(point.x()-other.x(),point.y()-other.y())
            if distance<rect.width()*.23:
                painter.setPen(QPen(QColor(18,99,211,24),.65))
                painter.drawLine(point,other)
        pulse=.5+.5*math.sin(phase*.9+i*.73)
        tint=QColor(tone.PINK if i%4==0 else tone.BLUE)
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
            paint_layered_panel(p,self.rect(),accent=tone.PINK if self.kind=='tier' else tone.BLUE,cut=17)
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
        ring_width = min(142. if self.compact else 155.,self.width()*.43)
        top = 30 if self.compact else 40
        radius = min(ring_width/2-(5 if self.compact else 14),(self.height()-(40 if self.compact else 54))/2)
        center = QPointF(ring_width/2+(0 if self.compact else 5),
                         self.height()/2+(13 if self.compact else 13.5))
        inner = radius*.61
        self._ring=(center,inner,radius+5)
        start=0.
        for index,(identity,_,count) in enumerate(self.rows):
            if count <= 0:
                continue
            span=count/self.total*360
            emphasis=max(float(identity==self.selected),self._hover_weights.get(identity,0.))
            r=radius+emphasis*3
            outer=QRectF(center.x()-r,center.y()-r,2*r,2*r)
            hole=QRectF(center.x()-inner,center.y()-inner,2*inner,2*inner)
            visible=span*self._reveal.progress
            path=QPainterPath()
            path.arcMoveTo(outer,90-start)
            path.arcTo(outer,90-start,-visible)
            path.lineTo(center+QPointF(math.sin(math.radians(start+visible))*inner,-math.cos(math.radians(start+visible))*inner))
            path.arcTo(hole,90-start-visible,visible)
            path.closeSubpath()
            p.fillPath(path,self._color(identity,index))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(tone.PAPER),1.4));p.drawPath(path)
            if identity==self.selected:
                p.setPen(QPen(QColor(tone.PINK),2));p.drawPath(path)
            start+=span
        active=next((row for row in self.rows if row[0]==self.hovered or row[0]==self.selected),None)
        number=share_text(active[2],self.total) if active else f'{self.total:,.0f}'
        font=face(self._fonts['tech'],17)
        p.setFont(font)
        while p.fontMetrics().horizontalAdvance(number)>inner*2-4 and font.pixelSize()>10:
            font.setPixelSize(font.pixelSize()-1);p.setFont(font)
        p.setPen(QColor(tone.BLUE))
        p.drawText(QRectF(center.x()-inner+1,center.y()-19,2*inner-2,24),Qt.AlignmentFlag.AlignCenter,number)
        p.setFont(face(self._fonts['chinese_tech'],13));p.setPen(QColor(tone.MUTED))
        caption = active[1] if active else '成员' if self.compact else '场次'
        caption = p.fontMetrics().elidedText(caption,Qt.TextElideMode.ElideRight,max(1,int(2*inner-3)))
        p.drawText(QRectF(center.x()-inner,center.y()+4,2*inner,20),Qt.AlignmentFlag.AlignCenter,caption)
        cols=2 if len(self.rows)>6 else 1
        rows=math.ceil(len(self.rows)/cols)
        left=ring_width+7
        width=max(1,(self.width()-left-(2 if self.compact else 17))/cols)
        row_height=min(26.,(self.height()-top-8)/rows)
        for i,(identity,name,count) in enumerate(self.rows):
            col,row=divmod(i,rows)
            rect=QRectF(left+col*width,top+row*row_height,width-3,row_height)
            self._targets.append((identity,rect))
            if identity==self.selected or self._hover_weights.get(identity,0.)>.01:
                p.fillRect(rect,QColor(18,99,211,round(20+16*self._hover_weights.get(identity,0.))))
            p.setFont(face(self._fonts['chinese_tech'] if self.kind=='class' or self.compact else self._fonts['tech'],14))
            p.setPen(QColor(tone.INK))
            if self.compact:
                p.fillRect(QRectF(rect.x()+3,rect.center().y()-4,8,8),self._color(identity,i))
            label = rect.adjusted(16 if self.compact else 3,0,-36 if self.compact else -2,0)
            shown = p.fontMetrics().elidedText(name,Qt.TextElideMode.ElideRight,max(1,int(label.width())))
            p.drawText(label,Qt.AlignmentFlag.AlignVCenter|Qt.AlignmentFlag.AlignLeft,shown)
            p.setFont(face(self._fonts['tech'],12));p.setPen(QColor(tone.BLUE))
            value = f'{count:,.0f}' if self.compact else share_text(count,self.total)
            p.drawText(rect.adjusted(0,0,-5,0),Qt.AlignmentFlag.AlignVCenter|Qt.AlignmentFlag.AlignRight,value)
        p.end()


class AccountExhibit(QWidget):
    aceRequested = Signal()

    def __init__(self,parent=None):
        super().__init__(parent)
        self._fonts=load_presentation_fonts()
        self._harbor = HarborShowcase(instrument=True)
        self._background_enabled = True
        self.setProperty('backgroundEffect', True)
        self.profile={};self.detail={};self._silhouette=None
        self._hero=QRectF()
        self._phase=2.4
        self.setMinimumSize(228,344)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

    def set_ambient_phase(self,phase):
        self._phase=phase
        if not self._background_enabled or self._harbor.art is not None:
            return
        dirty=QRegion()
        for point in constellation_marks(QRectF(self.rect()).adjusted(14,12,-18,-16)):
            dirty |= QRegion(QRectF(point.x()-4,point.y()-4,8,8).toAlignedRect())
        self.update(dirty)

    def set_background_enabled(self, enabled):
        if self._background_enabled != bool(enabled):
            self._background_enabled = bool(enabled)
            self.update()

    def showEvent(self, event):
        super().showEvent(event)
        preferences = getattr(self.window(), 'client_preferences', {})
        self.set_background_enabled(preferences.get('background_enabled', self._background_enabled))

    def set_profile(self,profile,detail):
        self.profile=deepcopy(profile or {});self.detail=deepcopy(detail or {})
        ace=self.profile.get('ace_ship') or {}
        self._silhouette=None
        if ace.get('ship_id'):
            source=default_naval_assets().ship_image(ace['ship_id'],prefer='silhouette')
            if source is not None:
                self._silhouette=source.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
                p=QPainter(self._silhouette)
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
                p.fillRect(self._silhouette.rect(),QColor(42,110,151,145))
                p.end()
        self.setToolTip(ace.get('name') or '')
        self.update()

    def mouseMoveEvent(self,event):
        self.setCursor(Qt.CursorShape.PointingHandCursor if self._hero.contains(event.position()) and self.profile.get('ace_ship') else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton and self._hero.contains(event.position()) and self.profile.get('ace_ship'):
            self.aceRequested.emit();event.accept()
        else:super().mousePressEvent(event)

    def keyPressEvent(self,event):
        if event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter) and self.profile.get('ace_ship'):
            self.aceRequested.emit();event.accept()
        else:super().keyPressEvent(event)

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.RenderHint.Antialiasing);p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        paint_layered_panel(p,self.rect(),accent=tone.PINK,cut=23)
        if self._background_enabled:
            surface = QRectF(self.rect()).adjusted(1, 1, -6, -6)
            inner = surface.adjusted(5, 3, -2, -2)
            # Leave the original folded corner and illuminated edge exposed.
            fold = QPainterPath()
            extent = min(60., surface.height() * .63 + 3)
            fold.moveTo(surface.right() - extent, surface.top())
            fold.lineTo(surface.topRight())
            fold.lineTo(surface.right(), surface.top() + extent)
            fold.closeSubpath()
            clip = panel_path(inner, 23).subtracted(fold)
            if not self._harbor.paint(p, inner, clip):
                paint_constellation(p,QRectF(self.rect()).adjusted(14,12,-18,-16),self._phase)
        width=self.width()-37
        self._hero=QRectF(16,12,width,122)
        p.setFont(face(self._fonts['chinese_tech'],18));p.setPen(QColor('#3E5C73'))
        p.drawText(QRectF(16,11,width-25,22),'本命舰')
        ace=self.profile.get('ace_ship') or {}
        if self._silhouette is not None:
            target=QRectF(24+width*.52,43,width*.45,60)
            ratio=min(target.width()/self._silhouette.width(),target.height()/self._silhouette.height())
            box=QRectF(target.center().x()-self._silhouette.width()*ratio/2,target.center().y()-self._silhouette.height()*ratio/2,self._silhouette.width()*ratio,self._silhouette.height()*ratio)
            p.drawImage(box,self._silhouette)
        name=english_ship_titles().get(str(ace.get('ship_id')),ace.get('name') or '未知')
        title_width = width*.52
        english = name.isascii()
        selected = face(self._fonts['tech'] if english else self._fonts['chinese_tech'],23)
        if english:
            display = face(self._fonts['display'],25)
            extent = QFontMetricsF(display).horizontalAdvance(name)
            pixels = min(25, int(25 * (title_width-3) / max(1., extent)))
            if pixels >= 16:
                display.setPixelSize(pixels)
                selected = display
        p.setFont(selected)
        while p.fontMetrics().horizontalAdvance(name)>title_width and selected.pixelSize()>12:
            selected.setPixelSize(selected.pixelSize()-1);p.setFont(selected)
        p.setPen(QColor('#15354E'))
        title_box=QRectF(16,45,title_width,43)
        p.save();p.setClipRect(title_box)
        p.drawText(title_box,Qt.AlignmentFlag.AlignVCenter|Qt.TextFlag.TextWordWrap,name)
        p.restore()
        battles=ace.get('battles');rate=ace.get('win_rate')
        actual=(f'{battles:,.0f} 场' if battles is not None else '场次未知')+' / '+(f'{rate:.1%}' if rate is not None else '胜率未知')
        p.setFont(face(self._fonts['tech'],13));p.setPen(QColor('#05658E'));p.drawText(QRectF(16,107,width,22),actual)
        top=139
        for title,key,ink in (('优势','strengths','#05658E'),('待提升','weaknesses','#A32665')):
            p.setFont(face(self._fonts['chinese_tech'],17));p.setPen(QColor(ink));p.drawText(QRectF(16,top,width,23),title)
            top+=25
            values=self.detail.get(key) or []
            if not values:
                p.setPen(QColor('#3E5C73'));p.drawText(QRectF(16,top,width,23),'未知');top+=31
                continue
            for label,_,score in values[:2]:
                label=str(label).replace('🐴含量','水面舰占比')
                p.setFont(face(self._fonts['chinese_tech'],14));p.setPen(QColor('#15354E'))
                p.drawText(QRectF(16,top,width-45,20),label)
                p.setFont(face(self._fonts['tech'],14));p.setPen(QColor(ink))
                p.drawText(QRectF(16,top,width,20),Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter,f'{score:.1f}')
                track = QRectF(16,top+21,width,3)
                p.fillRect(track,QColor('#A6BCCF'))
                fill = QLinearGradient(track.topLeft(), track.topRight())
                fill.setColorAt(0,QColor('#637FB2'))
                fill.setColorAt(.65,QColor(ink))
                p.fillRect(QRectF(track.x(),track.y(),width*max(0,min(1,score/150)),3),fill)
                top+=30
            top+=7
        p.end()

