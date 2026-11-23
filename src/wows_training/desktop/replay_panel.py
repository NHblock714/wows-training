"""Offline replay map and packet-clock controls over verified P0 reports."""
from __future__ import annotations

from bisect import bisect_right
import hashlib
import math
from pathlib import Path
import re
import time

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, QSignalBlocker, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QImageReader, QPainter, QPainterPath, QPen, QPolygonF, QLinearGradient, QRadialGradient, QBrush
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QProgressBar, QPushButton, QScrollArea, QSlider, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from wows_training.timebase import packet_to_battle_time
from wows_training.replay_battle_state import ReplayBattleState
from wows_training.replay_grid import GRID_ROWS
from wows_training.map_terrain import terrain_available
from .combat_layers import CombatLegend, draw_ship_symbol, health_fraction, quality_allowed, ship_kind, spotting_indicator
from .minimap_assets import MinimapAssets,default_assets,RELATION_COLORS,AMMO_COLORS
from . import academy_palette as tone
from .layered_panel import LayeredPanel, paint_layered_panel, panel_path
from .motion import motion_enabled, subscribe_motion
from .replay_match_hud import CONTESTED, NEUTRAL, UNKNOWN, HudCapture, HudShip, HudTeam, ReplayMatchHud, ReplayTeamRail, owner_text


def _archive_empty(painter, rect, caption, detail='', *, compact=False):
    """A static archive drawing, confined to an empty surface's own paint event."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    rect=QRectF(rect)
    surface=QLinearGradient(rect.topLeft(),rect.bottomRight())
    surface.setColorAt(0,QColor('#F7FBFF'));surface.setColorAt(1,QColor('#E9F0F8'))
    painter.fillRect(rect,surface)
    painter.setPen(QPen(QColor(89,141,190,17),1))
    for x in range(0,int(rect.width()),32):painter.drawLine(QPointF(x,0),QPointF(x,rect.height()))
    for y in range(0,int(rect.height()),32):painter.drawLine(QPointF(0,y),QPointF(rect.width(),y))
    diagonal=QPolygonF([QPointF(rect.right()-rect.width()*.45,rect.top()),rect.topRight(),
                        rect.bottomRight(),QPointF(rect.right()-rect.width()*.78,rect.bottom())])
    painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(QColor(240,87,166,10));painter.drawPolygon(diagonal)
    width=min(364.,rect.width()-28);height=176. if compact else 226.
    if rect.height()<height+20:height=max(80.,rect.height()-20)
    card=QRectF(rect.center().x()-width/2,rect.center().y()-height/2,width,height)
    # Offset file leaves occupy the whitespace, with no extra placeholder rows.
    painter.setPen(QPen(QColor('#B9CBDF'),1));painter.setBrush(QColor(255,255,255,105))
    painter.drawPath(panel_path(card.translated(-9,-9),14))
    paint_layered_panel(painter,card,accent=tone.BLUE,cut=18)
    center=QPointF(card.center().x()-2,card.top()+(54 if compact else 70))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for radius,alpha in ((26,140),(35,60)):
        painter.setPen(QPen(QColor(37,112,181,alpha),1.2));painter.drawEllipse(center,radius,radius)
    painter.setPen(QPen(QColor(tone.BLUE),1.7))
    painter.drawLine(center+QPointF(-11,-14),center+QPointF(11,-14))
    painter.drawLine(center+QPointF(-11,-14),center+QPointF(-11,13))
    painter.drawLine(center+QPointF(-11,13),center+QPointF(11,13))
    painter.drawLine(center+QPointF(11,-14),center+QPointF(11,13))
    for y in (-6,0,6):painter.drawLine(center+QPointF(-5,y),center+QPointF(6,y))
    painter.setPen(QPen(QColor(tone.PINK),2));painter.drawLine(center+QPointF(24,-22),center+QPointF(29,-17))
    from .presentation_fonts import load_presentation_fonts
    font=QFont(load_presentation_fonts()['chinese_tech']);font.setPixelSize(18 if compact else 24)
    painter.setFont(font);painter.setPen(QColor(tone.INK))
    title=QRectF(card.left()+24,center.y()+43,card.width()-55,29)
    painter.drawText(title,Qt.AlignmentFlag.AlignCenter,caption)
    if detail:
        font=QFont('Microsoft YaHei UI');font.setPixelSize(12);painter.setFont(font)
        painter.setPen(QColor(tone.MUTED))
        painter.drawText(QRectF(card.left()+25,title.bottom()+7,card.width()-58,48),
                         Qt.AlignmentFlag.AlignHCenter|Qt.AlignmentFlag.AlignTop|Qt.TextFlag.TextWordWrap,detail)
    painter.restore()


class _ReplayList(QListWidget):
    def __init__(self,caption,parent=None):
        super().__init__(parent);self.empty_caption=caption

    def paintEvent(self,event):
        super().paintEvent(event)
        if not self.count():
            painter=QPainter(self.viewport())
            _archive_empty(painter,self.viewport().rect(),self.empty_caption,compact=True)
            painter.end()


SUPPORTED_VERSION = (15, 9, 0, 13357625)
AXIS = 'SpaceSizeUtils.native_max_min_return'
MAP_HORIZONTAL_GUTTERS = (56, 22)
LAYER_DEFAULTS = {
    'heading': True, 'health': True, 'shells': True, 'torpedoes': True,
    'smokes': True, 'consumables': True, 'ranges': False, 'last_known': True,
    'estimates': False, 'capture_points': True, 'battle_hud': True, 'terrain': False,
    'ship_names': True, 'player_names': False, 'wrecks': True, 'grid': True, 'planes': True,
}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _clock_text(value, decimal=False):
    if not _finite(value):
        return '—'
    value = max(0.0, float(value))
    minutes, seconds = divmod(value, 60)
    return f'{int(minutes):02d}:{seconds:04.1f}' if decimal else f'{int(minutes):02d}:{int(seconds):02d}'


def _position_at(segments, clock):
    """Use a recorded point within one observed segment, never bridge a gap."""
    matches = []
    for segment in segments:
        if segment and segment[0][0] <= clock <= segment[-1][0]:
            index = bisect_right(segment, clock, key=lambda point: point[0]) - 1
            if index >= 0:
                matches.append(segment[index])
    return matches[0] if len(matches) == 1 else None


def _last_position(segments, clock):
    candidates = []
    for segment in segments:
        index = bisect_right(segment, clock, key=lambda point: point[0]) - 1
        if index >= 0:
            candidates.append(segment[index])
    return max(candidates, key=lambda point: point[0]) if candidates else None


def _load_segments(record):
    columns = record.get('columns', [])
    required = ('packet_clock', 'x_engine', 'z_engine', 'png_x', 'png_y')
    if not isinstance(columns, list) or any(columns.count(name) != 1 for name in required):
        return []
    indices = [columns.index(name) for name in required]
    result = []
    for source in record.get('segments', []):
        current = []
        if not isinstance(source, list):
            continue
        for row in source:
            if not isinstance(row, (list, tuple)) or len(row) <= max(indices):
                if current:
                    result.append(current)
                current = []
                continue
            point = tuple(row[index] for index in indices)
            if not all(_finite(value) for value in point) or point[0] < 0:
                if current:
                    result.append(current)
                current = []
                continue
            if current and point[0] < current[-1][0]:
                result.append(current)
                current = []
            if current and point[0] == current[-1][0]:
                current[-1] = point
            else:
                current.append(point)
        if current:
            result.append(current)
    return result


def _capture_badge(point, state, team_color):
    owner = state.get('owner_team')
    capturing = state.get('capturing_team')
    progress = state.get('capture_progress')
    return HudCapture(
        str(point.get('name') or '?')[:12],
        owner,
        team_color(owner) if owner in (0, 1) else NEUTRAL if owner == -1 else UNKNOWN,
        progress if _finite(progress) and 0 <= progress <= 1 else None,
        team_color(capturing) if capturing in (0, 1) else NEUTRAL,
        state.get('contested'),
        state.get('has_invaders'),
        state.get('capture_enabled'),
    )


def _read_verified_image(path, expected_hash):
    path_string = str(path)
    if path_string.startswith(('\\\\', '//')) or '://' in path_string:
        raise ValueError('底图必须是本地文件')
    source = Path(path_string)
    if not source.is_absolute() or not source.is_file() or source.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('本地底图不存在或大小不受支持')
    if not isinstance(expected_hash, str) or hashlib.sha256(source.read_bytes()).hexdigest() != expected_hash:
        raise ValueError('底图已变化，需要重新导入分析')
    reader = QImageReader(str(source))
    size = reader.size()
    if size.width() <= 0 or size.height() <= 0 or max(size.width(), size.height()) > 8192:
        raise ValueError('底图尺寸不受支持')
    image = reader.read()
    if image.isNull():
        raise ValueError('无法读取底图')
    return image


class _MapCanvas(QWidget):
    entity_selected = Signal(int)

    def __init__(self, parent=None, assets=None, *, evidence_frame=False):
        super().__init__(parent)
        self.evidence_frame = evidence_frame
        self.setObjectName('replayMap')
        self.setProperty('tacticalSurface',True)
        self.setMinimumSize(300, 280)
        self.setMouseTracking(True)
        self.clock = 0.0
        self.selected = None
        self.trails = False
        self.layers = dict(LAYER_DEFAULTS)
        self.assets=assets or default_assets();self.symbol_scale=1.
        self._hover_motion = QVariantAnimation(self)
        self._hover_motion.setStartValue(0.)
        self._hover_motion.setEndValue(1.)
        self._hover_motion.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._hover_motion.valueChanged.connect(self._step_hover)
        self.clear()

    def clear(self, reason='未载入回放'):
        self._reset_hover()
        self.land = self.water = None
        self.terrain = None
        self._terrain_report = None
        self.relative_grid = False
        self.full_art = False
        self.wire_headings={}
        self.projection = None
        self.tracks = {}
        self.players = {}
        self.caps = []
        self.battle_state = ReplayBattleState({})
        self._cap_hits = []
        self.deaths = {}
        self.scale = None
        self.map_display_name = ''
        self.selected = None
        self.clock = 0.0
        self.reason = reason
        self._hits = []
        self.intel = None
        self.host_team=None;self.own_entity=None;self._label_rects=[]
        self._state_cache = {}
        self._state_clock = None
        self.update()

    def preview_ship(self, entity):
        if entity == self._hover_target:
            return
        self._hover_motion.stop()
        self._hover_target = entity
        self._hover_start = dict(self._hover_levels)
        if entity is not None:
            self._hover_start.setdefault(entity, 0.)
        if not motion_enabled(self):
            self._step_hover(1.)
        else:
            self._hover_motion.setDuration(250 if entity is not None else 200)
            self._hover_motion.start()

    def _step_hover(self, value):
        self._hover_levels = {}
        for entity, start in self._hover_start.items():
            level = start + (float(entity == self._hover_target) - start) * value
            if level > .001:
                self._hover_levels[entity] = level
        self.update()

    def _reset_hover(self):
        self._hover_motion.stop()
        self._hover_target = None
        self._hover_start = {}
        self._hover_levels = {}
        self.update()

    def _hover_motion_preference(self, enabled):
        if not enabled:
            self._hover_motion.stop()
            self._step_hover(1.)

    def showEvent(self, event):
        subscribe_motion(self, self._hover_motion_preference)
        super().showEvent(event)

    def hideEvent(self, event):
        self._reset_hover()
        super().hideEvent(event)

    def configure(self, report, players, supported, intel=None):
        self.clear()
        self.players = players
        self.intel = intel
        self.battle_state = ReplayBattleState(report)
        if not report.get('multiview'):
            host=report.get('host_absolute_team')
            self.host_team=host if type(host) is int and host in (0,1) else None
            own=[entity for entity,p in players.items() if p.get('name')==(report.get('metadata') or {}).get('playerName') and p.get('shipId',0)>0]
            self.own_entity=own[0] if len(own)==1 else None
        analysis = report.get('geometry_analysis') or {}
        projection = analysis.get('png_projection') or {}
        if not supported:
            if report.get('replay_version') == '15.10.0.13533378' and self._configure_relative(report):
                return
            self.reason = '该版本尚未验证，地图投影不可用'
            return
        if (analysis.get('png_alignment_verified') is not True or analysis.get('errors')
                or projection.get('status') != 'source_chain_verified'
                or projection.get('mapping_scope') != 'single_identity_mapping'):
            self.reason = '地图投影证据不足\n仍可查看时间与事件记录'
            return
        try:
            viewport = projection['viewport_bounds_engine']
            texture = projection['texture_bounds_engine']
            if viewport.get('projection_convention') != AXIS or viewport.get('native_viewport_equality_verified') is not True:
                raise ValueError('旧版或未经验证的地图轴向')
            for bounds in (viewport, texture):
                if bounds.get('unit') != 'engine' or not all(_finite(bounds.get(key)) for key in ('min_x', 'max_x', 'min_z', 'max_z')):
                    raise ValueError('地图坐标不完整')
                if bounds['max_x'] <= bounds['min_x'] or bounds['max_z'] <= bounds['min_z']:
                    raise ValueError('地图范围无效')
            crop = projection['viewport_crop_pixels']
            if crop.get('coordinate_convention') != 'continuous_pixel_edges' or not all(_finite(crop.get(key)) for key in ('left', 'top', 'right', 'bottom')):
                raise ValueError('底图裁切证据不完整')
            paths, hashes = projection['source_paths'], projection['source_hashes']
            land = _read_verified_image(paths['map_minimap'], hashes['map_minimap'])
            water = _read_verified_image(paths['map_water'], hashes['map_water'])
            if list(projection['dimensions_pixels']) != [land.width(), land.height()]:
                raise ValueError('底图尺寸与分析记录不一致')
            if not (0 <= crop['left'] < crop['right'] <= land.width() and 0 <= crop['top'] < crop['bottom'] <= land.height()):
                raise ValueError('底图裁切范围无效')
            if 'map_settings' in paths:
                settings_path = str(paths['map_settings'])
                if settings_path.startswith(('\\\\', '//')) or '://' in settings_path:
                    raise ValueError('地图设置必须是本地文件')
                if hashlib.sha256(Path(settings_path).read_bytes()).hexdigest() != hashes.get('map_settings'):
                    raise ValueError('地图设置已变化，需要重新导入')
            self.land, self.water, self.projection = land, water, projection
            self._terrain_report = report if terrain_available(report) else None
            self.map_display_name = str(report.get('display_map_name') or projection['map_id'])
            scale = analysis.get('meters_per_engine_unit')
            self.scale = float(scale) if _finite(scale) and scale > 0 and analysis.get('distance_domain') == 'client_displayed_world_distance' else None
            for track in analysis.get('projected_tracks', []):
                entity = track.get('entity_id')
                if type(entity) is int:
                    self.tracks.setdefault(entity, []).extend(_load_segments(track))
            self.caps = report.get('capture_points') or []
            for event in report.get('death_events') or []:
                if type(event.get('victim_entity_id')) is int and _finite(event.get('clock')):
                    entity = event['victim_entity_id']
                    self.deaths[entity] = min(event['clock'], self.deaths.get(entity, event['clock']))
            self.reason = ''
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.clear(f'地图暂不可用\n{exc}')
            self.players = players
            self.intel = intel

    def _configure_relative(self, report):
        from ..ordinary_replay import verified_minimap_heading

        tracks = (report.get('ordinary') or {}).get('minimap_raw_tracks') or {}
        for key, segments in tracks.items():
            if not str(key).isdigit() or int(key) not in self.players:
                continue
            rows = [[[p[0], p[1], p[2], p[1], 2047-p[2]] for p in segment] for segment in segments]
            self.tracks[int(key)] = _load_segments({'columns':['packet_clock','x_engine','z_engine','png_x','png_y'],'segments':rows})
            headings = {point[0]: heading for segment in segments for point in segment
                        if len(point) > 3 and _finite(point[0])
                        and (heading := verified_minimap_heading(report, point[3])) is not None}
            if headings:
                self.wire_headings[int(key)] = headings
        points = [point for segments in self.tracks.values() for segment in segments for point in segment]
        if not points:
            return False
        x0,x1 = min(p[1] for p in points), max(p[1] for p in points)
        z0,z1 = min(p[2] for p in points), max(p[2] for p in points)
        pad = max(12, (x1-x0)*.035, (z1-z0)*.035)
        bounds = {'min_x':x0-pad,'max_x':x1+pad,'min_z':z0-pad,'max_z':z1+pad}
        crop = {'left':x0-pad,'right':x1+pad,'top':2047-z1-pad,'bottom':2047-z0+pad}
        self.projection = {'viewport_bounds_engine':bounds,'viewport_crop_pixels':crop}
        self.relative_grid = True
        observed = report['ordinary']
        if observed.get('map_identity') and observed.get('world_pairs',0)>=10 and _finite(observed.get('world_rmse')) and observed['world_rmse']<=5:
            from ..ordinary_replay import extra_map_record
            name=str((report.get('metadata') or {}).get('mapName','')).rsplit('/',1)[-1]
            rec=extra_map_record(name,report['replay_version'])
            if rec:
                try:
                    land=_read_verified_image(rec['source_paths']['map_minimap'],rec['source_hashes']['map_minimap'])
                    water=_read_verified_image(rec['source_paths']['map_water'],rec['source_hashes']['map_water'])
                except (ValueError,OSError): pass
                else:
                    b=rec['original_bounds_engine'];self.water=water;self.land=land;self.full_art=True
                    convert=lambda n:(n+2500)*2047/5000
                    # Preserve wire coordinates in track samples; map their full-art rectangle into the same frame.
                    self.projection['viewport_crop_pixels']={'left':convert(b['min_x']),'right':convert(b['max_x']),
                        'top':2047-convert(b['max_z']),'bottom':2047-convert(b['min_z'])}
                    self.projection['viewport_bounds_engine']=b
                    self._terrain_report = report if terrain_available(report) else None
                    self.caps = report.get('capture_points') or []
        self.map_display_name = report.get('display_map_name') or '本局'
        for event in report.get('death_events') or []:
            key = event.get('victim_entity_id')
            if key in self.players and _finite(event.get('clock')):
                self.deaths[key] = min(event['clock'],self.deaths.get(key,event['clock']))
        self.reason = ''
        return True

    def load_terrain(self):
        if self.terrain is None and self._terrain_report is not None:
            from .replay_terrain import terrain_overlay
            self.terrain = terrain_overlay(self._terrain_report, self.projection, self.land, full_art=self.full_art)
            self._terrain_report = None

    def _draw_capture_points(self, painter, canvas, bounds):
        self._cap_hits = []
        if not self.layers['capture_points']:
            return
        for cap in self.caps:
            if not all(_finite(cap.get(key)) for key in ('x', 'z', 'radius')) or cap['radius'] < 0:
                continue
            state = self.battle_state.capture_at(cap, self.clock)
            badge = _capture_badge(cap, state, lambda team: self.color(team=team).name())
            # Keep pre-existing neutral colors in archived minimap frame exports.
            color = QColor(badge.owner_color if badge.owner in (0, 1) else '#E8E8DC')
            if badge.enabled is False:
                color = QColor(UNKNOWN)
            fill = QColor(color)
            fill.setAlpha(28)
            painter.setBrush(fill)
            painter.setPen(QPen(color, 1.4, Qt.PenStyle.DashLine))
            center = self._world_point(cap['x'], cap['z'], canvas)
            rx = cap['radius'] / (bounds['max_x'] - bounds['min_x']) * canvas.width()
            ry = cap['radius'] / (bounds['max_z'] - bounds['min_z']) * canvas.height()
            painter.drawEllipse(center, rx, ry)
            painter.drawText(QRectF(center.x() - 18, center.y() - 12, 36, 24), Qt.AlignmentFlag.AlignCenter, badge.name)
            if badge.enabled is not False and badge.progress is not None and badge.progress > 0:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(badge.progress_color), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawArc(QRectF(center.x()-rx, center.y()-ry, rx*2, ry*2), 90*16, -round(360*16*badge.progress))
            detail = badge.detail()
            active = detail and (badge.enabled is False or badge.contested is True or badge.invaders is True or bool(badge.progress))
            if active:
                width = max(34, painter.fontMetrics().horizontalAdvance(detail) + 10)
                label_rect = QRectF(center.x()-width/2, center.y()+12, width, 17)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor('#11263A'))
                painter.drawRoundedRect(label_rect, 3, 3)
                status_color = UNKNOWN if badge.enabled is False else CONTESTED if badge.contested is True else badge.progress_color
                painter.setPen(QColor(status_color))
                painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, detail)
            self._cap_hits.append((QRectF(center.x()-22, center.y()-15, 44, 45),
                                   f'占领区 {badge.name} · {owner_text(badge.owner)}\n{detail}'.strip()))

    def ship_state(self, entity):
        if self._state_clock != self.clock:
            self._state_cache, self._state_clock = {}, self.clock
        if entity not in self._state_cache:
            state = self.intel.ship_state(entity, self.clock) if self.intel is not None else {}
            self._state_cache[entity] = state if isinstance(state, dict) else {}
        return self._state_cache[entity]

    def display_positions(self):
        displayed = {entity: (point, False) for entity, point in self.observed_positions().items()}
        if self.layers['last_known']:
            for entity, segments in self.tracks.items():
                if entity in displayed or (entity in self.deaths and self.clock >= self.deaths[entity]):
                    continue
                point = _last_position(segments, self.clock)
                if point is not None:
                    displayed[entity] = (point, True)
        if self.layers['wrecks']:
            for entity,death in self.deaths.items():
                if death<=self.clock:
                    point=_last_position(self.tracks.get(entity,[]),death)
                    if point is not None:displayed[entity]=(point,True)
        return displayed

    def relation(self,entity=None,team=None):
        if entity is not None and entity==self.own_entity:return 'own'
        if team is None:team=self.players.get(entity,{}).get('teamId')
        if type(team) is not int or team not in (0,1):return 'unknown'
        return 'ally' if team==(self.host_team if self.host_team is not None else 0) else 'enemy'

    def color(self,entity=None,team=None):return QColor(RELATION_COLORS[self.relation(entity,team)])

    def _text(self,painter,rect,text,color):
        path=QPainterPath();metrics=painter.fontMetrics();path.addText(QPointF(rect.left(),rect.top()+metrics.ascent()),painter.font(),text)
        painter.setPen(QPen(QColor(0,0,0,225),3,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(QColor(color));painter.drawPath(path);painter.setPen(Qt.PenStyle.NoPen);painter.drawPath(path)

    def _label(self,painter,position,lines,color,canvas,occupied):
        if not lines:return
        metrics=painter.fontMetrics();maximum=max(16,min(canvas.width()-12,max(140,canvas.width()*.42)))
        lines=[metrics.elidedText(str(text),Qt.TextElideMode.ElideMiddle,math.floor(maximum-4)) for text in lines]
        height=len(lines)*metrics.height();width=max(metrics.horizontalAdvance(t) for t in lines)+4
        candidates=[QRectF(position.x()+dx,position.y()+dy,width,height) for dx,dy in
                    ((16,-height-7),(-width/2,-height-18),(16,22),(-width-16,-height-7),(-width-16,22),(-width/2,34))]
        inside=canvas.adjusted(4,15,-4,-4)
        for rect in candidates:
            rect.moveLeft(min(max(inside.left(),rect.left()),max(inside.left(),inside.right()-width)))
            rect.moveTop(min(max(inside.top(),rect.top()),max(inside.top(),inside.bottom()-height)))
        rect=min(enumerate(candidates),key=lambda item:(sum(item[1].intersected(r).width()*item[1].intersected(r).height() for r in occupied),item[0]))[1]
        occupied.append(rect.adjusted(-2,-2,2,2));self._label_rects.append(rect)
        if abs(rect.center().x()-position.x())>width/2+22 or rect.top()>position.y()+30:
            painter.setPen(QPen(QColor(color),.65));painter.drawLine(position,rect.center())
        for index,text in enumerate(lines):self._text(painter,QRectF(rect.left()+2,rect.top()+index*metrics.height(),width,metrics.height()),text,color)

    def _consumable_badges(self,painter,position,entries,scale):
        count=min(len(entries),4);size=24*scale;gap=3*scale
        for index,entry in enumerate(entries[:count]):
            rect=QRectF(position.x()-(size*count+gap*(count-1))/2+index*(size+gap),position.y()+23*scale,size,size)
            painter.setPen(QPen(QColor('#5D8B78'),.7));painter.setBrush(QColor(7,22,28,230));painter.drawRoundedRect(rect,3,3)
            image=self.assets.consumable(entry.get('type'),size)
            if image is not None:painter.drawImage(rect.adjusted(1,1,-1,-1),image)
            else:self._text(painter,rect.adjusted(6,2,0,0),'?',QColor('#D4DDE5'))
            remaining=entry.get('remaining_seconds');text=str(max(0,math.ceil(remaining))) if _finite(remaining) else '?'
            badge=QRectF(rect.right()-max(14,painter.fontMetrics().horizontalAdvance(text)+3),rect.bottom()-11, max(14,painter.fontMetrics().horizontalAdvance(text)+3),13)
            painter.fillRect(badge,QColor(0,0,0,235));painter.setPen(QColor('#FFE291'));painter.drawText(badge,Qt.AlignmentFlag.AlignCenter,text)
        return QRectF(position.x()-(size*count+gap*(count-1))/2,position.y()+23*scale,size*count+gap*(count-1),size) if count else None

    def observed_positions(self):
        return {entity: point for entity, point in self.visible_positions().items()
                if self.ship_state(entity).get('visibility') != 'not_visible'}

    def visible_positions(self):
        result = {}
        for entity, segments in self.tracks.items():
            if entity in self.deaths and self.clock >= self.deaths[entity]:
                continue
            position = _position_at(segments, self.clock)
            if position is not None:
                result[entity] = position
        return result

    def _canvas_rect(self):
        left, right = MAP_HORIZONTAL_GUTTERS
        available = QRectF(left, 30, max(1, self.width() - left - right), max(1, self.height() - 76))
        if self.projection is None:
            return available
        crop = self.projection['viewport_crop_pixels']
        aspect = (crop['right'] - crop['left']) / (crop['bottom'] - crop['top'])
        width = min(available.width(), available.height() * aspect)
        height = width / aspect
        return QRectF(available.center().x() - width / 2, available.center().y() - height / 2, width, height)

    def _pixel_point(self, px, py, canvas):
        crop = self.projection['viewport_crop_pixels']
        return QPointF(canvas.left() + (px - crop['left']) / (crop['right'] - crop['left']) * canvas.width(),
                       canvas.top() + (py - crop['top']) / (crop['bottom'] - crop['top']) * canvas.height())

    def _world_point(self, x, z, canvas):
        bounds = self.projection['viewport_bounds_engine']
        return QPointF(canvas.left() + (x - bounds['min_x']) / (bounds['max_x'] - bounds['min_x']) * canvas.width(),
                       canvas.top() + (bounds['max_z'] - z) / (bounds['max_z'] - bounds['min_z']) * canvas.height())

    def _range_ellipse(self, painter, x, z, radius, canvas, color, *, estimated=False, filled=False):
        if not all(_finite(value) for value in (x, z, radius)) or radius <= 0:
            return
        bounds = self.projection['viewport_bounds_engine']
        center = self._world_point(x, z, canvas)
        rx = radius / (bounds['max_x'] - bounds['min_x']) * canvas.width()
        ry = radius / (bounds['max_z'] - bounds['min_z']) * canvas.height()
        pen = QPen(QColor(color), 1.2, Qt.PenStyle.DashLine if estimated else Qt.PenStyle.SolidLine)
        painter.setPen(pen)
        fill = QColor(color)
        fill.setAlpha(26 if filled else 10)
        painter.setBrush(fill)
        painter.drawEllipse(center, rx, ry)

    def _draw_combat(self, painter, canvas):
        if self.intel is None or (self.relative_grid and not self.full_art):
            return
        estimates = self.layers['estimates']
        if self.layers['smokes']:
            for smoke in self.intel.smokes(self.clock):
                if quality_allowed(smoke.get('quality'), estimates):
                    if _finite(smoke.get('radius_engine')) and smoke['radius_engine'] > 0:
                        self._range_ellipse(painter, smoke.get('x_engine'), smoke.get('z_engine'), smoke.get('radius_engine'),
                                            canvas, '#b6c6d2', estimated=smoke.get('quality') == 'estimated', filled=True)
                    elif all(_finite(smoke.get(key)) for key in ('x_engine', 'z_engine')):
                        point = self._world_point(smoke['x_engine'], smoke['z_engine'], canvas)
                        painter.setPen(QPen(QColor('#a5b7c7'), 1.3, Qt.PenStyle.DotLine))
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        painter.drawEllipse(point, 5, 5)
                        painter.drawText(QRectF(point.x() + 6, point.y() - 8, 18, 16), '烟')
        observed = self.observed_positions()
        if self.layers['ranges'] and self.selected in observed:
            position = observed[self.selected]
            state = self.ship_state(self.selected)
            if state.get('visibility') != 'not_visible':
                for consumable in state.get('consumables', []):
                    if consumable.get('quality') == 'reported_timer' and _finite(consumable.get('remaining_seconds')) and consumable['remaining_seconds'] > 0:
                        x, z = position[1:3]
                        if self.relative_grid:
                            x, z = x * 5000 / 2047 - 2500, z * 5000 / 2047 - 2500
                        self._range_ellipse(painter, x, z, consumable.get('radius_engine'),
                                            canvas, '#c5b0ee')
        for projectile in self.intel.projectiles(self.clock):
            kind = projectile.get('kind')
            if kind not in ('shell', 'torpedo') or not self.layers['shells' if kind == 'shell' else 'torpedoes']:
                continue
            if not quality_allowed(projectile.get('quality'), estimates):
                continue
            if not all(_finite(projectile.get(key)) for key in ('x_engine', 'z_engine')):
                continue
            point = self._world_point(projectile['x_engine'], projectile['z_engine'], canvas)
            estimated = projectile.get('quality') == 'estimated'
            metadata=self.assets.projectile(projectile.get('params_id'))
            color = QColor(AMMO_COLORS.get(metadata.get('ammo_type'),'#BBC9D9')) if kind=='shell' else self.color(projectile.get('owner_entity_id'),projectile.get('team_id'))
            color.setAlpha(135 if estimated else 230)
            caliber=metadata.get('caliber_mm');width=1.6 if not _finite(caliber) else 1.1 if caliber<150 else 1.6 if caliber<300 else 2.2
            painter.setPen(QPen(color, width, Qt.PenStyle.DashLine if estimated else Qt.PenStyle.SolidLine))
            if all(_finite(projectile.get(key)) for key in ('tail_x_engine', 'tail_z_engine')):
                tail = self._world_point(projectile['tail_x_engine'], projectile['tail_z_engine'], canvas)
                gradient=QLinearGradient(tail,point);fade=QColor(color);fade.setAlpha(0);gradient.setColorAt(0,fade);gradient.setColorAt(1,color)
                painter.setPen(QPen(QBrush(gradient),width,Qt.PenStyle.DashLine if estimated else Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap))
                painter.drawLine(tail, point)
            painter.setBrush(Qt.BrushStyle.NoBrush if estimated else color)
            if kind == 'torpedo':
                direction=projectile.get('direction_raw')
                if isinstance(direction,list) and len(direction)==3 and all(_finite(v) for v in direction):
                    length=math.hypot(direction[0],direction[2])
                    if length>0:
                        painter.setPen(QPen(color,1.2));painter.drawLine(point,point+QPointF(-direction[0]/length*6,direction[2]/length*6))
                painter.setPen(QPen(QColor('#10161B'),.7));painter.drawEllipse(point,2.4,2.4)
            else:
                painter.setPen(Qt.PenStyle.NoPen);painter.drawEllipse(point,width,width)
                if projectile.get('phase')=='recorded_end':
                    painter.setBrush(Qt.BrushStyle.NoBrush);painter.setPen(QPen(color,.8));painter.drawEllipse(point,3.8,3.8)
        if self.layers['planes'] and hasattr(self.intel,'aircraft'):
            for plane in self.intel.aircraft(self.clock):
                point=self._world_point(plane['x_engine'],plane['z_engine'],canvas)
                if not canvas.contains(point):continue
                relation=self.relation(team=plane.get('team_id'));image=self.assets.plane(plane.get('params_id'),relation,20*self.symbol_scale)
                if image is not None:
                    size=20*self.symbol_scale;painter.drawImage(QRectF(point.x()-size/2,point.y()-size/2,size,size),image)
                else:
                    painter.setPen(QPen(self.color(team=plane.get('team_id')),1));painter.setBrush(QColor('#132B3E'));painter.drawEllipse(point,6,6)
                    painter.drawText(QRectF(point.x()-5,point.y()-8,10,16),Qt.AlignmentFlag.AlignCenter,'?')

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._hits = []
            if self.projection is None:
                _archive_empty(painter,self.rect(),'回放待命',self.reason)
                return
            canvas = self._canvas_rect()
            self._paint_map_surround(painter)
            crop = self.projection['viewport_crop_pixels']
            painter.save()
            painter.setClipRect(canvas)
            if self.full_art:
                painter.drawImage(canvas,self.water);painter.drawImage(canvas,self.land)
            elif self.relative_grid:
                painter.fillRect(canvas,QColor('#142b3b'))
            else:
                painter.drawImage(canvas, self.water)
                painter.drawImage(canvas, self.land, QRectF(crop['left'], crop['top'], crop['right'] - crop['left'], crop['bottom'] - crop['top']))
            if self.layers['terrain'] and self.terrain is not None:
                painter.drawImage(canvas, self.terrain)
            painter.setPen(QPen(QColor(210, 230, 255, 32), 1))
            if self.layers['grid']:
                for i in range(1, len(GRID_ROWS)):
                    x = canvas.left() + canvas.width() * i / len(GRID_ROWS)
                    y = canvas.top() + canvas.height() * i / len(GRID_ROWS)
                    painter.drawLine(QPointF(x, canvas.top()), QPointF(x, canvas.bottom()))
                    painter.drawLine(QPointF(canvas.left(), y), QPointF(canvas.right(), y))
                if self.evidence_frame and not self.relative_grid:
                    painter.setFont(QFont('Microsoft YaHei UI', 8))
                    for i, letter in enumerate(GRID_ROWS):
                        self._text(painter,QRectF(canvas.left()+canvas.width()*(i+.5)/10-4,canvas.top()+2,18,15),str(i+1),'#C6D1DD')
                        self._text(painter,QRectF(canvas.left()+3,canvas.top()+canvas.height()*(i+.5)/10-7,18,15),letter,'#C6D1DD')
            bounds = self.projection['viewport_bounds_engine']
            self._draw_capture_points(painter, canvas, bounds)
            self._draw_combat(painter, canvas)
            if self.trails:
                for entity, segments in self.tracks.items():
                    color = self.color(entity)
                    color.setAlpha(210 if entity == self.selected else 105)
                    painter.setPen(QPen(color, 2.4 if entity == self.selected else 1.4))
                    for segment in segments:
                        points = [self._pixel_point(row[3], row[4], canvas) for row in segment
                                  if max(0, self.clock - 60) <= row[0] <= min(self.clock, self.deaths.get(entity, self.clock))]
                        if len(points) > 1:
                            painter.drawPolyline(QPolygonF(points))
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.setFont(QFont('Microsoft YaHei UI',9));self._label_rects=[];labels=[];occupied=[]
            displayed=self.display_positions()
            highlights = []
            for entity, (point, last_known) in displayed.items():
                position = self._pixel_point(point[3], point[4], canvas)
                if not canvas.contains(position):
                    continue
                player = self.players.get(entity, {})
                color = self.color(entity)
                state = self.ship_state(entity)
                dead=entity in self.deaths and self.clock>=self.deaths[entity]
                if last_known:
                    state=self.intel.ship_state(entity,min(point[0],self.deaths.get(entity,point[0]))) if self.intel else state
                heading=state.get('heading_radians') if self.layers['heading'] and not self.relative_grid else None
                if heading is None and self.layers['heading']:
                    heading=self.wire_headings.get(entity,{}).get(point[0])
                hover_level = self._hover_levels.get(entity, 0.)
                if hover_level:
                    highlights.append((position, QColor('#C2DEEF') if last_known or dead else color, hover_level))
                draw_ship_symbol(painter, position, player.get('_ship_kind', '?'), color,
                                 visibility=state.get('visibility', 'unknown'), last_known=last_known,
                                 selected=entity == self.selected,
                                 heading=heading,
                                 hp_fraction=health_fraction(state) if self.layers['health'] else None,
                                 spotted=spotting_indicator(state, last_known),dead=dead,own=entity==self.own_entity,
                                 size=self.symbol_scale,assets=self.assets)
                occupied.append(QRectF(position.x()-15*self.symbol_scale,position.y()-15*self.symbol_scale,30*self.symbol_scale,35*self.symbol_scale))
                lines=[]
                if not dead or entity==self.selected:
                    caption=player.get('_ship_label','未知舰船') if self.layers['ship_names'] else ''
                    if caption:lines.append(caption)
                    if self.layers['player_names']:lines.append(str(player.get('name','未知选手')))
                    if not self.relative_grid and not last_known and heading is None and self.layers['heading']:lines.append('航向未知')
                if lines:labels.append((entity,position,lines,QColor('#AEBAC7') if last_known else color))
                if self.layers['consumables'] and not last_known and not dead:
                    consumables = [entry for entry in state.get('consumables', [])
                                   if entry.get('quality') in ('reported_timer', 'activation_only')]
                    badge=self._consumable_badges(painter,position,consumables,self.symbol_scale)
                    if badge:occupied.append(badge)
                self._hits.append((position, entity))
            for position, ink, level in highlights:
                self._paint_hover_marker(painter, position, ink, level)
            for entity,position,lines,color in sorted(labels,key=lambda row:(row[0]!=self.selected,row[0]!=self.own_entity,row[0])):
                self._label(painter,position,lines,color,canvas,occupied)
            painter.restore()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor('#526b83'), 1))
            painter.drawRect(canvas)
            if self.evidence_frame:
                self._paint_evidence_axes(painter, canvas, bounds)
            else:
                painter.setPen(QColor('#34546F'))
                painter.setFont(QFont('Microsoft YaHei UI', 8))
                if self.layers['grid']:
                    self._paint_grid_labels(painter, canvas)
                label = '原生全图' if self.full_art else '相对航迹' if self.relative_grid else ''
                title = f'{self.map_display_name}  ·  {label}' if label else self.map_display_name
                painter.drawText(QRectF(canvas.left(), 4, canvas.width(), 22), title)
        finally:
            painter.end()

    def _paint_map_surround(self, painter):
        if self.evidence_frame:
            painter.fillRect(self.rect(), QColor('#0a1625'))
            return
        frame = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        surface = QLinearGradient(frame.topLeft(), frame.bottomRight())
        surface.setColorAt(0, QColor('#F7FBFF'))
        surface.setColorAt(.58, QColor('#E9F2FC'))
        surface.setColorAt(1, QColor('#F6EAF6'))
        surround = panel_path(frame, 14)
        painter.fillPath(surround, surface)
        painter.setPen(QPen(QColor('#A6C1D7'), 1))
        painter.drawPath(surround)
        painter.fillRect(QRectF(frame.left()+10, frame.top()+6, 19, 2), QColor(tone.BLUE))
        painter.fillRect(QRectF(frame.left()+32, frame.top()+6, 8, 2), QColor(tone.PINK))

    def _paint_grid_labels(self, painter, canvas):
        cell_width = canvas.width() / len(GRID_ROWS)
        cell_height = canvas.height() / len(GRID_ROWS)
        for index, row in enumerate(GRID_ROWS):
            top = canvas.top() + index * cell_height
            left = canvas.left() + index * cell_width
            painter.drawText(QRectF(canvas.left()-25, top, 20, cell_height), Qt.AlignmentFlag.AlignCenter, row)
            painter.drawText(QRectF(left, canvas.bottom()+4, cell_width, 19), Qt.AlignmentFlag.AlignCenter, str(index+1))

    def _paint_evidence_axes(self, painter, canvas, bounds):
        # The report preview keeps the coordinate convention and appearance of its stored visual references.
        painter.setPen(QColor('#a5b8cd'))
        painter.setFont(QFont('Microsoft YaHei UI', 8))
        for i in range(5):
            x = bounds['min_x'] + (bounds['max_x'] - bounds['min_x']) * i / 4
            z = bounds['max_z'] - (bounds['max_z'] - bounds['min_z']) * i / 4
            factor = self.scale / 1000 if self.scale else 1
            painter.drawText(QRectF(canvas.left() + i * canvas.width() / 4 - 30, canvas.bottom() + 7, 60, 20), Qt.AlignmentFlag.AlignCenter, f'{x * factor:g}')
            painter.drawText(QRectF(canvas.left() - 53, canvas.top() + i * canvas.height() / 4 - 10, 45, 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f'{z * factor:g}')
        unit = '原生全图 · 场景坐标' if self.full_art else '相对航迹 · 录像坐标' if self.relative_grid else ('km' if self.scale else '引擎单位')
        painter.drawText(QRectF(canvas.left(), 4, canvas.width(), 22), f'{self.map_display_name}  ·  {unit}')

    def _paint_hover_marker(self, painter, pos, ink, level):
        radius = (16. + 4.*level) * self.symbol_scale
        painter.save()
        glow = QRadialGradient(pos, radius+10)
        bright = QColor(ink).lighter(140)
        for stop, opacity in ((0, 44), (.62, 106), (1, 0)):
            color = QColor(bright)
            color.setAlpha(round(opacity*level))
            glow.setColorAt(stop, color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(pos, radius+10, radius+10)
        bright.setAlpha(round(237*level))
        painter.setPen(QPen(bright, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        ring = QRectF(pos.x()-radius, pos.y()-radius, 2*radius, 2*radius)
        for angle in range(0, 360, 90):
            painter.drawArc(ring, (angle+16)*16, 58*16)
        painter.restore()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            hits = [(math.hypot(event.position().x() - point.x(), event.position().y() - point.y()), entity) for point, entity in self._hits]
            if hits and min(hits)[0] <= 16:
                self.entity_selected.emit(min(hits)[1])
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        hits = [(math.hypot(event.position().x() - point.x(), event.position().y() - point.y()), entity) for point, entity in self._hits]
        if hits and min(hits)[0] <= 16:
            player = self.players.get(min(hits)[1], {})
            entity = min(hits)[1]
            state = self.ship_state(entity)
            self.setToolTip(f"{player.get('name', '未知选手')}\n{player.get('_ship_label', '未知舰船')} · {player.get('_ship_kind', '?')}\n"
                            f"{state.get('visibility_label') or '视野状态未知'}；图标朝向仅使用记录航向")
        else:
            self.setToolTip(next((text for rect, text in self._cap_hits if rect.contains(event.position())), ''))
        super().mouseMoveEvent(event)


class ReplayPanel(QWidget):
    clock_changed = Signal(float)

    def __init__(self, parent=None, asset_bundle=None):
        super().__init__(parent)
        self.assets=MinimapAssets(asset_bundle) if asset_bundle is not None else default_assets()
        self.setObjectName('replayPanel')
        self.setProperty('academyChrome',True)
        self._clock = 0.0
        self._playback_position = 0.0
        self._maximum = None
        self._report = {}
        self._coverage_frames = []
        self._coverage_clocks = []
        self._players = {}
        self._team_entities = ((), ())
        self._selected = None
        self._time_analysis = None
        self._supported = False
        self._intel = None
        self._base_status_text = '等待导入比赛'
        self._last_tick = 0.0
        self._resume_after_drag = False
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        self.status_label = QLabel()
        self.status_label.setObjectName('replayStatusLabel')
        self.status_label.setWordWrap(True)
        self.status_label.setContentsMargins(12,0,24,0)
        self.status_label.setMinimumHeight(23)
        layout.addWidget(self.status_label)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName('replayContentSplitter')
        stage = QWidget()
        flanks = QHBoxLayout(stage)
        flanks.setContentsMargins(0, 0, 0, 0)
        flanks.setSpacing(6)
        center = QVBoxLayout()
        center.setContentsMargins(0, 0, 0, 0)
        center.setSpacing(3)
        self.match_hud = ReplayMatchHud(MAP_HORIZONTAL_GUTTERS)
        self.map_canvas = _MapCanvas(assets=self.assets)
        self.map_canvas.entity_selected.connect(self._select_entity)
        center.addWidget(self.match_hud)
        center.addWidget(self.map_canvas, 1)
        self.team_rails = tuple(ReplayTeamRail(self.assets, right=right) for right in (False, True))
        for rail in self.team_rails:
            rail.ship_selected.connect(self._select_entity)
            rail.ship_hovered.connect(self.map_canvas.preview_ship)
        flanks.addWidget(self.team_rails[0])
        flanks.addLayout(center, 1)
        flanks.addWidget(self.team_rails[1])
        splitter.addWidget(stage)
        tabs = QTabWidget()
        tabs.setObjectName('replayDetailTabs')
        tabs.setMinimumWidth(224)
        self.detail_tabs = tabs
        self.event_list = _ReplayList('暂无事件')
        self.event_list.setObjectName('replayEvents')
        self.event_list.setWordWrap(True)
        self.event_list.itemClicked.connect(self._jump_event)
        self.event_list.itemActivated.connect(self._jump_event)
        detail_page = QWidget()
        detail_layout = QVBoxLayout(detail_page)
        detail_layout.setContentsMargins(5, 4, 6, 5)
        detail_layout.setSpacing(5)
        self.ship_card = LayeredPanel(cut=12)
        self.ship_card.setObjectName('selectedShipCard')
        card_layout = QVBoxLayout(self.ship_card)
        card_layout.setContentsMargins(14, 10, 38, 14)
        card_layout.setSpacing(3)
        self.ship_card_title = QLabel('舰船状态')
        self.ship_card_title.setObjectName('selectedShipTitle')
        self.ship_card_title.setWordWrap(True)
        self.ship_card_title.setTextFormat(Qt.TextFormat.PlainText)
        card_layout.addWidget(self.ship_card_title)
        self.ship_health_label = QLabel()
        self.ship_health_label.setObjectName('selectedShipHealth')
        card_layout.addWidget(self.ship_health_label)
        self.ship_health_bar = QProgressBar()
        self.ship_health_bar.setObjectName('selectedShipHealthBar')
        self.ship_health_bar.setRange(0, 1000)
        self.ship_health_bar.setMaximumHeight(5)
        self.ship_health_bar.setTextVisible(False)
        card_layout.addWidget(self.ship_health_bar)
        self.ship_visibility_label = QLabel()
        self.ship_visibility_label.setObjectName('selectedShipVisibility')
        self.ship_visibility_label.setWordWrap(True)
        card_layout.addWidget(self.ship_visibility_label)
        self.ship_heading_label = QLabel()
        self.ship_heading_label.setObjectName('selectedShipHeading')
        card_layout.addWidget(self.ship_heading_label)
        self.ship_consumable_label = QLabel()
        self.ship_consumable_label.setObjectName('selectedShipConsumables')
        self.ship_consumable_label.setWordWrap(True)
        card_layout.addWidget(self.ship_consumable_label)
        detail_layout.addWidget(self.ship_card)
        detail_layout.addStretch(1)
        tabs.addTab(detail_page, '舰船详情')
        tabs.addTab(self.event_list, '事件')
        layer_scroll = QScrollArea()
        layer_scroll.setObjectName('replayLayerScroll')
        layer_scroll.viewport().setObjectName('replayLayerViewport')
        layer_scroll.setWidgetResizable(True)
        layer_scroll.setFrameShape(QFrame.Shape.NoFrame)
        layer_page = QWidget()
        layer_page.setObjectName('replayLayers')
        layer_layout = QVBoxLayout(layer_page)
        layer_layout.setContentsMargins(7, 8, 7, 8)
        layer_layout.setSpacing(9)
        legend=CombatLegend(assets=self.assets)
        legend.setProperty('tacticalSurface',True)
        legend.setAttribute(Qt.WidgetAttribute.WA_StyledBackground,True)
        layer_layout.addWidget(legend)
        layer_grid = QGridLayout()
        self.layer_checks = {}
        layer_names = [('heading', '记录航向'), ('health', '已知血量'), ('shells', '炮弹'),
                       ('torpedoes', '鱼雷'), ('smokes', '烟幕观测'), ('consumables', '消耗品标记'),
                       ('ranges', '选中船范围'), ('last_known', '最后已知位置'),
                       ('estimates', '显示估算'), ('capture_points', '占领区'),
                       ('ship_names','舰船名称'),('player_names','选手名称'),('wrecks','击沉标记'),
                       ('grid','A–J / 1–10 网格'),('planes','飞机中队'),('battle_hud','战局状态'),
                       ('terrain','地形高低')]
        for index, (key, text) in enumerate(layer_names):
            checkbox = QCheckBox(text)
            checkbox.setObjectName('layer_' + key)
            checkbox.setChecked(LAYER_DEFAULTS[key])
            checkbox.toggled.connect(lambda enabled, name=key: self._set_layer(name, enabled))
            layer_grid.addWidget(checkbox, index // 2, index % 2)
            self.layer_checks[key] = checkbox
        self.layer_checks['ranges'].setToolTip('选中舰船启用水搜或雷达时，显示该舰消耗品的基础对舰探测范围')
        self.layer_checks['last_known'].setToolTip('空心图标：最后观测位置')
        self.layer_checks['estimates'].setToolTip('虚线：按已记录起终点估算')
        self.layer_checks['terrain'].setToolTip('与原生底图配准的 WoWSP 地形模型。相对高程由青蓝色过渡至浅金色。')
        layer_layout.addLayout(layer_grid)
        self.layer_notice = QLabel()
        self.layer_notice.setObjectName('replayLayerNotice')
        self.layer_notice.setWordWrap(True)
        layer_layout.addWidget(self.layer_notice)
        note = QLabel('单视角：友方绿、敌方红、录制者金。\n双边合并：队伍 0 绿、队伍 1 红。\n灰色空心为历史观测位置。\n血条只显示已知值；空白不表示满血。\nAP 白 / HE 黄 / CS(SAP) 红；弹种未知为灰。')
        note.setWordWrap(True)
        layer_layout.addWidget(note)
        layer_layout.addStretch()
        layer_scroll.setWidget(layer_page)
        tabs.addTab(layer_scroll, '图层')
        tabs.setCurrentWidget(self.event_list)
        splitter.addWidget(tabs)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([850, 245])
        layout.addWidget(splitter, 1)
        self.selected_label = QLabel('选择舰船')
        self.selected_label.setObjectName('replaySelectedLabel')
        self.selected_label.setWordWrap(True)
        self.selected_label.setContentsMargins(12,0,58,0)
        layout.addWidget(self.selected_label)
        times = QHBoxLayout()
        self.clock_label = QLabel()
        self.clock_label.setObjectName('replayClockLabel')
        self.clock_label.setContentsMargins(12,0,0,0)
        self.battle_time_label = QLabel()
        self.battle_time_label.setObjectName('replayBattleTimeLabel')
        self.battle_time_label.setContentsMargins(0,0,64,0)
        times.addWidget(self.clock_label)
        times.addStretch()
        times.addWidget(self.battle_time_label)
        layout.addLayout(times)
        self.timeline = QSlider(Qt.Orientation.Horizontal)
        self.timeline.setObjectName('replayTimeline')
        self.timeline.valueChanged.connect(lambda value: self.set_clock(value / 1000))
        self.timeline.sliderPressed.connect(self._start_drag)
        self.timeline.sliderReleased.connect(self._end_drag)
        layout.addWidget(self.timeline)
        controls = QHBoxLayout()
        controls.setContentsMargins(8,0,12,0)
        self.start_button = QPushButton('回到开始')
        self.start_button.setObjectName('replayStartButton')
        self.start_button.clicked.connect(lambda: self._jump_to(0))
        self.play_button = QPushButton('播放')
        self.play_button.setObjectName('replayPlayButton')
        self.play_button.clicked.connect(self._toggle_playback)
        self.speed_combo = QComboBox()
        self.speed_combo.setObjectName('replaySpeed')
        for speed in (.5, 1, 2, 4, 8):
            self.speed_combo.addItem(f'{speed:g}×', speed)
        self.speed_combo.setCurrentIndex(1)
        self.trails_box = QCheckBox('60 秒轨迹')
        self.trails_box.setObjectName('replayTrails')
        self.trails_box.setChecked(False)
        self.trails_box.setToolTip('已观测的连续航迹')
        self.trails_box.toggled.connect(self._set_trails)
        self.symbol_scale_combo=QComboBox();self.symbol_scale_combo.setObjectName('replaySymbolScale')
        for text,scale in (('小 85%',.85),('标准 100%',1.),('大 125%',1.25),('特大 150%',1.5)):self.symbol_scale_combo.addItem(text,scale)
        self.symbol_scale_combo.setCurrentIndex(1);self.symbol_scale_combo.currentIndexChanged.connect(self._set_symbol_scale)
        for widget in (self.start_button, self.play_button, self.speed_combo, self.trails_box):
            controls.addWidget(widget)
        controls.addWidget(QLabel('标记大小'));controls.addWidget(self.symbol_scale_combo)
        controls.addStretch()
        layout.addLayout(controls)
        from .theme import apply_native_controls
        apply_native_controls(self)
        self.clear()

    def paintEvent(self,event):
        painter=QPainter(self)
        painter.fillRect(self.rect(),QColor('#EDF4FA'))
        header_height = self.status_label.geometry().bottom() + 6
        paint_layered_panel(painter,QRectF(0,0,self.width(),header_height),cut=12)
        if hasattr(self,'detail_tabs'):
            origin=self.detail_tabs.mapTo(self,self.detail_tabs.rect().topLeft())
            panel=QRectF(origin.x(),origin.y(),self.detail_tabs.width(),self.detail_tabs.height()).adjusted(-2,-1,3,4)
            paint_layered_panel(painter,panel,accent=tone.PINK,cut=14)
        if hasattr(self,'selected_label'):
            top=self.selected_label.y()-7
            paint_layered_panel(painter,QRectF(0,top,self.width(),self.height()-top),cut=14)
        painter.end()

    @property
    def current_clock(self):
        return self._clock

    @property
    def roster_count(self):
        return len(self._players)

    @property
    def intel(self):
        return self._intel

    def _refresh_layers(self):
        capabilities = self._intel.capabilities if self._intel is not None else {}
        # The wire grid and decoded world-space events share an axis only after
        # map calibration. Preserve the user's switches when changing battles.
        relative_only = self.map_canvas.relative_grid and not self.map_canvas.full_art
        projected_layers = {'shells', 'torpedoes', 'smokes', 'ranges', 'planes', 'estimates'}
        self.layer_checks['ranges'].setToolTip('已启用消耗品的客户端基础探船半径' if self.map_canvas.relative_grid
                                              else '已记录的有效范围')
        for key, widget in self.layer_checks.items():
            if key in ('last_known','ship_names','player_names','wrecks','grid'):
                enabled = bool(self.map_canvas.tracks)
            elif key=='heading' and self.map_canvas.relative_grid:
                enabled = bool(self.map_canvas.wire_headings)
            elif key == 'capture_points':
                enabled = bool(self._report.get('capture_points'))
            elif key == 'battle_hud':
                enabled = self._has_team_hud() or bool(self.map_canvas.battle_state.points)
            elif key == 'terrain':
                if widget.isChecked():
                    self.map_canvas.load_terrain()
                enabled = bool(self.map_canvas._terrain_report is not None or self.map_canvas.terrain is not None)
                if not enabled and widget.isChecked():
                    with QSignalBlocker(widget):
                        widget.setChecked(False)
            elif key == 'estimates':
                enabled = not self._report.get('multiview') and any(capabilities.get(name) is True for name in ('shells', 'torpedoes'))
                if self._report.get('multiview'):widget.setChecked(False)
            else:
                enabled = capabilities.get(key) is True
            if key in projected_layers:
                preference = widget.property('checkedBeforeRelativeMap')
                with QSignalBlocker(widget):
                    if relative_only:
                        if preference is None:
                            widget.setProperty('checkedBeforeRelativeMap', widget.isChecked())
                        widget.setChecked(False)
                        enabled = False
                    elif preference is not None:
                        widget.setChecked(bool(preference))
                        widget.setProperty('checkedBeforeRelativeMap', None)
            widget.setEnabled(enabled)
            self.map_canvas.layers[key] = widget.isChecked()
        self._refresh_hud_visibility()
        if self.map_canvas.relative_grid and not getattr(self._intel, 'available', False):
            self.layer_notice.setText('旧版解析仅含舰位与击沉。点击「更新本局」读取战斗图层。')
        elif self._intel is None or not getattr(self._intel, 'available', False):
            self.layer_notice.setText('战斗图层未解析')
        else:
            names = {'heading': '航向', 'health': '血量', 'visibility': '视野', 'shells': '炮弹',
                     'torpedoes': '鱼雷', 'smokes': '烟幕', 'consumables': '消耗品', 'ranges': '范围','planes':'飞机中队'}
            available = [text for key, text in names.items() if capabilities.get(key) is True
                         and not (relative_only and key in projected_layers)]
            notice = '已记录：' + ('、'.join(available) or '无')
            if relative_only:
                notice += '。当前为相对航迹，场景坐标尚未校准。'
            self.layer_notice.setText(notice)
        self.map_canvas.update()

    def _set_layer(self, name, enabled):
        self.map_canvas.layers[name] = enabled
        if name == 'terrain' and enabled:
            self.map_canvas.load_terrain()
            self._refresh_layers()
        if name in ('capture_points', 'battle_hud'):
            self._refresh_hud_visibility()
            self._update_match_hud()
        self.map_canvas.update()
        self._update_selection()
        self._update_status_label()

    def _set_symbol_scale(self,*_):
        self.map_canvas.symbol_scale=self.symbol_scale_combo.currentData();self.map_canvas.update()

    def _has_team_hud(self):
        return bool(self._report.get('roster') or self._report.get('team_score_samples'))

    def _refresh_hud_visibility(self):
        layers = self.map_canvas.layers
        show_teams = layers['battle_hud'] and self._has_team_hud()
        for rail in self.team_rails:
            rail.setVisible(show_teams)
        has_captures = layers['capture_points'] and bool(self.map_canvas.battle_state.points)
        self.match_hud.setVisible(layers['battle_hud'] and has_captures)

    def _update_status_label(self):
        suffix = ''
        if self.map_canvas.projection is not None and self.map_canvas.layers['estimates']:
            suffix += ' · 弹迹示意'
        if self._report.get('multiview'):
            index = bisect_right(self._coverage_clocks, self._clock) - 1
            frame = self._coverage_frames[index] if index >= 0 else None
            if frame and abs(self._clock - frame['clock']) < .001:
                suffix += f' · 存活 {frame.get("position_count", 0)}/{frame.get("expected_alive_count", "?")}'
                if frame.get('conflicts_at_tick'):
                    suffix += ' · 记录差异'
            else:
                suffix += ' · 暂无对齐观测'
        self.status_label.setText(self._base_status_text + suffix)

    def _update_ship_card(self, player, state):
        details = (self.ship_health_label, self.ship_health_bar, self.ship_visibility_label,
                   self.ship_heading_label, self.ship_consumable_label)
        if player is None:
            self.ship_card_title.setText('选择舰船')
            for widget in details:
                widget.hide()
            return
        self.ship_card_title.setText(f"{player.get('_ship_kind', '?')} · {player['_ship_label']}\n{player.get('name', '未知选手')}")
        for widget in details:
            widget.show()
        hp, maximum = state.get('hp'), state.get('max_hp')
        dead=state.get('death_clock') is not None
        hp_text = f'{hp:,.0f}' if _finite(hp) and hp >= 0 else '未知'
        max_text = f'{maximum:,.0f}' if _finite(maximum) and maximum > 0 else '未知'
        self.ship_health_label.setText(f'{"最后血量记录" if dead else "血量记录"}：{hp_text} / {max_text}')
        stamp = state.get('health_clock')
        self.ship_health_label.setToolTip('血量观测时间：' + _clock_text(stamp, True))
        fraction = health_fraction(state)
        self.ship_health_bar.setVisible(fraction is not None and not dead)
        if fraction is not None:
            self.ship_health_bar.setValue(round(fraction * 1000))
        local = state.get('visibility_label') or '本视角观测状态未知'
        enemy = state.get('spotted_label') or '敌方点亮：未知'
        if self._report.get('multiview') and state.get('source_sha'):
            source = next((s for s in self._report['multiview'].get('sources', []) if s.get('sha256') == state['source_sha']), {})
            caption = source.get('recorder') or source.get('recorder_name') or state['source_sha'][:10]
            original = state.get('source_original_packet_clock')
            local += f'\n位置来源：{caption}' + (f' · 原回放 {_clock_text(original, True)}' if _finite(original) else '')
        self.ship_visibility_label.setText(f'{local}\n{enemy}')
        heading = state.get('heading_radians')
        if _finite(heading):
            compass = (90 - math.degrees(heading)) % 360
            self.ship_heading_label.setText(f'{"最后记录航向" if dead else "记录航向"}：{compass:05.1f}°（北为 0°）')
        else:
            self.ship_heading_label.setText('记录航向：未知')
        texts = []
        for entry in state.get('consumables', []):
            if entry.get('quality') not in ('reported_timer', 'activation_only'):
                continue
            label = str(entry.get('label') or '未识别消耗品')
            remaining = entry.get('remaining_seconds')
            suffix = f'报告剩余 {math.ceil(remaining)}s' if _finite(remaining) and remaining >= 0 else '结束未知'
            texts.append(f'{label} · {suffix}')
        self.ship_consumable_label.setText('消耗品：' + ('；'.join(texts[:2]) + (f'；另 {len(texts) - 2} 项' if len(texts) > 2 else '') if texts else '活动状态未记录'))
        if dead:self.ship_consumable_label.setText('消耗品：已沉没，不显示活动状态')
        self.ship_consumable_label.setToolTip('\n'.join(texts) if texts else '没有记录不代表未使用或已经关闭')

    def clear(self):
        self._pause()
        self._clock = 0.0
        self._playback_position = 0.0
        self._maximum = None
        self._report = {}
        self._coverage_frames = []
        self._coverage_clocks = []
        self._players = {}
        self._team_entities = ((), ())
        self._selected = None
        self._time_analysis = None
        self._supported = False
        self._intel = None
        self.event_list.clear()
        self.map_canvas.clear()
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.play_button.setEnabled(False)
        self.start_button.setEnabled(False)
        self.status_label.setText('等待导入比赛')
        self.status_label.setToolTip('')
        self._base_status_text = '等待导入比赛'
        self.clock_label.setText('记录时间 —')
        self.battle_time_label.setText('比赛时间 —')
        self.match_hud.set_frame((), (), None)
        for rail in self.team_rails:
            rail.set_team(None)
            rail.set_selected(None)
        self.selected_label.setText('选择舰船')
        self._update_ship_card(None, {})
        self._refresh_layers()

    def set_report(self, report: dict, ship_catalog: dict | None = None):
        self.clear()
        if not isinstance(report, dict) or report.get('status') == 'failed':
            self.status_label.setText('这份比赛尚未成功解析')
            self.map_canvas.clear('无可用地图数据')
            return
        self._report = report
        self._coverage_frames = ((report.get('multiview') or {}).get('coverage') or {}).get('frames', [])
        self._coverage_clocks = [row['clock'] for row in self._coverage_frames]
        try:
            version = tuple(int(value.strip()) for value in re.split('[,._]', str(report.get('replay_version', ''))))
        except ValueError:
            version = ()
        self._supported = version == SUPPORTED_VERSION
        catalog = ship_catalog or {}
        ships = catalog.get('ships', catalog)
        from ..ordinary_replay import current_combat_verified
        if self._supported or current_combat_verified(report):
            try:
                from wows_training.replay_intel import ReplayIntel
                self._intel = ReplayIntel(report, catalog)
            except (ImportError, ValueError, TypeError) as exc:
                self.layer_notice.setToolTip(str(exc))
        roster = sorted(report.get('roster') or [], key=lambda player: (str(player.get('teamId')), str(player.get('name', ''))))
        for player in roster:
            entity = player.get('shipId')
            if type(entity) is not int:
                continue
            record = dict(player)
            ship = ships.get(str(player.get('shipParamsId')), {})
            record['_ship_label'] = ship.get('display_name') or ship.get('name') or ship.get('internal_name') or f"舰船 {player.get('shipParamsId', '—')}"
            record['_ship_kind'] = ship_kind(ship.get('species'))
            self._players[entity] = record
        self.map_canvas.configure(report, self._players, self._supported, self._intel)
        self._team_entities = tuple(tuple(entity for entity, player in self._players.items()
                                          if type(player.get('teamId')) is int and player['teamId'] == team)
                                    for team in (0, 1))
        self._refresh_layers()
        maximum = (report.get('packet_clock_range') or {}).get('max')
        if not _finite(maximum) or maximum < 0:
            maximum = None
        self._maximum = maximum
        analysis = report.get('time_analysis')
        if isinstance(analysis, dict) and analysis.get('model') in ('15.9_observed_client_timer', '15.10_observed_client_timer',
                                                                     'multiview_aligned_battle_timer'):
            self._time_analysis = analysis
        if maximum is not None:
            self.timeline.setRange(0, int(math.ceil(maximum * 1000)))
            self.timeline.setSingleStep(1000 if report.get('multiview') else 100)
            self.timeline.setEnabled(maximum > 0)
            self.play_button.setEnabled(maximum > 0)
            self.start_button.setEnabled(True)
        self._populate_events()
        coverage = (self._time_analysis or {}).get('coverage', {})
        gap = coverage.get('result_minus_last_observed_elapsed_sec')
        if self.map_canvas.relative_grid:
            status = '录制视角 · 原生全图' if self.map_canvas.full_art else '录制视角 · 相对航迹'
        elif not self._supported:
            status = '版本未验证 · 仅显示事件'
        elif self.map_canvas.projection is None:
            status = '底图不可用 · 时间与事件仍可查看'
        else:
            status = '录制视角'
        if report.get('multiview'):
            status = '全局观测'
        if _finite(gap) and gap > 0:
            status += ' · 尾段不完整'
            self.status_label.setToolTip(f"末次记录 {_clock_text(coverage.get('last_observed_elapsed_sec'))}，结算 {_clock_text(coverage.get('result_duration_sec'))}")
        self._base_status_text = status
        self._update_status_label()
        self.set_clock(0.0)

    def _populate_events(self):
        events = []
        death_offsets = {}
        for entry in self._report.get('normalized_death_times') or []:
            event = entry.get('event', {})
            key = (event.get('clock'), event.get('victim_entity_id'), event.get('killer_entity_id'))
            death_offsets[key] = entry.get('source', {}).get('packet_offset')
        for transition in (self._time_analysis or {}).get('stage_transitions', []):
            name = {'COUNTDOWN': '准备倒计时开始', 'STARTED': '比赛开始', 'END_BATTLE_IDLING': '进入结束停留'}.get(transition.get('stage_name'))
            if name and _finite(transition.get('packet_clock')):
                events.append((transition['packet_clock'], name, None))
        for event in self._report.get('death_events') or []:
            if _finite(event.get('clock')):
                victim = self._players.get(event.get('victim_entity_id'), {})
                killer = self._players.get(event.get('killer_entity_id'), {})
                key = (event['clock'], event.get('victim_entity_id'), event.get('killer_entity_id'))
                events.append((event['clock'], f"击沉  {victim.get('name', '未知舰船')}\n攻击方：{killer.get('name', '未知来源')}", death_offsets.get(key)))
        events.extend(self._battle_events())
        if self._intel is not None:
            for event in self._intel.events:
                if event.get('kind') == 'death' or not _finite(event.get('clock')):
                    continue
                text = str(event.get('label') or event.get('kind') or '战斗记录')
                source = event.get('source')
                offset = source.get('packet_offset') if isinstance(source, dict) else None
                events.append((event['clock'], text, offset))
        for clock, text, offset in sorted(events, key=lambda event: event[0]):
            prefix = '比赛' if self._report.get('multiview') else '记录'
            item = QListWidgetItem(f'{prefix} {_clock_text(clock, True)}  {text}')
            item.setData(Qt.ItemDataRole.UserRole, float(clock))
            item.setData(int(Qt.ItemDataRole.UserRole) + 1, offset)
            self.event_list.addItem(item)

    def _battle_events(self):
        """Include score lead and contest changes without one row per score tick."""
        state = self.map_canvas.battle_state
        leader = None
        first_score = True
        clocks = sorted({clock for rows in state.score_changes.values() for clock, _ in rows})
        for clock in clocks:
            left, right = (state.score(team, clock) for team in (0, 1))
            if left is None or right is None:
                leader = None
                continue
            if first_score:
                yield clock, f'首个双方比分 · {left}–{right}', None
                first_score = False
            if left != right:
                ahead = int(right > left)
                if leader is not None and leader != ahead:
                    yield clock, f'队伍 {ahead} 取得领先 · {left}–{right}', None
                leader = ahead

        for key, point in state.points.items():
            name = str(point.get('name') or '?')
            for clock, owner in state.cap_changes[key]:
                if clock > 0:
                    yield clock, f'占领区 {name} · {owner_text(owner)}', None
            contested = None
            for clock in sorted(state.capture_times(key)):
                current = state.capture_at(key, clock)['contested']
                if current is True and contested is not True:
                    yield clock, f'{name} 区观测到双方同圈', None
                elif current is False and contested is True:
                    yield clock, f'{name} 区双方同圈结束', None
                contested = current

    def set_clock(self, clock: float):
        if not _finite(clock):
            raise ValueError('Clock must be finite')
        clock = min(max(0.0, float(clock)), self._maximum) if self._maximum is not None else 0.0
        self._playback_position = clock
        if self._report.get('multiview'):
            clock = float(math.floor(clock + 1e-8))
        changed = clock != self._clock
        self._clock = clock
        self.timeline.blockSignals(True)
        self.timeline.setValue(int(round(clock * 1000)))
        self.timeline.blockSignals(False)
        self.map_canvas.clock = clock
        self.map_canvas.update()
        prefix = '合并比赛时间' if self._report.get('multiview') else '记录'
        self.clock_label.setText(f'{prefix} {_clock_text(clock, True)} / {_clock_text(self._maximum, True)}')
        self.battle_time_label.setText(self._battle_time_text())
        positions = self.map_canvas.observed_positions()
        self._update_match_hud(positions)
        self._update_selection(positions)
        if self._report.get('multiview'):
            self._update_status_label()
        if changed:
            self.clock_changed.emit(clock)

    def _update_match_hud(self, positions=None):
        if not self.map_canvas.layers['battle_hud']:
            return
        canvas = self.map_canvas
        clock = self._clock
        positions = canvas.observed_positions() if positions is None else positions
        state = canvas.battle_state
        teams = []
        for team, entities in enumerate(self._team_entities):
            label = f'队伍 {team}'
            if canvas.host_team is not None:
                label = '我方' if team == canvas.host_team else '敌方'
            ships = []
            for entity in entities:
                player = self._players[entity]
                alive = canvas.ship_state(entity).get('is_alive')
                if entity in canvas.deaths and clock >= canvas.deaths[entity]:
                    alive = False
                elif alive is None and entity in positions:
                    alive = True
                ships.append(HudShip(player['_ship_kind'], player['_ship_label'], alive, entity))
            teams.append(HudTeam(label, canvas.color(team=team).name(), state.score(team, clock), tuple(ships)))
        left_team = canvas.host_team if canvas.host_team is not None else 0
        for rail, team in zip(self.team_rails, (teams[left_team], teams[1-left_team])):
            rail.set_team(team)
        points = canvas.battle_state.points.values() if canvas.layers['capture_points'] else ()
        caps = tuple(_capture_badge(cap, state.capture_at(cap, clock), lambda team: canvas.color(team=team).name())
                     for cap in sorted(points, key=lambda cap: str(cap.get('name') or '')))
        self.match_hud.set_frame(tuple(teams), caps, state.win_score(clock))

    def _battle_time_text(self):
        if self._time_analysis is None:
            return '比赛时间 —'
        rows = self._time_analysis.get('observations', [])
        before = [row for row in rows if _finite(row.get('packet_clock')) and row['packet_clock'] <= self._clock]
        row = before[-1] if before else None
        if row is None:
            return '比赛时间 —'
        stage = row.get('stage_name')
        if stage == 'WAITING':
            return '等待开局'
        if stage == 'COUNTDOWN':
            return f"准备倒计时 {_clock_text(row.get('preparation_remaining_sec'))} · 记录值"
        if stage == 'END_BATTLE_IDLING':
            return '比赛已结束 · 停留记录'
        result = packet_to_battle_time(self._time_analysis, self._clock)
        if result['status'] == 'observed_anchor':
            return f"比赛已进行 {_clock_text(result['elapsed_sec'])}"
        if result['status'] == 'bounded_interval':
            low, high = result['elapsed_interval_sec']
            return f"比赛已进行 {_clock_text(low)}–{_clock_text(high)} · 观测区间"
        last = self._time_analysis.get('coverage', {}).get('last_observed_elapsed_sec')
        return f'比赛时间未知 · 末次记录 {_clock_text(last)}'

    def _update_selection(self, positions=None):
        player = self._players.get(self._selected)
        if player is None:
            self._update_ship_card(None, {})
            return
        positions = self.map_canvas.observed_positions() if positions is None else positions
        point = positions.get(self._selected)
        axis = '比赛计时' if self._report.get('multiview') else '记录'
        state = f'位置观测于{axis} {_clock_text(point[0], True)}' if point else '此时没有已知位置'
        if self._selected in self.map_canvas.deaths and self._clock >= self.map_canvas.deaths[self._selected]:
            state = '已沉没 · 地图标记为击沉前最后记录位置'
        self.selected_label.setText(f"{player.get('name', '未知选手')} · {player['_ship_label']} · 队伍 {player.get('teamId', '—')}  |  {state}")
        if (point is None and self.map_canvas.layers['last_known']
                and not (self._selected in self.map_canvas.deaths and self._clock >= self.map_canvas.deaths[self._selected])):
            previous = _last_position(self.map_canvas.tracks.get(self._selected, []), self._clock)
            if previous is not None:
                self.selected_label.setText(f"{player.get('name', '未知选手')} · 最后已知位置：{axis} {_clock_text(previous[0], True)}"
                                            f"（{self._clock - previous[0]:.0f}s 前）；不代表当前位置")
        telemetry = self.map_canvas.ship_state(self._selected)
        if point is not None and telemetry.get('heading_radians') is None:
            heading = self.map_canvas.wire_headings.get(self._selected, {}).get(point[0])
            if heading is not None:
                telemetry = telemetry | {'heading_radians': heading}
        self._update_ship_card(player, telemetry)

    def _select_entity(self, entity):
        if entity not in self._players:
            return
        self._selected = entity
        for rail in self.team_rails:
            rail.set_selected(entity)
        self.map_canvas.selected = entity
        self.map_canvas.update()
        self._update_selection()
        self.detail_tabs.setCurrentIndex(0)

    def _jump_event(self, item):
        self._jump_to(item.data(Qt.ItemDataRole.UserRole))
        offset = item.data(int(Qt.ItemDataRole.UserRole) + 1)
        if type(offset) is int and self._time_analysis is not None and not self._report.get('multiview'):
            result = packet_to_battle_time(self._time_analysis, self._clock, packet_offset=offset)
            if result['status'] == 'unknown':
                self.battle_time_label.setText('该事件的比赛时间未知')

    def _jump_to(self, clock):
        self._pause()
        self.set_clock(clock)

    def _set_trails(self, enabled):
        self.map_canvas.trails = enabled
        self.map_canvas.update()

    def _pause(self):
        self._timer.stop()
        self.play_button.setText('播放')

    def _toggle_playback(self):
        if self._timer.isActive():
            self._pause()
        elif self._maximum is not None and self._clock < self._maximum:
            self._last_tick = time.monotonic()
            self._timer.start()
            self.play_button.setText('暂停')

    def _tick(self):
        now = time.monotonic()
        delta = max(0.0, now - self._last_tick)
        self._last_tick = now
        position = self._playback_position if self._report.get('multiview') else self._clock
        self.set_clock(position + delta * float(self.speed_combo.currentData()))
        if self._maximum is None or self._clock >= self._maximum:
            self._pause()

    def _start_drag(self):
        self._resume_after_drag = self._timer.isActive()
        self._pause()

    def _end_drag(self):
        if self._resume_after_drag:
            self._toggle_playback()
        self._resume_after_drag = False

    def hideEvent(self, event):
        self._pause()
        super().hideEvent(event)
