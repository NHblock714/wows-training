"""A quiet archive-paper cover for reports that have not been generated yet."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from . import academy_palette as colors
from .layered_panel import paint_layered_panel
from .motion import motion_enabled, subscribe_motion
from .presentation_fonts import load_presentation_fonts


def _page_path(rect, fold=20):
    path=QPainterPath(rect.topLeft())
    path.lineTo(rect.right()-fold,rect.top())
    path.lineTo(rect.right(),rect.top()+fold)
    path.lineTo(rect.right(),rect.bottom())
    path.lineTo(rect.left()+9,rect.bottom())
    path.lineTo(rect.left(),rect.bottom()-9)
    path.closeSubpath()
    return path


class ReportEmptyPage(QWidget):
    """Graphic empty state driven by the existing background clock, never a new timer."""
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setObjectName('reportEmptyPage')
        self.setProperty('backgroundEffect',True)
        self.setAccessibleName('报告预览，尚未生成')
        self._background_enabled=True
        self._phase=0.
        self._title='战术简报'
        self._label='单页简报'
        self.fonts=load_presentation_fonts()
        host=self.window()
        self._backdrop=getattr(host,'backdrop',None)
        if self._backdrop is not None:
            self._backdrop._scene_timer.timeout.connect(self._tick)
        subscribe_motion(self,self._motion_changed)

    def set_cover(self,title,label):
        self._title=str(title)
        self._label=str(label)
        self.update()

    def set_background_enabled(self,enabled):
        self._background_enabled=bool(enabled)
        if not enabled:self._phase=0.
        self.update()

    def _motion_changed(self,enabled):
        if not enabled:self._phase=0.
        self.update()

    def _tick(self):
        if self.isVisible() and self._background_enabled and motion_enabled(self):
            phase=round(self._backdrop._scene_time*8)/8
            if phase!=self._phase:
                self._phase=phase
                self.update()

    def paintEvent(self,event):
        painter=QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_layered_panel(painter,self.rect(),cut=24)
        width,height=self.width(),self.height()
        inner=QRectF(22,20,max(1,width-50),max(1,height-48))
        painter.save()
        painter.setClipRect(inner)
        # The paper field remains pale, with sparse pink and blue drafting marks.
        field=QLinearGradient(inner.topLeft(),inner.bottomRight())
        field.setColorAt(0,QColor(246,251,255,90))
        field.setColorAt(1,QColor(247,224,241,75))
        painter.fillRect(inner,field)
        painter.setPen(QPen(QColor(116,156,195,20),.7))
        for x in range(34,width,30):painter.drawLine(x,75,x,height-28)
        for y in range(88,height,30):painter.drawLine(26,y,width-28,y)
        dots=[QPointF(inner.left()+inner.width()*x,inner.top()+inner.height()*y)
              for x,y in ((.06,.65),(.17,.54),(.27,.71),(.4,.53),(.62,.31),(.74,.46),(.89,.34),(.94,.66))]
        for index in range(len(dots)-1):
            ink=QColor(colors.PINK if index%3==0 else colors.BLUE)
            ink.setAlpha(27)
            painter.setPen(QPen(ink,.8))
            painter.drawLine(dots[index],dots[index+1])
        for index,point in enumerate(dots):
            point=point+QPointF(0,math.sin(self._phase*.25+index)*2)
            ink=QColor(colors.PINK if index%2 else colors.BLUE)
            ink.setAlpha(65)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ink)
            painter.drawEllipse(point,1.7,1.7)
        painter.restore()

        title=QFont(self.fonts['chinese_tech'])
        title.setPixelSize(25)
        painter.setFont(title)
        painter.setPen(QColor(colors.INK))
        caption=QFontMetrics(title).elidedText(self._title,Qt.TextElideMode.ElideRight,max(1,width-98))
        painter.drawText(QRectF(26,21,max(1,width-98),37),Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,caption)
        small=QFont('Microsoft YaHei UI')
        small.setPixelSize(11)
        painter.setFont(small)
        painter.setPen(QColor('#55728E'))
        painter.drawText(QRectF(27,58,max(1,width-90),22),Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,self._label)
        painter.fillRect(QRectF(27,85,min(44,width-60),3),QColor(colors.BLUE))
        painter.fillRect(QRectF(74,85,14,3),QColor(colors.PINK))

        # Two offset leaves and the folded master sheet are a cover, not fake report data.
        page_w=max(106.,min(245.,inner.width()*.51,max(106.,(height-190)/1.05)))
        page_h=page_w*1.05
        center=QPointF(width*.5,min(height*.53,height-page_h*.5-55))
        center.setY(max(156.,center.y()))
        page=QRectF(center.x()-page_w/2,center.y()-page_h/2,page_w,page_h)
        for dx,dy,angle,fill in ((-16,8,-7,'#DDEBFA'),(15,3,6,'#F3E0EF')):
            painter.save()
            painter.translate(center+QPointF(dx,dy))
            painter.rotate(angle)
            shifted=QRectF(-page_w/2,-page_h/2,page_w,page_h)
            painter.setPen(QPen(QColor('#A9C2DA'),1))
            painter.setBrush(QColor(fill))
            painter.drawPath(_page_path(shifted))
            painter.restore()
        painter.setBrush(QColor(29,61,94,16))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(_page_path(page.translated(4,5)))
        painter.setBrush(QColor('#FFFFFF'))
        painter.setPen(QPen(QColor('#91B1D0'),1.2))
        painter.drawPath(_page_path(page))
        fold=QPainterPath(QPointF(page.right()-20,page.top()))
        fold.lineTo(page.right()-20,page.top()+20)
        fold.lineTo(page.right(),page.top()+20)
        fold.closeSubpath()
        painter.fillPath(fold,QColor(colors.NIGHT))
        painter.setPen(QPen(QColor(colors.PINK),1.7))
        painter.drawLine(QPointF(page.right()-18,page.top()+2),QPointF(page.right()-2,page.top()+18))
        painter.fillRect(QRectF(page.left()+17,page.top()+21,page_w*.28,4),QColor(colors.BLUE))
        painter.fillRect(QRectF(page.left()+17,page.top()+31,page_w*.13,2),QColor(colors.PINK))
        # A non-numeric diagram and index marks echo the eventual report layout.
        chart=QRectF(page.left()+22,page.top()+page_h*.34,page_w-44,page_h*.36)
        painter.setPen(QPen(QColor('#D9E5F0'),1))
        painter.drawLine(chart.bottomLeft(),chart.bottomRight())
        painter.drawLine(chart.bottomLeft(),chart.topLeft())
        route=QPainterPath(QPointF(chart.left()+7,chart.bottom()-12))
        for x,y in ((.27,.63),(.52,.76),(.72,.32),(.95,.16)):
            route.lineTo(chart.left()+chart.width()*x,chart.top()+chart.height()*y)
        painter.setPen(QPen(QColor(colors.BLUE),2.3))
        painter.drawPath(route)
        painter.setPen(QPen(QColor(colors.PINK),1.7))
        painter.drawLine(QPointF(chart.left()+8,chart.top()+11),QPointF(chart.right()-5,chart.bottom()-9))
        painter.setPen(QPen(QColor('#C7D9EA'),2))
        for row in range(3):
            y=page.bottom()-40+row*8
            painter.drawLine(QPointF(page.left()+20,y),QPointF(page.right()-22-row*14,y))
        state_y=min(height-65,page.bottom()+28)
        state=QRectF(width/2-64,state_y,128,29)
        painter.setPen(QPen(QColor('#BDD1E5'),1))
        painter.setBrush(QColor(242,248,254,235))
        painter.drawRoundedRect(state,4,4)
        painter.setFont(small)
        painter.setPen(QColor('#4C6D8D'))
        painter.drawText(state,Qt.AlignmentFlag.AlignCenter,'尚未生成')
        painter.end()
