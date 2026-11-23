"""A readable single-ship dossier beside the account comparison radar."""
from copy import deepcopy
import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy

from . import academy_palette as tone
from .chart_motion import ChartReveal
from .layered_panel import LayeredPanel, paint_layered_panel, panel_path
from .naval_assets import default_naval_assets
from .naval_widgets import english_ship_titles
from .presentation_fonts import load_presentation_fonts


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _count(value):
    return f'{value:,.0f}' if _number(value) and value >= 0 else '未知'


def _percent(value):
    return f'{value:.1%}' if _number(value) and 0 <= value <= 1 else '未知'


class ShipComparisonSummary(LayeredPanel):
    """Keep public figures and score confidence distinct, including unknowns."""

    def __init__(self, parent=None, *, assets=None):
        super().__init__(parent, accent=tone.BLUE)
        self.setObjectName('shipComparisonSummary')
        self.setMinimumWidth(290)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.assets = assets or default_naval_assets()
        self.fonts = load_presentation_fonts()
        self.accounts = []
        self.ship_id = None
        self.ship_name = ''
        self.display_title = ''
        self._image = None
        self._text = ''
        self._text_rects = []
        self._reveal = ChartReveal(self, 300)
        self.clear_summary()

    def sizeHint(self):
        return QSize(440, 378 if len(self.accounts) > 1 else 286)

    def text(self):
        """Plain text for accessibility and existing integrations."""
        return self._text

    def clear_summary(self, message='未选择舰船'):
        self.ship_id = None
        self.ship_name = self.display_title = ''
        self.accounts = []
        self._image = None
        self._text = str(message)
        self.setMinimumHeight(250)
        self.setAccessibleName('单舰记录')
        self.setAccessibleDescription(self._text)
        self.setToolTip(self._text)
        self._reveal.set_content(None)
        self.updateGeometry()
        self.update()

    def set_ship(self, ship_id, name, accounts):
        self.ship_id = str(ship_id)
        self.ship_name = str(name or ship_id)
        self.display_title = english_ship_titles().get(self.ship_id, self.ship_name)
        self.accounts = deepcopy(accounts[:2])
        try:
            self._image = self.assets.ship_image(ship_id, prefer='silhouette')
        except (OSError, ValueError):
            self._image = None
        lines = [self.ship_name]
        for entry in self.accounts:
            record, derived = entry.get('record') or {}, entry.get('ratings') or {}
            entry['values'] = (
                ('场次', _count(derived.get('battles') if _number(derived.get('battles')) else record.get('battles'))),
                ('胜率', _percent(record.get('win_rate'))),
                ('场均伤害', _count(record.get('avg_damage'))),
                ('样本系数', _percent(derived.get('confidence'))),
            )
            lines.append(str(entry.get('name') or '未知账号'))
            lines.extend(label + ' ' + value for label, value in entry['values'])
            if not derived.get('scores'):
                lines.append('维度资料不足')
        self._text = '\n'.join(lines)
        self.setAccessibleName('单舰记录 · ' + self.ship_name)
        self.setAccessibleDescription(self._text)
        self.setToolTip(self._text + '\n样本系数沿用单舰评分的样本口径。')
        self.setMinimumHeight(378 if len(self.accounts) > 1 else 286)
        self._reveal.set_content((self.ship_id, self.accounts))
        self.updateGeometry()
        self.update()

    def _font(self, family, pixels, bold=False):
        value = QFont(family)
        value.setPixelSize(pixels)
        value.setBold(bold)
        return value

    def _draw_text(self, painter, rect, text, font, colour, *, align=Qt.AlignmentFlag.AlignLeft):
        self._text_rects.append(QRectF(rect))
        painter.save()
        painter.setClipRect(rect)
        painter.setFont(font)
        painter.setPen(QColor(colour))
        label = QFontMetricsF(font).elidedText(str(text), Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, align | Qt.AlignmentFlag.AlignVCenter, label)
        painter.restore()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(self.rect()).adjusted(.5,.5,-.5,-.5)
        paint_layered_panel(p, box, accent=tone.BLUE, progress=self.hover_progress)
        p.setClipPath(panel_path(box.adjusted(1,1,-6,-6),18))
        self._text_rects = []
        width = self.width()
        ink_width = max(1.,width-46)
        # Quiet drafting marks and native art tie the record to the ship, without
        # turning the main reading surface into a dark banner.
        header = QRectF(18,12,max(1,width-41),68)
        shade = QLinearGradient(header.topLeft(),header.bottomRight())
        shade.setColorAt(0,QColor(18,99,211,12))
        shade.setColorAt(1,QColor(240,87,166,14))
        p.fillRect(header,shade)
        title_right = width - 55
        if self._image is not None:
            available = QRectF(width*.53,22,max(1,width*.4-24),52)
            scale = min(available.width()/self._image.width(),available.height()/self._image.height())
            target = QRectF(available.right()-self._image.width()*scale,
                            available.center().y()-self._image.height()*scale/2,
                            self._image.width()*scale,self._image.height()*scale)
            p.save()
            p.setOpacity(.15+.06*self._reveal.progress)
            p.drawImage(target,self._image)
            p.restore()
            title_right = min(title_right, available.left() - 10)
        title = self.display_title or '单舰记录'
        title_font = self._font(self.fonts['tech'] if title.isascii() else self.fonts['chinese_tech'],22,True)
        title_width = max(1, title_right - 23)
        while title_font.pixelSize()>16 and QFontMetricsF(title_font).horizontalAdvance(title)>title_width:
            title_font.setPixelSize(title_font.pixelSize()-1)
        self._draw_text(p,QRectF(23,17,title_width,30),title,title_font,tone.INK)
        subtitle = self.ship_name if self.display_title and self.display_title != self.ship_name else '单舰记录'
        self._draw_text(p,QRectF(23,48,title_width,20),subtitle,
                        self._font('Microsoft YaHei UI',12),tone.MUTED)
        p.setPen(QPen(QColor('#C8DCEE'),1))
        p.drawLine(QPointF(24,81),QPointF(width-24,81))
        p.setPen(QPen(QColor(tone.CYAN),2))
        p.drawLine(QPointF(24,81),QPointF(24+max(18,ink_width*.28*self._reveal.progress),81))
        if not self.accounts:
            p.setPen(QPen(QColor('#C1D9EC'),1))
            p.drawRoundedRect(QRectF(31,107,max(1,width-70),self.height()-139),5,5)
            for y in (132,155,178):
                p.drawLine(QPointF(51,y),QPointF(width-57,y))
            self._draw_text(p,QRectF(32,103,max(1,width-72),self.height()-138),self._text,
                            self._font(self.fonts['chinese_tech'],18),tone.MUTED,
                            align=Qt.AlignmentFlag.AlignCenter)
        else:
            row_height = min(160.,(self.height()-112-(len(self.accounts)-1)*10)/len(self.accounts))
            for index, entry in enumerate(self.accounts):
                self._paint_account(p,entry,index,QRectF(23,94+index*(row_height+10),ink_width,row_height))
        p.end()

    def _paint_account(self, p, entry, index, rect):
        colour = '#B92C79' if index else '#076A9F'
        tint = QColor(240,87,166,10) if index else QColor(18,99,211,9)
        p.fillRect(rect,tint)
        pen = QPen(QColor(colour),2)
        if index:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawLine(rect.topLeft()+QPointF(9,15),rect.topLeft()+QPointF(35,15))
        name_font=self._font(self.fonts['tech'],17,True)
        name_rect=QRectF(rect.left()+44,rect.top()+1,max(1,rect.width()-55),28)
        self._draw_text(p,name_rect,str(entry.get('name') or '未知账号'),name_font,colour)
        columns = 2
        cell_width=(rect.width()-20)/columns
        for metric,(label,value) in enumerate(entry['values']):
            col,row=metric%columns,metric//columns
            x=rect.left()+10+col*cell_width
            y=rect.top()+32+row*47
            p.setPen(QPen(QColor('#C9DCEC'),.8))
            if col:
                p.drawLine(QPointF(x-6,y+5),QPointF(x-6,y+44))
            self._draw_text(p,QRectF(x,y,cell_width-16,16),label,
                            self._font('Microsoft YaHei UI',11),tone.MUTED)
            font=self._font(self.fonts['tech'] if value!='未知' else self.fonts['chinese_tech'],22)
            self._draw_text(p,QRectF(x,y+16,cell_width-16,26),value,font,colour)
