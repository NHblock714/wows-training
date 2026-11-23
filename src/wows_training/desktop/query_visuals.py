"""Public career charts and a shareable snapshot card."""
import math

from PySide6.QtCore import QEvent, QEasingCurve, QPoint, QPointF, QRectF, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF, QRadialGradient, QRegion
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from .naval_assets import default_naval_assets
from . import academy_palette as tone
from .player_dossier_panel import REGIONS, count_text, percent, time_text
from .statistics_visuals import CLASS_COLORS, CLASS_LABELS
from .harbor_showcase import HarborShowcase
from .motion import motion_enabled, subscribe_motion
from .layered_panel import LayeredPanel, fold_content_inset, paint_layered_panel, panel_path
from .presentation_fonts import load_presentation_fonts
from .account_exhibit import constellation_marks, paint_constellation


def _presentation_face(family, pixels, *, bold=False, italic=False):
    face = QFont(family)
    face.setPixelSize(pixels)
    face.setBold(bold)
    face.setItalic(italic)
    return face


def plain_label(text=''):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def axis_title(key):
    return str(key).replace('🐴含量', '水面舰占比').replace('隐藏🐴', '隐藏比例')


class CareerDistribution(QWidget):
    """Shares use all filtered ship battles before the ranking limit is applied."""
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(69)
        self.groups = []
        self.total = 0
        self.setAccessibleName('当前筛选范围的舰种与等级场次构成')

    def set_records(self, rows):
        class_counts, tier_counts = {}, {}
        for row in rows:
            battles = row['battles']
            for counts, key in ((class_counts, row.get('species')), (tier_counts, row.get('tier'))):
                counts[key] = counts.get(key, 0) + battles
        self.total = sum(class_counts.values())
        self.groups = [
            ('舰种', [(CLASS_LABELS.get(k, '未知'), n, CLASS_COLORS.get(k, tone.MUTED))
                    for k, n in sorted(class_counts.items(), key=lambda entry: -entry[1])]),
            ('等级', [('T'+str(k) if k else '未知', n,
                     QColor.fromHsl(217, 155, 205 - int(k)*8).name() if k else tone.MUTED)
                    for k, n in sorted(tier_counts.items(), key=lambda entry: entry[0] or 0)])]
        captions = [f'{title}　' + '　'.join(f'{label} {count_text(n)} 场（{n/self.total:.1%}）'
                    for label, n, _ in values) for title, values in self.groups if self.total]
        self.setToolTip('\n'.join(captions))
        self.setAccessibleDescription('；'.join(captions))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.fillRect(self.rect(), QColor(tone.PAPER))
            if not self.total:
                p.setPen(QColor(tone.MUTED))
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, '无符合条件的舰船记录')
                return
            for i, (title, values) in enumerate(self.groups):
                y = 4 + i*32
                p.setPen(QColor(tone.MUTED)); p.drawText(QRectF(8,y,45,24), Qt.AlignmentFlag.AlignCenter, title)
                start, track = 61., max(0., self.width()-69.)
                for label, count, color in values:
                    width = track*count/self.total
                    if width <= 0:
                        continue
                    p.fillRect(QRectF(start,y,width,24),QColor(color))
                    text = f'{label} {count/self.total:.0%}'
                    if width >= p.fontMetrics().horizontalAdvance(text)+6:
                        bg = QColor(color)
                        rgb = [v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4
                               for v in (bg.redF(),bg.greenF(),bg.blueF())]
                        luminance = sum(v*w for v,w in zip(rgb,(.2126,.7152,.0722)))
                        p.setPen(QColor('#091320' if luminance>.19 else tone.PAPER))
                        p.drawText(QRectF(start,y,width,24), Qt.AlignmentFlag.AlignCenter, text)
                    start += width
        finally:
            p.end()


class RatingRadar(QWidget):
    """A contrast-rich instrument surface with bounded data and pointer transitions."""
    axisActivated = Signal(str)
    LIMIT = 150.0
    OWN_INK = '#05658E'
    OTHER_INK = '#A32665'
    LABEL_INK = '#15354E'
    MUTED_INK = '#3E5C73'

    def __init__(self, *, enlarged=False, background_enabled=True):
        super().__init__()
        self.enlarged = enlarged
        self._harbor = HarborShowcase(instrument=True)
        self._background_enabled = background_enabled
        self.setProperty('backgroundEffect', True)
        self.scores = {}
        self.comparison = {}
        self.keys = []
        self.active_axis = None
        self._targets = {}
        self._fonts = load_presentation_fonts()
        self._ambient_phase=2.4
        self._progress = 1.0
        self._pending_reveal = False
        self._weights = {}
        self._weight_start = {}
        self._subscribed = False
        self._observed_window = None
        self._reveal = QVariantAnimation(self)
        self._reveal.setDuration(420)
        self._reveal.setStartValue(0.)
        self._reveal.setEndValue(1.)
        self._reveal.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._reveal.valueChanged.connect(self._reveal_frame)
        self._reveal.finished.connect(self._reveal_finished)
        self._highlight = QVariantAnimation(self)
        self._highlight.setDuration(190)
        self._highlight.setStartValue(0.)
        self._highlight.setEndValue(1.)
        self._highlight.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._highlight.valueChanged.connect(self._highlight_frame)
        self.setMinimumHeight(180)
        self.setMinimumWidth(184)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName('战力维度')

    @staticmethod
    def _value(series, key):
        entry = series.get(key)
        if entry and type(entry[1]) in (int, float) and math.isfinite(entry[1]):
            return entry[1]
        return None

    def set_ambient_phase(self,phase):
        self._ambient_phase=phase
        if not self._background_enabled or self._harbor.art is not None:
            return
        dirty=QRegion()
        for point in constellation_marks(QRectF(self.rect()).adjusted(12,10,-15,-15)):
            dirty |= QRegion(QRectF(point.x()-4,point.y()-4,8,8).toAlignedRect())
        self.update(dirty)

    def set_background_enabled(self, enabled):
        if self._background_enabled != bool(enabled):
            self._background_enabled = bool(enabled)
            self.update()

    def _readout(self, key):
        own = self._value(self.scores, key)
        other = self._value(self.comparison, key)
        value = f'{own:.1f}' if own is not None else '未知'
        if self.comparison:
            value += ' / ' + (f'{other:.1f}' if other is not None else '未知')
        return value

    def set_scores(self, scores, comparison=None):
        own, other = dict(scores or {}), dict(comparison or {})
        changed = own != self.scores or other != self.comparison
        self.scores, self.comparison = own, other
        self.keys = list(dict.fromkeys((*own, *other)))
        self.active_axis = None
        self._weights.clear()
        self._targets.clear()
        self._highlight.stop()
        description = '\n'.join(f'{axis_title(key)}　{self._readout(key)}' for key in self.keys)
        self.setAccessibleDescription(description)
        self.setToolTip(description)
        if changed:
            self._reveal.stop()
            self._progress = 1.
            self._pending_reveal = len(self.keys) >= 3
            if self.isVisible():
                self._start_reveal()
        self.update()

    def _bind_motion(self):
        if not self._subscribed:
            self._subscribed = subscribe_motion(self, self._motion_preference)
        window = self.window()
        if window is not self._observed_window:
            if self._observed_window is not None:
                try:
                    self._observed_window.removeEventFilter(self)
                except RuntimeError:
                    pass
            self._observed_window = window
            window.installEventFilter(self)

    def _start_reveal(self):
        self._bind_motion()
        self._pending_reveal = False
        if motion_enabled(self) and self.isVisible() and not self.window().isMinimized():
            self._progress = 0.
            self._reveal.start()
        else:
            self._reveal_finished()

    def _reveal_frame(self, progress):
        self._progress = float(progress)
        self.update()

    def _reveal_finished(self):
        self._progress = 1.
        self.update()

    def _finish_motion(self):
        self._reveal.stop()
        self._highlight.stop()
        self._pending_reveal = False
        self._progress = 1.
        self._weights = {key:float(key == self.active_axis) for key in self.keys}
        self.update()

    def _motion_preference(self, enabled):
        if not enabled:
            self._finish_motion()

    def showEvent(self, event):
        super().showEvent(event)
        preferences = getattr(self.window(), 'client_preferences', {})
        self.set_background_enabled(preferences.get('background_enabled', self._background_enabled))
        self._bind_motion()
        if self._pending_reveal:
            self._start_reveal()

    def hideEvent(self, event):
        self._finish_motion()
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if watched is getattr(self,'_observed_window',None) and event.type() in (QEvent.Type.Hide, QEvent.Type.Close, QEvent.Type.WindowStateChange):
            if event.type() != QEvent.Type.WindowStateChange or watched.isMinimized():
                self._finish_motion()
        return False

    def _activate_axis(self, key):
        if key == self.active_axis:
            return
        self.active_axis = key
        self._highlight.stop()
        self._weight_start = dict(self._weights)
        if self.isVisible() and motion_enabled(self) and not self.window().isMinimized():
            self._highlight.start()
        else:
            self._weights = {axis:float(axis == key) for axis in self.keys}
            self.update()

    def _highlight_frame(self, progress):
        self._weights = {key:self._weight_start.get(key,0.) +
            (float(key == self.active_axis)-self._weight_start.get(key,0.))*float(progress) for key in self.keys}
        self.update()

    def mouseMoveEvent(self, event):
        key = next((key for key, boxes in self._targets.items()
                    if any(rect.contains(event.position()) for rect in boxes)), None)
        self._activate_axis(key)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._activate_axis(None)
        super().leaveEvent(event)

    def mousePressEvent(self,event):
        key = next((key for key, boxes in self._targets.items()
                    if any(rect.contains(event.position()) for rect in boxes)), None)
        if event.button()==Qt.MouseButton.LeftButton and key is not None:
            self._activate_axis(key)
            self.axisActivated.emit(key)
            event.accept()
        else:
            super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter,Qt.Key.Key_Space) and self.active_axis in self.keys:
            self.axisActivated.emit(self.active_axis)
            event.accept()
        elif self.keys and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            direction = -1 if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up) else 1
            index = self.keys.index(self.active_axis) if self.active_axis in self.keys else (-1 if direction > 0 else 0)
            self._activate_axis(self.keys[(index+direction) % len(self.keys)])
            event.accept()
        else:
            super().keyPressEvent(event)

    def _draw_series(self, p, series, ink, style, point):
        values = [self._value(series,key) for key in self.keys]
        vertices = [point(i,min(1.,max(0.,value/self.LIMIT))*self._progress) if value is not None else None
                    for i,value in enumerate(values)]
        path = QPainterPath()
        complete = all(vertex is not None for vertex in vertices)
        if complete:
            path.addPolygon(QPolygonF(vertices))
            path.closeSubpath()
            fill = QLinearGradient(path.boundingRect().topLeft(),path.boundingRect().bottomRight())
            tint = QColor(ink)
            tint.setAlpha(46 if style == Qt.PenStyle.SolidLine else 21)
            fill.setColorAt(0,tint)
            tint.setAlpha(12 if style == Qt.PenStyle.SolidLine else 4)
            fill.setColorAt(1,tint)
            p.fillPath(path,fill)
        else:
            for i,vertex in enumerate(vertices):
                if vertex is not None and vertices[i-1] is not None:
                    path.moveTo(vertices[i-1])
                    path.lineTo(vertex)
        width = (3.0 if self.enlarged else 2.4) if style == Qt.PenStyle.SolidLine else (2.4 if self.enlarged else 1.9)
        glow = QColor(ink)
        glow.setAlpha(44)
        p.setPen(QPen(glow,width+5,style,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.setPen(QPen(QColor(ink),width,style,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        for vertex in vertices:
            if vertex is None:
                continue
            p.setPen(QPen(QColor(ink),1.4))
            p.setBrush(QColor('#F3FCFF' if style == Qt.PenStyle.SolidLine else ink))
            if style == Qt.PenStyle.SolidLine:
                p.drawEllipse(vertex,2.6,2.6)
            else:
                p.drawPolygon(QPolygonF([vertex+QPointF(0,-3),vertex+QPointF(3,0),
                                        vertex+QPointF(0,3),vertex+QPointF(-3,0)]))

    def _draw_surface(self, p):
        rect = QRectF(self.rect()).adjusted(1.,1.,-4.,-4.)
        cut = min(26.,rect.width()*.09)
        path = panel_path(rect, cut)
        p.fillPath(path.translated(2,2),QColor(210,79,148,93))
        fill = QLinearGradient(rect.topLeft(),rect.bottomRight())
        fill.setColorAt(0,QColor('#E9F5FD'))
        fill.setColorAt(.6,QColor('#C8DFEF'))
        fill.setColorAt(1,QColor('#D7D7E9'))
        p.fillPath(path,fill)
        if self._background_enabled and not self._harbor.paint(p, rect, path):
            paint_constellation(p,QRectF(self.rect()).adjusted(12,10,-15,-15),self._ambient_phase)
        trim = QLinearGradient(rect.topLeft(), rect.bottomRight())
        trim.setColorAt(0, QColor('#238FBD'))
        trim.setColorAt(.47, QColor('#6797B0'))
        trim.setColorAt(.82, QColor('#819BAE'))
        trim.setColorAt(1, QColor('#B66D9C'))
        p.setPen(QPen(trim, 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.fillRect(QRectF(13,1, min(rect.width()*.21, 80.),3),QColor(self.OWN_INK))
        p.fillRect(QRectF(rect.right()-49,rect.bottom()-2, 34,2.5),QColor(self.OTHER_INK))
        p.setPen(QPen(QColor('#377B9B'), 1.5))
        p.drawPolyline(QPolygonF([QPointF(rect.left()+8,rect.top()+18), QPointF(rect.left()+8,rect.top()+9),
                                  QPointF(rect.left()+20,rect.top()+9)]))
        p.setPen(QPen(QColor('#AB5C88'), 1.5))
        p.drawPolyline(QPolygonF([QPointF(rect.right()-10,rect.bottom()-19),
                                  QPointF(rect.right()-10,rect.bottom()-9), QPointF(rect.right()-22,rect.bottom()-9)]))

    def _draw_plot(self, p, center, radius, point):
        # Short marks outside the scored rings make the instrument edge visible.
        if radius >= 37:
            for index in range(len(self.keys) * 5):
                major = index % 5 == 0
                angle = math.tau * index / (len(self.keys) * 5) - math.pi / 2
                unit = QPointF(math.cos(angle), math.sin(angle))
                p.setPen(QPen(QColor(47,110,146,173 if major else 90), 1.2 if major else 1.))
                p.drawLine(center+unit*(radius+2), center+unit*(radius+(7 if major else 5)))
        halo = QRadialGradient(center, radius*1.24)
        halo.setColorAt(0, QColor(224, 246, 255, 225))
        halo.setColorAt(.72, QColor(186, 221, 242, 228))
        halo.setColorAt(1, QColor(160, 202, 228, 209))
        p.setBrush(halo)
        p.setPen(QPen(QColor('#4986A6'), 1.3))
        p.drawPolygon(QPolygonF([point(i,1.) for i in range(len(self.keys))]))
        p.setBrush(Qt.BrushStyle.NoBrush)
        for fraction in (1/3, 2/3):
            p.setPen(QPen(QColor('#7AA8BF'), .9))
            p.drawPolygon(QPolygonF([point(i,fraction) for i in range(len(self.keys))]))
        for index, key in enumerate(self.keys):
            weight = self._weights.get(key,0.)
            end = point(index,1.)
            if weight:
                fan = QPainterPath(center)
                fan.lineTo(point((index-1) % len(self.keys),1.))
                fan.lineTo(end)
                fan.lineTo(point((index+1) % len(self.keys),1.))
                fan.closeSubpath()
                p.fillPath(fan,QColor(63,190,255,round(weight*46)))
            p.setPen(QPen(QColor('#188BB7' if weight else '#81ACC4'), .8+weight*1.1))
            p.drawLine(center,end)
        p.setPen(QPen(QColor('#5594B5'), .9))
        p.drawEllipse(center,2.8,2.8)

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._draw_surface(p)
            self._targets = {key:[] for key in self.keys}
            if len(self.keys)<3:
                p.setPen(QColor(self.MUTED_INK))
                p.drawText(self.rect(),Qt.AlignmentFlag.AlignCenter,'维度数据不足')
                return
            wide = self.width() >= 490
            ledger_width = min(280. if self.enlarged else 222.,max(198.,self.width()*.36)) if wide else 0.
            chart_width = self.width()-ledger_width
            pixels = 11 if chart_width < 230 else 12 if chart_width < 320 else 16 if self.enlarged else 13
            face = _presentation_face(self._fonts.get('chinese_tech',self._fonts['chinese_body']),pixels)
            p.setFont(face)
            fm = p.fontMetrics()
            captions = {}
            for key in self.keys:
                title = axis_title(key)
                split = math.ceil(len(title)/2)
                captions[key] = title[:split]+'\n'+title[split:] if chart_width<230 and len(title)>=4 else title
            max_label = max(fm.horizontalAdvance(line) for title in captions.values() for line in title.splitlines())
            label_height = max(fm.height()*len(title.splitlines()) for title in captions.values())
            radius = max(13.,min((chart_width-2*max_label-27)/2,
                         (self.height()-58-2*label_height)/2,206. if self.enlarged else 143.))
            center = QPointF(chart_width/2,(self.height()-28)/2)
            def point(i, fraction):
                angle = math.tau*i/len(self.keys)-math.pi/2
                return center+QPointF(math.cos(angle)*radius*fraction,math.sin(angle)*radius*fraction)
            self._draw_plot(p, center, radius, point)
            if self.comparison:
                self._draw_series(p,self.comparison,self.OTHER_INK,Qt.PenStyle.DashLine,point)
            self._draw_series(p,self.scores,self.OWN_INK,Qt.PenStyle.SolidLine,point)
            p.setFont(face)
            for i,key in enumerate(self.keys):
                endpoint = point(i,1.)
                side,ydir = (endpoint.x()-center.x())/radius,(endpoint.y()-center.y())/radius
                title = captions[key]
                width = max(fm.horizontalAdvance(line) for line in title.splitlines())+6
                height = fm.height()*len(title.splitlines())
                x = endpoint.x()+7 if side>.15 else endpoint.x()-width-7 if side<-.15 else center.x()-width/2
                y = endpoint.y()-height/2+(9*ydir if abs(side)<.15 else 2*ydir)
                rect = QRectF(max(8.,min(chart_width-width-8,x)),
                              max(11.,min(self.height()-34-height,y)),width,height)
                weight = self._weights.get(key,0.)
                if weight:
                    highlight = rect.adjusted(-3,-1,3,1)
                    p.fillRect(highlight,QColor(40,170,233,round(weight*54)))
                    p.fillRect(QRectF(highlight.x(),highlight.bottom()-1,highlight.width(),1),
                               QColor(21,125,171,round(weight*220)))
                p.setPen(QColor(self.OWN_INK if weight>.5 else self.LABEL_INK))
                p.drawText(rect,Qt.AlignmentFlag.AlignCenter,title)
                self._targets[key].append(rect.adjusted(-2,-2,2,2))
                value = self._value(self.scores,key)
                if value is not None:
                    dot = point(i,min(1.,max(0.,value/self.LIMIT)))
                    self._targets[key].append(QRectF(dot.x()-6,dot.y()-6,12,12))
            if wide:
                self._draw_ledger(p,QRectF(chart_width+4,13,ledger_width-16,self.height()-25))
            self._draw_footer(p,chart_width)
        finally:
            p.end()

    def _draw_footer(self, p, width):
        p.setPen(QColor(self.OWN_INK if self.active_axis else self.MUTED_INK))
        label_face = _presentation_face(self._fonts.get('chinese_tech',self._fonts['chinese_body']),11 if width<300 else 13)
        p.setFont(label_face)
        box = QRectF(8,self.height()-28,width-16,22)
        if self.active_axis:
            text = f'{axis_title(self.active_axis)}　{self._readout(self.active_axis)}'
            p.drawText(box,Qt.AlignmentFlag.AlignCenter,text)
        else:
            label_width = p.fontMetrics().horizontalAdvance('刻度')
            number_face = _presentation_face(self._fonts.get('tech',self._fonts['grade']),11 if width<300 else 13)
            ticks = '50  /  100  /  150'
            number_width = QFontMetricsF(number_face).horizontalAdvance(ticks)+3
            left = (width-label_width-number_width-8)/2
            p.drawText(QRectF(left,box.top(),label_width,22),Qt.AlignmentFlag.AlignVCenter,'刻度')
            p.setFont(number_face)
            p.drawText(QRectF(left+label_width+8,box.top(),number_width,22),Qt.AlignmentFlag.AlignVCenter,ticks)

    def _draw_ledger(self, p, box):
        backing = QPainterPath()
        backing.moveTo(box.left()+10,box.top())
        backing.lineTo(box.right()-8,box.top())
        backing.lineTo(box.right(),box.top()+8)
        backing.lineTo(box.right(),box.bottom())
        backing.lineTo(box.left(),box.bottom())
        backing.lineTo(box.left(),box.top()+10)
        backing.closeSubpath()
        glass = QLinearGradient(box.topLeft(),box.bottomRight())
        glass.setColorAt(0,QColor(239,249,255,231))
        glass.setColorAt(.66,QColor(213,232,245,230))
        glass.setColorAt(1,QColor(222,222,241,225))
        p.fillPath(backing,glass)
        p.setPen(QPen(QColor('#8DABC1'),1.))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(backing)
        x,w = box.left()+11,box.width()-22
        row_height = min(43. if self.enlarged else 29.,(box.height()-18)/len(self.keys))
        top = box.center().y()-row_height*len(self.keys)/2
        label_face = _presentation_face(self._fonts.get('chinese_tech',self._fonts['chinese_body']),15 if self.enlarged else 12)
        columns = [(self.scores,self.OWN_INK)]
        if self.comparison:
            columns.append((self.comparison,self.OTHER_INK))
        number_width = 63. if self.enlarged else 48.
        label_width = w-len(columns)*number_width
        for j, (_, ink) in enumerate(columns):
            edge = box.right()-12-(len(columns)-j-1)*number_width
            p.fillRect(QRectF(edge-24,box.top()-1,24,2),QColor(ink))
        for i,key in enumerate(self.keys):
            y = top+i*row_height
            rect = QRectF(x-3,y,w+6,row_height)
            self._targets[key].append(rect)
            weight = self._weights.get(key,0.)
            if weight:
                tint = QLinearGradient(rect.topLeft(),rect.topRight())
                tint.setColorAt(0,QColor(46,160,216,round(weight*69)))
                tint.setColorAt(1,QColor(220,84,161,round(weight*45)))
                p.fillRect(rect,tint)
                p.fillRect(QRectF(rect.left(),rect.top()+2,2,rect.height()-4),
                           QColor(22,129,182,round(weight*230)))
            p.setPen(QColor(self.LABEL_INK))
            p.setFont(label_face)
            label = axis_title(key)
            pixels = label_face.pixelSize()
            while p.fontMetrics().horizontalAdvance(label)>label_width and pixels>9:
                pixels -= 1
                p.setFont(_presentation_face(label_face.family(),pixels))
            p.drawText(QRectF(x,y,label_width,row_height),Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,label)
            for j,(series,ink) in enumerate(columns):
                value = self._value(series,key)
                caption = f'{value:.1f}' if value is not None else '未知'
                face = label_face if value is None else _presentation_face(self._fonts.get('tech',self._fonts['grade']),24 if self.enlarged else 19)
                pixels = face.pixelSize()
                while QFontMetricsF(face).horizontalAdvance(caption)>number_width-5 and pixels>10:
                    pixels -= 1
                    face.setPixelSize(pixels)
                p.setFont(face)
                p.setPen(QColor(ink if value is not None else self.MUTED_INK))
                p.drawText(QRectF(x+label_width+j*number_width+2,y,number_width-3,row_height),
                           Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter,caption)
            if i<len(self.keys)-1:
                p.setPen(QPen(QColor('#A8BDCD'),.75))
                p.drawLine(QPointF(x,y+row_height),QPointF(x+w,y+row_height))


class RadarFocusOverlay(QWidget):
    """An in-page, keyboard-accessible enlargement of the selected radar."""
    def __init__(self, owner):
        super().__init__(owner)
        self.setObjectName('radarFocusOverlay')
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.card = LayeredPanel(self, accent=tone.PINK, cut=26)
        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(24,20,26,24)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, fold_content_inset(self.card.cut, 20) - 26, 0)
        self.heading = plain_label()
        self.heading.setObjectName('sectionTitle')
        top.addWidget(self.heading,1)
        self.close_button = QPushButton('收起')
        self.close_button.setAccessibleName('收起雷达详情')
        self.close_button.clicked.connect(self.dismiss)
        top.addWidget(self.close_button)
        layout.addLayout(top)
        self.caption = plain_label()
        self.caption.setObjectName('subtitle')
        layout.addWidget(self.caption)
        self.chart = RatingRadar(enlarged=True)
        layout.addWidget(self.chart,1)
        self.progress = 0.
        self._opening = False
        self._opener = None
        self._window = None
        self._animation = QVariantAnimation(self)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._frame)
        self._animation.finished.connect(self._finish)
        owner.installEventFilter(self)
        self.chart.installEventFilter(self)
        self.close_button.installEventFilter(self)
        self.hide()

    def present(self, chart, heading, caption, opener):
        if len(chart.keys) < 3:
            return
        self._animation.stop()
        self._opener = opener
        self.heading.setText(heading)
        self.caption.setText(caption)
        self.chart.set_scores(chart.scores,chart.comparison)
        self.setGeometry(self.parentWidget().rect())
        if self.window() is not self._window:
            if self._window is not None:
                self._window.removeEventFilter(self)
            self._window = self.window()
            self._window.installEventFilter(self)
        subscribe_motion(self,self._preference_changed)
        self.show()
        self.raise_()
        self.chart.setFocus(Qt.FocusReason.OtherFocusReason)
        self._transition(True)

    def dismiss(self, *_):
        if not self.isHidden() and self._opening:
            self._transition(False)

    def reset(self, *_):
        if not self.isHidden():
            self._opening = False
            self._animation.stop()
            self._finish()

    def _preference_changed(self, enabled):
        if not enabled and not self.isHidden():
            self._animation.stop()
            self._finish()

    def _transition(self, opening):
        self._opening = opening
        self._animation.stop()
        if motion_enabled(self):
            self._animation.setDuration(420 if opening else 300)
            self._animation.setStartValue(self.progress)
            self._animation.setEndValue(1. if opening else 0.)
            self._animation.setEasingCurve(QEasingCurve.Type.OutCubic if opening else QEasingCurve.Type.InCubic)
            self._frame(self.progress)
            self._animation.start()
        else:
            self._finish()

    def _finish(self):
        self._frame(1. if self._opening else 0.)
        if not self._opening:
            has_focus = self.isAncestorOf(QApplication.focusWidget()) if QApplication.focusWidget() else False
            self.hide()
            if self._opener and self._opener.isVisible() and has_focus:
                self._opener.setFocus(Qt.FocusReason.OtherFocusReason)
            self._opener = None
            self.chart.set_scores({})

    def _frame(self, value):
        self.progress = float(value)
        t = 1.-self.progress
        margin = min(42,max(12, self.width()//24))
        full_w = max(1,min(1040,self.width()-2*margin))
        full_h = max(1,min(610,self.height()-2*margin))
        w,h = round(full_w*(1-.055*t)),round(full_h*(1-.055*t))
        x = round((self.width()-w)/2+min(46,margin)*t)
        y = round((self.height()-h)/2+min(30,margin)*t)
        self.card.setGeometry(x,y,w,h)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.fillRect(self.rect(),QColor(11,25,44,round(149*self.progress)))
            bounds = QRectF(self.card.geometry())
            offset = (1-self.progress)*28
            p.setBrush(Qt.BrushStyle.NoBrush)
            for distance,ink,alpha in ((14,tone.PINK,115),(25,tone.CYAN,75)):
                frame = bounds.adjusted(-distance-offset,-distance*.6,distance+offset,distance*.6)
                p.setPen(QPen(QColor(ink),1))
                for corner in (frame.topLeft(),frame.bottomRight()):
                    sign = 1 if corner==frame.topLeft() else -1
                    tint = QColor(ink);tint.setAlpha(round(alpha*self.progress))
                    p.setPen(QPen(tint,2))
                    p.drawLine(corner,corner+QPointF(48*sign,0))
                    p.drawLine(corner,corner+QPointF(0,26*sign))
        finally:
            p.end()

    def mousePressEvent(self, event):
        if event.button()==Qt.MouseButton.LeftButton and not self.card.geometry().contains(event.position().toPoint()):
            self.dismiss()
        event.accept()

    def eventFilter(self, watched, event):
        if watched is self._window and event.type() == QEvent.Type.WindowStateChange and watched.isMinimized():
            self._opener = None
            self.reset()
        if watched is self.parentWidget():
            if event.type() == QEvent.Type.Resize and not self.isHidden():
                self.setGeometry(watched.rect())
                self._frame(self.progress)
            elif event.type() == QEvent.Type.Hide and not self.isHidden():
                self._opener = None
                self.reset()
        elif event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.dismiss()
                return True
            if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                (self.close_button if watched is self.chart else self.chart).setFocus(Qt.FocusReason.TabFocusReason)
                return True
        return super().eventFilter(watched,event)


def snapshot_card(profile, detail, signature='', *, background_enabled=True):
    fonts = load_presentation_fonts()
    image = QImage(1100, 570, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    try:
        p.fillRect(image.rect(), QColor(tone.CANVAS))
        if background_enabled and not HarborShowcase().paint(p, QRectF(0,138,1100,432)):
            paint_constellation(p,QRectF(20,151,1060,399),2.4)
        paint_layered_panel(p,QRectF(26,12,1048,134),accent=tone.BLUE,cut=25)
        # Slender translucent file tabs leave the account name on the same
        # light reading surface as the rest of the exported dossier.
        for x, width, color in ((956,32,tone.BLUE),(1009,10,tone.CYAN),(1042,18,tone.PINK)):
            colour=QColor(color);colour.setAlpha(80 if color==tone.BLUE else 120)
            p.setBrush(colour); p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(QPolygonF([QPointF(x,22),QPointF(x+width,22),QPointF(x+width-53,133),QPointF(x-53,133)]))
        p.fillRect(QRectF(48, 132, 88, 3), QColor(tone.BLUE))
        p.fillRect(QRectF(138, 132, 18, 3), QColor(tone.PINK))
        def line(x, y, width, height, content, size, color=tone.INK, *, role='body', minimum=None):
            box = QRectF(x,y,width,height).adjusted(2,2,-3,-2)
            family = fonts['grade'] if role=='number' else fonts['display'] if role=='display' else fonts['chinese_body']
            if role=='heading':
                family = 'Microsoft YaHei UI'
            value = str(content)
            minimum = max(14,size-10) if minimum is None else minimum
            for pixels in range(size,minimum-1,-1):
                face = _presentation_face(family,pixels,bold=role=='heading',italic=role=='number')
                letters = QPainterPath()
                letters.addText(QPointF(0,0),face,value)
                bounds = letters.boundingRect()
                if bounds.width()<=box.width() and bounds.height()<=box.height():
                    break
            else:
                metrics = QFontMetricsF(face)
                shown = metrics.elidedText(value,Qt.TextElideMode.ElideRight,box.width()-4)
                letters = QPainterPath()
                letters.addText(QPointF(0,0),face,shown)
                bounds = letters.boundingRect()
            p.save()
            p.translate(box.left()-bounds.left(),box.center().y()-bounds.center().y())
            p.fillPath(letters,QColor(color))
            p.restore()
        line(48, 20, 770, 30, f"{REGIONS.get(profile.get('region'),'')}  /  随机战  /  {time_text(profile.get('timestamp'))}", 19, tone.MUTED)
        nickname = profile.get('nickname') or '公开战绩'
        line(47, 56, 744, 65, nickname, 46, tone.INK,
             role='display' if str(nickname).isascii() else 'heading',minimum=24)
        stats, rating = profile.get('metrics', {}), profile.get('rating') or {}
        for i, (name, value) in enumerate((('综合战力', count_text(detail.get('power'))), ('综合 PR', count_text(rating.get('pr'))),
                ('场次', count_text(stats.get('battles'))), ('胜率', percent(stats.get('win_rate'))))):
            x = 48 + i*253
            paint_layered_panel(p,QRectF(x-3,155,244,98),
                                accent=tone.PINK if i in (0,3) else tone.BLUE,cut=14)
            line(x+15,165,208,27,name,18,tone.MUTED)
            line(x+14,194,207,43,value,43,tone.BLUE if i == 0 else tone.INK,role='number',minimum=20)
        ace = profile.get('ace_ship') or {}
        paint_layered_panel(p,QRectF(44,260,711,198),accent=tone.PINK,cut=22)
        line(64,275,370,29,'本命舰',20,tone.MUTED)
        line(64,311,370,42,ace.get('name') or '资料不足',31,role='heading')
        if ace:
            line(64,361,367,28, f"{count_text(ace.get('battles'))} 场   ·   {percent(ace.get('win_rate'))} 胜率",18)
            art = default_naval_assets().ship_image(ace['ship_id'])
            if art is not None:
                area = QRectF(454,274,286,156)
                factor = min(area.width()/art.width(), area.height()/art.height())
                p.drawImage(QRectF(area.center().x()-art.width()*factor/2, area.center().y()-art.height()*factor/2,
                                   art.width()*factor, art.height()*factor), art)
        if detail.get('scores'):
            chart = RatingRadar(background_enabled=background_enabled)
            chart.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            chart.resize(285,202)
            chart.set_scores(detail['scores'])
            chart.render(p,QPoint(765,258),renderFlags=QWidget.RenderFlag.DrawChildren)
            chart.deleteLater()
        styles = '   /   '.join(str(i[0]) for i in detail.get('styles',[])[:3])
        line(64,405,367,28,styles,18,tone.BLUE)
        p.setPen(QColor(tone.LINE)); p.drawLine(48,475,1052,475)
        p.fillRect(QRectF(1003,474,49,3),QColor(tone.PINK))
        if signature:
            line(49,490,996,42,signature[:30],24,tone.INK)
        else:
            from .naval_assets import harbor_lettering
            mark = harbor_lettering(default_naval_assets().image('icons/harbor-wordmark.png'))
            if mark is None:
                line(49,490,996,42,'港口开发部 · 公开战绩',24,tone.INK)
            else:
                factor = min(118 / mark.width(), 44 / mark.height())
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                p.drawImage(QRectF(49, 512-mark.height()*factor/2, mark.width()*factor, mark.height()*factor), mark)
                line(177,490,869,42,'公开战绩',24,tone.INK)
    finally:
        p.end()
    return image


class SnapshotCardDialog(QDialog):
    def __init__(self, profile, detail, parent):
        super().__init__(parent)
        self.profile, self.detail = profile, detail
        self.setWindowTitle('战绩名片')
        self.preview = QLabel()
        self.signature = QLineEdit(); self.signature.setPlaceholderText('签名（可选）'); self.signature.setMaxLength(30)
        self.notice = plain_label()
        box = QVBoxLayout(self); box.addWidget(self.preview); box.addWidget(self.signature)
        actions = QHBoxLayout()
        for title, fn in [('复制图片', self.copy), ('保存 PNG', self.save)]:
            button = QPushButton(title); button.clicked.connect(fn); actions.addWidget(button)
        box.addLayout(actions); box.addWidget(self.notice)
        self.signature.textChanged.connect(self.refresh)
        self.refresh()

    def refresh(self):
        from PySide6.QtGui import QPixmap
        parent = self.parentWidget()
        preferences = getattr(parent.window() if parent is not None else self, 'client_preferences', {})
        self.image = snapshot_card(self.profile, self.detail, self.signature.text(),
                                   background_enabled=preferences.get('background_enabled', True))
        self.preview.setPixmap(QPixmap.fromImage(self.image).scaledToWidth(660, Qt.TransformationMode.SmoothTransformation))

    def copy(self):
        QApplication.clipboard().setImage(self.image)
        self.notice.setText('已复制战绩名片')

    def save(self):
        from PySide6.QtCore import QSaveFile,QIODevice
        path, _ = QFileDialog.getSaveFileName(self, '保存名片', '公开战绩.png', 'PNG (*.png)')
        if not path:
            return
        if not path.lower().endswith('.png'):
            path += '.png'
        target = QSaveFile(path)
        if target.open(QIODevice.OpenModeFlag.WriteOnly) and self.image.save(target, 'PNG') and target.commit():
            self.notice.setText('名片已保存')
        else:
            target.cancelWriting()
            self.notice.setText('图片保存失败，请检查目标目录')
