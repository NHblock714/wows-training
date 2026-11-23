"""Atomic desktop appearance preferences, seeded from existing local switches."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


TEXT_SIZES = {'compact': 12, 'standard': 13, 'large': 14}
DEFAULTS = {'motion_enabled': True, 'sound_enabled': False,
            'background_enabled': True, 'text_size': 'standard',
            'check_updates': True, 'download_updates': True}
_APPEARANCE_FIELDS = {'motion_enabled', 'sound_enabled', 'background_enabled', 'text_size'}


def validate_preferences(value):
    if not isinstance(value, dict) or not _APPEARANCE_FIELDS <= value.keys() <= DEFAULTS.keys():
        raise ValueError('客户端设置格式无效')
    value = {**DEFAULTS, **value}
    if any(type(flag) is not bool for key, flag in value.items() if key != 'text_size'):
        raise ValueError('客户端开关格式无效')
    if not isinstance(value['text_size'], str) or value['text_size'] not in TEXT_SIZES:
        raise ValueError('文字大小无效')
    return dict(value)


class ClientPreferences:
    def __init__(self, root):
        self.path = Path(root) / 'client_ui.json'

    def load(self, defaults=None):
        fallback = validate_preferences(DEFAULTS if defaults is None else defaults)
        try:
            if self.path.stat().st_size > 4096:
                return fallback
            value = json.loads(self.path.read_text(encoding='utf8'))
            if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
                return fallback
            return validate_preferences(value['settings'])
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return fallback

    def save(self, settings):
        settings = validate_preferences(settings)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.client-ui-', suffix='.json', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf8') as stream:
                json.dump({'version': 1, 'settings': settings}, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return settings
