"""Replay-sourced team columns and capture strip around the tactical map."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import academy_palette as tone
from .layered_panel import panel_path


UNKNOWN = '#8096A9'
NEUTRAL = '#DBE8F0'
CONTESTED = '#FFD484'
UNKNOWN_INK = '#597187'
CONTESTED_INK = '#88530D'


def _team_ink(color):
    """Adapt map signal colours to the pale HUD surfaces."""
    ink = QColor(color)
    if ink.green() > ink.red() * 1.4:
        return '#246C47'
    if ink.red() > ink.green() * 1.4:
        return '#A32D51'
    return UNKNOWN_INK


@dataclass(frozen=True)
class HudShip:
    kind: str
    name: str
    alive: bool | None
    entity: int


@dataclass(frozen=True)
class HudTeam:
    label: str
    color: str
    score: int | None
    ships: tuple[HudShip, ...]


@dataclass(frozen=True)
class HudCapture:
    name: str
    owner: int | None
    owner_color: str
    progress: float | None
    progress_color: str
    contested: bool | None
    invaders: bool | None
    enabled: bool | None

    def detail(self):
        if self.enabled is False:
            return '不可占领'
        if self.contested is True:
            return '争夺中' if self.progress is None else f'争夺 {self.progress:.0%}'
        if self.progress is not None and (self.progress > 0 or self.invaders is True):
            return f'{self.progress:.0%}'
        return '未观测' if self.owner is None else ''


def owner_text(owner):
    if owner == -1:
        return '中立'
    if owner in (0, 1):
        return f'队伍 {owner}'
    return '归属未观测'


class _HudSurface(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._hits: list[tuple[QRectF, str]] = []
        self.setMouseTracking(True)

    def _font(self, height, bold=False):
        font = QFont(self.font().family())
        font.setPixelSize(height)
        font.setBold(bold)
        return font

    def _text(self, painter, rect, text, ink=tone.MUTED, size=10, bold=False, align=Qt.AlignmentFlag.AlignCenter):
        painter.setFont(self._font(size, bold))
        painter.setPen(QColor(ink))
        painter.drawText(rect, align, text)

    def _frame(self, painter, rect, accent=tone.BLUE, *, right=False):
        face = rect.adjusted(0, 0, -2, -2)
        path = panel_path(face, 11)
        rim = QColor(accent)
        rim.setAlpha(88)
        painter.fillPath(panel_path(rect.adjusted(1, 1, 0, 0), 11), rim)

        surface = QLinearGradient(face.topLeft(), face.bottomRight())
        surface.setColorAt(0, QColor('#FCFDFF'))
        surface.setColorAt(.62, QColor('#F4EEF8' if right else '#ECF4FB'))
        surface.setColorAt(1, QColor('#E8EFF8' if right else '#E5F0FA'))
        painter.fillPath(path, surface)
        painter.setPen(QPen(QColor('#ABC2D6'), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        painter.save()
        painter.setClipPath(path)
        rail = face.left()+10
        painter.fillRect(QRectF(rail, face.top(), 28, 2.6), QColor(accent))
        painter.fillRect(QRectF(rail+32, face.top(), 8, 2.6), QColor(tone.PINK if accent == tone.BLUE else tone.BLUE))
        if face.height() >= 80:
            painter.setPen(QPen(QColor('#AEC6DE'), 1))
            x, y = face.right()-17, face.bottom()-9
            painter.drawLine(QPointF(x-21, y+4), QPointF(x, y+4))
            painter.drawLine(QPointF(x, y+4), QPointF(x+4, y))
            painter.fillRect(QRectF(x+6, y-1, 3, 3), rim)
        painter.restore()

    def mouseMoveEvent(self, event):
        self.setToolTip(next((text for rect, text in self._hits if rect.contains(event.position())), ''))
        super().mouseMoveEvent(event)


class ReplayTeamRail(_HudSurface):
    ship_selected = Signal(int)
    ship_hovered = Signal(object)

    def __init__(self, assets, *, right, parent=None):
        super().__init__(parent)
        self.assets = assets
        self.right = right
        self.team: HudTeam | None = None
        self.selected: int | None = None
        self._hovered: int | None = None
        self._keyboard_entity: int | None = None
        self._ships: list[tuple[QRectF, int]] = []
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.setMinimumWidth(88)
        self.setMaximumWidth(126)
        self.setObjectName('replayTeamRight' if right else 'replayTeamLeft')
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def sizeHint(self):
        return QSize(114, 340)

    def set_team(self, team):
        if self.team == team:
            return
        self.team = team
        if self._hovered is not None and (team is None or all(ship.entity != self._hovered for ship in team.ships)):
            self._set_hovered(None)
        if self._keyboard_entity is not None and (team is None or all(ship.entity != self._keyboard_entity for ship in team.ships)):
            self._keyboard_entity = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus if team and team.ships else Qt.FocusPolicy.NoFocus)
        self.setAccessibleName(f'{team.label}比分与舰船' if team else '')
        self.setAccessibleDescription(
            f'比分 {team.score if team.score is not None else "未知"}，' + '、'.join(ship.name for ship in team.ships)
            if team else ''
        )
        self.update()

    def set_selected(self, entity):
        if entity != self.selected:
            self.selected = entity
            self.update()

    def paintEvent(self, _event):
        self._hits.clear()
        self._ships.clear()
        if self.team is None:
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            area = QRectF(self.rect()).adjusted(1, 1, -1, -1)
            self._frame(painter, area, tone.PINK if self.right else tone.BLUE, right=self.right)
            self._paint_team(painter, area.adjusted(6, 5, -6, -5), self.team)
        finally:
            painter.end()

    def _paint_team(self, painter, rect, team):
        score = str(team.score) if team.score is not None else '—'
        color = _team_ink(team.color)
        self._text(painter, QRectF(rect.x(), rect.y(), rect.width(), 30),
                   score, color if team.score is not None else tone.INK, 24, True)
        self._text(painter, QRectF(rect.x(), rect.y()+30, rect.width(), 16), team.label, tone.INK, 12, True)
        alive = sum(ship.alive is True for ship in team.ships)
        dead = sum(ship.alive is False for ship in team.ships)
        unknown = len(team.ships) - alive - dead
        stats = f'存 {alive} · 沉 {dead}' + (f' · ? {unknown}' if unknown else '')
        if team.ships:
            self._text(painter, QRectF(rect.x(), rect.y()+47, rect.width(), 16), stats, size=10)
        detail = f'{team.label} · 分数 {score}'
        if team.ships:
            detail += f'\n已知存活 {alive}，确认沉没 {dead}，状态未知 {unknown}'
        self._hits.append((QRectF(rect.x(), rect.y(), rect.width(), 64), detail))

        if not team.ships:
            return
        divider_y = rect.y()+66
        painter.fillRect(QRectF(rect.x(), divider_y, rect.width(), 1), QColor('#B4CADB'))
        divider_x = rect.left() if self.right else rect.right()-22
        painter.fillRect(QRectF(divider_x, divider_y, 22, 2), QColor(color))
        pitch = min(30., (rect.height()-80) / len(team.ships))
        if pitch <= 2:
            return
        for index, ship in enumerate(team.ships):
            row = QRectF(rect.x(), rect.y()+72+index*pitch, rect.width(), pitch-1)
            if ship.entity == self.selected:
                row_path = panel_path(row, 5)
                painter.fillPath(row_path, QColor('#D5E8FB' if not self.right else '#EFDAEC'))
                painter.setPen(QPen(QColor(tone.BLUE if not self.right else '#BC5A86'), 1))
                painter.drawPath(row_path)
            elif ship.entity == self._hovered:
                painter.fillPath(panel_path(row, 5), QColor('#DFF1FC' if not self.right else '#F5E0F0'))
            elif index % 2 == 0:
                painter.fillRect(row, QColor('#E2EBF6' if not self.right else '#EBE5F2'))
            if self.hasFocus() and ship.entity == self._keyboard_entity:
                painter.setPen(QPen(QColor(tone.BLUE if not self.right else '#A32D51'), 1, Qt.PenStyle.DashLine))
                painter.drawPath(panel_path(row.adjusted(1, 1, -1, -1), 5))
            row_color = color if ship.alive is True else UNKNOWN_INK
            glyph = QRectF(row)
            glyph.setWidth(26)
            if not self.right:
                glyph.moveRight(row.right())
            label = row.adjusted(28, 0, -2, 0) if self.right else row.adjusted(2, 0, -28, 0)
            direction = Qt.AlignmentFlag.AlignLeft if self.right else Qt.AlignmentFlag.AlignRight
            self._paint_ship(painter, glyph, ship, row_color)
            painter.setFont(self._font(11))
            title = painter.fontMetrics().elidedText(ship.name, Qt.TextElideMode.ElideRight, int(label.width()))
            self._text(painter, label, title, tone.INK if ship.alive is True else tone.MUTED, 11, align=direction|Qt.AlignmentFlag.AlignVCenter)
            state = '已沉没' if ship.alive is False else '存活已知' if ship.alive is True else '状态未观测'
            self._hits.append((row, f'{ship.name} · {state}'))
            self._ships.append((row, ship.entity))

    def _paint_ship(self, painter, cell, ship, color):
        variant = 'dead' if ship.alive is False else 'last_visible' if ship.alive is None else ''
        asset = self.assets.ship(ship.kind, variant, color, 16)
        if asset is None:
            self._text(painter, cell, '•' if ship.alive is True else '×' if ship.alive is False else '?', color, 10)
            return
        logical = asset.deviceIndependentSize()
        scale = min(1., (cell.width()-2)/logical.height(), (cell.height()-2)/logical.width())
        if scale <= 0:
            return
        box = QRectF(0, 0, logical.width()*scale, logical.height()*scale)
        box.moveCenter(QPointF())
        painter.save()
        painter.translate(cell.center())
        painter.rotate(-90 if self.right else 90)
        painter.setOpacity(.67 if ship.alive is False else .78 if ship.alive is None else 1.)
        painter.drawImage(box, asset)
        painter.restore()

    def _set_hovered(self, entity):
        if self._hovered == entity:
            return
        self._hovered = entity
        self.setCursor(Qt.CursorShape.PointingHandCursor if entity is not None else Qt.CursorShape.ArrowCursor)
        self.ship_hovered.emit(entity)
        self.update()

    def mouseMoveEvent(self, event):
        self._set_hovered(next((entity for rect, entity in self._ships if rect.contains(event.position())), None))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._set_hovered(None)
        super().leaveEvent(event)

    def hideEvent(self, event):
        self._set_hovered(None)
        super().hideEvent(event)

    def focusInEvent(self, event):
        if self._keyboard_entity is None and self.team and self.team.ships:
            first = self.team.ships[0].entity
            self._keyboard_entity = next((ship.entity for ship in self.team.ships if ship.entity == self.selected), first)
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self.update()
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        ships = self.team.ships if self.team else ()
        key = event.key()
        directions = {Qt.Key.Key_Up: -1, Qt.Key.Key_Down: 1}
        if ships and key in (*directions, Qt.Key.Key_Home, Qt.Key.Key_End):
            index = next((index for index, ship in enumerate(ships) if ship.entity == self._keyboard_entity), 0)
            if key == Qt.Key.Key_Home:
                index = 0
            elif key == Qt.Key.Key_End:
                index = len(ships)-1
            else:
                index = min(max(0, index+directions[key]), len(ships)-1)
            self._keyboard_entity = ships[index].entity
            self.update()
            event.accept()
        elif self._keyboard_entity is not None and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.ship_selected.emit(self._keyboard_entity)
            event.accept()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            for rect, entity in self._ships:
                if rect.contains(event.position()):
                    self._keyboard_entity = entity
                    self.ship_selected.emit(entity)
                    self.update()
                    event.accept()
                    return
        super().mousePressEvent(event)


class ReplayMatchHud(_HudSurface):
    """Capture strip. Tracks the same replay frame as the two score columns."""
    def __init__(self, map_gutters, parent=None):
        super().__init__(parent)
        self.map_gutters = map_gutters
        self.teams: tuple[HudTeam, ...] = ()
        self.captures: tuple[HudCapture, ...] = ()
        self.target: int | None = None
        self.setObjectName('replayMatchHud')
        self.setAccessibleName('占领区')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(46)

    def set_frame(self, teams, captures, target):
        if (teams, captures, target) == (self.teams, self.captures, self.target):
            return
        self.teams, self.captures, self.target = teams, captures, target
        summary = [f'{cap.name} {owner_text(cap.owner)} {cap.detail()}'.strip() for cap in captures]
        self.setAccessibleDescription('  '.join(summary))
        self.update()

    def paintEvent(self, _event):
        self._hits.clear()
        # Same unequal left/right coordinate gutters as the map plot.
        left, right = self.map_gutters
        available = QRectF(self.rect()).adjusted(left, 1, -right, -1)
        if available.width() < 1:
            return
        width = min(available.width(), max(120., 53.*min(8, len(self.captures))+24))
        panel = QRectF(available.center().x()-width/2, available.top(), width, available.height())
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._frame(painter, panel)
            self._paint_captures(painter, panel.adjusted(6, 2, -6, -2))
        finally:
            painter.end()

    def _paint_captures(self, painter, area):
        if not self.captures:
            self._text(painter, area, '占领区未记录')
            return
        shown = self.captures[:8]
        pitch = area.width() / len(shown)
        for index, cap in enumerate(shown):
            cell = QRectF(area.left()+index*pitch, area.top(), pitch, area.height())
            badge = QRectF(0, 0, min(25., pitch-3), 22)
            badge.moveCenter(cell.center())
            ink = _team_ink(cap.owner_color) if cap.enabled is not False else UNKNOWN_INK
            path = panel_path(badge, 4)
            fill = QColor(ink)
            fill.setAlpha(30 if cap.owner is not None else 10)
            painter.fillPath(path, fill)
            contested = cap.contested is True and cap.enabled is not False
            base = QColor(CONTESTED_INK if contested else ink)
            base.setAlpha(137 if contested else 200)
            style = Qt.PenStyle.DashLine if contested or cap.owner is None else Qt.PenStyle.SolidLine
            painter.setPen(QPen(base, 1.15, style))
            painter.drawPath(path)
            self._text(painter, badge, cap.name, tone.INK, 12, True)
            if cap.enabled is not False and cap.progress is not None and cap.progress > 0:
                color = _team_ink(cap.progress_color)
                if color == UNKNOWN_INK:
                    color = CONTESTED_INK if contested else tone.BLUE
                painter.setPen(QPen(QColor(color), 2.1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                # Measure the stroke along the shape, starting at twelve o'clock like the map ring.
                offset = (badge.center().x()-path.pointAtPercent(0).x()) / path.length()
                painter.drawPath(path.trimmed(0., cap.progress, offset))
            detail = cap.detail()
            help_text = f'占领区 {cap.name} · {owner_text(cap.owner)}'
            if detail:
                help_text += f'\n{detail}'
            self._hits.append((cell, help_text))

