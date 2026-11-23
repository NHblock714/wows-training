# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import json
import os
import importlib.util
import tomllib
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, copy_metadata

root = Path(SPECPATH)
version = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
def needed_module(name):
    if name.startswith(('replay_unpack.clients.wot', 'replay_unpack.clients.wowp')):
        return False
    prefix = 'replay_unpack.clients.wows.versions.'
    return not name.startswith(prefix) or name.startswith(prefix + '15_3_0')

hidden = collect_submodules('replay_unpack', filter=needed_module) + ['Cryptodome.Cipher.Blowfish', 'Cryptodome.Util.Padding']
bundle_files = json.loads((root / 'runtime_bundle/bundle.json').read_text(encoding='utf-8'))['files']
datas = [(str(root / 'runtime_bundle' / relative), str(Path('runtime_bundle') / Path(relative).parent))
         for relative in ['bundle.json', *bundle_files]]
datas += collect_data_files('replay_unpack', includes=['clients/wows/versions/15_3_0/**/*'])
datas += copy_metadata('World-of-Warships-replays-parser')
datas += [(str(root / 'skills' / name), 'skills/' + name)
          for name in ('wows-tactical-brief', 'wows-career-profile')]
datas += [(str(root / 'naval_assets'), 'naval_assets'), (str(root / 'career_data'), 'career_data')]
datas += [(str(root / 'presentation_fonts'), 'presentation_fonts'), (str(root / 'native_ui'), 'native_ui')]
datas += [(str(root / name), name) for name in ("ordinary_profiles", "decision_models")]
a = Analysis(
    [str(root / 'desktop_launcher.py')],
    pathex=[str(root / 'src')], binaries=[], datas=datas,
    hiddenimports=hidden,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets'],
    noarchive=False,
)
# Qt uses the Windows ICU ABI. A Poppler ICU found on the build host PATH has
# version-suffixed exports and must not shadow the operating system library.
a.binaries = [item for item in a.binaries
              if Path(item[0]).name.lower() not in {'icuuc.dll', 'icudt78.dll'}]
# Keep the interpreter and Qt on the same bundled VC runtime revision.
qt_root = Path(importlib.util.find_spec('PySide6').origin).parent
a.binaries = [(name, str(qt_root / Path(name).name.lower()), kind)
              if name.lower() in {'vcruntime140.dll', 'vcruntime140_1.dll'} else (name, source, kind)
              for name, source, kind in a.binaries]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='WOWSTraining', icon=str(root/'packaging/fleet.ico'),
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=os.environ.get('WOWS_DEBUG_CONSOLE') == '1', disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='WOWSTraining-' + version)
