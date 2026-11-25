"""Public-account achievements presented as a filterable naval medal gallery."""
from __future__ import annotations

import math
import re

from PySide6.QtCore import QEvent, QEasingCurve, QPointF, QRectF, QSignalBlocker, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QHoverEvent, QLinearGradient, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QBoxLayout, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QSpacerItem, QVBoxLayout, QWidget,
)

from ..f1_ratings.challenges import CHALLENGES
from . import academy_palette as tone
from .achievement_banner_seal import paint_gallery_seal
from .achievement_expanded_art import expanded_art_width, paint_expanded_achievement
from .achievement_rarity import rarity_style
from .achievement_texture import paint_achievement_texture
from .achievement_wordmark import RarityWordmark
from .chart_motion import ChartReveal
from .harbor_showcase import HarborShowcase
from .layered_panel import LayeredPanel, panel_path
from .motion import motion_enabled
from .presentation_fonts import chinese_sans_families, load_presentation_fonts


GROUPS = {
    "record": "单场纪录",
    "mastery": "战舰专精",
    "career": "生涯积累",
    "fleet": "舰队构成",
    "collection": "战舰收藏",
    "battle": "战斗成就",
}

# Tuple-only snapshots written before these catalogue renames carry no IDs.
_HISTORICAL_NAMES = {
    "亡牌飞行员": "battle_planes",
    "观察模式": "battle_scouting",
    "碰碰船": "ramming",
}

# Engravings are drawn in medal-radius units so that the shield and ribbon
# remain consistent across the six families.
_ENGRAVINGS = {
    "record": (((0, -.42), (.11, -.10), (.41, 0), (.11, .10),
                (0, .42), (-.11, .10), (-.41, 0), (-.11, -.10), (0, -.42)),),
    "mastery": (((-.39, .05), (0, -.30), (.39, .05)), ((-.29, .26), (0, .01), (.29, .26))),
    "career": (((-.38, -.22), (0, -.02), (.38, -.22)), ((-.38, .06), (0, .26), (.38, .06))),
    "fleet": (((-.42, .24), (0, -.35), (.42, .24), (0, .08), (-.42, .24)),),
    "collection": (((-.40, -.08), (-.21, -.30), (.21, -.30), (.40, -.08), (0, .34),
                    (-.40, -.08), (.40, -.08)),),
}


def _label(text="", *, pixels=None, family=None, color=tone.INK, bold=False):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    style = f"background:transparent;color:{color};"
    if pixels:
        style += f"font-size:{pixels}px;"
    if family:
        families = (family,) if isinstance(family, str) else family
        style += "font-family:" + ",".join(f'"{name}"' for name in families) + ";"
    if bold:
        style += "font-weight:600;"
    label.setStyleSheet(style)
    return label


def _detail_section(title, body, accent, family, *, panel_alpha=110):
    section = QFrame()
    section.setObjectName("achievementDetailSection")
    section.setStyleSheet(
        f"QFrame#achievementDetailSection {{background:rgba(255,255,255,{panel_alpha});"
        f"border-left:2px solid {accent};border-radius:3px;}}"
    )
    layout = QVBoxLayout(section)
    layout.setContentsMargins(14, 10, 14, 12)
    layout.setSpacing(5)
    layout.addWidget(_label(title, pixels=12, family=family, color=accent, bold=True))
    layout.addWidget(body)
    return section


def _group_label(group):
    return GROUPS.get(group, group)


def earned_achievements(detail):
    """Return selectable earned records from current or legacy account detail."""
    rows = detail.get("achievement_gallery")
    if rows is None:
        rows = ({"name": row[0], "requirement": row[1], "description": row[2] if len(row) > 2 else "",
                 "status": "earned"} for row in detail.get("challenges", ()) if len(row) >= 2)
    by_id = {challenge.id: challenge for challenge in CHALLENGES}
    by_name = {challenge.name: challenge for challenge in CHALLENGES}
    earned = []
    for row in rows:
        if row.get("status") != "earned" or not row.get("name"):
            continue
        name = row["name"]
        source_id = row.get("id") or _HISTORICAL_NAMES.get(name)
        challenge = by_id.get(source_id) if source_id else by_name.get(name)
        earned.append({
            **row,
            "id": row.get("id") or (challenge.id if challenge else name),
            "name": challenge.name if challenge else name,
            "group": row.get("group") or (challenge.group if challenge else "战绩"),
            "rarity": rarity_style(challenge.rarity if challenge else row.get("rarity")).label,
        })
    return earned


def _measure(entry):
    value = entry.get("value")
    if type(value) not in (int, float) or not math.isfinite(value):
        return ""
    unit = entry.get("unit") or ""
    shown = f"{value:,.2f}".rstrip("0").rstrip(".")
    if entry.get("is_lower_bound"):
        shown = "≥ " + shown
    elif entry.get("is_upper_bound"):
        shown = "≤ " + shown
    return shown + unit


def paint_medal(painter, center, radius, color, *, earned=False, group=None):
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    ribbon = QPolygonF([center + QPointF(x * radius, y * radius)
                       for x, y in ((-.64, .31), (-.45, 1.19), (0, .96), (.45, 1.19), (.64, .31))])
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color.darker(145))
    painter.drawPolygon(ribbon)
    shield = QPolygonF([center + QPointF(x * radius, y * radius)
                       for x, y in ((-.64, -.74), (.64, -.74), (.87, -.38), (.65, .42),
                                    (0, .82), (-.65, .42), (-.87, -.38))])
    inset = QLinearGradient(center - QPointF(radius, radius), center + QPointF(radius, radius))
    inset.setColorAt(0, color.lighter(150))
    inset.setColorAt(.36, color)
    inset.setColorAt(1, color.darker(174))
    painter.setBrush(inset)
    painter.setPen(QPen(color.lighter(144), 1.1))
    painter.drawPolygon(shield)
    painter.setPen(QPen(QColor(tone.NIGHT), max(1., radius / 13)))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if group in _ENGRAVINGS:
        for engraving in _ENGRAVINGS[group]:
            painter.drawPolyline(QPolygonF([center + QPointF(x * radius, y * radius) for x, y in engraving]))
    elif earned:
        painter.drawPolyline(QPolygonF([center + QPointF(-.35 * radius, -.01 * radius),
                                       center + QPointF(-.07 * radius, .24 * radius),
                                       center + QPointF(.39 * radius, -.26 * radius)]))
    else:
        painter.drawPolygon(QPolygonF([center + QPointF(x * radius, y * radius)
                                       for x, y in ((0, -.34), (.33, 0), (0, .34), (-.33, 0))]))
    painter.restore()


class _Banner(LayeredPanel):
    def __init__(self):
        super().__init__(dark=True, accent=tone.CYAN, cut=23)
        self.setMinimumHeight(142)
        self.earned = 0
        self.setProperty("backgroundEffect", True)
        self._background_enabled = True
        self._art = HarborShowcase(instrument=True)
        self._reveal = ChartReveal(self, 575)
        fonts = load_presentation_fonts()
        row = QHBoxLayout(self)
        row.setContentsMargins(24, 16, 16, 18)
        labels = QVBoxLayout()
        labels.setSpacing(2)
        labels.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        labels.addWidget(_label("成就展厅", pixels=26, family=fonts["chinese_tech"], color="#F1F8FF"))
        self.count = _label(pixels=20, family=fonts["chinese_body"], color="#A4E9F6")
        labels.addWidget(self.count)
        row.addLayout(labels, 1)
        self._seal_space = QSpacerItem(150, 0)
        row.addSpacerItem(self._seal_space)

    def set_background_enabled(self, enabled):
        self._background_enabled = bool(enabled)
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        settings = getattr(self.window(), "client_preferences", {})
        self.set_background_enabled(settings.get("background_enabled", self._background_enabled))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._seal_space.changeSize(min(150, self.width() // 3), 0)
        self.layout().invalidate()

    def set_summary(self, earned):
        self.earned = earned
        self.count.setText(f"已达成 {earned} 项" if earned else "")
        self.count.setVisible(bool(earned))
        self._reveal.set_content(earned)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        surface = QRectF(self.rect()).adjusted(5, 2, -7, -7)
        painter.setClipPath(panel_path(surface, self.cut))
        painter.setClipRect(surface.adjusted(4, 0, -3, -1), Qt.ClipOperation.IntersectClip)
        if self._background_enabled:
            self._art.paint(painter, surface)
        shade = QLinearGradient(surface.topLeft(), surface.topRight())
        shade.setColorAt(0, QColor(10, 28, 52, 248))
        shade.setColorAt(.60, QColor(19, 52, 87, 212))
        shade.setColorAt(1, QColor(18, 53, 83, 153))
        painter.fillRect(surface, shade)
        seal_scale = min(1., self._seal_space.sizeHint().width() / 150)
        center = QPointF(surface.right() - 12 - 73 * seal_scale, surface.center().y())
        painter.setPen(QPen(QColor(161, 217, 252, 56), 1))
        for drift in (-32, -21, 24, 34):
            start = QPointF(surface.left() + surface.width() * .46, center.y() + drift)
            bend = QPointF(center.x() - 122 - abs(drift) * 1.4, start.y())
            end = QPointF(center.x()-58, center.y()+drift/2)
            painter.drawPolyline(QPolygonF([start,bend,end]))
        painter.translate(center)
        painter.scale(seal_scale, seal_scale)
        center = QPointF()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(161, 217, 252, 90), 1))
        painter.drawEllipse(center, 51, 51)
        painter.drawEllipse(center, 59, 59)
        arc_pen = QPen(QColor(tone.CYAN), 3.2)
        arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(arc_pen)
        if self.earned:
            painter.drawArc(QRectF(center.x()-51, center.y()-51, 102, 102),
                            90*16, -round(360*16*self._reveal.progress))
        for index in range(16):
            angle = index * math.tau / 16
            axis = QPointF(math.cos(angle), math.sin(angle))
            painter.setPen(QPen(QColor(121, 192, 234, 163), 1))
            painter.drawLine(center + axis * 61, center + axis * (66 if index % 4 == 0 else 63))
        paint_gallery_seal(painter, center, bool(self.earned), self._reveal.progress)
        painter.end()


class _MedalIcon(QWidget):
    def __init__(self, style, group):
        super().__init__()
        self.color = QColor(style.accent)
        self.rank = style.rank
        self.progress = 0.
        self.group = group
        self.setFixedSize(54, 54)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        mount = QRectF(2, 2, 50, 50)
        metal = QLinearGradient(mount.topLeft(), mount.bottomRight())
        metal.setColorAt(0, QColor("#405E77"))
        metal.setColorAt(.53, QColor("#21384D"))
        metal.setColorAt(1, QColor("#102339"))
        painter.setBrush(metal)
        painter.setPen(QPen(QColor("#B5C8D8"), 1))
        painter.drawEllipse(mount)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # Opposing index marks open into segmented, counter-rotating bezels.
        segments = 2 + self.rank // 2
        pitch = 360 // segments
        rotation = self.progress * (5 + 8 * self.rank)
        painter.setPen(QPen(self.color, 1.1 + self.rank * .19))
        for index in range(segments):
            start = 35 + pitch * index + rotation
            painter.drawArc(mount.adjusted(3, 3, -3, -3), round(start * 16), (pitch // 2) * 16)
        if self.rank >= 3:
            inner_ring = mount.adjusted(6, 6, -6, -6)
            painter.setPen(QPen(self.color.lighter(135), 1.1))
            for start in (105, 285):
                painter.drawArc(inner_ring, round((start - rotation) * 16), (9 * self.rank) * 16)
        paint_medal(painter, mount.center() - QPointF(0, 2), 17, self.color, earned=True, group=self.group)
        painter.end()


class _MedalCard(LayeredPanel):
    DETAIL_INK = "#2A4056"
    CLOSED_MIN_HEIGHT = 104
    expansionRequested = Signal()

    def __init__(self, entry, font_family):
        name = str(entry["name"])
        group = entry.get("group") or "战绩"
        style = rarity_style(entry.get("rarity"))
        super().__init__(accent=style.accent, cut=15)
        self.rarity = style
        self._background_enabled = True
        self._texture_pointer = None
        self._texture_motion = QVariantAnimation(self)
        self._texture_motion.setDuration(155)
        self._texture_motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._texture_motion.valueChanged.connect(self._texture_frame)
        self._ornament_phase = 0.
        self._ornament = None
        if style.rank >= 3:
            self._ornament = QVariantAnimation(self)
            self._ornament.setStartValue(0.)
            self._ornament.setEndValue(1.)
            self._ornament.setDuration(4200 - 350 * style.rank)
            self._ornament.setLoopCount(-1)
            self._ornament.valueChanged.connect(self._ornament_frame)
        self.setProperty("backgroundEffect", True)
        self.setMinimumHeight(self.CLOSED_MIN_HEIGHT)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._caption = f"{name} · 已达成 · {style.label}"
        self._expanded = False
        self._pressed = False
        self._detail_progress = 0.
        self._detail_base_height = self.CLOSED_MIN_HEIGHT
        self._detail_animation = QVariantAnimation(self)
        self._detail_animation.setStartValue(0.)
        self._detail_animation.setEndValue(1.)
        self._detail_animation.setDuration(230)
        self._detail_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._detail_animation.valueChanged.connect(self._detail_frame)
        self._detail_animation.finished.connect(self._settle_details)
        box = QVBoxLayout(self)
        self._position_content()
        box.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        box.setSpacing(0)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.medal = _MedalIcon(style, group)
        row.addWidget(self.medal)
        words = QVBoxLayout()
        words.setSpacing(2)
        tags = QHBoxLayout()
        tags.setSpacing(6)
        tags.addWidget(RarityWordmark(style), 0, Qt.AlignmentFlag.AlignVCenter)
        tags.addWidget(_label(_group_label(group), pixels=12, color=self.DETAIL_INK), 1)
        words.addLayout(tags)
        fonts = load_presentation_fonts()
        self._preview_title = _label(name, pixels=20, family=font_family, bold=True)
        self._preview_title.setObjectName("achievementPreviewTitle")
        title_family = fonts["display"] if name.isascii() else fonts["chinese_tech"]
        detail_name = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[_-])", "\u200b", name) if name.isascii() else name
        self._detail_title = _label(detail_name, pixels=29, family=title_family, bold=True)
        self._detail_title.setAccessibleName(name)
        self._detail_title.setObjectName("achievementDetailTitle")
        self._detail_title.hide()
        words.addWidget(self._preview_title)
        words.addWidget(self._detail_title)
        row.addLayout(words, 1)
        box.addLayout(row)
        rule = str(entry.get("requirement") or "")
        ship_match = entry.get("id") == "ship_damage_114514"
        value = "、".join(entry.get("matched_ships") or ()) if ship_match else _measure(entry)
        self._summary = (rule, value)
        self._detail_pane = QWidget(self)
        detail_box = QVBoxLayout(self._detail_pane)
        detail_box.setContentsMargins(0, 15, 0, 4)
        detail_box.setSpacing(12)
        detail_fonts = chinese_sans_families()
        self._detail_facts = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self._detail_facts.setSpacing(12)
        if rule:
            condition = _label(rule, pixels=16, family=detail_fonts, color=self.DETAIL_INK)
            condition.setObjectName("achievementRequirement")
            self._detail_facts.addWidget(_detail_section("达成条件", condition, style.ink, detail_fonts), 3)
        if value:
            result = _label(value, pixels=23, family=(fonts["tech"], *detail_fonts), bold=True, color=style.ink)
            label = "达标战舰" if ship_match else "达成记录"
            result.setObjectName("achievementDetailedValue")
            result.setAccessibleName(label)
            self._detail_facts.addWidget(_detail_section(label, result, style.ink, detail_fonts), 2)
        if self._detail_facts.count():
            detail_box.addLayout(self._detail_facts)
        speech = str(entry.get("description") or "")
        speech_font = fonts["grade"] if speech.isascii() else fonts["chinese_display"]
        self.description = _label(speech, pixels=21, family=(speech_font, *detail_fonts), color="#161C25")
        self.description.setObjectName("achievementDescription")
        self.description.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        if self.description.text():
            detail_box.addWidget(_detail_section("成就致辞", self.description, style.ink, detail_fonts, panel_alpha=62))
        else:
            self.description.setParent(self._detail_pane)
            self.description.hide()
        self._detail_pane.hide()
        box.addWidget(self._detail_pane)
        self._update_accessibility()

    def _update_accessibility(self):
        self.setAccessibleName(f"{self._caption} · {'已展开' if self._expanded else '展开详情'}")
        rule, value = self._summary
        details = (rule, value, self.description.text()) if self._expanded else ("按回车键或空格键展开详情",)
        self.setAccessibleDescription("。".join(text for text in details if text))

    def set_expanded(self, expanded, *, animate=True):
        if self._expanded == expanded:
            return
        self._detail_animation.stop()
        self._expanded = expanded
        self._preview_title.setVisible(not expanded)
        self._detail_title.setVisible(expanded)
        self.layout().setAlignment(Qt.AlignmentFlag.AlignTop if expanded else Qt.AlignmentFlag.AlignVCenter)
        self._position_content()
        self.layout().invalidate()
        self._update_accessibility()
        if expanded:
            self._detail_base_height = max(self.CLOSED_MIN_HEIGHT, self.height())
            self._detail_pane.setVisible(bool(self._detail_pane.layout().count()))
            if animate and motion_enabled(self) and not self.window().isMinimized():
                self._detail_frame(0.)
                self._detail_animation.start()
            else:
                self._settle_details()
        else:
            self._detail_pane.hide()
            self._detail_progress = 0.
            self.setMinimumHeight(self.CLOSED_MIN_HEIGHT)
        self.update()

    def _detail_frame(self, progress):
        self._detail_progress = float(progress)
        if self._expanded:
            target_height = max(188, self._detail_base_height + 28)
            self.setMinimumHeight(round(self._detail_base_height + (target_height - self._detail_base_height)
                                        * self._detail_progress))
            margins = self.layout().contentsMargins()
            available = max(1, self.width() - margins.left() - margins.right())
            body_height = self._detail_pane.layout().totalHeightForWidth(available)
            if body_height <= 0:
                body_height = self._detail_pane.sizeHint().height()
            self._detail_pane.setMaximumHeight(round(body_height * self._detail_progress))
        self.update()

    def _settle_details(self):
        self._detail_progress = float(self._expanded)
        if self._expanded:
            self.setMinimumHeight(max(188, self._detail_base_height + 28))
            self._detail_pane.setMaximumHeight(16777215)  # Qt's default widget height limit.
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._expanded and event.oldSize().width() != event.size().width():
            self._position_content()
            if self._detail_progress < 1.:
                self._detail_frame(self._detail_progress)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            if self.rect().contains(event.position().toPoint()):
                self.expansionRequested.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        toggle = key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) or (
            self._expanded and key == Qt.Key.Key_Escape)
        if toggle:
            if not event.isAutoRepeat():
                self.expansionRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def _position_content(self):
        # Keep the total inset constant so the grid and the hit target stay still.
        lift = round((1 + self.rarity.rank) * self.hover_progress)
        if self._expanded:
            left, right = 28, max(42, round(expanded_art_width(self.width())))
            self.layout().setContentsMargins(left, 26, right, 26)
            available = self.width() - left - right
            direction = (QBoxLayout.Direction.LeftToRight if available >= 455 and self._detail_facts.count() == 2
                         else QBoxLayout.Direction.TopToBottom)
            if self._detail_facts.direction() != direction:
                self._detail_facts.setDirection(direction)
        else:
            self.layout().setContentsMargins(18, 13 - lift, 24, 17 + lift)

    def _frame(self, value):
        super()._frame(value)
        self.medal.progress = self.hover_progress
        self.medal.update()
        self._position_content()

    def _texture_frame(self, point):
        self._texture_pointer = QPointF(point)
        self.update()

    def _ornament_frame(self, phase):
        self._ornament_phase = float(phase)
        self.update()

    def _follow_pointer(self, point):
        if not self._background_enabled or not self.isVisible() or not motion_enabled(self) or self.window().isMinimized():
            return
        if self._texture_pointer is not None and point == self._texture_motion.endValue():
            return
        current = self._texture_pointer if self._texture_pointer is not None else QPointF(self.rect().center())
        self._texture_motion.stop()
        self._texture_motion.setStartValue(current)
        self._texture_motion.setEndValue(point)
        self._texture_motion.start()

    def _reset_texture(self):
        self._texture_motion.stop()
        self._texture_pointer = None
        if self._ornament is not None:
            self._ornament.stop()
        self._ornament_phase = 0.

    def enterEvent(self, event):
        super().enterEvent(event)
        if (self._ornament is not None and self._background_enabled and self.isVisible()
                and motion_enabled(self) and not self.window().isMinimized()):
            self._ornament.start()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        if self._ornament is not None:
            self._ornament.stop()
        if self.hover_progress > 0.:
            self._follow_pointer(QPointF(self.rect().center()))

    def event(self, event):
        # HoverMove also reaches this card when the pointer crosses its labels.
        if event.type() == QEvent.Type.HoverMove and isinstance(event, QHoverEvent):
            self._follow_pointer(self.mapFromGlobal(event.globalPosition()))
        return super().event(event)

    def _finish(self, *_):
        super()._finish()
        self._pressed = False
        self._detail_animation.stop()
        self._settle_details()
        self._reset_texture()
        self.medal.progress = 0.
        self.medal.update()
        self._position_content()

    def set_background_enabled(self, enabled):
        self._background_enabled = bool(enabled)
        if not self._background_enabled:
            self._reset_texture()
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        settings = getattr(self.window(), "client_preferences", {})
        self.set_background_enabled(settings.get("background_enabled", self._background_enabled))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        depth = self.hover_progress
        if self._expanded:
            surface = QRectF(self.rect())
            paint_expanded_achievement(painter, surface, self.rarity,
                                       progress=max(depth, .66 * self._detail_progress), phase=self._ornament_phase,
                                       background_enabled=self._background_enabled)
            painter.end()
            return
        rank = self.rarity.rank
        plate = QRectF(self.rect()).adjusted(3, 7, -8, -8)
        accent = QColor(self.accent)
        ink = QColor(self.rarity.ink)
        rear = panel_path(plate.translated(3, 3), self.cut)
        shadow = panel_path(plate.translated(4 + depth, 4 + .5 * rank * depth), self.cut)
        painter.fillPath(shadow, QColor(19, 43, 66, 29 + round((12 + 6 * rank) * depth)))
        backing = QLinearGradient(plate.topLeft(), plate.bottomRight())
        backing.setColorAt(0, ink)
        backing.setColorAt(.58, accent)
        backing.setColorAt(1, ink)
        painter.fillPath(rear, backing)

        face = plate.translated(0, -(1 + rank) * depth)
        if depth > 0 and rank >= 3:
            # A second metal sheet separates the elevated face from its mounting.
            sheet = panel_path(plate.translated(1.5, 2 - 2 * depth), self.cut)
            anodized = QColor(ink).darker(185)
            anodized.setAlpha(round(210 * depth))
            painter.fillPath(sheet, anodized)
        outline = panel_path(face, self.cut)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if depth > 0:
            for width, alpha in ((2 + rank, 18 + 7 * rank), (1.5 + .5 * rank, 45 + 15 * rank)):
                glow = QColor(accent)
                glow.setAlpha(round(alpha * depth))
                painter.setPen(QPen(glow, width))
                painter.drawPath(outline)
        paper = QLinearGradient(face.topLeft(), face.bottomRight())
        paper.setColorAt(0, QColor("#FFFFFF"))
        paper.setColorAt(.60, QColor("#F3F8FD"))
        paper.setColorAt(1, QColor("#E6EFF7"))
        painter.fillPath(outline, paper)
        wash = QLinearGradient(face.topRight(), face.center())
        tint = QColor(accent)
        tint.setAlpha(25 + 5 * rank + round((12 + 8 * rank) * depth))
        wash.setColorAt(0, tint)
        tint.setAlpha(0)
        wash.setColorAt(1, tint)
        painter.fillPath(outline, wash)
        if self._background_enabled:
            paint_achievement_texture(painter, face, outline, self.rarity, depth, self._texture_pointer,
                                      phase=self._ornament_phase)
        rim = QColor(ink)
        rim.setAlpha(130 + round(125 * depth))
        painter.setPen(QPen(rim, 1 + (.2 + .16 * rank) * depth))
        painter.drawPath(outline)

        # The shoulder carries the medal metal against a dark base even on white medals.
        right, top = face.right(), face.top()
        shoulder = QPolygonF([QPointF(right - 40, top + 4), QPointF(right - 17, top + 4),
                              QPointF(right - 4, top + 17), QPointF(right - 4, top + 24)])
        painter.setPen(QPen(ink, 3.5))
        painter.drawPolyline(shoulder)
        painter.setPen(QPen(accent, 1.5))
        painter.drawPolyline(shoulder)
        if self._background_enabled and depth > 0 and rank:
            lead = face.left() + (face.width() - self.cut) * depth
            sweep = QLinearGradient(lead - 20 - 12 * rank, top, lead + 12 + 3 * rank, top)
            light = accent.lighter(145)
            light.setAlphaF(depth)
            accent.setAlphaF(depth)
            sweep.setColorAt(0, tint)
            sweep.setColorAt(.60, accent)
            sweep.setColorAt(.78, light)
            sweep.setColorAt(1, tint)
            painter.setPen(QPen(sweep, 1.2 + .25 * rank))
            rail = QPolygonF([QPointF(face.left() + 8, top), QPointF(right - self.cut, top),
                              QPointF(right, top + self.cut), QPointF(right, top + 29)])
            painter.drawPolyline(rail)
            # Each additional trace stays in the exposed bottom/right bevel.
            # The gold and crimson grades resolve into a segmented terminal.
            if rank >= 2:
                bottom = face.bottom()
                for index in range(1 + int(rank == 5)):
                    x = right - 3 - 3 * index
                    y = bottom - 3 - 3 * index
                    reach = (17 + 10 * rank) * depth
                    elbow = QPointF(x - 5, y)
                    trace = QPolygonF([QPointF(x - 5 - reach, y), elbow, QPointF(x, y - 5),
                                       QPointF(x, y - 5 - reach * .38)])
                    tracer = QColor(ink)
                    tracer.setAlpha(round((95 + 14 * rank) * depth))
                    painter.setPen(QPen(tracer, 1))
                    painter.drawPolyline(trace)
                    if rank >= 4:
                        painter.setPen(QPen(tracer, 2.4))
                        for tick in range(rank - 1):
                            end = QPointF(elbow.x() - 6 - 6 * tick, y)
                            painter.drawLine(end - QPointF(3, 0), end)
                if rank >= 3:
                    painter.setPen(QPen(accent, 1))
                    painter.drawLine(QPointF(right - self.cut + 2, top + 7),
                                     QPointF(right - 5, top + self.cut))
        painter.end()


class AchievementGallery(QScrollArea):
    """Show earned medals and retain the category filter within the current account."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._expanded_id = None
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._account = None
        self._cards = []
        self._columns = 0
        self._detail = None
        content = QWidget()
        box = QVBoxLayout(content)
        box.setContentsMargins(7, 8, 9, 12)
        box.setSpacing(12)
        self.banner = _Banner()
        box.addWidget(self.banner)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.group = QComboBox()
        self.group.setAccessibleName("成就类别")
        self.group.currentIndexChanged.connect(self._select_group)
        toolbar.addWidget(self.group)
        toolbar.addStretch()
        box.addLayout(toolbar)
        self.empty = _label("当前账号暂无已达成成就", color=tone.MUTED)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setMinimumHeight(90)
        box.addWidget(self.empty)
        self.cards = QGridLayout()
        self.cards.setSpacing(9)
        box.addLayout(self.cards)
        box.addStretch()
        self.setWidget(content)

    def set_detail(self, detail, account):
        rows = sorted(earned_achievements(detail), key=lambda row: rarity_style(row["rarity"]).rank,
                      reverse=True)
        signature = (account, rows)
        if self._detail == signature:
            return
        self._detail = signature
        if account != self._account:
            self._expanded_id = None
            self.verticalScrollBar().setValue(0)
        with QSignalBlocker(self.group):
            previous = self.group.currentData() if account == self._account else None
            self.group.clear()
            self.group.addItem("全部类别", None)
            groups = {row.get("group") or "战绩" for row in rows}
            for group in sorted(groups, key=lambda value: _group_label(value)):
                self.group.addItem(_group_label(group), group)
            self.group.setVisible(len(groups) > 1)
            self.group.setCurrentIndex(max(0, self.group.findData(previous)))
        self._account = account
        for _, widget in self._cards:
            self.cards.removeWidget(widget)
            widget.hide()
            widget.deleteLater()
        fonts = load_presentation_fonts()
        self._cards = [(row, _MedalCard(row, fonts["chinese_body"])) for row in rows]
        for row, card in self._cards:
            card.expansionRequested.connect(lambda identifier=row["id"]: self._toggle_expansion(identifier))
            card._detail_animation.finished.connect(lambda selected=card: self._reveal_expanded(selected))
        self.banner.set_summary(len(rows))
        self._render()

    def _select_group(self, *_):
        self._render()
        self.verticalScrollBar().setValue(0)

    def _column_count(self):
        return min(4, max(1, (self.viewport().width() - 32) // 260))

    def _toggle_expansion(self, identifier):
        self._expanded_id = None if self._expanded_id == identifier else identifier
        selected = self._render()
        self._reveal_expanded(selected)

    def _reveal_expanded(self, card):
        if card is not None and card._expanded and card.isVisible():
            self.widget().layout().activate()
            self.ensureWidgetVisible(card, 12, 16)

    def _render(self, *_):
        group = self.group.currentData()
        columns = self._columns = self._column_count()
        visible = []
        expanded = None
        for index in range(4):
            self.cards.setColumnStretch(index, int(index < columns))
        for row, widget in self._cards:
            self.cards.removeWidget(widget)
            if group is not None and (row.get("group") or "战绩") != group:
                widget.set_expanded(False)
                widget.hide()
                continue
            if row["id"] == self._expanded_id:
                expanded = widget
            visible.append(widget)

        if expanded is not None:
            # Occupy the selected card's original row, then repack that row's neighbours underneath.
            row_start = visible.index(expanded) // columns * columns
            visible.remove(expanded)
            self.cards.addWidget(expanded, row_start // columns, 0, 1, columns)
        else:
            self._expanded_id = None
            row_start = len(visible)

        for index, widget in enumerate(visible):
            line, column = divmod(index, columns)
            self.cards.addWidget(widget, line + int(index >= row_start), column)
            widget.set_expanded(False)
            widget.show()
        if expanded is not None:
            expanded.show()
            expanded.set_expanded(True, animate=self.isVisible())
        self.empty.setVisible(not visible and expanded is None)
        return expanded

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_columns") and self._columns != self._column_count():
            self._render()


class ShipCollectionSummary(LayeredPanel):
    """Account-wide random-battle counts for the identified ship series."""

    def __init__(self, parent=None):
        super().__init__(parent, accent=tone.CYAN, cut=14)
        self._signature = None
        box = QVBoxLayout(self)
        box.setContentsMargins(18, 11, 28, 14)
        box.setSpacing(5)
        box.addWidget(_label("战舰系列出战 · 随机战", pixels=15, bold=True))
        self.cards = QHBoxLayout()
        self.cards.setSpacing(14)
        box.addLayout(self.cards)
        self.hide()

    def set_records(self, records):
        summary = tuple((row["name"], row.get("ships"), row.get("battles"), bool(row.get("complete")))
                        for row in records or ())
        if summary == self._signature:
            return
        self._signature = summary
        while self.cards.count():
            widget = self.cards.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        for name, ships, battles, complete in summary:
            prefix = "" if complete else "≥ "
            measures = [f"{prefix}{value:,} {unit}" for value, unit in ((ships, "艘"), (battles, "场"))
                        if value is not None]
            card = _label(f"{name}　" + (" · ".join(measures) or "资料不足"), color=tone.MUTED)
            self.cards.addWidget(card, 1)
        self.setVisible(bool(summary))
