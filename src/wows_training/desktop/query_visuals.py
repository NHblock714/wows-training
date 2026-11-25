"""Public career charts and a shareable snapshot card."""
from collections import Counter
from functools import lru_cache
import math

from PySide6.QtCore import QEvent, QEasingCurve, QPoint, QPointF, QRectF, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF, QRadialGradient
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from .naval_assets import default_naval_assets
from . import academy_palette as tone
from .career_rating_visuals import rating_accent, rating_halo, rating_ink
from .dog_tag_visual import dog_tag_image, paint_dog_tag
from .player_dossier_panel import REGIONS, count_text, percent, time_text
from .statistics_visuals import CLASS_COLORS, CLASS_LABELS
from .harbor_showcase import HarborShowcase
from .motion import motion_enabled, subscribe_motion
from .layered_panel import LayeredPanel, fold_content_inset, paint_layered_panel, panel_path
from .presentation_fonts import load_presentation_fonts
from .account_exhibit import paint_constellation


# The reference uses 10 as its benchmark. Career axes use 100, with the
# bottom band split at 10 to include F.
DIMENSION_GRADES = (
    (110, 'S', '#8F5800'),
    (90, 'A', '#A62D73'),
    (70, 'B', '#624393'),
    (50, 'C', '#14698F'),
    (30, 'D', '#246956'),
    (10, 'E', '#546C80'),
    (0, 'F', '#A23F53'),
)
GRADE_SCALE = 'S ≥110　A ≥90　B ≥70　C ≥50　D ≥30　E ≥10　F <10'


def dimension_grade(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return '—', tone.MUTED
    return next(((letter, ink) for threshold, letter, ink in DIMENSION_GRADES
                 if value >= threshold))


def _blend_color(start, end, progress):
    return QColor.fromRgbF(*(a + (b-a)*progress for a,b in zip(
        QColor(start).getRgbF(), QColor(end).getRgbF())))


def _presentation_face(family, pixels, *, bold=False, italic=False):
    face = QFont(family)
    face.setFamilies([family, "Microsoft YaHei UI", "Segoe UI Emoji"])
    face.setPixelSize(pixels)
    face.setBold(bold)
    face.setItalic(italic)
    return face


@lru_cache(maxsize=96)
def _grade_letters(family, pixels, letter):
    path = QPainterPath()
    path.addText(QPointF(), _presentation_face(family, pixels, bold=True, italic=True), letter)
    return path


def _draw_grade(painter, box, family, pixels, grade, opacity=1., weight=0.):
    if opacity <= 0:
        return
    letter, ink = grade
    path = _grade_letters(family, pixels, letter)
    bounds = path.boundingRect()
    if bounds.isEmpty():
        return
    # Reserve space for italic overhang, glow and antialiasing on both edges.
    scale = min(1., (box.width()-5)/bounds.width(), (box.height()-5)/bounds.height())
    if scale <= 0:
        return
    painter.save()
    painter.setClipRect(box, Qt.ClipOperation.IntersectClip)
    painter.setOpacity(painter.opacity()*opacity)
    painter.translate(box.center())
    painter.scale(scale, scale)
    painter.translate(-bounds.center())
    if letter != '—':
        halo = QColor(ink)
        halo.setAlpha(round((45 if letter == 'S' else 28) + 55*weight))
        painter.strokePath(path, QPen(halo, 2.4+weight, Qt.PenStyle.SolidLine,
                                     Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.fillPath(path, QColor(ink))
    painter.restore()


def _space_side_labels(labels, bounds):
    """Keep the labels on each side in axis order when the window is short."""
    for side in (-1, 1):
        column = sorted((rect for axis_side, rect in labels if axis_side == side),
                        key=lambda rect: rect.center().y())
        edge = bounds.top()
        for rect in column:
            rect.moveTop(max(rect.top(), edge))
            edge = rect.bottom()+4.
        edge = bounds.bottom()
        for rect in reversed(column):
            rect.moveBottom(min(rect.bottom(), edge))
            edge = rect.top()-4.


def _axis_unit(index, count):
    angle = math.tau*index/count-math.pi/2
    return QPointF(math.cos(angle),math.sin(angle))


def plain_label(text=''):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def axis_title(key):
    return str(key).replace('隐藏🐴', '隐藏比例')


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
    MAIN_RIM = 100.0
    HIGH_GRADE = DIMENSION_GRADES[0][0]
    RIM_FRACTION = .64
    HIGH_FRACTION = .78
    OWN_INK = '#05658E'
    OTHER_INK = '#A32665'
    LABEL_INK = '#15354E'
    MUTED_INK = '#3E5C73'
    PLOT_GAP = 12.
    LEDGER_TOP = 15
    LEDGER_BOTTOM = 26
    LEDGER_BODY_PAD = 8
    LEDGER_ROW_MIN = 22

    def __init__(self, *, enlarged=False, background_enabled=True):
        super().__init__()
        self.enlarged = enlarged
        self._harbor = HarborShowcase(instrument=True)
        self._background_enabled = background_enabled
        self.setProperty('backgroundEffect', True)
        self.scores = {}
        self.comparison = {}
        self._previous_scores = {}
        self._previous_comparison = {}
        self._overall = None
        self._previous_overall = None
        self._grade_counts = Counter()
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
        if entry and type(entry[1]) in (int, float) and 0 <= entry[1] and math.isfinite(entry[1]):
            return entry[1]
        return None

    @classmethod
    def _score_fraction(cls, score):
        score = max(0., min(cls.LIMIT, score))
        if score <= cls.MAIN_RIM:
            return score/cls.MAIN_RIM*cls.RIM_FRACTION
        if score <= cls.HIGH_GRADE:
            progress = (score-cls.MAIN_RIM)/(cls.HIGH_GRADE-cls.MAIN_RIM)
            return cls.RIM_FRACTION+progress*(cls.HIGH_FRACTION-cls.RIM_FRACTION)
        progress = (score-cls.HIGH_GRADE)/(cls.LIMIT-cls.HIGH_GRADE)
        return cls.HIGH_FRACTION+progress*(1.-cls.HIGH_FRACTION)

    def set_ambient_phase(self, phase):
        self._ambient_phase = phase
        if self._background_enabled and self.isVisible() and len(self.keys) >= 3:
            self.update(QRectF(10, 10, self._chart_width()-20, self.height()-35).toAlignedRect())

    def set_background_enabled(self, enabled):
        if self._background_enabled != bool(enabled):
            self._background_enabled = bool(enabled)
            self.update()

    def _readout(self, key, *, include_grade=False):
        parts = []
        for series in (self.scores, self.comparison) if self.comparison else (self.scores,):
            score = self._value(series, key)
            if score is None:
                parts.append('未知')
            else:
                grade = f' · {dimension_grade(score)[0]}' if include_grade else ''
                parts.append(f'{score:.1f}{grade}')
        return ' / '.join(parts)

    def _distribution_text(self):
        return '   '.join(f'{letter} {self._grade_counts[letter]}'
                          for _, letter, _ in DIMENSION_GRADES if self._grade_counts[letter])

    def set_scores(self, scores, comparison=None, *, overall=None):
        own, other = dict(scores or {}), dict(comparison or {})
        values = [self._value(own, key) for key in own]
        known = [value for value in values if value is not None]
        if dimension_grade(overall)[0] == '—':
            overall = sum(known)/len(known) if known else None
        changed = own != self.scores or other != self.comparison or overall != self._overall
        if changed:
            self._previous_scores, self._previous_comparison = self.scores, self.comparison
            self._previous_overall = self._overall
        self.scores, self.comparison = own, other
        self.keys = list(dict.fromkeys((*own, *other)))
        self._overall = overall
        self._grade_counts = Counter(dimension_grade(value)[0] for value in known)
        self.active_axis = None
        self._weights.clear()
        self._targets.clear()
        self._highlight.stop()
        lines = [f'{axis_title(key)}　{self._readout(key, include_grade=True)}' for key in self.keys]
        if self._overall is not None:
            lines.extend((f'维度均分　{self._overall:.1f} · {dimension_grade(self._overall)[0]}',
                          self._distribution_text(), GRADE_SCALE))
        if '🐴含量' in self.keys:
            lines.append('🐴含量的百分比是纯水面舰场次占比。维度分按航母、潜艇与混合舰出场加权。')
        description = '\n'.join(lines)
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

    def _draw_breakthrough(self, p, index, fraction, point, radius, ink, solid):
        if fraction <= self.RIM_FRACTION:
            return
        vertex = point(index,fraction)
        axis = _axis_unit(index,len(self.keys))
        normal = QPointF(-axis.y(),axis.x())
        length = (fraction-self.RIM_FRACTION)*radius
        strength = min(1.,length/max(3.,radius*.24))
        inner = point(index,self.RIM_FRACTION)-axis*min(5.,radius*.07)
        tip = vertex+axis*min(5.,radius*.09)*strength
        span = min(8.,radius*.105)*strength*(1. if solid else .78)
        metal = QColor('#CA801D') if not self.comparison else QColor(ink)

        # The tapered trail crosses the 100-point rim below the measured contour.
        flare = QPainterPath(inner-normal*span*.32)
        flare.lineTo(vertex-axis*min(9.,radius*.11)+normal*span)
        flare.lineTo(tip)
        flare.lineTo(vertex-axis*min(9.,radius*.11)-normal*span)
        flare.closeSubpath()
        light = QLinearGradient(inner,tip)
        light.setColorAt(0,QColor(255,255,255,0))
        bloom = QColor(metal).lighter(164)
        bloom.setAlpha(round(83*strength))
        light.setColorAt(.62,bloom)
        bloom.setAlpha(round(154*strength))
        light.setColorAt(1,bloom)
        p.fillPath(flare,light)

        edge = QColor(metal)
        edge.setAlpha(round(154*strength))
        p.setPen(QPen(edge,1.25,Qt.PenStyle.SolidLine if solid else Qt.PenStyle.DashLine,
                      Qt.PenCapStyle.RoundCap))
        p.drawLine(inner+axis*4,tip)
        if radius >= 54 and solid:
            edge.setAlpha(round(132*strength))
            p.setPen(QPen(edge,1))
            for side in (-1,1):
                p.drawLine(vertex-axis*5+normal*span*.65*side,
                           vertex-axis*2+normal*(span*.65+2)*side)

    def _draw_series(self, p, series, ink, style, point, radius):
        values = [self._value(series,key) for key in self.keys]
        fractions = [self._score_fraction(value)*self._progress if value is not None else None for value in values]
        vertices = [point(i,fraction) if fraction is not None else None for i,fraction in enumerate(fractions)]
        solid = style == Qt.PenStyle.SolidLine
        for i,value in enumerate(values):
            if value is not None and value >= self.HIGH_GRADE:
                self._draw_breakthrough(p,i,fractions[i],point,radius,ink,solid)
        path = QPainterPath()
        complete = all(vertex is not None for vertex in vertices)
        if complete:
            path.addPolygon(QPolygonF(vertices))
            path.closeSubpath()
            fill = QLinearGradient(path.boundingRect().topLeft(),path.boundingRect().bottomRight())
            tint = QColor(ink)
            tint.setAlpha(46 if solid else 21)
            fill.setColorAt(0,tint)
            tint.setAlpha(12 if solid else 4)
            fill.setColorAt(1,tint)
            p.fillPath(path,fill)
        else:
            for i,vertex in enumerate(vertices):
                if vertex is not None and vertices[i-1] is not None:
                    path.moveTo(vertices[i-1])
                    path.lineTo(vertex)
        width = (3.0 if self.enlarged else 2.4) if solid else (2.4 if self.enlarged else 1.9)
        glow = QColor(ink)
        glow.setAlpha(44)
        p.setPen(QPen(glow,width+5,style,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.setPen(QPen(QColor(ink),width,style,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        for index,vertex in enumerate(vertices):
            if vertex is None:
                continue
            visible_high = values[index] >= self.HIGH_GRADE and fractions[index] > self.RIM_FRACTION
            if visible_high:
                flare = QColor('#FFC960' if not self.comparison else ink)
                strength = min(1.,(fractions[index]-self.RIM_FRACTION)/.24)
                flare.setAlpha(round(150*strength))
                p.setPen(QPen(flare,1.25))
                p.setBrush(Qt.BrushStyle.NoBrush)
                if solid:
                    p.drawEllipse(vertex,4.2,4.2)
                else:
                    p.drawPolygon(QPolygonF([vertex+QPointF(0,-5.6),vertex+QPointF(5.6,0),
                                            vertex+QPointF(0,5.6),vertex+QPointF(-5.6,0)]))
                if not self.comparison:
                    axis = _axis_unit(index,len(self.keys))
                    side = QPointF(-axis.y(),axis.x())*min(4.4,radius*.10)
                    tip = vertex+axis*min(5.7,radius*.14)
                    edge = QColor('#A96112')
                    edge.setAlpha(round(185*strength))
                    p.setPen(QPen(edge,1.18,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap))
                    p.drawPolyline(QPolygonF([vertex+axis-side,tip,vertex+axis+side]))
            p.setPen(QPen(QColor(ink),1.4))
            p.setBrush(QColor('#F3FCFF') if solid else QColor(ink))
            if solid:
                p.drawEllipse(vertex,2.6,2.6)
            else:
                p.drawPolygon(QPolygonF([vertex+QPointF(0,-3),vertex+QPointF(3,0),
                                        vertex+QPointF(0,3),vertex+QPointF(-3,0)]))

    def _chart_width(self):
        spare = self.LEDGER_TOP+self.LEDGER_BOTTOM+2*self.LEDGER_BODY_PAD
        if self.width() < 490 or self.height()-spare < self.LEDGER_ROW_MIN*len(self.keys):
            return float(self.width())
        ledger_limit = 302. if self.enlarged else 238.
        ledger_minimum = 222. if self.comparison else 204.
        return self.width()-min(ledger_limit, max(ledger_minimum,self.width()*.36))

    def _series_ink(self):
        if self.comparison or self._overall is None:
            return QColor(self.OWN_INK)
        after = dimension_grade(self._overall)[1]
        before = dimension_grade(self._previous_overall)[1] if self._previous_overall is not None else self.OWN_INK
        return _blend_color(before, after, self._progress)

    def _paint_grade_change(self, p, box, size, current, previous, weight=0.):
        if current == previous:
            _draw_grade(p, box, self._fonts['display'], size, current, weight=weight)
        else:
            _draw_grade(p, box, self._fonts['display'], size, previous, 1.-self._progress, weight)
            _draw_grade(p, box, self._fonts['display'], size, current, self._progress, weight)

    def _draw_surface(self, p, chart_width):
        rect = QRectF(self.rect()).adjusted(1.,1.,-4.,-4.)
        cut = min(26.,rect.width()*.09)
        path = panel_path(rect, cut)
        p.fillPath(path.translated(2,2),QColor(210,79,148,93))
        fill = QLinearGradient(rect.topLeft(),rect.bottomRight())
        fill.setColorAt(0,QColor('#E9F5FD'))
        fill.setColorAt(.6,QColor('#C8DFEF'))
        fill.setColorAt(1,QColor('#D7D7E9'))
        p.fillPath(path,fill)
        if self._background_enabled:
            self._harbor.paint(p, rect, path)
            if len(self.keys) >= 3:
                field = QRectF(15,12,chart_width-29,self.height()-39)
                p.save()
                p.setClipPath(path, Qt.ClipOperation.IntersectClip)
                p.setOpacity(.78)
                paint_constellation(p, field, self._ambient_phase+.7*self._progress,
                                    accent=self._series_ink())
                p.restore()
        trim = QLinearGradient(rect.topLeft(), rect.bottomRight())
        trim.setColorAt(0, QColor('#238FBD'))
        trim.setColorAt(.47, QColor('#6797B0'))
        trim.setColorAt(.82, QColor('#819BAE'))
        trim.setColorAt(1, QColor('#B66D9C'))
        p.setPen(QPen(trim, 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.fillRect(QRectF(13,1, min(rect.width()*.21, 80.),3),self._series_ink())
        p.fillRect(QRectF(rect.right()-49,rect.bottom()-2, 34,2.5),QColor(self.OTHER_INK))
        p.setPen(QPen(QColor('#377B9B'), 1.5))
        p.drawPolyline(QPolygonF([QPointF(rect.left()+8,rect.top()+18), QPointF(rect.left()+8,rect.top()+9),
                                  QPointF(rect.left()+20,rect.top()+9)]))
        p.setPen(QPen(QColor('#AB5C88'), 1.5))
        p.drawPolyline(QPolygonF([QPointF(rect.right()-10,rect.bottom()-19),
                                  QPointF(rect.right()-10,rect.bottom()-9), QPointF(rect.right()-22,rect.bottom()-9)]))

    def _draw_plot(self, p, center, radius, point):
        rim_radius = radius*self.RIM_FRACTION
        if self.enlarged and radius >= 65:
            # Open bezel segments frame the regular band and leave room for the contour to cross it.
            bezel_radius = rim_radius+3
            bezel = QRectF(center.x()-bezel_radius, center.y()-bezel_radius, 2*bezel_radius, 2*bezel_radius)
            p.setBrush(Qt.BrushStyle.NoBrush)
            for start in (24, 126, 204, 306):
                p.setPen(QPen(QColor(54,114,151,110),2.2))
                p.drawArc(bezel,start*16,33*16)
                p.setPen(QPen(QColor(113,206,223,177),1.1))
                p.drawArc(bezel,start*16,15*16)
        halo = QRadialGradient(center, rim_radius*1.22)
        halo.setColorAt(0, QColor(224, 246, 255, 225))
        halo.setColorAt(.72, QColor(186, 221, 242, 228))
        halo.setColorAt(1, QColor(160, 202, 228, 209))
        p.setBrush(halo)
        p.setPen(QPen(QColor('#4986A6'), 1.3))
        p.drawPolygon(QPolygonF([point(i,self.RIM_FRACTION) for i in range(len(self.keys))]))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor('#7AA8BF'), .9))
        p.drawPolygon(QPolygonF([point(i,self._score_fraction(50.)) for i in range(len(self.keys))]))
        for index, key in enumerate(self.keys):
            weight = self._weights.get(key,0.)
            end = point(index,self.RIM_FRACTION)
            outer = point(index,1.)
            if weight:
                fan = QPainterPath(center)
                fan.lineTo(point((index-1) % len(self.keys),self.RIM_FRACTION))
                fan.lineTo(outer)
                fan.lineTo(point((index+1) % len(self.keys),self.RIM_FRACTION))
                fan.closeSubpath()
                p.fillPath(fan,QColor(63,190,255,round(weight*46)))
            p.setPen(QPen(QColor('#188BB7' if weight else '#81ACC4'), .8+weight*1.1))
            p.drawLine(center,end)
            p.setPen(QPen(QColor(59,119,153,round(50+73*weight)), .8, Qt.PenStyle.DotLine))
            p.drawLine(end,outer)
            if radius >= 19:
                axis = _axis_unit(index,len(self.keys))
                cross = QPointF(-axis.y(),axis.x())*min(3.8,radius*.088)
                p.setPen(QPen(QColor(49,112,151,108),.95))
                p.drawLine(outer-cross,outer+cross)
        p.setPen(QPen(QColor('#5594B5'), .9))
        p.drawEllipse(center,2.8,2.8)

    def _plot_layout(self, chart_width):
        # The chart and its labels stop before the folded corners and footer.
        safe = QRectF(15, 21, chart_width-34, self.height()-61)
        # Inline grades leave the plotting diameter intact in a wide desktop instrument.
        compact = chart_width < 250 or self.height() < 245 or (
            self.enlarged and chart_width >= 420 and self.height() < 450)
        text_size = 10 if chart_width < 230 else 12 if chart_width < 320 else 16 if self.enlarged else 13
        grade_size = 13 if chart_width < 230 else 16 if chart_width < 320 else 24 if self.enlarged else 20
        grade_slot = grade_size+5
        face = _presentation_face(self._fonts['chinese_tech'],text_size)
        metrics = QFontMetricsF(face)
        labels, captions = [], []
        head = tail = side_width = 0.
        for i,key in enumerate(self.keys):
            unit = _axis_unit(i,len(self.keys))
            side = 1 if unit.x() > .15 else -1 if unit.x() < -.15 else 0
            title = axis_title(key)
            if (chart_width < 250 and len(title) >= 4) or (chart_width < 230 and key == '🐴含量'):
                split = 1 if key == '🐴含量' else math.ceil(len(title)/2)
                title = title[:split]+'\n'+title[split:]
            lines = title.splitlines()
            name_width = max(metrics.horizontalAdvance(line) for line in lines)+6
            name_height = math.ceil(metrics.height())*len(lines)
            width = name_width+grade_slot if compact else max(grade_slot,name_width)
            height = max(name_height,grade_slot) if compact else name_height+grade_slot
            labels.append((side,QRectF(0,0,width,height)))
            captions.append((title,name_height))
            if side:
                side_width = max(side_width,width)
            elif unit.y() < 0:
                head = height
            else:
                tail = height

        center = safe.adjusted(0,head,0,-tail).center()
        radius = max(13.,min(safe.width()/2-side_width-self.PLOT_GAP,
                             (safe.height()-head-tail)/2-self.PLOT_GAP,206. if self.enlarged else 143.))
        clearance = radius+self.PLOT_GAP
        for i,(side,rect) in enumerate(labels):
            direction = _axis_unit(i,len(labels))
            if side:
                rect.moveTop(center.y()+direction.y()*radius-rect.height()/2)
            else:
                rect.moveLeft(center.x()-rect.width()/2)
                rect.moveTop(center.y()-clearance-rect.height() if direction.y()<0 else center.y()+clearance)
        _space_side_labels(labels,safe)
        centered = [rect for side,rect in labels if not side]
        for side,rect in labels:
            if not side:
                continue
            dy = max(0.,rect.top()-center.y(),center.y()-rect.bottom())
            gap = max(5.,math.sqrt(max(0.,clearance**2-dy**2)))
            for middle in centered:
                if rect.top() < middle.bottom()+4 and rect.bottom() > middle.top()-4:
                    gap = max(gap,middle.width()/2+8)
            x = center.x()+gap if side > 0 else center.x()-gap-rect.width()
            rect.moveLeft(max(safe.left(),min(safe.right()-rect.width(),x)))
        return center,radius,labels,captions,face,grade_size,compact

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            chart_width = self._chart_width()
            self._draw_surface(p, chart_width)
            self._targets = {key:[] for key in self.keys}
            if len(self.keys)<3:
                p.setPen(QColor(self.MUTED_INK))
                p.drawText(self.rect(),Qt.AlignmentFlag.AlignCenter,'维度数据不足')
                return
            ledger_width = self.width()-chart_width
            center,radius,labels,captions,face,grade_size,compact = self._plot_layout(chart_width)
            grade_slot = grade_size+5
            def point(i, fraction):
                return center+_axis_unit(i,len(self.keys))*radius*fraction
            self._draw_plot(p, center, radius, point)
            if self.comparison:
                self._draw_series(p,self.comparison,self.OTHER_INK,Qt.PenStyle.DashLine,point,radius)
            self._draw_series(p,self.scores,self._series_ink(),Qt.PenStyle.SolidLine,point,radius)
            p.setFont(face)
            for i,key in enumerate(self.keys):
                side, rect = labels[i]
                title, height = captions[i]
                if compact:
                    badge = QRectF(rect.left() if side == -1 else rect.right()-grade_slot,
                                   rect.top(),grade_slot,rect.height())
                    title_box = rect.adjusted(grade_slot if side == -1 else 0,0,
                                              -grade_slot if side != -1 else 0,0)
                else:
                    badge = QRectF(rect.left(),rect.top()+height,rect.width(),grade_slot)
                    title_box = QRectF(rect.left(),rect.top(),rect.width(),height)
                weight = self._weights.get(key,0.)
                if weight:
                    highlight = rect.adjusted(-3,-1,3,1)
                    p.fillRect(highlight,QColor(40,170,233,round(weight*54)))
                    p.fillRect(QRectF(highlight.x(),highlight.bottom()-1,highlight.width(),1),
                               QColor(21,125,171,round(weight*220)))
                ink = '#884469' if key == '🐴含量' else self.OWN_INK if weight > .5 else self.LABEL_INK
                p.setPen(QColor(ink))
                p.drawText(title_box,Qt.AlignmentFlag.AlignCenter,title)
                self._targets[key].append(rect.adjusted(-2,-2,2,2))
                value = self._value(self.scores,key)
                previous = self._value(self._previous_scores,key)
                self._paint_grade_change(p,badge,grade_size,dimension_grade(value),dimension_grade(previous),weight)
                for reading in (value,self._value(self.comparison,key)):
                    if reading is None:
                        continue
                    dot = point(i,self._score_fraction(reading)*self._progress)
                    self._targets[key].append(QRectF(dot.x()-6,dot.y()-6,12,12))
            has_summary = False
            if ledger_width:
                ledger = QRectF(chart_width+5,self.LEDGER_TOP,ledger_width-19,
                                self.height()-self.LEDGER_TOP-self.LEDGER_BOTTOM)
                has_summary = self._draw_ledger(p,ledger)
            self._draw_footer(p,chart_width,has_summary)
        finally:
            p.end()

    def _draw_footer(self, p, width, has_summary):
        p.setPen(QColor(self.OWN_INK if self.active_axis else self.MUTED_INK))
        label_face = _presentation_face(self._fonts.get('chinese_tech',self._fonts['chinese_body']),11 if width<300 else 13)
        p.setFont(label_face)
        box = QRectF(25,self.height()-33,width-54,18)
        if self.active_axis:
            text = f'{axis_title(self.active_axis)}　{self._readout(self.active_axis, include_grade=True)}'
            p.drawText(box,Qt.AlignmentFlag.AlignCenter,
                       p.fontMetrics().elidedText(text,Qt.TextElideMode.ElideRight,int(box.width())))
        else:
            label_width = p.fontMetrics().horizontalAdvance('刻度')
            number_face = _presentation_face(self._fonts.get('tech',self._fonts['grade']),11 if width<300 else 13)
            ticks = '50  /  100  /  150'
            number_width = QFontMetricsF(number_face).horizontalAdvance(ticks)+3
            ticks_width = label_width+number_width+8
            mean = f'均分 {self._overall:.1f}' if self._overall is not None else ''
            text_width = p.fontMetrics().horizontalAdvance(mean)+5
            show_average = bool(mean and not has_summary and box.width() >= ticks_width+text_width+30)
            left = box.left() if show_average else box.center().x()-ticks_width/2
            p.drawText(QRectF(left,box.top(),label_width,box.height()),Qt.AlignmentFlag.AlignVCenter,'刻度')
            p.setFont(number_face)
            p.drawText(QRectF(left+label_width+8,box.top(),number_width,box.height()),Qt.AlignmentFlag.AlignVCenter,ticks)
            if show_average:
                p.setFont(label_face)
                edge = box.right()-21
                p.drawText(QRectF(edge-text_width,box.top(),text_width,box.height()),Qt.AlignmentFlag.AlignVCenter,mean)
                self._paint_grade_change(p,QRectF(edge,box.top(),21,box.height()),15,
                                         dimension_grade(self._overall),dimension_grade(self._previous_overall))

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
        label_face = _presentation_face(self._fonts.get('chinese_tech',self._fonts['chinese_body']),15 if self.enlarged else 12)
        body = box.adjusted(0,self.LEDGER_BODY_PAD,0,-self.LEDGER_BODY_PAD)
        has_summary = bool(self._overall is not None and self._grade_counts
                           and body.height()-65 >= self.LEDGER_ROW_MIN*len(self.keys))
        if has_summary:
            self._draw_ledger_summary(p, box, x, w, label_face)
            body.adjust(0,42,0,-23)
        row_height = min(43. if self.enlarged else 29.,body.height()/len(self.keys))
        top = body.center().y()-row_height*len(self.keys)/2
        columns = [(self.scores,self._previous_scores,self._series_ink())]
        if self.comparison:
            columns.append((self.comparison,self._previous_comparison,self.OTHER_INK))
        number_width = min(88. if self.enlarged else 76., (w-43)/len(columns))
        grade_width = 26. if self.enlarged else 21.
        label_width = w-len(columns)*number_width
        for j, (_, _, ink) in enumerate(columns):
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
            p.setPen(QColor('#884469' if key == '🐴含量' else self.LABEL_INK))
            p.setFont(label_face)
            label = axis_title(key)
            pixels = label_face.pixelSize()
            while p.fontMetrics().horizontalAdvance(label)>label_width-4 and pixels>9:
                pixels -= 1
                p.setFont(_presentation_face(label_face.family(),pixels))
            label = p.fontMetrics().elidedText(label,Qt.TextElideMode.ElideRight,int(label_width-4))
            row_top = y+2
            ink_height = row_height-4
            p.drawText(QRectF(x,row_top,label_width-4,ink_height),Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,label)
            for j,(series,previous,ink) in enumerate(columns):
                value = self._value(series,key)
                caption = f'{value:.1f}' if value is not None else '未知'
                size = min(24 if self.enlarged else 19, int(ink_height-2))
                face = _presentation_face(label_face.family(),min(size,label_face.pixelSize())) if value is None else (
                    _presentation_face(self._fonts['tech'],size))
                pixels = face.pixelSize()
                while QFontMetricsF(face).horizontalAdvance(caption)>number_width-grade_width-5 and pixels>10:
                    pixels -= 1
                    face.setPixelSize(pixels)
                p.setFont(face)
                p.setPen(QColor(ink if value is not None else self.MUTED_INK))
                column = x+label_width+j*number_width
                p.drawText(QRectF(column+1,row_top,number_width-grade_width-5,ink_height),
                           Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter,caption)
                if value is not None:
                    grade_box = QRectF(column+number_width-grade_width,row_top,grade_width,ink_height)
                    previous_grade = dimension_grade(self._value(previous,key))
                    self._paint_grade_change(p,grade_box,min(size+2, 21),dimension_grade(value),previous_grade,weight)
            if i<len(self.keys)-1:
                p.setPen(QPen(QColor('#A8BDCD'),.75))
                p.drawLine(QPointF(x,y+row_height),QPointF(x+w,y+row_height))
        return has_summary

    def _draw_ledger_summary(self, p, box, x, width, label_face):
        top = box.top()+9
        p.setFont(label_face)
        p.setPen(QColor(self.MUTED_INK))
        caption = '当前均分' if self.comparison else '维度均分'
        p.drawText(QRectF(x,top,width-86,33),Qt.AlignmentFlag.AlignVCenter,caption)
        p.setFont(_presentation_face(self._fonts['tech'],21))
        p.setPen(self._series_ink())
        p.drawText(QRectF(x+width-89,top,53,33),
                   Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter,f'{self._overall:.1f}')
        badge = QRectF(x+width-33,top,33,33)
        self._paint_grade_change(p,badge,28,dimension_grade(self._overall),dimension_grade(self._previous_overall))
        p.setPen(QPen(QColor('#94B4C9'),.9))
        p.drawLine(QPointF(x,top+37),QPointF(x+width,top+37))

        used = [(letter, ink, self._grade_counts[letter]) for _,letter,ink in DIMENSION_GRADES
                if self._grade_counts[letter]]
        cell_width = min(36.,width/len(used))
        y = box.bottom()-27
        origin = x+(width-len(used)*cell_width)/2
        p.setPen(QPen(QColor('#94B4C9'),.8))
        p.drawLine(QPointF(x,y),QPointF(x+width,y))
        p.setFont(_presentation_face(self._fonts['tech'],12))
        for index,(letter,ink,count) in enumerate(used):
            start = origin+index*cell_width
            _draw_grade(p,QRectF(start,y+3,cell_width*.55,21),self._fonts['display'],14,(letter,ink))
            p.setPen(QColor(self.LABEL_INK))
            p.drawText(QRectF(start+cell_width*.58,y+3,cell_width*.42,21),
                       Qt.AlignmentFlag.AlignVCenter,str(count))


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
        self.chart.set_scores(chart.scores,chart.comparison,overall=chart._overall)
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


def _draw_snapshot_medal(painter, center, accent, group):
    from .achievement_gallery import paint_medal

    color = QColor(accent)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(color,1.5))
    painter.setBrush(QColor(tone.PANEL))
    painter.drawEllipse(center,22,22)
    paint_medal(painter,center-QPointF(0,2),16,color,earned=True,group=group)
    painter.restore()


def snapshot_card(profile, detail, signature='', *, background_enabled=True, achievement_ids=(), dog_tag_png=None):
    from .achievement_gallery import earned_achievements
    from .achievement_rarity import rarity_style

    selected = set(achievement_ids)
    medals = [entry for entry in earned_achievements(detail) if entry['id'] in selected]
    medal_rows = (len(medals) + 2) // 3
    footer_y = 624 + (42 + 76 * medal_rows if medals else 0)
    fonts = load_presentation_fonts()
    dog_tag = dog_tag_image(dog_tag_png)
    image = QImage(1100, footer_y + 68, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    try:
        p.fillRect(image.rect(), QColor(tone.CANVAS))
        if background_enabled:
            HarborShowcase().paint(p, QRectF(0,138,image.width(),image.height()-138))
        paint_layered_panel(p,QRectF(26,12,1048,134),accent=tone.BLUE,cut=25)
        # Slender translucent file tabs leave the account name on the same
        # light reading surface as the rest of the exported dossier.
        tab_offset = 104 if not dog_tag.isNull() else 0
        for x, width, color in ((956,32,tone.BLUE),(1009,10,tone.CYAN),(1042,18,tone.PINK)):
            x -= tab_offset
            colour=QColor(color);colour.setAlpha(80 if color==tone.BLUE else 120)
            p.setBrush(colour); p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(QPolygonF([QPointF(x,22),QPointF(x+width,22),QPointF(x+width-53,133),QPointF(x-53,133)]))
        if not dog_tag.isNull():
            badge = QRectF(954,44,90,85)
            plate = panel_path(badge, 8)
            fill = QLinearGradient(badge.topLeft(), badge.bottomRight())
            fill.setColorAt(0, QColor('#FBFDFF'))
            fill.setColorAt(1, QColor('#E0EDF8'))
            p.fillPath(plate, fill)
            p.setPen(QPen(QColor('#A3BDD6'), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(plate)
            paint_dog_tag(p, badge.adjusted(5, 4, -5, -4), dog_tag)
        p.fillRect(QRectF(48, 132, 88, 3), QColor(tone.BLUE))
        p.fillRect(QRectF(138, 132, 18, 3), QColor(tone.PINK))
        def line(x, y, width, height, content, size, color=tone.INK, *, role='body', minimum=None, halo=None):
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
            p.setClipRect(box)
            p.translate(box.left()-bounds.left(),box.center().y()-bounds.center().y())
            if halo is not None:
                outer = QColor(halo)
                outer.setAlpha(halo.alpha()//5)
                inner = QColor(halo)
                inner.setAlpha(halo.alpha()//2)
                p.strokePath(letters,QPen(outer, 4., Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                p.strokePath(letters,QPen(inner, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.fillPath(letters,QColor(color))
            p.restore()
        line(48, 20, 770, 30, f"{REGIONS.get(profile.get('region'),'')}  /  随机战  /  {time_text(profile.get('timestamp'))}", 19, tone.MUTED)
        nickname = profile.get('nickname') or '公开战绩'
        styles = '  /  '.join(str(i[0]) for i in (detail.get('styles') or ())[:3])
        line(47, 50 if styles else 56, 744, 50 if styles else 65, nickname, 46, tone.INK,
             role='display' if str(nickname).isascii() else 'heading',minimum=24)
        if styles:
            line(48,102,744,25,styles,18,tone.BLUE)
        stats, rating = profile.get('metrics', {}), profile.get('rating') or {}
        power = detail.get('power')
        pr = rating.get('pr')
        accent = rating_accent(pr)
        for i, (name, value) in enumerate((('综合战力', count_text(power)), ('随机战 PR', count_text(pr)),
                ('场次', count_text(stats.get('battles'))), ('胜率', percent(stats.get('win_rate'))))):
            x = 48 + i*253
            tile = QRectF(x-3,155,244,98)
            paint_layered_panel(p, tile, accent=tone.PINK if i == 3 else tone.BLUE, cut=14)
            tile_accent = accent if i < 2 and value != '—' else None
            line(x+15,165,208,27,name,18,tone.MUTED)
            line(x+14,194,207,43,value,43,rating_ink(tile_accent) if i < 2 else tone.INK,
                 role='number',minimum=20,halo=rating_halo(tile_accent) if tile_accent is not None else None)
        ace = profile.get('ace_ship') or {}
        paint_layered_panel(p,QRectF(44,261,394,351),accent=tone.PINK,cut=22)
        line(64,274,270,24,'本命舰',17,tone.MUTED)
        line(64,300,329,43,ace.get('name') or '资料不足',34,role='heading')
        if ace:
            line(64,345,342,24,f"{count_text(ace.get('battles'))} 场   /   {percent(ace.get('win_rate'))} 胜率",18)
        area = QRectF(64,374,345,150)
        art = default_naval_assets().ship_image(ace.get('ship_id')) if ace else None
        if art is not None:
            factor = min(area.width()/art.width(), area.height()/art.height())
            p.drawImage(QRectF(area.center().x()-art.width()*factor/2, area.center().y()-art.height()*factor/2,
                               art.width()*factor, art.height()*factor), art)
        p.setPen(QColor(tone.LINE))
        p.drawLine(66,532,407,532)
        for x, key, label in ((64,'avg_damage','账号场均伤害'),(245,'avg_xp','账号场均经验')):
            line(x,539,162,21,label,16,tone.MUTED)
            line(x,562,162,29,count_text(stats.get(key)),29,role='number')

        chart = RatingRadar(background_enabled=background_enabled)
        chart.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        chart.resize(605,351)
        chart.set_scores(detail.get('scores') or {}, overall=detail.get('overall'))
        chart.render(p,QPoint(449,261),renderFlags=QWidget.RenderFlag.DrawChildren)
        chart.deleteLater()

        if medals:
            line(48,620,440,30,'展示成就',20,role='heading')
        for index, entry in enumerate(medals):
            row, column = divmod(index,3)
            x, y = 45 + 338 * column, 657 + 76 * row
            style = rarity_style(entry.get('rarity'))
            plate = QRectF(x,y,328,68)
            paint_layered_panel(p,plate,accent=style.accent,cut=10)
            _draw_snapshot_medal(p,QPointF(x+37,y+30),style.accent,entry.get('group'))
            line(x+66,y+5,203,19,style.label,15,style.ink)
            line(x+65,y+25,235,29,entry['name'],22,role='heading')

        p.setPen(QColor(tone.LINE)); p.drawLine(48,footer_y,1052,footer_y)
        p.fillRect(QRectF(1003,footer_y-1,49,3),QColor(tone.PINK))
        if signature:
            line(49,footer_y+11,996,42,signature[:30],24,tone.INK)
        else:
            from .naval_assets import harbor_lettering
            mark = harbor_lettering(default_naval_assets().image('icons/harbor-wordmark.png'))
            if mark is None:
                line(49,footer_y+11,996,42,'港口开发部 / 公开战绩',24,tone.INK)
            else:
                factor = min(118 / mark.width(), 44 / mark.height())
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                p.drawImage(QRectF(49, footer_y+32-mark.height()*factor/2, mark.width()*factor, mark.height()*factor), mark)
                line(177,footer_y+11,869,42,'公开战绩',24,tone.INK)
    finally:
        p.end()
    return image


class SnapshotCardDialog(QDialog):
    def __init__(self, profile, detail, parent, *, dog_tag_png=None):
        from html import escape
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QIcon, QPixmap
        from PySide6.QtWidgets import QListWidget, QListWidgetItem, QScrollArea
        from .achievement_gallery import earned_achievements
        from .achievement_rarity import rarity_style

        super().__init__(parent)
        self.profile, self.detail = profile, detail
        self._dog_tag = dog_tag_image(dog_tag_png)
        self.setWindowTitle('战绩名片')
        self.preview = QLabel()
        self.preview.setAccessibleName('战绩名片预览')
        self.preview_area = QScrollArea()
        self.preview_area.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.preview_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.preview_area.setWidget(self.preview)
        self.preview_area.viewport().installEventFilter(self)

        self.signature = QLineEdit()
        self.signature.setAccessibleName('名片签名')
        self.signature.setPlaceholderText('选填，最多 30 字')
        self.signature.setMaxLength(30)
        self.achievements = QListWidget()
        self.achievements.setAccessibleName('勾选要在名片上展示的成就')
        self.achievements.setAlternatingRowColors(True)
        self.achievements.setIconSize(QSize(24, 24))
        self.achievements.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for entry in earned_achievements(detail):
            style = rarity_style(entry.get('rarity'))
            icon = QPixmap(48, 48)
            icon.fill(Qt.GlobalColor.transparent)
            painter = QPainter(icon)
            _draw_snapshot_medal(painter,QPointF(24,24),style.accent,entry.get('group'))
            painter.end()
            item = QListWidgetItem(QIcon(icon), f"{style.label}  /  {entry['name']}", self.achievements)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(Qt.ItemDataRole.UserRole, entry['id'])
            item.setToolTip(f"<p>{escape(entry.get('requirement') or entry['name'])}</p>")
            item.setCheckState(Qt.CheckState.Unchecked)

        content = QHBoxLayout()
        content.addWidget(self.preview_area, 1)
        options = QWidget()
        options.setMinimumWidth(178)
        options.setMaximumWidth(232)
        settings = QVBoxLayout(options)
        settings.setContentsMargins(4, 2, 0, 0)
        signature_label = plain_label('名片签名')
        signature_label.setBuddy(self.signature)
        settings.addWidget(signature_label)
        settings.addWidget(self.signature)
        settings.addSpacing(9)
        self.achievement_label = plain_label()
        self.achievement_label.setBuddy(self.achievements)
        settings.addWidget(self.achievement_label)
        if self.achievements.count():
            settings.addWidget(self.achievements, 1)
        else:
            self.achievements.setParent(options)
            self.achievements.hide()
            settings.addWidget(plain_label('暂无已达成成就'))
            settings.addStretch()
        content.addWidget(options)
        self.notice = plain_label()
        box = QVBoxLayout(self)
        box.addLayout(content, 1)
        actions = QHBoxLayout()
        actions.addWidget(self.notice, 1)
        for title, fn in [('复制图片', self.copy), ('保存 PNG', self.save)]:
            button = QPushButton(title); button.clicked.connect(fn); actions.addWidget(button)
        box.addLayout(actions)
        screen_area = self.screen().availableGeometry()
        self.resize(min(1040, screen_area.width()-32), min(648, screen_area.height()-48))
        self.signature.textChanged.connect(self.refresh)
        self.achievements.itemChanged.connect(self.refresh)
        self.refresh()

    def refresh(self):
        selected = [item.data(Qt.ItemDataRole.UserRole) for i in range(self.achievements.count())
                    if (item := self.achievements.item(i)).checkState() == Qt.CheckState.Checked]
        self.achievement_label.setText(f'展示成就（{len(selected)}/{self.achievements.count()}）')
        parent = self.parentWidget()
        preferences = getattr(parent.window() if parent is not None else self, 'client_preferences', {})
        self.image = snapshot_card(self.profile, self.detail, self.signature.text(),
                                   background_enabled=preferences.get('background_enabled', True),
                                   achievement_ids=selected, dog_tag_png=self._dog_tag)
        self.notice.clear()
        self._fit_preview()

    def _fit_preview(self):
        from PySide6.QtGui import QPixmap
        width = min(self.image.width(), max(1, self.preview_area.viewport().width()))
        pixmap = QPixmap.fromImage(self.image).scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
        self.preview.setFixedSize(pixmap.size())
        self.preview.setPixmap(pixmap)

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.Type.Resize and hasattr(self, 'image')
                and watched is self.preview_area.viewport()):
            self._fit_preview()
        return super().eventFilter(watched, event)

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
