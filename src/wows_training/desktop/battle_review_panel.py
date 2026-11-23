"""Single-replay roster, actual result, damage observations and local decision model."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF,QRectF,QSignalBlocker,Qt,Signal
from PySide6.QtGui import QColor,QPainter,QPen,QPolygonF,QPixmap
from PySide6.QtWidgets import (QAbstractItemView,QComboBox,QFileDialog,QGridLayout,QHBoxLayout,QHeaderView,QLabel,
    QDialog,QPushButton,QScrollArea,QStackedWidget,QTableWidget,QTableWidgetItem,QTabWidget,QVBoxLayout,QWidget)

from ..battle_review import build_battle_evidence,build_review,clock_text,display,export_review
from ..ordinary_replay import METRICS,number
from .fleet_shell import FleetPageHeader
from .local_model_panel import LocalModelPanel
from . import academy_palette as tone
from .statistics_visuals import MetricCard
from .ui_components import align_cell,style_table
from .analysis_surface import PRIMARY_SERIES,SECONDARY_SERIES,paint_analysis_surface
from ..performance_evidence import selected_settlement
from ..ribbon_assets import ribbon_icon_path


class DamageTimeline(QWidget):
    seek=Signal(float)
    def __init__(self,parent=None):
        super().__init__(parent);self.setMinimumHeight(112);self.setMaximumHeight(175)
        self.series=([],[]);self.duration=None;self.ceiling=1.
        self.setAccessibleName('局中伤害观测曲线')

    def set_events(self,events,entity,duration):
        self.series=([],[]);sums=[0.,0.]
        for row in events:
            for index,key in enumerate(('actor','target')):
                if entity is not None and row[key]==entity:
                    sums[index]+=row['amount'];self.series[index].append((row['clock'],sums[index]))
        self.ceiling=max(*sums,1.);self.duration=duration;self.update()

    def plot(self):return QRectF(55,28,max(1,self.width()-78),max(1,self.height()-52))

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_analysis_surface(p,self.rect())
        try:
            rect=self.plot();p.setPen(QColor(tone.MUTED))
            if not any(self.series) or not self.duration:
                p.drawText(self.rect(),Qt.AlignmentFlag.AlignCenter,'缺少该舰的局中伤害观测');return
            p.drawText(12,18,'累计观测伤害');p.setPen(QColor(PRIMARY_SERIES));p.drawText(130,18,'输出');p.setPen(QColor(SECONDARY_SERIES));p.drawText(177,18,'承伤')
            p.setPen(QPen(QColor(tone.LINE),1));p.drawRect(rect)
            for i in range(1,4):
                y=rect.bottom()-rect.height()*i/4;p.drawLine(QPointF(rect.left(),y),QPointF(rect.right(),y))
            for rows,color in zip(self.series,(PRIMARY_SERIES,SECONDARY_SERIES)):
                if not rows:continue
                px=rect.left();py=rect.bottom();points=[QPointF(px,py)]
                for t,amount in rows:
                    x=rect.left()+rect.width()*min(1,t/self.duration);y=rect.bottom()-rect.height()*amount/self.ceiling
                    points.extend([QPointF(x,py),QPointF(x,y)]);py=y
                points.append(QPointF(rect.right(),py));p.setPen(QPen(QColor(color),2,Qt.PenStyle.DashLine if color==SECONDARY_SERIES else Qt.PenStyle.SolidLine));p.drawPolyline(QPolygonF(points))
            p.setPen(QColor(tone.MUTED));p.drawText(4,rect.top()+9,f'{self.ceiling/1000:.1f}k');p.drawText(int(rect.left()),self.height()-6,'00:00')
            p.drawText(int(rect.right())-45,self.height()-6,clock_text(self.duration))
        finally:p.end()

    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton and self.duration and any(self.series) and self.plot().contains(event.position()):
            self.seek.emit((event.position().x()-self.plot().left())/self.plot().width()*self.duration)
        super().mousePressEvent(event)


def label(text='',name=''):
    item=QLabel(text);item.setTextFormat(Qt.TextFormat.PlainText);item.setObjectName(name);item.setWordWrap(True);return item


def table(headers):
    widget=QTableWidget(0,len(headers));widget.setHorizontalHeaderLabels(headers)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection);widget.setAlternatingRowColors(True);widget.verticalHeader().hide();style_table(widget)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch);return widget


def fill(widget,rows):
    widget.setRowCount(len(rows))
    for i,values in enumerate(rows):
        for j,value in enumerate(values):
            item=QTableWidgetItem(display(value));align_cell(item,value);item.setToolTip(item.text());widget.setItem(i,j,item)


class BattleReviewPanel(QWidget):
    idle=Signal()
    def __init__(self,host):
        super().__init__(host);self.host=host;self.report=None;self.data=None
        self._replay_picker = None
        outer=QVBoxLayout(self);outer.setContentsMargins(24,18,24,12);outer.addWidget(FleetPageHeader('单局分析','replays'))
        bar=QHBoxLayout();self.summary=label('未打开录像','sectionTitle');bar.addWidget(self.summary,1)
        self.library_button=QPushButton('选择录像');self.library_button.setObjectName('reviewChooseReplay')
        self.library_button.clicked.connect(self.choose_replay);bar.addWidget(self.library_button)
        self.report_button=QPushButton('制作报告');self.report_button.clicked.connect(self.to_studio);bar.addWidget(self.report_button)
        self.export_button=QPushButton('导出本局');self.export_button.clicked.connect(self.choose_export);bar.addWidget(self.export_button);outer.addLayout(bar)
        self.scope=label('','subtitle');outer.addWidget(self.scope)
        self.recording_row=QWidget()
        recording_layout=QHBoxLayout(self.recording_row);recording_layout.setContentsMargins(0,0,0,0)
        recording_layout.addWidget(label('录制来源'))
        self.recording_choice=QComboBox();self.recording_choice.setObjectName('reviewRecordingSource')
        self.recording_choice.setAccessibleName('单局分析录制来源')
        self.recording_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.recording_choice.setMinimumContentsLength(16)
        self.recording_choice.setMaximumWidth(520)
        self.recording_choice.activated.connect(self._choose_recording_source)
        recording_layout.addWidget(self.recording_choice,1);recording_layout.addStretch(1)
        outer.addWidget(self.recording_row);self.recording_row.hide()
        self.tabs=QTabWidget();outer.addWidget(self.tabs,1)
        overview=QWidget();body=QVBoxLayout(overview);body.setContentsMargins(8,8,8,5)
        head=QHBoxLayout();head.addWidget(label('参战舰长'));self.player=QComboBox();self.player.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon);self.player.setMinimumContentsLength(14)
        head.addWidget(self.player,1);self.account_button=QPushButton('查询玩家');self.account_button.clicked.connect(self.open_account);head.addWidget(self.account_button);body.addLayout(head)
        tiles=QHBoxLayout();self.tiles={}
        for key in ('damage','ships_killed','scouting_damage','exp'):
            self.tiles[key]=MetricCard(METRICS[key]);tiles.addWidget(self.tiles[key],1)
        body.addLayout(tiles)
        self.ribbon_band = QScrollArea()
        self.ribbon_band.setObjectName('battleRibbonBand')
        self.ribbon_band.setAccessibleName('战斗勋带')
        self.ribbon_band.setWidgetResizable(True)
        self.ribbon_band.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.ribbon_band.setFixedHeight(55)
        self.ribbon_band.hide()
        body.addWidget(self.ribbon_band)
        self.chart=DamageTimeline();self.chart.seek.connect(self.seek);body.addWidget(self.chart)
        self.event_table=table(['录像时间','事件','记录']);self.event_table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch)
        for col,width in ((0,95),(1,90)):
            self.event_table.horizontalHeader().setSectionResizeMode(col,QHeaderView.ResizeMode.Fixed);self.event_table.setColumnWidth(col,width)
        self.event_table.cellDoubleClicked.connect(self.jump_event);body.addWidget(self.event_table,1)
        self.event_hint=label('','subtitle');body.addWidget(self.event_hint);self.tabs.addTab(overview,'战斗概览')
        roster_page=QWidget();roster_layout=QVBoxLayout(roster_page);self.roster_table=table(['阵营','玩家','舰船','输出伤害','击沉','结算经验'])
        for col,width in ((0,67),(3,94),(4,65),(5,92)):
            self.roster_table.horizontalHeader().setSectionResizeMode(col,QHeaderView.ResizeMode.Fixed);self.roster_table.setColumnWidth(col,width)
        roster_layout.addWidget(self.roster_table);self.roster_table.cellDoubleClicked.connect(self.pick_roster);self.tabs.addTab(roster_page,'双方阵容')
        self.model_panel=LocalModelPanel(host);self.model_panel.seekRequested.connect(self.seek);self.model_panel.idle.connect(self.idle);self.tabs.addTab(self.model_panel,'本地模型')
        from .single_battle_ai_panel import SingleBattleAIPanel
        self.ai_report = SingleBattleAIPanel(host, self._random_battle_evidence, self)
        self._ai_report_tab = self.tabs.addTab(self.ai_report, 'AI 报告')
        self.ai_report.idle.connect(self.idle)
        self.model_panel.idle.connect(self.ai_report.refresh_source)
        self.model_panel.select.currentIndexChanged.connect(lambda _index: self.ai_report.refresh_source())
        self.player.currentIndexChanged.connect(self.render_player);self.set_report(None)

    @property
    def busy(self):return self.model_panel.busy or self.ai_report.busy

    def cancel(self):
        self.model_panel.cancel()
        self.ai_report.cancel()

    def choose_replay(self):
        from .review_replay_picker import ReviewReplayPicker
        if self._replay_picker is not None:
            self._replay_picker.raise_()
            self._replay_picker.activateWindow()
            return
        picker = self._replay_picker = ReviewReplayPicker(self.host, self.host.current_battle)
        picker.finished.connect(self._finish_replay_choice)
        picker.open()

    def _finish_replay_choice(self, result):
        from .navigation import REVIEW_PAGE
        picker, self._replay_picker = self._replay_picker, None
        battle_id = picker.battle_id
        picker.deleteLater()
        if result == QDialog.DialogCode.Accepted and battle_id is not None:
            if self.report is None or battle_id != self.host.current_battle:
                self.host.open_battle(battle_id, page=REVIEW_PAGE)

    def sync_recording_sources(self, source_combo):
        entries = [(source_combo.itemText(index), source_combo.itemData(index))
                   for index in range(source_combo.count())]
        with QSignalBlocker(self.recording_choice):
            if entries != [(self.recording_choice.itemText(index), self.recording_choice.itemData(index))
                           for index in range(self.recording_choice.count())]:
                self.recording_choice.clear()
                for title, token in entries:
                    self.recording_choice.addItem(title, token)
                    self.recording_choice.setItemData(self.recording_choice.count()-1, title, Qt.ItemDataRole.ToolTipRole)
            self.recording_choice.setCurrentIndex(source_combo.currentIndex())
        self.recording_choice.setToolTip(source_combo.currentText())
        self.recording_row.setVisible(len(entries)>1)

    def _choose_recording_source(self, index):
        token = self.recording_choice.itemData(index)
        source_index = self.host.source_combo.findData(token) if token is not None else -1
        if source_index >= 0:
            self.host.source_combo.setCurrentIndex(source_index)

    def set_report(self,report):
        self.report=report if isinstance(report,dict) and report.get('sha256') else None
        self.data=build_review(self.report,self.host.catalog) if self.report else None
        path=self.host.current_replay_path() if self.report and hasattr(self.host,'current_replay_path') else None
        self.model_panel.set_source(self.report or {},path)
        random_replay = self._can_open_random_ai()
        self.tabs.setTabVisible(self._ai_report_tab, random_replay)
        self.ai_report.set_source((self.report['sha256'], self.host.current_recording_id) if random_replay else None)
        self.tabs.setVisible(bool(self.data));self.scope.setVisible(bool(self.data));self.report_button.setEnabled(bool(self.data));self.export_button.setEnabled(bool(self.data))
        self.player.blockSignals(True);self.player.clear();self.roster_order=[]
        if self.data:
            d=self.data;self.summary.setText(f"{d['mode']} · {d['map']} · {d['outcome']}")
            self.scope.setText(f"{d['recorder']}　 {d['date']}　 {d['version']}　 {d['quality']}" + ('　赛后结算未知' if not d['full_result'] else ''))
            self.roster_order=sorted(range(len(d['participants'])),key=lambda i:(d['participants'][i]['side']!='我方',d['participants'][i]['name'].casefold()))
            roster=[];selected=0
            for idx in self.roster_order:
                p=d['participants'][idx];self.player.addItem(f"{p['name']} · {p['ship']}",idx)
                if p['name']==d['recorder']:selected=self.player.count()-1
                roster.append([p['side'],p['name'],p['ship'],p['result']['damage'],p['result']['ships_killed'],p['result']['exp']])
            self.player.setCurrentIndex(selected);fill(self.roster_table,roster)
        else:self.summary.setText('未打开录像');self.scope.clear();self.roster_table.setRowCount(0)
        self.player.blockSignals(False);self.render_player()

    def render_player(self,*_):
        key=self.player.currentData();d=self.data
        if d is None or key is None:
            self.events=[];self.chart.set_events([],None,None);self.event_table.setRowCount(0);self.ribbon_band.hide();return
        p=d['participants'][key];entity=p['entity_id'];self.account_button.setEnabled(not p['bot'] and p['name']!='未知')
        for k,tile in self.tiles.items():tile.set_value(display(p['result'][k]))
        self._render_ribbons(entity)
        self.chart.set_events(d['damage_events'],entity,d['clock_max']);self.events=list(d['events'])
        hits=[hit for hit in d['damage_events'] if entity is not None and hit['target']==entity]
        if hits:
            largest=max(hits,key=lambda row:row['amount']);self.events.append({'clock':largest['clock'],'kind':'承伤','text':f"{p['name']} · 最大单次观测 {display(largest['amount'])}"})
        self.events.sort(key=lambda row:row['clock']);fill(self.event_table,[[clock_text(e['clock']),e['kind'],e['text']] for e in self.events]);self.event_hint.setText('' if self.events else '无可定位事件')
        if (d['full_result'] and self.report.get('quality')=='basic_verified' and not self.report.get('multiview')
                and not any(p['result']['damage'] is not None for p in d['participants'])):
            self.event_hint.setText('此版分析缺少结算字段')
        self.event_hint.setVisible(bool(self.event_hint.text()))
        self.event_table.setHorizontalHeaderItem(0,QTableWidgetItem('比赛时间' if d['clock_domain']=='battle_elapsed_seconds' else '录像时间'))

    def _render_ribbons(self, entity):
        card = selected_settlement(self.report, self.data, entity) or {}
        ribbons = card.get('ribbons') or []
        self.ribbon_band.setVisible(bool(ribbons))
        if not ribbons:
            return
        flow = QWidget()
        row = QHBoxLayout(flow)
        row.setContentsMargins(8,4,8,4)
        for ribbon in ribbons:
            tile = QWidget()
            slot = QHBoxLayout(tile)
            slot.setContentsMargins(3,0,3,0)
            slot.setSpacing(3)
            tooltip = f"{ribbon['label']} ×{ribbon['count']}"
            tile.setToolTip(tooltip)
            tile.setAccessibleName(tooltip)
            if icon := ribbon_icon_path(card.get('version'), ribbon['key']):
                image = QPixmap(str(icon))
                if not image.isNull():
                    mark = QLabel()
                    mark.setPixmap(image.scaled(43,26,Qt.AspectRatioMode.KeepAspectRatio,
                                                Qt.TransformationMode.SmoothTransformation))
                    mark.setToolTip(tooltip)
                    slot.addWidget(mark)
            count = QLabel(f"{ribbon['count']}")
            count.setToolTip(tooltip)
            slot.addWidget(count)
            row.addWidget(tile)
        row.addStretch()
        self.ribbon_band.setWidget(flow)

    def pick_roster(self,row,_column):
        if 0<=row<len(self.roster_order):self.player.setCurrentIndex(row);self.tabs.setCurrentIndex(0)

    def open_account(self):
        i=self.player.currentData()
        if self.data and i is not None:self.host.open_replay_account(self.data['participants'][i]['name'])

    def seek(self,clock):
        if self.data and number(self.data['clock_max']) and 0<=clock<=self.data['clock_max']:
            self.host.show_page(1);self.host.replay_panel.set_clock(clock)
        else:
            self.host.statusBar().showMessage('这份录像尚无可定位的地图时间轴。')

    def jump_event(self,row,_column):
        if 0<=row<len(self.events):self.seek(self.events[row]['clock'])

    def _report_model(self):
        from ..decision_review import build_decision_review
        entity_id = self.model_panel.select.currentData()
        return build_decision_review(self.report, self.data, self.model_panel.result, entity_id=entity_id)

    def _can_open_random_ai(self):
        return self.data is not None and self.data['mode'] == '随机战' and not self.report.get('multiview')

    def _random_battle_evidence(self):
        if not self._can_open_random_ai():
            return {}
        return build_battle_evidence(self.report, self.host.catalog, decision=self._report_model(), random_focus=True)

    def to_studio(self):
        if self.report:
            analysis = build_battle_evidence(self.report,self.host.catalog,decision=self._report_model(),random_focus=True,
                                             recording_id=self.host.current_recording_id)
            self.host.open_battle_report(analysis)

    def choose_export(self):
        if not self.report:return
        path,selected=QFileDialog.getSaveFileName(self,'导出单局复盘',str(Path(self.host.library.root)/'单局复盘.html'),'HTML (*.html);;JSON (*.json)')
        if path:
            target=Path(path).with_suffix('.json' if selected.startswith('JSON') else '.html')
            try:export_review(self.report,self.host.catalog,target,decision=self._report_model());self.host.statusBar().showMessage('单局复盘已导出。')
            except OSError as exc:self.host.statusBar().showMessage(f'导出失败：{exc}')
