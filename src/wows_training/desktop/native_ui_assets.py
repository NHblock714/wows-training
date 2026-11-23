"""Verified client UI textures with safe Qt search paths and CSS fallbacks."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path,PurePosixPath
import re
import sys

from PySide6.QtCore import QByteArray,QBuffer,QDir,QIODevice
from PySide6.QtGui import QImageReader

MAX_BYTES=8*1024*1024
CLIENT_VERSION='15.9.0.13357625'
REQUIRED_STATES={role:('normal','hover','pressed','selected','disabled') for role in ('button','primary','secondary','nav','tab')}
REQUIRED_STATES.update(header=('normal',),progress=('normal','fill'),slider=('normal','thumb','fill'))
REQUIRED_STATES.update({'replay_'+role:states for role,states in tuple(REQUIRED_STATES.items()) if role!='header'})
for role in ('button','primary','secondary','nav'):
    REQUIRED_STATES[role] += ('keyboard_normal','keyboard_selected')
REQUIRED_STATES['check_mark'] = ('normal',)


def default_ui_root():
    return (Path(sys._MEIPASS) if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[3])/'native_ui'


class NativeUiAssets:
    def __init__(self,root=None):
        self.root=Path(root or default_ui_root()).resolve();self.manifest={};self.errors=[];self._images={};self._paths={}
        self.prefix='wowsui'+hashlib.sha256(str(self.root).encode('utf8')).hexdigest()[:16]
        manifest=self.root/'manifest.json'
        if not manifest.is_file():return
        try:
            if manifest.stat().st_size>256*1024:raise ValueError('Native UI manifest exceeds limit')
            data=json.loads(manifest.read_text(encoding='utf8'))
            if (not isinstance(data,dict) or type(data.get('format_version')) is not int or data['format_version']!=1
                    or data.get('client_version')!=CLIENT_VERSION or data.get('theme')!='academy-1'
                    or not isinstance(data.get('files'),dict) or not 1<=len(data['files'])<=128
                    or not isinstance(data.get('roles'),dict)):
                raise ValueError('Unsupported native UI manifest')
            for role,states in REQUIRED_STATES.items():
                if not isinstance(data['roles'].get(role),dict) or any(state not in data['roles'][role] for state in states):
                    raise ValueError('Incomplete native UI role: '+role)
            total=0;pixels=0
            for relative,expected in data['files'].items():
                path=self._resolve(relative)
                if not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected):raise ValueError('Invalid native UI checksum')
                if not path.is_file() or path.stat().st_size>MAX_BYTES:raise ValueError('Native UI resource is missing or too large')
                payload=path.read_bytes();total+=len(payload)
                if total>MAX_BYTES:raise ValueError('Native UI bundle exceeds 8 MiB')
                if hashlib.sha256(payload).hexdigest()!=expected:raise ValueError('Native UI checksum mismatch: '+relative)
                self._paths[relative]=path
                if path.suffix.lower()=='.png':
                    buffer=QBuffer();buffer.setData(QByteArray(payload));buffer.open(QIODevice.OpenModeFlag.ReadOnly)
                    reader=QImageReader(buffer);size=reader.size();pixels+=size.width()*size.height()
                    if min(size.width(),size.height())<=0 or max(size.width(),size.height())>8192 or size.width()*size.height()>16_000_000 or pixels>32_000_000:
                        buffer.close();raise ValueError('Native UI image exceeds decoded size limit')
                    image=reader.read();buffer.close()
                    if image.isNull():
                        raise ValueError('Invalid native UI image')
                    self._images[relative]=image
            for role,states in data['roles'].items():
                if not isinstance(role,str) or not isinstance(states,dict):raise ValueError('Invalid native UI role')
                for state,record in states.items():
                    if not isinstance(state,str) or not isinstance(record,dict):raise ValueError('Invalid native UI state')
                    image=self._images.get(record.get('file'));margins=record.get('margins')
                    if image is None or not isinstance(margins,list) or len(margins)!=4 or any(type(v) is not int or v<0 for v in margins):
                        raise ValueError('Invalid native UI slice')
                    top,right,bottom,left=margins
                    if top+bottom>=image.height() or left+right>=image.width():raise ValueError('Native UI slice has no center')
                    ink=record.get('text_color')
                    if ink is not None and (not isinstance(ink,str) or not re.fullmatch('#[0-9a-fA-F]{6}',ink)):raise ValueError('Invalid native UI text color')
            self.manifest=data
            # Qt's stylesheet image loader does not decode file:// percent escapes.
            # A search path keeps spaces, non-ASCII and '#' outside the CSS URL.
            QDir.setSearchPaths(self.prefix,[str(self.root)])
        except (OSError,ValueError,TypeError,KeyError,RecursionError) as error:
            self.manifest={};self._images={};self._paths={};self.errors=[str(error)]

    def _resolve(self,relative):
        if not isinstance(relative,str) or '\\' in relative:raise ValueError('Invalid native UI path')
        key=PurePosixPath(relative)
        if key.is_absolute() or '..' in key.parts or not key.parts or any(':' in part for part in key.parts):raise ValueError('Unsafe native UI path')
        path=(self.root/Path(*key.parts)).resolve()
        if not path.is_relative_to(self.root):raise ValueError('Native UI path escapes bundle')
        return path

    @property
    def available(self):return bool(self.manifest)

    def state(self,role,state='normal'):
        return deepcopy(self.manifest.get('roles',{}).get(role,{}).get(state))

    def image(self,role,state='normal'):
        record=self.state(role,state)
        return self._images.get(record['file']) if record else None

    def uri(self,role,state='normal'):
        record=self.state(role,state)
        return self.prefix+':'+record['file'] if record else None

    def border_image(self,role,state='normal',widths=None):
        record=self.state(role,state)
        if not record:return ''
        margins=record['margins'];widths=margins if widths is None else widths
        return ('background: transparent; border-style: solid; border-color: transparent; border-radius: 0px; '
                'border-width: '+' '.join(str(v)+'px' for v in widths)+'; '
                'border-image: url("'+self.uri(role,state)+'") '+' '.join(map(str,margins))+' stretch stretch;')

    def replay_stylesheet(self):
        """The original native-state palette inside the tactical replay panel."""
        if not self.available:return ''
        rules=[]
        def add(selector,role,state='normal',widths=None,extra=''):
            role='replay_'+role
            record=self.state(role,state)
            if record:
                ink=' color:'+record['text_color']+';' if record.get('text_color') else ''
                rules.append(selector+' { '+self.border_image(role,state,widths)+ink+extra+' }')
        for selector,role in (('QPushButton','button'),('QPushButton#primary','primary'),('QPushButton#radarSave','primary')):
            for pseudo,state in (('','normal'),(':hover','hover'),(':pressed','pressed'),(':checked','selected'),(':disabled','disabled')):
                ink=' color:#A1AAA6;' if state=='disabled' else ' color:#F4F5EB;'
                add(selector+pseudo,role,state,[4,4,4,4],' padding:5px 10px;'+ink)
        for selector in ('QPushButton#soundToggle','QPushButton#radarSaveCopy','QPushButton#radarDiscard','QPushButton#radarQuerySettings'):
            for pseudo,state in (('','normal'),(':hover','hover'),(':pressed','pressed'),(':checked','selected'),(':disabled','disabled')):
                add(selector+pseudo,'secondary',state,[10,10,10,10],' padding:0px 3px; color:'+('#70807C' if state=='disabled' else '#E1E8E4')+';')
        for pseudo,state in (('','normal'),(':hover','hover'),(':pressed','pressed'),(':checked','selected'),(':disabled','disabled')):
            ink=' color:#E6EBE8;' if state not in ('selected','disabled') else ' color:#111111;' if state=='selected' else ' color:#6D7C7E;'
            add('QPushButton#nav'+pseudo,'nav',state,[0,9,0,12],' padding:8px 2px; text-align:left;'+ink)
        for pseudo,state in (('','normal'),(':hover','hover'),(':pressed','pressed'),(':selected','selected'),(':disabled','disabled')):
            ink=' color:#E1E7E5;' if state not in ('selected','disabled') else ' color:#111111;' if state=='selected' else ' color:#718080;'
            add('QTabBar::tab'+pseudo,'tab',state,[2,2,2,2],' padding:7px 14px; margin-right:1px;'+ink)
        add('QTabWidget::pane','tab','normal',[2,2,2,2],' padding-top:4px;')
        add('QProgressBar','progress','normal',[2,2,2,2],' text-align:center; color:#E9EEE5;')
        add('QProgressBar::chunk','progress','fill',[0,0,0,0])
        add('QSlider::groove:horizontal','slider','normal',[0,4,0,4],' height:8px;')
        add('QSlider::sub-page:horizontal','slider','fill',[0,0,0,0])
        add('QSlider::handle:horizontal','slider','thumb',[0,0,0,0],' width:10px; margin:-7px 0;')
        # Combat health is a semantic green gauge, not the generic gold loading bar.
        rules.append('QProgressBar#selectedShipHealthBar { border-image:none; border:0; background:#10181b; }')
        rules.append('QProgressBar#selectedShipHealthBar::chunk { border-image:none; background:#73cfa4; }')
        rules.append('QPushButton#radarMoveUp, QPushButton#radarMoveDown { padding:0px; }')
        return '\n'.join(rules)

    def stylesheet(self):
        if not self.available:
            return ''
        rules = []
        def add(selector, role, state, widths, extra=''):
            record = self.manifest['roles'][role][state]
            color = record.get('text_color')
            ink = f'color:{color};' if color else ''
            rules.append(f'{selector} {{ {self.border_image(role,state,widths)} {ink} {extra} }}')

        feedback = (('', 'normal'), (':hover', 'hover'), (':focus', 'keyboard_normal'),
                    (':checked', 'selected'), (':checked:focus','keyboard_selected'),
                    (':pressed', 'pressed'), (':disabled', 'disabled'))
        for selector, role in (('QPushButton', 'button'), ('QPushButton#primary', 'primary'),
                               ('QPushButton#radarSave', 'primary')):
            for pseudo, state in feedback:
                add(selector+pseudo,role,state,[4]*4,'padding:5px 10px;')
        for name in ('soundToggle','radarSaveCopy','radarDiscard','radarQuerySettings'):
            for pseudo, state in feedback:
                ink = 'color:#D6E9FF;' if name=='soundToggle' else ''
                add('QPushButton#'+name+pseudo,'secondary',state,[10]*4,'padding:0px 3px;'+ink)
        for pseudo, state in feedback:
            add('QPushButton#nav'+pseudo,'nav',state,[0,9,0,12],'padding:7px 2px; text-align:left;')
        for pseudo, state in (('','normal'),(':hover','hover'),(':selected','selected'),
                              (':pressed','pressed'),(':disabled','disabled')):
            add('QTabBar::tab'+pseudo,'tab',state,[2]*4,'padding:7px 14px; margin-right:2px;')
        add('QProgressBar','progress','normal',[2]*4,'background:#243D56; text-align:center;')
        add('QProgressBar::chunk','progress','fill',[0]*4)
        add('QSlider::groove:horizontal','slider','normal',[0,4,0,4],'height:8px; background:#D6E2F0;')
        add('QSlider::sub-page:horizontal','slider','fill',[0]*4)
        add('QSlider::handle:horizontal','slider','thumb',[0]*4,'width:10px; margin:-7px 0;')
        rules.extend((
            'QCheckBox::indicator:checked { image:url("'+self.uri('check_mark')+'"); }',
            'QTabBar::tab:selected { border-image:none; background:#D7E9F8; color:#164677; '
            'border:1px solid #9DBFDB; border-bottom:3px solid #3483C8; font-weight:600; }',
            'QProgressBar { border-image:none; background:#E7EFF7; color:#173B5C; border:1px solid #A9C2D8; }',
            'QProgressBar::chunk { border-image:none; background:#9DC6E9; }',
            'QProgressBar#selectedShipHealthBar { border-image:none; border:0; background:#10181b; }',
            'QProgressBar#selectedShipHealthBar::chunk { border-image:none; background:#73cfa4; }',
            'QPushButton#radarMoveUp, QPushButton#radarMoveDown { padding:0; }',
        ))
        return '\n'.join(rules)


@lru_cache(maxsize=1)
def default_native_ui_assets():return NativeUiAssets()
