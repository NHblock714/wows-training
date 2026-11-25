"""Fleet navigation, section banners and printed map-paper background."""
from functools import lru_cache
import math

from PySide6.QtCore import QElapsedTimer,QEasingCurve,QEvent,QPointF,QRectF,QSize,Qt,QTimer,QVariantAnimation,Signal
from PySide6.QtGui import QColor,QFont,QIcon,QImage,QLinearGradient,QPainter,QPainterPath,QPen,QPixmap,QPolygonF,QRegion
from PySide6.QtWidgets import QWidget,QSizePolicy,QLabel

from .naval_assets import default_naval_assets
from .presentation_fonts import load_presentation_fonts
from . import academy_palette as colors
from .native_ui_assets import default_native_ui_assets
from .motion import motion_enabled,subscribe_motion
from .layered_panel import panel_path


def _cover_source(image,target,center_y=.5):
    scale=max(target.width()/image.width(),target.height()/image.height())
    width,height=target.width()/scale,target.height()/scale
    top=max(0.,min(image.height()-height,image.height()*center_y-height/2))
    return QRectF((image.width()-width)/2,top,width,height)


class FleetBackdrop(QWidget):
    """Printed chart paper with slow signal pulses in the outer page margins."""
    parallaxChanged = Signal(QPointF)

    def __init__(self,parent=None):
        super().__init__(parent);self.setObjectName('fleetBackdrop')
        self._scene=None
        self._harbor_wash = QImage()
        try:self._scene=default_naval_assets().background('fleet')
        except (OSError,ValueError):pass
        self._scene_enabled = True
        self._routes = []
        self._motion_subscribed = False
        self._observed_window = None
        self._input_surface = None
        self._parallax = QPointF()
        self._parallax_target = QPointF()
        self._parallax_motion = QVariantAnimation(self)
        self._parallax_motion.setDuration(420)
        self._parallax_motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._parallax_motion.valueChanged.connect(self._set_parallax)
        self._elapsed = QElapsedTimer()
        self._scene_time = 0.
        self._scene_timer = QTimer(self)
        self._scene_timer.setInterval(42)
        self._scene_timer.timeout.connect(self._advance_signals)
        self._signals = []
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMouseTracking(True)

    def set_scene_enabled(self, enabled):
        """Suspend the ambient layer while a timeline or tactical map owns the stage."""
        changed = bool(enabled) != self._scene_enabled
        self._scene_enabled = bool(enabled)
        self._sync_scene()
        if changed:
            self.update()

    def _sync_scene(self, *_):
        allowed = (self._scene_enabled and self.isVisible() and
                   not self.window().isMinimized() and motion_enabled(self))
        if allowed:
            if not self._scene_timer.isActive():
                self._elapsed.start()
                self._scene_timer.start()
        else:
            self._scene_timer.stop()
            self._parallax_motion.stop()
            self._scene_time = 0.
            self._set_signals(self._sample_signals(0.) if self._scene_enabled else [])
            self._parallax_target = QPointF()
            self._set_parallax(QPointF())

    @property
    def parallax(self):
        return QPointF(self._parallax)

    def _dot_patch(self):
        return QRectF(self.width()-156, 0, 134, 23)

    def _set_parallax(self, offset):
        if self._parallax != offset:
            self._parallax = offset
            self.update(self._dot_patch().toAlignedRect())
            self.update(QRectF(self.width()-260, self.height()-204, 260, 204).toAlignedRect())
            self.parallaxChanged.emit(offset)

    def _track_pointer(self, position):
        if not self._scene_timer.isActive():
            return
        width = max(1, self.width()-190)
        x = max(-1., min(1., 2*(position.x()-190)/width-1))
        y = max(-1., min(1., 2*position.y()/max(1,self.height())-1))
        target = QPointF(7.*x, 4.5*y)
        if target == self._parallax_target:
            return
        self._parallax_target = target
        self._parallax_motion.stop()
        self._parallax_motion.setStartValue(self._parallax)
        self._parallax_motion.setEndValue(target)
        self._parallax_motion.start()

    def mouseMoveEvent(self,event):
        self._track_pointer(event.position())
        super().mouseMoveEvent(event)

    def _set_signals(self, signals):
        dirty = QRegion()
        for _, points in self._signals + signals:
            # Only a short tail and its node change on a tick.
            bounds = QRectF(points[0], points[-1]).normalized()
            for point in points[1:-1]:
                bounds = bounds.united(QRectF(point.x()-.5, point.y()-.5, 1., 1.))
            dirty |= QRegion(bounds.adjusted(-5,-5,5,5).toAlignedRect())
        self._signals = signals
        if not dirty.isEmpty():
            self.update(dirty)

    def _sample_signals(self, seconds):
        signals = []
        for path, length, offset, speed, color in self._routes:
            phase = (seconds*speed + offset*length) % (length+95)
            if phase < 1 or phase > length+18:
                continue
            distances = (max(0, min(length, phase-back)) for back in (18, 13, 8, 4, 0))
            signals.append((color, [path.pointAtPercent(path.percentAtLength(d)) for d in distances]))
        return signals

    def _advance_signals(self):
        self._scene_time = self._elapsed.elapsed()/1000
        self._set_signals(self._sample_signals(self._scene_time))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width, height = self.width(), self.height()
        if self._scene is not None:
            self._harbor_wash = self._faded_harbor(max(1,width-190),min(310,height))
        x0 = 198
        space = max(1, width-x0)
        # All routes stay inside the page trim so dense charts and text remain clear.
        definitions = (
            ([(x0+space*.08,8),(x0+space*.28,8),(x0+space*.28+6,14),(x0+space*.58,14)],
             .20, 38., colors.BLUE),
            ([(width-8,height*.12),(width-8,height*.30),(width-15,height*.30+7),
              (width-15,height*.53),(width-8,height*.53+7),(width-8,height*.83)],
             .53, 32., colors.PINK),
            ([(x0+space*.46,height-7),(x0+space*.30,height-7),(x0+space*.30-5,height-12),
              (x0+space*.04,height-12)], .19, 34., colors.BLUE),
            ([(width-8,height-107),(width-8,height-48),(width-37,height-19),
              (width-125,height-19),(width-132,height-12),(width-191,height-12)],
             .07, 24., '#188CA7'),
        )
        self._routes = []
        for vertices,offset,speed,color in definitions:
            path = QPainterPath(QPointF(*vertices[0]))
            for pt in vertices[1:]:
                path.lineTo(QPointF(*pt))
            self._routes.append((path, path.length(), offset, speed, color))
        self._signals = self._sample_signals(self._scene_time) if self._scene_enabled else []

    def _faded_harbor(self, width, height):
        image = QImage(width,height,QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        if image.isNull():
            return image
        p = QPainter(image)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(image.rect())
        p.drawImage(rect,self._scene,_cover_source(self._scene,rect))
        fade = QLinearGradient(0,0,0,height)
        fade.setColorAt(0,QColor(0,0,0,22))
        fade.setColorAt(.53,QColor(0,0,0,17))
        fade.setColorAt(1,QColor(0,0,0,0))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        p.fillRect(rect,fade)
        p.end()
        return image

    def showEvent(self,event):
        super().showEvent(event)
        if not self._motion_subscribed:
            self._motion_subscribed = subscribe_motion(self, self._sync_scene)
        window = self.window()
        if self._observed_window is not window:
            if self._observed_window is not None:
                self._observed_window.removeEventFilter(self)
            self._observed_window = window
            window.installEventFilter(self)
        surface = window.windowHandle()
        if surface is not self._input_surface:
            if self._input_surface is not None:
                self._input_surface.removeEventFilter(self)
            self._input_surface = surface
            if surface is not None:
                surface.installEventFilter(self)
        self._sync_scene()

    def hideEvent(self,event):
        self._scene_timer.stop()
        self._parallax_motion.stop()
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if watched is self._observed_window and event.type() == QEvent.Type.WindowStateChange:
            self._sync_scene()
        elif watched is self._input_surface:
            if event.type() == QEvent.Type.MouseMove:
                self._track_pointer(event.position())
            elif event.type() == QEvent.Type.Leave:
                self._parallax_target = QPointF()
                self._parallax_motion.stop()
                if self._scene_timer.isActive():
                    self._parallax_motion.setStartValue(self._parallax)
                    self._parallax_motion.setEndValue(QPointF())
                    self._parallax_motion.start()
        return super().eventFilter(watched, event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        bounds = QRectF(self.rect())
        paper = QLinearGradient(bounds.topLeft(), bounds.bottomRight())
        paper.setColorAt(0, QColor(colors.PAPER))
        paper.setColorAt(.42, QColor(colors.CANVAS))
        paper.setColorAt(1, QColor(colors.ICE))
        painter.fillRect(bounds, paper)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Broad translucent colour planes remain legible behind transparent work areas.
        pink = QPainterPath()
        pink.moveTo(190+self.width()*.13, self.height())
        pink.lineTo(190+self.width()*.45, self.height()*.26)
        pink.lineTo(self.width()+80, self.height()*.1)
        pink.lineTo(self.width(), self.height())
        pink.closeSubpath()
        tint = QLinearGradient(260, self.height(), self.width(), 140)
        tint.setColorAt(0, QColor(240,87,166,3))
        tint.setColorAt(.52, QColor(240,87,166,25))
        tint.setColorAt(1, QColor(241,113,185,55))
        painter.fillPath(pink,tint)
        fold = QPainterPath()
        fold.moveTo(self.width()-164,0)
        fold.lineTo(self.width()-65,0)
        fold.lineTo(self.width()-9,158)
        fold.lineTo(self.width()-9,331)
        fold.closeSubpath()
        painter.fillPath(fold,QColor(17,29,49,11))
        painter.setPen(QPen(QColor(240,87,166,42),2))
        painter.drawLine(QPointF(self.width()-162,0),QPointF(self.width()-10,330))

        if not self._harbor_wash.isNull():
            painter.drawImage(QPointF(190,0),self._harbor_wash)
        if self._scene_enabled:
            self._draw_paper_layers(painter,bounds)

        painter.setPen(QPen(QColor(18, 99, 211, 13), .8))
        for x in range(216, self.width(), 48):
            painter.drawLine(x, 0, x, self.height())
        for y in range(18, self.height(), 48):
            painter.drawLine(190, y, self.width(), y)
        # Broken range rings sit outside the work area and remain visible in its margins.
        center = QPointF(self.width()+32, self.height()+64)
        painter.setPen(QPen(QColor(18, 99, 211, 25), 1.0))
        for radius in (126, 154, 210):
            ring = QRectF(center.x()-radius, center.y()-radius, radius*2, radius*2)
            painter.drawArc(ring, 98*16, 93*16)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(54, 117, 186, 50), .9))
        for path,length,_,_,_ in self._routes:
            painter.drawPath(path)
            node = path.pointAtPercent(1)
            painter.drawRect(QRectF(node.x()-1.5,node.y()-1.5,3,3))
        if self._scene_enabled:
            self._draw_registration_marks(painter)
        for ink,points in self._signals:
            color = QColor(ink)
            for i,(p1,p2) in enumerate(zip(points,points[1:])):
                color.setAlpha(30+35*i)
                painter.setPen(QPen(color, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawLine(p1,p2)
            end = points[-1]
            glow = QColor(ink)
            glow.setAlpha(25)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(end,5,5)
            painter.setBrush(QColor(ink))
            painter.drawEllipse(end,1.8,1.8)
        patch = self._dot_patch().adjusted(8,5,-8,-5)
        painter.save()
        painter.translate(self._parallax)
        painter.setPen(Qt.PenStyle.NoPen)
        for col in range(15):
            painter.setBrush(QColor(18,99,211,22+5*col))
            for row in range(2 if col < 8 else 3):
                painter.drawEllipse(QPointF(patch.left()+col*7+row*2,patch.top()+row*4), 1., 1.)
        painter.restore()
        projection_area = QRectF(self.width()-260,self.height()-204,260,204)
        if self._scene_enabled and event.region().intersects(projection_area.toAlignedRect()):
            self._draw_projection(painter,projection_area)
        painter.end()

    def _draw_paper_layers(self,painter,bounds):
        field = bounds.adjusted(190,0,0,0)
        if field.isEmpty():
            return
        painter.save()
        painter.setClipRect(field,Qt.ClipOperation.IntersectClip)
        painter.translate(field.topLeft())
        w,h = field.width(),field.height()

        edge = QPainterPath()
        edge.addPolygon(QPolygonF([QPointF(0,h*.49),QPointF(w*.26,h*.70),
                                   QPointF(w*.12,h),QPointF(0,h)]))
        tint = QLinearGradient(0,h*.50,w*.24,h*.90)
        tint.setColorAt(0,QColor(113,171,225,29))
        tint.setColorAt(1,QColor(183,221,248,8))
        painter.fillPath(edge,tint)
        cap = QPainterPath()
        cap.addPolygon(QPolygonF([QPointF(w*.51,0),QPointF(w*.70,0),
                                  QPointF(w*.83,h*.37),QPointF(w*.72,h*.48)]))
        tint = QLinearGradient(w*.52,0,w*.82,h*.34)
        tint.setColorAt(0,QColor(125,185,236,20))
        tint.setColorAt(1,QColor(154,157,225,0))
        painter.fillPath(cap,tint)

        hatched = QPainterPath()
        hatched.addPolygon(QPolygonF([QPointF(w*.035,h*.83),QPointF(w*.18,h*.95),
                                      QPointF(w*.14,h),QPointF(0,h)]))
        painter.setClipPath(hatched,Qt.ClipOperation.IntersectClip)
        painter.setPen(QPen(QColor(57,126,191,25),.85))
        for x in range(-80,round(w*.18)+11,9):
            painter.drawLine(QPointF(x,h),QPointF(x+h*.17,h*.83))
        painter.restore()

    def _draw_registration_marks(self, painter):
        """Etched alignment marks share the margins with the moving signal trails."""
        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for path, length, _, _, ink in self._routes:
            start = path.pointAtPercent(0)
            colour = QColor(ink)
            colour.setAlpha(105)
            painter.setPen(QPen(colour,1))
            painter.drawEllipse(start,2.5,2.5)
            end = path.pointAtPercent(1)
            prev = path.pointAtPercent(path.percentAtLength(max(0,length-1)))
            tangent = end-prev
            normal = QPointF(-tangent.y(),tangent.x())
            for index in range(4):
                tick = end-tangent*(7+index*4)
                painter.drawLine(tick-normal*1.5,tick+normal*(3 if index==0 else 1.5))
        painter.restore()

    def _draw_projection(self, painter, area):
        """Two parallel chart plates projected onto the lower margin."""
        painter.save()
        painter.setClipRect(area,Qt.ClipOperation.IntersectClip)
        painter.translate(area.bottomRight())
        painter.translate(self._parallax*.35)
        # Parallel basis vectors give each plane and its measuring lines the same perspective.
        origin = QPointF(-242,-48)
        across = QPointF(154,47)
        depth = QPointF(94,-112)
        def project(u,v):
            return origin + across*u + depth*v
        footprint = QPolygonF([project(u,v) for u,v in ((0,.10),(.09,0),(.86,0),(1,.16),(1,1),(.14,1),(0,.84))])
        painter.setPen(QPen(QColor(27,85,131,51),1))
        painter.setBrush(QColor(65,133,186,10))
        painter.drawPolygon(footprint)
        lift = QPointF(0,-21)+self._parallax*.6
        for u,v in ((0,.10),(.86,0),(1,.16),(.14,1)):
            node = project(u,v)
            painter.drawLine(node,node+lift)
        painter.translate(lift)
        wash = QLinearGradient(project(.05,.95),project(.95,.05))
        wash.setColorAt(0,QColor(126,211,238,32))
        wash.setColorAt(.58,QColor(253,253,255,79))
        wash.setColorAt(1,QColor(237,152,205,41))
        painter.setBrush(wash)
        painter.setPen(QPen(QColor(71,126,170,66),1.2))
        painter.drawPolygon(footprint)
        painter.setPen(QPen(QColor(57,128,166,34),.8))
        for index in range(1,6):
            step = index/7
            painter.drawLine(project(.14,step),project(.88,step))
            painter.drawLine(project(step,.14),project(step,.84))
        painter.setPen(QPen(QColor(24,135,176,100),2))
        painter.drawLine(project(.2,1),project(.46,1))
        painter.setPen(QPen(QColor(212,74,148,93),2))
        painter.drawLine(project(.86,0),project(1,.16))
        # The two planes separate slightly as the pointer traverses the canvas.
        reflection = QLinearGradient(project(.04,.24),project(.78,.61))
        reflection.setColorAt(0,QColor(255,255,255,70))
        reflection.setColorAt(.58,QColor(255,255,255,26))
        reflection.setColorAt(1,QColor(255,255,255,0))
        reflected = QPainterPath()
        reflected.addPolygon(QPolygonF([project(.08,.13),project(.49,.13),project(.88,.71),project(.52,.75)]))
        painter.fillPath(reflected,reflection)
        painter.restore()


class FleetPageHeader(QWidget):
    def __init__(self,title,icon_key='warehouse',parent=None):
        super().__init__(parent);self.setObjectName('fleetPageHeader');self.title=str(title)
        self.setProperty('backgroundEffect', True)
        self._background_enabled = True
        self.setFixedHeight(66);self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        self._image=self._icon=None
        self._bar=default_native_ui_assets().image('header')
        try:
            assets = default_naval_assets()
            self._image = assets.background('fleet')
            original = assets.icon(icon_key)
            if original is not None:
                self._icon = original.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
                paint = QPainter(self._icon)
                paint.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
                paint.fillRect(self._icon.rect(), QColor(colors.PAPER))
                paint.end()
        except (OSError, ValueError):
            pass
        self.setAccessibleName(self.title)
        self._shine=-1.;self._motion_bound=False;self._motion_window=None
        self._backdrop=None
        self._scene_offset=QPointF()
        self._entrance=QVariantAnimation(self)
        self._entrance.setDuration(560);self._entrance.setStartValue(0.);self._entrance.setEndValue(1.)
        self._entrance.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._entrance.valueChanged.connect(self._shine_frame)
        self._entrance.finished.connect(self._finish_motion)

    def sizeHint(self):return QSize(620,66)

    def _shine_frame(self,value):
        self._shine = float(value)
        self.update(0, self.height()-39, self.width(), 27)

    def set_background_enabled(self, enabled):
        self._background_enabled = bool(enabled)
        if not enabled:
            self._finish_motion()
            self._scene_offset = QPointF()
        self.update()

    def _finish_motion(self):
        self._entrance.stop();self._shine=-1.;self.update()

    def _motion_preference(self,enabled):
        if not enabled:self._finish_motion()

    def showEvent(self,event):
        super().showEvent(event)
        if not self._motion_bound:self._motion_bound=subscribe_motion(self,self._motion_preference)
        window=self.window()
        if self._motion_window is not window:
            if self._motion_window is not None:self._motion_window.removeEventFilter(self)
            self._motion_window=window;window.installEventFilter(self)
        backdrop = window.findChild(FleetBackdrop)
        if self._backdrop is not backdrop:
            if self._backdrop is not None:
                self._backdrop.parallaxChanged.disconnect(self._shift_scene)
            self._backdrop = backdrop
            if backdrop is not None:
                backdrop.parallaxChanged.connect(self._shift_scene)
        if backdrop is not None and self._background_enabled:
            self._scene_offset = backdrop.parallax
        if self._background_enabled and motion_enabled(self) and not window.isMinimized():
            self._entrance.stop();self._shine=0.;self._entrance.start()

    def hideEvent(self,event):
        self._finish_motion();super().hideEvent(event)

    def _shift_scene(self, offset):
        if not self._background_enabled:
            return
        self._scene_offset = offset
        if self.isVisible() and self.width() >= 420:
            width = min(388, int(self.width()*.35))
            self.update(QRectF(self.width()-width, 0, width, self.height()).toAlignedRect())

    def eventFilter(self,watched,event):
        if watched is self._motion_window and event.type() in (QEvent.Type.Hide,QEvent.Type.Close,QEvent.Type.WindowStateChange):
            if event.type()!=QEvent.Type.WindowStateChange or watched.isMinimized():self._finish_motion()
        return super().eventFilter(watched,event)

    def paintEvent(self, event):
        width = self.width()
        height = self.height()
        if width <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        box = QRectF(self.rect())
        face = panel_path(box.adjusted(0,0,-5,-5),17)
        painter.fillPath(panel_path(box.adjusted(4,4,-1,-1),17),QColor(17,29,49,75))
        painter.fillPath(panel_path(box.adjusted(2,2,-3,-3),17),QColor(colors.PINK))
        paper = QLinearGradient(0,0,0,height)
        paper.setColorAt(0,QColor(colors.PAPER));paper.setColorAt(1,QColor('#EEF4FB'))
        painter.fillPath(face,paper)
        painter.setPen(QPen(QColor('#A9BFD6'),1));painter.drawPath(face)
        painter.setClipPath(face)

        # Slanted scene inset: use the native harbor art at its original aspect ratio.
        if self._background_enabled and width >= 420:
            scene_width = min(388, int(width*.35))
            start = width - scene_width
            inset = QPainterPath()
            inset.moveTo(start+43, 1)
            inset.lineTo(width-1, 1)
            inset.lineTo(width-1, height-1)
            inset.lineTo(start, height-1)
            inset.closeSubpath()
            painter.save()
            painter.setClipPath(inset, Qt.ClipOperation.IntersectClip)
            target = QRectF(start, 0, scene_width, height)
            painter.fillRect(target, QColor(colors.PANEL))
            if self._image is not None:
                source_width = self._image.width()*.50
                source_height = source_width * target.height() / target.width()
                # Keep six logical pixels of overscan available on each side of the crop.
                ratio = source_width / target.width()
                source = QRectF(self._image.width()-source_width-(6+self._scene_offset.x())*ratio,
                                self._image.height()*.335-source_height/2-self._scene_offset.y()*ratio,
                                source_width, source_height)
                painter.drawImage(target, self._image, source)
            shade = QLinearGradient(start, 0, width, 0)
            shade.setColorAt(0, QColor(18, 99, 211, 130))
            shade.setColorAt(.55, QColor(18, 65, 119, 63))
            shade.setColorAt(1, QColor(17, 29, 49, 36))
            painter.fillRect(target, shade)
            # Small halftone patch reads as printed color at the left edge of the image.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 75))
            for y in range(7, height, 7):
                for x in range(start+4, start+58, 7):
                    painter.drawEllipse(QPointF(x, y), .8, .8)
            # An engraved scale sits on the photographic edge, clear of the ship.
            baseline = height - 11
            end = width - 17
            rule = QLinearGradient(end-92, 0, end, 0)
            rule.setColorAt(0, QColor(211, 241, 255, 0))
            rule.setColorAt(1, QColor(223, 248, 255, 168))
            painter.setPen(QPen(rule, 1))
            painter.drawLine(QPointF(end-92, baseline), QPointF(end, baseline))
            for index in range(11):
                x = end - 6*index
                length = 5 if index % 5 == 0 else 2
                painter.drawLine(QPointF(x, baseline-length), QPointF(x, baseline))
            painter.restore()
            painter.setPen(QPen(QColor(colors.CYAN), 3))
            painter.drawLine(QPointF(start+40, 1), QPointF(start-2, height-1))
            painter.setPen(QPen(QColor(colors.PINK), 2))
            painter.drawLine(QPointF(start+13, height-24), QPointF(start-1, height-2))
            title_end = start-7
        else:
            title_end = width-10

        # The blue key retains the original harbor-strip texture and the page's game icon.
        key = QPainterPath()
        key.moveTo(0, 0)
        key.lineTo(73, 0)
        key.lineTo(52, height)
        key.lineTo(0, height)
        key.closeSubpath()
        painter.fillPath(key, QColor(colors.BLUE))
        if self._bar is not None:
            painter.save()
            painter.setClipPath(key, Qt.ClipOperation.IntersectClip)
            painter.setOpacity(.16)
            painter.drawImage(QRectF(0, 0, 74, height), self._bar,
                              _cover_source(self._bar, QRectF(0, 0, 74, height)))
            painter.restore()
        painter.fillRect(QRectF(0, height-4, 18, 4), QColor(colors.CYAN))
        if self._icon is not None:
            scale = min(29/self._icon.width(), 29/self._icon.height())
            w,h = self._icon.width()*scale,self._icon.height()*scale
            painter.drawImage(QRectF(30-w/2, height/2-h/2, w, h), self._icon)
        else:
            painter.setPen(QPen(QColor(colors.PAPER), 2))
            painter.drawEllipse(QPointF(30, height/2), 12, 12)
            painter.drawLine(24, height//2, 36, height//2)
            painter.drawLine(30, height//2-6, 30, height//2+6)

        title_left = 85
        face = QFont(load_presentation_fonts()['chinese_tech'])
        face.setPixelSize(26)
        face.setWeight(QFont.Weight.Bold)
        painter.setFont(face)
        painter.setPen(QColor(colors.INK))
        name = painter.fontMetrics().elidedText(self.title, Qt.TextElideMode.ElideRight,
                                                max(0, title_end-title_left))
        painter.drawText(QRectF(title_left, 1, max(0, title_end-title_left), height-3),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)
        # A small routed trace bridges the white plate and the photograph.
        trace_left = max(title_left+painter.fontMetrics().horizontalAdvance(name)+30, title_end-180)
        if self._background_enabled and width >= 420 and title_end-trace_left >= 54:
            painter.setPen(QPen(QColor(colors.LINE), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            trace = QPainterPath(QPointF(trace_left, height-18))
            trace.lineTo(title_end-30, height-18)
            trace.lineTo(title_end-12, height-36)
            trace.lineTo(title_end+15, height-36)
            painter.drawPath(trace)
            painter.setPen(QPen(QColor('#688DAB'), 1))
            painter.drawEllipse(QPointF(trace_left,height-18),2.6,2.6)
            painter.fillRect(QRectF(trace_left-1,height-19,2,2), QColor(colors.BLUE))
            if 0 < self._shine < 1:
                # Light travels along the actual trace, including the diagonal join.
                head = trace_left + (title_end+15-trace_left)*self._shine
                strength = round(220*math.sin(self._shine*math.pi))
                pulse = QLinearGradient(head-34, 0, head+3, 0)
                pulse.setColorAt(0, QColor(21, 120, 183, 0))
                pulse.setColorAt(.90, QColor(16, 132, 183, strength))
                pulse.setColorAt(1, QColor(16, 132, 183, 0))
                painter.setPen(QPen(pulse, 1.8))
                painter.drawPath(trace)
        painter.end()


def install_page_header(page,title,icon_key='warehouse'):
    """Replace a page title in place, keeping adjacent actions in their layout."""
    def replace(layout):
        if layout is None:return None
        for index in range(layout.count()):
            item=layout.itemAt(index);widget=item.widget()
            if isinstance(widget,QLabel) and widget.objectName()=='title':
                header=FleetPageHeader(title,icon_key,page)
                layout.replaceWidget(widget,header);widget.hide();widget.deleteLater()
                return header
            if item.layout() is not None:
                found=replace(item.layout())
                if found is not None:return found
        return None
    return replace(page.layout())


@lru_cache(maxsize=32)
def navigation_icon(index,size=24):
    """Fleet navigation icons, with independent normal and selected contrast."""
    image=QPixmap(size*2,size*2);image.fill(Qt.GlobalColor.transparent);image.setDevicePixelRatio(2)
    p=QPainter(image);p.setRenderHint(QPainter.RenderHint.Antialiasing);p.scale(size/24,size/24)
    p.setPen(QPen(QColor('#C0DAED'),1.4,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin));p.setBrush(Qt.BrushStyle.NoBrush)
    if index==0:
        p.drawRoundedRect(QRectF(5,5,15,15),1,1);p.drawLine(2,2,17,2);p.drawLine(2,2,2,17)
        for y in (9,13,17):p.drawLine(9,y,17,y)
    elif index==1:
        p.drawEllipse(QPointF(12,12),7,7);p.drawLine(12,1,12,23);p.drawLine(1,12,23,12);p.drawEllipse(QPointF(12,12),2,2)
    elif index==2:
        p.drawRoundedRect(QRectF(2,5,11,7),3,3);p.drawRoundedRect(QRectF(11,12,11,7),3,3);p.drawLine(9,10,15,14)
    elif index==3:
        p.drawRect(QRectF(5,2,14,20));p.drawLine(8,6,16,6);p.drawLine(8,10,16,10);p.drawLine(8,14,13,14);p.drawLine(8,18,16,18)
    elif index==4:
        p.drawEllipse(QPointF(12,12),9,9);path=QPainterPath();path.moveTo(16,5);path.lineTo(14,14);path.lineTo(7,19);path.lineTo(9,9);path.closeSubpath();p.drawPath(path)
    elif index==5:
        p.drawRoundedRect(QRectF(2,5,8,14),1,1);p.drawRoundedRect(QRectF(14,5,8,14),1,1);p.drawLine(7,2,17,2);p.drawLine(7,22,17,22)
    elif index==6:
        for x,y,h in ((3,14,7),(10,9,12),(17,3,18)):p.drawRect(QRectF(x,y,4,h))
    elif index==7:
        p.drawEllipse(QPointF(12,7),4,4);path=QPainterPath();path.moveTo(4,21);path.cubicTo(4,10,20,10,20,21);p.drawPath(path);p.drawLine(4,21,20,21)
    elif index==9:
        p.drawRect(QRectF(3,4,18,16)); path=QPainterPath();path.moveTo(5,15);path.lineTo(9,11);path.lineTo(12,14);path.lineTo(18,7);p.drawPath(path)
    elif index==10:
        p.drawEllipse(QPointF(10,10),6,6);p.drawLine(14,15,21,22)
    elif index==11:
        roof=QPainterPath(QPointF(2,11));roof.lineTo(12,3);roof.lineTo(22,11)
        p.drawPath(roof);p.drawRect(QRectF(5,11,14,10));p.drawRect(QRectF(10,15,4,6))
    else:
        path=QPainterPath()
        for i in range(8):
            a=i*math.pi/4;point=QPointF(12+9*math.sin(a),12-9*math.cos(a))
            if i==0:path.moveTo(point)
            else:path.lineTo(point)
            p.drawLine(QPointF(12,12),point)
        path.closeSubpath();p.drawPath(path)
    p.end();icon=QIcon(image)
    selected = image.copy()
    p = QPainter(selected)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    p.fillRect(selected.rect(), QColor(colors.PAPER))
    p.end()
    icon.addPixmap(selected, QIcon.Mode.Normal, QIcon.State.On)
    icon.addPixmap(selected, QIcon.Mode.Active, QIcon.State.On)
    return icon
