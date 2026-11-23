"""Compact team-level evidence for a tactical report, sourced from the replay clock."""
from .replay_battle_state import ReplayBattleState, valid_clock


KINDS = frozenset({'replay_team_score_flow', 'replay_capture_pressure'})


def _fact(kind, text, start, end, support, fields):
    return dict(kind=kind, text=text, entity_ids=[], clock_start=start, clock_end=end,
                details=dict(support=support, source_fields=fields, scope='队伍战场状态'))


def _score_rows(state):
    clocks = sorted({time for stream in state.score_changes.values() for time, _ in stream})
    frames = [(clock, state.score(0, clock), state.score(1, clock)) for clock in clocks]
    pairs = [r for r in frames if r[1] is not None and r[2] is not None]
    if not pairs:
        return []
    selected = {pairs[0], pairs[-1]}
    edges = []
    previous = None
    leader = None
    for row in pairs:
        margin = row[1] - row[2]
        if margin:
            current = int(margin > 0)
            if leader is not None and current != leader:
                edges.append(row)
            leader = current
        if previous and abs(row[1]-previous[1]) + abs(row[2]-previous[2]) >= 30:
            selected.add(row)
        previous = row
    events = sorted(selected)
    if len(events) > 6:
        events = [events[round(i*(len(events)-1)/5)] for i in range(6)]
    events = sorted(set(events) | set(edges[:2] + edges[-2:]))
    points = []
    for clock, a, b in events:
        point = {'clock':clock, 'team_0_score':a, 'team_1_score':b}
        goal = state.win_score(clock)
        if goal is not None:
            point['win_score'] = goal
        points.append(point)
    return points


def _cap_rows(state):
    for key, point in state.points.items():
        times = sorted(state.capture_times(key) | {clock for clock, _ in state.cap_changes[key]})
        states = [(clock, state.capture_at(key, clock)) for clock in times]
        changes = []
        before = None
        for clock, row in states:
            if not any(value is not None for value in row.values()):
                before = None
                continue
            progress = row['capture_progress']
            last = before['capture_progress'] if before is not None else None
            crossed = progress is not None and last is not None and int(progress * 4) != int(last * 4)
            if before is None or crossed or any(before[key] != row[key] for key in
                    ('owner_team','contested','has_invaders','capturing_team','capture_enabled')):
                changes.append((clock, row))
            before = row
        if len(changes) < 2:
            continue
        if len(changes) > 6:
            changes = [changes[round(i*(len(changes)-1)/5)] for i in range(6)]
        name = str(point.get('name') or '?')[:20]
        support = []
        for clock, row in changes:
            sample = {'clock':clock, **row}
            a, b = state.score(0, clock), state.score(1, clock)
            if a is not None or b is not None:
                sample.update(team_0_score=a, team_1_score=b)
            support.append(sample)
        contested = any(row['contested'] is True for _, row in states)
        text = f'{name} 区保留归属、圈内双方状态和推进进度的记录。'
        if contested:
            text += '记录到双方同时在圈。'
        yield _fact('replay_capture_pressure', text, changes[0][0], changes[-1][0], support,
                    ['cap_team_changes','cap_state_samples','team_score_samples'])


def battle_state_facts(report):
    if report.get('framing_error'):
        return []
    state = ReplayBattleState(report)
    rows = []
    scores = _score_rows(state)
    if scores:
        rows.append(_fact('replay_team_score_flow',
                          '按录像记录比较队伍 0 与队伍 1 的比分，包含早期、关键变化和最后一次双边已知读数。',
                          scores[0]['clock'], scores[-1]['clock'], scores,
                          ['team_score_samples','team_score_limit_samples']))
    rows.extend(_cap_rows(state))
    return rows
