"""Explicit AI preview, send and review UI; no automatic external requests."""
from __future__ import annotations

from copy import deepcopy
import base64
import hashlib
from html import escape
import json
from pathlib import Path
import re
import threading

from PySide6.QtCore import QElapsedTimer, QSize, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSpinBox, QTabWidget, QTextBrowser, QToolButton, QVBoxLayout, QWidget)

from wows_training.ai_analysis import (AIClient, AIConfig, AIError, AICancelled, MAX_CHART_IMAGES,
    DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_TIMEOUT_SECONDS, MAX_CUSTOM_OUTPUT_TOKENS, REASONING_EFFORTS,
    MAX_RESPONSE_BYTES, SINGLE_BATTLE_CONTRACTS, compact_brief, digest, import_manual_response, normalize_endpoint,
    offline_brief, prepare_evidence, evidence_location)
from wows_training.credential_store import SecretStore, SecretStoreError
from .ui_components import StatusLabel


def credential_name(base_url):
    endpoint = normalize_endpoint(base_url)
    return "ai_endpoint:" + hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def _plain_label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class _GenerationProgress(QFrame):
    """Report real request milestones and elapsed time during the provider's wait."""

    _steps = {
        "preparing": (0, "准备请求"),
        "requesting": (1, "正在请求 AI 服务 · 等待模型生成"),
        "retrying": (1, "正在重试 · 等待模型生成"),
        "checking": (2, "正在核验模型返回的内容"),
        "cached": (2, "正在读取缓存报告"),
        "saving": (3, "正在保存生成结果"),
    }

    def __init__(self, cancel, parent=None):
        super().__init__(parent)
        self.setObjectName("aiGenerationProgress")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(13, 7, 13, 9)
        layout.setSpacing(12)
        track = QVBoxLayout()
        track.setSpacing(5)
        row = QHBoxLayout()
        self.description = _plain_label()
        self.description.setObjectName("aiGenerationStage")
        self.time_label = _plain_label()
        self.time_label.setObjectName("subtitle")
        row.addWidget(self.description, 1)
        row.addWidget(self.time_label)
        track.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setObjectName("aiGenerationTrack")
        self.bar.setAccessibleName("AI 生成进度")
        self.bar.setRange(0, 4)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        track.addWidget(self.bar)
        layout.addLayout(track, 1)
        layout.addWidget(cancel)
        self.clock = QElapsedTimer()
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._update_time)
        self.hide()

    def reset(self):
        self.timer.stop()
        self.hide()

    def start(self):
        self.clock.start()
        self.stage("preparing")
        self.show()
        self.timer.start()

    def _describe(self, text):
        self.description.setText(text)
        self.bar.setAccessibleDescription(text)
        self._update_time()

    def _update_time(self):
        if self.clock.isValid():
            self.time_label.setText(f"用时 {self.clock.elapsed() // 1000} 秒")

    def stage(self, step):
        value, text = self._steps[step]
        self.bar.setValue(value)
        self._describe(text)

    def cancelling(self):
        self._describe("正在取消请求")

    def finish(self, text, *, completed=False):
        self.timer.stop()
        if completed:
            self.bar.setValue(self.bar.maximum())
        self._describe(text)


class _AIWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    progress = Signal(str)

    def __init__(self, config, api_key, prepared, parent=None):
        super().__init__(parent)
        self.config, self._key, self.prepared = config, api_key, deepcopy(prepared)
        self.is_connection_test = prepared is None
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        key = self._key
        try:
            client = AIClient(self.config, key)
            result = (client.test_connection(self.cancel_event) if self.is_connection_test
                      else client.analyze(self.prepared, self.cancel_event, progress=self.progress.emit))
            if self.cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.succeeded.emit(result)
        except AICancelled:
            self.cancelled.emit()
        except Exception as error:
            self.failed.emit(str(error).replace(key, "[REDACTED]") if key else "AI 请求失败。")
        finally:
            self._key = ""


class AIPanel(QWidget):
    changed = Signal(object)
    run_saved = Signal(object)
    status = Signal(str)
    idle = Signal()

    def __init__(self, store, bundle, analysis_getter, plan_getter, own_conditions_getter, parent=None):
        super().__init__(parent)
        self.store, self.bundle = store, bundle
        self.analysis_getter = analysis_getter
        self.plan_getter, self.own_conditions_getter = plan_getter, own_conditions_getter
        self.secrets = SecretStore(store.root)
        self._worker = None
        self._connection_feedback = False
        self._loading = True
        self._close_pending = False
        self._settings_id = None
        self._credential_generation = 0
        self.prepared = None
        self._preview_fingerprint = None
        self._preview_context_hash = None
        self._running_prepared = None
        self._running_context_hash = None
        self.current_run = None
        self.report_context_getter = None
        self.image_provider = None
        self._build_ui()
        self._load_settings()
        self._loading = False
        self._connect_inputs()
        self.refresh_sources()
        self._update_key_status()
        self._watcher = QTimer(self)
        self._watcher.setInterval(750)
        self._watcher.timeout.connect(self._watch_external)
        self._watcher.start()

    @property
    def busy(self):
        return self._worker is not None

    def _build_ui(self):
        layout = QVBoxLayout(self)
        self.status_label = StatusLabel()
        layout.addWidget(self.status_label)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.settings_page = QWidget()
        setting_layout = QVBoxLayout(self.settings_page)
        self.settings_fields = QWidget()
        fields_layout = QVBoxLayout(self.settings_fields)
        fields_layout.setContentsMargins(0, 0, 0, 0)
        setting_layout.addWidget(self.settings_fields)
        self.base_url = QLineEdit("https://api.openai.com/v1")
        self.model = QLineEdit()
        self.model.setPlaceholderText("模型名称")
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("留空保留已有密钥")
        self.key_status = _plain_label()
        form = QFormLayout()
        form.addRow("服务地址", self.base_url)
        form.addRow("模型", self.model)
        form.addRow("API 密钥", self.key_input)
        form.addRow("密钥状态", self.key_status)
        fields_layout.addLayout(form)
        advanced = QPushButton("调用限制与兼容选项 ▸")
        advanced.setCheckable(True)
        fields_layout.addWidget(advanced)
        controls = QGroupBox()
        controls.setVisible(False)
        advanced.toggled.connect(controls.setVisible)
        advanced.toggled.connect(lambda checked: advanced.setText("调用限制与兼容选项 ▾" if checked else "调用限制与兼容选项 ▸"))
        grid = QGridLayout(controls)
        self.token_parameter = QComboBox()
        self.token_parameter.addItems(["max_completion_tokens", "max_tokens"])
        self.output_tokens = QSpinBox()
        self.output_tokens.setRange(0, MAX_CUSTOM_OUTPUT_TOKENS)
        self.output_tokens.setSpecialValueText("服务默认（本地不限）")
        self.output_tokens.setValue(DEFAULT_MAX_OUTPUT_TOKENS)
        self.send_temperature = QCheckBox("发送温度参数")
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0, 2)
        self.temperature.setDecimals(2)
        self.temperature.setValue(.2)
        self.temperature.setEnabled(False)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0, 999_999_999)
        self.timeout.setSpecialValueText("不限时")
        self.timeout.setSingleStep(5)
        self.timeout.setValue(DEFAULT_TIMEOUT_SECONDS)
        self.timeout.setSuffix(" 秒")
        self.daily_limit = QSpinBox()
        self.daily_limit.setRange(0, 1000)
        self.daily_limit.setSpecialValueText("不限")
        self.daily_limit.setToolTip("0 表示不限。按服务地址和 UTC 日期记录次数，重试与失败也计数。")
        self.retry = QCheckBox("429 / 服务端错误最多重试一次")
        self.retry.setChecked(True)
        self.json_mode = QCheckBox("请求 JSON 模式（服务需支持）")
        self.reasoning_effort = QComboBox()
        self.reasoning_effort.addItem("服务默认", "")
        for effort, label in zip(REASONING_EFFORTS, ("低", "中", "高")):
            self.reasoning_effort.addItem(label, effort)
        self.reasoning_effort.setCurrentIndex(self.reasoning_effort.findData("high"))
        self.reasoning_effort.setToolTip("用于 OpenAI 的 GPT-6.2 Sol 和 Astra。测试连接使用低强度。")
        for row, (label, widget) in enumerate((("输出上限参数", self.token_parameter), ("单次输出 token 上限", self.output_tokens),
                                             ("全部尝试总超时", self.timeout), ("本地每日请求上限", self.daily_limit))):
            grid.addWidget(_plain_label(label), row, 0)
            grid.addWidget(widget, row, 1)
        grid.addWidget(self.send_temperature, 4, 0)
        grid.addWidget(self.temperature, 4, 1)
        grid.addWidget(self.retry, 5, 0, 1, 2)
        grid.addWidget(self.json_mode, 6, 0, 1, 2)
        grid.addWidget(_plain_label("推理强度"), 7, 0)
        grid.addWidget(self.reasoning_effort, 7, 1)
        fields_layout.addWidget(controls)
        connection_actions = QHBoxLayout()
        self.test_connection_button = QPushButton("测试连接")
        self.test_connection_button.setObjectName("testAIConnection")
        self.test_connection_button.setToolTip("发送一条固定短句，计入 API 用量和本软件的每日请求次数。")
        self.test_connection_button.clicked.connect(self._test_or_cancel_connection)
        connection_actions.addWidget(self.test_connection_button)
        self.save_settings_button = QPushButton("保存配置与新密钥")
        self.save_settings_button.clicked.connect(self.save_settings)
        connection_actions.addWidget(self.save_settings_button)
        connection_actions.addStretch(1)
        setting_layout.addLayout(connection_actions)
        setting_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.settings_page)
        self.tabs.addTab(scroll, "服务设置")

        self.evidence_page = QWidget()
        evidence_layout = QVBoxLayout(self.evidence_page)
        self.source_label = _plain_label()
        evidence_layout.addWidget(self.source_label)
        self.question = QPlainTextEdit()
        self.question.setPlaceholderText("本次分析重点")
        self.question.setPlainText("重点复盘选手用船、阵容分工和开局协同，给出一页内可读的改进建议。")
        self.question.setMaximumHeight(90)
        evidence_layout.addWidget(_plain_label("分析问题"))
        evidence_layout.addWidget(self.question)
        row = QHBoxLayout()
        self.kind_filter = QComboBox()
        self.kind_filter.addItem("全部证据类别", None)
        self.evidence_limit = QSpinBox()
        self.evidence_limit.setRange(1, 200)
        self.evidence_limit.setValue(100)
        row.addWidget(_plain_label("类别"))
        row.addWidget(self.kind_filter, 1)
        row.addWidget(_plain_label("取前 N 条"))
        row.addWidget(self.evidence_limit)
        evidence_layout.addLayout(row)
        self.evidence_ids = QPlainTextEdit()
        self.evidence_ids.setMaximumHeight(80)
        self.evidence_ids.setPlaceholderText("指定证据 ID（可选，最多 200 条）")
        evidence_layout.addWidget(self.evidence_ids)
        self.selection_label = _plain_label()
        evidence_layout.addWidget(self.selection_label)
        self.anonymize = QCheckBox("匿名化选手与队伍")
        self.anonymize.setToolTip("仅替换已知名称，自由文本可能含身份信息。")
        self.anonymize.setChecked(True)
        self.include_plan = QCheckBox("加入当前战术计划版本")
        self.include_plan.setChecked(True)
        self.include_own = QCheckBox("加入当前本方条件版本")
        self.include_own.setChecked(True)
        evidence_layout.addWidget(self.anonymize)
        evidence_layout.addWidget(self.include_plan)
        evidence_layout.addWidget(self.include_own)
        buttons = QHBoxLayout()
        refresh = QPushButton("更新来源")
        refresh.clicked.connect(self.refresh_sources)
        self.preview_button = QPushButton("预览所选证据")
        self.preview_button.clicked.connect(self.preview_request)
        self.offline_button = QPushButton("生成离线简报")
        self.offline_button.setObjectName("offlineTacticalBrief")
        self.offline_button.clicked.connect(self.generate_offline_brief)
        buttons.addWidget(refresh)
        buttons.addWidget(self.offline_button)
        buttons.addWidget(self.preview_button)
        evidence_layout.addLayout(buttons)
        evidence_layout.addStretch()
        self.tabs.addTab(self.evidence_page, "选择证据")

        preview_page = QWidget()
        preview_layout = QVBoxLayout(preview_page)
        self.preview_summary = _plain_label("暂无请求预览")
        self.include_images = QCheckBox("附上录像小地图")
        self.include_images.setChecked(True)
        self.include_images.toggled.connect(self._images_changed)
        self.image_bar = QScrollArea()
        self.image_bar.setWidgetResizable(True)
        self.image_bar.setFixedHeight(125)
        self.image_bar.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        strip = QWidget()
        self.image_bar.setWidget(strip)
        image_layout = QHBoxLayout(strip)
        image_layout.setContentsMargins(0, 0, 0, 0)
        self.image_buttons = []
        for index in range(MAX_CHART_IMAGES):
            thumbnail = QToolButton()
            thumbnail.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            thumbnail.setIconSize(QSize(108, 66))
            thumbnail.clicked.connect(lambda _checked=False, i=index: self._open_image(i))
            image_layout.addWidget(thumbnail)
            self.image_buttons.append(thumbnail)
        image_layout.addStretch(1)
        self.image_bar.hide()
        self.preview_text = QPlainTextEdit()
        self.preview_text.setReadOnly(True)
        preview_layout.addWidget(self.preview_summary)
        preview_layout.addWidget(self.include_images)
        preview_layout.addWidget(self.image_bar)
        self.review_budget_button = QPushButton("本次使用服务默认输出上限 / 不限时")
        self.review_budget_button.clicked.connect(self._use_review_budget)
        self.review_budget_button.hide()
        preview_layout.addWidget(self.review_budget_button)
        preview_layout.addWidget(self.preview_text)
        buttons = QHBoxLayout()
        self.send_button = QPushButton("发送回放信息并生成报告")
        self.send_button.setObjectName("primary")
        self.send_button.setMinimumHeight(38)
        self.send_button.setEnabled(False)
        self.send_button.clicked.connect(self.send_prepared)
        self.manual_button = QPushButton("导入分析结果")
        self.manual_button.setEnabled(False)
        self.manual_button.clicked.connect(self.import_response_file)
        self.cancel_button = QPushButton("取消请求")
        self.cancel_button.hide()
        self.cancel_button.clicked.connect(self.cancel)
        buttons.addWidget(self.send_button)
        buttons.addWidget(self.manual_button)
        buttons.addStretch(1)
        preview_layout.addLayout(buttons)
        self.tabs.addTab(preview_page, "请求预览")

        result_page = QWidget()
        result_layout = QVBoxLayout(result_page)
        self.result_summary = _plain_label("单页简报")
        self.result_summary.setToolTip("已核对引用，解释与建议待人工复核。")
        self.result_text = QTextBrowser()
        self.result_text.setReadOnly(True)
        self.result_text.setOpenExternalLinks(False)
        self.result_text.setOpenLinks(False)
        from .report_image_preview import attach_report_image_preview
        attach_report_image_preview(self.result_text, lambda: (self.current_run or {}).get('payload'))
        self.result_text.setObjectName("tacticalBrief")
        self.full_result_text = QPlainTextEdit()
        self.full_result_text.setReadOnly(True)
        self.review_text = QPlainTextEdit()
        self.review_text.setReadOnly(True)
        result_layout.addWidget(self.result_summary)
        self.result_view = QTabWidget()
        self.result_view.addTab(self.result_text, "单页简报")
        result_layout.addWidget(self.result_view, 1)
        self.details_button = QPushButton("展开完整内容与核查 ▸")
        self.details_button.setCheckable(True)
        self.details_button.toggled.connect(self._toggle_details)
        result_layout.addWidget(self.details_button)
        review_row = QHBoxLayout()
        self.review_decision = QComboBox()
        for label, value in (("采纳", "accepted"), ("驳回", "rejected"), ("待核查", "needs_review"), ("仅备注", "comment")):
            self.review_decision.addItem(label, value)
        self.review_comment = QLineEdit()
        self.review_comment.setPlaceholderText("复核备注")
        self.review_button = QPushButton("保存复核")
        self.review_button.setEnabled(False)
        self.review_button.clicked.connect(self.add_review)
        review_row.addWidget(self.review_decision)
        review_row.addWidget(self.review_comment, 1)
        review_row.addWidget(self.review_button)
        result_layout.addLayout(review_row)
        self.tabs.addTab(result_page, "战术简报")
        self.tabs.setCurrentIndex(1)
        self.generation_progress = _GenerationProgress(self.cancel_button, self)
        layout.addWidget(self.generation_progress)

    def _toggle_details(self, checked):
        if checked:
            self.result_view.addTab(self.full_result_text, "完整内容")
            self.result_view.addTab(self.review_text, "引用核查")
            self.result_view.setCurrentIndex(1)
        else:
            while self.result_view.count() > 1:
                self.result_view.removeTab(1)
            self.result_view.setCurrentIndex(0)
        self.details_button.setText("收起完整内容与核查 ▾" if checked else "展开完整内容与核查 ▸")

    def _connect_inputs(self):
        for widget in (self.model, self.key_input):
            widget.textChanged.connect(self._input_changed)
        self.base_url.textChanged.connect(self._endpoint_changed)
        self.question.textChanged.connect(self._input_changed)
        self.evidence_ids.textChanged.connect(self._selection_changed)
        for widget in (self.anonymize, self.include_plan, self.include_own,
                       self.send_temperature, self.retry, self.json_mode):
            widget.toggled.connect(self._input_changed)
        self.send_temperature.toggled.connect(self.temperature.setEnabled)
        for widget in (self.output_tokens, self.evidence_limit, self.temperature, self.timeout, self.daily_limit):
            widget.valueChanged.connect(self._selection_changed if widget is self.evidence_limit else self._input_changed)
        self.kind_filter.currentIndexChanged.connect(self._selection_changed)
        self.token_parameter.currentIndexChanged.connect(self._input_changed)
        self.reasoning_effort.currentIndexChanged.connect(self._input_changed)
        self.key_input.textChanged.connect(self._update_key_status)

    def _say(self, text):
        self._connection_feedback = False
        self.status_label.setText(text)
        self.status.emit(text)

    def _say_connection(self, text):
        self._say(text)
        self._connection_feedback = True

    def _settings_payload(self):
        return {"base_url": self.base_url.text().strip(), "model": self.model.text().strip(),
                "temperature": self.temperature.value() if self.send_temperature.isChecked() else None,
                "max_output_tokens": self.output_tokens.value(), "token_parameter": self.token_parameter.currentText(),
                "reasoning_effort": self.reasoning_effort.currentData() or None,
                "timeout_seconds": self.timeout.value(), "daily_request_limit": self.daily_limit.value(),
                "max_attempts": 2 if self.retry.isChecked() else 1, "json_mode": self.json_mode.isChecked()}

    def _config(self):
        return AIConfig(**self._settings_payload(), state_dir=Path(self.store.root) / "ai")

    def _load_settings(self):
        try:
            documents = self.store.list_documents("ai_settings")
            if not documents:
                return
            record = documents[-1]
            self._settings_id = record["id"]
            settings = record["payload"]
            self.base_url.setText(str(settings.get("base_url", "https://api.openai.com/v1")))
            self.model.setText(str(settings.get("model", "")))
            self.token_parameter.setCurrentText(settings.get("token_parameter", "max_completion_tokens"))
            output_tokens = int(settings.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS))
            if output_tokens == 8192 and not settings.get("output_limit_mode"):
                output_tokens = DEFAULT_MAX_OUTPUT_TOKENS
            self.output_tokens.setValue(output_tokens)
            timeout = float(settings.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
            if timeout == 120 and not settings.get("timeout_mode"):
                timeout = DEFAULT_TIMEOUT_SECONDS
            self.timeout.setValue(timeout)
            effort = settings.get("reasoning_effort", "high")
            self.reasoning_effort.setCurrentIndex(max(0, self.reasoning_effort.findData(effort or "")))
            self.daily_limit.setValue(int(settings.get("daily_request_limit", 0)))
            temperature = settings.get("temperature")
            self.send_temperature.setChecked(temperature is not None)
            self.temperature.setValue(float(temperature) if temperature is not None else .2)
            self.temperature.setEnabled(temperature is not None)
            self.retry.setChecked(settings.get("max_attempts", 2) == 2)
            self.json_mode.setChecked(settings.get("json_mode", False) is True)
        except Exception:
            self._say("已保存配置无法完整读取，请检查后重新保存。")
        finally:
            self.key_input.clear()

    def _update_key_status(self, *_):
        if self.key_input.text():
            self.key_status.setText("新密钥尚未保存")
            return
        try:
            exists = self.secrets.get(credential_name(self.base_url.text().strip())) is not None
            self.key_status.setText("已有加密密钥" if exists else "尚未保存密钥")
        except Exception:
            self.key_status.setText("地址无效，或无法读取此地址的加密密钥。")

    def _effective_key(self):
        value = self.key_input.text().strip()
        if value:
            return value
        try:
            return self.secrets.get(credential_name(self.base_url.text().strip()))
        except (AIError, OSError, SecretStoreError):
            return None

    def _endpoint_changed(self, *_):
        if self._loading:
            return
        self.key_input.clear()
        self._credential_generation += 1
        self._input_changed()
        self._update_key_status()

    def _input_changed(self, *_):
        if self._loading:
            return
        if self._connection_feedback and not self.busy:
            self._say("")
        self._credential_generation += 1
        self._invalidate("输入或配置已变化，请重新预览后发送。")

    def _selection_changed(self, *_):
        self._input_changed()
        self._update_selection_label()

    def _invalidate(self, message):
        was_prepared = self.prepared is not None
        self.prepared = None
        self._preview_fingerprint = None
        self.image_bar.hide()
        self.review_budget_button.hide()
        for button in self.image_buttons:
            button.setIcon(QIcon())
        self.send_button.setEnabled(False)
        self.manual_button.setEnabled(False)
        if not self.busy:
            self.generation_progress.reset()
        if was_prepared:
            self.preview_summary.setText("预览已失效：" + message)
            self._say(message)

    def _contexts(self):
        analysis = self.analysis_getter() or {}
        plan = self.plan_getter() or {}
        own = self.own_conditions_getter() or {}
        if not all(isinstance(value, dict) for value in (analysis, plan, own)):
            raise AIError("当前分析、计划和本方条件应为结构化对象。")
        return analysis, plan, own

    def _context_hash(self, contexts=None):
        analysis, plan, own = contexts or self._contexts()
        return digest({"analysis": analysis, "plan": plan, "own_conditions": own})

    def _fingerprint(self, contexts=None):
        return digest({"context": self._context_hash(contexts), "settings": self._settings_payload(),
                       "question": self.question.toPlainText(),
                       "anonymize": self.anonymize.isChecked(), "include_plan": self.include_plan.isChecked(),
                       "include_images": self.include_images.isChecked(),
                       "include_own": self.include_own.isChecked(), "kind": self.kind_filter.currentData(),
                       "limit": self.evidence_limit.value(), "ids": self.evidence_ids.toPlainText(),
                       "credential_generation": self._credential_generation})

    @staticmethod
    def _evidence(analysis):
        rows = analysis.get("evidence") or []
        if isinstance(rows, dict):
            return [dict(row, id=key) for key, row in rows.items() if isinstance(row, dict)]
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def _selected_ids(self, analysis):
        rows = self._evidence(analysis)
        team_row = next((row for row in rows if row.get("kind") == "replay_team_identity"), None)
        explicit = self.evidence_ids.toPlainText().strip()
        if explicit:
            selected = [value for value in re.split(r"[\s,，;；]+", explicit) if value]
            mode = "指定 ID"
            candidates = rows
        else:
            kind = self.kind_filter.currentData()
            candidates = [row for row in rows if kind is None or row.get("kind") == kind]
            if team_row and team_row not in candidates:
                candidates.insert(0, team_row)
            selected = [row.get("id") for row in candidates[:self.evidence_limit.value()]]
            mode = "按类别取前 N 条"
        if team_row and team_row["id"] not in selected:
            selected = [team_row["id"], *selected]
            if not explicit:
                selected = selected[:self.evidence_limit.value()]
        return selected, {"total": len(rows), "matching": len(candidates), "selected": len(selected),
                          "omitted": max(0, len(rows) - len(selected)), "mode": mode}

    def _update_selection_label(self):
        try:
            selected, counts = self._selected_ids(self._contexts()[0])
            self.selection_label.setText(f"选择 {counts['selected']} / {counts['total']} 条 · 省略 {counts['omitted']} 条")
            self.selection_label.setToolTip(f"当前范围 {counts['matching']} 条 · {counts['mode']}")
        except Exception:
            self.selection_label.setText("暂无分析证据")

    def refresh_sources(self):
        self._invalidate("分析来源已更新，请重新预览。")
        try:
            analysis, plan, own = self._contexts()
            rows = self._evidence(analysis)
            current = self.kind_filter.currentData()
            self.kind_filter.blockSignals(True)
            self.kind_filter.clear()
            self.kind_filter.addItem("全部证据类别", None)
            labels = {"region_dwell": "区域停留", "region_transfer": "区域转场", "support_arrival": "支援区域到达",
                      "zone_commitment": "区域投入", "fleet_geometry": "队形距离", "ship_appearance": "舰船出场",
                      "observed_spawn_orientation": "开局观测方位", "consumable_window": "消耗品窗口", "plan_arrival": "计划到位",
                      "local_model_summary": "本地模型覆盖", "local_model_checkpoint": "本地模型复看点",
                      "ordinary_overview": "对局范围", "replay_relative_position": "相对位置",
                      "replay_map_control": "地图控制", "replay_hull_angle": "舰体夹角",
                      "replay_action_frequency": "观测事件频率", "replay_context_coverage": "录像细节覆盖",
                      "replay_ship_phase": "分时段表现", "replay_consumable_timing": "消耗品启用时间",
                      "replay_consumable_overlap": "同类消耗品支援窗口", "replay_damage_efficiency": "伤害效率",
                      "replay_damage_composition": "伤害构成", "replay_performance_coverage": "表现数据覆盖",
                      "replay_minimap_sequence":"连续小地图", "replay_fleet_key":"图中舰名与阵容",
                      "replay_spotting_changes":"本舰亮灭窗口", "replay_gunnery":"分弹种命中",
                      "replay_target_impact":"目标与伤害占比", "replay_ship_benchmark":"同舰服务器基准",
                      "replay_battle_contribution":"本局贡献", "replay_damage_window":"连续伤害窗口",
                      "replay_team_identity":"敌我阵容", "replay_damage_over_time":"起火与进水",
                      "replay_incoming_damage":"本舰关键承伤", "replay_engagement_loadouts":"交火舰实装配件",
                      "replay_ship_traits":"舰船特性", "replay_battle_settlement":"战斗结算",
                      "replay_team_score_flow":"比分走势", "replay_capture_pressure":"争点进程", "replay_terrain_profile":"地图地形"}
            for kind in sorted({row.get("kind") for row in rows if isinstance(row.get("kind"), str)}):
                self.kind_filter.addItem(labels.get(kind, kind), kind)
            index = self.kind_filter.findData(current)
            self.kind_filter.setCurrentIndex(max(0, index))
            self.kind_filter.blockSignals(False)
            self.source_label.setText(f"{len(rows)} 条证据 · 计划{'已选' if plan else '未选'} · 本方条件{'已选' if own else '未选'}")
            self.preview_button.setEnabled(bool(rows) and not self.busy)
            self.offline_button.setEnabled(bool(rows) and not self.busy)
        except Exception:
            self.source_label.setText("暂无可用分析")
            self.preview_button.setEnabled(False)
            self.offline_button.setEnabled(False)
        self._update_selection_label()

    def _watch_external(self):
        if self.prepared is None or not self.isVisible():
            return
        try:
            if self._fingerprint() != self._preview_fingerprint:
                self._invalidate("当前分析、计划或本方条件已改变，请重新预览。")
                self._update_selection_label()
        except Exception:
            self._invalidate("当前来源暂不可用，请重新生成预览。")

    @staticmethod
    def _context_text(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True) if value else ""

    def _images_changed(self, *_):
        refresh = self.prepared is not None and not self.busy
        self._input_changed()
        if refresh:
            self.preview_request()

    def _show_images(self, prepared):
        from .replay_evidence_frames import _clock_label
        manifest = prepared['payload'].get('images', [])
        for index, button in enumerate(self.image_buttons):
            button.setVisible(index < len(manifest))
            if index < len(manifest):
                ref = manifest[index]
                pixmap = QPixmap()
                pixmap.loadFromData(base64.b64decode(prepared['image_data'][index]), 'PNG')
                button.setIcon(QIcon(pixmap))
                clocks = ref.get('frame_clocks') or [ref['clock']]
                if len(clocks) > 1:
                    button.setText(f'{index+1}　{_clock_label(ref["clock_domain"],clocks[0]).split()[-1][:5]}'
                                   f'–{_clock_label(ref["clock_domain"],clocks[-1]).split()[-1][:5]}')
                else:
                    button.setText(_clock_label(ref['clock_domain'], ref['clock']))
                button.setToolTip('查看原图 · ' + ref['evidence_id'])
        self.image_bar.setVisible(bool(manifest))

    def _use_review_budget(self):
        self.output_tokens.setValue(DEFAULT_MAX_OUTPUT_TOKENS)
        self.timeout.setValue(DEFAULT_TIMEOUT_SECONDS)
        self.preview_request()

    def _open_image(self, index):
        if self.prepared is None or index >= len(self.prepared.get('image_data', ())):
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(self.image_buttons[index].text())
        layout = QVBoxLayout(dialog)
        label, pixmap = QLabel(), QPixmap()
        pixmap.loadFromData(base64.b64decode(self.prepared['image_data'][index]), 'PNG')
        scroll = QScrollArea()
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll.setWidget(label)
        layout.addWidget(scroll, 1)
        size = self.screen().availableGeometry().size() * .84
        dialog.resize(size.boundedTo(pixmap.size() + QSize(32, 92)))
        def zoom(original):
            displayed = pixmap if original else pixmap.scaled(scroll.viewport().size(),
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            label.setPixmap(displayed)
            label.resize(displayed.size())
        toggle = QCheckBox('查看原始尺寸')
        toggle.toggled.connect(zoom)
        layout.addWidget(toggle)
        QTimer.singleShot(0, dialog, lambda: zoom(False))
        layout.addWidget(_plain_label('引用证据  ' + self.prepared['payload']['images'][index]['evidence_id']))
        dialog.exec()

    def preview_request(self, *, with_images=True):
        if self.busy:
            return None
        self._invalidate("正在生成新的预览。")
        try:
            contexts = self._contexts()
            analysis, plan, own = contexts
            selected, counts = self._selected_ids(analysis)
            images = []
            if with_images and self.include_images.isChecked() and self.image_provider is not None:
                by_id = {row.get('id'): row for row in self._evidence(analysis)}
                rows = [by_id[key] for key in selected if key in by_id]
                images = self.image_provider(analysis, rows)
            prepared = prepare_evidence(analysis, self.question.toPlainText(), evidence_ids=selected,
                anonymize=self.anonymize.isChecked(),
                plan=self._context_text(plan) if self.include_plan.isChecked() else "",
                own_conditions=self._context_text(own) if self.include_own.isChecked() else "", images=images)
            if self.report_context_getter is not None:
                prepared["report_context"] = deepcopy(self.report_context_getter())
            if analysis.get('report_contract') in SINGLE_BATTLE_CONTRACTS:
                row = next((r for r in self._evidence(analysis) if r.get('kind') == 'replay_battle_settlement'), None)
                if row and isinstance(row.get('details'), dict):
                    prepared.setdefault('report_context', {})['settlement'] = deepcopy(row['details'])
            key = self._effective_key()
            if key and key in json.dumps({k: v for k, v in prepared.items() if k != 'image_data'}, ensure_ascii=False):
                raise AIError("请求内容包含当前配置密钥，请移除后重新预览。")
            self.prepared = prepared
            self._preview_context_hash = self._context_hash(contexts)
            self._preview_fingerprint = self._fingerprint(contexts)
            self.preview_text.setPlainText(prepared["preview"]["payload_text"])
            self._show_images(prepared)
            evaluation = prepared['payload'].get('report_contract') in SINGLE_BATTLE_CONTRACTS
            self.review_budget_button.setVisible(evaluation and (
                self.output_tokens.value() != DEFAULT_MAX_OUTPUT_TOKENS or self.timeout.value() != DEFAULT_TIMEOUT_SECONDS))
            destination = self._preview_destination(key)
            frame_count = sum(len(image.get('frame_clocks') or [image['clock']]) for image in images)
            coverage = (f"全场分镜 {frame_count} 帧，分 {len(images)} 张图发送，可点开查看。" if frame_count > len(images) else
                        f"附 {len(images)} 张小地图，可点击查看原图。" if images else
                        "所选证据缺少可配图的舰位观测，使用文字。" if with_images and self.include_images.isChecked()
                        else "仅使用文字。")
            self.preview_summary.setText(f"{destination}\n已选 {counts['selected']} / {counts['total']} 条，省略 {counts['omitted']} 条。{coverage}")
            self.preview_summary.setToolTip(prepared['preview']['privacy_note'])
            self.send_button.setEnabled(True)
            self.manual_button.setEnabled(True)
            self.tabs.setCurrentIndex(2)
            self._say("请求预览已就绪")
            return prepared
        except Exception as error:
            self.prepared = None
            self._say(str(error))
            return None

    def _preview_destination(self, key):
        try:
            config = self._config()
        except AIError:
            return "发送前请在服务设置中填写地址、模型与密钥。"
        limit = f"输出上限 {config.max_output_tokens} token" if config.max_output_tokens else "输出上限由服务决定"
        time_limit = f"总超时 {config.timeout_seconds:g} 秒" if config.timeout_seconds else "总超时不限"
        details = f"{config.model} · {config.endpoint} · {limit} · {time_limit}"
        if config.supports_reasoning_effort:
            details += f" · 推理强度 {self.reasoning_effort.currentText()}"
        if not key:
            details += " · 待填写 API 密钥"
        return details

    def _require_fresh(self):
        if self.prepared is None or self._fingerprint() != self._preview_fingerprint:
            self._invalidate("来源或输入已变化，请先重新预览。")
            raise AIError("请先重新预览当前证据，旧预览不能发送或导入结果。")

    def save_settings(self):
        if self.busy:
            return None
        try:
            config = self._config()
            key = self.key_input.text().strip()
            active_key = key or self.secrets.get(credential_name(config.base_url))
            if active_key and active_key in json.dumps(self._settings_payload(), ensure_ascii=False):
                raise AIError("配置文本中包含密钥，请只在密码框中填写。")
            if key:
                AIClient(config, key)  # Validate syntax only; constructor never sends.
                self.secrets.set(credential_name(config.base_url), key)
            settings = self._settings_payload()
            settings["output_limit_mode"] = "custom" if config.max_output_tokens else "provider"
            settings["timeout_mode"] = "custom" if config.timeout_seconds else "unlimited"
            record = self.store.save_document("ai_settings", "AI 服务配置", settings, self._settings_id)
            self._settings_id = record["id"]
            self.key_input.clear()
            self._input_changed()
            self._update_key_status()
            self.changed.emit(record)
            self._say("配置已保存 · 未联网")
            return record
        except Exception as error:
            self._say(str(error))
            return None

    def _test_or_cancel_connection(self):
        if self._worker is not None and self._worker.is_connection_test:
            self.cancel()
        else:
            self.test_connection()

    def test_connection(self):
        if self.busy:
            return False
        try:
            config = self._config()
            key = self._effective_key()
            if not key:
                raise AIError("请填写当前服务的 API 密钥。")
            if key in json.dumps(self._settings_payload(), ensure_ascii=False):
                raise AIError("配置文本中包含密钥，请只在密码框中填写。")
            AIClient(config, key)
            worker = _AIWorker(config, key, None, self)
            self._worker = worker
            worker.succeeded.connect(self._connection_test_passed)
            worker.failed.connect(lambda message: self._say_connection("连接测试失败 · " + message))
            worker.cancelled.connect(self._connection_test_cancelled)
            worker.finished.connect(self._on_finished)
            self.generation_progress.reset()
            self._set_busy_controls(True)
            self._say_connection("正在测试连接 · " + config.model)
            worker.start()
            return True
        except Exception as error:
            self._say_connection(str(error))
            return False

    def _connection_test_passed(self, result):
        if self._worker is not None and self._worker.cancel_event.is_set():
            self._connection_test_cancelled()
            return
        self._say_connection(f"连接测试通过 · {result['model']} 已正常返回文本。")

    def _connection_test_cancelled(self):
        self._say_connection("连接测试已取消。")

    def send_prepared(self):
        if self.busy:
            return False
        try:
            self._require_fresh()
            try:
                config = self._config()
                key = self._effective_key()
                if not key:
                    raise AIError("请填写当前服务的 API 密钥。")
                AIClient(config, key)
            except AIError:
                self.tabs.setCurrentIndex(0)
                raise
            self._running_prepared = deepcopy(self.prepared)
            self._running_context_hash = self._preview_context_hash
            worker = _AIWorker(config, key, self._running_prepared, self)
            self._worker = worker
            worker.succeeded.connect(self._on_result)
            worker.failed.connect(self._on_failure)
            worker.cancelled.connect(self._on_cancelled)
            worker.progress.connect(self._update_generation_stage)
            worker.finished.connect(self._on_finished)
            self._set_busy_controls(True)
            self.generation_progress.start()
            self._say("")
            worker.start()
            return True
        except Exception as error:
            self._say(str(error))
            return False

    def _set_busy_controls(self, value):
        testing = value and self._worker is not None and self._worker.is_connection_test
        self.settings_fields.setEnabled(not value)
        self.save_settings_button.setEnabled(not value)
        self.test_connection_button.setEnabled(not value or testing)
        self.test_connection_button.setText("取消测试" if testing else "测试连接")
        self.evidence_page.setEnabled(not value)
        self.include_images.setEnabled(not value)
        self.send_button.setEnabled(not value and self.prepared is not None)
        self.manual_button.setEnabled(not value and self.prepared is not None)
        self.cancel_button.setVisible(value and not testing)
        self.cancel_button.setEnabled(value and not testing)
        self.review_button.setEnabled(not value and self.current_run is not None)

    def _update_generation_stage(self, step):
        if self._worker is not None and not self._worker.cancel_event.is_set():
            self.generation_progress.stage(step)

    def generate_offline_brief(self):
        if self.busy:
            return None
        prepared = self.preview_request(with_images=False)
        if prepared is None:
            return None
        try:
            result = offline_brief(prepared)
            return self._save_result(result, prepared, self._preview_context_hash)
        except Exception as error:
            self._say(str(error))
            return None

    def cancel(self):
        if self._worker is not None:
            self._worker.cancel()
            self.cancel_button.setEnabled(False)
            if self._worker.is_connection_test:
                self.test_connection_button.setEnabled(False)
                self._say_connection("正在取消连接测试…")
            else:
                self.generation_progress.cancelling()

    def _on_failure(self, message):
        self.generation_progress.finish("生成失败")
        self._say("AI 请求未完成：" + message)

    def _on_cancelled(self):
        self.generation_progress.finish("已取消")
        self._say("AI 请求已取消，未保存为完成结果。")

    def _on_finished(self):
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        self._running_prepared = None
        self._running_context_hash = None
        self._watch_external()
        self._set_busy_controls(False)
        self.idle.emit()
        if self._close_pending:
            self._close_pending = False
            self.close()

    def _save_result(self, result, prepared, context_hash):
        payload = deepcopy(result)
        payload["request"] = deepcopy(prepared["payload"])
        payload["input_context_fingerprint"] = context_hash
        if isinstance(prepared.get("report_context"), dict):
            payload["report_context"] = deepcopy(prepared["report_context"])
        if result.get('battle_assessment') and prepared.get('image_data'):
            from ..report_visuals import build_report_visuals
            if visuals := build_report_visuals(result, prepared):
                payload['report_visuals'] = visuals
        manual = payload.get("source_type") == "manual_import"
        offline = payload.get("source_type") == "offline_brief"
        career = prepared["payload"].get("report_contract") in ("career_profile_v1", "career_profile_v2")
        name = ("离线规则简报" if offline else "AI 建议 · 离线手动导入" if manual
                else "AI 建议 · " + (payload.get("metadata", {}).get("model") or "所选服务"))
        kind = "career_ai" if career else "ai"
        record = self.store.save_run(kind, "AI 战绩画像" if career else name[:1000], payload, input_fingerprint={
            "context": context_hash, "prepared_hash": prepared["prepared_hash"], "evidence_hash": prepared["evidence_hash"]})
        self.current_run = record
        self._render_result(payload)
        self.run_saved.emit(record)
        self.changed.emit(record)
        self.review_button.setEnabled(not self.busy)
        self.tabs.setCurrentIndex(3)
        self._say("AI 战绩画像已保存" if career else "离线规则简报已保存 · 未联网" if offline
                  else "离线响应已保存 · 未联网" if manual else "AI 简报已保存 · 待人工复核")
        return record

    def _on_result(self, result):
        if self._worker is not None and self._worker.cancel_event.is_set():
            self._on_cancelled()
            return
        try:
            self.generation_progress.stage("saving")
            self._save_result(result, self._running_prepared, self._running_context_hash)
            self.generation_progress.finish("生成完成", completed=True)
        except Exception:
            self.generation_progress.finish("报告保存失败")
            self._render_result(result)
            self.tabs.setCurrentIndex(3)
            self.current_run = None
            self.review_button.setEnabled(False)
            self._say("已收到响应，但结果保存失败；当前内容尚未进入分析历史。")

    def _render_result(self, result):
        offline = result.get("source_type") == "offline_brief"
        source = "离线规则简报" if offline else "离线导入" if result.get("source_type") == "manual_import" else "AI 战术简报"
        metadata = result.get("metadata") or {}
        if metadata.get("from_cache"):
            source += " · 缓存"
        brief = compact_brief(result)
        status = f" · {brief['review_count']} 项待核查" if brief['review_count'] else " · 待人工复核"
        self.result_summary.setText(source + status)
        blocks = ['<style>body{font-family:"Microsoft YaHei";font-size:13px;} '
                  'h2{font-size:17px;margin:6px 0;} h3{font-size:13px;margin:12px 0 5px;} '
                  'p{margin:5px 0;} .ref{font-size:10px;color:#7890a2;}</style><h2>战术简报</h2>']
        def show_row(row, prefix=""):
            return ("<p>" + escape(prefix + row["text"]) + '</p><p class="ref">'
                    + escape(row["location"]) + "</p>")
        if brief["conclusion"]:
            blocks.append(show_row(brief["conclusion"]))
        for key, label in (("findings", "关键发现"), ("actions", "下次改进")):
            if brief[key]:
                blocks.append("<h3>" + label + "</h3>")
                for index, row in enumerate(brief[key], 1):
                    blocks.append(show_row(row, f"{index}. "))
        if brief["unknowns"]:
            label = "待确认" if any(any(word in row["text"] for word in ("未知", "不确定", "无法", "不能", "未确认", "待确认"))
                                     for row in brief["unknowns"]) else "补充判断"
            blocks.append("<h3>" + label + "</h3>")
            blocks.extend(show_row(row) for row in brief["unknowns"])
        if not any(brief[key] for key in ("conclusion", "findings", "actions")):
            blocks.append("<p>暂无引用有效的内容</p>")
        if brief["omitted"] or brief["shortened"]:
            blocks.append('<p class="ref">正文已精简</p>')
        if result.get('battle_assessment'):
            from ..report_presentation import render_report
            self.result_text.setHtml(render_report(result, (result.get('report_context') or {}).get('layout'),
                                                  interactive=True))
        else:
            self.result_text.setHtml("".join(blocks))
        self.details_button.setChecked(False)
        sections = []
        facts={row.get('id'):row for row in (result.get('request') or {}).get('evidence',[]) if isinstance(row,dict)}
        for key, label in (("observations", "证据转述"), ("interpretations", "解释"), ("suggestions", "建议")):
            lines=[]
            for row in result.get(key,[]):
                locations=list(dict.fromkeys(evidence_location(facts[ref]) for ref in row.get('evidence_ids',[]) if ref in facts))
                lines.append(row['text']+('\n'+ '；'.join(locations) if locations else ''))
            sections.append(label + "\n" + ("\n\n".join(lines) if lines else "暂无有效引用"))
        self.full_result_text.setPlainText("\n\n".join(sections))
        reasons={'unknown_evidence_ids':'引用的比赛记录不存在','missing_evidence_ids':'未提供比赛依据'}
        review = [row.get('text','')+'\n待核查：'+'；'.join(reasons.get(reason,'引用需要核对') for reason in row.get('review_reasons',[]))
                  for row in result.get("needs_review", [])]
        if result.get("validation_errors"):
            review.append("返回内容不完整，请重新生成或核对完整内容。")
        self.review_text.setPlainText("\n\n".join(review) if review else "引用检查通过，可继续复核内容。")

    def import_response_text(self, text):
        if self.busy:
            return None
        try:
            self._require_fresh()
            key = self._effective_key()
            if key:
                text = text.replace(key, "[REDACTED]")
            result = import_manual_response(self.prepared, text)
            return self._save_result(result, self.prepared, self._preview_context_hash)
        except Exception as error:
            self._say(str(error))
            return None

    def import_response_file(self):
        if self.busy or self.prepared is None:
            return
        filename, _ = QFileDialog.getOpenFileName(self, "离线导入响应 JSON（不会联网）", "", "JSON / 文本 (*.json *.txt)")
        if not filename:
            return
        try:
            with Path(filename).open("rb") as stream:
                data = stream.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise AIError("响应文件超过 256 KiB 上限。")
            self.import_response_text(data.decode("utf-8-sig"))
        except Exception:
            self._say("无法读取响应文件，请使用不超过 256 KiB 的 UTF-8 文本。")

    def add_review(self):
        if self.current_run is None or self.busy:
            return None
        decision, comment = self.review_decision.currentData(), self.review_comment.text().strip()
        if decision == "comment" and not comment:
            self._say("请填写复核备注。")
            return None
        try:
            record = self.store.add_review(self.current_run["id"], decision, comment)
            self.changed.emit(self.current_run)
            self._say("复核已保存")
            self.review_comment.clear()
            return record
        except Exception:
            self._say("复核保存失败，请重试。")
            return None

    def closeEvent(self, event):
        if self.busy:
            self._close_pending = True
            self.cancel()
            event.ignore()
        else:
            event.accept()

    def showEvent(self, event):
        super().showEvent(event)
        self._watch_external()
