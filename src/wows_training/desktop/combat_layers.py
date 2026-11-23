"""Native WoWs silhouettes and state overlays following minimap-renderer."""
from __future__ import annotations
import math
from PySide6.QtCore import QPointF,QRectF,Qt
from PySide6.QtGui import QColor,QFont,QIcon,QPainter,QPen,QPixmap
from PySide6.QtWidgets import QWidget
from .minimap_assets import default_assets,screen_rotation

SPECIES={'Destroyer':('DD','驱逐舰'),'Cruiser':('CA','巡洋舰'),'Battleship':('BB','战列舰'),
         'AirCarrier':('CV','航空母舰'),'AircraftCarrier':('CV','航空母舰'),'Submarine':('SS','潜艇'),'Auxiliary':('AUX','辅助舰')}
TEAM_COLORS={0:'#50FF50',1:'#FF5050'}


def finite(value):return type(value) in (int,float) and math.isfinite(value)


def ship_kind(species):
    if not isinstance(species,str):return '?'
    return species if species in {'DD','CA','BB','CV','SS','AUX'} else SPECIES.get(species,('?','未知舰种'))[0]


def health_fraction(state):
    hp,maximum=state.get('hp'),state.get('max_hp')
    return hp/maximum if finite(hp) and finite(maximum) and maximum>0 and 0<=hp<=maximum else None


def heading_vector(heading):return (math.cos(heading),-math.sin(heading)) if finite(heading) else None


def quality_allowed(quality,show_estimates=False):return quality=='recorded' or (show_estimates and quality=='estimated')


def spotting_indicator(state,last_known=False):
    return state.get('spotted_by_enemy') is True and state.get('visibility')=='visible' and not last_known


def draw_ship_symbol(painter,center,kind,color,*,visibility='unknown',selected=False,last_known=False,
                     heading=None,hp_fraction=None,spotted=False,size=1.,dead=False,own=False,assets=None):
    assets=assets or default_assets();kind=ship_kind(kind)
    variant='self_dead' if dead and own else 'dead' if dead else 'last_visible' if last_known else 'invisible' if visibility=='not_visible' else 'self' if own else ''
    height=22 if own else 20;image=assets.ship(kind,variant,color,height)
    painter.save();painter.translate(center);painter.scale(size,size)
    if selected:
        painter.setPen(QPen(QColor('#FFDD6F'),1.5));painter.setBrush(Qt.BrushStyle.NoBrush);painter.drawEllipse(QPointF(),16,16)
    if spotted and not dead and not last_known:
        painter.setPen(QPen(QColor('#FFD65C'),1.5));painter.setBrush(QColor(255,202,72,28));painter.drawEllipse(QPointF(),13,13)
    painter.save();angle=screen_rotation(heading)
    if angle is not None:painter.rotate(angle)
    if visibility=='unknown' and not last_known and not dead:painter.setOpacity(.6)
    if image is not None:
        width=image.width()/image.devicePixelRatio();height=image.height()/image.devicePixelRatio()
        painter.drawImage(QRectF(-width/2,-height/2,width,height),image)
    else:
        painter.setPen(QPen(QColor(color),1.3));painter.setBrush(QColor('#152536'));painter.drawEllipse(QPointF(),6,6)
        painter.setPen(QColor('#FFFFFF'));painter.drawText(QRectF(-6,-8,12,16),Qt.AlignmentFlag.AlignCenter,'?')
    painter.restore()
    if hp_fraction is not None and not last_known and not dead:
        painter.setPen(QPen(QColor('#08121C'),.8));painter.setBrush(QColor(35,40,46,220));painter.drawRect(QRectF(-13,14,26,4))
        painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(QColor('#50E86E' if hp_fraction>.5 else '#FFD050' if hp_fraction>.25 else '#FF5050'));painter.drawRect(QRectF(-13,14,26*hp_fraction,4))
    if spotted and not last_known and not dead:
        painter.setPen(QColor('#FFDA59'));font=QFont('Microsoft YaHei UI',9);font.setBold(True);painter.setFont(font)
        painter.drawText(QRectF(13,-15,9,16),Qt.AlignmentFlag.AlignCenter,'!')
    painter.restore()


def class_icon(kind,color='#B7CDD9',assets=None,*,heading=math.pi/2):
    image=QPixmap(30,30);image.fill(Qt.GlobalColor.transparent);painter=QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing);draw_ship_symbol(painter,QPointF(15,15),kind,color,visibility='visible',heading=heading,assets=assets)
    painter.end();return QIcon(image)


class CombatLegend(QWidget):
    def __init__(self,parent=None,assets=None):
        super().__init__(parent);self.assets=assets;self.setObjectName('combatLegend');self.setFixedHeight(154)

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing);painter.setFont(QFont('Microsoft YaHei UI',8))
        width=max(40.,self.width()/5)
        for i,kind in enumerate(('DD','CA','BB','CV','SS')):
            x=width*(i+.5);draw_ship_symbol(painter,QPointF(x,16),kind,'#356D98',visibility='visible',heading=math.pi/2,assets=self.assets)
            painter.setPen(QColor('#34546F'));painter.drawText(QRectF(x-18,32,36,16),Qt.AlignmentFlag.AlignCenter,kind)
        states=[('visible','当前记录',False,False),('not_visible','最后记录',True,False),('visible','已击沉',False,True)]
        for i,(state,text,last,dead) in enumerate(states):
            x=18+i*self.width()/3;draw_ship_symbol(painter,QPointF(x,70),'CA','#23824C',visibility=state,last_known=last,dead=dead,heading=math.pi/2,assets=self.assets)
            painter.setPen(QColor('#34546F'));painter.drawText(QRectF(x-14,85,self.width()/3,17),text)
        painter.setPen(QColor('#34546F'));painter.drawText(QRectF(2,107,self.width()-4,18),'绿：友方 / 队伍 0　红：敌方 / 队伍 1')
        painter.setPen(QColor('#79510B'));painter.drawText(QRectF(2,127,self.width()-4,18),'金色：录制者　!：被对方点亮（记录）')
        painter.end()
