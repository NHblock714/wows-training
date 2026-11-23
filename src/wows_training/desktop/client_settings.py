"""A compact desktop settings window with local appearance and sound controls."""
from __future__ import annotations

from copy import deepcopy
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget)

from ..client_preferences import ClientPreferences, DEFAULTS, TEXT_SIZES, validate_preferences
from ..version import VERSION
from .fleet_shell import FleetPageHeader
from .layered_panel import LayeredPanel


def _within_replay(widget):
    while widget is not None:
        if widget.property('tacticalSurface') is True:
            return True
        if widget.objectName() == 'replayPanel':
            return widget.property('academyChrome') is not True
        widget = widget.parentWidget()
    return False


def _apply_runtime(host, settings):
    host.motion.set_enabled(settings['motion_enabled'], persist=False)
    host.ui_sound.set_enabled(settings['sound_enabled'], persist=False)
    pixels = TEXT_SIZES[settings['text_size']]
    for widget in host.findChildren(QWidget):
        if widget.property('backgroundEffect') is True:
            widget.set_background_enabled(settings['background_enabled'])
        if _within_replay(widget):
            continue
        if widget.inherits('QLabel') and widget.objectName() in ('title', 'sectionTitle', 'metric'):
            continue
        if any(widget.inherits(kind) for kind in ('QLabel', 'QPushButton', 'QToolButton',
                'QAbstractItemView', 'QLineEdit', 'QComboBox', 'QAbstractSpinBox',
                'QCheckBox', 'QRadioButton', 'QTextEdit', 'QPlainTextEdit')):
            widget.setProperty('clientTextRole', 'body')
    style = host._client_base_style + '\n*[clientTextRole="body"] { font-size:' + str(pixels) + 'px; }'
    host.setStyleSheet(style)
    if not hasattr(host, '_client_table_heights'):
        host._client_table_heights = {}
    for table in host.findChildren(QAbstractItemView):
        if _within_replay(table) or not hasattr(table, 'verticalHeader'):
            continue
        heights = host._client_table_heights
        base = heights.setdefault(table, table.verticalHeader().defaultSectionSize())
        table.verticalHeader().setDefaultSectionSize(max(base, round(base * pixels / 13)))
    host.client_preferences = dict(settings)
    page = host.pages.currentIndex() if hasattr(host, 'pages') else 0
    host.backdrop.set_scene_enabled(settings['background_enabled'] and page != 1)


def initialize_client_preferences(host, root=None):
    """Call once after the desktop widgets have been constructed."""
    if not hasattr(host, '_client_base_style'):
        host._client_base_style = host.styleSheet()
    host.client_preference_store = ClientPreferences(root or host.library.root)
    seed = dict(DEFAULTS, motion_enabled=host.motion.enabled, sound_enabled=host.ui_sound.enabled)
    settings = host.client_preference_store.load(seed)
    _apply_runtime(host, settings)
    return dict(settings)


def apply_client_preferences(host, settings=None):
    """Publish one local preference file before applying its running UI state."""
    if not hasattr(host, 'client_preference_store'):
        initialize_client_preferences(host)
    settings = validate_preferences(host.client_preferences if settings is None else settings)
    old = deepcopy(host.client_preferences)
    store = host.client_preference_store
    existed = store.path.exists()
    store.save(settings)
    try:
        _apply_runtime(host, settings)
    except Exception:
        if existed:
            store.save(old)
        else:
            store.path.unlink(missing_ok=True)
        _apply_runtime(host, old)
        raise
    return dict(settings)


class ClientSettingsDialog(QDialog):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        if not hasattr(host, 'client_preference_store'):
            initialize_client_preferences(host)
        self.setWindowTitle('客户端设置')
        self.setMinimumWidth(520)
        self.resize(590, 580)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(12)
        outer.addWidget(FleetPageHeader('客户端设置', 'profile'))
        panel = LayeredPanel()
        form = QFormLayout(panel)
        form.setContentsMargins(20, 18, 24, 20)
        form.setVerticalSpacing(15)
        settings = dict(host.client_preferences, motion_enabled=host.motion.enabled,
                        sound_enabled=host.ui_sound.enabled)
        self.motion = QCheckBox('开启')
        self.motion.setChecked(settings['motion_enabled'])
        self.background = QCheckBox('开启')
        self.background.setChecked(settings['background_enabled'])
        self.sound = QCheckBox('开启')
        self.sound.setChecked(settings['sound_enabled'])
        self.text_size = QComboBox()
        for key, label in (('compact', '紧凑'), ('standard', '标准'), ('large', '较大')):
            self.text_size.addItem(label, key)
        self.text_size.setCurrentIndex(self.text_size.findData(settings['text_size']))
        self.check_updates = QCheckBox('启动时检查')
        self.check_updates.setChecked(settings['check_updates'])
        self.download_updates = QCheckBox('自动下载新版')
        self.download_updates.setChecked(settings['download_updates'])
        form.addRow('界面动效', self.motion)
        form.addRow('背景演出', self.background)
        sound_row = QHBoxLayout()
        sound_row.addWidget(self.sound)
        self.preview_button = QPushButton('试听')
        self.preview_button.clicked.connect(self.preview_sound)
        sound_row.addStretch()
        sound_row.addWidget(self.preview_button)
        form.addRow('界面提示音', sound_row)
        form.addRow('文字大小', self.text_size)
        outer.addWidget(panel)
        updates = LayeredPanel()
        update_form = QFormLayout(updates)
        update_form.setContentsMargins(20, 14, 24, 14)
        switches = QHBoxLayout()
        switches.addWidget(self.check_updates)
        switches.addWidget(self.download_updates)
        switches.addStretch()
        update_form.addRow(f'版本更新 · {VERSION}', switches)
        update_buttons = QHBoxLayout()
        self.update_check_button = QPushButton('检查更新')
        self.update_check_button.clicked.connect(lambda: host.updates.check(self.download_updates.isChecked()))
        self.update_download_button = QPushButton('下载新版')
        self.update_download_button.clicked.connect(self._download_or_cancel)
        self.update_folder_button = QPushButton('打开下载目录')
        self.update_folder_button.clicked.connect(lambda: host.open_path(host.updates.directory))
        for widget in (self.update_check_button, self.update_download_button, self.update_folder_button):
            update_buttons.addWidget(widget)
        update_form.addRow(update_buttons)
        self.update_notice = QLabel()
        self.update_notice.setTextFormat(Qt.TextFormat.PlainText)
        self.update_notice.setWordWrap(True)
        self.update_progress = QProgressBar()
        self.update_progress.setFormat('下载进度 %p%')
        update_form.addRow(self.update_notice)
        update_form.addRow(self.update_progress)
        outer.addWidget(updates)
        host.updates.changed.connect(self._refresh_update_status)
        self._refresh_update_status()
        links = QHBoxLayout()
        for label, handler in (('资料目录', lambda: host.open_path(host.library.root)),
                               ('公开查询', self.open_public_settings),
                               ('AI 服务', self.open_ai_settings)):
            item = QPushButton(label)
            item.clicked.connect(handler)
            links.addWidget(item)
        outer.addLayout(links)
        self.notice = QLabel()
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        self.notice.hide()
        outer.addWidget(self.notice)
        outer.addStretch()
        actions = QDialogButtonBox()
        self.apply_button = actions.addButton('应用', QDialogButtonBox.ButtonRole.ApplyRole)
        self.done_button = actions.addButton('完成', QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_button = actions.addButton('取消', QDialogButtonBox.ButtonRole.RejectRole)
        self.apply_button.clicked.connect(self.apply)
        self.done_button.clicked.connect(self.finish)
        self.cancel_button.clicked.connect(self.reject)
        outer.addWidget(actions)
        self._sync_text_size()

    def _sync_text_size(self):
        pixels = TEXT_SIZES[self.host.client_preferences['text_size']]
        self.setStyleSheet('QLabel,QCheckBox,QComboBox,QPushButton { font-size:' + str(pixels) + 'px; }')

    def values(self):
        return {'motion_enabled': self.motion.isChecked(), 'sound_enabled': self.sound.isChecked(),
                'background_enabled': self.background.isChecked(), 'text_size': self.text_size.currentData(),
                'check_updates': self.check_updates.isChecked(), 'download_updates': self.download_updates.isChecked()}

    def _refresh_update_status(self):
        updates = self.host.updates
        self.update_check_button.setEnabled(not updates.busy)
        self.update_download_button.setText('取消更新' if updates.busy else '下载新版')
        self.update_download_button.setEnabled(updates.busy or (
            updates.installer is not None and updates.downloaded_path is None))
        self.update_folder_button.setEnabled(updates.downloaded_path is not None)
        self.update_notice.setText(updates.message)
        self.update_notice.setVisible(bool(updates.message))
        if updates.transfer:
            received, total = updates.transfer
            self.update_progress.setRange(0, total)
            self.update_progress.setValue(received)
        self.update_progress.setVisible(updates.transfer is not None)

    def _download_or_cancel(self):
        if self.host.updates.busy:
            self.host.updates.cancel()
        else:
            self.host.updates.download()

    def _say(self, text):
        self.notice.setText(text)
        self.notice.setVisible(bool(text))

    def apply(self):
        try:
            apply_client_preferences(self.host, self.values())
        except (OSError, ValueError, RuntimeError):
            self._say('设置未保存，请重试。')
            return False
        self._sync_text_size()
        self._say('设置已应用')
        return True

    def finish(self):
        if self.apply():
            self.accept()

    def preview_sound(self):
        self._say('' if self.host.ui_sound.preview('confirm') else '当前环境无法播放提示音')

    def open_public_settings(self):
        self.reject()
        self.host.query_center_panel.open_settings()

    def open_ai_settings(self):
        self.reject()
        self.host.show_page(3)
        self.host.report_studio.open_engine(0)
