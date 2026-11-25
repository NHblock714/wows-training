"""Startup page for importing and reviewing World of Warships recordings."""
from PySide6.QtCore import QEasingCurve, QPointF, QRectF, QSize, QTimer, Qt, QUrl, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from . import academy_palette as palette
from .fleet_shell import FleetBackdrop, navigation_icon
from .layered_panel import panel_path
from .motion import motion_enabled, subscribe_motion
from .naval_assets import default_naval_assets
from .naval_widgets import _visible_image_rect
from .navigation import QUERY_PAGE, REVIEW_PAGE
from .presentation_fonts import load_presentation_fonts


SHORTCUTS = (
    (0, "录像库", "浏览已导入的回放"),
    (REVIEW_PAGE, "单局分析", "查看结算与表现"),
    (QUERY_PAGE, "战绩查询", "查询账号与公会"),
    (3, "报告工作台", "编辑并导出报告"),
)

_SHORTCUT_INK = {0: '#276CB7', REVIEW_PAGE: '#087897', QUERY_PAGE: '#4E63A6', 3: '#755BA6'}


def _cover(image, rect, horizontal=.62):
    scale = max(rect.width()/image.width(), rect.height()/image.height())
    width, height = rect.width()/scale, rect.height()/scale
    return QRectF((image.width()-width)*horizontal, (image.height()-height)*.44, width, height)


class _HarborHero(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("homeHero")
        self.setProperty("backgroundEffect", True)
        self.setMinimumHeight(345)
        self.setMaximumHeight(625)
        self._background_enabled = True
        self._backdrop = None
        self._offset = QPointF()
        self._reveal = 1.
        self._entrance = QVariantAnimation(self)
        self._entrance.setDuration(560)
        self._entrance.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._entrance.setStartValue(0.)
        self._entrance.setEndValue(1.)
        self._entrance.valueChanged.connect(self._set_reveal)
        self._image = None
        self._wide_scene = False
        assets = default_naval_assets()
        for key in ("harbor-home", "harbor-account"):
            try:
                self._image = assets.background(key)
            except (OSError, ValueError):
                continue
            if self._image is not None:
                self._wide_scene = key == "harbor-home"
                break

    def sizeHint(self):
        return QSize(1150, 510)

    def set_background_enabled(self, enabled):
        self._background_enabled = bool(enabled)
        if not enabled:
            self._settle()
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        subscribe_motion(self, self._motion_changed)
        backdrop = self.window().findChild(FleetBackdrop)
        if backdrop is not self._backdrop:
            if self._backdrop is not None:
                self._backdrop.parallaxChanged.disconnect(self._shift_frame)
            self._backdrop = backdrop
            if backdrop is not None:
                backdrop.parallaxChanged.connect(self._shift_frame)
        if self._background_enabled and motion_enabled(self) and not self.window().isMinimized():
            if backdrop is not None:
                self._offset = backdrop.parallax
            self._set_reveal(0.)
            self._entrance.start()

    def hideEvent(self, event):
        self._settle()
        super().hideEvent(event)

    def _settle(self):
        self._entrance.stop()
        self._offset = QPointF()
        self._set_reveal(1.)

    def _motion_changed(self, enabled):
        if not enabled:
            self._settle()

    def _shift_frame(self, offset):
        if self.isVisible() and not self.window().isMinimized() and self._background_enabled and motion_enabled(self):
            self._offset = offset
            self._update_corners()

    def _set_reveal(self, value):
        self._reveal = float(value)
        self._update_corners()

    def _update_corners(self):
        self.update(0, self.height() - 76, 315, 76)
        self.update(self.width() - 300, 0, 300, 94)

    @staticmethod
    def _polygon(painter, rect, points, color):
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawPolygon(QPolygonF([QPointF(rect.left()+x*rect.width(), rect.top()+y*rect.height())
                                       for x,y in points]))

    def paintEvent(self, _event):
        bounds = QRectF(self.rect()).adjusted(1, 1, -5, -6)
        if bounds.isEmpty():
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            frame = panel_path(bounds, 36)
            painter.fillPath(panel_path(bounds.translated(3, 4), 36), QColor(24, 65, 109, 42))
            painter.setClipPath(frame)
            base = QLinearGradient(bounds.topLeft(), bounds.bottomRight())
            for stop, color in ((0, "#F8FCFF"), (.40, "#E4F1FD"), (.78, "#BDD5F5"), (1, "#B3B8E4")):
                base.setColorAt(stop, QColor(color))
            painter.fillRect(bounds, base)
            if self._background_enabled:
                if self._wide_scene:
                    self._paint_wide_scene(painter, bounds)
                else:
                    self._paint_scene(painter, bounds)
                    self._paint_facets(painter, bounds)
                self._paint_glass_corners(painter, bounds)
            painter.setClipping(False)
            painter.setPen(QPen(QColor("#9BBBE0"), 1.1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(frame)
            rail = QRectF(bounds.left()+13, bounds.top()+2, 65, 3)
            painter.fillRect(rail, QColor(palette.BLUE))
            painter.fillRect(rail.translated(70, 0).adjusted(0, 0, -49, 0), QColor(palette.PINK))
        finally:
            painter.end()

    def _paint_glass_corners(self, painter, bounds):
        # The foreground glass floats above the fixed harbor photograph. Only
        # the two corner strips need repainting as the shared parallax settles.
        inset = (1. - self._reveal) * 36.
        painter.save()
        painter.translate(bounds.right() + inset - self._offset.x() * .8,
                          bounds.top() - self._offset.y() * .55)
        pane = QPainterPath()
        pane.moveTo(-274, 0)
        pane.lineTo(-30, 0)
        pane.lineTo(1, 31)
        pane.lineTo(1, 82)
        pane.lineTo(-29, 52)
        pane.lineTo(-83, 52)
        pane.lineTo(-110, 25)
        pane.lineTo(-249, 25)
        pane.closeSubpath()
        glass = QLinearGradient(-260, 0, 0, 60)
        glass.setColorAt(0, QColor(228, 248, 255, 24))
        glass.setColorAt(.65, QColor(241, 252, 255, 105))
        glass.setColorAt(1, QColor(99, 183, 227, 85))
        painter.fillPath(pane, glass)
        outline = QPolygonF([QPointF(-249,25), QPointF(-110,25), QPointF(-83,52),
                             QPointF(-29,52), QPointF(-4,77)])
        painter.setPen(QPen(QColor(238, 253, 255, 201), 1.35))
        painter.drawPolyline(outline)
        painter.setPen(QPen(QColor(62, 126, 187, 125), 1))
        painter.drawPolyline(outline.translated(1, 3))
        painter.setPen(QPen(QColor(109, 226, 250, 204), 2.2))
        painter.drawLine(QPointF(-79, 49), QPointF(-38, 49))
        # Registration ticks are engraved inside the glass and move with its lip.
        painter.setPen(QPen(QColor(53, 110, 156, 119), .9))
        for index in range(13):
            x = -227 + index*6
            tick = 5 if index % 4 == 0 else 2
            painter.drawLine(QPointF(x, 21-tick), QPointF(x, 21))
        painter.setPen(QPen(QColor(240, 252, 255, 202), 1))
        painter.drawLine(QPointF(-229, 22), QPointF(-151, 22))
        painter.restore()

        painter.save()
        painter.translate(bounds.left() - inset + self._offset.x() * .65,
                          bounds.bottom() + self._offset.y() * .55)
        lip = QPolygonF([QPointF(0,-58), QPointF(47,-11), QPointF(177,-11),
                         QPointF(185,-19), QPointF(270,-19), QPointF(289,0), QPointF(0,0)])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(224, 245, 255, 110))
        painter.drawPolygon(lip)
        painter.setPen(QPen(QColor(79, 148, 194, 122), 1))
        painter.drawPolyline(lip.mid(0, 5))
        painter.setPen(QPen(QColor(36, 115, 176, 125), 1.6))
        for i in range(6):
            x = 52 + i * 5
            painter.drawLine(QPointF(x,-5), QPointF(x+4,-9))
        painter.setPen(QPen(QColor(53, 118, 170, 129), 1))
        painter.setBrush(QColor(238, 250, 255, 205))
        painter.drawEllipse(QPointF(249, -19), 2.5, 2.5)
        painter.restore()

    def _paint_wide_scene(self, painter, bounds):
        painter.drawImage(bounds, self._image, _cover(self._image, bounds, horizontal=.68))
        clarity = QLinearGradient(bounds.left(), 0, bounds.right(), 0)
        clarity.setColorAt(0, QColor(241, 249, 255, 180))
        clarity.setColorAt(.33, QColor(234, 244, 255, 130))
        clarity.setColorAt(.49, QColor(223, 235, 253, 83))
        clarity.setColorAt(.65, QColor(213, 231, 255, 0))
        painter.fillRect(bounds, clarity)

    def _paint_scene(self, painter, bounds):
        left, top, right, bottom = bounds.left(), bounds.top(), bounds.right(), bounds.bottom()
        width = bounds.width()
        scene = QRectF(left+width*.405, top, width*.595, bounds.height())
        seam_top = QPointF(left+width*.525, top)
        seam_bottom = QPointF(scene.left(), bottom)
        clip = QPainterPath()
        clip.addPolygon(QPolygonF([seam_top, QPointF(right, top), QPointF(right, bottom), seam_bottom]))
        painter.save()
        painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
        if self._image is not None:
            painter.drawImage(scene, self._image, _cover(self._image, scene))
        tint = QLinearGradient(scene.topLeft(), scene.bottomRight())
        tint.setColorAt(0, QColor(112, 177, 241, 48))
        tint.setColorAt(.58, QColor(55, 107, 178, 12))
        tint.setColorAt(1, QColor(40, 57, 134, 68))
        painter.fillRect(scene, tint)
        painter.restore()
        painter.setPen(QPen(QColor("#E5F7FF"), 3))
        painter.drawLine(seam_top, seam_bottom)
        painter.setPen(QPen(QColor(92, 165, 230, 175), 1))
        painter.drawLine(seam_top+QPointF(5, 0), seam_bottom+QPointF(5, 0))

    def _paint_facets(self, painter, bounds):
        for points, color in (
            (((0, 0), (.21, 0), (.09, .36)), QColor(108, 173, 232, 40)),
            (((.21, 0), (.39, 0), (.29, .29)), QColor(169, 196, 249, 38)),
            (((0, .74), (.12, 1), (0, 1)), QColor(105, 175, 238, 44)),
            (((.12, 1), (.44, 1), (.31, .62)), QColor(142, 159, 225, 44)),
            (((.57, 0), (.77, 0), (.68, .31)), QColor(203, 234, 255, 48)),
            (((.80, 1), (1, .58), (1, 1)), QColor(114, 112, 221, 51)),
        ):
            self._polygon(painter, bounds, points, color)

        x, y, width, height = bounds.x(), bounds.y(), bounds.width(), bounds.height()
        stripe = QPainterPath()
        stripe.addPolygon(QPolygonF([QPointF(x+.86*width,y+.12*height),
                                     QPointF(x+.955*width,y+.12*height),
                                     QPointF(x+.955*width,y+.34*height)]))
        painter.save()
        painter.setClipPath(stripe, Qt.ClipOperation.IntersectClip)
        painter.setPen(QPen(QColor(220, 245, 255, 99), 1.25))
        for i in range(34):
            xx = x+.81*width+i*7
            painter.drawLine(QPointF(xx,y+.11*height), QPointF(xx-.12*height,y+.36*height))
        painter.restore()


class _HomeShortcut(QPushButton):
    def __init__(self, page, title, hint):
        super().__init__()
        self.setObjectName("homeShortcut")
        self.setText(title)
        self.setToolTip(hint)
        self.setAccessibleName(title)
        self.setAccessibleDescription(hint)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._hint = hint
        self._accent = QColor(_SHORTCUT_INK[page])
        self._background_enabled = True
        self._icon = navigation_icon(page, 26).pixmap(QSize(26, 26), 2.)
        with QPainter(self._icon) as painter:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(self._icon.rect(), self._accent)

    def sizeHint(self):
        return QSize(185, 91)

    def minimumSizeHint(self):
        return QSize(148, 82)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event):
        # Standalone previews can render without a window motion controller.
        box = QRectF(self.rect()).adjusted(1, 1, -4, -5)
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            face = panel_path(box, 13)
            painter.fillPath(panel_path(box.translated(2, 3), 13), QColor(31,82,141,36))
            painter.fillPath(face, QColor("#DDEFFC" if self.underMouse() else "#EDF4FB"))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(palette.PINK if self.hasFocus() else "#AFC9E0"),1.2))
            painter.drawPath(face)
            painter.fillRect(QRectF(box.x()+12, box.y()+2, 30, 2), QColor(palette.BLUE))
            self.paint_card_content(painter, box, float(self.underMouse()))

    def paint_card_content(self, painter, box, hover):
        center_y = box.center().y()
        compact = box.width() < 180
        badge_size = 32 if compact else 36
        badge = QRectF(box.x()+(7 if compact else 10), center_y-badge_size/2, badge_size, badge_size)
        stamp = panel_path(badge, 7)
        tint = QColor(self._accent)
        if self._background_enabled:
            tint.setAlpha(round(37 + 16*hover))
            painter.fillPath(stamp.translated(0, 1.5), tint)
            enamel = QLinearGradient(badge.topLeft(), badge.bottomRight())
            enamel.setColorAt(0, QColor(247, 254, 255, 250))
            enamel.setColorAt(.54, QColor(224, 241, 249, 252))
            enamel.setColorAt(1, QColor(190, 214, 236, 246))
            painter.fillPath(stamp, enamel)
            painter.setPen(QPen(QColor(255, 255, 255, 220), 1))
            painter.drawLine(badge.topLeft()+QPointF(5, 2), badge.topRight()+QPointF(-9, 2))
            # A small slanted index on the shoulder follows the card's existing hover.
            tint.setAlpha(round(52 + 90*hover))
            painter.setPen(QPen(tint, 1.2))
            if not compact:
                for index in range(3):
                    x = box.right()-29 + 4*index
                    painter.drawLine(QPointF(x, box.bottom()-7), QPointF(x+2, box.bottom()-10-2*hover))
        else:
            tint.setAlpha(round(14 + 15 * hover))
            painter.fillPath(stamp, tint)
        tint.setAlpha(round(59 + 63 * hover))
        painter.setPen(QPen(tint, .9))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(stamp)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(badge.adjusted(5, 5, -5, -5), self._icon, QRectF(self._icon.rect()))
        painter.setPen(QPen(self._accent,1.6,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap))
        xx = box.right()-16+2*hover
        arrow_y = box.bottom()-10 if compact else center_y
        if hover > .01:
            painter.drawLine(QPointF(xx-2-7*hover,arrow_y), QPointF(xx-1,arrow_y))
        painter.drawLine(QPointF(xx-4,arrow_y-4),QPointF(xx,arrow_y))
        painter.drawLine(QPointF(xx,arrow_y),QPointF(xx-4,arrow_y+4))
        x = badge.right() + (6 if compact else 9)
        width = max(1,box.right()-(6 if compact else 25)-x)
        font = QFont(self.font())
        body = max(12,font.pixelSize())
        font.setPixelSize(body+3)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(palette.INK))
        flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        title = painter.fontMetrics().elidedText(self.text(),Qt.TextElideMode.ElideRight,int(width))
        painter.drawText(QRectF(x,center_y-24,width,24),flags,title)
        font.setPixelSize(body)
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(QColor("#345B83"))
        hint = painter.fontMetrics().elidedText(self._hint,Qt.TextElideMode.ElideRight,int(width))
        painter.drawText(QRectF(x,center_y+2,width,22),flags,hint)


class HomePanel(QWidget):
    import_requested = Signal()
    page_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("homePage")
        self.setAccessibleName("首页")
        self.setProperty("backgroundEffect", True)
        self._background_enabled = True
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 22)
        root.setSpacing(15)
        self.hero = _HarborHero()
        root.addWidget(self.hero, 5)
        row = QHBoxLayout(self.hero)
        row.setContentsMargins(37, 34, 32, 37)
        row.setSpacing(0)
        intro = QVBoxLayout()
        intro.setSpacing(0)
        intro.addStretch()
        logo = QLabel()
        logo.setAccessibleName("港口开发部")
        self._set_brand_logo(logo)
        intro.addWidget(logo, 0, Qt.AlignmentFlag.AlignLeft)
        intro.addSpacing(15)
        title = QLabel("战绩查询与录像分析")
        title.setObjectName("title")
        title.setWordWrap(True)
        family = load_presentation_fonts()["chinese_tech"]
        title.setStyleSheet(
            f'QLabel#title {{font-family:"{family}";font-size:38px;font-weight:400;'
            'color:#102B4B;border:none;padding:0; }'
        )
        intro.addWidget(title)
        intro.addSpacing(8)
        desc = QLabel("导入战舰世界录像，查看对局和战绩，整理分析报告。")
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#345A80;")
        intro.addWidget(desc)
        intro.addSpacing(27)
        self.import_button = QPushButton("导入录像")
        self.import_button.setObjectName("homeImport")
        self.import_button.setAccessibleName("导入录像")
        self.import_button.setMinimumSize(138, 43)
        self.import_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.import_button.clicked.connect(self.import_requested)
        intro.addWidget(self.import_button, 0, Qt.AlignmentFlag.AlignLeft)
        intro.addSpacing(10)
        drop = QLabel("也可将 .wowsreplay 文件拖入窗口")
        drop.setWordWrap(True)
        drop.setStyleSheet("color:#486784;")
        intro.addWidget(drop)
        intro.addStretch()
        row.addLayout(intro, 9)
        row.addStretch(11)

        shortcuts = QHBoxLayout()
        shortcuts.setSpacing(12)
        self._shortcut_timer = QTimer(self, singleShot=True, interval=100)
        self._shortcut_timer.timeout.connect(self._open_shortcut)
        self._shortcut_page = None
        self.shortcuts = {}
        for page,title,hint in SHORTCUTS:
            action = _HomeShortcut(page, title, hint)
            action.clicked.connect(lambda checked=False, i=page: self._activate_shortcut(i))
            shortcuts.addWidget(action, 1)
            self.shortcuts[page] = action
        root.addLayout(shortcuts)
        root.addStretch(1)
        self._credit = self._make_credit()
        root.addWidget(self._credit, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addStretch(1)

    def set_background_enabled(self, enabled):
        self._background_enabled = bool(enabled)
        for shortcut in self.shortcuts.values():
            shortcut._background_enabled = self._background_enabled
            shortcut.update()
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._background_enabled:
            return
        # The two chart traces end before the live link. Their span follows its
        # actual geometry, including changes in system font and display scale.
        credit = QRectF(self._credit.geometry())
        y = credit.center().y()
        if event.rect().bottom() < y-48 or event.rect().top() > y+48:
            return
        room = min(y - max(card.geometry().bottom() for card in self.shortcuts.values()),
                   self.height() - y)
        if room < 18:
            return
        depth = min(38., room * .40)
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            for origin, direction, end, ink in (
                (24., 1., credit.left()-18, '#419AC8'),
                (self.width()-24., -1., credit.right()+18, '#8983C1'),
            ):
                length = (end-origin) * direction
                if length < 84:
                    continue
                painter.save()
                painter.translate(origin, y)
                painter.scale(direction, direction)
                knee = length * .54
                edge = QPolygonF([QPointF(0,depth), QPointF(knee-depth,depth),
                                  QPointF(knee,0), QPointF(length,0)])
                lower = QPolygonF([QPointF(length-16,6), QPointF(knee+4,6),
                                   QPointF(knee-depth+4,depth+5), QPointF(0,depth+5)])
                wash = QLinearGradient(0, 0, length, 0)
                accent = QColor(ink)
                for stop, alpha in ((0, 0), (.42, 31), (1, 76)):
                    accent.setAlpha(alpha)
                    wash.setColorAt(stop, accent)
                painter.setBrush(wash)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawPolygon(edge + lower)
                painter.setPen(QPen(wash, 1))
                painter.drawPolyline(edge)
                painter.setPen(QPen(QColor(ink), 1.3))
                for i in range(3):
                    x = length-13+i*5
                    painter.drawLine(QPointF(x,-2), QPointF(x+2,-5))
                painter.restore()

    def showEvent(self, event):
        super().showEvent(event)
        subscribe_motion(self, self._motion_changed)

    def hideEvent(self, event):
        self._shortcut_timer.stop()
        self._shortcut_page = None
        super().hideEvent(event)

    def _activate_shortcut(self, page):
        if self._shortcut_timer.isActive():
            return
        if self.isVisible() and motion_enabled(self):
            # Let the click register visually before the page sweep begins.
            self._shortcut_page = page
            self._shortcut_timer.start()
        else:
            self.page_requested.emit(page)

    def _open_shortcut(self):
        page, self._shortcut_page = self._shortcut_page, None
        if page is not None:
            self.page_requested.emit(page)

    def _motion_changed(self, enabled):
        if not enabled and self._shortcut_timer.isActive():
            self._shortcut_timer.stop()
            self._open_shortcut()

    @staticmethod
    def _make_credit():
        link = QLabel('<a href="https://github.com/NHblock714" style="color:#25547E;text-decoration:none;">'
                      '<b>Developed by</b> NHblock</a>')
        link.setObjectName("homeCreditLink")
        link.setTextFormat(Qt.TextFormat.RichText)
        link.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse |
                                     Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        link.setToolTip("GitHub · NHblock714")
        link.setAccessibleName("Developed by NHblock · GitHub")
        link.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        link.setCursor(Qt.CursorShape.PointingHandCursor)
        family = load_presentation_fonts()["grade"]
        link.setStyleSheet(
            f'QLabel#homeCreditLink {{font-family:"{family}";font-size:23px;font-style:italic;'
            'background:transparent;border:1px solid transparent;border-radius:4px;padding:3px 8px;} '
            'QLabel#homeCreditLink:hover, QLabel#homeCreditLink:focus {'
            'background:rgba(196,225,248,130);border-color:#9DBFDF;}'
        )
        link.linkActivated.connect(lambda href: QDesktopServices.openUrl(QUrl(href)))
        return link

    @staticmethod
    def _set_brand_logo(label):
        try:
            image = default_naval_assets().image("icons/harbor-wordmark.png")
        except (OSError, ValueError):
            image = None
        if image is None:
            label.setText("港口开发部")
            return
        image = image.copy(_visible_image_rect(image).toRect())
        logo = image.scaled(QSize(330,124),Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)
        logo.setDevicePixelRatio(2)
        label.setPixmap(QPixmap.fromImage(logo))
