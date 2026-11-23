"""Visual report composition over immutable, explicitly selected analysis runs."""
from __future__ import annotations

from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime
import math
import json
import os
from pathlib import Path
import tempfile

from PySide6.QtCore import QMarginsF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPageLayout, QPageSize, QPdfWriter, QTextDocument, QTextTable
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QSplitter, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget)

from wows_training.ai_analysis import AIError, LOCAL_MODEL_KINDS, LOCAL_MODEL_SUMMARY, SINGLE_BATTLE_CONTRACT, SINGLE_BATTLE_CONTRACTS, digest
from wows_training.analysis_perspective import (
    COMPETITION, PERSPECTIVE_LABELS, RANDOM_BATTLE, REPLAY_CONTEXT_KINDS, evidence_priority,
)
from wows_training.battle_review import REPORT_EVIDENCE_REVISION
from wows_training.report_presentation import PALETTES, build_report, export_report, normalize_options, render_report
from .fleet_shell import FleetPageHeader
from .layered_panel import LayeredPanel
from .report_preview import ReportEmptyPage
from .ui_components import Disclosure


FOCUS = (
    ("lineup", "选船阵容", {"ship_appearance", "ordinary_lineup"}),
    ("opening", "开局走位", {"observed_spawn_orientation", "region_dwell", "zone_commitment"}),
    ("coordination", "协同支援", {"region_transfer", "support_arrival", "fleet_geometry", "consumable_window", "plan_arrival"}),
    ("player", "选手发挥", {"ship_appearance", "region_dwell", "region_transfer", "support_arrival", "consumable_window", "plan_arrival", "ordinary_result", "ordinary_event"}),
    ("battle", "本局记录", {"ordinary_overview", "ordinary_lineup", "ordinary_result", "ordinary_event"}),
    ("local_model", "本地模型", LOCAL_MODEL_KINDS),
    ("replay_context", "录像细节", REPLAY_CONTEXT_KINDS),
)
TEMPLATES = (("brief", "战术简报"), ("review", "赛后复盘"), ("player", "选手点评"))


def _label(text="", name=""):
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    return widget


def _button(text, callback, name=""):
    widget = QPushButton(text)
    widget.setObjectName(name)
    widget.clicked.connect(callback)
    return widget


def _balanced_evidence(rows, perspective=None):
    """Interleave kinds inside battles, then battles, before the bounded request cap."""
    battles = OrderedDict()
    for row in rows:
        key = str(row.get("battle_id") or row.get("arena_id") or "unknown")
        kinds = battles.setdefault(key, OrderedDict())
        kinds.setdefault(str(row.get("kind") or "unknown"), deque()).append(row)
    ordered = []
    queues = []
    for kinds in battles.values():
        keys = list(kinds)
        if perspective is not None:
            keys.sort(key=lambda kind: evidence_priority(kind, perspective))
        group = deque()
        while any(kinds.values()):
            for kind in keys:
                values = kinds[kind]
                if values:
                    group.append(values.popleft())
        queues.append(group)
    while any(queues):
        for values in queues:
            if values:
                ordered.append(values.popleft())
    if perspective is not None:
        # With many matches and a small request cap, a full first pass over
        # matches would otherwise send only the same first detail category.
        detail_samples = {}
        for row in ordered:
            if row.get("kind") in REPLAY_CONTEXT_KINDS:
                detail_samples.setdefault(row["kind"], row)
        featured = sorted(detail_samples.values(), key=lambda row: evidence_priority(row["kind"], perspective))
        used = {id(row) for row in featured}
        return featured + [row for row in ordered if id(row) not in used]
    return ordered


def _player_key(participant):
    confirmed = participant.get("confirmed_player_id")
    return "confirmed:" + str(confirmed) if confirmed else "name:" + str(participant.get("name") or "")


class ReportStudioPanel(QWidget):
    status = Signal(str)
    idle = Signal()

    def __init__(self, host, ai_panel, parent=None):
        super().__init__(parent or host)
        self.host, self.ai_panel, self.store = host, ai_panel, ai_panel.store
        self._original_analysis_getter = ai_panel.analysis_getter
        self._loading = True
        self._layout_id = None
        self._sources = {}
        self._current_run = None
        self._rendered_payload = None
        self._last_generation = None
        self._source_error = ""
        self._enriched_key = None
        self._enriched_payload = None
        self._model_focus_source = None
        self._replay_focus_source = None
        self._perspective_source = object()
        self._build_ui()
        self._load_preferences()
        ai_panel.analysis_getter = self.analysis_payload
        ai_panel.report_context_getter = self.report_context
        ai_panel.image_provider = self._replay_images
        ai_panel.run_saved.connect(self.show_run)
        ai_panel.changed.connect(self._review_changed)
        ai_panel.status.connect(self._engine_status)
        ai_panel.idle.connect(self._idle)
        self._loading = False
        self.refresh_sources()
        self._refresh_question()
        self._refresh_preview()
        self._watcher = QTimer(self)
        self._watcher.setInterval(400)
        self._watcher.timeout.connect(self._sync_busy)
        self._watcher.start()

    @property
    def busy(self):
        return self.ai_panel.busy

    @property
    def current_run(self):
        return deepcopy(self._current_run)

    def cancel(self):
        self.ai_panel.cancel()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 14)
        root.setSpacing(10)
        root.addWidget(FleetPageHeader("报告工作台", "warehouse"))
        bar = QHBoxLayout()
        self.history_combo = QComboBox()
        self.history_combo.setObjectName("reportHistory")
        self.history_combo.setMinimumWidth(150)
        self.history_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.history_combo.setMinimumContentsLength(12)
        self.history_combo.currentIndexChanged.connect(self._load_history)
        bar.addWidget(self.history_combo, 1)
        bar.addWidget(_button("AI 连接", lambda: self.open_engine(0), "reportAISettings"))
        bar.addWidget(_button("证据选择", lambda: self.open_engine(1)))
        self.review_button = _button("人工复核", lambda: self.open_engine(3))
        bar.addWidget(self.review_button)
        root.addLayout(bar)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        composition = QWidget()
        body = QHBoxLayout(composition)
        body.setContentsMargins(0, 0, 0, 0)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        body.addWidget(self.splitter)
        controls = QWidget()
        controls.setObjectName("reportOptions")
        self.controls_layout = QVBoxLayout(controls)
        self.controls_layout.setContentsMargins(0, 0, 12, 0)
        self.controls_layout.setSpacing(10)
        self.template_group = QButtonGroup(self)
        self.template_group.setExclusive(True)
        self.template_buttons = {}
        self.controls_layout.addWidget(_label("报告样式", "sectionTitle"))
        templates = QHBoxLayout()
        templates.setSpacing(5)
        for index, (key, title) in enumerate(TEMPLATES):
            item = QPushButton(title)
            item.setObjectName("reportTemplate")
            item.setCheckable(True)
            item.setMinimumHeight(38)
            item.setMinimumWidth(0)
            item.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.template_group.addButton(item, index)
            self.template_buttons[key] = item
            templates.addWidget(item)
        self.template_buttons["brief"].setChecked(True)
        self.template_group.idClicked.connect(self._template_changed)
        self.controls_layout.addLayout(templates)
        self.controls_layout.addWidget(_label("分析来源", "sectionTitle"))
        sources_row = QHBoxLayout()
        self.source_combo = QComboBox()
        self.source_combo.setObjectName("reportSource")
        self.source_combo.setMinimumContentsLength(12)
        self.source_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.source_combo.currentIndexChanged.connect(self._source_changed)
        sources_row.addWidget(self.source_combo, 1)
        sources_row.addWidget(_button("更新", self.refresh_sources))
        self.controls_layout.addLayout(sources_row)
        self.source_summary = _label("", "subtitle")
        self.controls_layout.addWidget(self.source_summary)
        perspective_row = QHBoxLayout()
        perspective_row.addWidget(_label("分析视角"))
        self.perspective_combo = QComboBox()
        self.perspective_combo.setObjectName("reportAnalysisPerspective")
        self.perspective_combo.setAccessibleName("分析视角")
        self.perspective_combo.setToolTip("按个人决策或团队分工组织复盘")
        for key, label in PERSPECTIVE_LABELS.items():
            self.perspective_combo.addItem(label, key)
        self.perspective_combo.setCurrentIndex(self.perspective_combo.findData(COMPETITION))
        self.perspective_combo.currentIndexChanged.connect(self._generation_changed)
        perspective_row.addWidget(self.perspective_combo, 1)
        self.controls_layout.addLayout(perspective_row)
        self.prepare_source_button = _button("打开赛训工作室", lambda: self.host.show_page(4))
        self.controls_layout.addWidget(self.prepare_source_button)
        self.player_combo = QComboBox()
        self.player_combo.setObjectName("reportPlayer")
        self.player_combo.currentIndexChanged.connect(self._generation_changed)
        self.controls_layout.addWidget(self.player_combo)
        self.controls_layout.addWidget(_label("分析重点", "sectionTitle"))
        chips = QGridLayout()
        chips.setHorizontalSpacing(6)
        self.focus_buttons = {}
        for index, (key, text, _) in enumerate(FOCUS):
            item = QPushButton(text)
            item.setObjectName("reportFocus")
            item.setCheckable(True)
            item.setChecked(key not in ("player", "battle", "local_model", "replay_context"))
            item.toggled.connect(self._generation_changed)
            self.focus_buttons[key] = item
            if key in ('battle', 'local_model', 'replay_context'):
                item.hide()
            chips.addWidget(item, index // 2, index % 2)
        self.controls_layout.addLayout(chips)
        self.question_edit = QPlainTextEdit()
        self.question_edit.setObjectName("reportQuestion")
        self.question_edit.setMaximumHeight(72)
        self.question_edit.setPlaceholderText("补充关注点（可不填）")
        self.question_edit.textChanged.connect(self._generation_changed)
        self.controls_layout.addWidget(self.question_edit)
        actions = QHBoxLayout()
        self.offline_button = _button("生成离线简报", self.generate_offline, "reportOffline")
        self.ai_button = _button("AI 分析预览", self.preview_ai, "primary")
        actions.addWidget(self.offline_button)
        actions.addWidget(self.ai_button)
        self.controls_layout.addLayout(actions)
        appearance = QWidget()
        form = QFormLayout(appearance)
        form.setContentsMargins(0, 3, 0, 3)
        self.title_edit = QLineEdit("战术简报")
        self.title_edit.setMaxLength(100)
        self.subtitle_edit = QLineEdit()
        self.subtitle_edit.setMaxLength(180)
        self.subtitle_edit.setPlaceholderText("赛事 / 队伍 / 备注")
        self.accent_combo = QComboBox()
        self.accent_combo.addItem("蓝粉 · 档案", "light")
        self.accent_combo.addItem("深蓝 · 展示", "navy")
        form.addRow("标题", self.title_edit)
        form.addRow("副标题", self.subtitle_edit)
        form.addRow("色调", self.accent_combo)
        modules = QWidget()
        module_layout = QHBoxLayout(modules)
        module_layout.setContentsMargins(0, 0, 0, 0)
        self.section_checks = {}
        for key, name in (("observations", "发现"), ("interpretations", "判断"), ("suggestions", "建议")):
            item = QCheckBox(name)
            item.setChecked(True)
            item.toggled.connect(self._presentation_changed)
            self.section_checks[key] = item
            module_layout.addWidget(item)
        form.addRow("模块", modules)
        self.evidence_check = QCheckBox("显示比赛与时间定位")
        self.evidence_check.setChecked(False)
        self.evidence_check.toggled.connect(self._presentation_changed)
        form.addRow(self.evidence_check)
        form.addRow(_button("记住我的版式", self.save_preferences))
        for widget in (self.title_edit, self.subtitle_edit):
            widget.textChanged.connect(self._presentation_changed)
        self.accent_combo.currentIndexChanged.connect(self._presentation_changed)
        self.appearance_disclosure = Disclosure("版式与输出", appearance, expanded=True)
        self.controls_layout.addWidget(self.appearance_disclosure)
        self.controls_layout.addStretch()
        self.options_scroll = QScrollArea()
        self.options_scroll.setWidgetResizable(True)
        self.options_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.options_scroll.setWidget(controls)
        self.options_scroll.setMinimumWidth(285)
        self.splitter.addWidget(self.options_scroll)
        output = QWidget()
        output.setMinimumWidth(340)
        output_layout = QVBoxLayout(output)
        output_layout.setContentsMargins(8, 0, 0, 0)
        preview_bar = QHBoxLayout()
        self.preview_caption = _label("版式预览", "sectionTitle")
        preview_bar.addWidget(self.preview_caption, 1)
        self.export_format = QComboBox()
        for caption, key in (("HTML 网页", "html"), ("PDF 文档", "pdf"), ("PNG 长图", "png"), ("Markdown", "md")):
            self.export_format.addItem(caption, key)
        preview_bar.addWidget(self.export_format)
        self.export_button = _button("导出", self.choose_export, "reportExport")
        preview_bar.addWidget(self.export_button)
        output_layout.addLayout(preview_bar)
        self.preview_stack = QStackedWidget()
        self.empty_preview = ReportEmptyPage(self)
        self.preview_stack.addWidget(self.empty_preview)
        self.preview_paper = LayeredPanel(cut=24)
        paper_layout = QVBoxLayout(self.preview_paper)
        paper_layout.setContentsMargins(16, 18, 21, 20)
        self.preview = QTextBrowser()
        self.preview.setObjectName("reportPreview")
        self.preview.setProperty('customEmptyPresentation', True)
        self.preview.setOpenLinks(False)
        from .report_image_preview import attach_report_image_preview
        attach_report_image_preview(self.preview, lambda: self._rendered_payload)
        self.preview.setOpenExternalLinks(False)
        paper_layout.addWidget(self.preview)
        self.preview_stack.addWidget(self.preview_paper)
        output_layout.addWidget(self.preview_stack, 1)
        self.report_status = _label("", "subtitle")
        output_layout.addWidget(self.report_status)
        self.splitter.addWidget(output)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([335, 780])
        self.stack.addWidget(composition)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_bar = QHBoxLayout()
        detail_bar.addWidget(_button("← 返回报告", lambda: self.stack.setCurrentIndex(0)))
        detail_bar.addStretch()
        detail_layout.addLayout(detail_bar)
        detail_layout.addWidget(self.ai_panel, 1)
        self.ai_panel.show()
        self.stack.addWidget(detail)
        self.notice = _label("", "notice")
        self.notice.setVisible(False)
        root.addWidget(self.notice)
        root.addWidget(self.ai_panel.generation_progress)
        self.setStyleSheet("QPushButton#reportTemplate { padding:6px 3px; font-size:12px; } "
                           "QPushButton#reportTemplate:checked, QPushButton#reportFocus:checked { "
                           "border-image:none; background:#CDE2FB; border:1px solid #86ACD4; border-bottom:3px solid #F057A6; color:#174C83; } "
                           "QPushButton#reportFocus { padding:5px 7px; } "
                           "QTextBrowser#reportPreview { padding:0; border:none; } ")

    def _template(self):
        return next((key for key, item in self.template_buttons.items() if item.isChecked()), "brief")

    def _perspective(self):
        return self.perspective_combo.currentData() or COMPETITION

    def options(self):
        return normalize_options({"title": self.title_edit.text().strip() or "战术简报",
            "subtitle": self.subtitle_edit.text().strip(), "template": self._template(),
            "accent": self.accent_combo.currentData(),
            "sections": [key for key, widget in self.section_checks.items() if widget.isChecked()],
            "show_evidence": self.evidence_check.isChecked()})

    def _apply_options(self, options):
        options = normalize_options(options)
        self._loading = True
        self.template_buttons[options["template"]].setChecked(True)
        self.title_edit.setText(options["title"])
        self.subtitle_edit.setText(options["subtitle"])
        self.accent_combo.setCurrentIndex(max(0, self.accent_combo.findData(options["accent"])))
        for key, widget in self.section_checks.items():
            widget.setChecked(key in options["sections"])
        self.evidence_check.setChecked(options["show_evidence"])
        self._loading = False
        self.player_combo.setVisible(self._template() == "player")

    def _load_preferences(self):
        try:
            records = self.store.list_documents("report_layout")
            if records:
                record = records[-1]
                self._layout_id = record["id"]
                self._apply_options(record["payload"]["options"])
        except (OSError, KeyError, ValueError, TypeError):
            self._say("已保存版式无法读取，使用默认版式。")

    def save_preferences(self):
        try:
            options = self._checked_options()
            record = self.store.save_document("report_layout", "报告默认版式", {"options": options}, self._layout_id)
            self._layout_id = record["id"]
            self._say("版式已记住。")
            return record
        except Exception as error:
            self._say(str(error) if isinstance(error, AIError) else "版式保存失败，请重试。")
            return None

    def _checked_options(self):
        options = self.options()
        key = self.ai_panel._effective_key()
        if key and key in json.dumps(options, ensure_ascii=False):
            raise AIError("报告文字中包含当前服务密钥，请移除后重试。")
        return options

    def _source_payload(self):
        key = self.source_combo.currentData()
        if key is None:
            value = self._original_analysis_getter() or {}
            return deepcopy(value)
        run = self.store.get_run(key)
        workspace = getattr(getattr(self.host, "workbench", None), "workspace", None)
        if workspace is not None and workspace.stale_reasons(run):
            raise AIError("该分析的比赛或账号资料已有更新，请在赛训工作室重新分析。")
        payload = run["payload"]
        if run.get("kind") == "single_replay" and payload.get("kind") == "ordinary_battle":
            return self._single_battle_source(key, payload)
        return deepcopy(payload)

    def _single_battle_source(self, run_id, saved):
        """Build a new request for a saved recording without rewriting its historical analysis."""
        chosen = self.player_combo.currentData() if self._template() == 'player' else None
        subjects = {p.get('entity_id') for p in saved.get('participants', [])
                    if chosen and _player_key(p) == chosen and type(p.get('entity_id')) is int}
        focus = next(iter(subjects)) if len(subjects) == 1 else saved.get('focus_entity_id')
        previous_model = [r for r in saved.get('evidence', []) if r.get('kind') in LOCAL_MODEL_KINDS]
        if focus is None:
            ids = next((r['entity_ids'] for r in previous_model if len(r.get('entity_ids', [])) == 1), [])
            focus = ids[0] if ids else None
        key = (run_id, focus)
        if self._enriched_key == key:
            return deepcopy(self._enriched_payload)
        result = dict(saved, report_contract=saved.get('report_contract') or SINGLE_BATTLE_CONTRACT)
        if (saved.get('report_evidence_revision') != REPORT_EVIDENCE_REVISION
                or saved.get('report_contract') != SINGLE_BATTLE_CONTRACT or focus != saved.get('focus_entity_id')):
            snapshot = saved.get('input_snapshot') or {}
            views = [v for group in snapshot.get('inputs', []) for v in group.get('views', [])]
            library = getattr(self.host, 'library', None)
            if not views and hasattr(library, 'list_analysis_versions') and not snapshot.get('merged_recordings'):
                shas = {r.get('source_sha') for r in saved.get('evidence', []) if r.get('source_sha')}
                if len(shas) == 1:
                    sha = shas.pop()
                    versions = library.list_analysis_versions(sha)
                    if len(versions) == 1:
                        views = [{'sha256':sha, 'revision':versions[0]['revision']}]
            if len(views) == 1 and library is not None and self.ai_panel.bundle:
                sha, revision = views[0].get('sha256'), views[0].get('revision')
                if type(revision) is int and sha:
                    from ..battle_review import build_battle_evidence
                    report = library.get_report(sha, revision)
                    if report.get('sha256') != sha:
                        raise AIError('当前录像来源与保存记录不一致。')
                    report = self._hydrate_replay_result(library, sha, report)
                    report = self.ai_panel.bundle.presentation_report(report)
                    result = build_battle_evidence(report, self.host.catalog, random_focus=True, focus_entity_id=focus)
                    result['input_snapshot'] = snapshot or {'inputs':[{'battle_id':result['battles'][0]['battle_id'], 'views':views}]}
                    result['evidence'].extend(row for row in previous_model if row.get('source_sha') == sha
                                              and (focus is None or focus in row.get('entity_ids', [])))
        self._enriched_key, self._enriched_payload = key, result
        return deepcopy(result)

    @staticmethod
    def _hydrate_replay_result(library, sha, report):
        from ..ordinary_replay import hydrate_performance, needs_performance_refresh
        from ..replay_loadout import replay_loadout_pending
        if needs_performance_refresh(report) or replay_loadout_pending(report):
            archived = library.root / 'raw_replays' / (sha + '.wowsreplay')
            paths = [archived, *(Path(entry['path']) for entry in (library.find_replay(sha) or {}).get('sources', []))]
            for path in paths:
                if path.is_file():
                    try:
                        return hydrate_performance(report, path)
                    except (ValueError, OSError):
                        continue
        return report

    def _replay_images(self, analysis, selected_rows):
        from .replay_evidence_frames import library_evidence_frames
        library = getattr(self.host, 'library', None)
        if library is None or self.ai_panel.bundle is None:
            return []
        return library_evidence_frames(analysis, selected_rows, library=library, bundle=self.ai_panel.bundle,
                                      recordings=getattr(self.host, 'recording_store', None),
                                      ship_catalog=getattr(self.host, 'catalog', {}))

    def analysis_payload(self):
        payload = self._source_payload()
        rows = self.ai_panel._evidence(payload)
        selected = [key for key, item in self.focus_buttons.items() if item.isChecked()]
        if not selected:
            raise AIError("请选择至少一个分析重点。")
        kinds = set().union(*(kinds for key, _, kinds in FOCUS if key in selected))
        rows = [row for row in rows if row.get("kind") in kinds or row.get("kind") == "replay_team_identity"]
        if self._template() == "player":
            chosen = self.player_combo.currentData()
            if not chosen:
                raise AIError("请选择要点评的选手。")
            pairs = {(str(p.get("battle_id")), str(p.get("entity_id")))
                     for p in payload.get("participants", []) if _player_key(p) == chosen}
            seats = {}
            for battle, entity in pairs:
                seats.setdefault(battle, set()).add(entity)
            if any(len(entities) > 1 for entities in seats.values()):
                raise AIError("该选手在同场关联了多个舰位，请在赛训工作室核对身份记录。")
            rows = [row for row in rows if any((str(row.get("battle_id")), str(entity)) in pairs
                    for entity in (row.get("entity_ids") or [row.get("entity_id")]))]
        # The request cap slices the first N rows. Keep the coverage summary
        # and its few replay checkpoints visible even in a full battle roster.
        overview_rows = [row for row in rows if row.get("kind") == "ordinary_overview"]
        model_rows = [row for row in rows if row.get("kind") in LOCAL_MODEL_KINDS]
        model_rows.sort(key=lambda row: row.get("kind") != LOCAL_MODEL_SUMMARY)
        other_rows = [row for row in rows if row.get("kind") not in LOCAL_MODEL_KINDS
                      and row.get("kind") != "ordinary_overview"]
        perspective = self._perspective()
        payload["analysis_perspective"] = perspective
        sequences = [r for r in other_rows if r.get('kind') == 'replay_minimap_sequence']
        payload["evidence"] = overview_rows + model_rows + sequences + _balanced_evidence(
            [r for r in other_rows if r.get('kind') != 'replay_minimap_sequence'], perspective)
        return payload

    def refresh_sources(self):
        current = self.source_combo.currentData()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem("当前工作室分析", None)
        self._sources = {}
        try:
            for run in self.store.run_index(('collection','counterplans','plan_evaluation','training_comparison','single_replay'), require_evidence=True):
                self._sources[run["id"]] = run
                stamp = str(run.get("created_at", ""))[:16].replace("T", " ")
                self.source_combo.addItem(run["name"] + " · " + stamp, run["id"])
            index = self.source_combo.findData(current)
            if index < 0:
                index = 0
            self.source_combo.setCurrentIndex(index)
            if current is None:
                try:
                    has_current = bool(self.ai_panel._evidence(self._original_analysis_getter() or {}))
                except Exception:
                    has_current = False
                if not has_current and self.source_combo.count() > 1:
                    self.source_combo.setCurrentIndex(1)
        finally:
            self.source_combo.blockSignals(False)
        self._refresh_history()
        self._source_changed()

    def _refresh_history(self):
        current = self._current_run["id"] if self._current_run else None
        self.history_combo.blockSignals(True)
        self.history_combo.clear()
        self.history_combo.addItem("新建报告 / 历史报告", None)
        for run in self.store.run_index(('ai',)):
            title = run.get("report_title") or run["name"]
            stamp = str(run.get("created_at", ""))[:16].replace("T", " ")
            self.history_combo.addItem(str(title) + " · " + stamp, run["id"])
        self.history_combo.setCurrentIndex(max(0, self.history_combo.findData(current)))
        self.history_combo.blockSignals(False)

    def select_single_source(self, run_id):
        index = self.source_combo.findData(run_id)
        if index < 0:
            self.refresh_sources()
            index = self.source_combo.findData(run_id)
        if index >= 0:
            self.history_combo.setCurrentIndex(0)
            self.source_combo.setCurrentIndex(index)
            self.template_buttons['review'].click()
            self.focus_buttons['battle'].setChecked(True)

    def _source_changed(self, *_):
        source = self.source_combo.currentData()
        ordinary = self._sources.get(source, {}).get('kind') == 'single_replay'
        self.focus_buttons['battle'].setVisible(ordinary)
        self.focus_buttons['battle'].setChecked(ordinary)
        previous = self.player_combo.currentData()
        self.player_combo.blockSignals(True)
        self.player_combo.clear()
        self.player_combo.addItem("选择选手", None)
        self._source_error = ""
        model_available = False
        replay_details_available = False
        default_perspective = COMPETITION
        try:
            payload = self._source_payload()
            kinds = {row.get("kind") for row in self.ai_panel._evidence(payload)}
            model_available = ordinary and not kinds.isdisjoint(LOCAL_MODEL_KINDS)
            replay_details_available = not kinds.isdisjoint(REPLAY_CONTEXT_KINDS)
            if payload.get("battle_mode") == "随机战":
                default_perspective = RANDOM_BATTLE
            people = {}
            for person in payload.get("participants", []):
                if person.get("name"):
                    people.setdefault(_player_key(person), person["name"])
            for key, name in sorted(people.items(), key=lambda item: item[1].casefold()):
                self.player_combo.addItem(name, key)
            self.player_combo.setCurrentIndex(max(0, self.player_combo.findData(previous)))
        except Exception as error:
            self._source_error = str(error)
        self.player_combo.blockSignals(False)
        if self._perspective_source != source:
            self._perspective_source = source
            self._select_perspective(default_perspective)
        self._model_focus_source = self._show_optional_focus('local_model', model_available, self._model_focus_source, source)
        self._replay_focus_source = self._show_optional_focus('replay_context', replay_details_available, self._replay_focus_source, source)
        self.player_combo.setVisible(self._template() == "player")
        self._generation_changed()

    def _select_perspective(self, perspective):
        index = self.perspective_combo.findData(perspective)
        if index >= 0:
            blocked = self.perspective_combo.blockSignals(True)
            self.perspective_combo.setCurrentIndex(index)
            self.perspective_combo.blockSignals(blocked)

    def _show_optional_focus(self, key, available, previous_source, source):
        focus = self.focus_buttons[key]
        if not available or focus.isHidden() or previous_source != source:
            blocked = focus.blockSignals(True)
            focus.setChecked(available)
            focus.blockSignals(blocked)
        focus.setVisible(available)
        return source if available else None

    def _template_changed(self, *_):
        if self._loading:
            return
        default_titles = {row[1] for row in TEMPLATES}
        if self.title_edit.text() in default_titles:
            self.title_edit.setText(next(row[1] for row in TEMPLATES if row[0] == self._template()))
        self.player_combo.setVisible(self._template() == "player")
        self._generation_changed()
        self._refresh_preview()

    def _generation_signature(self):
        return digest({"source": self.source_combo.currentData(), "template": self._template(),
            "analysis_perspective": self._perspective(),
            "player": self.player_combo.currentData() if self._template() == "player" else None,
            "focus": [key for key, item in self.focus_buttons.items() if item.isChecked()],
            "question": self.question_edit.toPlainText()})

    def _generation_changed(self, *_):
        if self._loading:
            return
        self._refresh_question()
        self.ai_panel.refresh_sources()
        try:
            payload = self.analysis_payload()
            rows = self.ai_panel._evidence(payload)
            battle_keys = [row.get("battle_id") or row.get("arena_id") for row in rows]
            battle_count = len({key for key in battle_keys if key})
            scope = (f"{battle_count} 场" if all(battle_keys) else f"已标识 {battle_count} 场") if battle_count else "比赛数未知" if rows else "0 场"
            chosen, _ = self.ai_panel._selected_ids(payload)
            self.source_summary.setText(f"{scope} · {len(rows)} 条匹配证据 · 本次 {len(chosen)} 条")
            self.prepare_source_button.setVisible(not rows)
            available = bool(rows)
        except Exception as error:
            self.source_summary.setText(str(error) if self._source_error or isinstance(error, AIError) else "尚无分析来源")
            self.prepare_source_button.setVisible(True)
            available = False
        self.offline_button.setEnabled(available and not self.busy)
        self.ai_button.setEnabled(available and not self.busy)
        self._update_report_status()

    def _refresh_question(self):
        focus = [text for key, text, _ in FOCUS if self.focus_buttons[key].isChecked()]
        title = next(row[1] for row in TEMPLATES if row[0] == self._template())
        question = f"生成{title}，重点分析{'、'.join(focus) or '已选择证据'}。"
        if self._template() == "player" and self.player_combo.currentData():
            question += f"点评对象：{self.player_combo.currentText()}。队伍级记录仅作上下文，不得直接归因到个人。"
        if self._sources.get(self.source_combo.currentData(), {}).get('kind') == 'single_replay':
            question += "沿小地图时间序列复盘本舰如何开局、应对转折并影响结局。直接评价走位、炮术、视野和争点取舍，以结算及同区服同船样本对照解释表现。"
        else:
            question += "用简体中文，默认控制在单页内，合并重复发现并说明判断依据。"
        extra = self.question_edit.toPlainText().strip()
        if extra:
            question += "\n补充关注：" + extra
        if self.ai_panel.question.toPlainText() != question:
            self.ai_panel.question.setPlainText(question)

    def _battle_labels(self, evidence):
        try:
            battles = self.host.library.list_battles()
        except Exception:
            return {}
        by_arena = {str(row.get("arena_id")): row for row in battles if row.get("arena_id") is not None}
        by_key = {str(row.get("id")): row for row in battles}
        labels = {}
        for item in evidence:
            battle_id, arena_id = str(item.get("battle_id") or ""), str(item.get("arena_id") or "")
            row = by_key.get(battle_id) or by_arena.get(arena_id)
            if row is None and battle_id.startswith("arena:"):
                row = by_arena.get(battle_id.split(":", 1)[1])
            if row is None:
                continue
            raw = row.get("date_raw") or ""
            try:
                stamp = datetime.strptime(raw, "%d.%m.%Y %H:%M:%S").strftime("%m-%d %H:%M")
            except (ValueError, TypeError):
                stamp = "日期未知"
            names = getattr(self.host, "map_names", {})
            map_name = names.get(row.get("map_id")) or row.get("map_label") or "地图未知"
            label = f"{stamp} · {map_name}"
            if battle_id:
                labels[battle_id] = label
            if arena_id:
                labels[arena_id] = label
        return labels

    def report_context(self):
        payload = self.analysis_payload()
        anonymous = self.ai_panel.anonymize.isChecked()
        return {"source_name": "所选分析" if anonymous else self.source_combo.currentText(),
            "source_run_id": self.source_combo.currentData(),
            "source_fingerprint": digest(payload),
            "analysis_perspective": self._perspective(),
            "focus": [key for key, item in self.focus_buttons.items() if item.isChecked()],
            "player_key": self.player_combo.currentData() if not anonymous and self._template() == "player" else None,
            "layout": self.options(), "generation_signature": self._generation_signature(),
            "battle_labels": self._battle_labels(self.ai_panel._evidence(payload)), "anonymized": anonymous}

    def _presentation_changed(self, *_):
        if not self._loading:
            self._refresh_preview()

    def _refresh_preview(self):
        try:
            options = self.options()
            report = build_report(self._rendered_payload, options)
            html = render_report(self._rendered_payload, options, interactive=True)
            self.preview.setHtml(html)
            ready = report['ready']
            palette = PALETTES[options['accent'] if ready else 'light']
            self.preview.setStyleSheet("QTextBrowser#reportPreview { background:" + palette['page'] + "; border:none; padding:0; }")
            self.preview_paper.dark = False
            self.preview_paper.update()
            self.empty_preview.set_cover(report['title'], report['template_label'])
            self.preview_stack.setCurrentWidget(self.preview_paper if ready or report['failure'] else self.empty_preview)
            self.export_button.setEnabled(ready)
            self.review_button.setEnabled(self._current_run is not None)
            self.preview_caption.setText("已生成报告" if ready else "上次生成未完成" if report['failure'] else "版式预览")
            self._update_report_status()
        except Exception as error:
            self.export_button.setEnabled(False)
            self._say("版式暂不可用：" + str(error))

    def _update_report_status(self):
        if not self._rendered_payload:
            self.report_status.clear()
            self.report_status.hide()
            return
        context = self._rendered_payload.get("report_context") or {}
        summary = build_report(self._rendered_payload, self.options()).get("summary") or {}
        count = summary.get("battle_count")
        scope = (("已标识 " if not summary.get("battle_count_complete", True) else "") + f"{count} 场") if count is not None else "比赛数未知"
        text = f"报告范围 · {scope}"
        if self.evidence_check.isChecked():
            text += f" · {summary.get('evidence_count', 0)} 条证据"
        saved_perspective = context.get("analysis_perspective")
        lens = PERSPECTIVE_LABELS.get(saved_perspective) if isinstance(saved_perspective, str) else None
        if lens:
            text += f" · {lens}"
        if self._last_generation and self._last_generation != self._generation_signature():
            text += " · 分析选项已变化，重新生成后应用"
        elif context.get("anonymized"):
            text += " · 已匿名化"
        self.report_status.setText(text)
        self.report_status.show()

    def generate_offline(self):
        if self.busy:
            return None
        self._refresh_question()
        record = self.ai_panel.generate_offline_brief()
        if record is None:
            self._say(self.ai_panel.status_label.text())
        return record

    def preview_ai(self):
        if self.busy:
            return None
        self._refresh_question()
        prepared = self.ai_panel.preview_request()
        if prepared is not None:
            self.open_engine(2)
        else:
            self._say(self.ai_panel.status_label.text())
        return prepared

    def open_engine(self, index):
        self.ai_panel.tabs.setCurrentIndex(index)
        self.stack.setCurrentIndex(1)

    def show_run(self, run):
        if not isinstance(run, dict) or run.get("kind") != "ai":
            return
        self._current_run = deepcopy(run)
        self._rendered_payload = deepcopy(run["payload"])
        context = self._rendered_payload.setdefault("report_context", {})
        source_id = context.get('source_run_id')
        if source_id and self.source_combo.currentData() != source_id:
            index = self.source_combo.findData(source_id)
            if index >= 0:
                self.source_combo.setCurrentIndex(index)
        if not context.get("battle_labels"):
            context["battle_labels"] = self._battle_labels((self._rendered_payload.get("request") or {}).get("evidence", []))
        self._restore_history_content(self._rendered_payload, source_id)
        self._last_generation = context.get("generation_signature")
        if context.get("layout"):
            self._apply_options(context["layout"])
        saved_perspective = context.get("analysis_perspective")
        if isinstance(saved_perspective, str) and saved_perspective in PERSPECTIVE_LABELS and saved_perspective != self._perspective():
            self._select_perspective(saved_perspective)
            self._generation_changed()
        self._apply_review()
        self.ai_panel.current_run = deepcopy(run)
        self.ai_panel._render_result(run["payload"])
        self.ai_panel.review_button.setEnabled(not self.busy)
        self._refresh_history()
        self._refresh_preview()
        self.stack.setCurrentIndex(0)

    def _restore_history_content(self, payload, source_id):
        """Recover older visuals and settlement from the matching local replay."""
        request = payload.get('request') or {}
        if request.get('report_contract') not in SINGLE_BATTLE_CONTRACTS:
            return
        rows = request.get('evidence') or []
        needs_map = bool(payload.get('battle_assessment') and request.get('images') and not payload.get('report_visuals'))
        context = payload['report_context']
        needs_result = not context.get('settlement') and not any(r.get('kind') == 'replay_battle_settlement' for r in rows)
        if not (needs_map or needs_result):
            return
        shas = {r.get('source_sha') for r in rows}
        library = getattr(self.host, 'library', None)
        if len(shas) != 1 or not hasattr(library, 'get_report') or not self.ai_panel.bundle:
            return
        sha, = shas
        if not isinstance(sha, str) or len(sha) != 64:
            return
        try:
            saved = self.store.get_run(source_id)['payload'] if source_id in self._sources else {}
            snapshot = saved.get('input_snapshot') or {}
            if snapshot.get('merged_recordings'):
                return
            versions = {v.get('revision') for item in snapshot.get('inputs', []) for v in item.get('views', [])
                        if v.get('sha256') == sha and type(v.get('revision')) is int}
            if len(versions) > 1:
                return
            report = self.ai_panel.bundle.presentation_report(library.get_report(sha, next(iter(versions), None)))
            if report.get('sha256') != sha or report.get('multiview'):
                return
            if needs_map:
                from ..report_visuals import restore_report_visuals
                if recovered := restore_report_visuals(payload, report, ship_catalog=self.host.catalog):
                    payload['report_visuals'] = recovered
            if needs_result:
                from ..battle_review import build_battle_evidence
                report = self._hydrate_replay_result(library, sha, report)
                subjects = next((r.get('entity_ids') for r in rows if r.get('kind') == 'ordinary_overview'), [])
                subject = subjects[0] if isinstance(subjects, list) and len(subjects) == 1 else None
                analysis = build_battle_evidence(report, self.host.catalog, random_focus=True, focus_entity_id=subject)
                if record := next((r for r in analysis['evidence'] if r['kind'] == 'replay_battle_settlement'), None):
                    context['settlement'] = record['details']
        except (KeyError, OSError, ValueError, TypeError):
            return

    def _apply_review(self):
        if self._current_run is None or self._rendered_payload is None:
            return
        reviews = self.store.list_reviews(self._current_run["id"])
        latest = next((row["decision"] for row in reversed(reviews)
                       if row.get("decision") in ("accepted", "rejected", "needs_review")), None)
        self._rendered_payload.setdefault("report_context", {})["review_decision"] = latest

    def _review_changed(self, record):
        if self._current_run and isinstance(record, dict) and record.get("id") == self._current_run["id"]:
            self._apply_review()
            self._refresh_preview()

    def _load_history(self, *_):
        key = self.history_combo.currentData()
        if key:
            try:
                self.show_run(self.store.get_run(key))
            except Exception:
                self._say("报告读取失败，请重试。")
        else:
            self._current_run = None
            self._rendered_payload = None
            self._last_generation = None
            self.ai_panel.current_run = None
            self.ai_panel.review_button.setEnabled(False)
            self._refresh_preview()

    def _engine_status(self, text):
        self._say(text)
        self._sync_busy()

    def _say(self, text):
        self.notice.setText(text)
        self.notice.setVisible(bool(text))
        self.status.emit(text)

    def _sync_busy(self):
        busy = self.busy
        self.options_scroll.setEnabled(not busy)
        self.history_combo.setEnabled(not busy)
        self.review_button.setEnabled(not busy and self._current_run is not None)

    def _idle(self):
        self._sync_busy()
        self.idle.emit()

    def choose_export(self):
        if self._rendered_payload is None:
            return
        kind = self.export_format.currentData()
        filters = {"html": "HTML (*.html)", "pdf": "PDF (*.pdf)", "png": "PNG (*.png)", "md": "Markdown (*.md)"}
        root = getattr(getattr(self.host, "library", None), "root", self.store.root)
        path, _ = QFileDialog.getSaveFileName(self, "导出报告", str(Path(root) / ("战术报告." + kind)), filters[kind])
        if path:
            try:
                self.export_to(path, kind)
                self._say("报告已导出。")
            except Exception as error:
                self._say("导出未完成：" + str(error))

    def export_to(self, path, kind="html"):
        if self._rendered_payload is None or not build_report(self._rendered_payload, self.options())["ready"]:
            raise ValueError("请先生成报告。")
        self._checked_options()
        destination = Path(path)
        if destination.suffix.lower() != "." + kind:
            destination = destination.with_suffix("." + kind)
        if kind in ("html", "md"):
            return export_report(self._rendered_payload, self.options(), destination, format=kind)
        if kind not in ("pdf", "png"):
            raise ValueError("不支持的导出格式。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        doc = QTextDocument()
        doc.setDefaultFont(QFont("Microsoft YaHei", 11))
        doc.setDocumentMargin(20)
        doc.setHtml(render_report(self._rendered_payload, self.options()))
        fd, temporary = tempfile.mkstemp(prefix=".report-", suffix="." + kind, dir=destination.parent)
        os.close(fd)
        try:
            if kind == "pdf":
                writer = QPdfWriter(temporary)
                writer.setResolution(144)
                writer.setTitle(self.options()["title"])
                writer.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.A4),
                    QPageLayout.Orientation.Portrait, QMarginsF(10, 10, 10, 10), QPageLayout.Unit.Millimeter))
                layout_width = 800
                doc.setTextWidth(layout_width)
                height = math.ceil(doc.documentLayout().documentSize().height())
                scale = writer.width() / layout_width
                page_height = writer.height() / scale
                intervals = []
                table_bounds = []
                frames = list(doc.rootFrame().childFrames())
                while frames:
                    frame = frames.pop()
                    frames.extend(frame.childFrames())
                    if isinstance(frame, QTextTable):
                        bounds = doc.documentLayout().frameBoundingRect(frame)
                        if bounds.height() <= page_height:
                            table_bounds.append((bounds.top(), bounds.bottom()))
                block = doc.begin()
                while block.isValid():
                    bounds = doc.documentLayout().blockBoundingRect(block)
                    lines = block.layout()
                    for index in range(lines.lineCount()):
                        line = lines.lineAt(index)
                        intervals.append((bounds.top() + line.y(), bounds.top() + line.y() + line.height()))
                    block = block.next()
                pages = []
                start = 0.0
                while start < height:
                    end = min(start + page_height, height)
                    tables = [top for top, bottom in table_bounds if start + 1 < top < end < bottom]
                    if tables:
                        end = min(tables)
                    crossings = [top for top, bottom in intervals if top < end < bottom]
                    if crossings and min(crossings) > start + page_height * .25:
                        end = min(crossings)
                    pages.append((start, end))
                    start = end
                painter = QPainter(writer)
                if not painter.isActive():
                    raise OSError("PDF 写入失败。")
                background = QColor(PALETTES[self.options()["accent"]]["page"])
                for index, (start, end) in enumerate(pages):
                    if index and not writer.newPage():
                        raise OSError("PDF 分页写入失败。")
                    painter.fillRect(0, 0, writer.width(), writer.height(), background)
                    painter.save()
                    painter.scale(scale, scale)
                    painter.setClipRect(QRectF(0, 0, layout_width, end - start))
                    painter.translate(0, -start)
                    doc.drawContents(painter, QRectF(0, start, layout_width, end - start))
                    painter.restore()
                painter.end()
                del writer
            else:
                width = 1200
                doc.setTextWidth(width - 40)
                height = math.ceil(doc.size().height()) + 40
                if height > 20000:
                    raise ValueError("报告过长，请改用 PDF 或 HTML 导出。")
                image = QImage(width, max(600, height), QImage.Format.Format_ARGB32_Premultiplied)
                image.fill(QColor(PALETTES[self.options()["accent"]]["page"]))
                painter = QPainter(image)
                painter.translate(20, 20)
                doc.drawContents(painter)
                painter.end()
                if not image.save(temporary, "PNG"):
                    raise OSError("图片写入失败。")
            if Path(temporary).stat().st_size < 20:
                raise OSError("输出为空。")
            os.replace(temporary, destination)
            return destination
        finally:
            Path(temporary).unlink(missing_ok=True)

    def showEvent(self, event):
        super().showEvent(event)
        if not self.busy:
            self.refresh_sources()

