"""Deterministic, evidence-linked collection and training analysis.

Public functions accept/return JSON-compatible values and perform no I/O.
Absolute team IDs are sides of an arena, never inferred cross-battle identities.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math
import re

from .analysis_spatial import analyze_spatial, timeline
from .contracts import parse_version
from .replay_intel import ReplayIntel, number


SCHEMA_VERSION = 1
RULES_VERSION = 'p2p3-observed-1'
DEFAULT_RULES = {'version': RULES_VERSION, 'time_basis': 'battle_elapsed', 'window_start_sec': 0.0,
                 'window_end_sec': 180.0, 'max_gap_sec': 3.0, 'sample_step_sec': 1.0, 'min_coverage': 0.8,
                 'rally_radius_m': 3000.0, 'zone_commitment_ships': 2, 'min_pattern_battles': 2,
                 'spawn_side_mode': 'explicit', 'spawn_window_sec': 10.0, 'spawn_offset_fraction': 0.1,
                 'support_triggers': []}


def _hash(value, prefix=''):
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return prefix + hashlib.sha256(raw).hexdigest()[:24]


def _rules(value=None):
    if value is not None and not isinstance(value, dict):
        raise ValueError('rules 必须是对象')
    result = deepcopy(DEFAULT_RULES)
    result.update(deepcopy(value or {}))
    if not isinstance(result['version'], str) or not result['version'].strip():
        raise ValueError('规则必须有版本')
    if result['time_basis'] not in ('battle_elapsed', 'packet_clock'):
        raise ValueError('时间基准必须是 battle_elapsed 或 packet_clock')
    for key in ('window_start_sec', 'window_end_sec', 'max_gap_sec', 'sample_step_sec', 'min_coverage', 'rally_radius_m', 'spawn_window_sec', 'spawn_offset_fraction'):
        if not number(result[key]):
            raise ValueError(f'规则 {key} 必须是有限数值')
    if not 0 <= result['window_start_sec'] < result['window_end_sec'] <= 86400:
        raise ValueError('分析窗口无效')
    if not 0 < result['max_gap_sec'] <= 3 or not .1 <= result['sample_step_sec'] <= 3:
        raise ValueError('观测新鲜度不得放宽超过 3 秒；采样步长为 0.1 至 3 秒')
    if not 0 < result['min_coverage'] <= 1 or result['rally_radius_m'] <= 0:
        raise ValueError('覆盖率或距离阈值无效')
    if result['spawn_side_mode'] not in ('explicit', 'observed_centroid') or not 0 < result['spawn_window_sec'] <= 10 or not 0 < result['spawn_offset_fraction'] <= .5:
        raise ValueError('开局观测方位规则无效')
    for key in ('zone_commitment_ships', 'min_pattern_battles'):
        if type(result[key]) is not int or result[key] < 1:
            raise ValueError(f'{key} 必须为正整数')
    if not isinstance(result['support_triggers'], list):
        raise ValueError('support_triggers 必须是列表')
    for trigger in result['support_triggers']:
        if not isinstance(trigger, dict) or not trigger.get('id') or not trigger.get('zone_id'):
            raise ValueError('支援触发项缺少 id 或 zone_id')
        if not number(trigger.get('trigger_sec')) or not number(trigger.get('end_sec')) or not result['window_start_sec'] <= trigger['trigger_sec'] < trigger['end_sec'] <= result['window_end_sec']:
            raise ValueError('支援窗口必须位于分析窗口内')
    return result


def _version(report):
    if not isinstance(report.get('replay_version'), str):
        return None
    try:
        parts = parse_version(report.get('replay_version'))
        return '.'.join(map(str, parts)) if len(parts) == 4 and parts[-1] > 0 else None
    except (TypeError, ValueError):
        return None


def _map(report):
    meta = report.get('metadata') or {}
    raw = meta.get('mapName') or meta.get('mapDisplayName') or ((report.get('geometry_analysis') or {}).get('png_projection') or {}).get('map_id')
    return str(raw).replace('\\', '/').rstrip('/').split('/')[-1] if raw else None


def _battle_id(report):
    version, arena = _version(report), report.get('arena_id')
    if version and str(arena).isdigit() and int(arena) > 0:
        return f'arena:{version}:{int(arena)}'
    return 'replay:' + str(report.get('sha256'))


def _signature(report):
    rows = report.get('roster') or []
    return sorted([(str(p.get('accountDBID') or p.get('name') or ''), str(p.get('shipParamsId')), str(p.get('teamId')))
                   for p in rows if type(p.get('shipId')) is int])


def _layout(report):
    caps = report.get('capture_points')
    if not isinstance(caps, list) or not caps:
        explicit = (report.get('analysis_context') or {}).get('layout_id')
        return str(explicit) if explicit else None
    rows = []
    for cap in caps:
        if not all(number(cap.get(key)) for key in ('x', 'z', 'radius')) or cap['radius'] <= 0:
            return None
        rows.append([str(cap.get('name', '')), round(cap['x'], 5), round(cap['z'], 5), round(cap['radius'], 5)])
    return _hash(sorted(rows), 'capture-layout:')


def _roster(report):
    return [p for p in report.get('roster') or [] if type(p.get('shipId')) is int and type(p.get('teamId')) is int]


def _team(report, selector):
    if not isinstance(selector, dict):
        return [], {'status': 'unknown', 'reason': '请明确选择队伍或逐局绝对侧'}
    roster = _roster(report)
    if selector.get('recording_side') is True:
        side = report.get('host_absolute_team')
        players = [p for p in roster if p['teamId'] == side] if type(side) is int and side in (0, 1) else []
        return players, {'status': 'selected' if players else 'unknown', 'team_id': side if players else None,
                         'scope': 'per_battle_recorder_side', 'label': '每局录制方',
                         'reason': None if players else '录像缺少可核对的录制方阵营'}
    by_battle = selector.get('by_battle')
    if isinstance(by_battle, dict):
        team = by_battle.get(_battle_id(report))
        players = [p for p in roster if p['teamId'] == team] if type(team) is int else []
        return players, {'status': 'selected' if players else 'unknown', 'team_id': team, 'scope': 'user_selected_per_battle', 'label': selector.get('label')}
    if 'clan_tag' in selector:
        tag = selector['clan_tag']
        if not isinstance(tag, str) or not tag:
            raise ValueError('clan_tag 必须是非空字符串')
        matches = [p for p in roster if (p.get('clan_tag') or p.get('clanTag')) == tag]
        sides = {p['teamId'] for p in matches}
        # An incomplete clan annotation must not silently select a subset of a side.
        players = matches if len(sides) == 1 and len(matches) == sum(p['teamId'] in sides for p in roster) else []
        return players, {'status': 'selected' if players else 'unknown', 'clan_tag': tag,
                         'team_id': next(iter(sides)) if len(sides) == 1 else None, 'scope': 'explicit_clan_tag',
                         'rule_source': (report.get('analysis_context') or {}).get('team_rule_source'),
                         'reason': None if players else '标签缺失、不完整或出现在双方；不从昵称猜测队伍'}
    if 'team_id' in selector:
        if type(selector['team_id']) is not int:
            raise ValueError('team_id 必须是整数')
        players = [p for p in roster if p['teamId'] == selector['team_id']]
        return players, {'status': 'selected' if players else 'unknown', 'team_id': selector['team_id'], 'scope': 'absolute_side_only',
                         'reason': '只选择每局该绝对侧，不表示跨局同一队伍'}
    return [], {'status': 'unknown', 'reason': '不支持的队伍选择条件'}


def _simple_polygon(vertices):
    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    edges = list(zip(vertices, vertices[1:] + vertices[:1]))
    for i, (a, b) in enumerate(edges):
        if a == b:
            return False
        for j, (c, d) in enumerate(edges):
            if j <= i or j == i + 1 or (i == 0 and j == len(edges) - 1):
                continue
            box_overlap = (max(min(a[0], b[0]), min(c[0], d[0])) <= min(max(a[0], b[0]), max(c[0], d[0]))
                           and max(min(a[1], b[1]), min(c[1], d[1])) <= min(max(a[1], b[1]), max(c[1], d[1])))
            if box_overlap and cross(a, b, c) * cross(a, b, d) <= 0 and cross(c, d, a) * cross(c, d, b) <= 0:
                return False
    return True


def _zone_config(report, zones):
    map_id = _map(report)
    if zones is None:
        rows = [{'id': 'capture:' + str(cap.get('name') or cap.get('entity_id')), 'name': '占领区 ' + str(cap.get('name', '?')),
                 'version': 'recorded-capture-layout-1', 'map_id': map_id, 'shape': 'circle', 'center': [cap['x'], cap['z']],
                 'radius_engine': cap['radius'], 'coordinate_units': 'engine', 'source': 'recorded_capture_point',
                 'tactical_role': 'unassigned'} for cap in report.get('capture_points') or []
                if all(number(cap.get(k)) for k in ('x', 'z', 'radius')) and cap['radius'] > 0]
        return sorted(rows, key=lambda row: row['id'])
    if not isinstance(zones, dict) or not isinstance(zones.get('version'), str) or not zones['version'] or not isinstance(zones.get('zones'), list):
        raise ValueError('区域配置需要 version 和 zones 列表')
    result = []
    ids = set()
    for supplied in zones['zones']:
        if not isinstance(supplied, dict):
            raise ValueError('每个区域必须是对象')
        zone = deepcopy(supplied)
        if not isinstance(zone.get('id'), str) or not zone['id'] or zone['id'] in ids:
            raise ValueError('区域 id 缺失或重复')
        ids.add(zone['id'])
        if zone.get('coordinate_units') != 'engine' or not zone.get('map_id'):
            raise ValueError('区域必须绑定地图和 engine 原始坐标')
        zone['version'] = zones['version']
        zone.setdefault('name', zone['id'])
        if zone.get('shape') == 'polygon':
            vertices = zone.get('points')
            if not isinstance(vertices, list) or len(vertices) < 3 or any(not isinstance(p, (list, tuple)) or len(p) != 2 or not all(number(v) for v in p) for p in vertices):
                raise ValueError('多边形至少需要三个有限坐标顶点')
            if vertices[0] == vertices[-1]:
                vertices = vertices[:-1]
                zone['points'] = vertices
            if len(vertices) < 3 or not _simple_polygon(vertices):
                raise ValueError('多边形不能自交或包含重复边')
            area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(vertices, vertices[1:] + vertices[:1]))
            if abs(area) < 1e-8:
                raise ValueError('区域多边形面积为零')
        elif zone.get('shape') == 'circle':
            if not isinstance(zone.get('center'), list) or len(zone['center']) != 2 or not all(number(v) for v in zone['center']) or not number(zone.get('radius_engine')) or zone['radius_engine'] <= 0:
                raise ValueError('圆形区域坐标或半径无效')
        else:
            raise ValueError('区域形状只支持 polygon/circle')
        if zone['map_id'] == map_id:
            result.append(zone)
    return sorted(result, key=lambda row: row['id'])


class _Evidence:
    def __init__(self, rules):
        self.rules = rules
        self.rows = {}

    def add(self, report, kind, text, *, start=None, end=None, entities=None, details=None):
        row = {'source_sha': report.get('sha256'), 'battle_id': _battle_id(report), 'arena_id': report.get('arena_id'),
               'clock_start': start, 'clock_end': end, 'kind': kind, 'entity_ids': sorted(entities or []), 'text': text,
               'details': dict(details or {}, rules_version=self.rules['version'], time_basis=self.rules['time_basis'])}
        if report.get('multiview'):
            row['clock_domain'] = 'battle_elapsed_seconds'
            row['original_source_shas'] = list(report['multiview']['source_shas'])
            row['recording_id'] = (report.get('analysis_context') or {}).get('source_recording_id')
        row['id'] = _hash([row, self.rules], 'evidence:')
        self.rows[row['id']] = row
        return row['id']


def _fusion(views):
    result = {'view_count': len(views), 'fusion_performed': False, 'field_fusion_validated': False,
              'status': 'not_applicable' if len(views) < 2 else 'unvalidated', 'conflicts': [],
              'policy': '仅选择代表录制；不合并位置、伤害、事件或消耗品'}
    if len(views) < 2:
        return result
    for name, fn in (('version', _version), ('map', _map), ('roster', _signature), ('layout', _layout)):
        values = [fn(v) for v in views]
        if any(v != values[0] for v in values[1:]):
            result['conflicts'].append(name)
        if any(v is None or v == [] for v in values):
            result.setdefault('missing', []).append(name)
    clocks = [{row['battle_elapsed_sec']: row['packet_clock'] for row in (v.get('time_analysis') or {}).get('observations', [])
               if number(row.get('battle_elapsed_sec')) and number(row.get('packet_clock')) and row.get('stage_name') == 'STARTED'
               and not row.get('diagnostics')} for v in views]
    if any(len({row.get('battle_segment_id') for row in (v.get('time_analysis') or {}).get('observations', [])
                if number(row.get('battle_elapsed_sec'))}) > 1 for v in views):
        result['conflicts'].append('time_domains')
    common = set.intersection(*(set(rows) for rows in clocks))
    if len(common) < 2:
        result.setdefault('missing', []).append('shared_time_anchors')
    else:
        offsets = [max(rows[t] for rows in clocks) - min(rows[t] for rows in clocks) for t in common]
        result['max_anchor_packet_clock_difference_sec'] = max(offsets)
        if max(offsets) > 1.0:
            result['conflicts'].append('time_anchors')
    result['status'] = 'conflict' if result['conflicts'] else 'unvalidated' if result.get('missing') else 'ready_unvalidated'
    return result


def _spawn(report, team, entities, rules, intel):
    context = report.get('analysis_context') or {}
    explicit = (context.get('team_spawn_sides') or {}).get(str(team.get('team_id'))) or context.get('spawn_side')
    if explicit:
        return {'value': str(explicit), 'source': 'explicit_user_annotation'}
    if rules['spawn_side_mode'] != 'observed_centroid' or not intel.geometry_verified or not entities:
        return {'value': None, 'source': 'unknown'}
    bounds = ((report.get('geometry_analysis') or {}).get('png_projection') or {}).get('viewport_bounds_engine') or {}
    if not all(number(bounds.get(k)) for k in ('min_x', 'max_x', 'min_z', 'max_z')):
        return {'value': None, 'source': 'unknown'}
    short = min(bounds['max_x'] - bounds['min_x'], bounds['max_z'] - bounds['min_z'])
    cx, cz = (bounds['min_x'] + bounds['max_x']) / 2, (bounds['min_z'] + bounds['max_z']) / 2
    early_rules = dict(rules, time_basis='battle_elapsed', window_start_sec=0, window_end_sec=rules['spawn_window_sec'])
    for anchor in timeline(report, early_rules):
        points = {e: p for e, p in intel.positions(anchor['clock']).items() if e in entities and p.get('quality') == 'recorded'
                  and 0 <= anchor['clock'] - p['observation_clock'] < rules['max_gap_sec']}
        if len(points) / len(entities) < rules['min_coverage']:
            continue
        x = sum(p['x_engine'] for p in points.values()) / len(points)
        z = sum(p['z_engine'] for p in points.values()) / len(points)
        dx, dz = x - cx, z - cz
        if max(abs(dx), abs(dz)) < short * rules['spawn_offset_fraction']:
            continue
        side = ('N' if dz > 0 else 'S') if abs(dz) >= abs(dx) else ('E' if dx > 0 else 'W')
        return {'value': side, 'source': 'observed_centroid', 'label': '开局观测方位（非官方出生侧）',
                'clock': anchor['clock'], 'battle_elapsed_sec': anchor['time_sec'], 'centroid_engine': [x, z],
                'entity_ids': sorted(points), 'observed_entities': len(points), 'selected_entities': len(entities)}
    return {'value': None, 'source': 'unknown', 'reason': '前 10 秒有效队伍位置不足或质心接近地图中心'}


def _consumables(report, entities, intel, evidence):
    maximum = (report.get('packet_clock_range') or {}).get('max')
    windows, unknown = [], []
    for row in intel.consumables:
        if row.get('entity_id') not in entities:
            continue
        start = row['start_clock']
        end, source = row.get('end_clock'), 'observed_end'
        if not number(end):
            end, source = row.get('expected_end_clock'), 'client_reported_timer'
        if number(maximum) and number(end) and end > maximum:
            end, source = maximum, 'clipped_at_recording_end'
        event = {'entity_id': row['entity_id'], 'type': row.get('type'), 'label': row.get('label'),
                 'clock_start': start, 'clock_end': end if number(end) and end >= start else None,
                 'end_source': source if number(end) and end >= start else 'unknown'}
        event['evidence_ids'] = [evidence.add(report, 'consumable_window', '已记录的消耗品启用及可用时间窗口', start=start, end=event['clock_end'], entities=[row['entity_id']], details=event)]
        (windows if event['clock_end'] is not None else unknown).append(event)
    groups = defaultdict(list)
    for row in windows:
        groups[str(row['type'])].append(row)
    connections = []
    for kind, rows in groups.items():
        rows.sort(key=lambda r: (r['clock_start'], r['entity_id']))
        end = None
        prior = None
        for row in rows:
            if end is not None:
                connections.append({'type': kind, 'clock_start': min(end, row['clock_start']), 'clock_end': max(end, row['clock_start']),
                                     'previous_window_end_clock': end, 'next_window_start_clock': row['clock_start'],
                                     'gap_sec': row['clock_start'] - end, 'evidence_ids': prior['evidence_ids'] + row['evidence_ids'],
                                     'quality': 'between_recorded_windows_only',
                                     'interpretation': '正值为已记录窗口间隔，负值为重叠；缺记录不证明全队能力空窗'})
            if end is None or row['clock_end'] > end:
                end, prior = row['clock_end'], row
    return {'status': 'partial' if windows or unknown else 'unknown', 'windows': windows, 'unknown_end_activations': unknown,
            'connections': connections, 'time_basis': 'packet_clock', 'coverage': 'event_records_only_not_full_team_coverage'}


def _ship_usage(battles):
    count = Counter()
    appearances = defaultdict(set)
    refs = defaultdict(set)
    for battle in battles:
        for row in battle['ship_appearances']:
            count[row['ship_id']] += row['count']
            appearances[row['ship_id']].add(battle['battle_id'])
            refs[row['ship_id']].update(row['evidence_ids'])
    denominator = sum(count.values())
    return [{'ship_id': ship, 'appearance_count': total, 'appearance_denominator': denominator,
             'denominator_scope': 'selected_observed_roster_only',
             'appearance_share': total / denominator if denominator else None, 'battle_count': len(appearances[ship]),
             'battle_denominator': len(battles), 'battle_share': len(appearances[ship]) / len(battles) if battles else None,
             'evidence_ids': sorted(refs[ship])} for ship, total in sorted(count.items())]


def analyze_collection(reports, *, ship_catalog=None, team_selector=None, zones=None, rules=None):
    rules = _rules(rules)
    evidence = _Evidence(rules)
    output = {'schema_version': SCHEMA_VERSION, 'kind': 'collection_analysis', 'status': 'unknown', 'rules_version': rules['version'],
              'rules': rules, 'team_selector': deepcopy(team_selector), 'counts': {}, 'battles': [], 'groups': [], 'participants': [],
              'ship_usage': [], 'exclusions': [], 'fusion_checks': [], 'evidence': [], 'limitations': [
                  '仅代表导入集合；选船频率不是完整赛季禁选率，覆盖率不是统计置信度。',
                  '同场只计一次阵容；有当前已保存双边记录时采用其对齐观测，否则选择代表录制，不直接相加各来源事件。',
                  '不推断同目标集火、指挥意图、违令、有效火力支援或胜率。',
                  '绝对 team_id 仅代表每局一侧；只有显式队伍标签或逐局选择才指向目标队伍。']}
    reports = list(reports)
    views = {}
    for report in reports:
        sha = report.get('sha256') if isinstance(report, dict) else None
        if not isinstance(sha, str) or not re.fullmatch('[0-9a-fA-F]{64}', sha) or report.get('status') == 'failed':
            output['exclusions'].append({'source_sha': sha, 'reason': '缺少有效回放 SHA256 或解析失败'})
            continue
        sha = sha.lower()
        report = dict(report, sha256=sha)
        # Deterministic representative for duplicate documents of the same recording.
        score = (bool(report.get('combat')), len(report.get('roster') or []), _hash(report))
        if sha not in views or score > views[sha][0]:
            views[sha] = (score, report)
    grouped = defaultdict(list)
    for _, report in views.values():
        grouped[_battle_id(report)].append(report)
    for battle_id, candidates in sorted(grouped.items()):
        candidates.sort(key=lambda r: (-((r.get('packet_clock_range') or {}).get('max') if number((r.get('packet_clock_range') or {}).get('max')) else 0), r['sha256']))
        selected = candidates[0]
        fusion = _fusion(candidates)
        merged = selected.get('multiview') if len(candidates) == 1 else None
        if merged:
            fusion = {'view_count': len(merged['source_shas']), 'fusion_performed': True, 'field_fusion_validated': True,
                      'status': 'saved_aligned_recording', 'conflicts': [], 'policy': '已保存的逐比赛计时锚点对齐；按所属侧取观测，保留来源与未知',
                      'engine_version': merged.get('engine_version'), 'observation_conflict_counts': merged.get('conflict_counts', {})}
        output['fusion_checks'].append(dict(fusion, battle_id=battle_id, source_shas=merged['source_shas'] if merged else [r['sha256'] for r in candidates]))
        if fusion['status'] == 'conflict':
            output['exclusions'].append({'battle_id': battle_id, 'reason': '同场来源结构或时间冲突', 'conflicts': fusion['conflicts']})
            continue
        players, team = _team(selected, team_selector)
        if not players:
            output['exclusions'].append({'battle_id': battle_id, 'source_sha': selected['sha256'], 'reason': team.get('reason') or '未明确选择队伍'})
            continue
        intel = ReplayIntel(selected, ship_catalog)
        entity_ids = [p['shipId'] for p in players]
        selected_zones = _zone_config(selected, zones)
        if any(t['zone_id'] not in {z['id'] for z in selected_zones} for t in rules['support_triggers']):
            raise ValueError('支援窗口引用了当前地图不存在的区域')
        spatial = analyze_spatial(selected, entity_ids, selected_zones, rules, ship_catalog=ship_catalog, intel=intel)
        spawn = _spawn(selected, team, entity_ids, rules, intel)
        metadata = selected.get('metadata') or {}
        mode = {key: metadata.get(key) for key in ('gameMode', 'gameType', 'matchGroup', 'scenario')}
        conditions = {'map_id': _map(selected), 'version': _version(selected), 'mode': mode, 'layout_id': _layout(selected),
                      'spawn_side': spawn.get('value'), 'spawn_source': spawn['source'], 'team_scope': team['scope'],
                      'clan_tag': team.get('clan_tag'), 'rules_fingerprint': _hash(rules), 'zones_fingerprint': _hash(selected_zones)}
        comparable = bool(conditions['map_id'] and conditions['version'] and conditions['layout_id'] and spawn.get('value') and metadata.get('gameMode') is not None)
        comparison_key = _hash(conditions if comparable else [conditions, battle_id], 'group:')
        participants = [{'entity_id': p['shipId'], 'name': p.get('name'), 'ship_id': str(p.get('shipParamsId')),
                         'team_id': p['teamId'], 'clan_tag': p.get('clan_tag') or p.get('clanTag'),
                         'confirmed_player_id': (p.get('resolved_identity') or {}).get('player_id')
                             if (p.get('resolved_identity') or {}).get('status') == 'confirmed' else None} for p in players]
        battle = {'battle_id': battle_id, 'source_sha': selected['sha256'], 'arena_id': selected.get('arena_id'), 'view_count': len(merged['source_shas']) if merged else len(candidates),
                  'representative_policy': 'saved_multiview_owner_preferred; battle_elapsed_axis' if merged else 'longest_recording_then_sha; no_fusion', 'team': team, 'status': spatial['status'],
                  'comparison_key': comparison_key, 'comparable': comparable, 'conditions': conditions, 'spawn_side': spawn,
                  'participants': participants, 'coverage': spatial['coverage'], 'zones': selected_zones, 'spatial': spatial,
                  'roster_quality': {'parsed_participants': len(_roster(selected)), 'header_participants': selected.get('header_roster_count'),
                                     'matches_header': selected.get('roster_matches_header'), 'selected_observed_participants': len(players)},
                  'ship_appearances': [], 'evidence_ids': []}
        catalog = (ship_catalog or {}).get('ships', ship_catalog or {})
        for ship, n in sorted(Counter(str(p.get('shipParamsId')) for p in players).items()):
            members = [p['shipId'] for p in players if str(p.get('shipParamsId')) == ship]
            label = catalog.get(ship, {}).get('display_name') or catalog.get(ship, {}).get('name') or ship
            ref = evidence.add(selected, 'ship_appearance', f'{label}：本局所选一侧出场 {n} 艘', entities=members,
                               details={'ship_id': ship, 'ship_name': label, 'count': n, 'denominator': len(players)})
            battle['ship_appearances'].append({'ship_id': ship, 'ship_name': label, 'count': n, 'evidence_ids': [ref]})
        if spawn.get('clock') is not None:
            spawn['evidence_ids'] = [evidence.add(selected, 'observed_spawn_orientation', spawn['label'], start=spawn['clock'], end=spawn['clock'], entities=spawn['entity_ids'], details=spawn)]
        for ent in spatial['entities']:
            for region in ent['zones']:
                spans = region['evidence_spans']
                definition = next(z for z in selected_zones if z['id'] == region['zone_id'])
                region['evidence_ids'] = [evidence.add(selected, 'region_dwell', f"{intel.name(ent['entity_id'])} 在 {definition['name']} 的观测采样驻留",
                    start=spans[0]['clock_start'] if spans else None, end=spans[-1]['clock_end'] if spans else None, entities=[ent['entity_id']],
                    details={'zone_id': region['zone_id'], 'zone_version': definition['version'], 'zone_definition': definition,
                             'inside_seconds': region['inside_seconds'], 'observed_seconds': ent['observed_seconds'],
                             'coverage_ratio': ent['coverage_ratio'], 'quality': 'sampled_observation', 'spans': spans})]
            for transfer in ent['transitions']:
                transfer['evidence_ids'] = [evidence.add(selected, 'region_transfer', f"{intel.name(ent['entity_id'])} 的区域转场记录及缺口", start=transfer['departure_clock'], end=transfer['arrival_clock'], entities=[ent['entity_id']], details=transfer)]
        for row in spatial['support_arrivals']:
            row['evidence_ids'] = [evidence.add(selected, 'support_arrival', f"{intel.name(row['entity_id'])} 的支援目标区域到达记录，不证明有效支援", start=row['clock_start'], end=row['clock_end'], entities=[row['entity_id']], details=row)]
        for zone in selected_zones:
            observations = [r for r in spatial['deployment'] if r['zone_id'] == zone['id'] and r['coverage_ratio'] >= rules['min_coverage']]
            peak = max(observations, key=lambda r: (r['count'], -r['clock'])) if observations else None
            if peak:
                peak.update(zone_version=zone['version'], zone_definition=zone)
                peak['evidence_ids'] = [evidence.add(selected, 'zone_commitment', '所选队伍在区域中的观测投入数量', start=peak['clock'], end=peak['clock'], entities=peak['entity_ids'], details=peak)]
        rally = [r for r in spatial['rally'] if r['max_pair_distance_m'] is not None and r['observed_entities'] / len(entity_ids) >= rules['min_coverage']]
        if rally:
            tight = min(rally, key=lambda r: (r['max_pair_distance_m'], r['clock']))
            tight['evidence_ids'] = [evidence.add(selected, 'fleet_geometry', '同一观测时刻的舰船两两距离', start=tight['clock'], end=tight['clock'], entities=tight['entity_ids'], details=tight)]
        battle['consumable_windows'] = _consumables(selected, entity_ids, intel, evidence)
        battle['evidence_ids'] = sorted(e['id'] for e in evidence.rows.values() if e['battle_id'] == battle_id)
        output['battles'].append(battle)
        output['participants'].extend(dict(p, battle_id=battle_id, source_sha=selected['sha256']) for p in participants)
    by_group = defaultdict(list)
    for battle in output['battles']:
        by_group[battle['comparison_key']].append(battle)
    for key, battles in sorted(by_group.items()):
        group = {'id': key, 'conditions': battles[0]['conditions'], 'comparable': battles[0]['comparable'],
                 'battle_ids': [b['battle_id'] for b in battles], 'ship_usage': _ship_usage(battles), 'patterns': [],
                 'coverage': {'battle_count': len(battles), 'spatial_eligible_battles': sum((b['coverage']['ratio'] or 0) >= rules['min_coverage'] for b in battles)}}
        for zone in battles[0]['zones']:
            eligible, matches, refs = [], [], []
            for battle in battles:
                if (battle['coverage']['ratio'] or 0) < rules['min_coverage']:
                    continue
                eligible.append(battle['battle_id'])
                rows = [r for r in battle['spatial']['deployment'] if r['zone_id'] == zone['id'] and r['coverage_ratio'] >= rules['min_coverage'] and r.get('evidence_ids')]
                if rows:
                    peak = max(rows, key=lambda r: r['count'])
                    refs.extend(peak['evidence_ids'])
                    if peak['count'] >= rules['zone_commitment_ships']:
                        matches.append(battle['battle_id'])
            group['patterns'].append({'id': _hash([key, zone['id'], rules], 'pattern:'), 'rule_id': 'zone_commitment',
                'label': f"区域 {zone['name']} 至少投入 {rules['zone_commitment_ships']} 艘（观测规则）", 'zone_id': zone['id'],
                'rules_version': rules['version'], 'numerator': len(matches), 'denominator': len(eligible),
                'ratio': len(matches) / len(eligible) if eligible else None, 'matching_battle_ids': matches,
                'exception_battle_ids': sorted(set(eligible) - set(matches)), 'excluded_battle_ids': sorted(set(group['battle_ids']) - set(eligible)),
                'status': 'observed_pattern' if group['comparable'] and len(eligible) >= rules['min_pattern_battles'] else 'insufficient_samples',
                'coverage': {'eligible_battles': len(eligible), 'selected_battles': len(battles), 'minimum_position_ratio': rules['min_coverage']},
                'evidence_ids': sorted(set(refs))})
        eligible, matches, refs = [], [], []
        for battle in battles:
            rows = [r for r in battle['spatial']['rally'] if r.get('evidence_ids')]
            if (battle['coverage']['ratio'] or 0) >= rules['min_coverage'] and rows:
                eligible.append(battle['battle_id'])
                tight = min(rows, key=lambda r: r['max_pair_distance_m'])
                refs.extend(tight['evidence_ids'])
                if tight['max_pair_distance_m'] <= rules['rally_radius_m']:
                    matches.append(battle['battle_id'])
        group['patterns'].append({'id': _hash([key, 'fleet_concentration', rules], 'pattern:'), 'rule_id': 'fleet_concentration',
            'label': f"窗口内观测舰船最大两两距离不超过 {rules['rally_radius_m']:g} 米（几何规则）", 'zone_id': None,
            'rules_version': rules['version'], 'numerator': len(matches), 'denominator': len(eligible),
            'ratio': len(matches) / len(eligible) if eligible else None, 'matching_battle_ids': matches,
            'exception_battle_ids': sorted(set(eligible) - set(matches)), 'excluded_battle_ids': sorted(set(group['battle_ids']) - set(eligible)),
            'status': 'observed_pattern' if group['comparable'] and len(eligible) >= rules['min_pattern_battles'] else 'insufficient_samples',
            'coverage': {'eligible_battles': len(eligible), 'selected_battles': len(battles), 'minimum_position_ratio': rules['min_coverage']},
            'evidence_ids': sorted(set(refs))})
        output['groups'].append(group)
    output['counts'] = {'input_reports': len(reports), 'unique_views': len(views), 'duplicate_inputs': len(reports) - len(views) - sum(e.get('reason') == '缺少有效回放 SHA256 或解析失败' for e in output['exclusions']),
                        'battles': len(grouped), 'selected_battles': len(output['battles']), 'comparison_groups': len(output['groups'])}
    if any(report.get('multiview') for _, report in views.values()):
        source_shas = {sha for _, report in views.values() for sha in (report.get('multiview') or {}).get('source_shas', [report['sha256']])}
        output['counts']['unique_views'] = len(source_shas)
        output['counts']['merged_battles'] = sum(bool(report.get('multiview')) for _, report in views.values())
    output['ship_usage'] = _ship_usage(output['battles'])
    output['evidence'] = sorted(evidence.rows.values(), key=lambda e: e['id'])
    output['status'] = 'partial' if output['battles'] else 'unknown'
    return output


def _target_players(players, selector, roster=None):
    if not isinstance(selector, dict) or len(selector) != 1:
        raise ValueError('计划对象 selector 必须且只能选择 player_id/entity_id/name/ship_id/all_team 一项')
    if selector.get('all_team') is True:
        return players
    if 'player_id' in selector and isinstance(selector['player_id'], str) and selector['player_id']:
        matches = [p for p in (roster if roster is not None else players)
                   if (p.get('resolved_identity') or {}).get('status') == 'confirmed'
                   and (p.get('resolved_identity') or {}).get('player_id') == selector['player_id']]
        return matches if len(matches) == 1 and matches[0]['shipId'] in {p['shipId'] for p in players} else []
    if 'entity_id' in selector and type(selector['entity_id']) is int:
        return [p for p in players if p['shipId'] == selector['entity_id']]
    if 'name' in selector and isinstance(selector['name'], str):
        matches = [p for p in players if p.get('name') == selector['name']]
        return matches if len(matches) == 1 else []
    if 'ship_id' in selector:
        return [p for p in players if str(p.get('shipParamsId')) == str(selector['ship_id'])]
    raise ValueError('计划对象 selector 无效')


def _covered_between(intervals, start, end):
    if end < start:
        return False
    cursor = start
    for row in intervals:
        if row['time_end'] < cursor:
            continue
        if row['time_start'] > cursor + 1e-8:
            return False
        cursor = max(cursor, row['time_end'])
        if cursor >= end - 1e-8:
            return True
    return False


def evaluate_plan(report, plan, zones, *, ship_catalog=None, team_selector=None):
    if not isinstance(report, dict) or report.get('status') == 'failed' or not re.fullmatch('[0-9a-fA-F]{64}', str(report.get('sha256', ''))):
        raise ValueError('计划评估需要有效回放 SHA256 与已解析报告')
    if not isinstance(plan, dict) or not isinstance(plan.get('version'), str) or not plan['version'] or not isinstance(plan.get('targets'), list):
        raise ValueError('计划需要 version 和 targets 列表')
    if len(plan['targets']) > 100:
        raise ValueError('每份计划最多 100 个目标')
    basis = plan.get('time_basis', 'battle_elapsed')
    rules = _rules(dict(plan.get('rules') or {}, time_basis=basis))
    registry = _Evidence(rules)
    players, team = _team(report, team_selector)
    selected_zones = _zone_config(report, zones)
    intel = ReplayIntel(report, ship_catalog)
    output = {'schema_version': 1, 'kind': 'plan_evaluation', 'status': 'unknown', 'rules_version': rules['version'],
              'rules': rules, 'plan_version': plan['version'], 'time_basis': basis, 'battle_id': _battle_id(report),
              'source_sha': report.get('sha256'), 'team': team, 'results': [], 'evidence': [],
              'limitations': ['仅核对人工计划与可观测到达时间；不判定指挥因果、违令或训练效果。',
                              '首次进入缺失、过界区间跨越允许边界或存在失观时，结果为无法判断。']}
    output['participants'] = [{'entity_id': p['shipId'], 'name': p.get('name'), 'ship_id': str(p.get('shipParamsId')),
                               'team_id': p['teamId'], 'clan_tag': p.get('clan_tag') or p.get('clanTag'),
                               'source_sha': report.get('sha256'), 'battle_id': _battle_id(report),
                               'confirmed_player_id': (p.get('resolved_identity') or {}).get('player_id')
                                   if (p.get('resolved_identity') or {}).get('status') == 'confirmed' else None} for p in players]
    end_values = [row.get('battle_elapsed_sec') for row in (report.get('time_analysis') or {}).get('observations', [])]
    maximum = max((v for v in end_values if number(v)), default=None) if basis == 'battle_elapsed' else (report.get('packet_clock_range') or {}).get('max')
    ids = set()
    cache = {}
    labels = {'arrived': '已到', 'early': '早到', 'late': '迟到', 'unknown': '无法判断'}
    for target in plan['targets']:
        if not isinstance(target, dict):
            raise ValueError('每个计划目标必须是对象')
        target_id = target.get('id')
        if not isinstance(target_id, str) or not target_id or target_id in ids:
            raise ValueError('计划目标 id 缺失或重复')
        ids.add(target_id)
        start, end = target.get('start_sec'), target.get('end_sec')
        tolerance, trigger = target.get('tolerance_sec', 0), target.get('trigger_sec', 0)
        if not all(number(v) for v in (start, end, tolerance, trigger)) or not 0 <= trigger <= start <= end or tolerance < 0:
            raise ValueError('计划窗口、触发时间或允许偏差无效')
        if target.get('zone_id') not in {z['id'] for z in selected_zones}:
            raise ValueError('计划区域不存在或不属于本地图')
        targets = _target_players(players, target.get('selector'), _roster(report))
        if not targets:
            output['results'].append({'target_id': target_id, 'entity_id': None, 'status': 'unknown', 'label': labels['unknown'],
                                      'reason': '对象缺失、重名或未明确选择队伍', 'arrival_time_sec': None,
                                      'arrival_interval_sec': None, 'coverage': None, 'evidence_ids': []})
            continue
        evaluation_end = max(end + tolerance, maximum) if number(maximum) else end + tolerance
        evaluation_end = max(evaluation_end, trigger + .001)
        if evaluation_end > 86400:
            raise ValueError('计划或录制时间超过支持范围')
        cache_key = (trigger, evaluation_end)
        if cache_key not in cache:
            target_rules = _rules(dict(rules, window_start_sec=trigger, window_end_sec=evaluation_end, support_triggers=[]))
            cache[cache_key] = analyze_spatial(report, [p['shipId'] for p in players], selected_zones, target_rules, ship_catalog=ship_catalog, intel=intel)
        spatial = cache[cache_key]
        for player in targets:
            entity = player['shipId']
            state = next((e for e in spatial['entities'] if e['entity_id'] == entity), None)
            region = next((z for z in state['zones'] if z['zone_id'] == target['zone_id']), None) if state else None
            visit = region.get('first_entry') if region else None
            result = {'target_id': target_id, 'entity_id': entity, 'name': player.get('name'), 'zone_id': target['zone_id'],
                      'zone_version': next(z['version'] for z in selected_zones if z['id'] == target['zone_id']),
                      'zone_definition': next(z for z in selected_zones if z['id'] == target['zone_id']),
                      'planned_window_sec': [start, end], 'allowed_window_sec': [max(trigger, start - tolerance), end + tolerance],
                      'trigger_sec': trigger, 'status': 'unknown', 'arrival_time_sec': visit['time_sec'] if visit else None,
                      'arrival_interval_sec': visit['time_interval_sec'] if visit else None,
                      'coverage': state['coverage_ratio'] if state else None,
                      'reason': '未记录到可确定首次到达；缺失不等于未执行'}
            lower, upper = result['allowed_window_sec']
            if visit and visit['time_interval_sec'] is not None and state:
                a, b = visit['time_interval_sec']
                if _covered_between(state['valid_intervals'], trigger, b):
                    if b < lower:
                        result['status'] = 'early'
                    elif a > upper:
                        result['status'] = 'late'
                    elif a >= lower and b <= upper:
                        result['status'] = 'arrived'
                    result['reason'] = '首次进入的观测上下界与允许窗口比较' if result['status'] != 'unknown' else '过界时间区间跨越允许窗口边界'
                else:
                    result['reason'] = '触发至首次观测进入之间存在观测缺口，不能排除此前已到达'
            elif visit:
                result['reason'] = '首次有效位置已经在区域内，实际首次到达时间未知'
            result['label'] = labels[result['status']]
            ref = registry.add(report, 'plan_arrival', f"目标 {target_id} / {player.get('name') or intel.name(entity)}：{result['label']}",
                               start=visit['clock'] if visit else None, end=visit['clock'] if visit else None,
                               entities=[entity], details={k: v for k, v in result.items() if k != 'name'})
            result['evidence_ids'] = [ref]
            output['results'].append(result)
    output['evidence'] = sorted(registry.rows.values(), key=lambda e: e['id'])
    output['status'] = 'partial' if any(row['status'] != 'unknown' for row in output['results']) else 'unknown'
    return output


def compare_training(before_reports, after_reports, **analysis_kwargs):
    before = analyze_collection(before_reports, **analysis_kwargs)
    after = analyze_collection(after_reports, **analysis_kwargs)
    output = {'schema_version': 1, 'kind': 'training_comparison', 'status': 'not_comparable', 'rules_version': before['rules_version'],
              'rules': before['rules'], 'before_counts': before['counts'], 'after_counts': after['counts'], 'groups': [],
              'unmatched_before_groups': [], 'unmatched_after_groups': [], 'evidence': [],
              'participants': before['participants'] + after['participants'],
              'limitations': ['前后变化仅描述已导入同条件样本，不证明训练造成变化。',
                              '版本、地图、模式、布局、方位、区域或规则不同时不合并比较；缺失值不作零。']}
    prior = {g['id']: g for g in before['groups'] if g['comparable']}
    later = {g['id']: g for g in after['groups'] if g['comparable']}
    overlapping = set(b['battle_id'] for b in before['battles']) & set(b['battle_id'] for b in after['battles'])
    output['overlapping_battle_ids'] = sorted(overlapping)
    minimum = before['rules']['min_pattern_battles']
    for key in sorted(prior.keys() & later.keys()):
        left, right = prior[key], later[key]
        eligible = len(left['battle_ids']) >= minimum and len(right['battle_ids']) >= minimum and not (set(left['battle_ids'] + right['battle_ids']) & overlapping)
        row = {'id': key, 'conditions': left['conditions'], 'status': 'comparable' if eligible else 'not_comparable',
               'before_battles': len(left['battle_ids']), 'after_battles': len(right['battle_ids']), 'changes': [],
               'reason': None if eligible else '样本不足或训练前后重复包含同一场比赛'}
        if eligible:
            right_patterns = {(p['rule_id'], p.get('zone_id')): p for p in right['patterns']}
            for p in left['patterns']:
                q = right_patterns.get((p['rule_id'], p.get('zone_id')))
                if q and p['denominator'] >= minimum and q['denominator'] >= minimum:
                    row['changes'].append({'metric': p['rule_id'], 'zone_id': p.get('zone_id'),
                        'before': {'numerator': p['numerator'], 'denominator': p['denominator'], 'value': p['ratio']},
                        'after': {'numerator': q['numerator'], 'denominator': q['denominator'], 'value': q['ratio']},
                        'difference': q['ratio'] - p['ratio'], 'evidence_ids': sorted(set(p['evidence_ids'] + q['evidence_ids']))})
            for zone_id in [p.get('zone_id') for p in left['patterns'] if p.get('zone_id') is not None]:
                values, refs = [], []
                for collection, ids in ((before, left['battle_ids']), (after, right['battle_ids'])):
                    side_values = []
                    for battle in collection['battles']:
                        if battle['battle_id'] not in ids or (battle['coverage']['ratio'] or 0) < before['rules']['min_coverage']:
                            continue
                        regions = [z for e in battle['spatial']['entities'] for z in e['zones'] if z['zone_id'] == zone_id and z['inside_seconds'] is not None]
                        if len(regions) != len(battle['participants']):
                            continue
                        side_values.append(sum(z['inside_seconds'] for z in regions))
                        refs.extend(ref for z in regions for ref in z['evidence_ids'])
                    values.append(side_values)
                if all(len(v) >= minimum for v in values):
                    averages = [sum(v) / len(v) for v in values]
                    row['changes'].append({'metric': 'mean_observed_zone_ship_seconds', 'zone_id': zone_id,
                        'before': {'denominator': len(values[0]), 'value': averages[0]},
                        'after': {'denominator': len(values[1]), 'value': averages[1]},
                        'difference': averages[1] - averages[0], 'evidence_ids': sorted(set(refs)),
                        'interpretation': '每局所选队伍观测驻留采样总舰秒；不解释为训练因果'})
            if not row['changes']:
                row.update(status='not_comparable', reason='没有满足覆盖率与样本门槛的同口径指标')
        output['groups'].append(row)
    output['unmatched_before_groups'] = sorted(set(g['id'] for g in before['groups']) - later.keys())
    output['unmatched_after_groups'] = sorted(set(g['id'] for g in after['groups']) - prior.keys())
    output['evidence'] = sorted({e['id']: e for e in before['evidence'] + after['evidence']}.values(), key=lambda e: e['id'])
    if any(g['status'] == 'comparable' for g in output['groups']):
        output['status'] = 'comparable'
    return output


def _assign_resources(conditions, count=2):
    if not isinstance(conditions, dict):
        return None, ['缺少本方人员与舰船条件']
    players, available = conditions.get('players'), conditions.get('available_ships')
    maximum = conditions.get('max_players')
    if not isinstance(players, list) or not isinstance(available, list) or type(maximum) is not int or maximum < count:
        return None, ['人员、可用舰船或参赛人数限制未完整提供']
    allowed = {str(s.get('id')) if isinstance(s, dict) else str(s) for s in available}
    banned = {str(s) for s in conditions.get('banned_ships', [])}
    allowed -= banned
    limit = conditions.get('max_per_ship', {})
    if not isinstance(limit, dict) or any(type(v) is not int or v < 0 for v in limit.values()):
        raise ValueError('max_per_ship 必须是舰船 id 到非负整数限制的对象')
    roster = []
    seen = set()
    for player in players:
        if not isinstance(player, dict) or not player.get('id') or str(player['id']) in seen:
            continue
        seen.add(str(player['id']))
        ships = sorted({str(s) for s in player.get('ship_ids', [])} & allowed)
        roster.append((str(player['id']), ships))
    roster.sort(key=lambda r: (len(r[1]), r[0]))
    def search(index, result, used):
        if len(result) == count:
            return result
        if index >= len(roster) or len(result) + len(roster) - index < count:
            return None
        identity, ships = roster[index]
        for ship in ships:
            if used[ship] < limit.get(ship, count):
                used[ship] += 1
                found = search(index + 1, result + [{'player_id': identity, 'ship_id': ship}], used)
                used[ship] -= 1
                if found:
                    return found
        return search(index + 1, result, used)
    assignment = search(0, [], Counter())
    return assignment, [] if assignment else ['本方可用关系、禁船或同船数量限制下，不能组成两名不同选手的观察/接应分工']


def build_counterplans(analysis, own_conditions):
    if not isinstance(analysis, dict) or analysis.get('kind') != 'collection_analysis':
        raise ValueError('需要 analyze_collection 的输出')
    resources, reasons = _assign_resources(own_conditions)
    output = {'schema_version': 1, 'kind': 'counterplans', 'status': 'general_only' if resources is None else 'conditional_options',
              'rules_version': analysis.get('rules_version'), 'rules': deepcopy(analysis.get('rules')), 'plans': [],
              'conditions_validated': resources is not None, 'constraint_issues': reasons,
              'participants': deepcopy(analysis.get('participants') or []),
              'evidence': deepcopy(analysis.get('evidence') or []),
              'limitations': ['规则方案是备选训练与准备材料，不是必胜策略。',
                              '只核对提供的人员、可用舰船、人数、禁船和同船数量；未提供的赛制条款未验证。',
                              '不凭舰名猜测当局射程、雷达、隐蔽或实际配装。']}
    patterns = [p for g in analysis.get('groups', []) for p in g.get('patterns', [])
                if p.get('rule_id') == 'zone_commitment' and p.get('status') == 'observed_pattern'
                and p.get('numerator', 0) >= (analysis.get('rules') or DEFAULT_RULES)['min_pattern_battles']]
    for pattern in patterns[:12]:
        base = {'id': _hash([pattern['id'], resources, own_conditions], 'counterplan:'), 'kind': 'rule_suggestion',
                'basis': {'text': pattern['label'], 'numerator': pattern['numerator'], 'denominator': pattern['denominator'],
                          'exception_battle_ids': pattern['exception_battle_ids'], 'rules_version': pattern['rules_version']},
                'conditions': ['地图、布局与开局观测方位须与依据样本一致', '本局先确认该区域仍出现相似投入', '观察与接应路线需要教练核对地形和有效射界'],
                'actions': ['在该区域外围安排一次可撤出的观察，先确认投入数量', '由另一名选手保持接应并记录到达窗口；只有观察得到确认后再讨论转移资源'],
                'resources': {'assignments': deepcopy(resources) if resources else [], 'required_players': 2,
                              'validation': 'provided_constraints_checked' if resources else 'not_configured'},
                'risks': ['观测可能不完整，区域投入不等于推进意图', '抽调人员会降低其他区域的可用兵力', '几何接近不能保证有效火力或安全撤退'],
                'adjustment_signals': ['首轮有效观测与历史投入数量不符', '观察舰失去观测或不能安全退出', '接应无法在约定窗口到达'],
                'evidence_ids': list(pattern['evidence_ids']), 'scope': 'conditional_own_roster' if resources else 'general_preparation_only'}
        if resources is None:
            base['actions'] = ['把该区域的投入数量与例外局列入赛前核对表', '补齐本方人员、可用舰船与赛制后再分配具体观察和接应任务']
        output['plans'].append(base)
    if not output['plans']:
        output['status'] = 'insufficient_evidence'
        output['constraint_issues'].append('尚无达到重复样本和覆盖门槛的区域投入规律；不生成针对性对抗结论')
    return output
