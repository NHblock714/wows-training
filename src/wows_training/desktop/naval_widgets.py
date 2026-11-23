"""Native artwork and restrained operations-room identity widgets."""
from functools import lru_cache
from base64 import b64decode
from PySide6.QtCore import QPointF,QRectF,QSize,Qt
from PySide6.QtGui import QColor,QFont,QFontMetricsF,QIcon,QImage,QPainter,QPen,QPainterPath,QPixmap
from PySide6.QtWidgets import QWidget,QSizePolicy
from .naval_assets import default_naval_assets
from . import academy_palette as colors
from .chart_motion import ChartReveal
from .layered_panel import paint_layered_panel, panel_path
from .presentation_fonts import load_presentation_fonts


@lru_cache(maxsize=1)
def english_ship_titles():
    from ..career_service import load_references
    try:
        encyclopedia = (load_references().get('legacy') or {}).get('encyclopedia') or {}
        return {key: row['name'] for key, row in encyclopedia.items()
                if isinstance(row.get('name'), str) and row['name'].isascii()}
    except (OSError, ValueError):
        return {}


def _visible_image_rect(image, padding=1):
    if image.hasAlphaChannel():
        rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
        raw = rgba.constBits()
        width, height, stride = rgba.width(), rgba.height(), rgba.bytesPerLine()
        left, right, top, bottom = width, 0, height, 0
        for y in range(height):
            alpha = bytes(raw[y*stride+3:y*stride+width*4:4])
            start = width - len(alpha.lstrip(b'\0'))
            if start < width:
                left = min(left, start)
                right = max(right, len(alpha.rstrip(b'\0')))
                top = min(top, y)
                bottom = y + 1
        if right > left and bottom > top:
            left, right = max(0, left-padding), min(width, right+padding)
            top, bottom = max(0, top-padding), min(height, bottom+padding)
            return QRectF(left, top, right-left, bottom-top)
    return QRectF(image.rect())


class ShipArtwork(QWidget):
    def __init__(self,parent=None,assets=None):
        super().__init__(parent);self.assets=assets or default_naval_assets();self.ship=None;self.info={};self.title='';self.subtitle='';self.species=None;self.nation=None;self._source_rects={}
        self.setObjectName('shipArtwork');self.setMinimumSize(200,180);self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Preferred)
        self._reveal=ChartReveal(self,300)

    def sizeHint(self):return QSize(440,260)

    def set_ship(self,ship_id_or_index,title=None,subtitle=None,*,species=None,nation=None):
        self.ship=ship_id_or_index;self.info=self.assets.ship_info(ship_id_or_index)
        self.title=self.info.get('name','舰船资料') if title is None else str(title)
        self.subtitle=self.info.get('index','') if subtitle is None else str(subtitle);self.species=species or self.info.get('species');self.nation=nation or self.info.get('nation')
        identifier = str(self.info.get('ship_id') or self.ship)
        self.display_title = english_ship_titles().get(identifier, self.title) if identifier.isdecimal() else self.title
        self.setToolTip(self.title+' · '+self.display_title if self.display_title!=self.title else self.title)
        self._reveal.set_content(self.ship);self.update()

    def _source_rect(self,image):
        key=image.cacheKey()
        if key not in self._source_rects:
            self._source_rects[key]=_visible_image_rect(image)
        return self._source_rects[key]

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing);painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        box = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        paint_layered_panel(painter, box, accent=colors.PINK, cut=23)
        painter.setClipPath(panel_path(box.adjusted(1, 1, -6, -6), 23))

        painter.setPen(QPen(QColor(18, 99, 211, 31), .8))
        for y in range(24, self.height()-43, 24):
            painter.drawLine(1, y, self.width()-1, y)
        for x in range(24, self.width(), 24):
            painter.drawLine(x, 1, x, self.height()-44)
        # A translucent sighting circle frames the native cutout without covering labels.
        cx, cy = box.center().x(), (box.height()-45)/2
        radius = max(22., min(box.width()*.23, cy*.7))
        painter.setPen(QPen(QColor(18, 99, 211, 57), .9))
        painter.drawEllipse(QPointF(cx, cy), radius, radius)
        for a in range(0,360,45):
            painter.drawArc(QRectF(cx-radius-5, cy-radius-5, 2*radius+10, 2*radius+10), a*16, 7*16)

        painter.setPen(QPen(QColor(colors.LINE), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(panel_path(box.adjusted(1, 1, -6, -6), 23))
        for x in range(18, self.width()-15, 8):
            painter.setPen(QPen(QColor(18, 99, 211, 90 if x%32 == 18 else 42), 1))
            painter.drawLine(x, self.height()-46, x, self.height()-(52 if x%32 == 18 else 49))
        painter.fillRect(QRectF(0, self.height()-43, self.width(), 43), QColor(colors.NIGHT))
        painter.fillRect(QRectF(0, self.height()-43, 64, 2), QColor(colors.CYAN))
        painter.fillRect(QRectF(65, self.height()-43, 8, 2), QColor(colors.PINK))
        image=None;flag=None
        try:
            image=self.assets.ship_image(self.ship);flag=self.assets.flag(self.nation) if self.nation else None
        except (OSError,ValueError):pass
        available=box.adjusted(22,46,-22,-54)
        if image is not None:
            source=self._source_rect(image);scale=min(available.width()/source.width(),available.height()/source.height(),1.5)
            rect=QRectF(available.center().x()-source.width()*scale/2,available.center().y()-source.height()*scale/2,source.width()*scale,source.height()*scale)
            painter.save();painter.setOpacity(.6+.4*self._reveal.progress)
            rect.translate((1.-self._reveal.progress)*8,0)
            painter.drawImage(rect,image,source);painter.restore()
        else:
            try:
                from .combat_layers import class_icon
                icon=class_icon(self.species,colors.BLUE);icon.paint(painter,int(available.center().x()-24),int(available.center().y()-35),48,48)
            except (OSError,ValueError):pass
            painter.setPen(QColor(colors.MUTED));painter.drawText(available.adjusted(0,45,0,0),Qt.AlignmentFlag.AlignCenter,'暂无舰图')
        if flag is not None:
            available_width = min(34., self.width()*.15)
            rect = QRectF(20,17,available_width,available_width*flag.height()/flag.width())
            painter.save()
            painter.setOpacity(.72)
            painter.drawImage(rect, flag)
            painter.restore()
        fonts=load_presentation_fonts()
        title = getattr(self,'display_title',self.title)
        font = QFont(fonts['display'] if title.isascii() else fonts['chinese_tech'])
        font.setPixelSize(24)
        font.setWeight(QFont.Weight.DemiBold)
        safe_width = max(1,self.width()-44)
        while font.pixelSize()>14 and QFontMetricsF(font).horizontalAdvance(title)>safe_width:
            font.setPixelSize(font.pixelSize()-1)
        painter.setFont(font)
        painter.setPen(QColor(colors.PAPER))
        label = painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, safe_width)
        title_rect = QRectF(22, self.height()-41, safe_width, 24 if self.subtitle else 32)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
        if self.subtitle:
            font.setWeight(QFont.Weight.Normal)
            font.setPixelSize(12)
            painter.setFont(font)
            painter.setPen(QColor('#B6CEE6'))
            caption = painter.fontMetrics().elidedText(self.subtitle, Qt.TextElideMode.ElideRight, max(1,self.width()-32))
            painter.drawText(QRectF(15,self.height()-18,self.width()-30,16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, caption)
        painter.end()


# The user-drawn 32 x 32 PNG is embedded verbatim for standalone execution.
# Its colours, alpha and pixel placement are never reconstructed or recoloured.
_USER_EMBLEM_PNG = b"iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAACXElEQVR4AeyVv0scQRTH5yUSBUmiQdKoGExsQsqkS5F/wCYpQrqAIqigaKMIIjYiNqLFCaJoJxZaWYi2goWtKCj+vFM5T11//0TW+47sua47M293CxuX+97NvH3v+z47M8e+Es98vQBoV2Dte8x2X18uNm3djnnzCw6XtPnw0gIgwa3lnGKhgkDzT3PV7nTWOBAAHP0gwjaHX2AAFAHCvTVhnhw+kBIgylPBGEq9LxOmc6AEyC79AI/IysvO0XooAbxVZZdxAU2fHnhvyfn6jz4B7c+uyDn3iw3gGNZknTlD39+TuinfuCoYGEBlFDbOBsDJd+Q0IyJnKPBPcJQJMgZKgKyCXG05EYn0X1EQPUD4FZTfvPYLZ2JKgEyGz4DovjlucSCQp1IoADR1G3rn7numsRLgY6ycUrUTpnrt/aajhOh+V6jdIyUAnKNAoHlXXrG2OXpoAZAACApxcZrD3wiAJJU2+r8Z3/eqWiceCcAxifIbCSC+Y0XpLWuNAIsDvx4tM5YdmmkvlPH+yny7488bu/PvWzmXrumv8Zavj+bpkO/HCOCtKqmaJ+hn2xYtxM9F1YBFLWPX1Dx6YjzxXi/MjQCLq7vIC6wra4NVYwTQGe1Z+lczh8AIABPufiIXQn7qOh9Do4wA//rOCKsw2Vr65FBh770dRhtL7ETyWNQNJlhnwgiABoCw9pJipDrXxtMh5hUa91YU2cmjW3ZzeLAAkAgICKsBCGi4/rM9WFMktW3dyMbcJ4cnxAZAMgSI3x0LBP3vWaGKWEKqYWibteTwcCswgLuYMzbl3AEAAP//WTY/CgAAAAZJREFUAwC0zf1BHcxZRgAAAABJRU5ErkJggg=="


@lru_cache(maxsize=3)
def _pixel_fleet_master(size=32):
    if size not in (16,24,32):
        raise ValueError('Pixel master must be 16, 24 or 32 pixels')
    source=QImage.fromData(b64decode(_USER_EMBLEM_PNG),'PNG')
    if size==32:
        return source
    return source.scaled(size,size,Qt.AspectRatioMode.IgnoreAspectRatio,Qt.TransformationMode.FastTransformation)


def _pixel_emblem_rect(bounds, dpr, offset=QPointF()):
    # The physical device grid, rather than arbitrary logical widget dimensions,
    # decides the magnification. Every source cell therefore stays a square.
    factor=max(1,int(min(bounds.width(),bounds.height())*dpr//32))
    physical=32*factor
    left=round(bounds.center().x()*dpr+offset.x()-physical/2)
    top=round(bounds.center().y()*dpr+offset.y()-physical/2)
    return QRectF((left-offset.x())/dpr,(top-offset.y())/dpr,physical/dpr,physical/dpr)


def _draw_cat_emblem(painter, bounds):
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing,False)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,False)
    # QWidget.render may redirect painting to an image at a different DPR from
    # the widget's screen; deviceTransform is the actual physical paint grid.
    transform=painter.deviceTransform()
    dpr=abs(transform.m11())
    target=_pixel_emblem_rect(bounds,dpr,QPointF(transform.dx(),transform.dy()))
    physical=round(target.width()*dpr)
    image=_pixel_fleet_master(32).scaled(physical,physical,Qt.AspectRatioMode.IgnoreAspectRatio,Qt.TransformationMode.FastTransformation)
    image.setDevicePixelRatio(dpr)
    painter.drawImage(target.topLeft(),image)
    painter.restore()


def _fleet_icon_image(size):
    if size in (16,24,32):
        return _pixel_fleet_master(size)
    if size not in (48,64,128,256):
        raise ValueError('Unsupported fleet icon size')
    master=_pixel_fleet_master(32)
    if size==48:
        # 48/32 is fractional: keep the original cells intact on a clear canvas.
        image=QImage(48,48,QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter=QPainter(image)
        painter.drawImage(QPointF(8,8),master)
        painter.end()
        return image
    return master.scaled(size,size,Qt.AspectRatioMode.IgnoreAspectRatio,Qt.TransformationMode.FastTransformation)


@lru_cache(maxsize=1)
def fleet_window_icon():
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(QPixmap.fromImage(_fleet_icon_image(size)))
    return icon


class OperationsBrand(QWidget):
    _MARK_LEFT = 58
    _MARK_LIFT = 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(84)
        self.setObjectName('operationsBrand')
        self.setAccessibleName('WOWS 港口开发部')
        self._title_family = load_presentation_fonts()['display']
        self._wows_mark = self._load_mark('icons/wows-wordmark.svg')
        self._harbor_mark = self._load_mark('icons/harbor-wordmark.png')
        self._emblem_bottom = _visible_image_rect(_pixel_fleet_master(32), padding=0).bottom()/32
        self._scaled_marks = {}

    @staticmethod
    def _load_mark(relative):
        try:
            image = default_naval_assets().image(relative)
        except (OSError, ValueError):
            return None
        if image is None:
            return None
        return image.copy(_visible_image_rect(image).toRect())

    def sizeHint(self):
        return QSize(166,84)

    def _paint_mark(self, painter, image, box, caption, font, color):
        if image is None:
            while font.pixelSize() > 12 and QFontMetricsF(font).horizontalAdvance(caption) > box.width():
                font.setPixelSize(font.pixelSize()-1)
            painter.setFont(font)
            painter.setPen(QColor(color))
            painter.drawText(box, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, caption)
            return

        transform = painter.deviceTransform()
        dpr = abs(transform.m11())
        fit = QSize(max(1, int(box.width()*dpr)), max(1, int(box.height()*dpr)))
        key = (fit.width(), fit.height(), dpr)
        cache_key = image.cacheKey()
        cached = self._scaled_marks.get(cache_key)
        if cached is None or cached[0] != key:
            # Filter at the final physical resolution before painting the logo.
            raster = image.scaled(fit, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            raster = raster.copy(_visible_image_rect(raster, padding=0).toRect())
            raster.setDevicePixelRatio(dpr)
            self._scaled_marks[cache_key] = (key, raster)
        else:
            raster = cached[1]
        x = (round(box.left()*dpr + transform.dx())-transform.dx())/dpr
        top = box.bottom() - raster.deviceIndependentSize().height()
        y = (round(top*dpr + transform.dy())-transform.dy())/dpr
        painter.drawImage(QPointF(x, y), raster)

    def _paint_wordmarks(self, painter, emblem_bounds):
        transform = painter.deviceTransform()
        emblem = _pixel_emblem_rect(emblem_bounds, abs(transform.m11()), QPointF(transform.dx(), transform.dy()))
        bottom = emblem.top() + emblem.height() * self._emblem_bottom - self._MARK_LIFT
        left = self._MARK_LEFT
        width = max(1, self.width()-left-5)
        gap = 2
        harbor_height = min(42, width*self._harbor_mark.height()/self._harbor_mark.width()) if self._harbor_mark else 42
        wows_height = min(33, width*self._wows_mark.height()/self._wows_mark.width()) if self._wows_mark else 33
        scale = min(1, (bottom-gap)/(harbor_height+wows_height))
        harbor_height *= scale
        wows_height *= scale
        wows_top = bottom-wows_height
        harbor_top = wows_top-gap-harbor_height

        head = QFont(self._title_family)
        head.setWeight(QFont.Weight.Bold)
        head.setPixelSize(27)
        subtitle = QFont('Microsoft YaHei UI')
        subtitle.setPixelSize(14)
        subtitle.setWeight(QFont.Weight.DemiBold)
        self._paint_mark(painter, self._harbor_mark, QRectF(left, harbor_top, width, harbor_height), '港口开发部', subtitle, '#CAE2F6')
        self._paint_mark(painter, self._wows_mark, QRectF(left, wows_top, width, wows_height), 'WOWS', head, colors.PAPER)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # A floating, physically pixel-aligned emblem replaces the old frame.
        emblem_bounds = QRectF(0, 0, 64, 64)
        _draw_cat_emblem(painter, emblem_bounds)
        self._paint_wordmarks(painter, emblem_bounds)
        painter.fillRect(QRectF(2,78,self.width()-4,1), QColor('#34465C'))
        painter.fillRect(QRectF(2,77,36,2), QColor(colors.CYAN))
        painter.fillRect(QRectF(41,77,8,2), QColor(colors.PINK))
        painter.end()
