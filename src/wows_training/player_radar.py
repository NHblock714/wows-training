"""Manual eight-axis player dossiers with separately evidenced public PR."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4


DIMENSIONS = (
    ('experience', '经验资历'), ('awareness', '大局观'), ('account', '账号数据'),
    ('mechanics', '个人能力'), ('consistency', '稳定性'), ('ceiling', '上限发挥'),
    ('synergy', '队伍相性'), ('leadership', '组织能力'),
)
SPECIALTIES = ('Destroyer', 'Cruiser', 'Battleship')
GRADES = ('F-', 'F', 'F+', 'E-', 'E', 'E+', 'D-', 'D', 'D+', 'C-', 'C', 'C+',
          'B-', 'B', 'B+', 'A-', 'A', 'A+', 'S-', 'S', 'S+')
DEFAULT_SETTINGS = {'default_pr_max': 3000.0, 'interval_seconds': 8}
SCORING_VERSION = 'player-radar-pr-or-manual-2'
MAX_CARDS = 2000
MAX_CARD_BYTES = 65536
STATUSES = {'ok', 'partial', 'private', 'no_battles', 'not_fetched', 'not_found', 'error', 'unsupported'}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _text(value, field, maximum, *, multiline=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f'{field}长度或格式无效')
    if any(ord(c) < 32 and not (multiline and c == '\n') for c in value):
        raise ValueError(f'{field}包含无效控制字符')
    return value.strip()


def _optional_text(value, field, maximum):
    return None if value is None else _text(value, field, maximum)


def _identifier(value, field):
    if value is None or value == '':
        return None
    raw = str(value)
    if (type(value) not in (int, str) or not raw.isascii() or not raw.isdigit()
            or len(raw) > 20 or int(raw) <= 0):
        raise ValueError(f'{field}必须是有效的数字 ID')
    return str(int(raw))


def _region(value):
    if not isinstance(value, str):
        raise ValueError('区服无效')
    value = value.lower()
    if value == 'com':
        value = 'na'
    if value not in ('asia', 'eu', 'na'):
        raise ValueError('区服无效')
    return value


def _pr_max(value):
    if not _number(value) or not 100 <= value <= 100000:
        raise ValueError('PR 满分基准须在 100–100000 之间')
    return float(value)


def grade_score(value):
    """Project display grades: six-point steps, with S+ reserved for 120."""
    if value is None:
        return '—'
    if not _number(value) or not 0 <= value <= 120:
        raise ValueError('维度分数须在 0–120 之间')
    return GRADES[min(20, int(value // 6))]


def pr_to_score(pr, pr_max=3000):
    maximum = _pr_max(pr_max)
    if pr is None:
        return None
    if not _number(pr) or pr < 0:
        raise ValueError('PR 必须是非负有限数值')
    return min(120.0, float(pr) / maximum * 120.0)


def _reference_summary(value):
    """Keep provenance fields, never arbitrary account responses or credentials."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('评分基准来源无效')
    result = {}
    for name in ('expected_reference', 'encyclopedia_reference'):
        source = value.get(name)
        if source is None:
            continue
        if not isinstance(source, dict):
            raise ValueError('评分基准来源无效')
        result[name] = {}
        for key in ('provider', 'url', 'region', 'language', 'game_version', 'timestamp',
                    'time_kind', 'fetched_at_utc', 'sha256', 'snapshot_id'):
            if source.get(key) is not None:
                text = _text(source[key], '评分基准来源', 2048)
                # One canonical public URL is used for both saved and portable evidence.
                if key == 'url':
                    try:
                        address = urlsplit(text)
                        if address.scheme not in ('http', 'https') or not address.hostname:
                            continue
                        hostname = '[' + address.hostname + ']' if ':' in address.hostname else address.hostname
                        port = ':' + str(address.port) if address.port else ''
                        text = urlunsplit((address.scheme, hostname + port, address.path, '', ''))
                    except ValueError:
                        continue
                result[name][key] = text
    for key in ('pinned', 'legacy_compatibility'):
        if key in value:
            result[key] = value[key] is True
    return result


def _evidence(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('公开账号证据无效')
    status = value.get('status', 'not_fetched')
    if not isinstance(status, str) or status not in STATUSES:
        raise ValueError('公开账号状态无效')
    region = _region(value.get('region', 'asia'))
    account_id = _identifier(value.get('account_id'), '公开账号')
    mode = _text(value.get('mode', 'pvp'), '战斗模式', 32)
    pr = value.get('pr')
    if pr is not None and (not _number(pr) or pr < 0):
        raise ValueError('PR 必须是非负有限数值')
    if status not in ('ok', 'partial') or mode != 'pvp' or account_id is None:
        pr = None
    coverage = value.get('coverage')
    if coverage is not None and (not _number(coverage) or not 0 <= coverage <= 1):
        raise ValueError('PR 覆盖比例无效')
    result = {
        'status': status, 'pr': None if pr is None else float(pr), 'region': region,
        'account_id': account_id, 'nickname': _text(value.get('nickname') or '', '公开昵称', 128),
        'mode': mode, 'time_kind': _optional_text(value.get('time_kind'), '时间来源', 64),
        'timestamp': _optional_text(value.get('timestamp'), '快照时间', 128),
        'snapshot_id': _optional_text(value.get('snapshot_id'), '快照标识', 128),
        'coverage': None if coverage is None else float(coverage),
        'reference': _reference_summary(value.get('reference')),
        'source_type': _optional_text(value.get('source_type'), '公开资料来源', 64),
    }
    for key in ('covered_battles', 'total_battles'):
        number = value.get(key)
        if number is not None and (type(number) is not int or not 0 <= number <= 10**9):
            raise ValueError('PR 场次证据无效')
        result[key] = number
    if (result['covered_battles'] is not None and result['total_battles'] is not None
            and result['covered_battles'] > result['total_battles']):
        raise ValueError('PR 场次证据不一致')
    return result


def account_from_profile(profile):
    """Extract the aggregate random-battle PR from an existing career projection."""
    if not isinstance(profile, dict):
        raise ValueError('公开账号资料无效')
    rating = profile.get('rating') if isinstance(profile.get('rating'), dict) else {}
    reference = profile.get('reference_context')
    if reference is None:
        reference = rating.get('baseline') if isinstance(rating.get('baseline'), dict) else {}
    return _evidence({
        'status': profile.get('status', 'not_fetched'), 'pr': rating.get('pr'),
        'region': profile.get('region', 'asia'), 'account_id': profile.get('account_id'),
        'nickname': profile.get('nickname') or '', 'mode': profile.get('mode', 'pvp'),
        'time_kind': profile.get('time_kind'), 'timestamp': profile.get('timestamp'),
        'snapshot_id': profile.get('snapshot_id'), 'coverage': rating.get('coverage_ratio'),
        'covered_battles': rating.get('covered_battles'), 'total_battles': rating.get('total_battles'),
        'reference': reference, 'source_type': profile.get('source_type'),
    })


def evaluate_card(card):
    """Validate draft input and derive eight scores without mutating the caller."""
    if not isinstance(card, dict):
        raise ValueError('选手档案必须是对象')
    region = _region(card.get('region', 'asia'))
    account_id = _identifier(card.get('account_id'), '公开账号')
    specialties = card.get('specialties', [])
    if not isinstance(specialties, (list, tuple)) or any(s not in SPECIALTIES for s in specialties):
        raise ValueError('擅长舰种无效')
    manual = card.get('scores', {})
    if not isinstance(manual, dict) or set(manual) - {key for key, _ in DIMENSIONS}:
        raise ValueError('八维评分字段无效')
    evidence = _evidence(card.get('account_evidence'))
    if evidence is not None and (evidence['region'] != region or evidence['account_id'] != account_id):
        raise ValueError('公开账号证据与当前区服或账号 ID 不一致，请重新查询')
    maximum = _pr_max(card.get('pr_max', 3000))
    account_mode = card.get('account_score_mode', 'pr')
    if account_mode not in ('pr', 'manual'):
        raise ValueError('账号评分来源须为 PR 换算或手动输入')
    manual_account = card.get('manual_account_score')
    if manual_account is not None and (not _number(manual_account) or not 0 <= manual_account <= 120):
        raise ValueError('手动账号分数须在 0–120 之间')
    manual_account = None if manual_account is None else float(manual_account)
    scores = {}
    for key, _ in DIMENSIONS:
        if key == 'account':
            scores[key] = manual_account if account_mode == 'manual' else (pr_to_score(evidence['pr'], maximum) if evidence is not None else None)
        else:
            value = manual.get(key)
            if value is not None and (not _number(value) or not 0 <= value <= 120):
                raise ValueError('维度分数须在 0–120 之间')
            scores[key] = None if value is None else float(value)
    known = sum(value is not None for value in scores.values())
    overall = sum(scores.values()) / len(DIMENSIONS) if known == len(DIMENSIONS) else None
    return {
        'team': _text(card.get('team', ''), '队伍', 64),
        'player_name': _text(card.get('player_name', ''), '个人 ID', 128),
        'region': region, 'account_id': account_id,
        'role': _text(card.get('role', ''), '个人定位', 240, multiline=True),
        'ship_id': _identifier(card.get('ship_id'), '立绘舰船'),
        'specialties': [kind for kind in SPECIALTIES if kind in specialties],
        'scores': scores, 'account_evidence': evidence, 'pr_max': maximum,
        'account_score_mode': account_mode, 'manual_account_score': manual_account,
        'grades': {key: grade_score(value) for key, value in scores.items()},
        'known_count': known, 'overall_score': overall, 'overall_grade': grade_score(overall),
        'scoring_version': SCORING_VERSION,
    }


def _encoded(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(text.encode('utf8')) > MAX_CARD_BYTES:
        raise ValueError('选手档案超过保存大小限制')
    return text


class RadarStore:
    """A separate, versioned SQLite notebook for manual player radar cards."""
    def __init__(self, root):
        self.root = Path(root).expanduser().absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()
        self.database_path = self.root / 'player_radar.sqlite3'
        with self._connection() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError('不支持的八维图资料版本')
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS cards(
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    position INTEGER NOT NULL, deleted INTEGER NOT NULL,
                    payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS cards_order ON cards(deleted,position,id);
                CREATE TABLE IF NOT EXISTS revisions(
                    card_id TEXT NOT NULL REFERENCES cards(id), revision INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL, action TEXT NOT NULL,
                    PRIMARY KEY(card_id,revision));
                CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), payload_json TEXT NOT NULL);
                PRAGMA user_version=1;
            ''')

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _decode(row):
        values = dict(row)
        result = json.loads(values.pop('payload_json'))
        result.setdefault('account_score_mode', 'pr')
        result.setdefault('manual_account_score', None)
        result.update(values)
        result['deleted'] = bool(result['deleted'])
        return result

    @staticmethod
    def _record_revision(db, record, action):
        db.execute('INSERT INTO revisions VALUES(?,?,?,?)',
                   (record['id'], record['revision'], _encoded(record), action))

    @staticmethod
    def _compact(db):
        ids = [row['id'] for row in db.execute('SELECT id FROM cards WHERE deleted=0 ORDER BY position,id')]
        for position, identifier in enumerate(ids):
            db.execute('UPDATE cards SET position=? WHERE id=?', (position, identifier))

    def save(self, card, card_id=None, expected_revision=None):
        value = evaluate_card(card)
        if not value['player_name']:
            raise ValueError('请填写个人 ID')
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise ValueError('档案修订号无效')
        if card_id is None and expected_revision is not None:
            raise ValueError('新档案不能指定已有修订号')
        encoded = _encoded(value)
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            stamp = _now()
            if card_id is None:
                if db.execute('SELECT COUNT(*) FROM cards WHERE deleted=0').fetchone()[0] >= MAX_CARDS:
                    raise ValueError('八维图列表已达上限')
                identifier = str(uuid4())
                position = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM cards WHERE deleted=0').fetchone()[0]
                record = dict(value, id=identifier, revision=1, position=position, deleted=False,
                              created_at=stamp, updated_at=stamp)
                db.execute('INSERT INTO cards VALUES(?,?,?,?,?,?,?)',
                           (identifier, 1, position, 0, encoded, stamp, stamp))
            else:
                row = db.execute('SELECT * FROM cards WHERE id=? AND deleted=0', (card_id,)).fetchone()
                if row is None:
                    raise KeyError(card_id)
                previous = self._decode(row)
                if expected_revision is not None and previous['revision'] != expected_revision:
                    raise ValueError('档案已被其他操作更新，请重新载入后保存')
                record = dict(value, id=card_id, revision=previous['revision'] + 1,
                              position=previous['position'], deleted=False,
                              created_at=previous['created_at'], updated_at=stamp)
                db.execute('UPDATE cards SET revision=?,payload_json=?,updated_at=? WHERE id=?',
                           (record['revision'], encoded, stamp, card_id))
            self._record_revision(db, record, 'save')
            db.commit()
        return deepcopy(record)

    def list_cards(self, include_deleted=False):
        with self._connection() as db:
            clause = '' if include_deleted else ' WHERE deleted=0'
            return [self._decode(row) for row in db.execute('SELECT * FROM cards' + clause + ' ORDER BY deleted,position,id')]

    def import_cards(self, cards):
        """Append a validated list atomically, ignoring portable IDs and duplicates."""
        if not isinstance(cards, list) or len(cards) > MAX_CARDS:
            raise ValueError('导入列表最多包含 2000 个选手')
        prepared = []
        for card in cards:
            value = evaluate_card(card)
            if not value['player_name']:
                raise ValueError('导入选手缺少个人 ID')
            prepared.append((value, _encoded(value)))
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = list(db.execute('SELECT payload_json FROM cards WHERE deleted=0'))
            seen = {_encoded(evaluate_card(json.loads(row['payload_json']))) for row in existing}
            additions = []
            duplicates = 0
            for value, encoded in prepared:
                if encoded in seen:
                    duplicates += 1
                else:
                    seen.add(encoded)
                    additions.append((value, encoded))
            if len(existing) + len(additions) > MAX_CARDS:
                raise ValueError('合并后超过 2000 个选手上限，未导入任何记录')
            position = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM cards WHERE deleted=0').fetchone()[0]
            stamp = _now()
            ids = []
            for value, encoded in additions:
                identifier = str(uuid4())
                record = dict(value, id=identifier, revision=1, position=position, deleted=False,
                              created_at=stamp, updated_at=stamp)
                db.execute('INSERT INTO cards VALUES(?,?,?,?,?,?,?)',
                           (identifier, 1, position, 0, encoded, stamp, stamp))
                self._record_revision(db, record, 'import')
                ids.append(identifier)
                position += 1
            db.commit()
        return {'added': len(ids), 'duplicates': duplicates, 'ids': ids,
                'total': len(existing) + len(ids)}

    def get(self, card_id, include_deleted=False):
        with self._connection() as db:
            clause = '' if include_deleted else ' AND deleted=0'
            row = db.execute('SELECT * FROM cards WHERE id=?' + clause, (card_id,)).fetchone()
            if row is None:
                raise KeyError(card_id)
            return self._decode(row)

    def history(self, card_id):
        with self._connection() as db:
            return [dict(json.loads(row['snapshot_json']), history_action=row['action'])
                    for row in db.execute('SELECT snapshot_json,action FROM revisions WHERE card_id=? ORDER BY revision', (card_id,))]

    def delete(self, card_id):
        return self._set_deleted(card_id, True)

    def restore(self, card_id):
        return self._set_deleted(card_id, False)

    def _set_deleted(self, card_id, deleted):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM cards WHERE id=?', (card_id,)).fetchone()
            if row is None:
                raise KeyError(card_id)
            record = self._decode(row)
            if record['deleted'] == deleted:
                db.commit()
                return record
            if not deleted:
                if db.execute('SELECT COUNT(*) FROM cards WHERE deleted=0').fetchone()[0] >= MAX_CARDS:
                    raise ValueError('八维图列表已达上限')
                record['position'] = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM cards WHERE deleted=0').fetchone()[0]
            record.update(deleted=deleted, revision=record['revision'] + 1, updated_at=_now())
            db.execute('UPDATE cards SET deleted=?,revision=?,position=?,updated_at=? WHERE id=?',
                       (int(deleted), record['revision'], record['position'], record['updated_at'], card_id))
            self._record_revision(db, record, 'delete' if deleted else 'restore')
            self._compact(db)
            db.commit()
        return self.get(card_id, include_deleted=True)

    def reorder(self, ids):
        if (not isinstance(ids, (list, tuple)) or any(not isinstance(i, str) for i in ids)
                or len(ids) != len(set(ids)) or len(ids) > MAX_CARDS):
            raise ValueError('列表顺序必须包含每个档案且不能重复')
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current = {row['id'] for row in db.execute('SELECT id FROM cards WHERE deleted=0')}
            if set(ids) != current:
                raise ValueError('列表顺序与当前档案不一致，请刷新后重试')
            for position, identifier in enumerate(ids):
                db.execute('UPDATE cards SET position=? WHERE id=?', (position, identifier))
            db.commit()
        return self.list_cards()

    def settings(self):
        with self._connection() as db:
            row = db.execute('SELECT payload_json FROM settings WHERE id=1').fetchone()
        return dict(DEFAULT_SETTINGS, **(json.loads(row['payload_json']) if row else {}))

    def save_settings(self, default_pr_max=None, interval_seconds=None):
        if default_pr_max is not None:
            default_pr_max = _pr_max(default_pr_max)
        if interval_seconds is not None and (type(interval_seconds) is not int or not 2 <= interval_seconds <= 120):
            raise ValueError('播放间隔须为 2–120 秒')
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload_json FROM settings WHERE id=1').fetchone()
            settings = dict(DEFAULT_SETTINGS, **(json.loads(row['payload_json']) if row else {}))
            if default_pr_max is not None:
                settings['default_pr_max'] = default_pr_max
            if interval_seconds is not None:
                settings['interval_seconds'] = interval_seconds
            db.execute('INSERT INTO settings VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json',
                       (_encoded(settings),))
            db.commit()
        return settings
