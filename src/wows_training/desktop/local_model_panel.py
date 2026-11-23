"""Local, original-view WoWSP decision scores and navigable coverage."""
from __future__ import annotations

import base64
from datetime import datetime
from html import escape
from pathlib import Path
import threading
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QPointF, QRectF, QSaveFile, QThread, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget)
from .. import local_decision as model
from ..decision_review import MAX_GAP_SECONDS, MIN_CONFIDENCE, build_decision_review
from ..replay_battle_state import ReplayBattleState
from . import academy_palette as tone
from .analysis_surface import PRIMARY_SERIES,SECONDARY_SERIES,paint_analysis_surface
from .layered_panel import LayeredPanel


def _pct(value):
    return '未知' if value is None else f'{100*value:.1f}%'


def _clock(value):
    seconds=max(0,int(value))
    return f'{seconds//60:02}:{seconds%60:02}'


class _Analysis(QThread):
    resultReady=Signal(object)
    failed=Signal(str)

    def __init__(self, report, path, parent):
        super().__init__(parent)
        self.report, self.path=report,path
        self.cancel_event=threading.Event()

    def run(self):
        try:
            result=model.analyze(self.report,self.path,self.cancel_event)
            if not self.cancel_event.is_set():
                self.resultReady.emit(result)
        except model.DecisionError as exc:
            if not self.cancel_event.is_set():
                self.failed.emit(str(exc))
        except Exception:
            self.failed.emit('本地分析未完成。')


class _Timeline(QWidget):
    seek=Signal(float)
    inspected=Signal(object)
    series=(('move',PRIMARY_SERIES),('fire',SECONDARY_SERIES))

    def __init__(self):
        super().__init__()
        self.samples=[]
        self.selected=None
        self.pinned=None
        self.checkpoints=[]
        self.series_visible={'move':True,'fire':True}
        self.setMinimumHeight(165)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName('走位与开火一致度时间曲线')
        self.setToolTip('悬停检查采样。用左右方向键切换，回车跳到小地图。')

    def set_samples(self,samples,checkpoints=()):
        self.samples=samples
        self.checkpoints=list(checkpoints)
        self.selected=None
        self.pinned=None
        self.update()

    def show_series(self, name, visible):
        self.series_visible[name] = visible
        self.update()

    def inspect_clock(self, clock, *, pin=True):
        if self.samples:
            self.selected = min(self.samples, key=lambda row:abs(row['t']-clock))
            if pin:
                self.pinned = self.selected
            self.inspected.emit(self.selected)
            self.update()

    def _plot(self, width, height):
        return QRectF(48,39,max(1,width-70),max(1,height-70))

    def x(self,t,plot):
        return plot.left()+plot.width()*t/max(1,self.samples[-1]['t'])

    def draw(self,p,width,height):
        paint_analysis_surface(p,QRectF(0,0,width,height))
        plot=self._plot(width,height)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for i in range(3):
            y=plot.bottom()-plot.height()*i*.5
            p.setPen(QPen(QColor(tone.LINE),1))
            p.drawLine(QPointF(plot.left(),y),QPointF(plot.right(),y))
            p.setPen(QColor(tone.MUTED))
            p.drawText(QRectF(0,y-10,43,20),Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter,f'{i*50}%')
        if not self.samples:
            p.setPen(QColor(tone.MUTED))
            p.drawText(plot,Qt.AlignmentFlag.AlignCenter,'暂无决策时间点')
            return
        # Short discontinuities in the recorded samples remain visible as gaps.
        p.setPen(QPen(QColor(tone.LINE),1))
        p.drawLine(QPointF(plot.left(),plot.top()-16),QPointF(plot.right(),plot.top()-16))
        for point in self.checkpoints:
            x = self.x(point['clock'],plot)
            y = plot.top()-16
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(PRIMARY_SERIES if point['axis']=='move' else SECONDARY_SERIES))
            p.drawPolygon(QPolygonF([QPointF(x,y-6),QPointF(x+6,y),QPointF(x,y+6),QPointF(x-6,y)]))
        p.setBrush(Qt.BrushStyle.NoBrush)
        for k,color in self.series:
            if not self.series_visible[k]:
                continue
            p.setPen(QPen(QColor(color),2,Qt.PenStyle.DashLine if k == "fire" else Qt.PenStyle.SolidLine))
            path=QPainterPath()
            prev=None
            for s in self.samples:
                if s.get(k) is None:
                    prev=None
                    continue
                point=QPointF(self.x(s['t'],plot),plot.bottom()-plot.height()*s[k])
                if prev is None or s['t']-prev>MAX_GAP_SECONDS:
                    path.moveTo(point)
                    p.drawPoint(point)
                else:
                    path.lineTo(point)
                prev=s['t']
            p.drawPath(path)
        p.setPen(QColor(tone.MUTED))
        for fraction in (0,.5,1):
            x=plot.left()+plot.width()*fraction
            p.drawText(QRectF(x-28,plot.bottom()+3,55,20),Qt.AlignmentFlag.AlignCenter,_clock(self.samples[-1]['t']*fraction))
        if self.selected is not None:
            x=self.x(self.selected['t'],plot)
            p.setPen(QPen(QColor(tone.BLUE),1,Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x,plot.top()),QPointF(x,plot.bottom()))
            for key, color in self.series:
                value = self.selected.get(key)
                if value is not None and self.series_visible[key]:
                    p.setBrush(QColor(color))
                    p.setPen(QPen(QColor('#F8FCFF'),1.6))
                    p.drawEllipse(QPointF(x,plot.bottom()-value*plot.height()),4.5,4.5)
            if width >= 320 and height >= 155:
                left = x + 10 if x + 210 < plot.right() else x - 208
                box = QRectF(max(plot.left()+2,left),plot.top()+10,198,68)
                p.setBrush(QColor(249,253,255,246))
                p.setPen(QPen(QColor('#7EAEC9'),1))
                p.drawRoundedRect(box,5,5)
                p.setPen(QColor(tone.INK))
                p.drawText(QPointF(box.left()+10,box.top()+18),_clock(self.selected['t']))
                p.setPen(QColor(PRIMARY_SERIES))
                p.drawText(QPointF(box.left()+10,box.top()+37),'走位 ' + _pct(self.selected.get('move')))
                p.setPen(QColor(SECONDARY_SERIES))
                p.drawText(QPointF(box.left()+104,box.top()+37),'开火 ' + _pct(self.selected.get('fire')))
                p.setPen(QColor(tone.MUTED))
                p.drawText(QPointF(box.left()+10,box.top()+56),'置信度 ' + _pct(self.selected.get('confidence')))

    def paintEvent(self,event):
        p=QPainter(self)
        try:
            self.draw(p,self.width(),self.height())
        finally:
            p.end()

    def mouseMoveEvent(self,event):
        if self.samples:
            plot=self._plot(self.width(),self.height())
            nearest=min(self.samples,key=lambda s: abs(self.x(s['t'],plot)-event.position().x()))
            self.selected=nearest if abs(self.x(nearest['t'],plot)-event.position().x())<=10 else self.pinned
            self.inspected.emit(self.selected)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self,event):
        self.selected=self.pinned
        self.inspected.emit(self.pinned)
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self,event):
        if self.samples and event.button()==Qt.MouseButton.LeftButton:
            plot=self._plot(self.width(),self.height())
            if event.position().y() < plot.top() and self.checkpoints:
                key = min(self.checkpoints,key=lambda s:abs(self.x(s['clock'],plot)-event.position().x()))
                if abs(self.x(key['clock'],plot)-event.position().x())<=10:
                    self.seek.emit(key['clock'])
                    return
            nearest=min(self.samples,key=lambda s: abs(self.x(s['t'],plot)-event.position().x()))
            if abs(self.x(nearest['t'],plot)-event.position().x())<=10:
                self.seek.emit(nearest['t'])
        super().mousePressEvent(event)

    def keyPressEvent(self,event):
        if not self.samples:
            super().keyPressEvent(event)
            return
        key=event.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            index=self.samples.index(self.selected) if self.selected in self.samples else 0
            step=-1 if key==Qt.Key.Key_Left else 1
            self.inspect_clock(self.samples[min(len(self.samples)-1,max(0,index+step))]['t'])
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.selected is not None:
            self.seek.emit(self.selected['t'])
        elif key == Qt.Key.Key_Escape:
            self.pinned=self.selected=None
            self.inspected.emit(None)
            self.update()
        else:
            super().keyPressEvent(event)


class LocalModelPanel(QWidget):
    idle=Signal()
    seekRequested=Signal(float)

    def __init__(self,host):
        super().__init__(host)
        self.host=host
        self.report={}
        self.path=None
        self.result=None
        self._worker=None
        self._ticket=0
        self._worker_ticket=-1
        self._source_issue=''
        self._source_key=None
        layout=QVBoxLayout(self)
        controls=QHBoxLayout()
        self.run_button=QPushButton('运行本地评分')
        self.run_button.setObjectName('primary')
        self.run_button.clicked.connect(self.start)
        self.cancel_button=QPushButton('取消')
        self.cancel_button.clicked.connect(self.cancel)
        self.choose_button=QPushButton('选择原录像')
        self.choose_button.clicked.connect(self.choose_file)
        self.export_button=QPushButton('导出评分')
        self.export_button.clicked.connect(self.choose_export)
        for w in (self.run_button,self.choose_button,self.cancel_button,self.export_button):
            controls.addWidget(w)
        controls.addStretch()
        layout.addLayout(controls)
        self.status=QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.select=QComboBox()
        self.select.currentIndexChanged.connect(self._selection_changed)
        self.select.setAccessibleName('决策评分舰船')
        layout.addWidget(self.select)
        metrics=QGridLayout()
        metrics.setHorizontalSpacing(9)
        self.numbers=[]
        self.coverage_notes=[]
        for i,title in enumerate(('综合一致度','开火一致度','走位一致度','有效时点')):
            metric = LayeredPanel(accent=tone.PINK if i==1 else tone.BLUE,cut=10)
            block = QVBoxLayout(metric); block.setContentsMargins(12,6,12,9);block.setSpacing(1)
            block.addWidget(QLabel(title))
            value=QLabel('—')
            value.setObjectName('metric')
            block.addWidget(value)
            self.numbers.append(value)
            note = QLabel(' ')
            note.setObjectName('subtitle')
            block.addWidget(note)
            self.coverage_notes.append(note)
            metrics.addWidget(metric,0,i)
        layout.addLayout(metrics)
        self.checkpoints = QHBoxLayout()
        self.key_heading = QLabel('关键时刻')
        self.key_heading.setObjectName('sectionTitle')
        self.checkpoints.addWidget(self.key_heading)
        self.checkpoints.addStretch()
        self.key_buttons = []
        layout.addLayout(self.checkpoints)
        legend=QHBoxLayout()
        self.show_move = QCheckBox('走位曲线')
        self.show_fire = QCheckBox('开火曲线')
        for check,color in ((self.show_move,PRIMARY_SERIES),(self.show_fire,SECONDARY_SERIES)):
            check.setChecked(True)
            check.setStyleSheet('color:' + color)
            legend.addWidget(check)
        legend.addStretch()
        legend.addWidget(QLabel('录像时钟'))
        layout.addLayout(legend)
        self.timeline=_Timeline()
        self.timeline.seek.connect(self.seekRequested)
        self.timeline.inspected.connect(self._inspect)
        self.show_move.toggled.connect(lambda value:self.timeline.show_series('move',value))
        self.show_fire.toggled.connect(lambda value:self.timeline.show_series('fire',value))
        layout.addWidget(self.timeline,1)
        self.detail=QLabel()
        self.detail.setWordWrap(True)
        inspection = QHBoxLayout()
        inspection.addWidget(self.detail, 1)
        self.locate_button = QPushButton('回看小地图')
        self.locate_button.setEnabled(False)
        self.locate_button.clicked.connect(self._locate)
        inspection.addWidget(self.locate_button)
        layout.addLayout(inspection)
        self.source=QLabel('WoWSP-D1 · 水面舰 · 本地 CPU')
        self.source.setObjectName('subtitle')
        self.source.setToolTip('分值表示实际开火选择和移动方向与模型预测的吻合程度。开火不可评价的窗口仅计走位。')
        layout.addWidget(self.source)
        self.set_source(None)

    @property
    def busy(self):
        return self._worker is not None

    def _controls(self):
        self.run_button.setEnabled(not self.busy and not self._source_issue)
        self.choose_button.setEnabled(not self.busy and not self._source_issue)
        self.export_button.setEnabled(not self.busy and self._selected() is not None)
        self.cancel_button.setVisible(self.busy)

    def set_source(self,report,replay_path=None):
        report=report or {}
        key=(report.get('sha256'),bool(report.get('multiview')),str(replay_path or ''))
        if key==self._source_key:
            return
        self._source_key=key
        self._ticket+=1
        self.cancel()
        self.report=report
        self.path=Path(replay_path) if replay_path else None
        self._source_issue=model.source_issue(report)
        cached=None
        if not self._source_issue:
            cached=model.load_cache(self.host.library.root,report['sha256'])
        self._show_result(cached)

    def choose_file(self):
        path,_=QFileDialog.getOpenFileName(self,'选择本局原始录像','','WoWS 录像 (*.wowsreplay)')
        if path:
            self.path=Path(path)
            self.status.setText('已选原始录像')

    def start(self):
        if self.busy or self._source_issue:
            return False
        if not self.path or not self.path.is_file():
            self.choose_file()
        if not self.path or not self.path.is_file():
            return False
        self._ticket+=1
        self._worker_ticket=self._ticket
        worker=_Analysis(self.report,self.path,self)
        self._worker=worker
        worker.resultReady.connect(self._ready)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._finished)
        self.status.setText('正在核对录像并计算决策时间线…')
        self._controls()
        worker.start()
        return True

    def cancel(self):
        if self._worker is not None:
            self._worker.cancel_event.set()

    def _ready(self,result):
        if (self._worker_ticket!=self._ticket or self._worker.cancel_event.is_set()
                or result['sourceSha']!=self.report.get('sha256')):
            return
        self._show_result(result)
        try:
            model.save_result(result,model.cache_path(self.host.library.root,result['sourceSha']))
        except (OSError,ValueError):
            self.status.setText(self.status.text()+' · 缓存写入失败，可手动导出。')

    def _failed(self,text):
        if self._worker_ticket==self._ticket and not self._worker.cancel_event.is_set():
            self.status.setText(text)

    def _finished(self):
        if self._worker_ticket==self._ticket and self._worker.cancel_event.is_set():
            self.status.setText('本次分析已取消。')
        self._worker.deleteLater()
        self._worker=None
        self._controls()
        self.idle.emit()

    def _name(self,ship):
        matching=[p for p in self.report.get('roster',[]) if str(p.get('shipId'))==str(ship['entityId'])
            and str(p.get('shipParamsId'))==str(ship['shipId'])]
        desc=self.report.get('metadata') or {}
        if len(matching)==1 and matching[0].get('name'):
            name=matching[0]['name']
        elif ship['isRecorder']:
            name=desc.get('playerName') or '录制者'
        else:
            candidates=[row.get('name') for row in self.report.get('roster',[])
                        if str(row.get('shipParamsId'))==str(ship['shipId'])]
            if not candidates:
                candidates=[row.get('name') for row in desc.get('vehicles',[]) if str(row.get('shipId'))==str(ship['shipId'])]
            name=candidates[0] if len(candidates)==1 and candidates[0] else '选手未知'
        hull=(self.host.catalog.get(str(ship['shipId'])) or {}).get('display_name') or model.SURFACE[ship['shipClass']]
        side='录制者' if ship['isRecorder'] else '友方' if ship['teamId']==0 else '对方'
        return f'{side} · {name} · {hull}'

    def _show_result(self,result):
        self.result=result
        self.select.blockSignals(True)
        self.select.clear()
        ships=result.get('perShip',[]) if result else []
        own=next((s for s in ships if s['isRecorder']),None)
        if result and own is None:
            self.select.addItem('录制者 · 无有效决策时点',None)
        for ship in sorted(ships,key=lambda s:(not s['isRecorder'],s['teamId'],s['entityId'])):
            self.select.addItem(self._name(ship),ship['entityId'])
        self.select.blockSignals(False)
        if result:
            terrain='已载入地图遮挡' if result.get('terrainAvailable') else '地图遮挡未知'
            self.status.setText(f'{len(ships)} 艘水面舰可评 · {terrain}' if ships else '本局没有可评分的水面舰。舰种、队伍归属或连续观测未满足模型条件。')
        else:
            self.status.setText(self._source_issue or ('尚未评分' if self.path else '缺少原始录像'))
        self._selection_changed()

    def _selected(self):
        key=self.select.currentData()
        return next((s for s in (self.result or {}).get('perShip',[]) if s['entityId']==key),None)

    def _selection_changed(self,*_):
        ship=self._selected()
        points = ship['samples'] if ship else []
        qualified = sum(p['confidence'] >= MIN_CONFIDENCE for p in points)
        values=(_pct(ship['wholeGame']),_pct(ship['wholeFire']),_pct(ship['wholeMove']),f'{qualified} / {len(points)}') if ship else ('—',)*4
        for label,v in zip(self.numbers,values):
            label.setText(v)
        fire = sum(p['fire'] is not None and p['confidence'] >= MIN_CONFIDENCE for p in points)
        move = sum(p['move'] is not None and p['confidence'] >= MIN_CONFIDENCE for p in points)
        for note, caption in zip(self.coverage_notes,
                ('按模型输出', f'{fire} 个有效开火时点', f'{move} 个有效走位时点', '按模型置信度门槛') if ship else (' ',)*4):
            note.setText(caption)
        self._battle_state = ReplayBattleState(self.report) if ship else None
        anchors=[]
        if ship:
            from ..battle_review import build_review
            review = build_review(self.report, self.host.catalog)
            decision = build_decision_review(self.report, review, self.result, ship['entityId'])
            anchors = decision['checkpoints']
        for button in self.key_buttons:
            self.checkpoints.removeWidget(button)
            button.hide()
            button.deleteLater()
        self.key_buttons.clear()
        for anchor in sorted(anchors, key=lambda item:item['clock']):
            clock = anchor['clock']
            title = '低开火一致度' if anchor['axis']=='fire' else '低走位一致度'
            button = QPushButton(f'{_clock(clock)} · {title}')
            button.setToolTip(anchor['text'] + ' 点击查看该时点。')
            button.clicked.connect(lambda _=False, t=clock:self.timeline.inspect_clock(t))
            self.checkpoints.insertWidget(self.checkpoints.count()-1,button)
            self.key_buttons.append(button)
        self.key_heading.setVisible(bool(anchors))
        self.timeline.set_samples(points, anchors)
        self._inspect(None)
        self._controls()

    def _inspect(self,point):
        ship=self._selected()
        self.locate_button.setEnabled(point is not None)
        if point is not None:
            text=f"{_clock(point['t'])}　综合 {_pct(point['inst'])}　开火 {_pct(point['fire'])}　走位 {_pct(point['move'])}　模型置信度 {_pct(point['confidence'])}"
            if self._battle_state:
                scores = [self._battle_state.score(team, point['t']) for team in (0,1)]
                if any(score is not None for score in scores):
                    text += '　当时比分 ' + ' : '.join(str(score) if score is not None else '—' for score in scores)
            self.detail.setText(text)
        elif ship:
            first,last=ship['samples'][0]['t'],ship['samples'][-1]['t']
            self.detail.setText(f'有效时点 {_clock(first)}–{_clock(last)}')
        elif self.result and self.result.get('perShip'):
            self.detail.setText('录制者缺少有效决策时点')
        else:
            self.detail.clear()

    def _locate(self):
        if self.timeline.selected is not None:
            self.seekRequested.emit(self.timeline.selected['t'])

    def choose_export(self):
        path,fmt=QFileDialog.getSaveFileName(self,'导出本地评分','本局决策评分.html','当前舰船图表 (*.html);;全局评分数据 (*.json)')
        if path:
            try:
                self.export_to(path,'json' if '*.json' in fmt else 'html')
                self.status.setText('本地评分已导出。')
            except (ValueError,OSError) as exc:
                self.status.setText('导出失败。'+str(exc))

    def export_to(self,path,kind):
        if self.result is None:
            raise ValueError('请先运行本地评分。')
        path=Path(path).with_suffix('.'+kind)
        if kind=='json':
            model.save_result(self.result,path)
            return path
        ship=self._selected()
        if kind!='html' or ship is None:
            raise ValueError('请选择已评分舰船。')
        image=QImage(1080,320,QImage.Format.Format_ARGB32_Premultiplied)
        painter=QPainter(image)
        selected=self.timeline.selected
        try:
            self.timeline.selected=None
            self.timeline.draw(painter,1080,320)
        finally:
            self.timeline.selected=selected
            painter.end()
        data=QByteArray()
        buffer=QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer,'PNG'):
            raise OSError('图表写入失败')
        buffer.close()
        src=base64.b64encode(bytes(data)).decode('ascii')
        name=escape(self._name(ship))
        meta=self.report.get('metadata') or {}
        stamp=str(meta.get('dateTime') or '')
        try:
            stamp=datetime.strptime(stamp,'%d.%m.%Y %H:%M:%S').strftime('%Y-%m-%d %H:%M')
        except ValueError:
            pass
        map_name=self.report.get('display_map_name') or self.result.get('mapName','').rsplit('/',1)[-1]
        context=escape(' · '.join(s for s in (stamp,map_name) if s))
        text=(f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>本局决策评分</title>'
              '<style>body{max-width:1080px;margin:48px auto;padding:18px;background:#EDF4FA;color:#12263D;font:16px sans-serif}h1{color:#1263D3}img{width:100%}</style>'
              f'<h1>{name}</h1><p>{context}</p><p>综合一致度 {_pct(ship["wholeGame"])}　开火选择 {_pct(ship["wholeFire"])}　走位方向 {_pct(ship["wholeMove"])}　有效时点 {len(ship["samples"])}</p>'
              f'<img alt="走位与开火时间线" src="data:image/png;base64,{src}"><p>青色实线为走位，粉色虚线为开火。横轴为录像时钟。曲线间断处缺少可用窗口。</p>'
              '<p>WoWSP-D1 · 权重序列 3 · 实际决策与模型预测的一致度</p></html>')
        target=QSaveFile(str(path))
        encoded=text.encode('utf8')
        if not target.open(QIODevice.OpenModeFlag.WriteOnly) or target.write(encoded)!=len(encoded) or not target.commit():
            raise OSError('文件写入失败')
        return path
