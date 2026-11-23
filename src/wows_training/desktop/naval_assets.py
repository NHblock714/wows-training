"""Portable native ship art and quiet optional UI feedback for the operations room."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import sys
import time

from PySide6.QtCore import QByteArray,QBuffer,QIODevice,Qt
from PySide6.QtGui import QColor,QImage,QImageReader,QPainter


PALETTE={'canvas':'#12191C','panel':'#1C272B','line':'#405052','text':'#E1DED0',
         'muted':'#9DAAA9','brass':'#C9A661','positive':'#79A797','alert':'#CC776B'}
NATIONS={'ussr':'Russia','russia':'Russia','uk':'United_Kingdom','united_kingdom':'United_Kingdom',
         'usa':'USA','japan':'Japan','germany':'Germany','france':'France','italy':'Italy','europe':'Europe',
         'pan_asia':'Pan_Asia','pan_america':'Pan_America','commonwealth':'Commonwealth',
         'netherlands':'Netherlands','spain':'Spain','poland':'Poland'}


def default_asset_root():
    return (Path(sys._MEIPASS) if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[3])/'naval_assets'


def halo_wordmark(original):
    """Add a slim translucent edge around the mark's alpha silhouette on dark surfaces."""
    edge = original.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    tint = QPainter(edge)
    tint.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    tint.fillRect(edge.rect(), QColor(195, 239, 255, 140))
    tint.end()
    result = QImage(original.size(), QImage.Format.Format_ARGB32_Premultiplied)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    for x, y in ((-4, 0), (4, 0), (0, -4), (0, 4), (-3, -3), (3, -3), (-3, 3), (3, 3)):
        painter.drawImage(x, y, edge)
    painter.drawImage(0, 0, original)
    painter.end()
    return result


def harbor_lettering(original):
    """Deepen the two blue glyphs of the supplied transparent wordmark at render time."""
    if original is None or (original.width(), original.height()) != (486, 250):
        return original
    result = original.copy()
    for y in range(97, 178):
        # The sloping boundary follows the gap just left of the blue orbital stroke.
        for x in range(18, int(271 - .56*y)):
            pixel = original.pixelColor(x, y)
            if pixel.alpha() and pixel.blue() - pixel.red() > 50 and pixel.blue() > pixel.green():
                pixel.setRed(round(pixel.red()*.66))
                pixel.setGreen(round(pixel.green()*.72))
                pixel.setBlue(round(pixel.blue()*.79))
                result.setPixelColor(x, y, pixel)
    return result


class NavalAssets:
    def __init__(self,root=None):
        self.root=Path(root or default_asset_root()).resolve();self.manifest={};self._bytes={};self._images={}
        path=self.root/'manifest.json'
        if not path.is_file():return
        if path.stat().st_size>4*1024*1024:raise ValueError('Naval asset manifest is too large')
        data=json.loads(path.read_text(encoding='utf8'))
        if (not isinstance(data,dict) or type(data.get('format_version')) is not int or data.get('format_version')!=1
                or data.get('client_version')!='15.9.0.13357625' or not isinstance(data.get('files'),dict)
                or any(not isinstance(data.get(key,{}),dict) for key in ('ships','index_to_ship','flags','backgrounds','icons','sounds'))):
            raise ValueError('Unsupported naval asset manifest')
        self.manifest=data

    @property
    def available(self):return bool(self.manifest)

    def checked_path(self,relative):
        if not isinstance(relative,str):raise ValueError('Invalid naval asset path')
        key=PurePosixPath(relative)
        if key.is_absolute() or '..' in key.parts or any(':' in part for part in key.parts):raise ValueError('Unsafe naval asset path')
        expected=self.manifest.get('files',{}).get(relative)
        if not isinstance(expected,str):return None
        path=(self.root/Path(*key.parts)).resolve()
        if not path.is_relative_to(self.root) or not path.is_file() or path.stat().st_size>8*1024*1024:
            raise ValueError('Naval asset is unavailable or outside the bundle')
        payload=path.read_bytes()
        if hashlib.sha256(payload).hexdigest()!=expected:raise ValueError('Naval asset checksum mismatch: '+relative)
        self._bytes[relative]=payload
        return path

    def image(self,relative):
        if not relative:return None
        if relative not in self._images:
            if self.checked_path(relative) is None:return None
            buffer=QBuffer();buffer.setData(QByteArray(self._bytes[relative]));buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            reader=QImageReader(buffer);size=reader.size()
            if min(size.width(),size.height())<=0 or max(size.width(),size.height())>8192:raise ValueError('Unsupported naval image size')
            image=reader.read();buffer.close()
            if image.isNull():raise ValueError('Unreadable naval image')
            self._images[relative]=image
        return self._images[relative]

    def ship_info(self,ship_id_or_index):
        key=str(ship_id_or_index)
        ships=self.manifest.get('ships',{})
        if key not in ships:key=self.manifest.get('index_to_ship',{}).get(key,key)
        return dict(ships.get(key,{}))

    def ship_image(self,ship_id_or_index,prefer='render'):
        info=self.ship_info(ship_id_or_index)
        order=('silhouette','render') if prefer=='silhouette' else ('render','silhouette')
        return next((image for field in order if (image:=self.image(info.get(field))) is not None),None)

    def flag(self,nation):
        key=NATIONS.get(str(nation).casefold(),str(nation))
        return self.image(self.manifest.get('flags',{}).get(key))

    def background(self,name='fleet'):return self.image(self.manifest.get('backgrounds',{}).get(name))

    def icon(self,name):return self.image(self.manifest.get('icons',{}).get(name))

    def verify_all(self):
        for relative in self.manifest.get('files',{}):self.checked_path(relative)
        return len(self.manifest.get('files',{}))


@lru_cache(maxsize=1)
def default_naval_assets():return NavalAssets()


class NavalSound:
    """Original PCM cues, muted by default; no game soundbank or system-sound fallback."""
    def __init__(self,data_root,assets=None):
        self.assets=assets or default_naval_assets();self.path=Path(data_root)/'naval_sound.json';self.enabled=False;self._last_play=-float('inf')
        try:
            if self.path.is_file() and self.path.stat().st_size<=16384:
                value=json.loads(self.path.read_text(encoding='utf8'))
                self.enabled=(isinstance(value,dict) and type(value.get('schema_version')) is int
                              and value.get('schema_version')==1 and value.get('sound_enabled') is True)
        except (OSError,ValueError,TypeError):pass

    def set_enabled(self,enabled, *, persist=True):
        enabled = enabled is True
        if not persist:
            self.enabled = enabled
            return
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_suffix('.writing')
        temporary.write_text(json.dumps({'schema_version':1,'sound_enabled':enabled}),encoding='utf8');temporary.replace(self.path)
        self.enabled = enabled

    @staticmethod
    def _play_file(path):
        import winsound
        winsound.PlaySound(str(path),winsound.SND_FILENAME|winsound.SND_ASYNC|winsound.SND_NODEFAULT|winsound.SND_NOSTOP)

    def play(self,cue='navigate'):
        if (not self.enabled or sys.platform!='win32' or os.environ.get('QT_QPA_PLATFORM','').casefold()=='offscreen'
                or cue not in ('navigate','confirm')):return False
        now=time.monotonic()
        if now-self._last_play<.15:return False
        try:
            relative=self.assets.manifest.get('sounds',{}).get(cue)
            path=self.assets.checked_path(relative) if relative else None
            if path is None:return False
            self._play_file(path);self._last_play=now;return True
        except (OSError,ValueError,RuntimeError):return False

    def preview(self, cue='confirm'):
        if sys.platform!='win32' or os.environ.get('QT_QPA_PLATFORM','').casefold()=='offscreen':
            return False
        if cue not in ('navigate','confirm'):
            return False
        try:
            relative=self.assets.manifest.get('sounds',{}).get(cue)
            path=self.assets.checked_path(relative) if relative else None
            if path is None:
                return False
            self._play_file(path)
            return True
        except (OSError,ValueError,RuntimeError):
            return False
