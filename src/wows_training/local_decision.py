"""Verified CPU-only scoring for a selected original replay."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


PACK_SHA = 'fa52cc3990140e0aaaaf65b6b4943a3ac80781e9eb8e8d5c9c4f9c40d6811dba'
REPLAY_LIMIT = 512 << 20
RESULT_LIMIT = 16 << 20
SURFACE = {'Destroyer': '驱逐舰', 'Cruiser': '巡洋舰', 'Battleship': '战列舰'}
BUILDS = {'15.3.0.12267945', '15.9.0.13357625', '15.10.0.13533378'}


class DecisionError(ValueError):
    pass


def bundle_path():
    base = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2]
    return base/'decision_models'


def sha_ok(text):
    return isinstance(text, str) and re.fullmatch('[0-9a-f]{64}', text) is not None


def source_issue(report):
    if not report:
        return '打开一份原始录像后可运行模型。'
    if report.get('multiview'):
        return '请切换到单一录制视角。'
    if not sha_ok(report.get('sha256')):
        return '录像缺少已确认的文件身份。'
    raw = (report.get('metadata') or {}).get('clientVersionFromExe') or report.get('replay_version') or ''
    version = re.sub(r'\s+', '', str(raw)).replace(',', '.')
    if version not in BUILDS:
        return '该版本的决策特征尚未验证。已适配 15.3、15.9 和 15.10 的已验证客户端。'
    return ''


def _cancelled(event):
    if event is not None and event.is_set():
        raise DecisionError('分析已取消。')


def _sha_file(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_pack(root=None):
    root = Path(root or bundle_path()).resolve()
    try:
        manifest_file = root/'pack.json'
        if manifest_file.stat().st_size > 65536 or _sha_file(manifest_file) != PACK_SHA:
            raise DecisionError('本地模型清单未通过校验。')
        files = json.loads(manifest_file.read_bytes())['files']
        for relative, expected in files.items():
            file = (root/relative).resolve()
            if not file.is_relative_to(root) or _sha_file(file) != expected:
                raise DecisionError('本地模型资源未通过校验，请使用完整桌面包。')
        return root
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise DecisionError('本地模型未完整安装，请使用完整桌面包。') from exc


def _prob(value):
    return type(value) in (float,int) and math.isfinite(value) and 0 <= value <= 1


def _validate(result):
    if not isinstance(result, dict) or result.get('schema') != 1 or not isinstance(result.get('perShip'),list):
        raise DecisionError('模型结果格式无效。')
    ships = result['perShip']
    ids = set()
    if (len(ships)>128 or type(result.get('terrainAvailable')) is not bool
            or result.get('recorderEntity') is not None and type(result['recorderEntity']) is not int):
        raise DecisionError('模型结果超出上限。')
    for ship in ships:
        if (not isinstance(ship,dict) or ship.get('shipClass') not in SURFACE or type(ship.get('entityId')) is not int
                or type(ship.get('shipId')) is not int or type(ship.get('teamId')) is not int
                or ship['teamId'] not in (0,1) or type(ship.get('isRecorder')) is not bool
                or ship['isRecorder'] != (ship['entityId']==result.get('recorderEntity'))):
            raise DecisionError('模型舰船身份无效。')
        if ship['entityId'] in ids:
            raise DecisionError('模型舰船身份重复。')
        ids.add(ship['entityId'])
        samples = ship.get('samples')
        if not isinstance(samples,list) or not 1<=len(samples)<=4096:
            raise DecisionError('模型时间线无效。')
        if 'wholeFire' not in ship or any(not _prob(ship.get(k)) for k in ('wholeGame','wholeMove')) or (ship.get('wholeFire') is not None and not _prob(ship['wholeFire'])):
            raise DecisionError('模型汇总值无效。')
        prev=-1.
        for point in samples:
            if not isinstance(point,dict) or type(point.get('t')) not in (float,int) or not math.isfinite(point['t']) or point['t'] <= prev:
                raise DecisionError('模型时间点无效。')
            if 'fire' not in point or any(not _prob(point.get(k)) for k in ('inst','move','confidence')) or (point.get('fire') is not None and not _prob(point['fire'])):
                raise DecisionError('模型评分无效。')
            prev=point['t']
    return result


def _snapshot(source, target, expected_sha, cancel_event):
    if source.suffix.lower() != '.wowsreplay' or not source.is_file():
        raise DecisionError('请选择这场比赛对应的原始 .wowsreplay 文件。')
    if not 0 < source.stat().st_size <= REPLAY_LIMIT:
        raise DecisionError('原始录像为空或超过 512 MiB。')
    digest = hashlib.sha256()
    count=0
    with source.open('rb') as stream, target.open('xb') as saved:
        while block := stream.read(1024*1024):
            _cancelled(cancel_event)
            count+=len(block)
            if count>REPLAY_LIMIT:
                raise DecisionError('原始录像超过 512 MiB。')
            digest.update(block)
            saved.write(block)
    if digest.hexdigest()!=expected_sha:
        raise DecisionError('这份文件与当前录像不匹配，请选择对应的原始录制。')


def analyze(report, replay_path, cancel_event=None):
    issue=source_issue(report)
    if issue:
        raise DecisionError(issue)
    pack=verify_pack()
    _cancelled(cancel_event)
    try:
        with tempfile.TemporaryDirectory(prefix='wows-model-') as directory:
            tmp=Path(directory)
            source=tmp/'source.wowsreplay'
            _snapshot(Path(replay_path), source, report['sha256'], cancel_event)
            result_file=tmp/'score.json'
            with result_file.open('wb') as output:
                proc=subprocess.Popen([str(pack/'wows-decision.exe'), str(source)], cwd=pack,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                deadline=time.monotonic()+120
                try:
                    while proc.poll() is None:
                        _cancelled(cancel_event)
                        if time.monotonic()>=deadline or result_file.stat().st_size>RESULT_LIMIT:
                            raise DecisionError('模型分析超出时间或输出上限。')
                        if cancel_event is not None:
                            cancel_event.wait(.06)
                        else:
                            time.sleep(.06)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait()
            _cancelled(cancel_event)
            if proc.returncode or not 0<result_file.stat().st_size<=RESULT_LIMIT:
                raise DecisionError('模型未能解析这份录像的决策事件。')
            result=_validate(json.loads(result_file.read_bytes()))
            result.update(model='WoWSP-D1', modelSequence=3, sourceSha=report['sha256'], packSha=PACK_SHA)
            return result
    except (OSError, json.JSONDecodeError) as exc:
        raise DecisionError('无法读取录像或运行本地模型。') from exc


def cache_path(data_root, source_sha):
    if not sha_ok(source_sha):
        raise DecisionError('录像身份无效。')
    return Path(data_root)/'local_decisions'/(source_sha + '-' + PACK_SHA[:12] + '.json')


def load_cache(data_root, source_sha):
    path=cache_path(data_root, source_sha)
    try:
        if not 0<path.stat().st_size<=RESULT_LIMIT:
            return None
        value=json.loads(path.read_bytes())
        if value.get('sourceSha')==source_sha and value.get('packSha')==PACK_SHA:
            return _validate(value)
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        pass
    return None


def save_result(result, destination):
    path=Path(destination)
    encoded=json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf8')
    if len(encoded)>RESULT_LIMIT:
        raise DecisionError('模型结果超出上限。')
    path.parent.mkdir(parents=True,exist_ok=True)
    fd, name=tempfile.mkstemp(dir=path.parent,prefix='.wows-decision-',suffix='.tmp')
    try:
        with os.fdopen(fd,'wb') as handle:
            handle.write(encoded)
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)
