"""Hash-checked native 15.9 map artwork and GameParams display metadata."""
from functools import lru_cache
import hashlib
import json
import math
from PySide6.QtCore import QByteArray,QRectF,Qt
from PySide6.QtGui import QColor,QImage,QPainter
from PySide6.QtSvg import QSvgRenderer
from wows_training.runtime_bundle import load_bundle

SPECIES={'DD':'destroyer','CA':'cruiser','BB':'battleship','CV':'aircarrier','SS':'submarine','AUX':'auxiliary'}
RELATION_COLORS={'own':'#FFD700','ally':'#50FF50','enemy':'#FF5050','unknown':'#C9D1DB'}
ABSOLUTE_COLORS={0:'#50FF50',1:'#FF5050'}
AMMO_COLORS={'AP':'#FFFFFF','HE':'#FFD050','CS':'#FF5050','SAP':'#FF5050'}


def screen_rotation(heading):
    return 90-math.degrees(heading) if type(heading) in (float,int) and math.isfinite(heading) else None


class MinimapAssets:
    def __init__(self,bundle=None):
        self.bundle=bundle or load_bundle(verify=False);self.profile={};self.images={};self.raw={}
        key='minimap/profile.json';expected=self.bundle.manifest.get('files',{}).get(key)
        if expected:
            payload=self.bundle.path(key).read_bytes()
            if hashlib.sha256(payload).hexdigest()!=expected:raise ValueError('小地图资源定义校验失败')
            profile=json.loads(payload)
            if (profile.get('schema_version')!=1 or profile.get('client_version')!=self.bundle.version
                    or profile.get('status')!='source_verified'
                    or profile.get('combat_profile_sha256')!=self.bundle.manifest['files'].get('combat_schema.json')):
                raise ValueError('小地图图标与客户端版本不匹配')
            self.profile=profile

    def read(self,relative):
        if relative not in self.raw:
            expected=self.profile.get('files',{}).get(relative);key='minimap/'+relative
            if not expected or self.bundle.manifest['files'].get(key)!=expected:return None
            payload=self.bundle.path(key).read_bytes()
            if hashlib.sha256(payload).hexdigest()!=expected:raise ValueError('小地图原生资源已变化：'+relative)
            self.raw[relative]=payload
        return self.raw[relative]

    def ship(self,kind,variant,color,height):
        suffix=SPECIES.get(kind);height=max(10,round(height))
        if suffix is None:return None
        stem=(f'minimap_self_{"dead" if variant=="self_dead" else "alive"}_{suffix}' if variant in ('self','self_dead')
              else f'minimap_{suffix}'+('_'+variant if variant else ''))
        relative=self.profile.get('ships',{}).get(stem)
        if not relative and variant in ('self','self_dead'):
            relative=self.profile.get('ships',{}).get('minimap_'+suffix+('_dead' if variant=='self_dead' else ''))
        if not relative:return None
        key=(relative,QColor(color).name(),height)
        if key not in self.images:
            payload=self.read(relative)
            if payload is None:return None
            text=payload.decode('utf8')
            if variant not in ('dead','self_dead','invisible','last_visible'):
                text=text.replace('#333333',QColor(color).darker(220).name()).replace('"white"','"'+QColor(color).name()+'"')
            renderer=QSvgRenderer(QByteArray(text.encode('utf8')))
            if not renderer.isValid():return None
            view=renderer.viewBoxF();width=max(1,round(height*view.width()/view.height()))
            image=QImage(width*3,height*3,QImage.Format.Format_ARGB32_Premultiplied);image.fill(Qt.GlobalColor.transparent)
            painter=QPainter(image);renderer.render(painter,QRectF(0,0,width*3,height*3));painter.end();image.setDevicePixelRatio(3)
            self.images[key]=image
        return self.images[key]

    def png(self,relative,height):
        key=(relative,round(height))
        if key not in self.images:
            payload=self.read(relative)
            if payload is None:return None
            image=QImage.fromData(payload)
            if image.isNull():return None
            image=image.scaledToHeight(round(height*2),Qt.TransformationMode.SmoothTransformation);image.setDevicePixelRatio(2)
            self.images[key]=image
        return self.images[key]

    def consumable(self,kind,height=23):
        key=self.profile.get('consumables',{}).get(str(kind),{}).get('icon_key')
        return self.png('consumables/'+key+'.png',height) if key else None

    def plane(self,params,relation,height=20):
        key=self.profile.get('aircraft',{}).get(str(params),{}).get('marker_key')
        return self.png('planes/'+key+'_'+relation+'.png',height) if key else None

    def projectile(self,params):return self.profile.get('projectiles',{}).get(str(params),{})


@lru_cache(maxsize=4)
def default_assets():return MinimapAssets()
