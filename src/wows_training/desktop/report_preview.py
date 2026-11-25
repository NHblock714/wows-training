"""A quiet archive-paper cover for reports that have not been generated yet."""
from __future__ import annotations

import math

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
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


def _paint_folio(painter, center, scale, phase, decorated):
    """Draw the folded cover in print units so its details scale together."""
    painter.save()
    painter.translate(center)
    painter.scale(scale, scale)
    page = QRectF(-82, -85, 164, 170)
    face = _page_path(page, 18)
    for x, y, angle, shade in ((-6, 3, -6, '#D9E7F4'), (7, 2, 5, '#EFE1EC')):
        painter.save()
        painter.translate(x, y)
        painter.rotate(angle)
        painter.setPen(QPen(QColor('#B2C4D5'), .8))
        painter.setBrush(QColor(shade))
        painter.drawPath(face)
        painter.restore()

    stock = QLinearGradient(page.topLeft(), page.bottomRight())
    stock.setColorAt(0, QColor('#FFFFFF'))
    stock.setColorAt(.65, QColor('#F7FAFD'))
    stock.setColorAt(1, QColor('#E5EDF5'))
    painter.setPen(QPen(QColor('#93ACC3'), 1))
    painter.setBrush(stock)
    painter.drawPath(face)
    painter.save()
    painter.setClipPath(face, Qt.ClipOperation.IntersectClip)
    # Stitched cloth spine and two stamped rules along its edge.
    painter.fillRect(QRectF(-82, -85, 16, 170), QColor('#294966'))
    painter.fillRect(QRectF(-68, -85, 2, 170), QColor('#6F9BB5'))
    painter.setPen(QPen(QColor('#A3BECC'), .8, Qt.PenStyle.DashLine))
    painter.drawLine(QPointF(-74, -81), QPointF(-74, 81))
    painter.setPen(QPen(QColor('#C4D6E4'), .8))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(QRectF(-58, -74, 129, 146))
    painter.fillRect(QRectF(-44, -60, 26, 2.4), QColor(colors.BLUE))
    painter.fillRect(QRectF(-15, -60, 7, 2.4), QColor(colors.PINK))
    fold = QPainterPath(QPointF(64, -85))
    fold.lineTo(64, -67)
    fold.lineTo(82, -67)
    fold.closeSubpath()
    painter.fillPath(fold, QColor('#A9C3D6'))

    # A blind-stamped wind rose has no ranks, results or numerical scale.
    painter.translate(8, 2)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor('#ACC4D5'), 1))
    painter.drawEllipse(QPointF(), 43, 43)
    painter.setPen(QPen(QColor('#9EBDCD'), .7))
    painter.drawEllipse(QPointF(), 36, 36)
    if decorated:
        for i in range(32):
            angle = i * math.tau / 32
            dx, dy = math.cos(angle), math.sin(angle)
            inner = 37 if i % 4 == 0 else 40
            painter.drawLine(QLineF(inner * dx, inner * dy, 42 * dx, 42 * dy))
    shine = 12 * math.sin(phase * .48)
    foil = QLinearGradient(-32 + shine, -27, 30 + shine, 27)
    for stop, shade in ((0, '#416989'), (.33, '#8AB7C7'), (.50, '#D9E9EE'), (.65, '#8CABBE'), (1, '#537795')):
        foil.setColorAt(stop, QColor(shade))
    painter.rotate(15)
    for i in range(8):
        radius = 48 if i % 2 == 0 else 27
        facet = QPainterPath(QPointF(0, 0))
        facet.lineTo(-5, -5)
        facet.lineTo(0, -radius)
        facet.closeSubpath()
        painter.fillPath(facet, QColor('#416784'))
        painter.save()
        painter.scale(-1, 1)
        painter.fillPath(facet, foil)
        painter.restore()
        painter.rotate(45)
    painter.setPen(QPen(QColor('#426781'), 1))
    painter.setBrush(QColor('#ECF5FA'))
    painter.drawEllipse(QPointF(), 2.7, 2.7)
    painter.restore()
    painter.restore()


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

    def showEvent(self, event):
        subscribe_motion(self, self._motion_changed)
        if not motion_enabled(self):
            self._phase = 0.
        super().showEvent(event)

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
        if self._background_enabled:
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

        # Keep the title, illustration and status in separate bands at compact heights.
        field_top = 96.
        field_bottom = height - 67.
        scale = min(1.45, (width - 72) / 198, (field_bottom - field_top) / 194)
        state_y = max(92., height - 59.)
        if scale >= .38:
            center = QPointF(inner.center().x(), (field_top + field_bottom) / 2)
            _paint_folio(painter, center, scale, self._phase, self._background_enabled)
            state_y = min(state_y, center.y() + scale * 98 + 12)
        state=QRectF(inner.center().x()-55,state_y,110,27)
        painter.setPen(QPen(QColor('#BDD1E5'),1))
        painter.setBrush(QColor(242,248,254,235))
        painter.drawRoundedRect(state,4,4)
        painter.setFont(small)
        painter.setPen(QColor('#4C6D8D'))
        painter.drawText(state,Qt.AlignmentFlag.AlignCenter,'尚未生成')
        painter.end()
