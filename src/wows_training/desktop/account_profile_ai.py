"""A compact, snapshot-scoped AI portrait embedded in public career lookup."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from ..ai_analysis import digest
from ..career_profile_evidence import PROFILE_QUESTION, build_career_profile_evidence
from .account_profile_report import PortraitView
from .ai_panel import AIPanel
from .ui_components import StatusLabel


def _matching_portrait(payload, identity, token):
    if not token:
        return False
    saved = (payload.get('report_context') or {}).get('source_fingerprint')
    if saved == token:
        return True
    request = payload.get('request') or {}
    evidence = request.get('evidence')
    if (not saved or request.get('report_contract') != 'career_profile_v1'
            or not isinstance(evidence, list) or not 1 <= len(evidence) <= 7):
        return False
    # V1 stored a digest of the whole anonymous source. A complete old request
    # preserves those exact rows, and the digest proves the current snapshot.
    ids = {f'P{index:02}' for index in range(1, 8)}
    if any(not isinstance(row, dict) or set(row) != {'id', 'kind', 'label', 'text', 'details'}
           or not isinstance(row.get('id'), str) or row['id'] not in ids for row in evidence):
        return False
    if len({row['id'] for row in evidence}) != len(evidence):
        return False
    source = {'kind':'career_profile', 'report_contract':'career_profile_v1', 'source_name':'公开账号资料',
              'evidence':sorted(evidence, key=lambda row: row['id'])}
    return saved == digest([*identity, source])


class AccountProfileAI(QWidget):
    idle = Signal()
    busy_state = Signal(bool)

    def __init__(self, shared, parent=None):
        super().__init__(parent)
        self.shared = shared
        self._analysis = {}
        self._token = None
        self._identity = ()
        self._reload = set()
        self.engine = AIPanel(shared.store, shared.bundle, lambda: self._analysis, dict, dict, self)
        self.engine.setObjectName('accountProfileAIEngine')
        self.engine.report_context_getter = self._context
        self.engine.question.setPlainText(PROFILE_QUESTION)
        self.engine.include_plan.setChecked(False)
        self.engine.include_own.setChecked(False)
        self.engine.include_images.setChecked(False)
        self.engine.include_images.hide()
        self.engine.tabs.tabBar().hide()
        self.engine.manual_button.hide()
        self.engine.send_button.setText('发送战绩信息并生成画像')

        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.settings_button = QPushButton('AI 连接')
        self.settings_button.clicked.connect(self._open_settings)
        self.preview_button = QPushButton('AI 分析预览')
        self.preview_button.setObjectName('primary')
        self.preview_button.clicked.connect(self._preview)
        self.back_button = QPushButton('返回画像')
        self.back_button.clicked.connect(self._back)
        for widget in (self.settings_button, self.preview_button, self.back_button):
            bar.addWidget(widget)
        bar.addStretch(1)
        layout.addLayout(bar)
        self.notice = StatusLabel()
        layout.addWidget(self.notice)
        self.stack = QStackedWidget()
        self.viewer = PortraitView()
        self.stack.addWidget(self.viewer)
        self.stack.addWidget(self.engine)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.engine.generation_progress)
        self.engine.run_saved.connect(self._show_run)
        self.engine.status.connect(self._show_status)
        self.engine.idle.connect(self._finished)
        self.engine.changed.connect(self._local_settings)
        self.shared.changed.connect(self._shared_settings)
        self.shared.idle.connect(self._shared_idle)
        self._sync()

    @property
    def busy(self):
        return self.engine.busy

    def cancel(self):
        self.engine.cancel()

    def set_source(self, profile=None, target=None, snapshot=None):
        analysis = {}
        if profile and target and snapshot:
            try:
                analysis = build_career_profile_evidence(profile, target, snapshot)
            except (ValueError, KeyError, TypeError):
                pass
        identity = (target['region'], target['id'], profile['snapshot_id']) if analysis.get('evidence') else ()
        token = digest(identity) if identity else None
        if token == self._token and analysis == self._analysis:
            if token is None and target:
                self.viewer.set_message('当前账号缺少可分析的公开战绩。')
            return
        if self.busy:
            self.cancel()
        self._analysis, self._token, self._identity = analysis, token, identity
        self.engine.current_run = None
        self.engine._invalidate('战绩快照已切换，请重新预览。')
        self.engine.refresh_sources()
        self.notice.clear()
        self.stack.setCurrentWidget(self.viewer)
        if not token:
            self.viewer.set_message('当前账号缺少可分析的公开战绩。')
            self._sync()
            return
        self.viewer.set_message('基于当前公开战绩快照，分析舰船偏好、游玩风格和游戏经验。')
        latest = next((run for run in reversed(self.engine.store.list_runs('career_ai'))
                       if _matching_portrait(run['payload'], identity, token)), None)
        if latest:
            self._show_run(latest)
        self._sync()

    def _context(self):
        return {'source_fingerprint':self._token, 'source_name':'公开玩家战绩',
                'layout':{'template':'player', 'title':'AI 战绩画像', 'show_evidence':False}}

    def _open_settings(self):
        self.engine.tabs.setCurrentIndex(0)
        self.stack.setCurrentWidget(self.engine)
        self._sync()

    def _preview(self):
        if not self.busy and self._token and self.engine.preview_request() is not None:
            self.stack.setCurrentWidget(self.engine)
        self._sync()

    def _back(self):
        self.stack.setCurrentWidget(self.viewer)
        self._sync()

    def _show_run(self, run):
        payload = run['payload']
        if not _matching_portrait(payload, self._identity, self._token):
            return
        self.viewer.show_report(payload)
        self.stack.setCurrentWidget(self.viewer)
        self._sync()

    def _show_status(self, message):
        self.notice.setText(message if self.stack.currentWidget() is self.viewer else '')
        self._sync()

    def _sync(self):
        self.preview_button.setEnabled(bool(self._token) and not self.busy)
        self.settings_button.setEnabled(not self.busy)
        self.back_button.setVisible(self.stack.currentWidget() is self.engine)
        self.back_button.setEnabled(not self.busy)
        self.busy_state.emit(self.busy)

    def _reload_settings(self, panel):
        if panel.busy:
            self._reload.add(panel)
            return
        self._reload.discard(panel)
        panel._loading = True
        try:
            panel._load_settings()
        finally:
            panel._loading = False
        panel._input_changed()
        panel._update_key_status()

    def _local_settings(self, record):
        if record.get('kind') == 'ai_settings':
            self._reload_settings(self.shared)
            self.shared.changed.emit(record)

    def _shared_settings(self, record):
        if record.get('kind') == 'ai_settings':
            self._reload_settings(self.engine)

    def _finished(self):
        if self.engine in self._reload:
            self._reload_settings(self.engine)
        self._sync()
        self.idle.emit()

    def _shared_idle(self):
        if self.shared in self._reload:
            self._reload_settings(self.shared)
