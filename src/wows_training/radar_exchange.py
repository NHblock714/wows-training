"""Portable saved radar lists: bounded JSON, atomic export and transactional merge."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile

from .player_radar import DIMENSIONS, MAX_CARDS, SCORING_VERSION, evaluate_card


FORMAT = 'wows-player-radar-list'
SCHEMA_VERSION = 2
LEGACY_SCORING_VERSION = 'player-radar-linear-pr-1'
MAX_FILE_BYTES = 16 * 1024 * 1024
LEGACY_CARD_FIELDS = ('team', 'player_name', 'region', 'account_id', 'role', 'ship_id',
                      'specialties', 'scores', 'account_evidence', 'pr_max')
CARD_FIELDS = (*LEGACY_CARD_FIELDS, 'account_score_mode', 'manual_account_score')
EVIDENCE_FIELDS = {'status', 'pr', 'region', 'account_id', 'nickname', 'mode', 'time_kind',
                   'timestamp', 'snapshot_id', 'coverage', 'reference', 'source_type',
                   'covered_battles', 'total_battles'}
REFERENCE_FIELDS = {'expected_reference', 'encyclopedia_reference', 'pinned', 'legacy_compatibility'}
SOURCE_FIELDS = {'provider', 'url', 'region', 'language', 'game_version', 'timestamp',
                 'time_kind', 'fetched_at_utc', 'sha256', 'snapshot_id'}


def _portable(card):
    normalized = evaluate_card(card)
    return {key: deepcopy(normalized[key]) for key in CARD_FIELDS}


def _validate_card(value, index, schema_version=SCHEMA_VERSION):
    prefix = f'第 {index + 1} 个选手'
    required = LEGACY_CARD_FIELDS if schema_version == 1 else CARD_FIELDS
    if not isinstance(value, dict) or set(value) != set(required):
        raise ValueError(prefix + '字段不完整或不是八维图档案')
    scores = value['scores']
    if not isinstance(scores, dict) or set(scores) != {key for key, _ in DIMENSIONS}:
        raise ValueError(prefix + '必须提供完整的八维评分字段')
    for number in scores.values():
        if number is not None and (type(number) not in (int, float)
                or not math.isfinite(number) or not 0 <= number <= 120):
            raise ValueError(prefix + '含有无效的维度分数')
    evidence = value['account_evidence']
    if evidence is not None:
        if not isinstance(evidence, dict) or set(evidence) - EVIDENCE_FIELDS:
            raise ValueError(prefix + '的账号来源字段无效')
        reference = evidence.get('reference')
        if reference is not None:
            if not isinstance(reference, dict) or set(reference) - REFERENCE_FIELDS:
                raise ValueError(prefix + '的评分基准字段无效')
            for name in ('expected_reference', 'encyclopedia_reference'):
                source = reference.get(name)
                if source is not None and (not isinstance(source, dict) or set(source) - SOURCE_FIELDS):
                    raise ValueError(prefix + '的评分基准来源无效')
    source = dict(value, account_score_mode='pr', manual_account_score=None) if schema_version == 1 else value
    result = _portable(source)
    if not result['player_name']:
        raise ValueError(prefix + '缺少个人 ID')
    actual, supplied = result['scores']['account'], scores['account']
    if (actual is None) != (supplied is None) or (actual is not None and not math.isclose(actual, supplied, rel_tol=1e-10, abs_tol=1e-8)):
        raise ValueError(prefix + '的账号轴与评分来源、原始 PR 或换算基准不一致')
    return result


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('列表包含重复 JSON 字段')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('列表包含无效数值')


def _bounded_tree(value):
    stack = [(value, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if depth > 12 or count > 250000:
            raise ValueError('列表结构过深或过大')
        if isinstance(node, dict):
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)


def export_saved_list(store, path):
    """Export only saved cards; raw PR and per-card calibration remain portable."""
    cards = [_portable(card) for card in store.list_cards()]
    if len(cards) > MAX_CARDS:
        raise ValueError('列表超过 2000 个选手上限')
    value = {'format': FORMAT, 'schema_version': SCHEMA_VERSION,
             'scoring_version': SCORING_VERSION,
             'exported_at': datetime.now(timezone.utc).isoformat(), 'cards': cards}
    encoded = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf8')
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError('列表超过 16 MiB 导出上限')
    destination = Path(path).expanduser().absolute()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.radar-list-', suffix='.writing',
                                         dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {'path': str(destination), 'count': len(cards), 'bytes': len(encoded)}


def import_saved_list(store, path):
    """Validate the entire document before a single append-only transaction."""
    source = Path(path).expanduser().absolute()
    with source.open('rb') as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError('导入列表超过 16 MiB 上限')
    try:
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_object,
                           parse_constant=_invalid_constant)
        _bounded_tree(value)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ValueError('无法读取八维图列表：JSON 格式或内容无效') from error
    if (not isinstance(value, dict) or set(value) != {'format', 'schema_version', 'scoring_version', 'exported_at', 'cards'}
            or value.get('format') != FORMAT or type(value.get('schema_version')) is not int
            or value['schema_version'] not in (1, SCHEMA_VERSION)
            or value.get('scoring_version') != (LEGACY_SCORING_VERSION if value['schema_version'] == 1 else SCORING_VERSION)):
        raise ValueError('文件不是受支持版本的选手八维图列表')
    stamp = value.get('exported_at')
    if not isinstance(stamp, str) or len(stamp) > 128:
        raise ValueError('列表导出时间格式无效')
    try:
        datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('列表导出时间格式无效') from None
    rows = value.get('cards')
    if not isinstance(rows, list) or len(rows) > MAX_CARDS:
        raise ValueError('列表必须包含不超过 2000 个选手的数组')
    try:
        cards = [_validate_card(card, index, value['schema_version']) for index, card in enumerate(rows)]
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError('列表校验未通过：' + str(error)) from error
    return dict(store.import_cards(cards), path=str(source), source_count=len(cards))
