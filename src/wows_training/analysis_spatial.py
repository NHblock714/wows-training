"""Conservative spatial measurements from recorded positions and clock anchors.

No file, network, prediction or rendered projectile data is used here.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from itertools import combinations
import math

from .replay_intel import ReplayIntel, number


def inside(zone, x, z):
    """Boundary-inclusive polygon/circle membership in original engine axes."""
    if zone['shape'] == 'circle':
        cx, cz = zone['center']
        return math.hypot(x - cx, z - cz) <= zone['radius_engine'] + 1e-9
    vertices = zone['points']
    hit = False
    for (ax, az), (bx, bz) in zip(vertices, vertices[1:] + vertices[:1]):
        cross = (x - ax) * (bz - az) - (z - az) * (bx - ax)
        if abs(cross) <= 1e-8 and min(ax, bx) - 1e-9 <= x <= max(ax, bx) + 1e-9 and min(az, bz) - 1e-9 <= z <= max(az, bz) + 1e-9:
            return True
        if (az > z) != (bz > z) and x < (bx - ax) * (z - az) / (bz - az) + ax:
            hit = not hit
    return hit


def timeline(report, rules):
    """Use exact observed battle timer anchors, never an inferred start offset."""
    start, end = rules['window_start_sec'], rules['window_end_sec']
    if rules['time_basis'] == 'packet_clock':
        maximum = (report.get('packet_clock_range') or {}).get('max')
        if not number(maximum):
            return []
        stop = min(end, maximum)
        count = int(max(0, stop - start) / rules['sample_step_sec'])
        rows = [{'time_sec': start + i * rules['sample_step_sec'], 'clock': start + i * rules['sample_step_sec'], 'segment': 0}
                for i in range(count + 1) if start + i * rules['sample_step_sec'] <= stop]
        if rows and rows[-1]['clock'] < stop:
            rows.append({'time_sec': stop, 'clock': stop, 'segment': 0})
        return rows
    rows = []
    maximum = (report.get('packet_clock_range') or {}).get('max')
    if not number(maximum):
        return []
    for row in (report.get('time_analysis') or {}).get('observations', []):
        elapsed, clock = row.get('battle_elapsed_sec'), row.get('packet_clock')
        if (row.get('stage_name') == 'STARTED' and number(elapsed) and number(clock)
                and start <= elapsed <= end and 0 <= clock <= maximum and row.get('battle_segment_id') is not None
                and not row.get('diagnostics')):
            rows.append({'time_sec': elapsed, 'clock': clock, 'segment': row['battle_segment_id']})
    rows.sort(key=lambda row: (row['clock'], row['time_sec']))
    # Repeated or reset timer domains cannot silently become one elapsed timeline.
    if len({row['segment'] for row in rows}) > 1:
        return []
    result = []
    for row in rows:
        if result and (row['time_sec'] <= result[-1]['time_sec'] or row['clock'] <= result[-1]['clock']):
            continue
        result.append(row)
    return result


def _raw_segments(report):
    result = {}
    for track in (report.get('geometry_analysis') or {}).get('projected_tracks', []):
        columns = track.get('columns') or []
        if any(columns.count(key) != 1 for key in ('packet_clock', 'x_engine', 'z_engine')):
            continue
        ix = [columns.index(key) for key in ('packet_clock', 'x_engine', 'z_engine')]
        segments = []
        for raw in track.get('segments') or []:
            part = []
            for row in raw:
                if len(row) <= max(ix) or not all(number(row[i]) for i in ix):
                    if part:
                        segments.append(part)
                        part = []
                    continue
                point = tuple(row[i] for i in ix)
                if part and point[0] <= part[-1][0]:
                    segments.append(part)
                    part = []
                part.append(point)
            if part:
                segments.append(part)
        result[track.get('entity_id')] = segments
    return result


def _continuous(segments, states, start, end, gap):
    for segment in segments:
        if segment[0][0] <= start <= end <= segment[-1][0]:
            a = max(0, bisect_right(segment, start, key=lambda r: r[0]) - 1)
            b = min(len(segment), bisect_left(segment, end, key=lambda r: r[0]) + 1)
            if any(y[0] - x[0] > gap for x, y in zip(segment[a:b], segment[a + 1:b])):
                return False
            first = bisect_right(states, start, key=lambda r: r['clock'])
            last = bisect_right(states, end, key=lambda r: r['clock'])
            return not any(row.get('visible_to_recorder') is False for row in states[first:last])
    return False


def analyze_spatial(report, entity_ids, zones, rules, *, ship_catalog=None, intel=None):
    intel = intel or ReplayIntel(report, ship_catalog)
    selected = sorted(set(entity_ids))
    anchors = timeline(report, rules)
    total = rules['window_end_sec'] - rules['window_start_sec']
    output = {'status': 'unknown', 'time_basis': rules['time_basis'], 'window': [rules['window_start_sec'], rules['window_end_sec']],
              'coverage': {'observed_entity_seconds': 0.0, 'possible_entity_seconds': total * len(selected), 'ratio': None},
              'entities': [], 'deployment': [], 'rally': [], 'support_arrivals': [], 'limitations': [
                  '驻留采用观测采样口径：只累计同一连续段、间隔不超过阈值且两端都在区域内的区间；不是逐帧精确驻留。',
                  '区域进入是记录采样时刻或上下界，不证明精确过界时刻；缺失段不补路线。',
                  '集结距离为几何关系，不证明通视、火力支援或指挥因果。']}
    if not selected or not intel.geometry_verified or not anchors:
        output['reason'] = '缺少明确队伍、已验证地图位置或有效时间锚点'
        return output
    geometry = report.get('geometry_analysis') or {}
    scale = 30.0 if geometry.get('meters_per_engine_unit') == 30 and geometry.get('distance_domain') == 'client_displayed_world_distance' else None
    output['distance_scale_m_per_engine_unit'] = scale
    if scale is None:
        output['limitations'].append('未确认世界距离 30 米比例，距离指标停用。')
    raw = _raw_segments(report)
    points = []
    for row in anchors:
        positions = {e: p for e, p in intel.positions(row['clock']).items() if e in selected
                     and p.get('quality') == 'recorded' and number(p.get('observation_clock'))
                     and 0 <= row['clock'] - p['observation_clock'] < rules['max_gap_sec']}
        points.append(positions)
        for zone in zones:
            members = [e for e, p in positions.items() if inside(zone, p['x_engine'], p['z_engine'])]
            output['deployment'].append({'time_sec': row['time_sec'], 'clock': row['clock'], 'zone_id': zone['id'],
                                         'count': len(members), 'entity_ids': members,
                                         'observed_entities': len(positions), 'selected_entities': len(selected),
                                         'coverage_ratio': len(positions) / len(selected)})
        distances = [math.hypot(a['x_engine'] - b['x_engine'], a['z_engine'] - b['z_engine']) * scale
                     for a, b in combinations(positions.values(), 2)] if scale else []
        output['rally'].append({'time_sec': row['time_sec'], 'clock': row['clock'], 'entity_ids': sorted(positions),
                                'observed_entities': len(positions), 'selected_entities': len(selected),
                                'max_pair_distance_m': max(distances) if distances else None,
                                'mean_pair_distance_m': sum(distances) / len(distances) if distances else None,
                                'quality': 'recorded_geometry' if distances else 'unknown'})
    for entity in selected:
        states = intel.ships.get(entity, {}).get('samples', [])
        valid = []
        for i in range(len(anchors) - 1):
            a, b = anchors[i:i + 2]
            dt = b['time_sec'] - a['time_sec']
            valid.append(entity in points[i] and entity in points[i + 1]
                         and 0 < dt <= rules['max_gap_sec'] and 0 < b['clock'] - a['clock'] <= rules['max_gap_sec']
                         and _continuous(raw.get(entity, []), states, a['clock'], b['clock'], rules['max_gap_sec']))
        observed = sum(anchors[i + 1]['time_sec'] - anchors[i]['time_sec'] for i, yes in enumerate(valid) if yes)
        entry = {'entity_id': entity, 'observed_seconds': observed, 'coverage_ratio': observed / total if total else None,
                 'zones': [], 'transitions': [], 'valid_intervals': [
                     {'clock_start': anchors[i]['clock'], 'clock_end': anchors[i + 1]['clock'],
                      'time_start': anchors[i]['time_sec'], 'time_end': anchors[i + 1]['time_sec']}
                     for i, yes in enumerate(valid) if yes]}
        for zone in zones:
            memberships = [entity in p and inside(zone, p[entity]['x_engine'], p[entity]['z_engine']) for p in points]
            visits, spans, inside_seconds = [], [], 0.0
            for i, hit in enumerate(memberships):
                if hit and (i == 0 or not memberships[i - 1]):
                    bounded = i > 0 and valid[i - 1]
                    visits.append({'clock': anchors[i]['clock'], 'time_sec': anchors[i]['time_sec'],
                                   'time_interval_sec': [anchors[i - 1]['time_sec'], anchors[i]['time_sec']] if bounded else None,
                                   'quality': 'bounded_entry' if bounded else 'first_observed_inside',
                                   'first_arrival_proven': bounded and all(valid[:i])
                                       and anchors[0]['time_sec'] <= rules['window_start_sec'] + 1e-8})
                if i < len(valid) and valid[i] and hit and memberships[i + 1]:
                    dt = anchors[i + 1]['time_sec'] - anchors[i]['time_sec']
                    inside_seconds += dt
                    span = {'clock_start': anchors[i]['clock'], 'clock_end': anchors[i + 1]['clock'],
                            'time_start': anchors[i]['time_sec'], 'time_end': anchors[i + 1]['time_sec'],
                            'entity_id': entity, 'zone_id': zone['id']}
                    if spans and spans[-1]['clock_end'] == span['clock_start']:
                        spans[-1].update(clock_end=span['clock_end'], time_end=span['time_end'])
                    else:
                        spans.append(span)
            entry['zones'].append({'zone_id': zone['id'], 'inside_seconds': inside_seconds if observed else None,
                                   'first_entry': visits[0] if visits else None, 'entry_events': visits, 'evidence_spans': spans})
        previous_zone, previous_zone_index, departure, departure_index, departure_upper = None, None, None, None, None
        for i, positions in enumerate(points):
            if entity not in positions:
                continue
            p = positions[entity]
            current = [z['id'] for z in zones if inside(z, p['x_engine'], p['z_engine'])]
            # Overlapping regions do not establish an exclusive transfer.
            if len(current) > 1:
                previous_zone, previous_zone_index, departure, departure_index, departure_upper = None, None, None, None, None
                continue
            current = current[0] if current else None
            if previous_zone and current != previous_zone and departure is None:
                departure, departure_index, departure_upper = anchors[previous_zone_index], previous_zone_index, anchors[i]
            if current and previous_zone and current != previous_zone and departure is not None:
                connected = i > departure_index and all(valid[departure_index:i])
                entry['transitions'].append({'from_zone': previous_zone, 'to_zone': current,
                                              'departure_clock': departure['clock'], 'arrival_clock': anchors[i]['clock'],
                                              'duration_sec': anchors[i]['time_sec'] - departure['time_sec'] if connected else None,
                                              'duration_interval_sec': [max(0, anchors[i - 1]['time_sec'] - departure_upper['time_sec']),
                                                                        anchors[i]['time_sec'] - departure['time_sec']] if connected else None,
                                              'quality': 'observed_sample_interval' if connected else 'unknown',
                                              'reason': '观测采样间隔，不是精确过界时间' if connected else '转场之间存在失观或长间隔'})
                previous_zone, previous_zone_index, departure, departure_index, departure_upper = current, i, None, None, None
            elif current and (previous_zone is None or current == previous_zone):
                previous_zone, previous_zone_index, departure, departure_index, departure_upper = current, i, None, None, None
        output['entities'].append(entry)
    output['coverage']['observed_entity_seconds'] = sum(e['observed_seconds'] for e in output['entities'])
    output['coverage']['ratio'] = output['coverage']['observed_entity_seconds'] / (total * len(selected)) if total else None
    output['status'] = 'reliable' if output['coverage']['ratio'] is not None and output['coverage']['ratio'] >= rules['min_coverage'] else 'partial'
    output['sampling'] = {'anchors': len(anchors), 'method': 'recorded_timer_anchors' if rules['time_basis'] == 'battle_elapsed' else 'explicit_packet_clock_grid',
                           'max_gap_sec': rules['max_gap_sec'], 'missing_tail_counted_in_denominator': True}
    for trigger in rules.get('support_triggers', []):
        for entity in output['entities']:
            zone = next((z for z in entity['zones'] if z['zone_id'] == trigger['zone_id']), None)
            visit = next((v for v in (zone or {}).get('entry_events', []) if trigger['trigger_sec'] <= v['time_sec'] <= trigger['end_sec']), None)
            proven = visit is not None and visit['time_interval_sec'] is not None and visit['time_interval_sec'][0] >= trigger['trigger_sec']
            if proven:
                cursor = trigger['trigger_sec']
                for interval in entity['valid_intervals']:
                    if interval['time_end'] < cursor:
                        continue
                    if interval['time_start'] > cursor + 1e-8:
                        break
                    cursor = max(cursor, interval['time_end'])
                    if cursor >= visit['time_sec']:
                        break
                proven = cursor >= visit['time_sec']
            output['support_arrivals'].append({'trigger_id': trigger['id'], 'entity_id': entity['entity_id'], 'zone_id': trigger['zone_id'],
                                               'arrival_time_sec': visit['time_sec'] if visit else None,
                                               'arrival_interval_sec': visit['time_interval_sec'] if visit else None,
                                               'delay_sec': visit['time_sec'] - trigger['trigger_sec'] if proven else None,
                                               'delay_interval_sec': [v - trigger['trigger_sec'] for v in visit['time_interval_sec']] if proven else None,
                                               'quality': 'bounded_observed_arrival' if proven else 'unknown',
                                               'clock_start': visit['clock'] if visit else None, 'clock_end': visit['clock'] if visit else None,
                                               'interpretation': '区域到达记录，不证明反应速度或实际支援效果'})
    return output
