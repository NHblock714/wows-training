"""Native ribbon icons for result fields backed by the matching client build."""
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import sys


_MANIFEST_SHA256 = '68b6cdca7e99088366c211520dd470264f2d72b6c67717c5cdee5758b57f9664'


def _asset_root():
    root = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2]
    return root / 'naval_assets' / 'ribbons'


@lru_cache(maxsize=2)
def _manifest(root):
    try:
        raw = (root / 'manifest.json').read_bytes()
        if sha256(raw).hexdigest() == _MANIFEST_SHA256:
            return json.loads(raw)
    except OSError:
        pass
    return {'builds': {}, 'files': {}}


@lru_cache(maxsize=256)
def _verified_icon(path, fingerprint):
    try:
        raw = path.read_bytes()
        if sha256(raw).hexdigest() == fingerprint:
            return path
    except OSError:
        pass
    return None


def ribbon_icon_path(replay_version: str, ribbon_key: str) -> Path | None:
    """Resolve a verified PNG, or allow the caller to display the count alone."""
    if not isinstance(replay_version, str) or not isinstance(ribbon_key, str):
        return None
    root = _asset_root()
    manifest = _manifest(root)
    filenames = manifest['builds'].get(replay_version, {}).get('icons', {})
    filename = filenames.get(ribbon_key)
    if filename is None:
        return None
    return _verified_icon(root / filename, manifest['files'][filename]['sha256'])
