"""Render selected, source-bound replay observations onto a private minimap."""
from __future__ import annotations

import base64
from bisect import bisect_left, bisect_right
import math
import re
import sqlite3

from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF

from ..replay_context_evidence import _ReplayObservations
from ..replay_intel import ReplayIntel, number
from ..replay_storyboard import MAX_SHEET_PNG_BYTES, STORYBOARD_KIND, build_replay_storyboard_rows
from ..replay_battle_state import ReplayBattleState
from ..replay_grid import GRID_ROWS, location_label
from ..ship_traits import ship_display_name
from .combat_layers import draw_ship_symbol, health_fraction, ship_kind
from .minimap_assets import ABSOLUTE_COLORS
from .presentation_fonts import load_presentation_fonts
from .replay_panel import LAYER_DEFAULTS, SUPPORTED_VERSION, _MapCanvas, _position_at


_TITLES = {
    'local_model_checkpoint': '决策复看点', 'replay_map_control': '占领区归属记录',
    'replay_consumable_overlap': '同类支援消耗品启用时的舰位', 'replay_consumable_timing': '消耗品启用时的舰位',
    'replay_hull_angle': '舰艏相对夹角', 'replay_relative_position': '相对舰位',
    'replay_ship_phase': '分时段舰位样本', 'ordinary_event': '战斗事件',
    'replay_incoming_damage': '本舰承伤时段', 'replay_damage_window': '短时交火',
}
_BACKGROUND = '#0A1625'
_NEUTRAL = '#D4DFEA'
_RING = '#FFE586'
_SIZE = 640
_MAP_HEIGHT = 561


def _eligible(report, rows):
    sha = report.get('sha256')
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
        return
    domain = 'battle_elapsed_seconds' if report.get('multiview') else 'packet_clock'
    version = str(report.get('replay_version') or '').replace(',', '.')
    arena = report.get('arena_id')
    battle = f'arena:{version}:{arena}' if arena else 'replay:' + sha
    for row in rows:
        if (row.get('kind') not in _TITLES or not row.get('id') or row.get('source_sha') != sha
                or row.get('battle_id') != battle or row.get('clock_domain') != domain):
            continue
        start, end = row.get('clock_start'), row.get('clock_end')
        if not number(start) or not number(end) or not 0 <= start <= end:
            continue
        kind = row['kind']
        details = row.get('details') or {}
        if kind == 'local_model_checkpoint':
            if details.get('status') != 'usable':
                continue
            clocks = [details.get('clock')]
        elif kind in ('replay_consumable_timing', 'replay_consumable_overlap', 'replay_ship_phase'):
            # Select an actual activation or trajectory sample. A phase boundary or
            # client-predicted end is not itself evidence for a decision at that time.
            fields = ('clock_start', 'clock_end') if kind == 'replay_ship_phase' else ('clock_start',)
            clocks = [item.get(field) for item in details.get('support', []) for field in fields]
        elif kind in ('replay_incoming_damage', 'replay_damage_window'):
            clocks = [start, end] if start != end else [start]
        elif start == end:
            clocks = [start]
        else:
            continue
        for clock in clocks:
            if number(clock) and start <= clock <= end:
                yield row, clock


def _clock_label(domain, value):
    minutes, milliseconds = divmod(round(value * 1000), 60000)
    axis = '比赛时间' if domain == 'battle_elapsed_seconds' else '录像时间'
    return f'{axis}  {minutes:02d}:{milliseconds // 1000:02d}.{milliseconds % 1000:03d}'


def _font(size, bold=False):
    font = QFont()
    font.setFamilies(['Microsoft YaHei UI', load_presentation_fonts()['chinese_body']])
    font.setPixelSize(size)
    font.setBold(bold)
    return font


def _draw_ship(painter, canvas, center, kind, team, heading, highlighted):
    color = QColor(ABSOLUTE_COLORS.get(team, _NEUTRAL))
    if highlighted:
        painter.setPen(QPen(QColor(_RING), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(center, 12, 12)
    if heading is not None:
        tip = center + QPointF(math.cos(heading) * 15, -math.sin(heading) * 15)
        painter.setPen(QPen(QColor(_BACKGROUND), 4))
        painter.drawLine(center, tip)
        painter.setPen(QPen(color, 2))
        painter.drawLine(center, tip)
    if heading is not None and kind != '?':
        draw_ship_symbol(painter, center, kind, color, visibility='visible', heading=heading,
                         size=.72, assets=canvas.assets)
    else:
        painter.setPen(QPen(QColor(_BACKGROUND), 1.5))
        painter.setBrush(color)
        painter.drawEllipse(center, 4.5, 4.5)


def _draw_ship_names(painter, area, players, points, highlighted):
    painter.setFont(_font(12, True))
    occupied = []
    for entity in sorted(points, key=lambda key: (key not in highlighted, key)):
        center = points[entity][1]
        name = players[entity]['ship_name']
        width = painter.fontMetrics().horizontalAdvance(name) + 8
        positions = ((9, -16), (-width - 9, -16), (9, 4), (-width - 9, 4),
                     (-width / 2, -29), (-width / 2, 14), (12, -24), (-width - 12, 11))
        candidates = [QRectF(center.x() + dx, center.y() + dy, width, 15) for dx, dy in positions]
        label = next((rect for rect in candidates if area.contains(rect)
                      and not any(rect.intersects(previous) for previous in occupied)
                      and not any(rect.contains(point[1]) for key, point in points.items() if key != entity)), None)
        if label is None and entity in highlighted:
            label = next((rect for rect in candidates if area.contains(rect)
                          and not any(rect.intersects(previous) for previous in occupied)), None)
        if label is not None:
            occupied.append(label)
            color = QColor(_RING if entity in highlighted else ABSOLUTE_COLORS.get(players[entity].get('teamId'), _NEUTRAL))
            painter.fillRect(label, QColor(8, 18, 30, 235))
            painter.setPen(color.lighter(129))
            painter.drawText(label, Qt.AlignmentFlag.AlignCenter, name)


def _png(image, limit=256 * 1024):
    # Qt's fixed palette keeps detailed island textures within the image budget.
    for candidate in (image, None):
        if candidate is None:
            candidate = image.convertToFormat(QImage.Format.Format_Indexed8, Qt.ImageConversionFlag.ThresholdDither)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if candidate.save(buffer, 'PNG'):
            data = bytes(buffer.data())
            if len(data) <= limit:
                return base64.b64encode(data).decode('ascii')
    return None


def _draw_frame(canvas, observed, players, points, row, clock):
    # Render the independently configured map widget into memory. Only static map
    # layers are active; all moving marks below come from the same fresh sample set.
    canvas.clock = clock
    image = QImage(_SIZE, _SIZE, QImage.Format.Format_RGB32)
    image.fill(QColor(_BACKGROUND))
    canvas.render(image)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = canvas._canvas_rect()
        painter.save()
        painter.setClipRect(area)
        cited = set(row.get('entity_ids') or [])
        for entity in sorted(points, key=lambda key: (key in cited, key)):
            point, center = points[entity]
            heading, _ = observed.heading(entity, clock, point)
            player = players[entity]
            _draw_ship(painter, canvas, center, player['_ship_kind'], player.get('teamId'), heading, entity in cited)
        _draw_ship_names(painter, area, players, points, cited)
        painter.restore()

        painter.fillRect(QRectF(0, 0, _SIZE, 28), QColor(_BACKGROUND))
        painter.setFont(_font(17, True))
        painter.setPen(QColor('#F1F6FC'))
        kind = row['kind']
        title = _TITLES[kind]
        if kind == 'local_model_checkpoint':
            title = {'move': '走位复看点', 'fire': '开火复看点'}.get(row['details'].get('axis'), title)
        painter.drawText(QRectF(16, 1, 320, 25), Qt.AlignmentFlag.AlignVCenter, title)
        painter.drawText(QRectF(290, 1, 334, 25), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                         _clock_label(row['clock_domain'], clock))

        # The recorded evidence frame uses coordinate units for measured positions.
        if canvas.relative_grid and not canvas.full_art:
            unit = '相对航迹图 · 原始小地图坐标'
        elif canvas.full_art or not canvas.scale:
            unit = '地图坐标 · 场景单位'
        else:
            unit = '地图坐标 · 客户端公里'
        scope = '多视角对齐' if row['clock_domain'] == 'battle_elapsed_seconds' else '录制视角'
        painter.setFont(_font(14))
        painter.setPen(QColor('#E7EEF6'))
        painter.drawText(16, 575, f'{scope} · 已观测 {len(points)} 艘  |  {unit}  |  上北')
        painter.drawText(16, 601, '绿：队伍 0　红：队伍 1　灰：队伍未知　金环：所引证据涉及舰船')
        caps = '圈线为已记录占领区。' if canvas.caps else '占领区几何未知。'
        painter.setPen(QColor('#C4D3E2'))
        painter.drawText(16, 626, f'短线为已核验舰艏朝向。{caps}缺少近期观测的舰位留空。')
    finally:
        painter.end()
    return _png(image)


def _map_source(report, ship_catalog):
    catalog = ship_catalog or {}
    ships = catalog.get('ships', catalog)
    version = str(report.get('replay_version') or '').replace(',', '.')
    players = {p['shipId']: {'teamId': p.get('teamId'),
               'ship_name': ship_display_name(p.get('shipParamsId'), version, catalog),
               '_ship_kind': ship_kind(ships.get(str(p.get('shipParamsId')), {}).get('species'))}
               for p in report.get('roster') or [] if type(p.get('shipId')) is int and p['shipId'] > 0}
    observed = _ReplayObservations(report, players)
    if not observed.tracks:
        return None
    load_presentation_fonts()  # The offscreen Qt backend may have no system fonts.
    canvas = _MapCanvas(evidence_frame=True)
    canvas.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    canvas.configure(report, players, observed.version == SUPPORTED_VERSION)
    if not canvas.projection:
        return None
    canvas.resize(_SIZE, _MAP_HEIGHT)
    canvas.map_display_name = ''
    canvas.own_entity = canvas.host_team = None  # Palette represents absolute teams.
    canvas.layers = {key: key in ('grid', 'capture_points') for key in LAYER_DEFAULTS}
    if canvas.relative_grid and not observed.combat_verified:
        canvas.caps = []
    # Ship names come from game data. Player handles, file names and free-form
    # evidence are excluded from pixels.
    canvas.caps = [{**cap, 'name': cap['name'] if cap.get('name') in tuple('ABCDEFGHIJ') else ''}
                   for cap in canvas.caps if all(number(cap.get(k)) for k in ('x', 'z', 'radius')) and cap['radius'] > 0]
    tracks, canvas.tracks = canvas.tracks, {}
    return canvas, observed, players, tracks


def _map_positions(canvas, observed, tracks, clock):
    area = canvas._canvas_rect()
    points = {}
    for entity, point in observed.positions(clock).items():
        sample = _position_at(tracks.get(entity, ()), clock)
        if sample is None or sample[0] != point[0]:
            continue
        center = canvas._pixel_point(sample[3], sample[4], area)
        if area.contains(center):
            points[entity] = point, center
    return points


_TILE = 384
_TILE_HEIGHT = _TILE + 72
_GUTTER = 8
_HEADER = 78


def _sheet_size(frame_count):
    return (3 * (_TILE + _GUTTER) + _GUTTER,
            _HEADER + math.ceil(frame_count / 3) * (_TILE_HEIGHT + _GUTTER) + 32)


def _tile_origin(index):
    return (_GUTTER + (index % 3) * (_GUTTER + _TILE),
            _HEADER + (index // 3) * (_TILE_HEIGHT + _GUTTER))


def crop_replay_storyboard_tile(png, index, frame_count):
    """Extract the rendered map, recorded clock and status strip for one tile."""
    image = QImage.fromData(png, 'PNG')
    if not 0 <= index < frame_count or (image.width(), image.height()) != _sheet_size(frame_count):
        return None
    x, y = _tile_origin(index)
    return _png(image.copy(x, y, _TILE, _TILE_HEIGHT))


def _draw_recent_track(painter, canvas, observed, tracks, entity, clock):
    area = canvas._canvas_rect()
    samples = observed.states.get(entity) or []
    painter.setPen(QPen(QColor('#FFD76E'), 2.2))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for segment in tracks.get(entity, ()):
        first = bisect_left(segment, clock - 90, key=lambda p: p[0])
        last = bisect_right(segment, clock, key=lambda p: p[0])
        line, previous = [], None
        for point in segment[first:last]:
            t = point[0]
            state = observed._state_at(entity, t)
            raw = _position_at(observed.tracks[entity], t)
            valid = (raw is not None and raw[0] == t and t < observed.deaths.get(entity, math.inf)
                     and state.get('visible_to_recorder') is not False and state.get('is_alive') is not False)
            interrupted = previous is None or t - previous > 2.5
            if valid and not interrupted:
                lo = bisect_right(samples, previous, key=lambda s: s['clock'])
                hi = bisect_left(samples, t, key=lambda s: s['clock'])
                interrupted = any(s.get('visible_to_recorder') is False or s.get('is_alive') is False for s in samples[lo:hi])
            if not valid or interrupted:
                if len(line) > 1:
                    painter.drawPolyline(QPolygonF(line))
                line = []
            if valid:
                line.append(canvas._pixel_point(point[3], point[4], area))
            previous = t if valid else None
        if len(line) > 1:
            painter.drawPolyline(QPolygonF(line))


def _fresh(stamp, clock):
    return number(stamp) and 0 <= clock - stamp <= 2.5


def _ship_marks(painter, canvas, observed, players, points, states, focus, clock):
    area = canvas._canvas_rect()
    for entity in sorted(points, key=lambda key: (key == focus, key)):
        point, center = points[entity]
        heading, _ = observed.heading(entity, clock, point)
        state = states[entity]
        player = players[entity]
        color = QColor(ABSOLUTE_COLORS.get(player.get('teamId'), _NEUTRAL))
        if heading is not None:
            painter.setPen(QPen(color, 2))
            painter.drawLine(center, center + QPointF(13 * math.cos(heading), -13 * math.sin(heading)))
        if heading is not None and player['_ship_kind'] != '?':
            draw_ship_symbol(painter, center, player['_ship_kind'], color, heading=heading, visibility='visible',
                             size=.72, assets=canvas.assets,
                             hp_fraction=health_fraction(state) if _fresh(state.get('health_clock'), clock) else None)
        else:
            painter.setPen(QPen(QColor(_BACKGROUND), 1.2))
            painter.setBrush(color)
            painter.drawEllipse(center, 4.5, 4.5)
        if entity == focus:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(_RING), 2.5))
            painter.drawEllipse(center, 10, 10)
    _draw_ship_names(painter, area, players, points, {focus})


def _focus_status(state, clock, has_point):
    from ..combat_capture import LABELS

    if state.get('is_alive') is False:
        return '复盘舰已击沉', ''
    sight = ('有近期舰位' if has_point else '本视角已失去观测' if state.get('visibility') == 'not_visible'
             else '无近期舰位')
    hp = state.get('hp')
    hp_text = f"最近记录 HP {hp:g}" if number(hp) else 'HP 未记录'
    spot = state.get('spotted_by_enemy')
    spotted = ('被敌方点亮' if spot else '敌方未点亮') if type(spot) is bool and _fresh(state.get('spotted_clock'), clock) else '敌方点亮状态未知'
    items = []
    for item in state.get('consumables', ()):
        kind = item.get('type')
        if type(kind) is not int or not 0 <= kind <= 255:
            continue
        title = LABELS.get(kind, f'消耗品 {kind}')
        remaining = item.get('remaining_seconds')
        items.append(f'{title} {math.ceil(remaining)}s' if number(remaining) else title + '启用')
    return f'{sight} / {spotted} / {hp_text}', '客户端计时/启用　' + '、'.join(items) if items else ''


def _sequence_map(canvas, observed, players, tracks, intel, focus, clock):
    canvas.clock = clock
    points = _map_positions(canvas, observed, tracks, clock)
    states = {key: intel.ship_state(key, clock) for key in points}
    state = states.get(focus) or (intel.ship_state(focus, clock) if focus is not None else {})
    image = QImage(canvas.size(), QImage.Format.Format_RGB32)
    image.fill(QColor(_BACKGROUND))
    canvas.render(image)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = canvas._canvas_rect()
        painter.setClipRect(area)
        if focus in points:
            _draw_recent_track(painter, canvas, observed, tracks, focus, clock)
        _ship_marks(painter, canvas, observed, players, points, states, focus, clock)
        if not points:
            painter.setPen(QColor('#F5F7F9'))
            painter.setFont(_font(19, True))
            panel = QRectF(area.center().x() - 89, area.center().y() - 17, 178, 34)
            painter.fillRect(panel, QColor(_BACKGROUND))
            painter.drawText(panel, Qt.AlignmentFlag.AlignCenter, '缺少近期舰位')
    finally:
        painter.end()
    return image, area, len(points), _focus_status(state, clock, focus in points) if focus is not None else ('复盘舰待确认', '')


def _draw_sheet_grid(painter, rect):
    painter.setFont(_font(10, True))
    painter.setPen(QColor('#B9D5E7'))
    width, height = rect.width() / len(GRID_ROWS), rect.height() / len(GRID_ROWS)
    for index, letter in enumerate(GRID_ROWS):
        painter.drawText(QRectF(rect.left() - 14, rect.top() + index * height, 13, height),
                         Qt.AlignmentFlag.AlignCenter, letter)
        painter.drawText(QRectF(rect.left() + index * width, rect.bottom(), width, 14),
                         Qt.AlignmentFlag.AlignCenter, str(index + 1))


def _sheet(canvas, observed, players, tracks, intel, focus, page, details, state=None):
    clocks = details['frame_clocks']
    recorder = details['source'] if details.get('ship_label_mode') == 'display_name' else '录制视角'
    markers = {}
    show_grid = details.get('map_grid') == 1
    locations = {row['clock']: row for row in details.get('frame_locations', ())}
    for marker in details.get('frame_anchors', ()):
        markers.setdefault(marker['clock'], []).append(marker['label'])
    width, height = _sheet_size(len(clocks))
    sheet = QImage(width, height, QImage.Format.Format_RGB32)
    sheet.fill(QColor(_BACKGROUND))
    painter = QPainter(sheet)
    try:
        painter.setFont(_font(22, True))
        painter.setPen(QColor('#F3F7FC'))
        title = f"复盘舰 {players[focus]['ship_name']}" if focus in players else '复盘舰待确认'
        terrain = '相对坐标图 / 地形未知' if canvas.relative_grid and not canvas.full_art else '地图上方为北'
        if recorder == title.replace('复盘舰 ', '录制舰 ', 1):
            recorder = '录制视角'
        heading = f'连续小地图   /   第 {page} 页　　　{title}　　　{recorder} / 录像包时钟'
        painter.drawText(12, 27, painter.fontMetrics().elidedText(heading, Qt.TextElideMode.ElideRight, width - 24))
        painter.setFont(_font(15))
        painter.setPen(QColor('#D0DEEA'))
        team = players.get(focus, {}).get('teamId')
        relation = {team: '（我方）', 1 - team: '（敌方）'} if type(team) is int and team in (0, 1) else {}
        painter.drawText(12, 49, f'绿 队伍0{relation.get(0, "")}   红 队伍1{relation.get(1, "")}'
                         f'   灰 未知　　金环 复盘舰　金线 近90秒舰位　短线 舰艏　　{terrain}')
        caps = '圈线为记录的占领区' if canvas.caps else '占领区几何未知'
        painter.drawText(12, 70, f'拥挤处优先显示复盘舰名，同名舰按阵营和方位区分　　{caps}')
        for index, clock in enumerate(clocks):
            x, y = _tile_origin(index)
            image, area, count, (status, activations) = _sequence_map(canvas, observed, players, tracks, intel, focus, clock)
            painter.fillRect(QRectF(x, y, _TILE, _TILE_HEIGHT), QColor('#182A3B'))
            painter.setFont(_font(17, True))
            painter.setPen(QColor('#FFFFFF'))
            ts = _clock_label('packet_clock', clock).split()[-1]
            painter.drawText(x + 7, y + 20, f'{index + 1}   {ts}')
            painter.setFont(_font(14))
            caption = f'已观测 {count} 艘'
            if state is not None:
                score = [state.score(team, clock) for team in (0,1)]
                if any(value is not None for value in score):
                    caption = '比分 ' + ' : '.join(str(s) if s is not None else '—' for s in score)
            painter.drawText(QRectF(x + 200, y + 1, _TILE - 207, 24),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, caption)
            if clock in markers:
                painter.setPen(QColor('#F5E2A1'))
                mark_width = 152 if state is not None else 176
                mark = painter.fontMetrics().elidedText(' · '.join(markers[clock]), Qt.TextElideMode.ElideRight, mark_width)
                painter.drawText(QRectF(x + 122, y + 1, mark_width, 24), Qt.AlignmentFlag.AlignVCenter, mark)
            # Retain equal scale on both axes, including relative grids without a terrain texture.
            size = _TILE - 15 if show_grid else _TILE
            scale = min(size / area.width(), size / area.height())
            w, h = area.width() * scale, area.height() * scale
            target = QRectF(x + (_TILE - w) / 2 + (7 if show_grid else 0), y + 25 + (size - h) / 2, w, h)
            painter.drawImage(target, image, area)
            if show_grid:
                _draw_sheet_grid(painter, target)
            painter.setFont(_font(14))
            painter.setPen(QColor('#F5E2A1'))
            if clock in locations:
                _, separator, context = status.partition(' / ')
                status = location_label(locations[clock]) + separator + context
                status = painter.fontMetrics().elidedText(status, Qt.TextElideMode.ElideRight, _TILE - 12)
            painter.drawText(x + 6, y + _TILE + 43, status)
            painter.setPen(QColor('#D0DEEA'))
            painter.drawText(x + 6, y + _TILE + 63, activations)
        painter.setFont(_font(14))
        painter.setPen(QColor('#B9CBDA'))
        painter.drawText(12, height - 10, '观察顺序从左至右、从上至下。缺少近期位置的舰船留空；金线在轨迹间断处断开。')
    finally:
        painter.end()
    return _png(sheet, MAX_SHEET_PNG_BYTES)


def _sequence_images(report, selected, canvas, observed, players, tracks, ship_catalog):
    scopes = {tuple(row.get('entity_ids') or ()) for row in selected}
    if len(scopes) != 1:
        return []
    entities = scopes.pop()
    if len(entities) > 1:
        return []
    focus = entities[0] if entities else None
    legacy_sampling = all('sampling_version' not in (row.get('details') or {}) for row in selected)
    include_battle_state = all((row.get('details') or {}).get('battle_state_overlay') == 1 for row in selected)
    ship_names = all((row.get('details') or {}).get('ship_label_mode') == 'display_name' for row in selected)
    include_map_locations = all((row.get('details') or {}).get('map_grid') == 1 for row in selected)
    canonical = build_replay_storyboard_rows(report, focus_entity_id=focus if focus is not None else -1,
                                             legacy_sampling=legacy_sampling, include_battle_state=include_battle_state,
                                             ship_names=ship_names, include_map_locations=include_map_locations)
    by_id = {row.get('id'): row for row in selected}
    # A square native viewport gives each tile most of its 384px to the map.
    canvas.resize(_TILE + 78, _TILE + 76)
    intel = ReplayIntel(report, ship_catalog)
    state = ReplayBattleState(report) if include_battle_state else None
    frames = []
    for page, row in enumerate(canonical, 1):
        given = by_id.get(row['id'])
        if not given or any(given.get(key) != row[key] for key in
                            ('source_sha', 'battle_id', 'clock_domain', 'clock_start', 'clock_end', 'details')):
            continue
        clocks = row['details']['frame_clocks']
        png = _sheet(canvas, observed, players, tracks, intel, focus, page, row['details'], state)
        if png:
            frames.append({'evidence_id': row['id'], 'source_sha': row['source_sha'], 'battle_id': row['battle_id'],
                           'clock_domain': row['clock_domain'], 'clock': clocks[0], 'frame_clocks': clocks, 'png_base64': png})
    return frames


def build_replay_evidence_frames(report, selected_rows, *, ship_catalog=None):
    """Render selected storyboard pages or up to three historical spot illustrations."""
    if not isinstance(report, dict) or report.get('framing_error') or report.get('status') == 'failed':
        return []
    selected = list(selected_rows)
    sequences = [row for row in selected if row.get('kind') == STORYBOARD_KIND]
    candidates = list(_eligible(report, selected))
    if not candidates and not sequences:
        return []
    source = _map_source(report, ship_catalog)
    if source is None:
        return []
    canvas, observed, players, tracks = source
    if sequences:
        return _sequence_images(report, sequences, canvas, observed, players, tracks, ship_catalog)
    pool = []
    for row, clock in candidates:
        points = _map_positions(canvas, observed, tracks, clock)
        entities = row.get('entity_ids') or []
        kind = row['kind']
        if not points or (entities and not any(entity in points for entity in entities)):
            continue
        if (kind in ('replay_relative_position', 'replay_hull_angle', 'replay_consumable_overlap')
                and not all(entity in points for entity in entities)):
            continue
        if kind == 'replay_map_control' and not canvas.caps:
            continue
        if kind == 'ordinary_event' and not entities and not canvas.caps:
            continue
        pool.append((row, clock, points))

    # Keep the first usable decision point, then prefer different categories and
    # widely separated clocks. Never invent extra moments to meet the image limit.
    taken = []
    priority = list(_TITLES)
    while pool and len(taken) < 3:
        kinds = {row['kind'] for row, _, _ in taken}
        def rank(candidate):
            row, clock, _ = candidate
            gap = min((abs(clock - other) for _, other, _ in taken), default=0)
            return (row['kind'] in kinds, priority.index(row['kind']), -gap, clock)
        winner = min(pool, key=rank)
        taken.append(winner)
        pool = [item for item in pool if item[0]['id'] != winner[0]['id'] and abs(item[1] - winner[1]) >= 12]

    frames = []
    for row, clock, points in sorted(taken, key=lambda item: item[1]):
        png = _draw_frame(canvas, observed, players, points, row, clock)
        if png:
            frames.append({'evidence_id': row['id'], 'source_sha': row['source_sha'], 'battle_id': row['battle_id'],
                           'clock_domain': row['clock_domain'], 'clock': clock, 'png_base64': png})
    return frames


def library_evidence_frames(analysis, selected_rows, *, library, recordings, bundle, ship_catalog):
    """Resolve each selected source in the saved analysis, never the open replay."""
    snapshot = analysis.get('input_snapshot') or {}
    revisions = {(item.get('battle_id'), view.get('sha256')): view.get('revision')
                 for item in snapshot.get('inputs', []) for view in item.get('views', [])}
    selected_rows = list(selected_rows)
    single_view = len({(row.get('battle_id'), row.get('source_sha')) for row in selected_rows
                       if row.get('source_sha')}) == 1
    groups = {}
    for row in selected_rows:
        sha = row.get('source_sha')
        eligible = row.get('kind') in _TITLES or single_view and row.get('kind') == STORYBOARD_KIND
        if (eligible and number(row.get('clock_start')) and isinstance(sha, str)
                and re.fullmatch('[0-9a-f]{64}', sha)):
            key = (row.get('battle_id'), sha, row.get('clock_domain'))
            groups.setdefault(key, []).append(row)
    per_source = []
    for (battle, sha, domain), rows in groups.items():
        try:
            if domain == 'battle_elapsed_seconds':
                recs = {row['recording_id'] for row in rows if row.get('recording_id')}
                recs.update(item['id'] for item in snapshot.get('merged_recordings', [])
                            if item.get('battle_id') == battle and item.get('id'))
                if len(recs) != 1 or recordings is None:
                    continue
                report = recordings.load_report(recs.pop())
            elif domain == 'packet_clock':
                revision = revisions.get((battle, sha))
                if revision is None:
                    versions = library.list_analysis_versions(sha)
                    if len(versions) != 1:
                        continue  # Earlier single-replay runs may not pin a parser revision.
                    revision = versions[0]['revision']
                report = library.get_report(sha, revision)
            else:
                continue
            if report.get('sha256') != sha:
                continue
            frames = build_replay_evidence_frames(bundle.presentation_report(report), rows, ship_catalog=ship_catalog)
        except (OSError, ValueError, TypeError, IndexError, KeyError, sqlite3.DatabaseError):
            continue  # Missing or unverifiable historical sources leave their text usable.
        if frames:
            if single_view and 'frame_clocks' in frames[0]:
                return frames
            per_source.append(frames)
            if len(per_source) == 3:
                break
    # One per represented battle first; then fill unused slots in time order.
    return [frames[i] for i in range(3) for frames in per_source if i < len(frames)][:3]
