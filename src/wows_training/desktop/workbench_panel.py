"""P2/P3 workflows: saved cohorts, deterministic reports, training and AI."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import threading
from uuid import uuid4

from PySide6.QtCore import Qt, QThread, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QLineEdit, QCheckBox, QDoubleSpinBox, QSpinBox, QFormLayout, QTabWidget, QTextBrowser,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QSplitter, QFileDialog,
    QScrollArea, QGroupBox, QStackedWidget, QHeaderView, QMenu, QButtonGroup)
from wows_training.workbench_store import WorkbenchStore
from wows_training.workbench_service import AnalysisWorkspace, digest, attach_identities, compact_analysis_input
from wows_training.workbench_reporting import render_run, export_run
from .zone_editor import ZoneCanvas
from .ui_components import Disclosure, StatusLabel
from .workbench_visuals import CollectionDashboard, compact_run_html


_SCOPE_ALL = 'scope:all'
_SCOPE_SELECTED = 'scope:selection'


class Job(QThread):
    progress = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)
    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function, self.cancel_event = function, threading.Event()
    def run(self):
        try:
            result = self.function(self.cancel_event, self.progress.emit)
            if self.cancel_event.is_set():
                raise InterruptedError("操作已取消")
            self.succeeded.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


def btn(text, function, name=""):
    item = QPushButton(text)
    item.setObjectName(name)
    def invoke():
        try:
            function()
        except Exception as exc:
            owner=item.parentWidget()
            while owner is not None and not hasattr(owner,"notice"):
                owner=owner.parentWidget()
            if owner is not None:
                owner.notice.setText("未完成："+str(exc))
    item.clicked.connect(invoke)
    return item


def spin(value, maximum=3600, decimals=1, minimum=0):
    item = QDoubleSpinBox()
    item.setRange(minimum, maximum)
    item.setDecimals(decimals)
    item.setValue(value)
    return item


def grid(headers, name):
    item = QTableWidget(0, len(headers))
    item.setObjectName(name)
    item.setHorizontalHeaderLabels(headers)
    item.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    item.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    item.setAlternatingRowColors(True)
    item.setShowGrid(False)
    item.setWordWrap(False)
    item.verticalHeader().setDefaultSectionSize(38)
    item.verticalHeader().hide()
    item.horizontalHeader().setStretchLastSection(True)
    item.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return item


def rows(table, values):
    table.setRowCount(len(values))
    for r, row in enumerate(values):
        for c, value in enumerate(row):
            item = QTableWidgetItem(str(value if value is not None else "未知"))
            item.setToolTip(item.text())
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(r,c,item)


class WorkbenchPanel(QWidget):
    status = Signal(str)
    idle = Signal()
    def __init__(self, host):
        super().__init__(host)
        self.host, self.library, self.bundle = host, host.library, host.bundle
        self.store = WorkbenchStore(self.library.root)
        self.workspace = AnalysisWorkspace(self.library.root, self.bundle)
        self.job = None
        self.current_run = self.analysis_run = self.counter_run = None
        self.battle_ids = []
        self.plan_targets = []
        self._build()
        self.refresh_documents()
        self.load_own()
        self.use_all()

    @property
    def busy(self):
        return bool(self.job and self.job.isRunning()) or self.identity_panel.busy or self.ai_panel.busy

    def cancel(self):
        if self.job and self.job.isRunning():
            self.job.cancel_event.set()
        self.identity_panel.cancel()
        self.ai_panel.cancel()

    def _start(self, function, done):
        if self.busy:
            self.notice.setText("当前有任务正在运行")
            return
        self.job = Job(function, self)
        self.job.progress.connect(self.notice.setText)
        self.job.failed.connect(lambda message: self.notice.setText("未完成：" + message))
        self.job.succeeded.connect(done)
        self.job.finished.connect(self._finish_job)
        self.notice.setText("正在处理…")
        self.cancel_workbench_button.show()
        self.run_collection_button.setEnabled(False)
        self.job.start()

    def _finish_job(self):
        if self.sender() is self.job:
            self.cancel_workbench_button.hide()
            self.run_collection_button.setEnabled(True)
        self.idle.emit()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24,18,24,14)
        title = QLabel("赛训工作室")
        title.setObjectName("title")
        layout.addWidget(title)
        self.notice = StatusLabel()
        self.notice.setObjectName("notice")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("workbenchTabs")
        self._tool_pages = {}
        self.tools_button = QPushButton("更多工具")
        self.tools_button.setObjectName("workbenchTools")
        self.tools_menu = QMenu(self.tools_button)
        self.tools_button.setMenu(self.tools_menu)
        self.cancel_workbench_button = btn("取消任务", self.cancel, "cancelWorkbench")
        self.cancel_workbench_button.hide()
        corner = QWidget()
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(4, 0, 0, 0)
        corner_layout.addWidget(self.cancel_workbench_button)
        corner_layout.addWidget(self.tools_button)
        self.tabs.setCornerWidget(corner, Qt.Corner.TopRightCorner)
        layout.addWidget(self.tabs,1)
        self._build_collection()
        self._build_results()
        self._build_regions()
        self._build_training()
        from .identity_panel import IdentityPanel
        self.identity_panel = IdentityPanel(self.library,self.store,self.bundle,lambda:self.host.current_report,self)
        self.identity_panel.status.connect(self.notice.setText)
        self.identity_panel.idle.connect(self.idle)
        self.identity_panel.changed.connect(self._configuration_changed)
        self._add_tool("identity", self.identity_panel, "身份与战绩")
        self.tabs.currentChanged.connect(self._hide_inactive_tools)
        from .ai_panel import AIPanel
        self.ai_panel = AIPanel(self.store,self.bundle,self.analysis_payload,self.ai_plan_payload,self.own_payload,self)
        self.ai_panel.include_plan.setText("加入计划、方案与教练批注")
        self.ai_panel.status.connect(self.notice.setText)
        self.ai_panel.idle.connect(self.idle)
        # The main window places the shared AI panel in the report studio.
        self.ai_panel.hide()

    def _tab(self, name, scroll=False, *, tool=None):
        widget=QWidget()
        layout=QVBoxLayout(widget)
        layout.setContentsMargins(12,12,12,10)
        page = widget
        if scroll:
            page = QScrollArea()
            page.setWidgetResizable(True)
            page.setWidget(widget)
        if tool is None:
            self.tabs.addTab(page, name)
        else:
            self._add_tool(tool, page, name)
        return layout

    def _add_tool(self, key, page, name):
        self._tool_pages[key] = page
        self.tabs.setTabVisible(self.tabs.addTab(page, name), False)
        action = self.tools_menu.addAction(name)
        action.triggered.connect(lambda _checked=False, k=key: self.show_tool(k))

    def show_tool(self, key):
        page = self._tool_pages[key]
        self.tabs.setTabVisible(self.tabs.indexOf(page), True)
        self.tabs.setCurrentWidget(page)

    def _hide_inactive_tools(self, selected):
        for page in self._tool_pages.values():
            index = self.tabs.indexOf(page)
            if index != selected and self.tabs.isTabVisible(index):
                self.tabs.setTabVisible(index, False)

    def _build_collection(self):
        from .layered_panel import LayeredPanel, fold_content_inset
        from .ui_components import Disclosure

        layout = self._tab("跨局分析", scroll=True)
        layout.setSpacing(12)
        card = LayeredPanel()
        card.setObjectName("workbenchQuickAnalysis")
        quick = QVBoxLayout(card)
        quick.setContentsMargins(20, 16, fold_content_inset(card.cut, 16), 18)
        quick.setSpacing(10)
        settings = QHBoxLayout()
        settings.setSpacing(10)
        self.collection_combo = QComboBox()
        self.collection_combo.setObjectName("collectionCombo")
        self.collection_combo.setAccessibleName("比赛范围")
        self.collection_combo.setMinimumContentsLength(12)
        self.collection_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.collection_combo.currentIndexChanged.connect(self.load_collection)
        self.collection_combo.activated.connect(self._scope_activated)
        settings.addWidget(QLabel("比赛范围"))
        settings.addWidget(self.collection_combo, 3)
        self.team = QComboBox()
        self.team.setEditable(True)
        self.team.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.team.addItems(["每局录制方", "BYL", "FND", "ARIS", "绝对队伍 0", "绝对队伍 1"])
        self.team.setObjectName("analysisTeam")
        self.team.setAccessibleName("分析队伍")
        settings.addWidget(QLabel("分析队伍"))
        settings.addWidget(self.team, 2)
        quick.addLayout(settings)

        summary_row = QHBoxLayout()
        self.collection_summary = QLabel()
        self.collection_summary.setObjectName("subtitle")
        self.collection_summary.setWordWrap(True)
        summary_row.addWidget(self.collection_summary, 1)
        self.run_collection_button = btn("运行分析", self.run_collection, "runCollection")
        self.run_collection_button.setStyleSheet('font-weight:700; color:#174D9B;')
        summary_row.addWidget(self.run_collection_button)
        quick.addLayout(summary_row)
        layout.addWidget(card)

        save_body = QWidget()
        save = QHBoxLayout(save_body)
        save.setContentsMargins(10, 0, 12, 6)
        self.collection_name = QLineEdit()
        self.collection_name.setPlaceholderText("集合名称")
        self.collection_name.setObjectName("collectionName")
        save.addWidget(self.collection_name, 1)
        save.addWidget(btn("保存为新集合", self.save_collection, "saveCollection"))
        self.save_collection_options = Disclosure("保存集合", save_body)
        self.save_collection_options.setObjectName("workbenchSaveCollection")
        layout.addWidget(self.save_collection_options)

        rule_body = QWidget()
        form = QFormLayout(rule_body)
        form.setContentsMargins(10, 0, 12, 6)
        form.setSpacing(8)
        self.only_formal=QCheckBox("仅正式比赛")
        self.only_formal.setToolTip("纳入已手工分类为“正式比赛”的场次")
        form.addRow(self.only_formal)
        self.rule_combo=QComboBox()
        self.rule_combo.currentIndexChanged.connect(self.load_rules)
        form.addRow("已保存规则",self.rule_combo)
        self.rules_name=QLineEdit("开局 180 秒")
        form.addRow("规则名称",self.rules_name)
        self.time_basis=QComboBox()
        self.time_basis.addItem("开局计时","battle_elapsed")
        self.time_basis.addItem("录像计时","packet_clock")
        form.addRow("时间基准",self.time_basis)
        times=QHBoxLayout()
        self.start_sec,self.end_sec=spin(0),spin(180)
        times.addWidget(self.start_sec);times.addWidget(QLabel("至"));times.addWidget(self.end_sec)
        form.addRow("分析窗口 / 秒",times)
        self.coverage=spin(.8,1,2,.05)
        self.gap=spin(3,3,1,.1)
        self.rally=spin(3000,50000,0,1)
        form.addRow("最低有效观测比例",self.coverage)
        form.addRow("最长连续观测间隔 / 秒",self.gap)
        form.addRow("集结距离规则 / 显示米",self.rally)
        self.spawn=QCheckBox("按开局观测方位分组")
        self.spawn.setToolTip("方位按最早有效观测的舰群质心计算")
        self.spawn.setChecked(True)
        form.addRow(self.spawn)
        self.zones_combo=QComboBox()
        form.addRow("战术区域版本",self.zones_combo)
        self.support_enabled=QCheckBox("统计支援到达时间")
        form.addRow(self.support_enabled)
        self.support_zone=QLineEdit()
        self.support_zone.setPlaceholderText("区域 ID")
        form.addRow("支援目标区域",self.support_zone)
        support_times=QHBoxLayout()
        self.support_start,self.support_end=spin(60),spin(180)
        support_times.addWidget(self.support_start);support_times.addWidget(QLabel("至"));support_times.addWidget(self.support_end)
        form.addRow("触发 / 观察截止秒",support_times)
        form.addRow(btn("保存规则版本",self.save_rules,"saveRules"))
        self.advanced_rules = Disclosure("高级规则", rule_body)
        self.advanced_rules.setObjectName("workbenchAdvancedRules")
        layout.addWidget(self.advanced_rules)
        self.time_basis.currentIndexChanged.connect(self._collection_changed)
        self.start_sec.valueChanged.connect(self._collection_changed)
        self.end_sec.valueChanged.connect(self._collection_changed)
        self.only_formal.toggled.connect(self._collection_changed)
        layout.addStretch()

    def _build_regions(self):
        layout=self._tab("地图区域", tool="regions")
        actions=QHBoxLayout()
        actions.addWidget(btn("使用当前复盘地图",self.use_current_map,"useCurrentMap"))
        self.zone_name=QLineEdit()
        self.zone_name.setPlaceholderText("区域名称")
        actions.addWidget(self.zone_name,1)
        actions.addWidget(btn("撤销点",lambda:self.zone_canvas.undo_point()))
        actions.addWidget(btn("清空点",lambda:self.zone_canvas.clear_points()))
        actions.addWidget(btn("保存区域新版本",self.save_zone,"saveZone"))
        layout.addLayout(actions)
        self.zone_hint=QLabel("未载入地图")
        self.zone_hint.setWordWrap(True)
        layout.addWidget(self.zone_hint)
        self.zone_canvas=ZoneCanvas()
        self.zone_canvas.points_changed.connect(lambda points:self.zone_hint.setText(f"{len(points)} 个顶点"))
        layout.addWidget(self.zone_canvas,1)
        self.zone_table=grid(["名称","地图","顶点数"],"zoneTable")
        self.zone_table.setMaximumHeight(160)
        layout.addWidget(self.zone_table)

    def _build_results(self):
        layout = self._tab("分析结果")
        self.results_page = layout.parentWidget()
        actions = QHBoxLayout()
        self.run_combo = QComboBox()
        self.run_combo.setObjectName("analysisRun")
        self.run_combo.setAccessibleName("历史分析")
        self.run_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.run_combo.setMinimumContentsLength(16)
        self.run_combo.currentIndexChanged.connect(self.load_run)
        actions.addWidget(self.run_combo, 1)

        self.result_views = QStackedWidget()
        self.result_views.setObjectName("workbenchResultViews")
        overview_button = btn("总览", lambda: self.result_views.setCurrentIndex(0), "workbenchOverview")
        evidence_button = btn("证据", lambda: self.result_views.setCurrentIndex(1), "workbenchEvidence")
        view_buttons = QButtonGroup(self.results_page)
        for button in (overview_button, evidence_button):
            button.setCheckable(True)
            view_buttons.addButton(button)
            actions.addWidget(button)
        overview_button.setAccessibleName("分析总览")
        evidence_button.setAccessibleName("比赛证据")
        result_menu_button = QPushButton("结果操作")
        result_menu_button.setObjectName("workbenchResultActions")
        result_menu = QMenu(result_menu_button)
        result_menu_button.setMenu(result_menu)
        actions.addWidget(result_menu_button)

        def add_result_action(text, callback, name):
            action = result_menu.addAction(text)
            action.setObjectName(name)

            def invoke():
                try:
                    callback()
                except Exception as exc:
                    self.notice.setText("未完成：" + str(exc))

            action.triggered.connect(invoke)
            return action

        full_report = add_result_action("查看完整报告", lambda: self.result_views.setCurrentIndex(2), "viewWorkbenchReport")
        counterplan = add_result_action("生成对抗方案", self.counterplans, "runCounterplans")
        export_report = add_result_action("导出报告", self.export_current_run, "exportWorkbench")

        def update_menu():
            full_report.setEnabled(self.current_run is not None)
            counterplan.setEnabled(self.analysis_run is not None)
            export_report.setEnabled(self.current_run is not None)

        result_menu.aboutToShow.connect(update_menu)
        layout.addLayout(actions)
        layout.addWidget(self.result_views, 1)
        self.summary_stack=QStackedWidget()
        self.dashboard=CollectionDashboard()
        self.dashboard.evidence_requested.connect(self.filter_evidence)
        self.summary_stack.addWidget(self.dashboard)
        self.summary_browser=QTextBrowser()
        self.summary_browser.setOpenLinks(False)
        self.summary_browser.setPlainText('暂无分析结果')
        self.summary_stack.addWidget(self.summary_browser)
        self.summary_stack.setCurrentWidget(self.summary_browser)
        self.result_views.addWidget(self.summary_stack)
        evidence_page=QWidget(); evidence_layout=QVBoxLayout(evidence_page)
        evidence_layout.setContentsMargins(0, 0, 0, 0)
        filters=QHBoxLayout()
        self.evidence_query=QLineEdit(); self.evidence_query.setObjectName("workbenchEvidenceSearch")
        self.evidence_query.setPlaceholderText("搜索比赛、舰船、选手或证据类型")
        self.evidence_query.textChanged.connect(self._render_evidence)
        filters.addWidget(self.evidence_query,1)
        filters.addWidget(btn("全部证据",self.reset_evidence,"allWorkbenchEvidence"))
        self.open_evidence_button=btn("定位比赛",self.open_selected_evidence,"openWorkbenchEvidence")
        filters.addWidget(self.open_evidence_button)
        evidence_layout.addLayout(filters)
        self.evidence_caption=QLabel(); self.evidence_caption.setObjectName("subtitle")
        evidence_layout.addWidget(self.evidence_caption)
        self.evidence_table=grid(["比赛 / 地图","记录时间","类型","观测摘要"],"analysisEvidence")
        self.evidence_table.setColumnWidth(0,260);self.evidence_table.setColumnWidth(1,105);self.evidence_table.setColumnWidth(2,115)
        self.evidence_table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeMode.Stretch)
        self.evidence_table.itemDoubleClicked.connect(self.jump_evidence)
        evidence_layout.addWidget(self.evidence_table,1)
        self.result_views.addWidget(evidence_page)
        self.result_browser=QTextBrowser()
        self.result_browser.setObjectName("analysisReport")
        self.result_browser.setStyleSheet("QTextBrowser { background:#f0f4f8; color:#182b40; border:1px solid #c5d3e2; }")
        self.result_browser.setOpenLinks(False)
        self.result_views.addWidget(self.result_browser)
        self.result_views.currentChanged.connect(self._show_result_detail)

        def sync_views(index):
            view_buttons.setExclusive(False)
            overview_button.setChecked(index == 0)
            evidence_button.setChecked(index == 1)
            view_buttons.setExclusive(True)

        self.result_views.currentChanged.connect(sync_views)
        sync_views(0)
        review_body = QWidget()
        review = QHBoxLayout(review_body)
        review.setContentsMargins(0, 0, 0, 0)
        self.review_decision=QComboBox()
        self.review_decision.addItems(["批注","采纳","驳回","需复核"])
        self.review_text=QLineEdit()
        self.review_text.setPlaceholderText("添加教练批注")
        review.addWidget(self.review_decision);review.addWidget(self.review_text,1)
        review.addWidget(btn("保存批注",self.save_review,"saveReview"))
        review_disclosure = Disclosure("批注与复核", review_body)
        review_disclosure.setObjectName("workbenchReviewDisclosure")
        layout.addWidget(review_disclosure)

    def _build_training(self):
        own_box = self._tab("本方条件", tool="own")
        self.own_table=QTableWidget(3,2)
        self.own_table.setObjectName("ownRoster")
        self.own_table.setHorizontalHeaderLabels(["选手","可用舰船"])
        self.own_table.horizontalHeader().setStretchLastSection(True)
        own_box.addWidget(self.own_table)
        own_form=QFormLayout()
        self.max_players=QSpinBox();self.max_players.setRange(1,30);self.max_players.setValue(9)
        self.max_same=QSpinBox();self.max_same.setRange(0,30);self.max_same.setValue(0);self.max_same.setSpecialValueText("不设限制")
        self.banned=QLineEdit()
        own_form.addRow("参赛人数上限",self.max_players);own_form.addRow("同船上限",self.max_same);own_form.addRow("禁用舰船",self.banned)
        own_box.addLayout(own_form)
        own_actions=QHBoxLayout()
        own_actions.addWidget(btn("增加选手",lambda:self.own_table.insertRow(self.own_table.rowCount())))
        own_actions.addWidget(btn("保存本方条件",self.save_own,"saveOwn"))
        own_box.addLayout(own_actions)
        box = self._tab("计划对照", tool="plan")
        line=QHBoxLayout()
        self.plan_combo=QComboBox();self.plan_combo.currentIndexChanged.connect(self.load_plan)
        self.plan_name=QLineEdit("训练计划")
        line.addWidget(self.plan_combo,1);line.addWidget(self.plan_name,1)
        line.addWidget(btn("读取当前复盘的舰船与区域",self.refresh_plan_options,"planOptions"))
        box.addLayout(line)
        form=QFormLayout()
        self.plan_actor=QComboBox()
        self.plan_zone=QComboBox()
        self.plan_begin,self.plan_end,self.plan_tol=spin(60),spin(180),spin(10)
        form.addRow("负责对象",self.plan_actor);form.addRow("目标区域",self.plan_zone)
        timing=QHBoxLayout();timing.addWidget(self.plan_begin);timing.addWidget(QLabel("至"));timing.addWidget(self.plan_end)
        form.addRow("比赛已进行时间窗口 / 秒",timing);form.addRow("允许偏差 / 秒",self.plan_tol)
        box.addLayout(form)
        line=QHBoxLayout()
        line.addWidget(btn("添加目标",self.add_target,"addPlanTarget"))
        line.addWidget(btn("移除选中目标",self.remove_target))
        line.addWidget(btn("保存计划版本",self.save_plan,"savePlan"))
        line.addWidget(btn("对照当前比赛",self.evaluate_current,"evaluatePlan"))
        box.addLayout(line)
        self.targets_table=grid(["对象","区域","起始秒","截止秒","偏差秒"],"planTargets")
        box.addWidget(self.targets_table)
        retest = self._tab("训练复测", tool="retest")
        rbox = QFormLayout()
        self.before_combo,self.after_combo=QComboBox(),QComboBox()
        rbox.addRow("训练前集合",self.before_combo);rbox.addRow("训练后集合",self.after_combo)
        rbox.addRow(btn("比较训练复测",self.compare_training,"compareTraining"))
        retest.addLayout(rbox)
        retest.addStretch()

    def _combo_documents(self, combo, kind, empty):
        selected=combo.currentData()
        combo.blockSignals(True);combo.clear();combo.addItem(empty,None)
        for doc in self.store.list_documents(kind):
            combo.addItem(f"{doc['name']} · v{doc['revision']}",doc["id"])
        combo.setCurrentIndex(max(0,combo.findData(selected)))
        combo.blockSignals(False)

    def refresh_documents(self):
        self._refresh_collection_choices()
        for combo,kind,empty in ((self.before_combo,"collection","选择训练前集合"),
             (self.after_combo,"collection","选择训练后集合"),(self.rule_combo,"rules","未保存的当前规则"),
             (self.zones_combo,"zones","使用回放中的占领区"),(self.plan_combo,"plan","当前未保存计划")):
            self._combo_documents(combo,kind,empty)
        self.run_combo.blockSignals(True);selected=self.run_combo.currentData();self.run_combo.clear()
        self.run_combo.addItem("选择历史分析",None)
        for run in self.store.run_index(('collection','counterplans','plan_evaluation','training_comparison')):
            self.run_combo.addItem(f"{run['name']} · {run['created_at'][:19]}",run["id"])
        self.run_combo.setCurrentIndex(max(0,self.run_combo.findData(selected)));self.run_combo.blockSignals(False)
        zone_rows=self.zones_payload().get("zones",[]) if self.zones_payload() else []
        rows(self.zone_table,[[z["name"],self.host.map_names.get(z["map_id"],z["map_id"]),len(z.get("points",[]))] for z in zone_rows])

    def _refresh_collection_choices(self):
        combo = self.collection_combo
        selected = combo.currentData()
        blocked = combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("全部已导入", _SCOPE_ALL)
            combo.addItem("录像库选中项", _SCOPE_SELECTED)
            for doc in self.store.list_documents("collection"):
                combo.addItem(f"{doc['name']} · v{doc['revision']}", doc["id"])
            combo.setCurrentIndex(max(0, combo.findData(selected)))
        finally:
            combo.blockSignals(blocked)
        if combo.currentData() != selected:
            self.load_collection()
        else:
            self._refresh_live_scope()

    def _refresh_live_scope(self):
        scope = self.collection_combo.currentData()
        if scope == _SCOPE_ALL:
            self.battle_ids = [row["id"] for row in self.library.list_battles()]
        elif scope == _SCOPE_SELECTED:
            self.battle_ids = list(self.host.selected_battles())
        else:
            return
        self._collection_changed()

    def _scope_activated(self, _index):
        if self.collection_combo.currentData() == _SCOPE_SELECTED:
            self._refresh_live_scope()

    def use_all(self):
        self._set_scope(_SCOPE_ALL)

    def use_selected(self):
        self._set_scope(_SCOPE_SELECTED)

    def _set_scope(self, scope):
        index = self.collection_combo.findData(scope)
        if index == self.collection_combo.currentIndex():
            self._refresh_live_scope()
        elif index >= 0:
            self.collection_combo.setCurrentIndex(index)

    def _collection_changed(self, *_):
        basis = '开局' if self.time_basis.currentData() == 'battle_elapsed' else '录像'
        window = f'{basis} {self.start_sec.value():g}–{self.end_sec.value():g} 秒'
        formal = ' · 仅正式赛' if self.only_formal.isChecked() else ''
        self.collection_summary.setText(f"范围 {len(self.battle_ids)} 场 · {window}{formal}")

    def load_collection(self,*_):
        key=self.collection_combo.currentData()
        if key in (_SCOPE_ALL, _SCOPE_SELECTED):
            self.collection_name.setText("已导入比赛集合" if key == _SCOPE_ALL else "录像库选中集合")
            self._refresh_live_scope()
            return
        if key:
            doc=self.store.get_document(key);self.battle_ids=doc["payload"]["battle_ids"]
            self.collection_name.setText(doc["name"]);self._collection_changed()

    def save_collection(self):
        if not self.battle_ids:
            self.notice.setText("请先从比赛库选择场次。");return
        doc=self.store.save_document("collection",self.collection_name.text().strip() or "比赛集合",{"battle_ids":self.battle_ids})
        self.refresh_documents();self.collection_combo.setCurrentIndex(self.collection_combo.findData(doc["id"]))
        self.notice.setText("集合已保存")

    def team_selector(self):
        text=self.team.currentText().strip()
        if text == "每局录制方":
            return {"recording_side":True}
        return {"team_id":int(text[-1])} if text in ("绝对队伍 0","绝对队伍 1") else {"clan_tag":text}

    def rules_payload(self):
        value={"version":self.rules_name.text().strip() or "自定义规则","time_basis":self.time_basis.currentData(),
            "window_start_sec":self.start_sec.value(),"window_end_sec":self.end_sec.value(),
            "min_coverage":self.coverage.value(),"max_gap_sec":self.gap.value(),"rally_radius_m":self.rally.value(),
            "spawn_side_mode":"observed_centroid" if self.spawn.isChecked() else "explicit"}
        if self.support_enabled.isChecked():
            value["support_triggers"]=[{"id":"support:manual","zone_id":self.support_zone.text().strip(),
                "trigger_sec":self.support_start.value(),"end_sec":self.support_end.value()}]
        return value

    def save_rules(self):
        from wows_training.analysis_engine import _rules
        try:
            payload=_rules(self.rules_payload())
            doc=self.store.save_document("rules",self.rules_name.text().strip() or "空间规则",payload,self.rule_combo.currentData())
            self.refresh_documents();self.rule_combo.setCurrentIndex(self.rule_combo.findData(doc["id"]))
            self.notice.setText(f"规则 v{doc['revision']} 已保存")
        except Exception as exc:self.notice.setText(str(exc))

    def load_rules(self,*_):
        key=self.rule_combo.currentData()
        if not key:return
        doc=self.store.get_document(key);p=doc["payload"];self.rules_name.setText(doc["name"])
        for field,widget in (("window_start_sec",self.start_sec),("window_end_sec",self.end_sec),("min_coverage",self.coverage),("max_gap_sec",self.gap),("rally_radius_m",self.rally)):
            widget.setValue(p[field])
        self.time_basis.setCurrentIndex(max(0,self.time_basis.findData(p["time_basis"])))
        self.spawn.setChecked(p.get("spawn_side_mode")=="observed_centroid")
        triggers=p.get("support_triggers") or []
        self.support_enabled.setChecked(bool(triggers))
        if triggers:
            self.support_zone.setText(triggers[0]["zone_id"])
            self.support_start.setValue(triggers[0]["trigger_sec"]);self.support_end.setValue(triggers[0]["end_sec"])

    def zones_payload(self):
        key=self.zones_combo.currentData()
        return deepcopy(self.store.get_document(key)["payload"]) if key else None

    def _document_versions(self):
        return [{key:doc[key] for key in ("id","revision")} for combo in (self.rule_combo,self.zones_combo)
                if combo.currentData() for doc in [self.store.get_document(combo.currentData())]]

    def run_collection(self):
        self._refresh_live_scope()
        ids=list(self.battle_ids)
        if self.only_formal.isChecked():
            ids=[key for key in ids if self.library.get_battle(key)["label"]=="正式比赛"]
        if not ids:
            self.notice.setText("当前范围没有比赛");return
        kwargs={"team_selector":self.team_selector(),"zones":self.zones_payload(),"rules":self.rules_payload(),"document_versions":self._document_versions()}
        self._start(lambda cancel,progress:self.workspace.analyze(ids,**kwargs,cancel=cancel,progress=progress),self.show_run)

    def show_run(self, run):
        if run.get("kind")=="ai":
            self.ai_panel.current_run=run
            self.ai_panel._render_result(run["payload"])
            self.ai_panel.review_button.setEnabled(not self.ai_panel.busy)
            self.ai_panel.tabs.setCurrentIndex(3)
            if hasattr(self.host, "open_report_studio"):
                self.host.open_report_studio(run)
            self.notice.setText("离线简报已载入。" if run["payload"].get("source_type")=="offline_brief" else "AI 简报已载入，待人工复核。")
            return
        self.current_run=run
        if run["kind"]=="collection":self.analysis_run=run
        elif run["kind"]=="counterplans":self.counter_run=run
        self.refresh_documents()
        self.run_combo.blockSignals(True);self.run_combo.setCurrentIndex(max(0,self.run_combo.findData(run["id"])));self.run_combo.blockSignals(False)
        reviews=self.store.list_reviews(run["id"])
        self._detail_rendered=False
        self.result_browser.clear()
        if run.get("kind")=="collection":
            self.dashboard.set_payload(run["payload"])
            self.summary_stack.setCurrentWidget(self.dashboard)
        else:
            self.summary_browser.setHtml(compact_run_html(run,reviews))
            self.summary_stack.setCurrentWidget(self.summary_browser)
        self._evidence=run["payload"].get("evidence") or []
        self._evidence_filter_ids=None
        self._battle_labels={}
        maps=(run["payload"].get("presentation") or {}).get("maps") or {}
        library_battles={str(row.get("arena_id")):row for row in self.library.list_battles()}
        for evidence in self._evidence:
            key=evidence.get("battle_id")
            if key in self._battle_labels:continue
            source=library_battles.get(str(evidence.get("arena_id")))
            if source:
                date=source.get('date_raw') or '日期未知'
                try:date=datetime.strptime(date,"%d.%m.%Y %H:%M:%S").strftime("%m-%d %H:%M")
                except ValueError:pass
                map_name=maps.get(source.get('map_id')) or source.get('map_label') or source.get('map_id') or '未知地图'
                self._battle_labels[key]=f"{date} · {map_name}"
            else:
                battle=next((b for b in run["payload"].get("battles",[]) if b.get("battle_id")==key),{})
                map_id=(battle.get("conditions") or {}).get("map_id")
                self._battle_labels[key]=maps.get(map_id,map_id or '比赛')
        self.evidence_query.clear()
        self._render_evidence()
        self.result_views.setCurrentIndex(0)
        stale=self.workspace.stale_reasons(run)
        self.notice.setText("；".join(stale) if stale else ("已载入分析" if run.get("reused") else "分析已保存"))
        self.tabs.setCurrentWidget(self.results_page)
        self.ai_panel.refresh_sources()

    def load_run(self,*_):
        key=self.run_combo.currentData()
        if key:self.show_run(self.store.get_run(key))

    def _show_result_detail(self,index):
        if index==2 and self.current_run and not getattr(self,"_detail_rendered",False):
            self.result_browser.setHtml(render_run(self.current_run,self.store.list_reviews(self.current_run["id"]),preview=True))
            self._detail_rendered=True

    def filter_evidence(self,refs):
        self._evidence_filter_ids=set(refs)
        self.evidence_query.clear()
        self._render_evidence()
        self.result_views.setCurrentIndex(1)

    def reset_evidence(self):
        self._evidence_filter_ids=None
        self.evidence_query.clear()
        self._render_evidence()

    def _render_evidence(self,*_):
        kinds={"ship_appearance":"阵容", "observed_spawn_orientation":"开局方位", "region_dwell":"区域驻留",
            "region_transfer":"转场", "support_arrival":"支援到达", "zone_commitment":"区域投入",
            "fleet_geometry":"舰队距离", "consumable_window":"消耗品",
            "replay_relative_position":"相对位置", "replay_map_control":"地图控制",
            "replay_hull_angle":"舰体夹角", "replay_action_frequency":"观测事件频率",
            "replay_context_coverage":"录像细节覆盖", "replay_ship_phase":"分段表现",
            "replay_consumable_timing":"消耗品时机", "replay_consumable_overlap":"消耗品协同窗口",
            "replay_damage_efficiency":"伤害效率", "replay_damage_composition":"伤害构成",
            "replay_performance_coverage":"选手观测覆盖"}
        selected=getattr(self,"_evidence_filter_ids",None)
        query=self.evidence_query.text().strip().casefold()
        labels=getattr(self,"_battle_labels",{})
        candidates=[e for e in getattr(self,"_evidence",[]) if selected is None or e.get("id") in selected]
        self._visible_evidence=[e for e in candidates if not query or query in ' '.join(str(v) for v in
            (labels.get(e.get("battle_id"),e.get("battle_id")),e.get("text"),kinds.get(e.get("kind"),e.get("kind")),e.get("id"))).casefold()]
        values=[]
        for evidence in self._visible_evidence:
            clock=evidence.get("clock_start")
            clock_text=f"{int(clock)//60:02d}:{int(clock)%60:02d}" if isinstance(clock,(int,float)) else "无时间点"
            values.append([labels.get(evidence.get("battle_id"),evidence.get("battle_id")),clock_text,
                kinds.get(evidence.get("kind"),evidence.get("kind")),evidence.get("text") or "未提供摘要"])
        rows(self.evidence_table,values)
        for index,evidence in enumerate(self._visible_evidence):
            self.evidence_table.item(index,0).setToolTip(self.evidence_table.item(index,0).text())
            self.evidence_table.item(index,1).setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.evidence_table.item(index,1).setToolTip("录像时间")
        self.evidence_caption.setText(f"{len(self._visible_evidence)} 条" + (" · 当前图表关联比赛" if selected is not None else " · 全部证据"))
        self.open_evidence_button.setEnabled(bool(self._visible_evidence))
        if self._visible_evidence:self.evidence_table.selectRow(0)

    def open_selected_evidence(self):
        selected=self.evidence_table.selectionModel().selectedRows()
        if selected:self.jump_evidence(self.evidence_table.item(selected[0].row(),0))

    def jump_evidence(self,item):
        visible=getattr(self,"_visible_evidence",getattr(self,"_evidence",[]))
        if item.row()>=len(visible):return
        evidence=visible[item.row()];sha=evidence.get("source_sha")
        replay=self.library.find_replay(sha) if sha else None
        if evidence.get('recording_id'):
            self.host.open_recording(evidence['recording_id'])
            if isinstance(evidence.get('clock_start'),(int,float)):self.host.replay_panel.set_clock(evidence['clock_start'])
            return
        if replay:
            self.host.open_battle(replay["battle_id"])
            index=self.host.source_combo.findData(sha)
            if index>=0:self.host.source_combo.setCurrentIndex(index)
            if isinstance(evidence.get("clock_start"),(int,float)):self.host.replay_panel.set_clock(evidence["clock_start"])

    def save_review(self):
        if not self.current_run:return
        self.store.add_review(self.current_run["id"],self.review_decision.currentText(),self.review_text.text())
        self.review_text.clear();self.show_run(self.current_run)

    def export_current_run(self):
        if not self.current_run:return
        folder=QFileDialog.getExistingDirectory(self,"导出赛训报告",str(self.library.root))
        if folder:
            paths=export_run(self.current_run,folder,self.store.list_reviews(self.current_run["id"]))
            self.notice.setText("报告已保存："+paths["html"])
            self.last_export=paths

    def analysis_payload(self):
        if not self.analysis_run:return {}
        if self.workspace.stale_reasons(self.analysis_run):
            raise ValueError("分析输入已变化，请重新运行跨局分析后再生成方案或 AI 请求。")
        return self.analysis_run["payload"]

    def use_current_map(self):
        report=self.host.current_report
        if not report:
            self.notice.setText("请先从比赛库打开一场复盘。");return
        self.zone_report=report
        self.zone_canvas.set_report(report)
        self.zone_hint.setText(report.get("display_map_name",""))

    def save_zone(self):
        if not getattr(self,"zone_report",None) or len(self.zone_canvas.points)<3:
            self.notice.setText("需要当前地图和至少三个顶点。");return
        name=self.zone_name.text().strip()
        if not name:self.notice.setText("请填写区域名称。");return
        from wows_training.analysis_engine import _zone_config
        current=self.zones_payload() or {"version":"user-zones-1","zones":[]}
        zone={"id":"zone:"+uuid4().hex[:10],"name":name,"shape":"polygon","points":list(self.zone_canvas.points),
              "map_id":self.zone_report["metadata"]["mapName"].split("/")[-1],"coordinate_units":"engine"}
        current["zones"].append(zone);current["version"]="zones:"+digest(current)[:12]
        try:
            _zone_config(self.zone_report,current)
            doc=self.store.save_document("zones","手工战术区域",current,self.zones_combo.currentData())
            self.refresh_documents();self.zones_combo.setCurrentIndex(self.zones_combo.findData(doc["id"]))
            self.refresh_documents();self.zone_canvas.clear_points();self.notice.setText("区域已保存为新版本。")
        except Exception as exc:self.notice.setText(str(exc))

    def _ship_ids(self,text):
        result=[]
        for value in text.replace("，",",").split(","):
            value=value.strip()
            if not value:continue
            if value in self.host.catalog:result.append(value);continue
            matches=[key for key,ship in self.host.catalog.items() if value in (ship.get("display_name"),ship.get("internal_name"))]
            if len(matches)!=1:raise ValueError(f"舰船“{value}”未唯一匹配，请使用舰船参数 ID。")
            result.append(matches[0])
        return list(dict.fromkeys(result))

    def own_payload(self):
        docs=self.store.list_documents("own_conditions")
        return deepcopy(docs[-1]["payload"]) if docs else {}

    def save_own(self):
        try:
            players=[]
            existing={row["name"]:row["id"] for row in self.own_payload().get("players",[])}
            for row in range(self.own_table.rowCount()):
                name=(self.own_table.item(row,0).text() if self.own_table.item(row,0) else "").strip()
                ships=(self.own_table.item(row,1).text() if self.own_table.item(row,1) else "").strip()
                if name or ships:
                    if not name or not ships:raise ValueError("每名选手都需要姓名和可用舰船。")
                    if any(player["name"]==name for player in players):raise ValueError("本方选手名称重复，请使用可区分的别名。")
                    players.append({"id":existing.get(name) or str(uuid4()),"name":name,"ship_ids":self._ship_ids(ships)})
            if not players:raise ValueError("请至少填写一位选手及其可用舰船。")
            payload={"players":players,"available_ships":sorted({s for p in players for s in p["ship_ids"]}),
                     "max_players":self.max_players.value(),"banned_ships":self._ship_ids(self.banned.text())}
            if self.max_same.value():payload["max_per_ship"]={ship:self.max_same.value() for ship in payload["available_ships"]}
            old=self.store.list_documents("own_conditions")
            self.store.save_document("own_conditions","本方可用条件",payload,old[-1]["id"] if old else None)
            self.notice.setText("本方条件已保存")
        except Exception as exc:self.notice.setText(str(exc))

    def load_own(self):
        payload=self.own_payload()
        if not payload:return
        players=payload.get("players",[])
        self.own_table.setRowCount(max(3,len(players)))
        for index,player in enumerate(players):
            self.own_table.setItem(index,0,QTableWidgetItem(player["name"]))
            self.own_table.setItem(index,1,QTableWidgetItem(",".join(map(str,player["ship_ids"]))))
        self.max_players.setValue(payload.get("max_players",9))
        limits=payload.get("max_per_ship") or {}
        values=set(limits.values()) if isinstance(limits,dict) else set()
        self.max_same.setValue(next(iter(values)) if len(values)==1 else 0)
        self.banned.setText(",".join(map(str,payload.get("banned_ships",[]))))

    def counterplans(self):
        from wows_training.analysis_engine import build_counterplans
        try:
            analysis=deepcopy(self.analysis_payload());own=self.own_payload()
            if not analysis:raise ValueError("请先运行跨局分析。")
            payload=build_counterplans(analysis,own)
            payload["source_analysis_run"]=self.analysis_run["id"];payload["own_conditions"]=own
            payload["opponent_selector"]=analysis.get("team_selector")
            self.show_run(self.store.save_run("counterplans","规则对抗方案",payload))
            refs=list(dict.fromkeys(ref for plan in payload.get("plans",[])[:2] for ref in plan.get("evidence_ids",[])))[:200]
            if refs:
                self.ai_panel.evidence_ids.setPlainText("\n".join(refs))
                self.ai_panel.include_plan.setChecked(True)
        except Exception as exc:self.notice.setText(str(exc))

    def refresh_plan_options(self):
        report=self.host.current_report
        if not report:self.notice.setText("请先打开当前复盘。");return
        from wows_training.analysis_engine import _zone_config
        attach_identities(report,self.store,self.library.list_mappings())
        self.plan_actor.clear();self.plan_actor.addItem("所选队伍全部舰船",{"all_team":True})
        for player in report.get("roster",[]):
            resolved=player.get("resolved_identity") or {}
            selector={"player_id":resolved["player_id"]} if resolved.get("status")=="confirmed" and resolved.get("player_id") else {"name":player["name"]}
            self.plan_actor.addItem(player["name"],selector)
        self.plan_zone.clear()
        for zone in _zone_config(report,self.zones_payload()):
            self.plan_zone.addItem(zone["name"],zone["id"])

    def add_target(self):
        if not self.plan_zone.currentData():self.refresh_plan_options()
        if not self.plan_zone.currentData():return
        if self.plan_end.value()<self.plan_begin.value():self.notice.setText("截止时间不得早于起始时间。");return
        self.plan_targets.append({"id":"target:"+uuid4().hex[:10],"selector":self.plan_actor.currentData(),
            "zone_id":self.plan_zone.currentData(),"start_sec":self.plan_begin.value(),"end_sec":self.plan_end.value(),
            "tolerance_sec":self.plan_tol.value()})
        self._show_targets()

    def _show_targets(self):
        players={row['payload'].get('player_id',row['id']):row['payload'].get('display_name',row['name']) for row in self.store.list_documents('player')}
        values=[]
        for target in self.plan_targets:
            selector=target['selector'];actor=('全队' if selector.get('all_team') else selector.get('name') or players.get(selector.get('player_id'),'所选舰船'))
            index=self.plan_zone.findData(target['zone_id']);zone=self.plan_zone.itemText(index) if index>=0 else '所选区域'
            values.append([actor,zone,target['start_sec'],target['end_sec'],target['tolerance_sec']])
        rows(self.targets_table,values)

    def remove_target(self):
        selected=sorted({index.row() for index in self.targets_table.selectionModel().selectedRows()},reverse=True)
        for row in selected:self.plan_targets.pop(row)
        self._show_targets()

    def plan_payload(self):
        return {"version":self.plan_name.text().strip() or "训练计划","time_basis":"battle_elapsed","targets":deepcopy(self.plan_targets)}

    def ai_plan_payload(self):
        value={"training_plan":self.plan_payload()}
        if self.analysis_run:
            value["coach_reviews"]=[{key:row[key] for key in ("id","decision","comment","created_at")}
                for row in self.store.list_reviews(self.analysis_run["id"])[-10:]]
            for row in value["coach_reviews"]:row["comment"]=row["comment"][:500]
        if (self.counter_run and self.analysis_run
                and self.counter_run["payload"].get("source_analysis_run")==self.analysis_run["id"]
                and self.counter_run["payload"].get("own_conditions")==self.own_payload()):
            plans=self.counter_run["payload"].get("plans",[])
            value["rule_options"]=deepcopy(plans[:2])
            value["rule_options_omitted"]=max(0,len(plans)-2)
            # Keep the reviewed request within the adapter's plan text limit.
            while value["rule_options"] and len(json.dumps(value,ensure_ascii=False))>7000:
                value["rule_options"].pop()
                value["rule_options_omitted"]+=1
        return value

    def save_plan(self):
        if not self.plan_targets:self.notice.setText("请先添加计划目标。");return
        doc=self.store.save_document("plan",self.plan_name.text().strip() or "训练计划",self.plan_payload(),self.plan_combo.currentData())
        self.refresh_documents();self.plan_combo.setCurrentIndex(self.plan_combo.findData(doc["id"]))
        self.notice.setText("计划已保存")

    def load_plan(self,*_):
        key=self.plan_combo.currentData()
        if key:
            doc=self.store.get_document(key);self.plan_targets=deepcopy(doc["payload"]["targets"]);self.plan_name.setText(doc["name"]);self._show_targets()

    def evaluate_current(self):
        if not self.host.current_report:self.notice.setText("请先打开一场复盘。");return
        if not self.plan_targets:self.notice.setText("请先添加计划目标。");return
        from wows_training.analysis_engine import evaluate_plan
        sha=self.host.current_sha;team=self.team_selector();plan=self.plan_payload();zones=self.zones_payload()
        selected_recording_id=getattr(self.host,'current_recording_id',None)
        battle_id=self.host.current_battle
        def operation(cancel,progress):
            reports=self.workspace.reports([battle_id],team,cancel,progress)
            report=next((row for row in reports if (row.get('analysis_context') or {}).get('source_recording_id')==selected_recording_id),None) if selected_recording_id else next((row for row in reports if row["sha256"]==sha),None)
            if report is None:
                if selected_recording_id:
                    raise ValueError('当前显示的是历史合并记录。请切换当前双边记录再做计划对照。')
                report=self.workspace.bundle.presentation_report(self.library.get_report(sha))
                from wows_training.workbench_service import attach_identities
                from wows_training.identity_rules import parse_kots_name
                attach_identities(report,self.store,self.library.list_mappings())
                if team.get('clan_tag'):
                    for player in report.get('roster',[]):
                        parsed=parse_kots_name(player.get('name',''),report.get('event_key'))
                        if parsed.get('event_matches_suffix'):player['clan_tag']=parsed.get('clan')
            payload=evaluate_plan(report,plan,zones,ship_catalog=self.host.catalog,team_selector=team)
            payload["plan_snapshot"]=plan;payload["zones_snapshot"]=zones
            if cancel.is_set():raise InterruptedError("操作已取消")
            return self.store.save_run("plan_evaluation","计划执行对照",payload)
        self._start(operation,self.show_run)

    def compare_training(self):
        before,after=self.before_combo.currentData(),self.after_combo.currentData()
        if not before or not after or before==after:self.notice.setText("请选择两个不同的已保存集合。");return
        from wows_training.analysis_engine import compare_training
        left=self.store.get_document(before);right=self.store.get_document(after)
        team,zones,rules=self.team_selector(),self.zones_payload(),self.rules_payload()
        def operation(cancel,progress):
            a=self.workspace.reports(left["payload"]["battle_ids"],team,cancel,progress)
            b=self.workspace.reports(right["payload"]["battle_ids"],team,cancel,progress)
            if cancel.is_set():raise InterruptedError("操作已取消")
            payload=compare_training(a,b,ship_catalog=self.host.catalog,team_selector=team,zones=zones,rules=rules)
            if cancel.is_set():raise InterruptedError("操作已取消")
            payload["collection_versions"]=[{k:x[k] for k in ("id","name","revision")} for x in (left,right)]
            return self.store.save_run("training_comparison","训练复测对比",payload)
        self._start(operation,self.show_run)

    def _configuration_changed(self):
        if self.analysis_run and self.workspace.stale_reasons(self.analysis_run):
            self.notice.setText("身份资料已更新，分析待重算")
        self.ai_panel.refresh_sources()
