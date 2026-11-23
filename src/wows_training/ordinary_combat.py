"""Read selected combat messages using the installed 13533378 client's wire definitions."""
from functools import lru_cache
import hashlib
from io import BytesIO
import json
import math
import struct
from types import SimpleNamespace

from replay_unpack.clients.wows.network.packets import EntityCreate, NestedProperty
from replay_unpack.core import Entity
from replay_unpack.core.entity_def.bit_reader import BitReader
from replay_unpack.core.entity_def.data_types.nested_types import PyFixedDict, PyFixedList
from replay_unpack.core.entity_def.definitions import Definitions

from .combat_capture import CombatCapture, CONSUMABLE_NAMES, LABELS
from .evidence import BattleLogicEvidence
from .ordinary_replay import CURRENT, _root, _profile
from .timebase import analyze_timebase


BATTLE_STATE_SCHEMA_VERSION = 1
_TIMER_PROPERTIES = frozenset(('duration', 'timeLeft', 'battleStage'))
_CURRENT_STAGES = {'INVALID': -1, 'STARTED': 0, 'COUNTDOWN': 1, 'WAITING': 2, 'END_BATTLE_IDLING': 3}
_TIMER_MODULES = {
    'scripts/BattleLogic.pyc': 'b39472a4ce5470a7df413d05294dd81207d70d1d47b52c9cef7beceb7244c081',
    'scripts/shared_constants/m0e80a902.pyc': '8ed7b58e781ab810afcb9597525cc61dddfb68b724359eeef331d3aecef16ba4',
}
_CAP_LOGIC_FIELD = 'InteractiveZone.componentsState.captureLogic'
_SCORE_FIELD = 'BattleLogic.state.missions.teamsScore'


class _NestedBits(BitReader):
    def _get_next_byte(self):
        value = self._stream.read(1)
        if not value:
            raise ValueError('嵌套字段路径不完整')
        return value


def _nested_field(obj, bits, *, terminal=False, is_slice=False):
    if isinstance(obj, Entity):
        index = bits.get(bits.bits_required(len(obj.client_properties)))
        prop = obj.client_properties[index]
        return prop.get_name(), prop, obj.properties['client']
    if isinstance(obj, PyFixedDict):
        index = bits.get(bits.bits_required(len(obj)))
        return obj.get_field_name_for_index(index), obj.get_field_type_for_index(index), obj
    if isinstance(obj, PyFixedList):
        width = bits.bits_required(len(obj) + is_slice)
        start = bits.get(width)
        if terminal and is_slice:
            end = bits.get(width)
            if not 0 <= start <= end <= len(obj):
                raise ValueError('嵌套字段切片超出范围')
            return slice(start, end), obj.get_element_type(), obj
        if start >= len(obj):
            raise ValueError('嵌套字段索引超出范围')
        return start, obj.get_element_type(), obj
    raise ValueError('嵌套字段容器类型无效')


def _update_nested(entity, payload, is_slice, paths):
    """Decode selected property paths, committing only a fully consumed value."""
    bits = _NestedBits(payload)
    obj, path = entity, ()
    while True:
        child = bits.get(1)
        field, field_type, container = _nested_field(
            obj, bits, terminal=not child, is_slice=is_slice and not child)
        path += (field,)
        if not any(path[:len(prefix)] == prefix or prefix[:len(path)] == path for prefix in paths):
            return None
        if not child:
            break
        obj = container[field]
    stream = BytesIO(bits.get_rest())
    if is_slice:
        if not isinstance(field, slice):
            raise ValueError('嵌套字段切片类型无效')
        values = []
        end = stream.getbuffer().nbytes
        while stream.tell() < end:
            position = stream.tell()
            values.append(field_type.create_from_stream(stream))
            if stream.tell() <= position:
                raise ValueError('嵌套切片没有消耗数据')
        if stream.tell() != end:
            raise ValueError('嵌套字段切片大小无效')
        value = values
    else:
        value = field_type.create_from_stream(stream)
        if stream.read(1):
            raise ValueError('嵌套字段含未解码字节')
    container[field] = value
    return path


@lru_cache(maxsize=1)
def _definitions():
    directory = _root() / 'schema1510'
    hashes = {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in directory.rglob('*') if p.is_file()}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    if digest != _profile(CURRENT + '.json')['entity_tree_sha256']:
        raise ValueError('15.10 战斗图层协议校验失败')
    return Definitions(str(directory))


class _LocalEntity(Entity):
    """Nested updates target this replay only, independent of global subscribers."""
    def set_client_nested_property(self, prop_path, obj):
        pass


class CurrentCombat(CombatCapture):
    # CommonConsumables.ConsumableConstants, client build 13533378.
    consumable_names = CONSUMABLE_NAMES[:66] + (
        'SMOKE_GENERATOR_HEAL', 'SMOKE_PLANE_HEAL', 'SHIELD_REGENERATION',
        'ALLY_SHIELD_REGENERATION', 'RLS_EVENT', 'ANY', 'ALL', 'SPECIAL')
    consumable_labels = {**LABELS, 66: '维修发烟器', 67: '维修烟幕飞机',
                         68: '护盾恢复', 69: '友舰护盾恢复', 70: '活动雷达'}

    def __init__(self):
        super().__init__()
        self.definitions = _definitions()
        self.registry = SimpleNamespace(_methods_subscriptions={}, _properties_subscriptions={},
                                        _nested_properties_subscription={})
        self.attach(self.registry)
        self.entities = {}
        self.caps = {}
        self.cap_teams = {}
        self.cap_states = {}
        self.team_scores = {}
        self.score_limits = []
        self.logic_evidence = BattleLogicEvidence()
        self._zone_positions = {}

    def _zone(self, entity):
        props = entity.properties['client']
        components = props.get('componentsState') or {}
        zone = components.get('controlPoint')
        team = props.get('teamId')
        if entity.id not in self.caps:
            if not isinstance(zone, dict):
                return
            position = self._zone_positions[entity.id]
            index, radius = zone.get('index'), props.get('radius')
            if (type(index) is not int or not 0 <= index < 26 or not isinstance(radius, (int, float))
                    or not all(math.isfinite(v) for v in (radius, position.x, position.z)) or radius <= 0):
                return
            self.caps[entity.id] = {'entity_id': entity.id, 'name': chr(65 + index),
                'x': position.x, 'z': position.z, 'radius': radius, 'initial_team': team}
        if isinstance(zone, dict) and type(team) is int and team >= -1:
            changes = self.cap_teams.setdefault(str(entity.id), [])
            if not changes or changes[-1][1] != team:
                changes.append([self.clock, team])
        self._capture_state(entity.id, components.get('captureLogic') if isinstance(zone, dict) else None)

    def _capture_state(self, entity_id, capture):
        key = str(entity_id)
        if capture is None and key not in self.cap_states:
            return
        capture = capture or {}
        rows = self.cap_states.setdefault(key, [])
        progress, invader = capture.get('progress'), capture.get('invaderTeam')
        values = {
            'capture_progress': progress if isinstance(progress, (int, float)) and 0 <= progress <= 1 else None,
            'capturing_team': invader if type(invader) is int and invader >= -1 else None,
            'has_invaders': _wire_bool(capture.get('hasInvaders')),
            'contested': _wire_bool(capture.get('bothInside')),
            'capture_enabled': _wire_bool(capture.get('isEnabled')),
        }
        if not rows or any(rows[-1][key] != value for key, value in values.items()):
            rows.append(values | {'packet_clock': self.clock, 'source': self.source(_CAP_LOGIC_FIELD)})

    def _battle_logic(self, entity, field=None):
        props = entity.properties['client']
        if field is None or field in _TIMER_PROPERTIES:
            self.logic_evidence.observe(entity.id, {key: props[key] for key in _TIMER_PROPERTIES if key in props},
                                        self.clock, self._source['packet_offset'], self._source['packet_type'])
        if field not in (None, 'state'):
            return
        missions = (props.get('state') or {}).get('missions')
        teams = missions.get('teamsScore') if missions else None
        if isinstance(teams, list):
            scores = {row['teamId']: row['score'] for row in teams
                      if type(row.get('teamId')) is int and row['teamId'] >= 0 and type(row.get('score')) is int}
            if len(scores) == len(teams):
                for team, score in scores.items():
                    rows = self.team_scores.setdefault(str(team), [])
                    if not rows or rows[-1][1] != score:
                        rows.append([self.clock, score])
        win = missions.get('teamWinScore') if missions else None
        lose = missions.get('teamLoseScore') if missions else None
        limits = {'win_score': win if win != -1 else None, 'lose_score': lose if lose != -1 else None}
        if (self.score_limits or any(value is not None for value in limits.values())) and (
                not self.score_limits or any(self.score_limits[-1][key] != value for key, value in limits.items())):
            self.score_limits.append(limits | {'packet_clock': self.clock,
                'source': self.source('BattleLogic.state.missions')})

    @staticmethod
    def _consume(stream):
        if stream.read(1):
            raise ValueError('战斗字段含未解码字节')

    def _create(self, raw):
        stream = BytesIO(raw)
        packet = EntityCreate(stream)
        self._consume(stream)
        spec = self.definitions.get_entity_def_by_index(packet.type)
        name = spec.get_name()
        if name not in ('Vehicle', 'SmokeScreen', 'InteractiveZone', 'BattleLogic'):
            return
        entity = _LocalEntity(packet.entityID, spec)
        stream = packet.state.io()
        count, = struct.unpack('B', stream.read(1))
        for _ in range(count):
            idx, = struct.unpack('B', stream.read(1))
            prop = entity.client_properties[idx]
            entity.properties['client'][prop.get_name()] = prop.create_from_stream(stream)
        self._consume(stream)
        self.entities[entity.id] = entity
        self.after_packet(packet, self.entities)
        if name == 'InteractiveZone':
            self._zone_positions[entity.id] = packet.position
            self._zone(entity)
        elif name == 'BattleLogic':
            self._battle_logic(entity)

    def _apply(self, frame):
        raw = frame.payload
        if frame.type == 0 and len(raw) >= 10:
            entity, kind, size = struct.unpack_from('<IHI', raw)
            if len(raw) == 10 + size and kind == _profile(CURRENT+'.json')['entity_types']['Avatar']:
                self.entities[entity] = _LocalEntity(entity, self.definitions.get_entity_def_by_name('Avatar'))
        elif frame.type == 5:
            self._create(raw)
        elif frame.type in (7, 8):
            eid, index, size = struct.unpack_from('<III', raw)
            entity = self.entities.get(eid)
            if not entity or len(raw) != 12 + size:
                return
            name = entity.get_name()
            stream = BytesIO(raw[12:])
            if frame.type == 8:
                method = entity._methods[index]
                key = name + '_' + method.get_name()
                callbacks = self.registry._methods_subscriptions.get(key)
                if not callbacks:
                    return
                args, kwargs = method.create_from_stream(stream)
                self._consume(stream)
                for callback in callbacks:
                    callback(entity, *args, **kwargs)
            else:
                prop = entity.client_properties[index]
                field = prop.get_name()
                callbacks = self.registry._properties_subscriptions.get(name + '_' + field, ())
                recorded_zone = name == 'InteractiveZone' and field in ('teamId', 'componentsState', 'radius')
                recorded_logic = name == 'BattleLogic' and (field == 'state' or field in _TIMER_PROPERTIES)
                if not (callbacks or recorded_zone or recorded_logic):
                    return
                value = prop.create_from_stream(stream)
                self._consume(stream)
                entity.properties['client'][field] = value
                for callback in callbacks:
                    callback(entity, value)
                if name == 'InteractiveZone':
                    self._zone(entity)
                elif name == 'BattleLogic':
                    self._battle_logic(entity, field)
                elif name == 'SmokeScreen':
                    self._smoke_sample(entity)
                    self._smoke_dirty.pop(eid, None)
        elif frame.type == 35 and len(raw) >= 9:
            eid, is_slice, size = struct.unpack_from('<IBI', raw)
            entity = self.entities.get(eid)
            if not entity or size != len(raw)-9 or is_slice not in (0, 1):
                return
            name = entity.get_name()
            if name == 'SmokeScreen':
                packet = NestedProperty.__new__(NestedProperty)
                packet.is_slice, packet.payload = bool(is_slice), raw[9:]
                packet.read_and_apply(entity)
                self._smoke_sample(entity)
            elif name == 'BattleLogic':
                paths = (('state', 'missions'), *[(field,) for field in _TIMER_PROPERTIES])
                path = _update_nested(entity, raw[9:], bool(is_slice), paths)
                if path:
                    self._battle_logic(entity, path[0])
            elif name == 'InteractiveZone':
                paths = (('componentsState',), ('teamId',), ('radius',))
                if _update_nested(entity, raw[9:], bool(is_slice), paths):
                    self._zone(entity)
        elif frame.type == 4 and len(raw) == 4:
            eid, = struct.unpack('<I', raw)
            if eid in self._smokes:
                smoke = self._smokes.pop(eid)
                smoke.update(end_clock=self.clock, end_reason='observation_ended_aoi_leave', end_source=self.source())
                self._closed_smokes.append(smoke)
            if eid in self._zone_positions:
                self._zone_positions.pop(eid)
                self.entities.pop(eid)
                if str(eid) in self.cap_states:
                    self._capture_state(eid, None)

    def observe(self, frame):
        if frame.type not in (0, 4, 5, 7, 8, 35):
            return
        self.begin_packet(frame)
        try:
            self._apply(frame)
        except (ValueError, KeyError, TypeError, IndexError, struct.error, AssertionError, NotImplementedError) as exc:
            if len(self.diagnostics['errors']) < 30:
                self.diagnostics['errors'].append({'packet_type': frame.type, 'clock': frame.clock,
                                                  'offset': frame.offset, 'error': str(exc)[:140]})

    def finish(self, roster, end_clock):
        combat = super().finish(roster, end_clock)
        profile = _profile(CURRENT + '.json')
        combat.update(client_version=CURRENT, provenance={'client_version': CURRENT,
            'entity_tree_sha256': profile['entity_tree_sha256'], 'source_archive_sha256': profile['source_archive_sha256'],
            'semantic_evidence': '13533378 client entity definitions and CommonConsumables'})
        return combat

    def battle_state(self, end_clock, result_duration):
        profile = _profile(CURRENT + '.json')
        timing = None
        if self.logic_evidence.time_samples:
            timing = analyze_timebase(self.logic_evidence.time_samples, result_duration_sec=result_duration,
                                      packet_clock_max=end_clock, stage_values=_CURRENT_STAGES)
            timing['model'] = '15.10_observed_client_timer'
        return {
            'battle_state_schema_version': BATTLE_STATE_SCHEMA_VERSION,
            'battle_state_provenance': {
                'client_version': CURRENT, 'entity_tree_sha256': profile['entity_tree_sha256'],
                'source_archive_sha256': profile['source_archive_sha256'],
                'score_field': _SCORE_FIELD, 'owner_field': 'InteractiveZone.teamId',
                'capture_field': _CAP_LOGIC_FIELD, 'timer_modules': _TIMER_MODULES,
            },
            'capture_points': list(self.caps.values()),
            'cap_team_changes': self.cap_teams,
            'cap_state_samples': self.cap_states,
            'team_score_samples': self.team_scores,
            'team_score_limit_samples': self.score_limits,
            'time_analysis': timing,
        }


def _wire_bool(value):
    return bool(value) if type(value) is int and value in (0, 1) else None
