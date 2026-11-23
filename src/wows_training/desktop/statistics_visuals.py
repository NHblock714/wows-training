"""Small native Qt views for statistical comparisons and reversible selection."""
from collections import Counter, defaultdict
from functools import lru_cache
import re

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget
from .naval_assets import default_naval_assets
from .naval_widgets import _visible_image_rect
from .chart_motion import ChartReveal
from . import academy_palette as tone
from .layered_panel import LayeredPanel, fold_content_inset, panel_path


CLASS_ORDER=('Battleship','Cruiser','Destroyer','AirCarrier','Submarine',None)
CLASS_LABELS={'Battleship':'战列','Cruiser':'巡洋','Destroyer':'驱逐','AirCarrier':'航母','Submarine':'潜艇'}
CLASS_COLORS={'Battleship':'#A7B7B4','Cruiser':'#799C91','Destroyer':'#C9A661','AirCarrier':'#A695A6','Submarine':'#9DA980'}
RESULT_COLORS={'wins':'#79A797','losses':'#CC776B','draws':'#C9A661','unknown_results':'#677573'}


def _ship_key(key):
    if isinstance(key,(tuple,list)) and len(key)==2 and isinstance(key[0],str) and key[0].startswith(('account:','player:')):
        key=key[1]
    return str(key) if type(key) is int and key>0 or isinstance(key,str) and key.isdigit() else None


def _art_for_key(key):
    identifier=_ship_key(key)
    if identifier is None:return None
    try:return default_naval_assets().ship_image(identifier)
    except (OSError,ValueError):return None


def _draw_art(painter,image,box,opacity):
    if image is None:return
    scale=min(box.width()/image.width(),box.height()/image.height())
    target=QRectF(box.center().x()-image.width()*scale/2,box.center().y()-image.height()*scale/2,image.width()*scale,image.height()*scale)
    painter.save();painter.setOpacity(opacity);painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform);painter.drawImage(target,image);painter.restore()


@lru_cache(maxsize=64)
def _lineup_art(identifier):
    image = _art_for_key(identifier)
    return image.copy(_visible_image_rect(image).toRect()) if image is not None else None


def lineup_groups(matches):
    """Group actual appearances by class counts; never infer tactical roles."""
    battles=defaultdict(list)
    for row in matches:battles[row['battle_id']].append(row)
    groups={}
    for battle_id,players in battles.items():
        counts=Counter(p.get('ship_class') for p in players)
        signature=tuple(sorted(counts.items(),key=lambda item:str(item[0])))
        ordered=[kind for kind in CLASS_ORDER if kind in counts]
        ordered.extend(sorted((kind for kind in counts if kind not in CLASS_ORDER),key=str))
        label=' / '.join(f'{counts[kind]} {CLASS_LABELS.get(kind,"未知")}' for kind in ordered)
        group=groups.setdefault(signature,{'key':signature,'label':label,'battles':[],'count':0,'class_counts':{kind:counts[kind] for kind in ordered}})
        group['count']+=1
        ships=Counter(p['ship_name'] for p in players)
        group['battles'].append({'battle_id':battle_id,'date':players[0]['date'],'map_name':players[0]['map_name'],
                                'outcome':players[0].get('outcome','unknown'),'players':players,
                                'ships':' · '.join(f'{name} ×{n}' if n>1 else name for name,n in sorted(ships.items()))})
    return sorted(groups.values(),key=lambda g:(-g['count'],g['label']))


class MetricCard(LayeredPanel):
    def __init__(self,label,parent=None):
        super().__init__(parent);self.setProperty('instrument',True);layout=QVBoxLayout(self);layout.setContentsMargins(17,12,24,14);layout.setSpacing(3)
        self.label=QLabel(label);self.label.setObjectName('subtitle');self.value=QLabel('—');self.value.setObjectName('metric')
        self.label.setContentsMargins(0, 0, fold_content_inset(self.cut, 12) - 24, 0)
        self.caption=QLabel('');self.caption.setObjectName('subtitle')
        for widget in (self.label,self.value,self.caption):layout.addWidget(widget)

    def set_value(self,value,caption=''):
        self.value.setText(str(value));self.caption.setText(caption);self.caption.setVisible(bool(caption))


class RankedBars(QWidget):
    activated=Signal(object)
    row_height=66

    def __init__(self,parent=None,compact=False):
        super().__init__(parent);self.items=[];self.selected=None;self.compact=compact;self.row_height=34 if compact else 66
        self.setMouseTracking(True);self.setMinimumWidth(210)
        self._reveal=ChartReveal(self)

    def set_items(self,items,selected=None):
        self.items=items;self.selected=selected;self.setMinimumHeight(max(92,len(items)*self.row_height+8))
        self._reveal.set_content(items if any(isinstance(row.get('ratio'),(int,float)) and row['ratio']>0 for row in items) else None);self.update()

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.items:
            painter.setPen(QColor(tone.MUTED));painter.drawText(self.rect(),Qt.AlignmentFlag.AlignCenter,'暂无可用记录');return
        width=self.width();normal=QFont(self.font());normal.setPointSizeF(9 if self.compact else 10);bold=QFont(normal);bold.setBold(True)
        small=QFont(normal);small.setPointSizeF(8.5)
        first=max(0,event.rect().top()//self.row_height-1);last=min(len(self.items),event.rect().bottom()//self.row_height+2)
        for i in range(first,last):
            item=self.items[i]
            y=i*self.row_height+4;active=item.get('key')==self.selected and self.selected is not None
            painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(QColor(tone.ICE if active else tone.PAPER))
            painter.drawRect(QRectF(0,y,width,self.row_height-4))
            if active:
                painter.fillRect(QRectF(0,y,3,self.row_height-4),QColor(tone.BLUE))
            if not self.compact:
                image=_art_for_key(item.get('key'))
                _draw_art(painter,image,QRectF(max(12,width-235),y+2,min(125,width*.3),self.row_height-8),.19)
            painter.setPen(QPen(QColor(tone.LINE),.7));painter.drawLine(12,int(y+self.row_height-5),width-12,int(y+self.row_height-5))
            painter.setFont(bold);painter.setPen(QColor(tone.INK))
            right=item.get('value','');right_width=min(width*.47,painter.fontMetrics().horizontalAdvance(right)+10)
            title=painter.fontMetrics().elidedText(item.get('title',''),Qt.TextElideMode.ElideRight,max(20,int(width-right_width-28)))
            painter.drawText(QRectF(12,y+5,width-right_width-20,22),Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,title)
            painter.setPen(QColor(tone.BLUE if active else tone.INK));painter.drawText(QRectF(width-right_width-12,y+5,right_width,22),Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter,right)
            if not self.compact:
                painter.setFont(small);painter.setPen(QColor(tone.MUTED))
                text=painter.fontMetrics().elidedText(item.get('subtitle',''),Qt.TextElideMode.ElideRight,max(20,width-24))
                painter.drawText(QRectF(12,y+28,width-24,18),Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,text)
            bar_y=y+(27 if self.compact else 51)
            painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(QColor(tone.LINE if active else tone.ICE));painter.drawRoundedRect(QRectF(12,bar_y,width-24,3 if self.compact else 4),2,2)
            ratio=item.get('ratio')
            if ratio is None:
                painter.setPen(QPen(QColor(tone.MUTED),1,Qt.PenStyle.DotLine));painter.drawLine(12,int(bar_y+2),width-12,int(bar_y+2))
            else:
                bar_width=(width-24)*max(0,min(1,ratio))*self._reveal.progress;segments=item.get('segments')
                if segments:
                    offset=12
                    for fraction,color in segments:
                        painter.setBrush(QColor(color));painter.drawRect(QRectF(offset,bar_y,bar_width*fraction,3 if self.compact else 4));offset+=bar_width*fraction
                else:
                    painter.setBrush(QColor(item.get('color',tone.BLUE)));painter.drawRoundedRect(QRectF(12,bar_y,bar_width,3 if self.compact else 4),2,2)
        painter.end()

    def mouseMoveEvent(self,event):
        index=int((event.position().y()-4)//self.row_height)
        item=self.items[index] if 0<=index<len(self.items) else None
        self.setCursor(Qt.CursorShape.PointingHandCursor if item and item.get('key') is not None else Qt.CursorShape.ArrowCursor)
        self.setToolTip((item.get('tooltip') or '\n'.join((item.get('title',''),item.get('subtitle','')))) if item else '')

    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:
            index=int((event.position().y()-4)//self.row_height)
            if 0<=index<len(self.items) and self.items[index].get('key') is not None:self.activated.emit(self.items[index]['key'])
        super().mousePressEvent(event)


class LineupRoster(QWidget):
    """Show the actual players and ships of one selected battle in a compact grid."""
    ROW_HEIGHT = 64

    def __init__(self,parent=None):
        super().__init__(parent)
        self.players = []
        self._icons = {}
        self.setMinimumWidth(240)
        self.setFixedHeight(self.ROW_HEIGHT)
        self.setMouseTracking(True)
        self.setAccessibleName('逐场阵容')

    def set_players(self,players):
        order={kind:index for index,kind in enumerate(CLASS_ORDER)}
        self.players=sorted(players,key=lambda r:(order.get(r.get('ship_class'),99),r['ship_name'],r['player_name']))
        self._sync_height()
        self.setToolTip('')
        self.setAccessibleDescription('\n'.join(self._description(player) for player in self.players))
        self.update()

    @staticmethod
    def _description(player):
        name = player['player_name']
        detail = f"{player['ship_name']} · {name}"
        tournament = player.get('tournament_name')
        return detail + f'（{tournament}）' if tournament and tournament != name else detail

    def _columns(self):
        return 3 if self.width() >= 402 else 2

    def _sync_height(self):
        columns = self._columns()
        rows = max(1, (len(self.players)+columns-1)//columns)
        height = rows*self.ROW_HEIGHT
        if height != self.minimumHeight():
            self.setFixedHeight(height)

    def _card_rect(self, index):
        columns = self._columns()
        width = self.width()/columns
        row, column = divmod(index, columns)
        return QRectF(column*width+1, row*self.ROW_HEIGHT+1, width-6, self.ROW_HEIGHT-6)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_height()

    def mouseMoveEvent(self, event):
        position = event.position()
        columns = self._columns()
        row, column = int(position.y()//self.ROW_HEIGHT), int(position.x()//(self.width()/columns))
        index = row*columns+column
        if 0 <= column < columns and 0 <= index < len(self.players) and self._card_rect(index).contains(position):
            self.setToolTip(self._description(self.players[index]))
        else:
            self.setToolTip('')
        super().mouseMoveEvent(event)

    def paintEvent(self,event):
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            if not self.players:
                painter.setPen(QColor(tone.MUTED))
                painter.drawText(self.rect(),Qt.AlignmentFlag.AlignCenter,'选择比赛查看阵容')
                return
            title = QFont(self.font())
            title.setPixelSize(12 if self.width()/self._columns() < 160 else 13)
            title.setBold(True)
            player_name = QFont(self.font())
            player_name.setPixelSize(11)
            for index, player in enumerate(self.players):
                box = self._card_rect(index)
                if event.rect().intersects(box.adjusted(0,0,2,3).toAlignedRect()):
                    self._paint_card(painter, box, player, title, player_name)

    def _paint_card(self, painter, box, player, title_font, player_font):
        from .combat_layers import class_icon

        kind = player.get('ship_class')
        accent = QColor(CLASS_COLORS.get(kind, '#849BAE')).darker(130)
        cut = 10
        face = panel_path(box, cut)
        painter.fillPath(panel_path(box.translated(1.5, 2.5), cut), QColor(35,65,97,37))
        surface = QLinearGradient(box.topLeft(), box.bottomRight())
        surface.setColorAt(0, QColor('#FBFEFF'))
        surface.setColorAt(.52, QColor('#F2F8FE'))
        surface.setColorAt(1, QColor('#E8EFF9'))
        painter.fillPath(face, surface)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor('#ACC6DD'), .9))
        painter.drawPath(face)

        painter.save()
        painter.setClipPath(face)
        painter.fillRect(QRectF(box.left()+1,box.top()+8,2.5,box.height()-19), accent)
        painter.setPen(QPen(QColor('#D6E4F1'), .8))
        painter.drawLine(QPointF(box.left()+11,box.bottom()-4),QPointF(box.right()-9,box.bottom()-4))
        painter.setPen(QPen(QColor(240,87,166,140), 1.2))
        painter.drawLine(QPointF(box.right()-cut-3,box.top()+1),QPointF(box.right()-2,box.top()+cut+2))

        x,y = box.x(),box.y()
        if kind not in self._icons:
            self._icons[kind] = class_icon(kind, accent.name(), heading=0.)
        self._icons[kind].paint(painter, QRectF(x+7,y+5,24,24).toAlignedRect())

        image = _lineup_art(_ship_key(player.get('ship_id')))
        name_right = box.right()-10
        if image is not None:
            art_width = min(84., max(28., (box.width()-103)*.65))
            art = QRectF(box.right()-art_width-8,y+29,art_width,box.height()-34)
            _draw_art(painter,image,art,.64)
            name_right = art.left()-5
        title = QRectF(x+32,y+5,box.width()-41,24)
        player_name = QRectF(x+11,y+28,name_right-x-11,21)
        name = re.sub(r'_KOTS\w*$','',player['player_name'],flags=re.IGNORECASE)
        self._paint_label(painter, title, player['ship_name'], title_font, tone.INK)
        self._paint_label(painter, player_name, name, player_font, '#365675')
        painter.restore()

    @staticmethod
    def _paint_label(painter, rect, text, font, color):
        painter.setFont(font)
        painter.setPen(QColor(color))
        label = painter.fontMetrics().elidedText(text,Qt.TextElideMode.ElideRight,max(0,int(rect.width())))
        painter.drawText(rect,Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,label)


def chart_scroll(chart):
    scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff);scroll.setWidget(chart);return scroll


def section(title,subtitle=''):
    widget=QWidget();layout=QVBoxLayout(widget);layout.setContentsMargins(12,12,12,12);layout.setSpacing(8)
    heading=QLabel(title);font=heading.font();font.setPointSizeF(12);font.setBold(True);heading.setFont(font);layout.addWidget(heading)
    if subtitle:
        description=QLabel(subtitle);description.setObjectName('subtitle');description.setWordWrap(True);layout.addWidget(description)
    return widget,layout
