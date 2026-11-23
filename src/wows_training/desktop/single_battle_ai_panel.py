"""Replay-scoped AI report controls embedded in single-battle analysis."""
from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLineEdit, QPushButton, QStackedWidget, QTextBrowser,
    QVBoxLayout, QWidget,
)

from ..ai_analysis import digest
from ..analysis_perspective import PERSPECTIVE_LABELS, RANDOM_BATTLE, evidence_priority
from ..report_presentation import render_report
from .ai_panel import AIPanel
from .ui_components import StatusLabel


_QUESTION = (
    "以全局时序小地图为主要依据，评价所选舰在这场对局打得如何。解释开局选择、局势转折和结局之间的联系，"
    "先核对复盘舰所属队伍和敌我阵容。按舰名讨论走位、炮术、战场控制与争点的得失，结合实装配件解释条件。"
    "分别分析本舰输出的起火、进水及主要承伤时段，再结合亮灭、角度、消耗品和同服同舰基准解释原因。"
    "指出值得保留的选择、代价最大的一次取舍和当时可行的替代打法。某项缺乏观测时说明评价边界。"
)
_EVIDENCE_PRIORITY = {
    "ordinary_overview": 0, "local_model_summary": 1, "local_model_checkpoint": 2,
}


class SingleBattleAIPanel(QWidget):
    idle = Signal()

    def __init__(self, host, analysis_getter, parent=None):
        super().__init__(parent or host)
        self.setObjectName("singleBattleAIReport")
        self.host = host
        self.analysis_getter = analysis_getter
        self._source_key = None
        self._source_token = None
        self._analysis = {}
        self._current_run = None
        self._reload_on_idle = set()
        self.shared_engine = host.workbench.ai_panel
        self.engine = AIPanel(self.shared_engine.store, self.shared_engine.bundle,
                             lambda: self._analysis, dict, dict, self)
        self.engine.report_context_getter = self._report_context
        self.engine.image_provider = self._replay_images
        self.engine.include_plan.setChecked(False)
        self.engine.include_own.setChecked(False)
        self.engine.tabs.tabBar().hide()
        self.engine.manual_button.hide()
        self._build_ui()
        self.engine.run_saved.connect(self._show_run)
        self.engine.status.connect(self._show_status)
        self.engine.idle.connect(self._engine_idle)
        self.shared_engine.idle.connect(self._shared_idle)
        self.shared_engine.changed.connect(self._shared_settings_saved)
        self.engine.changed.connect(self._embedded_settings_saved)
        self._render_empty()
        self._sync_controls()

    @property
    def busy(self):
        return self.engine.busy

    @property
    def current_run(self):
        return deepcopy(self._current_run)

    def cancel(self):
        self.engine.cancel()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(2, 5, 2, 2)
        root.setSpacing(7)
        bar = QHBoxLayout()
        self.perspective_combo = QComboBox()
        self.perspective_combo.setObjectName("singleBattleAnalysisPerspective")
        self.perspective_combo.setAccessibleName("分析视角")
        self.perspective_combo.setToolTip("按个人决策或团队分工组织复盘")
        for key, label in PERSPECTIVE_LABELS.items():
            self.perspective_combo.addItem(label, key)
        self.perspective_combo.currentIndexChanged.connect(self.refresh_source)
        bar.addWidget(self.perspective_combo)
        self.focus_edit = QLineEdit()
        self.focus_edit.setObjectName("singleBattleAIFocus")
        self.focus_edit.setAccessibleName("补充复盘关注点")
        self.focus_edit.setPlaceholderText("补充关注点（可选）")
        self.focus_edit.setMaxLength(180)
        self.focus_edit.textChanged.connect(self._set_question)
        bar.addWidget(self.focus_edit, 1)
        self.settings_button = QPushButton("AI 连接")
        self.settings_button.clicked.connect(self._open_settings)
        self.offline_button = QPushButton("生成离线简报")
        self.offline_button.clicked.connect(self._generate_offline)
        self.preview_button = QPushButton("AI 分析预览")
        self.preview_button.setObjectName("primary")
        self.preview_button.clicked.connect(self._preview)
        self.studio_button = QPushButton("打开报告工作台")
        self.studio_button.clicked.connect(self._open_studio)
        self.back_button = QPushButton("返回报告")
        self.back_button.clicked.connect(self._back)
        for button in (self.settings_button, self.offline_button, self.preview_button,
                       self.studio_button, self.back_button):
            bar.addWidget(button)
        root.addLayout(bar)
        self.notice = StatusLabel()
        self.notice.setObjectName("notice")
        root.addWidget(self.notice)
        self.stack = QStackedWidget()
        self.viewer = QTextBrowser()
        self.viewer.setObjectName("singleBattleAIResult")
        self.viewer.setAccessibleName("单局复盘报告")
        self.viewer.setOpenLinks(False)
        self.viewer.setOpenExternalLinks(False)
        from .report_image_preview import attach_report_image_preview
        attach_report_image_preview(self.viewer, lambda: (self._current_run or {}).get('payload'))
        self.stack.addWidget(self.viewer)
        self.stack.addWidget(self.engine)
        root.addWidget(self.stack, 1)
        root.addWidget(self.engine.generation_progress)

    def set_source(self, key):
        """Set the original replay identity, or clear the tab's source with None."""
        changed = key != self._source_key
        self._source_key = key
        if changed:
            self.focus_edit.clear()
            blocked = self.perspective_combo.blockSignals(True)
            self.perspective_combo.setCurrentIndex(self.perspective_combo.findData(RANDOM_BATTLE))
            self.perspective_combo.blockSignals(blocked)
        self.refresh_source()

    def refresh_source(self):
        payload = deepcopy(self.analysis_getter() or {}) if self._source_key is not None else {}
        rows = self.engine._evidence(payload)
        if rows:
            perspective = self._perspective()
            payload["analysis_perspective"] = perspective

            def priority(row):
                kind = row.get("kind")
                return _EVIDENCE_PRIORITY[kind] if kind in _EVIDENCE_PRIORITY else 3 + evidence_priority(kind, perspective)

            payload["evidence"] = sorted(rows, key=priority)
        token = digest({"replay": self._source_key, "analysis": payload}) if rows else None
        if token == self._source_token:
            return
        if self.engine.busy:
            self.engine.cancel()
        self._analysis = payload
        self._source_token = token
        self._current_run = None
        self.engine.current_run = None
        self.engine._invalidate("本局证据或分析视角已变化，请重新预览。")
        self.engine.refresh_sources()
        self.engine.preview_text.clear()
        self.engine.preview_summary.setText("暂无请求预览")
        self._render_empty()
        self.stack.setCurrentWidget(self.viewer)
        self.notice.setText("")
        self._sync_controls()

    def _perspective(self):
        return self.perspective_combo.currentData() or RANDOM_BATTLE

    def _set_question(self, *_):
        focus = self.focus_edit.text().strip()
        question = f"{_QUESTION}\n补充关注点：{focus}" if focus else _QUESTION
        self.engine.question.setPlainText(question)

    def _report_context(self):
        anonymous = self.engine.anonymize.isChecked()
        labels = {str(row["battle_id"]): "本局随机战" for row in self._analysis.get("evidence", [])
                  if row.get("battle_id")}
        perspective = self._perspective()
        return {
            "source_name": "当前随机战" if anonymous else self._analysis.get("source_name", "当前随机战"),
            "source_fingerprint": self._source_token,
            "analysis_perspective": perspective,
            "layout": {"template": "review", "title": PERSPECTIVE_LABELS[perspective], "show_evidence": False},
            "battle_labels": labels,
            "anonymized": anonymous,
        }

    def _replay_images(self, _analysis, selected_rows):
        from .replay_evidence_frames import build_replay_evidence_frames
        report = getattr(self.host, 'current_report', None)
        if not report or not self._source_key or report.get('sha256') != self._source_key[0]:
            return []
        try:
            return build_replay_evidence_frames(report, selected_rows, ship_catalog=getattr(self.host, 'catalog', {}))
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            return []

    def _open_settings(self):
        self._open_engine(0)

    def _open_engine(self, index):
        self.engine.tabs.setCurrentIndex(index)
        self.stack.setCurrentWidget(self.engine)
        self.notice.setText("")
        self._sync_controls()

    def _generate_offline(self):
        if self.engine.busy or not self._source_token:
            return
        self._set_question()
        self.engine.generate_offline_brief()

    def _preview(self):
        if self.engine.busy or not self._source_token:
            return
        self._set_question()
        if self.engine.preview_request() is not None:
            self._open_engine(2)

    def _back(self):
        self.engine._invalidate("已返回报告，请重新预览后发送。")
        self.stack.setCurrentWidget(self.viewer)
        self.notice.setText("")
        self._sync_controls()

    def _show_run(self, run):
        payload = run["payload"]
        context = payload.get("report_context") or {}
        if self._source_token is None or context.get("source_fingerprint") != self._source_token:
            return
        self._current_run = run
        self.viewer.setHtml(render_report(payload, context["layout"], interactive=True))
        self.stack.setCurrentWidget(self.viewer)
        self._sync_controls()

    def _render_empty(self):
        message = "尚未生成报告" if self._source_token else "当前录像暂无可用证据"
        self.viewer.setPlainText(message)

    def _open_studio(self):
        if self._current_run is not None:
            self.host.open_report_studio(self._current_run)

    def _show_status(self, message):
        if self.stack.currentWidget() is self.viewer:
            self.notice.setText(message)
        self._sync_controls()

    def _sync_controls(self):
        ready = self._source_token is not None
        working = self.engine.busy
        using_engine = self.stack.currentWidget() is self.engine
        self.perspective_combo.setEnabled(ready and not working)
        self.focus_edit.setEnabled(ready and not working)
        self.settings_button.setEnabled(not working)
        self.offline_button.setEnabled(ready and not working)
        self.preview_button.setEnabled(ready and not working)
        self.studio_button.setVisible(self._current_run is not None and not using_engine)
        self.studio_button.setEnabled(not working)
        self.back_button.setVisible(using_engine)
        self.back_button.setEnabled(not working)

    def _shared_settings_saved(self, record):
        if record.get("kind") == "ai_settings":
            self._reload_settings(self.engine)

    def _embedded_settings_saved(self, record):
        if record.get("kind") == "ai_settings":
            self._reload_settings(self.shared_engine)

    def _reload_settings(self, panel):
        if panel.busy:
            self._reload_on_idle.add(panel)
            return
        self._reload_on_idle.discard(panel)
        panel._loading = True
        try:
            panel._load_settings()
        finally:
            panel._loading = False
        panel._input_changed()
        panel._update_key_status()

    def _engine_idle(self):
        if self.engine in self._reload_on_idle:
            self._reload_settings(self.engine)
        self._sync_controls()
        self.idle.emit()

    def _shared_idle(self):
        if self.shared_engine in self._reload_on_idle:
            self._reload_settings(self.shared_engine)
