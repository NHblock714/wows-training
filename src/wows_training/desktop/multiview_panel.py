"""Import, retain and exchange paired replay recordings without blocking Qt."""
from __future__ import annotations
from bisect import bisect_right
from datetime import datetime
from pathlib import Path
import threading

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QLabel,
    QListWidget, QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget)
from .ui_components import Disclosure,align_cell,style_table


class RecordingJob(QThread):
    progress = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.cancel_event = threading.Event()

    def run(self):
        try:
            self.succeeded.emit(self.operation(self.cancel_event, self.progress.emit))
        except Exception as exc:
            self.failed.emit(str(exc))


class MultiviewPanel(QWidget):
    idle = Signal()
    saved = Signal()

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.job = None
        self._paths = []
        self._records = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 23, 26, 18)
        title = QLabel('双边录像');title.setObjectName('title');layout.addWidget(title)
        actions = QHBoxLayout()
        self.files_button = self._button('选择双方回放', self.choose_replays, 'multiviewChooseReplays')
        self.archives_button = self._button('选择压缩包', self.choose_archives, 'multiviewChooseArchives')
        self.folder_button = self._button('选择文件夹', self.choose_folder, 'multiviewChooseFolder')
        self.package_button = self._button('导入合并录像包', self.choose_package, 'multiviewChoosePackage')
        self.clear_button = self._button('清空待导入', self.clear_inputs, 'multiviewClear')
        for button in (self.files_button,self.archives_button,self.folder_button,self.package_button,self.clear_button):actions.addWidget(button)
        actions.addStretch();layout.addLayout(actions)
        self.inputs = QListWidget();self.inputs.setObjectName('multiviewInputs');self.inputs.setMaximumHeight(105);layout.addWidget(self.inputs)
        run = QHBoxLayout()
        self.import_button = self._button('配对、合并并保存', self.start_import, 'multiviewImport')
        self.import_button.setObjectName('primary')
        self.cancel_button = self._button('取消', self.cancel, 'multiviewCancel');self.cancel_button.setEnabled(False)
        run.addWidget(self.import_button);run.addWidget(self.cancel_button);run.addStretch()
        self.notice=QLabel()
        self.notice.setWordWrap(True);run.addWidget(self.notice,2);layout.addLayout(run)
        self.recordings=QTableWidget(0,5);self.recordings.setObjectName('multiviewRecordings')
        self.recordings.setHorizontalHeaderLabels(['比赛时间','地图','来源数','资料状态','保存时间'])
        self.recordings.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.recordings.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.recordings.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.recordings.verticalHeader().hide();self.recordings.horizontalHeader().setStretchLastSection(True)
        style_table(self.recordings)
        for i,w in enumerate([175,150,80,270,190]):self.recordings.setColumnWidth(i,w)
        self.recordings.itemSelectionChanged.connect(self.describe_selected)
        self.recordings.itemDoubleClicked.connect(lambda _:self.open_selected())
        layout.addWidget(self.recordings,1)
        self.details=QPlainTextEdit();self.details.setReadOnly(True);self.details.setObjectName('multiviewDetails')
        self.details.setMaximumHeight(210)
        self.detail_disclosure=Disclosure('录像信息',self.details);layout.addWidget(self.detail_disclosure)
        bottom=QHBoxLayout()
        self.open_button=self._button('打开合并复盘',self.open_selected,'multiviewOpen')
        self.export_button=self._button('导出完整录像包',self.export_selected,'multiviewExport')
        bottom.addWidget(self.open_button);bottom.addWidget(self.export_button)
        bottom.addWidget(self._button('刷新',self.refresh,'multiviewRefresh'));bottom.addStretch();layout.addLayout(bottom)
        self.refresh()

    def _button(self,text,callback,name):
        button=QPushButton(text);button.setObjectName(name);button.clicked.connect(callback);return button

    @property
    def busy(self):return bool(self.job and self.job.isRunning())

    def set_inputs(self,paths,*,append=False):
        if self.busy:return
        values=self._paths if append else []
        self._paths=list(dict.fromkeys([*values,*[str(Path(p).resolve()) for p in paths]]))
        self.inputs.clear();self.inputs.addItems(self._paths)
        self.notice.setText(f'待处理 {len(self._paths)} 项')

    def clear_inputs(self):self.set_inputs([])

    def choose_replays(self):
        paths,_=QFileDialog.getOpenFileNames(self,'选择同场双方回放（可多选）','','WOWS 回放 (*.wowsreplay)')
        if paths:self.set_inputs(paths,append=True)

    def choose_archives(self):
        paths,_=QFileDialog.getOpenFileNames(self,'选择双方压缩包（可同时选择两份）','','回放压缩包 (*.rar *.zip)')
        if paths:self.set_inputs(paths,append=True)

    def choose_folder(self):
        path=QFileDialog.getExistingDirectory(self,'选择包含双方回放的文件夹')
        if path:self.set_inputs([path],append=True)

    def choose_package(self):
        paths,_=QFileDialog.getOpenFileNames(self,'选择合并录像包','','完整录像包 (*.wowsbattle)')
        if paths:self.set_inputs(paths,append=True)

    def _start(self,operation,success):
        if hasattr(self.host,'dossier_panel') and self.host.dossier_panel.busy:
            self.notice.setText('请先完成或取消当前个人档案任务。');return False
        if hasattr(self.host,'statistics_panel') and self.host.statistics_panel.busy:
            self.notice.setText('请先完成或取消当前统计。');return False
        if self.busy or (self.host.import_worker and self.host.import_worker.isRunning()):
            self.notice.setText('当前还有导入任务，请完成或取消后重试。');return False
        self.job=RecordingJob(operation,self)
        self.job.progress.connect(self.notice.setText)
        self.job.succeeded.connect(success)
        self.job.failed.connect(lambda error:self.notice.setText(f'处理未完成：{error}'))
        self.job.finished.connect(self._finished)
        self._set_enabled(False);self.job.start();return True

    def _set_enabled(self,enabled):
        for widget in (self.files_button,self.archives_button,self.folder_button,self.package_button,self.clear_button,self.import_button,self.export_button):widget.setEnabled(enabled)
        self.cancel_button.setEnabled(not enabled)

    def _finished(self):
        self._set_enabled(True);self.idle.emit()

    def cancel(self):
        if self.busy:self.job.cancel_event.set();self.cancel_button.setEnabled(False);self.notice.setText('正在停止当前任务；已保存资料保留。')

    def start_import(self):
        if not self._paths:self.notice.setText('请先选择双方回放、压缩包或录像包。');return False
        paths=list(self._paths)
        def operation(cancel,progress):
            from wows_training.multiview_service import MultiviewImportService
            return MultiviewImportService(self.host.library.root,self.host.bundle.root).import_inputs(paths,cancel=cancel,progress=progress)
        return self._start(operation,self._import_done)

    def _import_done(self,result):
        self.host.refresh_library();self.host.refresh_open_sources();self.refresh();self.saved.emit()
        items=result.get('items',[])
        saved=sum(bool(item.get('recording_id')) for item in items)
        pending=sum(item.get('status')=='pending_counterpart' for item in items)
        unfinished=[item for item in items if not item.get('recording_id') and item.get('status')!='pending_counterpart']
        summary=[f'已合并 {saved} 项']
        if pending:summary.append(f'待补视角 {pending} 项')
        if unfinished:summary.append(f'未完成 {len(unfinished)} 项')
        self.notice.setText(' · '.join(summary))
        reasons=[]
        for item in unfinished:
            name=Path(item['input']).name if item.get('input') else ''
            reason=item.get('message') or '来源无法合并'
            reasons.append(f'{name} · {reason}' if name else reason)
        self.details.setPlainText('\n'.join(reasons))
        self.detail_disclosure.setExpanded(bool(reasons))

    def selected_id(self):
        indexes=self.recordings.selectionModel().selectedRows()
        return self.recordings.item(indexes[0].row(),0).data(Qt.ItemDataRole.UserRole) if indexes else None

    def refresh(self):
        from wows_training.multiview_selection import current_recording
        previous=self.selected_id() if self.recordings.rowCount() else None
        self._records=self.host.recording_store.list_recordings()
        self.recordings.blockSignals(True);self.recordings.setRowCount(len(self._records))
        for i,record in enumerate(self._records):
            try:
                battle=self.host.library.get_battle(record['battle_id']);current=current_recording(self.host.library,self.host.recording_store,record['battle_id'])
                status='当前版本' if current and current['id']==record['id'] else '历史版本'
            except KeyError:battle={};status='已保存 · 原来源可恢复'
            created=record.get('created_at','')
            try:created=datetime.fromisoformat(created).astimezone().strftime('%Y-%m-%d %H:%M')
            except ValueError:pass
            values=[battle.get('date_raw',''),self.host.map_names.get(battle.get('map_id'),battle.get('map_id','')),record.get('source_count',len(record.get('sources',[]))),status,created]
            for col,value in enumerate(values):
                item=QTableWidgetItem(str(value));item.setData(Qt.ItemDataRole.UserRole,record['id']);item.setToolTip(str(value));align_cell(item,value);self.recordings.setItem(i,col,item)
            if record['id']==previous:self.recordings.selectRow(i)
        self.recordings.blockSignals(False)

    def describe_selected(self):
        key=self.selected_id()
        if not key:return
        try:
            report=self.host.recording_store.load_report(key);mv=report['multiview'];coverage=mv.get('coverage',{})
            lines=[f'比赛时间：{coverage.get("first_elapsed_sec","?")}–{coverage.get("last_elapsed_sec","?")} 秒',f'位置覆盖：{coverage.get("full_alive_position_seconds","?")} / {coverage.get("observed_timer_seconds","?")} 秒','录制视角：']
            for source in mv.get('sources',[]):
                lines.append(str(source.get('recorder') or source.get('recorder_name') or source.get('player_name') or '未命名录制者'))
            conflicts=mv.get('conflict_counts')
            if conflicts:
                names={'hp_disagreement':'血量记录差异','position_disagreement':'位置记录差异','death_time_disagreement':'击沉时刻差异','death_killer_disagreement':'击沉归属差异','result_duration_disagreement':'结算时长差异','score_disagreement':'得分记录差异','capture_team_disagreement':'占点归属差异'}
                lines.append('视角差异：')
                lines.extend(f'{names.get(kind,"其他观测差异")}：{count}' for kind,count in conflicts.items())
            self.details.setPlainText('\n'.join(lines))
        except Exception as exc:self.notice.setText(f'无法读取保存记录：{exc}')

    def open_selected(self):
        key=self.selected_id()
        if key:self.host.open_recording(key)
        else:self.notice.setText('请先选择一项保存记录。')

    def export_selected(self):
        key=self.selected_id()
        if not key:self.notice.setText('请先选择需要导出的合并记录。');return
        filename,_=QFileDialog.getSaveFileName(self,'导出包含双方原文件的完整录像包',str(self.host.library.root/f'battle_{key[:16]}.wowsbattle'),'完整录像包 (*.wowsbattle)')
        if not filename:return
        if not filename.lower().endswith('.wowsbattle'):filename+='.wowsbattle'
        def operation(cancel,progress):
            if cancel.is_set():raise InterruptedError('已取消')
            progress('正在打包双方回放、来源记录与合并复盘…')
            self.host.recording_store.export_package(key,Path(filename));return filename
        self._start(operation,lambda path:self.notice.setText(f'已导出完整录像包：{path}'))
