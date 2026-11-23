"""Separate replay packet clocks from observed client battle timers.

Only timer observations supply battle elapsed time. Result duration, server
timestamps, and packet maxima never synthesize missing timer observations.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


CLIENT_STAGES_15_9 = {'INVALID': -1, 'STARTED': 0, 'COUNTDOWN': 1, 'WAITING': 2, 'END_BATTLE_IDLING': 3}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def load_time_schema(path: Path, resource_manifest: Path):
    schema = json.loads(path.read_text(encoding='utf-8'))
    manifest = json.loads(resource_manifest.read_text(encoding='utf-8'))
    if schema['source_archive_sha256'] != manifest['scripts_zip_audit']['sha256']:
        raise ValueError('Time schema comes from a different client archive')
    stages = schema['stage_values']
    if stages != CLIENT_STAGES_15_9 or schema.get('battle_elapsed_formula') != 'duration - timeLeft':
        raise ValueError('Time schema semantics are not supported by this adapter')
    return stages, {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'source_archive_sha256': schema['source_archive_sha256']}


def analyze_timebase(samples, *, result_duration_sec=None, packet_clock_max=None, stage_values=None):
    """Analyze full property snapshots in original packet order.

    Required sample keys are packet_clock, entity_id, duration, timeLeft,
    battleStage. packet_offset and packet_type retain packet provenance.
    Adjacent snapshots at the same entity/clock are coalesced to avoid partial
    state during separate duration/timeLeft/battleStage property messages.
    """
    stages = dict(CLIENT_STAGES_15_9 if stage_values is None else stage_values)
    if set(CLIENT_STAGES_15_9) - set(stages) or len(set(stages.values())) != len(stages):
        raise ValueError('Incomplete or ambiguous client stage values')
    if result_duration_sec is not None and (not _number(result_duration_sec) or result_duration_sec < 0):
        raise ValueError('Invalid result duration')
    if packet_clock_max is not None and (not _number(packet_clock_max) or packet_clock_max < 0):
        raise ValueError('Invalid packet clock maximum')
    stage_names = {value: name for name, value in stages.items()}
    groups = []
    raw_count = 0
    for source in samples:
        raw_count += 1
        row = {key: source.get(key) for key in ('packet_clock', 'packet_offset', 'packet_type', 'entity_id', 'duration', 'timeLeft', 'battleStage')}
        key = (row['entity_id'], row['packet_clock'])
        if groups and _number(row['packet_clock']) and key == groups[-1]['key']:
            group = groups[-1]
            group['source_offsets'].append(row['packet_offset'])
            group['source_count'] += 1
            group['row'] = row
        else:
            groups.append({'key': key, 'row': row, 'source_offsets': [row['packet_offset']], 'source_count': 1})
    observations, transitions, anomalies = [], [], []
    previous, previous_active = None, None
    clock_segment = 0
    battle_segment = -1
    for group in groups:
        row = dict(group['row'])
        row.update(source_offsets=group['source_offsets'], source_count=group['source_count'])
        clock = row['packet_clock']
        duration, left, stage = row['duration'], row['timeLeft'], row['battleStage']
        row['stage_name'] = stage_names.get(stage, 'UNKNOWN') if type(stage) is int else 'UNKNOWN'
        reasons = []
        if not _number(clock) or clock < 0:
            reasons.append('missing_or_invalid_packet_clock')
        if previous is not None and (_number(clock) and _number(previous['packet_clock'])):
            if clock < previous['packet_clock'] or row['entity_id'] != previous['entity_id']:
                clock_segment += 1
                previous_active = None
                anomalies.append({'kind': 'clock_regression_or_entity_change', 'packet_offset': row['packet_offset'], 'packet_clock': clock})
        row['clock_segment_id'] = clock_segment
        if previous is None or stage != previous['battleStage'] or row['entity_id'] != previous['entity_id']:
            transitions.append({key: row[key] for key in ('packet_clock', 'packet_offset', 'entity_id', 'battleStage', 'stage_name', 'duration', 'timeLeft')})
        timer_valid = _number(duration) and _number(left) and duration > 0 and 0 <= left <= duration
        row['battle_elapsed_sec'] = None
        row['battle_remaining_sec'] = None
        row['preparation_remaining_sec'] = None
        row['battle_segment_id'] = None
        if row['stage_name'] == 'STARTED':
            if not timer_valid:
                reasons.append('missing_or_inconsistent_started_timer')
                previous_active = None
            elif not reasons:
                elapsed = duration - left
                if previous_active is None or duration != previous_active['duration'] or elapsed < previous_active['battle_elapsed_sec'] or clock_segment != previous_active['clock_segment_id']:
                    battle_segment += 1
                    if previous_active is not None:
                        anomalies.append({'kind': 'active_timer_reset_or_duration_change', 'packet_offset': row['packet_offset'], 'packet_clock': clock})
                row.update(battle_elapsed_sec=elapsed, battle_remaining_sec=left, battle_segment_id=battle_segment)
                previous_active = row
            else:
                previous_active = None
        elif row['stage_name'] == 'COUNTDOWN':
            if timer_valid and not reasons:
                row['preparation_remaining_sec'] = left
            elif not timer_valid:
                reasons.append('missing_or_inconsistent_preparation_timer')
            previous_active = None
        else:
            previous_active = None
        row['diagnostics'] = reasons
        observations.append(row)
        previous = row
    active = [r for r in observations if r['battle_elapsed_sec'] is not None]
    segment_ids = sorted({r['battle_segment_id'] for r in active})
    first = active[0] if active else None
    last = active[-1] if active else None
    last_elapsed = last['battle_elapsed_sec'] if last else None
    difference = result_duration_sec - last_elapsed if result_duration_sec is not None and last_elapsed is not None else None
    diagnostics = []
    if not active:
        diagnostics.append('no_valid_started_timer_observations')
    if len(segment_ids) > 1:
        diagnostics.append('multiple_active_timer_segments')
    if difference is not None and difference > 0:
        diagnostics.append('result_duration_exceeds_last_observed_elapsed')
    return {
        'model': '15.9_observed_client_timer', 'stage_values': stages,
        'raw_observation_count': raw_count, 'observation_count': len(observations),
        'coalesced_same_clock_updates': raw_count - len(observations),
        'observations': observations, 'stage_transitions': transitions, 'anomalies': anomalies,
        'coverage': {
            'active_timer_segments': len(segment_ids),
            'first_started_packet_clock': first['packet_clock'] if first else None,
            'first_observed_elapsed_sec': first['battle_elapsed_sec'] if first else None,
            'last_started_packet_clock': last['packet_clock'] if last else None,
            'last_observed_elapsed_sec': last_elapsed,
            'last_observed_remaining_sec': last['timeLeft'] if last else None,
            'end_idling_stage_observed': any(r['stage_name'] == 'END_BATTLE_IDLING' for r in observations),
            'result_duration_sec': result_duration_sec,
            'result_minus_last_observed_elapsed_sec': difference,
            'packet_clock_max': packet_clock_max,
        },
        'diagnostics': diagnostics,
        'clock_domains': {
            'packet': 'Replay packet header clock; preserve stream order and resets.',
            'battle_elapsed': 'duration - timeLeft only while CLIENT_BATTLE_STATE.STARTED.',
            'preparation': 'timeLeft while COUNTDOWN; not battle elapsed.',
            'post_battle_duration': 'Independent verified common result duration_sec.',
            'client_runtime': 'BigWorld.time() domain; no verified transform to packet clock here.',
            'server': 'Raw timestamp/tick observations require separate synchronization evidence; not Unix or packet offsets.',
        },
        'conversion_policy': 'Exact observed timer anchors or bounded intervals; no global offset, interpolation, extrapolation, or result-duration padding.',
    }


def packet_to_battle_time(report, packet_clock, *, packet_offset=None):
    """Return an observed anchor or a bounded elapsed interval, never a fit."""
    if not _number(packet_clock) or packet_clock < 0:
        raise ValueError('Invalid query packet clock')
    if packet_offset is not None and (type(packet_offset) is not int or packet_offset < 0):
        raise ValueError('Invalid query packet offset')
    observations = report['observations']
    active = [r for r in observations if r['battle_elapsed_sec'] is not None]
    candidates = []
    for index, row in enumerate(observations):
        if row['battle_elapsed_sec'] is None or row['packet_clock'] != packet_clock:
            continue
        effective_offset = row['packet_offset']
        next_offset = None
        next_observation = observations[index + 1] if index + 1 < len(observations) else None
        if next_observation is not None:
            source_offsets = next_observation.get('source_offsets')
            next_offset = source_offsets[0] if source_offsets else next_observation['packet_offset']
        if packet_offset is not None:
            # A coalesced state is known only after its final property packet.
            # Its offset window ends at the next raw timer observation, even
            # when that observation begins a non-active or incomplete state.
            if type(effective_offset) is not int or packet_offset < effective_offset:
                continue
            if next_observation is not None and (type(next_offset) is not int or packet_offset >= next_offset):
                continue
        candidates.append({'status': 'observed_anchor', 'elapsed_sec': row['battle_elapsed_sec'],
                           'elapsed_interval_sec': [row['battle_elapsed_sec'], row['battle_elapsed_sec']],
                           'source_offsets': row['source_offsets'], 'battle_segment_id': row['battle_segment_id'],
                           'anchor_packet_offset': effective_offset, 'next_timer_packet_offset': next_offset})
    if not candidates:
        for left, right in zip(active, active[1:]):
            if left['battle_segment_id'] != right['battle_segment_id']:
                continue
            if not left['packet_clock'] < packet_clock < right['packet_clock']:
                continue
            if packet_offset is not None and (left['packet_offset'] is None or right['packet_offset'] is None or not left['packet_offset'] < packet_offset < right['packet_offset']):
                continue
            candidates.append({'status': 'bounded_interval', 'elapsed_sec': None,
                               'elapsed_interval_sec': [left['battle_elapsed_sec'], right['battle_elapsed_sec']],
                               'source_offsets': [left['packet_offset'], right['packet_offset']],
                               'battle_segment_id': left['battle_segment_id']})
    if len(candidates) == 1:
        return candidates[0]
    return {'status': 'unknown', 'elapsed_sec': None, 'elapsed_interval_sec': None,
            'reason': 'ambiguous_clock_segments' if candidates else 'outside_observed_active_interval'}
