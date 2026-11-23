"""Bounded replay input expansion and cancellable, process-isolated importing."""
from __future__ import annotations

from collections import Counter, defaultdict
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile

from .contracts import parse_version
from .library import Library

MAX_REPLAYS = 256
MAX_REPLAY_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
MAX_DIRECTORY_ENTRIES = 10000
MAX_INPUT_ROOTS = 256
MAX_TAR_LIST_BYTES = 8 * 1024 * 1024
PARSE_TIMEOUT_SECONDS = 120
EXTRACT_TIMEOUT_SECONDS = 60
SUPPORTED_VERSION = (15, 9, 0, 13357625)


def _cancelled(cancel):
    return cancel is not None and cancel.is_set()


def _check_cancel(cancel):
    if _cancelled(cancel):
        raise InterruptedError('导入已取消')


def _notify(progress, text):
    if progress is not None:
        progress(text)


def _digest_file(path, limit=MAX_REPLAY_BYTES, cancel=None):
    digest, size = hashlib.sha256(), 0
    with Path(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
            _check_cancel(cancel)
            size += len(block)
            if size > limit:
                raise ValueError('文件超过大小限制')
            digest.update(block)
    return digest.hexdigest()


def _safe_member(name):
    if not isinstance(name, str) or not name or len(name) > 1024 or any(ord(c) < 32 for c in name):
        raise ValueError('压缩包成员名称无效')
    normalized = name.replace('\\', '/')
    path = PurePosixPath(normalized)
    if path.is_absolute() or ':' in normalized or any(p in ('', '.', '..') for p in normalized.split('/')):
        raise ValueError('压缩包包含不安全的回放路径')
    return path


def _output_name(name):
    value = re.sub(r'[<>:"/\\|?*]', '_', name).rstrip(' .')
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{prefix}{i}' for prefix in ('COM', 'LPT') for i in range(1, 10)}
    if value.split('.')[0].upper() in reserved:
        value = '_' + value
    if not value or len(value) > 180:
        raise ValueError('回放文件名过长或无效')
    return value


def _link(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _decode_tar(data):
    try:
        return data.decode('utf-8', 'strict'), 'utf-8'
    except UnicodeDecodeError as exc:
        if os.name != 'nt':
            raise ValueError('RAR 成员清单不是可验证的 UTF-8') from exc
        encoding = 'cp' + str(ctypes.windll.kernel32.GetACP())
        text = data.decode(encoding, 'strict')
        if text.encode(encoding, 'strict') != data:
            raise ValueError('RAR 成员清单编码不能往返验证')
        return text, encoding


def _tar_listing(archive, verbose, cancel):
    executable = shutil.which('tar')
    if not executable:
        raise ValueError('本机缺少 tar，无法读取 RAR；可先手动解压回放')
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([executable, '-tvf' if verbose else '-tf', str(archive)], stdout=output, stderr=errors,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        start = time.monotonic()
        try:
            while process.poll() is None:
                _check_cancel(cancel)
                if time.monotonic() - start > EXTRACT_TIMEOUT_SECONDS:
                    raise TimeoutError('读取 RAR 清单超时')
                if os.fstat(output.fileno()).st_size > MAX_TAR_LIST_BYTES or os.fstat(errors.fileno()).st_size > MAX_TAR_LIST_BYTES:
                    raise ValueError('RAR 清单超过限制')
                time.sleep(.04)
            if process.returncode:
                raise ValueError('RAR 清单读取失败，压缩包可能损坏或加密')
            output.seek(0)
            raw = output.read(MAX_TAR_LIST_BYTES + 1)
            if len(raw) > MAX_TAR_LIST_BYTES:
                raise ValueError('RAR 清单超过限制')
            return _decode_tar(raw)
        finally:
            _stop(process)


class _Expansion:
    def __init__(self, root, cancel, progress):
        self.root, self.cancel, self.progress = root.resolve(), cancel, progress
        self.root.mkdir(parents=True, exist_ok=True)
        self.paths, self.origins, self.seen_paths = [], [], set()
        self.bytes, self.entries = 0, 0
        self.expanded_count = 0

    def reserve(self, size):
        if self.expanded_count >= MAX_REPLAYS:
            raise ValueError(f'一次最多展开 {MAX_REPLAYS} 份回放')
        if size < 0 or size > MAX_REPLAY_BYTES or self.bytes + size > MAX_EXPANDED_BYTES:
            raise ValueError('回放单文件或总展开大小超过限制')

    def add(self, path, origin):
        size = path.stat().st_size
        self.reserve(size)
        self.bytes += size
        self.expanded_count += 1
        self.paths.append(path.resolve())
        self.origins.append(dict(origin, extracted_path=str(path.resolve()), bytes=size))

    def target(self, folder, index, member):
        name = _output_name(member.name)
        target = (folder / f'{index:04d}' / name).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError('暂存路径越界')
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def stream(self, source, target, declared_size=None):
        self.reserve(declared_size or 0)
        size = 0
        try:
            with target.open('xb') as output:
                while block := source.read(64 * 1024):
                    _check_cancel(self.cancel)
                    size += len(block)
                    if size > MAX_REPLAY_BYTES or self.bytes + size > MAX_EXPANDED_BYTES:
                        raise ValueError('回放单文件或总展开大小超过限制')
                    output.write(block)
            if not size or (declared_size is not None and size != declared_size):
                raise ValueError('解压数据为空或长度不符')
            return size
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def zip(self, path, folder, archive_sha):
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise ValueError('ZIP 成员数超过限制')
            seen = set()
            for info in infos:
                _check_cancel(self.cancel)
                if info.is_dir() or not info.filename.lower().endswith('.wowsreplay'):
                    continue
                member = _safe_member(info.filename)
                kind = stat.S_IFMT(info.external_attr >> 16)
                if kind not in (0, stat.S_IFREG) or info.flag_bits & 1:
                    raise ValueError('ZIP 回放不能是链接、特殊文件或加密成员')
                key = str(member).casefold()
                if key in seen:
                    raise ValueError('ZIP 包含重复回放路径')
                seen.add(key)
                self.reserve(info.file_size)
                target = self.target(folder, len(seen), member)
                _notify(self.progress, f'提取 ZIP 回放：{member.name}')
                with archive.open(info, 'r') as source:
                    self.stream(source, target, info.file_size)
                self.add(target, {'archive': str(path), 'archive_sha256': archive_sha, 'member': info.filename, 'kind': 'zip'})

    def rar(self, path, folder, archive_sha):
        listing, encoding = _tar_listing(path, False, self.cancel)
        verbose, _ = _tar_listing(path, True, self.cancel)
        members = [line for line in listing.splitlines() if line]
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError('RAR 成员数超过限制')
        seen = set()
        for name in members:
            _check_cancel(self.cancel)
            if not name.lower().endswith('.wowsreplay'):
                continue
            member = _safe_member(name)
            if any(c in name for c in '*?[]'):
                raise ValueError('RAR 回放路径不能含通配符')
            key = str(member).casefold()
            matches = [line for line in verbose.splitlines() if line.endswith(' ' + name)]
            if key in seen or len(matches) != 1 or not matches[0].startswith('-'):
                raise ValueError('RAR 回放必须是无重名的普通文件')
            seen.add(key)
            target = self.target(folder, len(seen), member)
            executable = shutil.which('tar')
            _notify(self.progress, f'提取 RAR 回放：{member.name}')
            with tempfile.TemporaryFile() as errors:
                process = subprocess.Popen([executable, '-xOf', str(path), '--', name], stdout=subprocess.PIPE, stderr=errors,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                finished = threading.Event()
                reason = []
                start = time.monotonic()
                def watch():
                    while not finished.wait(.04):
                        if _cancelled(self.cancel) or time.monotonic() - start > EXTRACT_TIMEOUT_SECONDS:
                            reason.append('cancelled' if _cancelled(self.cancel) else 'timeout')
                            _stop(process)
                            return
                        if os.fstat(errors.fileno()).st_size > MAX_TAR_LIST_BYTES:
                            reason.append('stderr_limit')
                            _stop(process)
                            return
                watcher = threading.Thread(target=watch, daemon=True)
                watcher.start()
                try:
                    self.stream(process.stdout, target)
                    process.wait(timeout=5)
                    _check_cancel(self.cancel)
                    if reason or process.returncode:
                        raise ValueError('RAR 提取失败或超时')
                except BaseException:
                    target.unlink(missing_ok=True)
                    _check_cancel(self.cancel)
                    raise
                finally:
                    finished.set()
                    _stop(process)
                    process.stdout.close()
                    watcher.join(timeout=5)
            self.add(target, {'archive': str(path), 'archive_sha256': archive_sha, 'member': name, 'listing_encoding': encoding, 'kind': 'rar'})

    def visit(self, value):
        _check_cancel(self.cancel)
        path = Path(value).expanduser().absolute()
        if _link(path):
            raise ValueError('不通过链接或目录联接展开输入')
        path = path.resolve()
        identity = os.path.normcase(str(path))
        if identity in self.seen_paths:
            return
        self.seen_paths.add(identity)
        if path.is_dir():
            for current, directories, filenames in os.walk(path, followlinks=False):
                _check_cancel(self.cancel)
                directories[:] = sorted(d for d in directories if not _link(Path(current) / d)
                                         and not (Path(current) / d).resolve().is_relative_to(self.root))
                self.entries += len(directories) + len(filenames)
                if self.entries > MAX_DIRECTORY_ENTRIES:
                    raise ValueError('文件夹项目数超过限制')
                for name in sorted(filenames):
                    file = Path(current) / name
                    if file.suffix.lower() in ('.wowsreplay', '.zip', '.rar') and not _link(file):
                        self.visit(file)
            return
        if not path.is_file():
            raise ValueError('输入文件不存在')
        suffix = path.suffix.lower()
        if suffix == '.wowsreplay':
            self.add(path, {'path': str(path), 'kind': 'replay'})
            return
        if suffix not in ('.zip', '.rar'):
            raise ValueError('支持回放、文件夹、ZIP、RAR；比赛包请使用包导入')
        archive_sha = _digest_file(path, MAX_ARCHIVE_BYTES, self.cancel)
        folder = Path(tempfile.mkdtemp(prefix='archive_', dir=self.root))
        (self.zip if suffix == '.zip' else self.rar)(path, folder, archive_sha)
        if _digest_file(path, MAX_ARCHIVE_BYTES, self.cancel) != archive_sha:
            raise ValueError('压缩包在提取期间发生变化')


def expand_inputs(paths, staging_dir, cancel=None, progress=None):
    """Return only replay paths; nested archives and non-replay members are ignored."""
    expansion = _Expansion(Path(staging_dir), cancel, progress)
    for index, value in enumerate(paths):
        if index >= MAX_INPUT_ROOTS:
            raise ValueError('输入路径数超过限制')
        expansion.visit(value)
    _check_cancel(cancel)
    manifest = {'schema_version': 1, 'files': expansion.origins, 'expanded_bytes': expansion.bytes,
                'limits': {'replays': MAX_REPLAYS, 'single_replay_bytes': MAX_REPLAY_BYTES, 'total_bytes': MAX_EXPANDED_BYTES}}
    (expansion.root / 'expansion_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return expansion.paths


def _check_header(path):
    if path.stat().st_size > MAX_REPLAY_BYTES:
        raise ValueError('回放超过 64 MiB 限制')
    with path.open('rb') as stream:
        prefix = stream.read(12)
        if len(prefix) != 12 or prefix[:4] != bytes.fromhex('12323411'):
            raise ValueError('不是有效回放容器')
        count, size = struct.unpack('<II', prefix[4:])
        if not 1 <= count <= 64 or not 0 < size <= 1024 * 1024:
            raise ValueError('回放文件头超过限制')
        raw = stream.read(size)
        if len(raw) != size:
            raise ValueError('回放文件头被截断')
        header = json.loads(raw.decode('utf-8'))
    if not isinstance(header, dict) or not isinstance(header.get('clientVersionFromExe'), str):
        raise ValueError('缺少完整客户端版本')
    version = parse_version(header['clientVersionFromExe'])
    if version != SUPPORTED_VERSION:
        raise ValueError(f"版本未验证：{header['clientVersionFromExe']}；仅支持 15.9.0.13357625")
    return header


class MultiviewImportService:
    def __init__(self, data_root, bundle_root=None):
        from .multiview_store import BattleRecordingStore
        from .runtime_bundle import default_bundle_root
        self.library = Library(Path(data_root))
        self.store = BattleRecordingStore(self.library.root)
        self.bundle_root = Path(bundle_root) if bundle_root is not None else default_bundle_root()
        self.jobs = self.library.root / 'multiview' / 'import-jobs'
        self.jobs.mkdir(parents=True, exist_ok=True)

    def _parse(self, path, work, cancel, progress):
        output, log = work / (uuid.uuid4().hex + '.json'), work / (uuid.uuid4().hex + '.log')
        command = [sys.executable, '--parse-worker'] if getattr(sys, 'frozen', False) else [sys.executable, '-B', '-m', 'wows_training.app', '--parse-worker']
        command += ['--input', str(path), '--output', str(output), '--bundle', str(self.bundle_root)]
        environment = dict(os.environ, PYTHONIOENCODING='utf-8')
        if not getattr(sys, 'frozen', False):
            environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]) + os.pathsep + environment.get('PYTHONPATH', '')
        _notify(progress, f'解析回放：{path.name}')
        with log.open('wb') as stream:
            process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, env=environment,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            start = time.monotonic()
            try:
                while process.poll() is None:
                    _check_cancel(cancel)
                    if time.monotonic() - start > PARSE_TIMEOUT_SECONDS:
                        raise TimeoutError('当前回放解析超过 120 秒，其他来源可继续')
                    if os.fstat(stream.fileno()).st_size > MAX_TAR_LIST_BYTES:
                        raise ValueError('解析日志超过限制')
                    time.sleep(.05)
                _check_cancel(cancel)
                if not output.is_file() or output.stat().st_size > 128 * 1024 * 1024:
                    raise ValueError('解析进程未生成有界报告')
                report = json.loads(output.read_text(encoding='utf-8'))
                if process.returncode or report.get('status') == 'failed':
                    raise ValueError(report.get('error') or '解析失败')
                return report
            finally:
                _stop(process)

    def _ingest_replay(self, path, work, cancel, progress):
        path = path.resolve()
        _check_cancel(cancel)
        _check_header(path)
        fingerprint = _digest_file(path, cancel=cancel)
        existing = self.library.find_replay(fingerprint)
        if existing:
            report = self.library.get_report(fingerprint)
            report_path = self.library.root / existing['report_relative_path']
            revision, expected = existing['analysis_revision'], existing['report_sha256']
        else:
            report = self._parse(path, work, cancel, progress)
            if report.get('sha256') != fingerprint or _digest_file(path, cancel=cancel) != fingerprint:
                raise ValueError('回放在解析期间变化或指纹不匹配')
            report_path = work / (fingerprint + '.canonical.json')
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
            revision, expected = 1, _digest_file(report_path, 128 * 1024 * 1024, cancel)
        _check_cancel(cancel)
        retained = self.store.retain_source({'sha256': fingerprint, 'source_path': path, 'report': report_path,
                                             'analysis_revision': revision, 'report_sha256': expected})
        _check_cancel(cancel)
        record = self.library.ingest(report, Path(retained['raw_path']))
        self.library.ingest(report, path)
        return {'sha256': fingerprint, 'battle_id': record['id'], 'status': 'reused' if existing else 'imported',
                'path': str(path), 'retained_raw_path': retained['raw_path'], 'message': '已保存原始来源' if not existing else '复用已核验分析并保管来源'}

    def _sources_for_battle(self, battle_id, cancel):
        reports, sources, warnings = [], [], []
        for row in self.library.list_replays(battle_id):
            _check_cancel(cancel)
            report = self.library.get_report(row['sha256'])
            candidates = [Path(value['path']) for value in row.get('sources', [])]
            raw = None
            for candidate in candidates:
                if candidate.suffix.lower() == '.wowsreplay' and candidate.is_file():
                    try:
                        if _digest_file(candidate, cancel=cancel) == row['sha256']:
                            raw = candidate
                            break
                    except (OSError, ValueError):
                        continue
            if raw is None:
                warnings.append({'code': 'existing_raw_unavailable', 'source_sha': row['sha256'],
                                 'message': '原回放路径不可用；保留原分析，但本次不能纳入可独立保存的比赛记录'})
                continue
            source = self.store.retain_source({'sha256': row['sha256'], 'source_path': raw,
                'report': self.library.root / row['report_relative_path'], 'analysis_revision': row['analysis_revision'],
                'report_sha256': row['report_sha256']})
            self.library.ingest(report, Path(source['raw_path']))
            source['report'] = self.library.root / row['report_relative_path']
            reports.append(report)
            sources.append(source)
        return reports, sources, warnings

    def _merge_battle(self, battle_id, cancel, progress):
        from .multiview import analyze_pairing, merge_reports
        reports, sources, warnings = self._sources_for_battle(battle_id, cancel)
        _check_cancel(cancel)
        pairing = analyze_pairing(reports)
        if not pairing.get('can_merge'):
            status = pairing.get('status', 'incompatible')
            return {'battle_id': battle_id, 'status': status, 'source_count': len(sources), 'recording_id': None,
                    'message': '来源已保管，等待补充另一侧有效录制' if status == 'pending_counterpart' else '来源保留，当前无法合并',
                    'pairing': pairing, 'warnings': warnings}
        _notify(progress, f'合并同场录制：{battle_id}（{len(sources)} 份来源）')
        merged = merge_reports(reports)
        _check_cancel(cancel)
        record = self.store.save(merged, sources)
        return {'battle_id': battle_id, 'status': 'merged', 'source_count': record['source_count'], 'recording_id': record['id'],
                'message': '同场来源已对齐并保存，原始录制分别保留', 'warnings': warnings, 'record': record}

    def import_inputs(self, paths, cancel=None, progress=None):
        result = {'items': [], 'source_results': [], 'counts': {}, 'cancelled': False}
        work = Path(tempfile.mkdtemp(prefix='import_', dir=self.jobs))
        replay_paths, package_paths = [], []
        try:
            expansion = _Expansion(work / 'expanded', cancel, progress)
            for index, value in enumerate(paths):
                _check_cancel(cancel)
                if index >= MAX_INPUT_ROOTS:
                    raise ValueError('输入路径数超过限制')
                path = Path(value).expanduser().absolute()
                if path.suffix.lower() == '.wowsbattle' and path.is_file():
                    package_paths.append(path)
                    continue
                try:
                    before = len(expansion.paths)
                    expansion.visit(path)
                except InterruptedError:
                    raise
                except Exception as exc:
                    # Failed archives do not contribute a partially validated input;
                    # keep their consumed quota so a series of failures cannot bypass limits.
                    del expansion.paths[before:]
                    del expansion.origins[before:]
                    result['items'].append({'battle_id': None, 'status': 'failed', 'source_count': 0, 'message': str(exc), 'input': str(path)})
            replay_paths = expansion.paths
            (work / 'expansion_manifest.json').write_text(json.dumps({'files': expansion.origins,
                'expanded_bytes': expansion.bytes, 'expanded_count_including_failed_archives': expansion.expanded_count},
                ensure_ascii=False, indent=2), encoding='utf-8')
            if len(replay_paths) > MAX_REPLAYS or sum(p.stat().st_size for p in replay_paths) > MAX_EXPANDED_BYTES:
                raise ValueError('本批回放数或总大小超过限制')
            touched = set()
            seen = set()
            for path in replay_paths:
                identity = os.path.normcase(str(path.resolve()))
                if identity in seen:
                    continue
                seen.add(identity)
                try:
                    source = self._ingest_replay(path, work, cancel, progress)
                    touched.add(source['battle_id'])
                    result['source_results'].append(source)
                except InterruptedError:
                    raise
                except Exception as exc:
                    result['source_results'].append({'path': str(path), 'status': 'failed', 'message': str(exc)})
                    result['items'].append({'battle_id': None, 'status': 'failed', 'source_count': 0, 'message': str(exc), 'input': str(path)})
            for battle_id in sorted(touched):
                try:
                    result['items'].append(self._merge_battle(battle_id, cancel, progress))
                except InterruptedError:
                    raise
                except Exception as exc:
                    result['items'].append({'battle_id': battle_id, 'status': 'failed', 'source_count': len(self.library.list_replays(battle_id)),
                                             'message': str(exc), 'pairing': getattr(exc, 'diagnostics', None)})
            for path in package_paths:
                try:
                    imported = self.import_package(path, cancel, progress)
                    result['items'].extend(imported['items'])
                    result['source_results'].extend(imported['source_results'])
                    if imported['cancelled']:
                        raise InterruptedError('导入已取消')
                except InterruptedError:
                    raise
                except Exception as exc:
                    result['items'].append({'battle_id': None, 'status': 'failed', 'source_count': 0, 'message': str(exc), 'input': str(path)})
        except InterruptedError:
            result['cancelled'] = True
            result['items'].append({'battle_id': None, 'status': 'cancelled', 'source_count': 0, 'message': '已取消；已完成的原始来源保留，未完成的合并结果未发布'})
        except Exception as exc:
            result['items'].append({'battle_id': None, 'status': 'failed', 'source_count': 0, 'message': str(exc)})
        result['counts'] = dict(Counter(item['status'] for item in result['items']))
        result['counts'].update(source_imported=sum(s['status'] == 'imported' for s in result['source_results']),
                                source_reused=sum(s['status'] == 'reused' for s in result['source_results']),
                                source_failed=sum(s['status'] == 'failed' for s in result['source_results']))
        result['cancelled'] = result['cancelled'] or _cancelled(cancel)
        result['job_directory'] = str(work)
        (work / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return result

    def import_package(self, path, cancel=None, progress=None):
        from .multiview_store import BattleRecordingStore
        _check_cancel(cancel)
        _notify(progress, '核对比赛包完整性；包内原回放将重新解析或复用已有可信分析')
        with tempfile.TemporaryDirectory(prefix='package_verify_', dir=self.jobs) as temporary:
            isolated = BattleRecordingStore(temporary)
            packaged = isolated.import_package(Path(path))
            _check_cancel(cancel)
            named = []
            for index, source in enumerate(packaged['sources']):
                member = _safe_member(source['source_name'])
                if len(member.parts) != 1 or member.suffix.lower() != '.wowsreplay':
                    raise ValueError('比赛包来源文件名无效')
                alias = Path(temporary) / 'named_sources' / str(index) / _output_name(member.name)
                alias.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source['raw_path'], alias)
                named.append(alias)
            result = self.import_inputs(named, cancel, progress)
            result['package_verification'] = {'input': str(Path(path).resolve()), 'container_integrity_checked': True,
                                              'packaged_merged_report_published': False,
                                              'policy': '原始回放重新解析或复用正式库同 SHA 报告，再生成合并结果'}
            return result
