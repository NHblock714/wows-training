"""Startup page for importing and reviewing World of Warships recordings."""
from PySide6.QtCore import QPointF, QRectF, QSize, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from . import academy_palette as palette
from .fleet_shell import navigation_icon
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
        self.update()

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
            painter.setClipping(False)
            painter.setPen(QPen(QColor("#9BBBE0"), 1.1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(frame)
            rail = QRectF(bounds.left()+13, bounds.top()+2, 65, 3)
            painter.fillRect(rail, QColor(palette.BLUE))
            painter.fillRect(rail.translated(70, 0).adjusted(0, 0, -49, 0), QColor(palette.PINK))
        finally:
            painter.end()

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
        self._icon = navigation_icon(page).pixmap(QSize(26, 26))
        with QPainter(self._icon) as painter:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(self._icon.rect(), QColor("#2477C1"))

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
        painter.drawPixmap(QRectF(box.x()+15,center_y-13,26,26), self._icon, QRectF(self._icon.rect()))
        painter.setPen(QPen(QColor("#427EB7"),1.6,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap))
        xx = box.right()-16+2*hover
        painter.drawLine(QPointF(xx-4,center_y-4),QPointF(xx,center_y))
        painter.drawLine(QPointF(xx,center_y),QPointF(xx-4,center_y+4))
        x = box.left()+55
        width = max(1,box.right()-25-x)
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
        root.addWidget(self._make_credit(), 0, Qt.AlignmentFlag.AlignHCenter)
        root.addStretch(1)

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
