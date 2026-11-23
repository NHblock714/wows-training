"""Read timestamped score and control-point states without extrapolating."""
from bisect import bisect_right
import math


BATTLE_STATE_SCHEMA_VERSION = 1


def valid_clock(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value < 100_000


def needs_battle_state_refresh(report):
    from .ordinary_replay import CURRENT, OLD
    version = str(report.get('replay_version') or '').replace(',', '.')
    return (version in (OLD, CURRENT) and not report.get('multiview') and bool(report.get('roster'))
            and report.get('battle_state_schema_version') != BATTLE_STATE_SCHEMA_VERSION)


def _team(value, *, neutral=False):
    return value if type(value) is int and value in ((-1, 0, 1) if neutral else (0, 1)) else None


def _score(value):
    return value if type(value) is int and 0 <= value <= 65535 else None


def _flag(value):
    return bool(value) if type(value) in (bool, int) and value in (0, 1) else None


def capture_logic_fields(logic):
    """Normalize the exact-client CAPTURE_LOGIC_STATE dictionary."""
    progress = logic.get('progress')
    progress = float(progress) if valid_clock(progress) and progress <= 1 else None
    return dict(capture_progress=progress, capturing_team=_team(logic.get('invaderTeam')),
                has_invaders=_flag(logic.get('hasInvaders')), contested=_flag(logic.get('bothInside')))


def score_limit_fields(missions):
    win = _score(missions.get('teamWinScore'))
    lose = _score(missions.get('teamLoseScore'))
    return dict(win_score=win if win else None, lose_score=lose)


def _pairs(rows, convert):
    return tuple(sorted(((row[0], convert(row[1])) for row in rows
                         if isinstance(row, (list, tuple)) and len(row) >= 2 and valid_clock(row[0])),
                        key=lambda row: row[0]))


def _states(rows):
    return tuple(sorted(((row['packet_clock'], row) for row in rows
                         if isinstance(row, dict) and valid_clock(row.get('packet_clock'))),
                        key=lambda row: row[0]))


def _at(rows, clock):
    if not valid_clock(clock):
        return None
    i = bisect_right(rows, clock, key=lambda row: row[0])
    return rows[i-1][1] if i else None


class ReplayBattleState:
    """One clock domain, with explicit unknowns preserved in replicated streams."""
    def __init__(self, report):
        scores = report.get('team_score_samples') or {}
        changes = report.get('cap_team_changes') or {}
        states = report.get('cap_state_samples') or {}
        self.score_changes = {team: _pairs(scores.get(str(team), []), _score) for team in (0,1)}
        self.points = {str(row['entity_id']): row for row in report.get('capture_points') or []
                       if isinstance(row, dict) and type(row.get('entity_id')) is int}
        self.cap_changes = {key:_pairs(changes.get(key, []), lambda v:_team(v, neutral=True)) for key in self.points}
        self._cap_samples = {key:_states(states.get(key, [])) for key in self.points}
        self._limits = _states(report.get('team_score_limit_samples') or [])

    @property
    def has_state(self):
        return any(self.score_changes.values()) or any(self._cap_samples.values())

    def score(self, team, clock):
        return _at(self.score_changes.get(team, ()), clock)

    def win_score(self, clock):
        value = (_at(self._limits, clock) or {}).get('win_score')
        return _score(value) or None

    def capture_at(self, entity_id, clock):
        key = str(entity_id.get('entity_id') if isinstance(entity_id, dict) else entity_id)
        changes = self.cap_changes.get(key, ())
        owner = _at(changes, clock)
        if not changes and valid_clock(clock):
            point = self.points.get(key) or {}
            first = point.get('packet_clock', 0)
            if valid_clock(first) and first <= clock:
                owner = _team(point.get('initial_team'), neutral=True)
        state = _at(self._cap_samples.get(key, ()), clock) or {}
        progress = state.get('capture_progress')
        return dict(owner_team=owner,
                    capture_progress=progress if valid_clock(progress) and progress <= 1 else None,
                    capturing_team=_team(state.get('capturing_team')),
                    has_invaders=_flag(state.get('has_invaders')), contested=_flag(state.get('contested')),
                    capture_enabled=_flag(state.get('capture_enabled')))

    def capture_times(self, entity_id):
        return {clock for clock, _ in self._cap_samples.get(str(entity_id), ())}
