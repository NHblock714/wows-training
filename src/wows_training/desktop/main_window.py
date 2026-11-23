"""Offline desktop workflow for the verified 15.9 replay capabilities."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QStackedWidget, QTableWidget, QTableWidgetItem, QTextBrowser,
    QVBoxLayout, QWidget, QToolButton,
)

from wows_training.library import Library
from wows_training.runtime_bundle import load_bundle
from wows_training.multiview_store import BattleRecordingStore
from wows_training.version import VERSION
from .replay_panel import ReplayPanel
from .theme import STYLE,apply_native_controls
from .workers import ExportWorker, ImportWorker
from .ui_components import Disclosure,align_cell,style_table
from .fleet_shell import FleetBackdrop,FleetPageHeader,install_page_header
from .navigation import FeatureIndex, HOME_PAGE, REVIEW_PAGE, QUERY_PAGE
from .harbor_sidebar import HarborSidebar


def button(text, callback, name=""):
    widget = QPushButton(text)
    widget.setObjectName(name)
    widget.clicked.connect(callback)
    return widget


def table(headers, name):
    widget = QTableWidget(0, len(headers))
    widget.setObjectName(name)
    widget.setHorizontalHeaderLabels(headers)
    widget.setAlternatingRowColors(True)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.verticalHeader().hide()
    widget.horizontalHeader().setStretchLastSection(True)
    widget.verticalHeader().setDefaultSectionSize(38)
    style_table(widget)
    return widget


def fill_row(widget, row, values, identity=None):
    for column, value in enumerate(values):
        item = QTableWidgetItem(str(value if value is not None else "—"))
        item.setToolTip(item.text())
        item.setData(Qt.ItemDataRole.UserRole, identity)
        align_cell(item,value)
        widget.setItem(row, column, item)


def date_text(raw):
    try:
        return datetime.strptime(raw, "%d.%m.%Y %H:%M:%S").strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(raw)


class MainWindow(QMainWindow):
    def __init__(self, data_root, bundle_root=None):
        super().__init__()
        self.setWindowTitle(f"WOWS 港口开发部 · {VERSION}")
        self.resize(1480, 940)
        self.setMinimumSize(1080, 720)
        from .presentation_fonts import load_presentation_fonts
        fonts=load_presentation_fonts()
        self.setStyleSheet(STYLE+'\nQLabel#sectionTitle, QLabel#title { font-family:"'+fonts['chinese_tech']+'"; font-weight:400; }')
        self.setAcceptDrops(True)
        self.bundle = load_bundle(Path(bundle_root) if bundle_root else None)
        data_root = Path(data_root)
        self.library = Library(data_root)
        self.recording_store = BattleRecordingStore(self.library.root)
        self.current_recording_id = None
        self.current_analysis_revision = None
        self._merged_sources = {}
        self.catalog = self.bundle.ship_catalog()
        self._ships_by_index = {ship['index'].upper(): ship for ship in self.catalog.values() if ship.get('index')}
        self.map_names = self.bundle.json("labels.json").get("maps", {})
        from wows_training.ordinary_replay import map_labels
        self.map_names.update(map_labels())
        self.current_report = None
        self.current_sha = None
        self.current_battle = None
        self.intel = None
        self.import_worker = None
        self._combat_refresh_attempts = set()
        self.export_worker = None
        self.last_export = None
        self._closing = False
        self._battles = []
        from .naval_assets import NavalSound
        self.ui_sound=NavalSound(self.library.root)
        from .motion import MotionController
        self.motion = MotionController(self, self.library.root)
        self._build_ui()
        from .archive_canvas import install_archive_canvases
        install_archive_canvases(self)
        self.motion.register_stack(self.pages)
        self.motion.register_stack(self.report_studio.stack)
        self.motion.register_stack(self.query_center_panel.stack)
        self.motion.refresh()
        from .client_settings import initialize_client_preferences
        initialize_client_preferences(self)
        from .update_controls import UpdateController
        self.updates = UpdateController(self)
        self.updates.idle.connect(self._check_close)
        self.updates.changed.connect(self._show_downloaded_update)
        from .combo_effects import install_combo_effects
        self.combo_effects = install_combo_effects(self)
        self.refresh_library()
        self.statusBar().clearMessage()
        self.statusBar().setToolTip(str(self.library.root))

    def start_update_check(self):
        if not self._closing and self.client_preferences['check_updates']:
            self.updates.check(self.client_preferences['download_updates'])

    def _show_downloaded_update(self):
        if self.updates.downloaded_path and self.updates.message.endswith("已下载并校验。"):
            self.statusBar().showMessage(self.updates.message + " 可在客户端设置中打开下载目录。", 12000)

    def _build_ui(self):
        root = self.backdrop = FleetBackdrop()
        self.setCentralWidget(root)
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        sidebar = HarborSidebar()
        sidebar.setFixedWidth(190)
        navigation = QVBoxLayout(sidebar)
        navigation.setContentsMargins(12, 18, 12, 14)
        navigation.setSpacing(4)
        from .naval_widgets import OperationsBrand, fleet_window_icon
        app_icon = fleet_window_icon()
        QApplication.instance().setWindowIcon(app_icon)
        self.setWindowIcon(app_icon)
        brand = OperationsBrand()
        navigation.addWidget(brand)
        navigation.addSpacing(9)
        self.feature_index = FeatureIndex(sidebar)
        self.feature_index.requested.connect(self._navigate)
        self.nav_buttons = [self.feature_index.buttons[i] for i in sorted(self.feature_index.buttons)]
        navigation.addWidget(self.feature_index, 1)
        self.pages = QStackedWidget()
        self.settings_button = button("客户端设置", self.open_client_settings, "clientSettings")
        navigation.addWidget(self.settings_button)
        self._settings_dialog = None
        row.addWidget(sidebar)
        row.addWidget(self.pages, 1)
        self._build_library()
        self._build_replay()
        # Preserve the existing logical page IDs while retiring the legacy mapping UI.
        self.pages.addWidget(QWidget())
        self._build_reports()
        from .workbench_panel import WorkbenchPanel
        self.workbench = WorkbenchPanel(self)
        self.workbench.status.connect(self.statusBar().showMessage)
        self.workbench.idle.connect(self._check_close)
        self.pages.addWidget(self.workbench)
        from .report_studio_panel import ReportStudioPanel
        self.report_studio = ReportStudioPanel(self, self.workbench.ai_panel)
        self.report_studio.status.connect(self.statusBar().showMessage)
        self.report_studio.idle.connect(self._check_close)
        self.workbench.ai_panel.run_saved.connect(lambda _run: self.show_page(3))
        self._reports_layout.insertWidget(0, self.report_studio, 1)
        from .multiview_panel import MultiviewPanel
        self.multiview_panel = MultiviewPanel(self)
        self.multiview_panel.idle.connect(self._check_close)
        self.pages.addWidget(self.multiview_panel)
        from .statistics_panel import StatisticsPanel
        self.statistics_panel=StatisticsPanel(self)
        self.statistics_panel.idle.connect(self._check_close)
        self.multiview_panel.saved.connect(self.statistics_panel.refresh_scopes)
        self.pages.addWidget(self.statistics_panel)
        from .player_dossier_panel import PlayerDossierPanel
        self.dossier_panel=PlayerDossierPanel(self)
        self.dossier_panel.idle.connect(self._check_close)
        self.dossier_panel.status.connect(self.statusBar().showMessage)
        self.workbench.identity_panel.changed.connect(self.dossier_panel.refresh)
        self.pages.addWidget(self.dossier_panel)
        from .player_radar_panel import PlayerRadarPanel
        self.radar_panel=PlayerRadarPanel(self)
        self.radar_panel.idle.connect(self._check_close)
        self.radar_panel.status.connect(self.statusBar().showMessage)
        self.pages.addWidget(self.radar_panel)
        from .battle_review_panel import BattleReviewPanel
        from .query_center_panel import QueryCenterPanel
        self.battle_review_panel = BattleReviewPanel(self)
        self.query_center_panel = QueryCenterPanel(self)
        for panel in (self.battle_review_panel, self.query_center_panel):
            panel.idle.connect(self._check_close)
            self.pages.addWidget(panel)
        self.query_center_panel.status.connect(self.statusBar().showMessage)
        from .home_panel import HomePanel
        self.home_panel = HomePanel(self)
        self.home_panel.page_requested.connect(self._navigate)
        self.home_panel.import_requested.connect(self.choose_files)
        self.pages.addWidget(self.home_panel)
        for page,title,icon_key in ((self.workbench,'赛训工作室','clans'),
                (self.multiview_panel,'双边录像','replays'),(self.statistics_panel,'选手与阵容','clans'),
                (self.dossier_panel,'个人作战档案','profile')):
            install_page_header(page,title,icon_key)
        self.show_page(HOME_PAGE)

    def _navigate(self,index):
        self.ui_sound.play('navigate');self.show_page(index)

    def open_client_settings(self):
        if self._settings_dialog is not None:
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return
        from .client_settings import ClientSettingsDialog
        dialog = self._settings_dialog = ClientSettingsDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.finished.connect(lambda _result: setattr(self, '_settings_dialog', None))
        dialog.show()

    def open_player_profile(self,key=None,battle_ids=None):
        if key:self.dossier_panel.open_player(key,battle_ids=battle_ids)
        self.show_page(7)

    def open_replay_account(self, nickname, region=None):
        self.show_page(QUERY_PAGE)
        self.query_center_panel.set_search(nickname, region)

    def open_account_picker(self, context):
        self.query_center_panel.begin_account_picker(context)
        self.show_page(QUERY_PAGE)

    def return_account_picker(self, context, target):
        self.show_page(7)
        self.dossier_panel.accept_account_pick(context, target)

    def _page(self, title):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(26, 23, 26, 18)
        layout.setSpacing(12)
        icon_key={'录像库':'warehouse','地图复盘':'replays','基础报告':'warehouse'}.get(title,'clans')
        layout.addWidget(FleetPageHeader(title,icon_key))
        self.pages.addWidget(widget)
        return layout

    def _build_library(self):
        from .library_counter import LibraryCounter
        from .layered_panel import install_empty_table_art
        layout = self._page("录像库")
        cards = QHBoxLayout()
        self.metrics = []
        for title, icon in (("对局", 0), ("回放来源", 5), ("含结算对局", 3)):
            card = LibraryCounter(title, icon, primary=icon == 0)
            self.metrics.append(card.value)
            cards.addWidget(card, 1)
        layout.addLayout(cards)
        actions = QHBoxLayout()
        self.import_files_button = button("＋ 批量导入", self.choose_files, "primary")
        self.import_files_button.setAccessibleName("批量导入回放")
        self.import_folder_button = button("导入文件夹", self.choose_folder, "importFolder")
        actions.addWidget(self.import_files_button)
        actions.addWidget(self.import_folder_button)
        actions.addStretch()
        actions.addWidget(button("单局分析", self.open_selected_review, "reviewSelected"))
        actions.addWidget(button("地图复盘", self.open_selected, "openReplay"))
        more = QPushButton("更多操作")
        menu = QMenu(more)
        menu.addAction("双边导入 / 合并", self.prepare_selected_merge)
        self.refresh_analysis_button = menu.addAction("更新选中回放", self.refresh_selected_analysis)
        menu.addAction("修改分类 / 备注", self.edit_annotation)
        menu.addAction("导出选中对局", self.export_selected)
        more.setMenu(menu)
        actions.addWidget(more)
        layout.addLayout(actions)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setObjectName("librarySearch")
        self.search.setPlaceholderText("搜索选手、战队、日期、赛事或备注")
        self.search.textChanged.connect(self.filter_library)
        self.map_filter = QComboBox()
        self.map_filter.setObjectName("mapFilter")
        self.map_filter.currentIndexChanged.connect(self.filter_library)
        self.event_filter = QComboBox()
        self.event_filter.setObjectName("eventFilter")
        self.event_filter.currentIndexChanged.connect(self.filter_library)
        self.mode_filter = QComboBox()
        self.mode_filter.setAccessibleName("战斗模式")
        self.mode_filter.currentIndexChanged.connect(self.filter_library)
        filters.addWidget(self.search, 3)
        filters.addWidget(self.map_filter, 1)
        filters.addWidget(self.event_filter, 1)
        filters.addWidget(self.mode_filter, 1)
        layout.addLayout(filters)
        self.library_table = table(["日期 / 时间", "地图", "模式", "赛事", "分类", "结果", "人数", "来源", "舰船", "录制账号", "资料状态"], "libraryTable")
        self.library_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.library_table.itemDoubleClicked.connect(lambda _: self.open_selected_review())
        for col, width in enumerate((140, 100, 78, 78, 70, 50, 50, 50, 115, 175, 130)):
            self.library_table.setColumnWidth(col, width)
        layout.addWidget(self.library_table,1)
        install_empty_table_art(self.library_table)
        queue = QWidget()
        qlayout = QVBoxLayout(queue)
        qlayout.setContentsMargins(0, 4, 0, 0)
        qhead = QHBoxLayout()
        self.queue_label = QLabel("暂无导入任务")
        qhead.addWidget(self.queue_label, 1)
        self.cancel_import_button = button("取消导入", self.cancel_import, "cancelImport")
        self.cancel_import_button.setEnabled(False)
        qhead.addWidget(self.cancel_import_button)
        qlayout.addLayout(qhead)
        self.queue_table = table(["回放文件", "状态", "说明"], "importQueue")
        self.queue_table.setColumnWidth(0, 440)
        self.queue_table.setColumnWidth(1, 85)
        qlayout.addWidget(self.queue_table)
        self.import_progress = QProgressBar()
        self.import_progress.setRange(0, 1)
        self.import_progress.setValue(0)
        self.import_progress.setTextVisible(False)
        qlayout.addWidget(self.import_progress)
        self.queue_table.setMaximumHeight(160)
        self.queue_disclosure=Disclosure('导入进度',queue)
        layout.addWidget(self.queue_disclosure)

    def _build_replay(self):
        layout = self._page("地图复盘")
        layout.parentWidget().setProperty('suppressPageTransition', True)
        top = QHBoxLayout()
        self.replay_title = QLabel("未打开录像")
        self.replay_title.setWordWrap(True)
        top.addWidget(self.replay_title, 1)
        top.addWidget(QLabel("录制来源"))
        self.source_combo = QComboBox()
        self.source_combo.setObjectName("replaySource")
        self.source_combo.setMinimumContentsLength(12)
        self.source_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.source_combo.currentIndexChanged.connect(self.load_source)
        top.addWidget(self.source_combo)
        top.addWidget(button("单局分析", lambda: self.show_page(REVIEW_PAGE)))
        top.addWidget(button("更新本局", self.refresh_current_analysis, "refreshCurrentAnalysis"))
        top.addWidget(button("导出本局", self.export_current, "exportCurrent"))
        layout.addLayout(top)
        self.view_notice = QLabel()
        self.view_notice.hide()
        self.view_notice.setObjectName("notice")
        self.view_notice.setWordWrap(True)
        layout.addWidget(self.view_notice)
        self.replay_panel = ReplayPanel(asset_bundle=self.bundle)
        apply_native_controls(self.replay_panel)
        layout.addWidget(self.replay_panel, 1)

    def _build_reports(self):
        page = QWidget()
        layout = self._reports_layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.pages.addWidget(page)
        exports = QWidget()
        export_layout = QVBoxLayout(exports)
        export_layout.setContentsMargins(0, 0, 0, 0)
        actions = QHBoxLayout()
        self.single_export_button = button("导出当前复盘", self.export_current, "reportSingle")
        self.summary_export_button = button("导出比赛库选中项", self.export_selected, "reportSummary")
        actions.addWidget(self.single_export_button)
        actions.addWidget(self.summary_export_button)
        actions.addWidget(button('选手与阵容统计', lambda: self.show_page(6), 'reportStatistics'))
        export_layout.addLayout(actions)
        self.export_status = QLabel("尚未导出报告")
        self.export_status.setWordWrap(True)
        export_layout.addWidget(self.export_status)
        self.export_links = QTextBrowser()
        self.export_links.setOpenLinks(False)
        self.export_links.anchorClicked.connect(QDesktopServices.openUrl)
        self.export_links.setMaximumHeight(90)
        export_layout.addWidget(self.export_links)
        self.data_export_disclosure = Disclosure('数据导出', exports)
        self.data_export_disclosure.setContentsMargins(22, 0, 22, 10)
        layout.addWidget(self.data_export_disclosure)

    def open_report_studio(self, run=None):
        if run is not None:
            self.report_studio.show_run(run)
        self.show_page(3)

    def open_battle_report(self, analysis):
        if not self.current_sha or not analysis.get('evidence'):
            self.statusBar().showMessage('当前录像尚无可用于报告的记录。')
            return
        from wows_training.workbench_service import digest
        if self.current_recording_id:
            snapshot = {'merged_recordings': [{'id': self.current_recording_id, 'battle_id': self.current_battle}]}
        else:
            snapshot = {'inputs': [{'battle_id': self.current_battle, 'views': [
                {'sha256': self.current_sha, 'revision': self.current_analysis_revision}]}]}
        analysis = {**analysis, 'input_snapshot': snapshot}
        fingerprint = {'source': self.current_sha, 'recording': self.current_recording_id, 'content': digest(analysis)}
        store = self.workbench.store
        run = next((row for row in reversed(store.list_runs('single_replay'))
                    if row.get('input_fingerprint') == fingerprint), None)
        if run is None:
            metadata = self.current_report.get('metadata') or {}
            name = f"{self.current_report.get('display_map_name') or '单局复盘'} · {date_text(metadata.get('dateTime'))}"
            run = store.save_run('single_replay', name, analysis, input_fingerprint=fingerprint)
        self.show_page(3)
        self.report_studio.select_single_source(run['id'])

    def show_page(self, index):
        if index == 2:
            index = QUERY_PAGE
        background = getattr(self,"client_preferences",{}).get("background_enabled",True)
        self.backdrop.set_scene_enabled(background and index != 1)
        if index != 8 and hasattr(self,'radar_panel'):
            self.radar_panel.pause()
        self.motion.switch_page(self.pages, index)
        self.feature_index.select(index)
        if index == 3 and hasattr(self, "report_studio"):
            self.report_studio.refresh_sources()
        elif index == 4 and hasattr(self, "workbench"):
            self.workbench.refresh_documents()
            self.workbench.identity_panel.refresh()
        elif index == 7 and hasattr(self,'dossier_panel'):
            self.dossier_panel.refresh()
        elif index == 8 and hasattr(self,'radar_panel'):
            self.radar_panel.refresh()

    def refresh_library(self):
        self._battles = self.library.list_battles()
        for combo, pairs, title in (
            (self.map_filter, sorted({(row["map_id"], self.map_names.get(row["map_id"], row["map_id"])) for row in self._battles}), "全部地图"),
            (self.event_filter, sorted({(row["event_key"], "未指定赛事" if row["event_key"] == "unassigned" else row["event_key"]) for row in self._battles}), "全部赛事"),
            (self.mode_filter, sorted({(row['battle_mode'], row['battle_mode']) for row in self._battles if row.get('battle_mode')}), "全部模式"),
        ):
            previous = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(title, "")
            for key, label in pairs:
                combo.addItem(label, key)
            combo.setCurrentIndex(max(0, combo.findData(previous)))
            combo.blockSignals(False)
        self.metrics[0].setText(str(len(self._battles)))
        self.metrics[1].setText(str(sum(row["replay_count"] for row in self._battles)))
        self.metrics[2].setText(str(sum(row.get("result_duration") is not None for row in self._battles)))
        self.filter_library()

    def filter_library(self):
        selected = set(self.selected_battles())
        query = self.search.text().strip()
        rows = self.library.list_battles(search=query, map_id=self.map_filter.currentData() or "", event_key=self.event_filter.currentData() or "")
        if query and not rows:
            # Localized map labels are presentation data, not replay metadata.
            rows = [item for item in self._battles if query.casefold() in self.map_names.get(item["map_id"], "").casefold()
                    and (not self.map_filter.currentData() or self.map_filter.currentData() == item["map_id"])
                    and (not self.event_filter.currentData() or self.event_filter.currentData() == item["event_key"])]
        if self.mode_filter.currentData():
            rows = [item for item in rows if item.get('battle_mode') == self.mode_filter.currentData()]
        rows.sort(key=lambda item: date_text(item["date_raw"]), reverse=True)
        self.library_table.setRowCount(len(rows))
        for index, item in enumerate(rows):
            fill_row(self.library_table, index, [date_text(item["date_raw"]), self.map_names.get(item["map_id"], item["map_id"]),
                item.get("battle_mode") or "未知模式", item["event_key"] if item["event_key"] != "unassigned" else "—", item["label"],
                {"win": "胜", "loss": "负", "draw": "平", "unknown": "待定"}.get(item["outcome"], "待定"),
                item["participant_count"], item["replay_count"], self._library_ship_name(item), item["recorder"], self.quality_text(item)], item["id"])
            if item["id"] in selected:
                for column in range(self.library_table.columnCount()):
                    self.library_table.item(index, column).setSelected(True)

    def _library_ship_name(self, item):
        vehicle = item.get('recorder_vehicle') or ''
        code = vehicle.replace('_', '-').partition('-')[0].upper()
        ship = self.catalog.get(item.get('recorder_ship_id')) or self._ships_by_index.get(code, {})
        return (ship.get('display_name') or ship.get('name')
                or (ship.get('internal_name') or vehicle).replace('_', '-').partition('-')[-1].replace('-', ' ')
                or '—')

    @staticmethod
    def quality_text(item):
        if item['quality'] == 'header_only':
            return '阵容资料'
        if item['quality'] == 'result_verified':
            return '结算与观测'
        if item['quality'] == 'observations_verified':
            return '局中观测'
        if item["quality"] in ("failed", "error"):
            return "需复核"
        if item.get("observed_duration") is not None and item.get("result_duration") is not None and item["result_duration"] - item["observed_duration"] > 2:
            return "基础可用 · 末段缺失"
        return "基础可用"

    def selected_battles(self):
        return [self.library_table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
                for index in self.library_table.selectionModel().selectedRows()]

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择回放文件", "", "WOWS 回放 (*.wowsreplay)")
        if paths:
            self.start_import(paths)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择回放文件夹（包含子文件夹）")
        if folder:
            self.start_import(sorted(Path(folder).rglob("*.wowsreplay")))

    def start_import(self, paths, *, refresh=False, background=False):
        if hasattr(self,'radar_panel') and self.radar_panel.busy:
            self.statusBar().showMessage('八维图账号仍在查询，请完成或取消后再导入。');return False
        if hasattr(self,'dossier_panel') and self.dossier_panel.busy:
            self.statusBar().showMessage('个人档案仍在处理，请完成或取消后再导入。');return False
        if hasattr(self,'statistics_panel') and self.statistics_panel.busy:
            self.statusBar().showMessage('请先完成或取消当前统计。');return False
        if hasattr(self, 'multiview_panel') and self.multiview_panel.busy:
            self.statusBar().showMessage('双边录像仍在处理，请完成或取消后再导入。')
            return False
        if self.import_worker and self.import_worker.isRunning():
            self.statusBar().showMessage("当前批次仍在处理，请完成或取消后再导入。")
            return False
        paths = list(dict.fromkeys(str(Path(path).resolve()) for path in paths))
        if not paths:
            self.queue_label.setText("所选位置没有 .wowsreplay 文件")
            return False
        self.queue_table.setRowCount(len(paths))
        for row, path in enumerate(paths):
            fill_row(self.queue_table, row, [Path(path).name, "等待", ""])
        self.import_progress.setRange(0, len(paths))
        self.import_progress.setValue(0)
        self.queue_label.setText(f"导入队列 · {len(paths)} 份回放")
        self.queue_disclosure.setExpanded(True)
        self.cancel_import_button.setEnabled(True)
        self.import_files_button.setEnabled(False)
        self.import_folder_button.setEnabled(False)
        self.refresh_analysis_button.setEnabled(False)
        self.import_worker = ImportWorker(paths, self.library.root, self.bundle.root, self, refresh=refresh)
        self.import_worker.item_started.connect(lambda index, path: self.queue_table.item(index, 1).setText("解析中"))
        self.import_worker.item_finished.connect(self._import_item)
        self.import_worker.batch_finished.connect(self._import_done)
        self.import_worker.finished.connect(self._check_close)
        self.import_worker.start()
        if not background:
            self.show_page(0)
        return True

    def _import_item(self, index, status, message, battle_id):
        self.queue_table.item(index, 1).setText(status)
        summary={'已导入':'已加入比赛库','已更新':'复盘已更新','重复':'比赛库中已存在','已取消':'已取消'}.get(status,str(message))
        self.queue_table.item(index, 2).setText(summary)
        self.queue_table.item(index, 2).setToolTip(summary)
        self.import_progress.setValue(index + 1)
        self.queue_table.scrollToItem(self.queue_table.item(index, 0))
        if battle_id:
            self.refresh_library()

    def _import_done(self, totals):
        self.queue_label.setText(f"导入完成 · 新增 {totals['imported']} / 更新 {totals.get('updated', 0)} / 重复 {totals['duplicate']} / 失败 {totals['failed']} / 取消 {totals['cancelled']}")
        self.import_progress.setValue(self.import_progress.maximum())
        for row in range(self.queue_table.rowCount()):
            if self.queue_table.item(row, 1).text() == "等待":
                self.queue_table.item(row, 1).setText("已取消")
        self.cancel_import_button.setEnabled(False)
        self.import_files_button.setEnabled(True)
        self.import_folder_button.setEnabled(True)
        self.refresh_analysis_button.setEnabled(True)
        self.refresh_library()
        if totals.get("updated") and self.current_sha:
            self.refresh_open_sources()
        elif (self.current_sha in self._combat_refresh_attempts and not getattr(self.intel, 'available', False)
              and (totals.get('failed') or totals.get('cancelled'))):
            self.view_notice.setText('战斗图层尚未更新，可用「更新本局」重试。')

    def refresh_open_sources(self):
        if not self.current_battle:
            return
        token = self.source_combo.currentData()
        clock = self.replay_panel.current_clock
        page = self.pages.currentIndex()
        self.open_battle(self.current_battle, page=page, source_token=token)
        if self.source_combo.currentData() == token:
            self.replay_panel.set_clock(clock)

    def _refresh_battles(self, battle_ids):
        paths = []
        for battle_id in battle_ids:
            for replay in self.library.list_replays(battle_id):
                archived = self.library.root / 'raw_replays' / (replay['sha256'] + '.wowsreplay')
                candidates = [str(archived), *(source["path"] for source in replay["sources"])]
                path = next((value for value in candidates if Path(value).is_file()), replay["source_path"])
                paths.append(path)
        if paths:
            self.start_import(paths, refresh=True)
        else:
            self.statusBar().showMessage("未选中待更新录像")

    def refresh_selected_analysis(self):
        self._refresh_battles(self.selected_battles())

    def refresh_current_analysis(self):
        self._refresh_battles([self.current_battle] if self.current_battle else [])

    def cancel_import(self):
        if self.import_worker and self.import_worker.isRunning():
            self.import_worker.requestInterruption()
            self.cancel_import_button.setEnabled(False)
            self.queue_label.setText("正在停止当前解析；已导入的比赛保留。")

    def open_selected(self):
        rows = self.selected_battles()
        if rows:
            self.open_battle(rows[0])
        else:
            self.statusBar().showMessage("未选中录像")

    def open_selected_review(self):
        selected = self.selected_battles()
        if selected:
            self.open_battle(selected[0], page=REVIEW_PAGE)
        else:
            self.statusBar().showMessage("未选中录像")

    def current_replay_path(self):
        if not self.current_sha or self.current_recording_id:
            return None
        archived = self.library.root / 'raw_replays' / (self.current_sha + '.wowsreplay')
        if archived.is_file():
            return archived
        record = self.library.find_replay(self.current_sha)
        if record:
            for source in record.get('sources', []):
                path = Path(source['path'])
                if path.is_file():
                    return path
        return None

    def open_battle(self, battle_id, *, page=1, source_token=None):
        try:
            record = self.library.get_battle(battle_id)
            self.current_battle = battle_id
            self.source_combo.blockSignals(True)
            self.source_combo.clear()
            self._merged_sources = {}
            from wows_training.multiview_selection import current_recording
            current = current_recording(self.library, self.recording_store, battle_id)
            saved = self.recording_store.list_recordings(battle_id)
            saved.sort(key=lambda r: r['id'] != (current or {}).get('id'))
            for recording in saved:
                token = 'recording:' + recording['id']
                self._merged_sources[token] = recording['id']
                label = '双边合并 · 当前来源' if current and current['id'] == recording['id'] else '双边合并 · 历史保存版本'
                self.source_combo.addItem(f'{label} · {recording["source_count"]} 份录制', token)
            for replay in record["replays"]:
                self.source_combo.addItem(f"{replay['recorder']} · {replay['source_name']}", replay["sha256"])
            preferred = self.source_combo.findData(source_token) if source_token else -1
            if preferred >= 0:
                self.source_combo.setCurrentIndex(preferred)
            elif not current and saved:
                self.source_combo.setCurrentIndex(len(saved))
            self.source_combo.blockSignals(False)
            self.source_combo.setEnabled(self.source_combo.count() > 1)
            self.load_source()
            self.show_page(page)
        except Exception as exc:
            self.statusBar().showMessage(f"无法打开比赛：{exc}")

    def load_source(self, *_):
        self.battle_review_panel.sync_recording_sources(self.source_combo)
        sha = self.source_combo.currentData()
        if not sha:
            return
        try:
            self.current_recording_id = self._merged_sources.get(sha)
            self.current_analysis_revision = None
            if self.current_recording_id:
                report = self.recording_store.load_report(self.current_recording_id)
                annotation = self.library.get_battle(self.current_battle)
                report['battle_annotation'] = {key: annotation.get(key) for key in ('event_key', 'label', 'notes')}
                report['event_key'] = annotation['event_key']
            else:
                self.current_analysis_revision = self.library.find_replay(sha)['analysis_revision']
                report = self.library.get_report(sha, self.current_analysis_revision)
            report = self.bundle.presentation_report(report)
            self.replay_panel.set_report(report, {"ships": self.catalog})
            self.current_report, self.current_sha = report, report['sha256']
            self.intel = self.replay_panel.intel
            metadata = report.get("metadata") or {}
            from wows_training.ordinary_replay import battle_mode_label
            self.replay_title.setText(f"{report.get('display_map_name', '')}  /  {date_text(metadata.get('dateTime', ''))}  /  {battle_mode_label(metadata)}")
            self.battle_review_panel.set_report(report)
            self.view_notice.show()
            enhanced = getattr(self.intel, "available", False)
            if report.get('multiview'):
                sources = report['multiview'].get('source_shas', [])
                from wows_training.multiview_selection import current_recording
                current = current_recording(self.library, self.recording_store, self.current_battle)
                historical = '历史保存版本（来源已有变化） · ' if not current or current['id'] != self.current_recording_id else ''
                self.view_notice.setText(f'{historical}双边合并 · {len(sources)} 份来源 · 全局观测 / 比赛已进行时间')
            else:
                self.view_notice.setText('单视角 · 记录计时' if enhanced else self.quality_text(report))
            first = ((report.get("time_analysis") or {}).get("coverage") or {}).get("first_started_packet_clock")
            if isinstance(first, (int, float)):
                self.replay_panel.set_clock(first)
            if hasattr(self, "workbench"):
                self.workbench.identity_panel.refresh()
            from ..ordinary_replay import CURRENT, needs_performance_refresh
            from ..replay_loadout import replay_loadout_pending
            from ..replay_battle_state import needs_battle_state_refresh
            if (not self.current_recording_id and sha not in self._combat_refresh_attempts
                    and (needs_battle_state_refresh(report) or needs_performance_refresh(report) or replay_loadout_pending(report)
                         or (report.get('replay_version') == CURRENT and not enhanced))):
                QTimer.singleShot(0, self, lambda: self._upgrade_combat_layers(sha))
        except Exception as exc:
            self.current_report = self.current_sha = self.current_recording_id = None
            self.intel = None
            self.replay_panel.clear()
            self.battle_review_panel.set_report(None)
            self.view_notice.show()
            self.view_notice.setText(f"该来源无法显示：{exc}")
            if self.pages.currentIndex() == REVIEW_PAGE:
                self.statusBar().showMessage(f"该来源无法显示：{exc}")

    def _upgrade_combat_layers(self, sha):
        if self.current_sha != sha or sha in self._combat_refresh_attempts:
            return
        path = self.current_replay_path()
        if path is None:
            self.view_notice.setText('缺少原始录像，请重新选择原文件更新战斗图层。')
            return
        if self.start_import([path], refresh=True, background=True):
            self._combat_refresh_attempts.add(sha)
            self.view_notice.setText('正在从原始录像补全复盘数据…')

    def prepare_selected_merge(self):
        paths = []
        for battle_id in self.selected_battles():
            for source in self.library.list_replays(battle_id):
                paths.append(next((s['path'] for s in source['sources'] if Path(s['path']).is_file()), source['source_path']))
        if paths:
            self.multiview_panel.set_inputs(paths)
        self.show_page(5)

    def open_recording(self, recording_id):
        record = self.recording_store.get(recording_id)
        self.open_battle(record['battle_id'])
        index = self.source_combo.findData('recording:' + recording_id)
        if index >= 0:
            self.source_combo.setCurrentIndex(index)
            self.load_source()
            self.show_page(1)

    def edit_annotation(self):
        selected = self.selected_battles()
        if not selected:
            self.statusBar().showMessage("请先选择一场比赛。")
            return
        record = self.library.get_battle(selected[0])
        dialog = QDialog(self)
        dialog.setWindowTitle("比赛分类与备注")
        dialog.resize(480, 320)
        form = QFormLayout(dialog)
        event = QLineEdit(record["event_key"])
        label = QComboBox()
        label.setEditable(True)
        label.addItems(["未分类", "正式比赛", "训练", "重开", "其他"])
        label.setCurrentText(record["label"])
        notes = QPlainTextEdit(record["notes"])
        form.addRow("赛事", event)
        form.addRow("分类", label)
        form.addRow("备注", notes)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.library.set_battle_annotation(selected[0], event.text().strip(), label.currentText().strip(), notes.toPlainText())
            self.refresh_library()
            if self.current_battle == selected[0]:
                self.load_source()

    def export_current(self):
        if not self.current_sha:
            self.export_status.setText("未打开录像")
            self.show_page(3)
            return
        folder = QFileDialog.getExistingDirectory(self, "选择单局报告保存目录", str(self.library.root))
        if folder:
            self.start_export([self.current_sha], folder, clock=self.replay_panel.current_clock, recording_id=self.current_recording_id)

    def export_selected(self):
        selected = self.selected_battles()
        if not selected:
            self.export_status.setText("未选中待导出的录像")
            self.show_page(3)
            return
        folder = QFileDialog.getExistingDirectory(self, "选择汇总报告保存目录", str(self.library.root))
        if folder:
            # Include every source in the report; reporting handles arena deduplication.
            shas = [source["sha256"] for battle_id in selected for source in self.library.list_replays(battle_id)]
            self.start_export(shas, folder, summary=True)

    def start_export(self, shas, output, summary=False, clock=None, recording_id=None):
        if self.export_worker and self.export_worker.isRunning():
            self.statusBar().showMessage("报告仍在生成，请稍候。")
            return False
        self.show_page(3)
        self.export_status.setText("正在生成报告…")
        self.single_export_button.setEnabled(False)
        self.summary_export_button.setEnabled(False)
        self.export_worker = ExportWorker(self.library.root, self.bundle.root, shas, output, summary, clock, self, recording_id=recording_id)
        self.export_worker.succeeded.connect(self._export_done)
        self.export_worker.failed.connect(self._export_failed)
        self.export_worker.finished.connect(self._check_close)
        self.export_worker.start()
        return True

    def _export_done(self, paths):
        import html
        self.last_export = paths
        self.export_status.setText("报告已保存")
        self.export_links.setHtml("<br><br>".join(f'<a style="color:#125AB8" href="{QUrl.fromLocalFile(str(path)).toString()}">{html.escape(str(kind).upper())} · {html.escape(Path(path).name)}</a>' for kind, path in paths.items()))
        self.single_export_button.setEnabled(True)
        self.summary_export_button.setEnabled(True)

    def _export_failed(self, error):
        self.export_status.setText(f"导出失败：{error}")
        self.single_export_button.setEnabled(True)
        self.summary_export_button.setEnabled(True)

    @staticmethod
    def open_path(path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = []
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            paths.extend(sorted(path.rglob("*.wowsreplay")) if path.is_dir() else [path])
        if any(Path(p).suffix.lower() in ('.rar', '.zip', '.wowsbattle') for p in paths):
            self.multiview_panel.set_inputs(paths)
            self.show_page(5)
            self.multiview_panel.start_import()
        else:
            self.start_import(paths)
        event.acceptProposedAction()

    def closeEvent(self, event):
        if not self._closing and hasattr(self,'radar_panel') and not self.radar_panel.can_close():
            event.ignore();return
        if (any(worker and worker.isRunning() for worker in (self.import_worker, self.export_worker))
                or (hasattr(self, "workbench") and self.workbench.busy)
                or (hasattr(self, 'multiview_panel') and self.multiview_panel.busy)
                or (hasattr(self,'statistics_panel') and self.statistics_panel.busy)
                or (hasattr(self,'dossier_panel') and self.dossier_panel.busy)
                or self.query_center_panel.busy or self.battle_review_panel.busy or self.updates.busy
                or (hasattr(self,'radar_panel') and self.radar_panel.busy)):
            self._closing = True
            self.cancel_import()
            if hasattr(self, "workbench"):
                self.workbench.cancel()
            if hasattr(self, 'multiview_panel'):
                self.multiview_panel.cancel()
            if hasattr(self,'statistics_panel'):
                self.statistics_panel.cancel()
            if hasattr(self,'dossier_panel'):
                self.dossier_panel.cancel()
            if hasattr(self,'radar_panel'):
                self.radar_panel.cancel()
            self.query_center_panel.cancel()
            self.battle_review_panel.cancel()
            self.updates.cancel()
            self.statusBar().showMessage("正在完成写入并关闭…")
            event.ignore()
        else:
            if hasattr(self,'radar_panel'):
                self.radar_panel.pause()
            self.replay_panel.clear()
            event.accept()

    def _check_close(self):
        if self._closing:
            QTimer.singleShot(0, self.close)
