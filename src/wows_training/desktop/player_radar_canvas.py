"""Native eight-axis presentation poster with deterministic high-resolution export."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import math
import random
from PySide6.QtCore import QEvent,QPointF,QRectF,QSize,Qt,QVariantAnimation,QEasingCurve,QSaveFile,QIODevice,QTimer,QElapsedTimer
from PySide6.QtGui import QColor,QFont,QFontDatabase,QImage,QLinearGradient,QPainter,QPainterPath,QPen,QPolygonF,QRadialGradient,QTransform
from PySide6.QtWidgets import QWidget,QSizePolicy
from wows_training.desktop.naval_assets import default_naval_assets
from wows_training.desktop.minimap_assets import default_assets
from .presentation_fonts import load_presentation_fonts
from .motion import motion_enabled, subscribe_motion
from . import academy_palette as palette
from .layered_panel import paint_layered_panel
from .naval_widgets import english_ship_titles
from .fleet_shell import _cover_source

from ..player_radar import DIMENSIONS,evaluate_card,grade_score
COLORS={'accent':palette.CYAN,'white':palette.PAPER,'cyan':'#9BDDF1','muted':'#A3BBD2','line':'#30465D'}
# Anchors follow the grade families; the top S+ endpoint has its own gold.
GRADE_COLOR_STOPS=((0,'#F76768'),(18,'#FF9F43'),(36,'#F7DF61'),(54,'#63D798'),
                   (72,'#5C9DFF'),(90,'#FF85BC'),(108,'#B084F5'),(120,'#FFD76A'))
CONSTELLATION_COUNT=70
CONSTELLATION_LINK_DISTANCE=180.0
_star_random=random.Random(159008)
_STAR_SEEDS=tuple((_star_random.uniform(0,1600),_star_random.uniform(0,900),_star_random.uniform(-9,9),
    _star_random.uniform(-9,9),_star_random.uniform(.8,2.2),_star_random.uniform(0,math.tau)) for _ in range(CONSTELLATION_COUNT))


def color(value,alpha=None):
    c=QColor(COLORS.get(value,value))
    if alpha is not None:c.setAlpha(alpha)
    return c


def font(px,bold=False,serif=False,italic=False):
    f=QFont('Georgia' if serif else 'Microsoft YaHei UI');f.setPixelSize(px);f.setBold(bold);f.setItalic(italic);return f


def text(p,rect,value,px=20,ink='white',bold=False,serif=False,italic=False,align=Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,family=None):
    selected=font(px,bold,serif,italic)
    if family is None and any('㐀'<=ch<='鿿' for ch in str(value)):
        family=load_presentation_fonts()['chinese_tech'];selected.setBold(False)
    if family:selected.setFamily(family)
    p.setFont(selected);p.setPen(color(ink));p.drawText(QRectF(*rect),align,str(value))


def number(value):return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def formatted(value):return f'{value:.1f}'.rstrip('0').rstrip('.') if number(value) else '—'


def grade(value):return grade_score(value)


def grade_color(value):
    if not number(value):return '#617984'
    value=max(0.,min(120.,value))
    for (low,left),(high,right) in zip(GRADE_COLOR_STOPS,GRADE_COLOR_STOPS[1:]):
        if value<=high:
            start,end=QColor(left),QColor(right);fraction=(value-low)/(high-low)
            channels=[round(a+(b-a)*fraction) for a,b in zip(start.getRgb()[:3],end.getRgb()[:3])]
            return QColor(*channels).name()
    return GRADE_COLOR_STOPS[-1][1]


def account_caption(evidence):
    status=(evidence or {}).get('status')
    return {'private':'战绩隐藏','error':'查询失败','not_found':'未找到账号','no_battles':'暂无场次',
        'unsupported':'暂不支持','ok':'PR 未知','partial':'PR 未知'}.get(status,'PR 待查询')


def specialty_icon_image(kind,assets=None,ink='#D8BD83',height=30,*,outline=False):
    """Native upright markers rotated right, then stretched 1.5x horizontally."""
    kind={'Destroyer':'DD','Cruiser':'CA','Battleship':'BB'}.get(kind,kind)
    try:original=(assets or default_assets()).ship(kind,'last_visible' if outline else '',ink,height)
    except (OSError,ValueError):return None
    if original is None:return None
    ratio=original.devicePixelRatio()
    if outline:
        original=original.copy()
        painter=QPainter(original)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(QRectF(0,0,original.width()/ratio,original.height()/ratio),Qt.GlobalColor.black)
        painter.end()
    turned=original.transformed(QTransform().rotate(90),Qt.TransformationMode.SmoothTransformation)
    result=turned.scaled(round(turned.width()*1.5),turned.height(),Qt.AspectRatioMode.IgnoreAspectRatio,Qt.TransformationMode.SmoothTransformation)
    result.setDevicePixelRatio(ratio)
    return result


def constellation_scene(phase_seconds):
    points=[]
    for x,y,vx,vy,size,twinkle in _STAR_SEEDS:
        points.append(((x+vx*phase_seconds+50)%1700-50,(y+vy*phase_seconds+50)%1000-50,size,.7+.3*math.sin(twinkle+phase_seconds*2)))
    links=[]
    for i,a in enumerate(points):
        for b in points[i+1:]:
            distance=math.hypot(a[0]-b[0],a[1]-b[1])
            if distance<CONSTELLATION_LINK_DISTANCE:
                links.append((QPointF(a[0],a[1]),QPointF(b[0],b[1]),(1-distance/CONSTELLATION_LINK_DISTANCE)*.35))
    return points,links


def radar_geometry(scores,center=QPointF(470,428),radius=226):
    """Unknown axes have no vertex or adjacent edge; a recorded zero is central."""
    points=[]
    for index,(key,_) in enumerate(DIMENSIONS):
        value=scores.get(key)
        if value is None:points.append(None);continue
        angle=math.radians(index*45-90);distance=value/120*radius
        points.append(QPointF(center.x()+distance*math.cos(angle),center.y()+distance*math.sin(angle)))
    complete=all(point is not None for point in points);path=QPainterPath()
    if complete:path.addPolygon(QPolygonF(points));path.closeSubpath()
    else:
        for index,first in enumerate(points):
            second=points[(index+1)%8]
            if first is not None and second is not None:path.moveTo(first);path.lineTo(second)
    return points,path,complete


class PosterRenderer:
    def __init__(self,assets=None,minimap=None):
        self.assets=assets or default_naval_assets();self.minimap=minimap or default_assets();self._bounds={};self._text_paths={};self._specialty_images={}
        self.fonts=load_presentation_fonts()
        try:
            self.background_art = self.assets.background('harbor-dossier')
        except (OSError, ValueError):
            self.background_art = None

    def bounds(self,image):
        key=image.cacheKey()
        if key not in self._bounds:
            box=QRectF(0,0,image.width(),image.height())
            if image.hasAlphaChannel():
                rgba=image.convertToFormat(QImage.Format.Format_RGBA8888);raw=rgba.constBits();w=rgba.width();stride=rgba.bytesPerLine()
                left,right,top,bottom=w,0,rgba.height(),0
                for y in range(rgba.height()):
                    alpha=bytes(raw[y*stride+3:y*stride+w*4:4]);start=w-len(alpha.lstrip(b'\0'))
                    if start==w:continue
                    left=min(left,start);right=max(right,len(alpha.rstrip(b'\0')));top=min(top,y);bottom=y+1
                if right>left and bottom>top:
                    left=max(0,left-1);top=max(0,top-1);right=min(w,right+1);bottom=min(rgba.height(),bottom+1)
                    box=QRectF(left,top,right-left,bottom-top)
            self._bounds[key]=box
        return self._bounds[key]

    def wrapped(self,p,rect,value,maximum=48,minimum=26,ink='white',bold=True,family=None):
        box=QRectF(*rect);flags=Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter|Qt.TextFlag.TextWordWrap|Qt.TextFlag.TextWrapAnywhere
        for size in range(maximum,minimum-1,-1):
            selected=font(size,bold)
            if family:selected.setFamily(family)
            p.setFont(selected);bound=p.fontMetrics().boundingRect(box.toRect(),int(flags),str(value))
            if bound.height()<=box.height() and bound.width()<=box.width()+2:break
        p.save();p.setClipRect(box);p.setPen(color(ink));p.drawText(box,flags,str(value));p.restore()

    def art_text(self,p,rect,value,maximum=55,minimum=14,*,phase_seconds=2.4,serif=False,center=False,font_role=None,tint=None,on_paper=False):
        font_role=font_role or ('grade' if serif else 'display')
        family={'heading':self.fonts['chinese_tech'],'callsign':self.fonts['tech']}.get(font_role) or self.fonts[font_role]
        box=QRectF(*rect);value=str(value);key=(tuple(rect),value,maximum,minimum,serif,center,font_role,family)
        if key not in self._text_paths:
            from PySide6.QtGui import QFontMetricsF
            for size in range(maximum,minimum-1,-1):
                selected=QFont(family);selected.setPixelSize(size);selected.setBold(font_role in ('display','grade','heading','callsign'));selected.setItalic(serif)
                metrics=QFontMetricsF(selected);lines=[];current=''
                for char in value:
                    if char=='\n':lines.append(current);current='';continue
                    if current and metrics.horizontalAdvance(current+char)>box.width():lines.append(current);current=char
                    else:current+=char
                lines.append(current)
                path=QPainterPath();advance=max(metrics.horizontalAdvance(line) for line in lines)
                for index,line in enumerate(lines):
                    x=(advance-metrics.horizontalAdvance(line))/2 if center else 0
                    path.addText(QPointF(x,metrics.ascent()+index*metrics.lineSpacing()),selected,line)
                bounds=path.boundingRect()
                if bounds.width()<=box.width() and bounds.height()<=box.height():break
            dx=box.left()-bounds.left()+((box.width()-bounds.width())/2 if center else 0)
            dy=box.top()-bounds.top()+(box.height()-bounds.height())/2
            path=QTransform.fromTranslate(dx,dy).map(path)
            if len(self._text_paths)>160:self._text_paths.clear()
            self._text_paths[key]=path
        path=self._text_paths[key];p.save();p.setClipRect(box.adjusted(-15,-10,15,10));p.setBrush(Qt.BrushStyle.NoBrush)
        if on_paper:
            p.fillPath(path,color(palette.INK));p.restore();return
        p.setPen(QPen(color(tint or 'accent',28),2,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin));p.drawPath(path)
        metal=QLinearGradient(0,box.top(),0,box.bottom())
        stops=((0,QColor(tint).lighter(112).name()),(.55,tint),(1,QColor(tint).darker(108).name())) if tint else ((0,'#FFFFFF'),(.6,'#EDFAFF'),(1,'#B5DCF1'))
        for at,ink in stops:metal.setColorAt(at,color(ink))
        p.fillPath(path,metal);p.setClipPath(path)
        shine_x=box.left()-180+(phase_seconds*.1%1)*(box.width()+360);shine=QLinearGradient(shine_x,box.top(),shine_x+180,box.bottom())
        shine.setColorAt(0,color('#FFFFFF',0));shine.setColorAt(.5,color('#FFFFFF',80));shine.setColorAt(1,color('#FFFFFF',0));p.fillRect(box,shine);p.restore()

    def effects(self,p,phase):
        p.save();p.setClipRect(QRectF(28,28,1544,844))
        for index,(x,y,radius,ink) in enumerate(((360,285,350,'#28649D'),(730,550,370,'#1C7E92'),(730,820,270,'#663958'))):
            center=QPointF(x+math.sin(phase*.1+index)*25,y+math.cos(phase*.12+index)*20)
            haze=QRadialGradient(center,radius);haze.setColorAt(0,color(ink,35));haze.setColorAt(.5,color(ink,16));haze.setColorAt(1,color(ink,0))
            p.fillRect(QRectF(x-radius-30,y-radius-30,radius*2+60,radius*2+60),haze)
        points,links=constellation_scene(phase)
        for first,second,alpha in links:
            p.setPen(QPen(color(palette.CYAN,round(alpha*215)),.8));p.drawLine(first,second)
        for x,y,size,flicker in points:
            ink=palette.PINK if size>2 else palette.CYAN
            p.fillRect(QRectF(x-size,y-size,2*size,2*size),color(ink,round(150*flicker)))
        p.restore()

    def _paint_background(self, painter, with_art):
        poster = QRectF(0, 0, 1600, 900)
        base = QLinearGradient(0, 0, 950, 900)
        base.setColorAt(0, color('#0B1628'))
        base.setColorAt(.58, color('#152A43'))
        base.setColorAt(1, color('#111A2C'))
        painter.fillRect(poster, base)
        if with_art:
            image = self.background_art
            painter.save()
            painter.setOpacity(.88)
            painter.drawImage(poster, image, _cover_source(image, poster))
            painter.restore()
            # Dampen background contrast behind the chart and open the outer edge.
            wash = QLinearGradient(0, 0, 1600, 0)
            wash.setColorAt(0, color('#0B1628', 180))
            wash.setColorAt(.51, color('#101D35', 164))
            wash.setColorAt(.65, color('#11243D', 102))
            wash.setColorAt(1, color('#11243D', 18))
            painter.fillRect(poster, wash)

    def paint(self,p,card,scores=None,phase_seconds=2.4,comparison=None,arrival=1.,reference_opacity=1.,pointer=None,*,background_enabled=True):
        p.setRenderHints(QPainter.RenderHint.Antialiasing|QPainter.RenderHint.SmoothPixmapTransform|QPainter.RenderHint.TextAntialiasing)
        scores=card.get('scores',{}) if scores is None else scores
        with_art = background_enabled and self.background_art is not None and not self.background_art.isNull()
        self._paint_background(p, with_art)
        self.effects(p,phase_seconds)
        def plate(vertices,ink,alpha=None):
            p.setPen(Qt.PenStyle.NoPen);p.setBrush(ink if isinstance(ink, QLinearGradient) else color(ink,alpha))
            p.drawPolygon(QPolygonF([QPointF(x,y) for x,y in vertices]))
        if with_art:
            plate([(931,0),(962,0),(918,900),(873,900)],palette.BLUE,190)
            paper = QLinearGradient(0, 0, 0, 900)
            paper.setColorAt(0, color(palette.CANVAS, 184))
            paper.setColorAt(.89, color(palette.CANVAS, 174))
            paper.setColorAt(1, color(palette.CANVAS, 105))
            plate([(959,0),(1600,0),(1600,900),(914,900)],paper)
        else:
            plate([(931,0),(1600,0),(1600,900),(873,900)],palette.BLUE)
        plate([(940,0),(967,0),(921,900),(894,900)],palette.PINK,213 if with_art else None)
        if not with_art:
            plate([(959,0),(1600,0),(1600,900),(914,900)],palette.CANVAS)
        # Offset sheets give the identity panel a visible thickness without tilting its text.
        plate([(971,335),(1546,335),(1546,618),(950,618)],'#C9D7E7')
        plate([(1530,585),(1600,511),(1600,900),(1435,900)],'#F6DFEF',96 if with_art else None)
        plate([(968,0),(1600,0),(1600,900),(1484,900),(1558,0)],palette.ICE,142 if with_art else None)
        plate([(1530,0),(1600,0),(1600,77)],palette.BLUE)
        plate([(1503,0),(1518,0),(1550,36),(1535,36)],palette.PINK)
        plate([(0,27),(304,27),(280,86),(0,86)],palette.BLUE)
        plate([(0,86),(280,86),(270,101),(0,101)],'#07111F')
        plate([(0,90),(147,90),(140,96),(0,96)],palette.PINK)
        p.fillRect(QRectF(20,39,6,30),color(palette.CYAN))
        self.art_text(p,(43,34,220,43),'选手档案',maximum=37,minimum=27,phase_seconds=phase_seconds,font_role='heading')
        p.setPen(QPen(color('line'),1));p.drawLine(34,107,295,107)
        if comparison:
            self.comparison_legend(p,card,comparison)
        self.radar(p,card,scores,phase_seconds,comparison,reference_opacity)
        p.save()
        p.translate(0,-13*(1.-arrival))
        self.identity(p,card,phase_seconds)
        p.restore()
        p.save()
        offset=pointer or QPointF()
        p.translate(24*(1.-arrival)+offset.x(),offset.y())
        p.setOpacity(.6+.4*arrival)
        self.ship(p,card,phase_seconds)
        p.restore()
        self.role(p,card)
        self.overall(p,card,phase_seconds,scores)
        p.fillRect(QRectF(980,852,120,3),color(palette.BLUE));p.fillRect(QRectF(1106,852,18,3),color(palette.PINK))

    def comparison_legend(self,p,card,comparison):
        for x,player,ink,style in ((320,card,palette.CYAN,Qt.PenStyle.SolidLine),(622,comparison,palette.PINK,Qt.PenStyle.DashLine)):
            p.setPen(QPen(color(ink),3,style));p.drawLine(x,59,x+35,59)
            p.setFont(font(21,True))
            label=p.fontMetrics().elidedText(str(player.get('player_name') or '当前选手'),Qt.TextElideMode.ElideRight,229)
            text(p,(x+42,43,232,32),label,21,'white',True)

    def radar(self,p,card,scores,phase_seconds=2.4,comparison=None,reference_opacity=1.):
        center=QPointF(470,428);radius=226
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color('#030B15',135));p.drawEllipse(center+QPointF(8,12),248,248)
        dish=QRadialGradient(center-QPointF(60,80),350)
        dish.setColorAt(0,color('#264A68',220));dish.setColorAt(1,color('#091B2E',245))
        p.setBrush(dish);p.drawEllipse(center,248,248)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(color('#4E7596',140),1.2));p.drawArc(QRectF(220,178,500,500),20*16,154*16)
        p.setPen(QPen(color('#06111C'),4));p.drawArc(QRectF(221,179,498,498),197*16,142*16)
        def point(i,value):
            angle=math.radians(i*45-90);r=value/120*radius
            return QPointF(center.x()+r*math.cos(angle),center.y()+r*math.sin(angle))
        p.setPen(QPen(color('cyan',62),1));p.setBrush(Qt.BrushStyle.NoBrush);p.drawEllipse(center,246,246)
        for orbit_radius,begin,span,ink in ((266,phase_seconds*4,55,'cyan'),(266,180+phase_seconds*4,55,'cyan'),(258,80-phase_seconds*3,35,palette.PINK),(258,260-phase_seconds*3,35,palette.PINK)):
            p.setPen(QPen(color(ink,70),.9));p.drawArc(QRectF(center.x()-orbit_radius,center.y()-orbit_radius,orbit_radius*2,orbit_radius*2),round(begin*16),span*16)
        for i in range(72):
            a=math.radians(i*5);r=250 if i%3 else 254
            p.setPen(QPen(color('accent' if i%9==0 else 'cyan',130 if i%9==0 else 50),2 if i%9==0 else 1))
            p.drawLine(QPointF(center.x()+246*math.cos(a),center.y()+246*math.sin(a)),QPointF(center.x()+r*math.cos(a),center.y()+r*math.sin(a)))
        for value in (20,40,60,80,100,120):
            p.setPen(QPen(color('cyan',95 if value==120 else 38),1.2 if value==120 else .8))
            p.drawPolygon(QPolygonF([point(i,value) for i in range(8)]))
        for i in range(8):
            p.setPen(QPen(color('cyan',42),.8));p.drawLine(center,point(i,120))
        points,path,complete=radar_geometry(scores,center,radius)
        if complete:
            fill=QLinearGradient(240,240,690,640);fill.setColorAt(0,color(palette.CYAN,108));fill.setColorAt(1,color(palette.BLUE,49))
            p.fillPath(path,fill)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for width,alpha in ((10,18),(5,48),(2.7,255)):
            p.setPen(QPen(color('accent',alpha),width,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin));p.drawPath(path)
        if comparison:
            p.save();p.setOpacity(reference_opacity)
            other_points,other_path,_=radar_geometry(comparison['scores'],center,radius)
            p.setPen(QPen(color(palette.PINK),3,Qt.PenStyle.DashLine));p.drawPath(other_path)
            p.setPen(Qt.PenStyle.NoPen);p.setBrush(color(palette.PINK))
            for node in other_points:
                if node is not None:p.drawRect(QRectF(node.x()-3,node.y()-3,6,6))
            p.restore()
        for index,pt in enumerate(points):
            if pt is not None:
                pulse=9+(math.sin(phase_seconds*2.2+index)+1)*2.2
                p.setPen(Qt.PenStyle.NoPen);p.setBrush(color('cyan',26));p.drawEllipse(pt,pulse,pulse);p.setBrush(color('white'));p.drawEllipse(pt,3.6,3.6)
        p.setPen(QPen(color('accent',125),1));p.drawLine(QPointF(462,428),QPointF(478,428));p.drawLine(QPointF(470,420),QPointF(470,436))
        placements=((386,96,168,88),(704,188,180,88),(748,386,150,88),(704,608,180,88),(386,686,168,88),(106,608,180,88),(42,386,150,88),(106,188,180,88))
        for i,((key,label),box) in enumerate(zip(DIMENSIONS,placements)):
            x,y,w,h=box;value=(card.get('scores') or {}).get(key)
            badge=QPainterPath()
            badge.addPolygon(QPolygonF([QPointF(x+10,y-2),QPointF(x+w-4,y-2),
                                         QPointF(x+w-17,y+25),QPointF(x+3,y+25)]))
            p.fillPath(QTransform.fromTranslate(2,4).map(badge),color('#050C16',200))
            p.fillPath(badge,color('#1C3955',235))
            p.setPen(QPen(color(palette.PINK if i%3==0 else palette.CYAN,145),1))
            p.drawLine(QPointF(x+13,y-2),QPointF(x+43,y-2))
            text(p,(x,y-2,w,27),label,22,'white',False,align=Qt.AlignmentFlag.AlignCenter,family=self.fonts['chinese_tech'])
            if number(value):self.art_text(p,(x,y+23,w,48),grade(value),maximum=64,minimum=42,serif=True,center=True,tint=grade_color(scores.get(key)),phase_seconds=phase_seconds)
            else:text(p,(x,y+25,w,45),'—',38,'muted',True,True,True,Qt.AlignmentFlag.AlignHCenter|Qt.AlignmentFlag.AlignVCenter)

    def identity(self,p,card,phase_seconds=2.4):
        team=card.get('team') or '未设置队伍'
        p.fillRect(QRectF(982,74,22,5),color(palette.PINK))
        self.wrapped(p,(983,93,550,57),team,maximum=30,minimum=14,ink=palette.BLUE,bold=False,family=self.fonts['chinese_tech'] if any('㐀'<=ch<='鿿' for ch in team) else self.fonts['tech'])
        name=card.get('player_name') or '待录入选手'
        self.art_text(p,(982,157,550,116),name,maximum=68,minimum=14,phase_seconds=phase_seconds,
                      font_role='heading' if any('㐀'<=ch<='鿿' for ch in name) else 'callsign',on_paper=True)
        region={'asia':'ASIA','eu':'EUROPE','na':'AMERICAS','com':'AMERICAS'}.get(card.get('region'),'REGION —')
        text(p,(984,283,536,24),region,17,palette.MUTED,True,family='Bahnschrift')
        p.setPen(QPen(color(palette.LINE),1));p.drawLine(982,318,1540,318)

    def ship(self,p,card,phase_seconds=2.4):
        ship_id=card.get('ship_id');info=self.assets.ship_info(ship_id)
        try:image=self.assets.ship_image(ship_id) if ship_id else None
        except (OSError,ValueError):image=None
        available=QRectF(983,355,555,210)
        stage=QPainterPath()
        stage.addPolygon(QPolygonF([QPointF(978,335),QPointF(1564,335),QPointF(1564,590),
                                    QPointF(1537,615),QPointF(957,615),QPointF(957,357)]))
        stage.closeSubpath()
        backdrop=QLinearGradient(960,340,1540,600)
        backdrop.setColorAt(0,color('#D8EAF7'));backdrop.setColorAt(.55,color('#F4F9FD'));backdrop.setColorAt(1,color('#EAE3F2'))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.fillPath(stage,backdrop)
        p.setPen(QPen(color('#365777',48),3));p.drawPath(QTransform.fromTranslate(3,5).map(stage))
        p.fillPath(stage,backdrop)
        p.setPen(QPen(color('#7DA8CE',190),1));p.drawPath(stage)
        p.save();p.setClipPath(stage)
        p.setPen(QPen(color('#5995C4',37),1))
        for x in range(900,1600,44):p.drawLine(x,336,x-110,615)
        for y in range(358,616,40):p.drawLine(957,y,1564,y)
        p.restore()
        p.fillRect(QRectF(982,342,53,3),color(palette.BLUE))
        if image is not None:
            source=self.bounds(image);scale=min(available.width()/source.width(),available.height()/source.height())
            target=QRectF(available.center().x()-source.width()*scale/2,available.center().y()-source.height()*scale/2,source.width()*scale,source.height()*scale)
            p.drawImage(target,image,source)
            title=english_ship_titles().get(str(ship_id),info.get('name') or '')
            self.wrapped(p,(984,570,530,35),title,maximum=28,minimum=14,ink=palette.INK,bold=False,
                         family=self.fonts['display'] if title.isascii() else self.fonts['chinese_tech'])
        else:
            text(p,(990,430,530,70),'未设置代表舰船',25,palette.MUTED,align=Qt.AlignmentFlag.AlignCenter)

    def role(self,p,card):
        values=card.get('specialties') or [];names={'Destroyer':('DD','驱逐'),'Cruiser':('CA','巡洋'),'Battleship':('BB','战列')}
        text(p,(985,640,200,27),'擅长舰种',22,palette.MUTED,False,family=self.fonts['chinese_tech'])
        for i,key in enumerate(values[:3]):
            if key not in names:continue
            kind,label=names[key];x=987+i*177
            if kind not in self._specialty_images:self._specialty_images[kind]=specialty_icon_image(kind,self.minimap,ink=palette.BLUE,height=30)
            icon=self._specialty_images[kind]
            if icon is not None:
                width=icon.width()/icon.devicePixelRatio();height=icon.height()/icon.devicePixelRatio();p.drawImage(QRectF(x,692-height/2,width,height),icon)
            text(p,(x+55,672,120,40),label,25,palette.INK,False,family=self.fonts['chinese_tech'])
        if not values:text(p,(987,672,530,40),'—',24,palette.MUTED)
        p.setPen(QPen(color(palette.LINE),1));p.drawLine(QPointF(985,722),QPointF(1537,722))
        self.wrapped(p,(987,733,550,93),card.get('role') or '—',maximum=26,minimum=10,ink=palette.INK,bold=False,family=self.fonts['chinese_tech'])

    def overall(self,p,card,phase_seconds=2.4,scores=None):
        value=card.get('overall_score')
        paint_layered_panel(p,QRectF(44,773,839,93),dark=True,accent=palette.PINK,cut=20)
        text(p,(70,796,178,36),'综合评分',27,'white',False,family=self.fonts['chinese_tech'])
        self.art_text(p,(302,790,294,43),formatted(value)+' / 120' if number(value) else '未完成',maximum=40,minimum=27,phase_seconds=phase_seconds,font_role='grade' if number(value) else 'chinese_display')
        values=list((scores or card['scores']).values())
        animated_score=sum(values)/8 if len(values)==8 and all(number(v) for v in values) else value
        self.art_text(p,(705,789,140,51),grade(value) if number(value) else f'{card.get("known_count",0)} / 8',maximum=68,minimum=42,phase_seconds=phase_seconds,serif=True,center=True,tint=grade_color(animated_score) if number(value) else None)


class RadarCanvas(QWidget):
    """A 16:9 canvas. Widget previews letterbox; PNGs always use final values."""
    LOGICAL_SIZE=QSize(1600,900)
    EXPORT_PHASE_SECONDS=2.4

    def __init__(self,parent=None,assets=None,minimap_assets=None):
        super().__init__(parent)
        self.setObjectName('playerRadarCanvas')
        self.setProperty('backgroundEffect', True)
        self.background_enabled = True
        self.setMinimumSize(320,180)
        policy=QSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Expanding)
        policy.setHeightForWidth(True);self.setSizePolicy(policy)
        self.renderer=PosterRenderer(assets,minimap_assets)
        self._comparison=None
        self._reference_clock=QElapsedTimer()
        self._arrival=1.
        self._pointer=QPointF()
        self._pointer_target=QPointF()
        self.setMouseTracking(True)
        self._card=evaluate_card({});self._display_scores=dict(self._card['scores'])
        self._start_scores=dict(self._display_scores)
        self.animation=QVariantAnimation(self);self.animation.setStartValue(0.0);self.animation.setEndValue(1.0)
        self.animation.setDuration(700);self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self._animate)
        self.animation.finished.connect(self._animation_finished)
        self.effects_timer=QTimer(self);self.effects_timer.setInterval(42);self.effects_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.effects_timer.timeout.connect(self._advance_effects);self.effects_clock=QElapsedTimer()
        self._phase_seconds=self.EXPORT_PHASE_SECONDS;self._phase_origin=self._phase_seconds
        self._motion_window=None
        self.setAccessibleName('选手八维图预览')

    @property
    def card(self):return deepcopy(self._card)

    def set_background_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled != self.background_enabled:
            self.background_enabled = enabled
            self.update()

    def set_comparison(self,card=None):
        value=evaluate_card(card) if card is not None else None
        if self._comparison==value:return
        self._comparison=value
        if value is not None and self.effects_timer.isActive():
            self._reference_clock.start()
        else:self._reference_clock.invalidate()
        self.update()

    def sizeHint(self):return QSize(960,540)

    def minimumSizeHint(self):return QSize(320,180)

    def heightForWidth(self,width):return max(1,round(width*9/16))

    def set_card(self,card=None,animate=False):
        value=evaluate_card({} if card is None else card)
        previous=dict(self._display_scores)
        self.animation.stop();self._card=value
        self.setAccessibleDescription('；'.join(label+' '+formatted(value['scores'][key]) for key,label in DIMENSIONS))
        if animate and self.isVisible() and not self.window().isMinimized() and motion_enabled(self):
            self._start_scores={key:previous[key] if number(previous.get(key)) else 0.0 for key,_ in DIMENSIONS}
            self._animate(0.0);self.animation.start()
        else:
            self._arrival=1.;self._display_scores=dict(value['scores']);self.update()

    def _animate(self,progress):
        progress=float(progress)
        self._arrival=progress
        self._display_scores={key:None if target is None else self._start_scores.get(key,0.0)+(target-self._start_scores.get(key,0.0))*progress
            for key,target in self._card['scores'].items()}
        self.update()

    def _animation_finished(self):
        self._arrival=1.;self._display_scores=dict(self._card['scores']);self.update()

    def showEvent(self,event):
        super().showEvent(event)
        subscribe_motion(self,self._sync_motion)
        window=self.window()
        if window is not self._motion_window:
            if self._motion_window is not None:
                try:self._motion_window.removeEventFilter(self)
                except RuntimeError:pass
            self._motion_window=window;window.installEventFilter(self)
        self._sync_motion()

    def _sync_motion(self,*_):
        if motion_enabled(self) and self.isVisible() and not self.window().isMinimized():
            if not self.effects_timer.isActive():
                self._phase_origin=self._phase_seconds;self.effects_clock.start();self.effects_timer.start()
        else:
            self._pointer=QPointF();self._pointer_target=QPointF()
            self._reference_clock.invalidate();self.effects_timer.stop();self.animation.stop();self._animation_finished()

    def mouseMoveEvent(self,event):
        if self.effects_timer.isActive():
            self._pointer_target=QPointF(
                6*(2*event.position().x()/max(1,self.width())-1),
                3.5*(2*event.position().y()/max(1,self.height())-1))
        super().mouseMoveEvent(event)

    def leaveEvent(self,event):
        self._pointer_target=QPointF()
        super().leaveEvent(event)

    def eventFilter(self,watched,event):
        if watched is self._motion_window and event.type() in (QEvent.Type.WindowStateChange,QEvent.Type.Show):
            self._sync_motion()
        return super().eventFilter(watched,event)

    def _advance_effects(self):
        if not self.isVisible() or self.window().isMinimized() or not motion_enabled(self):
            self._sync_motion();return
        self._phase_seconds=self._phase_origin+self.effects_clock.elapsed()/1000
        self._pointer += (self._pointer_target-self._pointer)*.16
        self.update()

    def hideEvent(self,event):
        self._pointer=QPointF();self._pointer_target=QPointF()
        self._reference_clock.invalidate();self.effects_timer.stop();self.animation.stop();self._animation_finished();super().hideEvent(event)

    def closeEvent(self,event):
        self.effects_timer.stop();self.animation.stop();super().closeEvent(event)

    def paintEvent(self,event):
        painter=QPainter(self)
        alpha=min(1.,self._reference_clock.elapsed()/180) if self._reference_clock.isValid() else 1.
        try:self._paint_fitted(painter,self.width(),self.height(),self._display_scores,phase_seconds=self._phase_seconds,arrival=self._arrival,reference_opacity=alpha,pointer=self._pointer)
        finally:painter.end()

    def _paint_fitted(self,painter,width,height,scores,phase_seconds=2.4,arrival=1.,reference_opacity=1.,pointer=None):
        painter.fillRect(QRectF(0,0,width,height),color(palette.CANVAS))
        scale=min(width/1600,height/900)
        painter.save();painter.translate((width-1600*scale)/2,(height-900*scale)/2);painter.scale(scale,scale)
        self.renderer.paint(painter,self._card,scores,phase_seconds=phase_seconds,comparison=self._comparison,arrival=arrival,
                            reference_opacity=reference_opacity,pointer=pointer,background_enabled=self.background_enabled)
        painter.restore()

    def render_image(self,width=1920,height=1080):
        if (type(width) is not int or type(height) is not int or not 1<=width<=8192 or not 1<=height<=8192
                or width*height>40_000_000):raise ValueError('导出尺寸无效或超过 4000 万像素')
        image=QImage(width,height,QImage.Format.Format_ARGB32_Premultiplied)
        if image.isNull():raise MemoryError('无法分配导出图像')
        image.setText('Title',self._card.get('player_name') or '选手八维图')
        painter=QPainter(image)
        try:self._paint_fitted(painter,width,height,self._card['scores'],phase_seconds=self.EXPORT_PHASE_SECONDS)
        finally:painter.end()
        return image

    def export_png(self,path,width=2560,height=1440):
        destination=Path(path).expanduser().absolute()
        if destination.suffix.lower()!='.png':raise ValueError('请选择 PNG 图片文件')
        image=self.render_image(width,height)
        stream=QSaveFile(str(destination));stream.setDirectWriteFallback(False)
        if not stream.open(QIODevice.OpenModeFlag.WriteOnly):raise OSError('无法写入图片：'+stream.errorString())
        try:
            if not image.save(stream,'PNG'):raise OSError('PNG 图像编码失败')
            if not stream.commit():raise OSError('图片保存失败：'+stream.errorString())
        except BaseException:
            stream.cancelWriting();stream.commit();raise
        return destination


