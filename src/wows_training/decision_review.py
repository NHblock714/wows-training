"""Grounded review cues from a verified WoWSP-D1 run on an original replay."""
from __future__ import annotations

import math
from statistics import mean, median

from .local_decision import PACK_SHA, DecisionError, _validate, source_issue


MODEL_NAME = 'WoWSP-D1'
MODEL_SEQUENCE = 3
MIN_CONFIDENCE = .5
MIN_AXIS_SAMPLES = 6
MIN_WINDOW_SAMPLES = 3
# WoWSP-D1 evaluates movement rows every four seconds. Allow f32 rounding.
MAX_GAP_SECONDS = 4.1
POLICY = {
    'minimum_confidence': MIN_CONFIDENCE,
    'minimum_axis_samples': MIN_AXIS_SAMPLES,
    'minimum_window_samples': MIN_WINDOW_SAMPLES,
    'maximum_gap_seconds': MAX_GAP_SECONDS,
}
AXES = {'fire': ('开火', 'wholeFire'), 'move': ('走位', 'wholeMove')}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _empty_metrics(reason):
    return {
        axis: dict(score=None, sample_count=0, qualified_count=0, qualified_ratio=None,
                   clock_start=None, clock_end=None, gap_count=0, status='unavailable', reason=reason)
        for axis in AXES
    }


def _participant(report, review, scored):
    entity_id, ship_id = scored['entityId'], scored['shipId']
    people = review.get('participants', [])
    by_entity = [p for p in people if p.get('entity_id') == entity_id]
    if len(by_entity) == 1 and by_entity[0].get('ship_id') == ship_id:
        return by_entity[0]
    if by_entity or not scored['isRecorder']:
        return None

    # A header-only replay may have no decoded entity IDs. The original
    # replay hash and the model's recorder ID are already verified. Only
    # the unique relation-0 header row can supplement that recorder label.
    name = (report.get('metadata') or {}).get('playerName')
    own = [p for p in report.get('roster') or [] if p.get('relation') == 0]
    if (not name or len(own) != 1 or own[0].get('name') != name
            or own[0].get('shipId') is not None or own[0].get('shipParamsId') != ship_id):
        return None
    by_header = [p for p in people if p.get('name') == name and p.get('ship_id') == ship_id]
    return by_header[0] if len(by_header) == 1 and by_header[0].get('entity_id') is None else None


def _metric(axis, scored):
    _, field = AXES[axis]
    available = [point for point in scored['samples'] if point[axis] is not None]
    qualified = [point for point in available if point['confidence'] >= MIN_CONFIDENCE]
    count = len(qualified)
    ratio = count / len(available) if available else None
    gaps = sum(b['t'] - a['t'] > MAX_GAP_SECONDS for a, b in zip(qualified, qualified[1:]))
    if not available:
        reason = '有效样本不足，模型在此视角没有可评分的开火窗口。' if axis == 'fire' else '有效样本不足，缺少走位时点。'
    elif count < MIN_AXIS_SAMPLES:
        reason = f'有效样本不足，仅 {count} 个时点达到置信度门槛，至少需 {MIN_AXIS_SAMPLES} 个。'
    else:
        notes = []
        if ratio < .5:
            notes.append('过半评分时点未达到复核的置信度门槛')
        if gaps:
            notes.append(f'有效时点间有 {gaps} 处超过 {MAX_GAP_SECONDS:g} 秒的间隔')
        reason = '；'.join(notes) + '。' if notes else '有效时点达到复核门槛。'
    return dict(
        score=scored[field], sample_count=len(available), qualified_count=count,
        qualified_ratio=ratio, clock_start=qualified[0]['t'] if qualified else None,
        clock_end=qualified[-1]['t'] if qualified else None, gap_count=gaps,
        status='usable' if count >= MIN_AXIS_SAMPLES else 'insufficient', reason=reason,
    ), qualified


def _low_window(axis, qualified):
    """Choose one short cluster, never pad it across a missing or weak row."""
    # The heads have different distributions (binary fire vs 17 move bins).
    # Use a per-axis ceiling and this ship's median to choose what to inspect.
    # The window average is more stable than ranking a single low point.
    ceiling = .35 if axis == 'fire' else .12
    low_mean = min(ceiling, median(point[axis] for point in qualified))
    windows = []
    for start in range(len(qualified)-MIN_WINDOW_SAMPLES+1):
        group = qualified[start:start+MIN_WINDOW_SAMPLES]
        if any(point[axis] > ceiling for point in group):
            continue
        if any(b['t']-a['t'] > MAX_GAP_SECONDS for a, b in zip(group, group[1:])):
            continue
        average = mean(point[axis] for point in group)
        if average <= low_mean:
            windows.append((average, group[0]['t'], group))
    winner = min(windows, default=None)
    return winner[2] if winner else None


def _related_observation(review, entity_id, clock):
    """One nearby, independently decoded observation on the same entity."""
    observations = []
    for event in review.get('events', []):
        time = event.get('clock')
        if (event.get('kind') == '击沉' and entity_id in event.get('entities', [])
                and _finite(time) and abs(time-clock) <= MAX_GAP_SECONDS):
            observations.append({'kind': '击沉', 'clock': time, 'text': event['text']})
    for hit in review.get('damage_events', []):
        time, amount = hit.get('clock'), hit.get('amount')
        if not (_finite(time) and abs(time-clock) <= MAX_GAP_SECONDS and _finite(amount) and amount > 0):
            continue
        if hit.get('actor') == entity_id:
            text = f'录像观测到本舰造成 {amount:,.0f} 伤害。'
        elif hit.get('target') == entity_id:
            text = f'录像观测到本舰受到 {amount:,.0f} 伤害。'
        else:
            continue
        observations.append({'kind': '观测伤害', 'clock': time, 'text': text})
    return min(observations, key=lambda event: (abs(event['clock']-clock), event['clock']), default=None)


def _checkpoint(axis, group, review, entity_id):
    clock = group[len(group)//2]['t']
    score = mean(point[axis] for point in group)
    name = AXES[axis][0]
    related = _related_observation(review, entity_id, clock)
    text = f'{name}一致度在连续 {len(group)} 个有效时点偏低，均值 {score:.1%}。'
    if related is None:
        text += '附近未记录到本舰的击沉或伤害观测。'
    recommendation = (
        '回看该窗口的目标可见性、装填和暴露代价。训练时在开火前报出目标与遮挡，复核时比较决策条件。'
        if axis == 'fire' else
        '回看该窗口的航向、转向空间和友舰位置。训练时在转向前确认下一处掩体，复核时比较路线与风险。'
    )
    return dict(axis=axis, clock_start=group[0]['t'], clock_end=group[-1]['t'], clock=clock,
                score=score, sample_count=len(group), confidence_min=min(point['confidence'] for point in group),
                text=text, recommendation=recommendation, related_event=related)


def build_decision_review(report, review, result, entity_id=None):
    """Attach bounded, source-specific decision review data without loading files."""
    source = report.get('sha256')
    output = dict(model=MODEL_NAME, model_sequence=MODEL_SEQUENCE, source_sha=source,
                  clock_domain='packet_clock', player=None, terrain_available=None,
                  policy=dict(POLICY), metrics={}, checkpoints=[])

    def unavailable(status, reason):
        return dict(output, status=status, reason=reason, metrics=_empty_metrics(reason))

    issue = source_issue(report)
    if issue:
        return unavailable('unavailable', issue)
    if review.get('clock_domain') != 'packet_clock':
        return unavailable('unavailable', '缺少可核验的单视角录像时钟。')
    if result is None:
        return unavailable('not_run', '本录制视角尚未运行本地评分。')
    if (not isinstance(result, dict) or result.get('sourceSha') != source or result.get('packSha') != PACK_SHA
            or result.get('model') != MODEL_NAME or result.get('modelSequence') != MODEL_SEQUENCE):
        return unavailable('mismatch', '模型结果与当前录像或模型版本不匹配。')
    try:
        _validate(result)
    except (DecisionError, OverflowError, TypeError):
        return unavailable('mismatch', '模型结果未通过格式校验。')

    output['terrain_available'] = result['terrainAvailable']
    target = result.get('recorderEntity') if entity_id is None else entity_id
    scored = next((ship for ship in result['perShip'] if type(target) is int and ship['entityId'] == target), None)
    if scored is None:
        role = '录制者' if entity_id is None else '所选舰船'
        return unavailable('insufficient', f'{role}在当前视角缺少有效模型时点。')
    participant = _participant(report, review, scored)
    if participant is None:
        return unavailable('unavailable', '模型舰船与本局名单未能唯一对应。')
    output['player'] = dict(entity_id=target, ship_id=scored['shipId'], name=participant['name'], ship=participant['ship'])
    clock_range = report.get('packet_clock_range') or {}
    lower, upper = clock_range.get('min'), clock_range.get('max')
    first, last = scored['samples'][0]['t'], scored['samples'][-1]['t']
    if first < 0 or _finite(lower) and first < lower-.05 or _finite(upper) and last > upper+.05:
        return unavailable('mismatch', '模型时点超出当前录像时钟范围。')

    for axis in AXES:
        metric, qualified = _metric(axis, scored)
        output['metrics'][axis] = metric
        if metric['status'] == 'usable':
            window = _low_window(axis, qualified)
            if window:
                output['checkpoints'].append(_checkpoint(axis, window, review, target))
            else:
                metric['reason'] += '未找到连续低一致度的有效窗口。'
    usable = any(metric['status'] == 'usable' for metric in output['metrics'].values())
    output['status'] = 'usable' if usable else 'insufficient'
    output['reason'] = '一致度用于选择复看时点，按录像观察复核。' if usable else '本舰开火与走位均缺少足量有效时点。'
    return output
