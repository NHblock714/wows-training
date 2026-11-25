"""Hash-checked OFL fonts registered only inside the running Qt application."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import weakref

from PySide6.QtGui import QFontDatabase, QGuiApplication


FALLBACK_ROLES = {
    'display': 'Georgia', 'grade': 'Georgia',
    'chinese_display': 'Microsoft YaHei UI', 'chinese_body': 'Microsoft YaHei UI',
    'tech': 'Bahnschrift', 'chinese_tech': 'Microsoft YaHei UI',
}
MAX_BYTES = 32 * 1024 * 1024
_CACHE = weakref.WeakKeyDictionary()
_SANS_CACHE = weakref.WeakKeyDictionary()


def default_font_root():
    base = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[3]
    return base / 'presentation_fonts'


def _path(root, relative):
    if not isinstance(relative, str) or '\\' in relative:
        raise ValueError('Invalid presentation font path')
    key = PurePosixPath(relative)
    if key.is_absolute() or '..' in key.parts or any(':' in p for p in key.parts):
        raise ValueError('Unsafe presentation font path')
    path = (root / Path(*key.parts)).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Presentation font path escapes the bundle')
    return path


def _load(root=None):
    base = Path(root if root is not None else default_font_root()).resolve()
    result = {'status': 'pending_application', 'roles': dict(FALLBACK_ROLES), 'fallback': True,
              'loaded': [], 'verified_files': 0, 'source_commit': None, 'errors': [], 'root': str(base)}
    app = QGuiApplication.instance()
    if not isinstance(app, QGuiApplication):
        return result
    cache = _CACHE.setdefault(app, {})
    if str(base) in cache:
        return cache[str(base)]
    manifest_path = base / 'manifest.json'
    if not manifest_path.is_file():
        result.update(status='missing', errors=['Presentation font bundle is absent'])
        return result
    try:
        if manifest_path.stat().st_size > 256 * 1024:
            raise ValueError('Presentation font manifest exceeds limit')
        manifest = json.loads(manifest_path.read_text(encoding='utf8'))
        if (not isinstance(manifest, dict) or type(manifest.get('format_version')) is not int
                or manifest['format_version'] != 1 or not isinstance(manifest.get('files'), dict)
                or not 1 <= len(manifest['files']) <= 64 or not isinstance(manifest.get('roles'), dict)
                or set(manifest['roles']) != set(FALLBACK_ROLES)
                or not isinstance(manifest.get('role_files'), dict)
                or set(manifest['role_files']) != set(FALLBACK_ROLES)
                or not all(isinstance(value, str) and value for value in manifest['roles'].values())):
            raise ValueError('Invalid presentation font manifest')
        commit = manifest.get('source_commit')
        if not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit):
            raise ValueError('Missing pinned presentation font revision')
        paths = {}
        total = 0
        for relative, expected in manifest['files'].items():
            if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
                raise ValueError('Invalid presentation font checksum')
            path = _path(base, relative)
            if not path.is_file() or path.stat().st_size > MAX_BYTES:
                raise ValueError('Presentation font file is missing or too large')
            raw = path.read_bytes()
            total += len(raw)
            if total > MAX_BYTES:
                raise ValueError('Presentation font bundle exceeds 32 MiB')
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError('Presentation font checksum mismatch: ' + relative)
            paths[relative] = path
        fonts = [name for name in paths if name.endswith('.ttf')]
        if not fonts:
            raise ValueError('Presentation font bundle contains no TTF files')
        for files in manifest['role_files'].values():
            if not isinstance(files, list) or not files or any(name not in fonts for name in files):
                raise ValueError('Presentation font role has no verified TTF source')
        result.update(verified_files=len(paths), source_commit=commit)
        for relative in fonts:
            identifier = QFontDatabase.addApplicationFont(str(paths[relative]))
            families = QFontDatabase.applicationFontFamilies(identifier) if identifier >= 0 else []
            if not families:
                result['errors'].append('Qt could not load ' + relative)
                continue
            result['loaded'].append({'file': relative, 'font_id': identifier, 'families': list(families),
                                     'sha256': manifest['files'][relative]})
        result['family_aliases'] = {}
        result['role_sources'] = {}
        for role, desired in manifest['roles'].items():
            candidates = [row for row in result['loaded'] if row['file'] in manifest['role_files'][role]]
            available = {name.casefold(): name for row in candidates for name in row['families']}
            result['role_sources'][role] = [row['file'] for row in candidates]
            if available:
                actual = available.get(desired.casefold(), next(iter(available.values())))
                result['roles'][role] = actual
                if actual != desired:
                    # Qt's offscreen backend may expose a localized family alias.
                    result['family_aliases'][role] = {'expected': desired, 'actual': actual}
            else:
                result['errors'].append('Required font family did not load: ' + desired)
        result['fallback'] = bool(result['errors'])
        result['status'] = 'partial' if result['fallback'] else 'loaded'
        cache[str(base)] = result
    except (OSError, ValueError, TypeError, KeyError) as error:
        result.update(status='invalid', errors=[str(error)])
    return result


def load_presentation_fonts(root=None):
    """Return verified display, grade, body and technology font families after Qt setup."""
    return dict(_load(root)['roles'])


def chinese_sans_families():
    """Prefer the installed Chinese sans faces for long-form body text."""
    app = QGuiApplication.instance()
    if app in _SANS_CACHE:
        return _SANS_CACHE[app]
    preferred = ('SimHei', '黑体', 'Microsoft YaHei UI', 'Microsoft YaHei',
                 'Noto Sans CJK SC', 'Source Han Sans SC', 'DengXian', 'PingFang SC')
    families = [family for family in preferred if QFontDatabase.hasFamily(family)]
    if not families and sys.platform == 'win32':
        system_fonts = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'Fonts'
        for name in ('simhei.ttf', 'msyh.ttc'):
            path = system_fonts / name
            if path.is_file():
                font_id = QFontDatabase.addApplicationFont(str(path))
                families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
                if families:
                    break
    choice = tuple(dict.fromkeys((*families, load_presentation_fonts()['chinese_tech'])))
    if app is not None:
        _SANS_CACHE[app] = choice
    return choice


def font_inventory(root=None):
    """Report verified bundle fonts and actual Qt families for release acceptance."""
    return deepcopy(_load(root))
