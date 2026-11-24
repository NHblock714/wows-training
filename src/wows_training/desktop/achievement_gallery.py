"""Public-account achievements presented as a filterable naval medal gallery."""
from __future__ import annotations

from html import escape
import math

from PySide6.QtCore import QPointF, QRectF, QSignalBlocker, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QSpacerItem, QVBoxLayout, QWidget,
)

from . import academy_palette as tone
from .chart_motion import ChartReveal
from .harbor_showcase import HarborShowcase
from .layered_panel import LayeredPanel, panel_path
from .presentation_fonts import load_presentation_fonts


GROUPS = {
    "record": ("单场纪录", "#E0AF65"),
    "mastery": ("舰船专精", "#55C9DF"),
    "career": ("生涯积累", "#81A9FB"),
    "fleet": ("舰队构成", "#A7A0E2"),
    "collection": ("联动与收藏", "#F591C4"),
    "battle": ("战斗勋章", "#E0AF65"),
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
        style += f'font-family:"{family}";'
    if bold:
        style += "font-weight:600;"
    label.setStyleSheet(style)
    return label


def _group_label(group):
    return GROUPS.get(group, (group, tone.BLUE))[0]


def _measure(entry):
    value = entry.get("value")
    if type(value) not in (int, float) or not math.isfinite(value):
        return ""
    unit = entry.get("unit") or ""
    shown = f"{value:,.2f}".rstrip("0").rstrip(".")
    if entry.get("is_lower_bound"):
        shown = "≥ " + shown
    return shown + unit


def _medal(painter, center, radius, color, *, earned=False, group=None):
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
        labels.addWidget(_label("成就展厅", pixels=26, family=fonts["chinese_tech"], color="#F1F8FF"))
        self.count = _label(pixels=20, family=fonts["chinese_body"], color="#A4E9F6")
        labels.addWidget(self.count)
        labels.addWidget(_label("随机战账号成就", color="#CFDEEB"))
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
        _medal(painter, center, 30, QColor("#E3B864" if self.earned else "#8BA3BC"), earned=bool(self.earned))
        painter.end()


class _MedalIcon(QWidget):
    def __init__(self, color, group):
        super().__init__()
        self.color = color
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
        painter.setPen(QPen(self.color, 1.5))
        for angle in (35, 215):
            painter.drawArc(mount.adjusted(3, 3, -3, -3), angle * 16, 76 * 16)
        _medal(painter, mount.center() - QPointF(0, 2), 17, self.color, earned=True, group=self.group)
        painter.end()


class _MedalCard(LayeredPanel):
    def __init__(self, entry, font_family):
        name = str(entry["name"])
        group = entry.get("group") or "战绩"
        color = GROUPS.get(group, ("", tone.BLUE))[1]
        super().__init__(accent=color, cut=15)
        self.setMinimumHeight(133)
        self.setAccessibleName(f"{name} · 已达成")
        box = QVBoxLayout(self)
        self._position_content()
        box.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(_MedalIcon(QColor(color), group))
        words = QVBoxLayout()
        words.setSpacing(0)
        group_ink = QColor(color).darker(240).name()
        words.addWidget(_label(_group_label(group), color=group_ink, bold=True))
        header = _label(name, pixels=19, family=font_family, bold=True)
        words.addWidget(header)
        row.addLayout(words, 1)
        box.addLayout(row)
        rule = str(entry.get("requirement") or "")
        if rule:
            box.addWidget(_label(rule, color=tone.MUTED))
        box.addStretch()
        value = _measure(entry)
        if value:
            box.addWidget(_label(value, pixels=17, family=font_family, bold=True))
        flavor = str(entry.get("description") or "")
        if flavor:
            self.setToolTip(f"<p>{escape(flavor)}</p>")
        self.setAccessibleDescription("。".join(t for t in (rule, value, flavor) if t))

    def _position_content(self):
        # Keep the total inset constant so the grid and the hit target stay still.
        lift = round(3 * self.hover_progress)
        self.layout().setContentsMargins(18, 13 - lift, 24, 17 + lift)

    def _frame(self, value):
        super()._frame(value)
        self._position_content()

    def _finish(self, *_):
        super()._finish()
        self._position_content()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        depth = self.hover_progress
        plate = QRectF(self.rect()).adjusted(2, 5, -8, -8)
        rear = panel_path(plate.translated(3, 3), self.cut)
        shadow = panel_path(plate.translated(4 + depth, 4 + 2 * depth), self.cut)
        painter.fillPath(shadow, QColor(19, 43, 66, 29 + round(29 * depth)))
        backing = QLinearGradient(plate.topLeft(), plate.bottomRight())
        backing.setColorAt(0, QColor("#72A9BC"))
        backing.setColorAt(.52, QColor("#C7D9E4"))
        backing.setColorAt(1, QColor("#C76B9D"))
        painter.fillPath(rear, backing)

        face = plate.translated(0, -3 * depth)
        outline = panel_path(face, self.cut)
        paper = QLinearGradient(face.topLeft(), face.bottomRight())
        paper.setColorAt(0, QColor("#FFFFFF"))
        paper.setColorAt(.60, QColor("#F3F8FD"))
        paper.setColorAt(1, QColor("#E6EFF7"))
        painter.fillPath(outline, paper)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#A9BFD2"), 1))
        painter.drawPath(outline)

        # Two offset rails meet at the clipped shoulder; only the rim catches the light.
        right, top = face.right(), face.top()
        shoulder = QPolygonF([QPointF(right - 53, top + 4), QPointF(right - 18, top + 4),
                              QPointF(right - 4, top + 18), QPointF(right - 4, top + 28)])
        painter.setPen(QPen(QColor("#BCCDDC"), 1))
        painter.drawPolyline(shoulder)
        painter.setPen(QPen(QColor(self.accent), 2))
        painter.drawLine(QPointF(face.left() + 10, top + 1), QPointF(face.left() + 34, top + 1))
        if depth > 0:
            lead = face.left() + face.width() * (.12 + .84 * depth)
            sweep = QLinearGradient(lead - 65, top, lead + 22, top)
            sweep.setColorAt(0, QColor(67, 184, 215, 0))
            sweep.setColorAt(.45, QColor(70, 170, 199, round(220 * depth)))
            sweep.setColorAt(.74, QColor(103, 222, 239, round(255 * depth)))
            sweep.setColorAt(.88, QColor(219, 106, 172, round(240 * depth)))
            sweep.setColorAt(1, QColor(219, 106, 172, 0))
            painter.setPen(QPen(sweep, 2))
            rim = QPolygonF([QPointF(face.left() + 8, top), QPointF(right - self.cut, top),
                             QPointF(right, top + self.cut), QPointF(right, top + 29)])
            painter.drawPolyline(rim)
        painter.end()


class AchievementGallery(QScrollArea):
    """Show earned medals and retain the category filter within the current account."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
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
        rows = detail.get("achievement_gallery")
        if rows is None:
            rows = [{"name": row[0], "requirement": row[1], "group": "战绩", "status": "earned"}
                    for row in detail.get("challenges", ()) if len(row) >= 2]
        rows = [row for row in rows if row.get("status") == "earned"]
        signature = (account, rows)
        if self._detail == signature:
            return
        self._detail = signature
        if account != self._account:
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
        self.banner.set_summary(len(rows))
        self._render()

    def _select_group(self, *_):
        self._render()
        self.verticalScrollBar().setValue(0)

    def _column_count(self):
        return min(4, max(1, (self.viewport().width() - 32) // 260))

    def _render(self, *_):
        group = self.group.currentData()
        columns = self._columns = self._column_count()
        visible = 0
        for index in range(4):
            self.cards.setColumnStretch(index, int(index < columns))
        for row, widget in self._cards:
            self.cards.removeWidget(widget)
            include = (row.get("group") or "战绩") == group or group is None
            if include:
                line, column = divmod(visible, columns)
                self.cards.addWidget(widget, line, column)
                visible += 1
            widget.setVisible(include)
        self.empty.setVisible(visible == 0)

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
        box.addWidget(_label("舰船系列出战 · 随机战", pixels=15, bold=True))
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
