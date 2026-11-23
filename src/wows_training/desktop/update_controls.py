"""Background release checks and download state shared with client settings."""
from threading import Event

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout

from ..release_updates import UpdateCancelled, UpdateError, download_installer, find_update
from ..version import VERSION
from .layered_panel import LayeredPanel


class _UpdateWorker(QThread):
    message = Signal(str)
    found = Signal(object)
    progress = Signal(int, int)
    downloaded = Signal(object)

    def __init__(self, parent, directory, *, check, automatic_download, installer):
        super().__init__(parent)
        self.directory = directory
        self.check = check
        self.automatic_download = automatic_download
        self.installer = installer
        self.cancelled = Event()

    def run(self):
        try:
            installer = self.installer
            if self.check:
                self.message.emit("正在检查 GitHub 发行版本…")
                installer = find_update()
                if self.cancelled.is_set():
                    raise UpdateCancelled("已取消更新检查。")
                self.found.emit(installer)
                if installer is None:
                    self.message.emit("当前已是最新正式版。")
                    return
                if not self.automatic_download:
                    return
            self.message.emit(f"正在下载 {installer.version} 安装包…")
            path = download_installer(installer, self.directory, cancel=self.cancelled,
                                      progress=self.progress.emit)
            self.downloaded.emit(path)
            self.message.emit(f"新版 {installer.version} 已下载并校验。")
        except UpdateError as error:
            self.message.emit(str(error))


class UpdateController(QObject):
    changed = Signal()
    update_available = Signal(object)
    idle = Signal()

    def __init__(self, host):
        super().__init__(host)
        self.directory = host.library.root / "updates"
        self.installer = None
        self.downloaded_path = None
        self.transfer = None
        self.message = ""
        self._worker = None
        self._notify_on_check = False

    @property
    def busy(self):
        return self._worker is not None

    def check(self, automatic_download=False, *, notify=False):
        return self._start(check=True, automatic_download=automatic_download, notify=notify)

    def download(self):
        if self.installer is None or self.downloaded_path is not None:
            return False
        return self._start(check=False, automatic_download=False)

    def cancel(self):
        if self._worker:
            self._worker.cancelled.set()

    def _start(self, *, check, automatic_download, notify=False):
        if self._worker is not None:
            return False
        worker = _UpdateWorker(self, self.directory, check=check, automatic_download=automatic_download,
                               installer=self.installer)
        self.transfer = None
        self._worker = worker
        self._notify_on_check = notify
        worker.message.connect(self._message)
        worker.found.connect(self._found)
        worker.progress.connect(self._progress)
        worker.downloaded.connect(self._downloaded)
        worker.finished.connect(self._finished)
        self.changed.emit()
        worker.start()
        return True

    def _message(self, text):
        self.message = text
        self.changed.emit()

    def _found(self, installer):
        if self.installer != installer:
            self.downloaded_path = None
        self.installer = installer
        if installer is not None:
            self.message = f"发现新版 {installer.version}。"
        self.changed.emit()
        if self._notify_on_check and installer is not None:
            self.update_available.emit(installer)

    def _progress(self, count, total):
        self.transfer = count, total
        self.changed.emit()

    def _downloaded(self, path):
        self.downloaded_path = path
        self.changed.emit()

    def _finished(self):
        worker, self._worker = self._worker, None
        worker.deleteLater()
        self.transfer = None
        self._notify_on_check = False
        self.changed.emit()
        self.idle.emit()


class UpdateDialog(QDialog):
    """A modeless update notice that follows the shared download state."""

    def __init__(self, host, installer):
        super().__init__(host)
        self.host = host
        self.updates = host.updates
        self.installer = installer
        self.setObjectName("updateAvailableDialog")
        self.setWindowTitle("版本更新")
        self.setMinimumWidth(410)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        panel = LayeredPanel()
        content = QVBoxLayout(panel)
        content.setContentsMargins(18, 16, 18, 16)
        heading = QLabel(f"发现新版 {installer.version}")
        heading.setObjectName("sectionTitle")
        content.addWidget(heading)
        content.addWidget(QLabel(f"当前版本 {VERSION}"))
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        content.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setAccessibleName("更新下载进度")
        self.progress.setFormat("下载进度 %p%")
        content.addWidget(self.progress)
        layout.addWidget(panel)

        actions = QHBoxLayout()
        actions.addStretch()
        later = QPushButton("稍后查看")
        later.clicked.connect(self.reject)
        actions.addWidget(later)
        self.primary = QPushButton()
        self.primary.setObjectName("primary")
        self.primary.clicked.connect(self._activate)
        actions.addWidget(self.primary)
        layout.addLayout(actions)
        self.updates.changed.connect(self._refresh)
        self._refresh()

    def _refresh(self):
        if self.updates.installer != self.installer:
            self.reject()
            return
        message = self.updates.message
        self.status.setText(message)
        self.status.setVisible(bool(message) and message != f"发现新版 {self.installer.version}。")
        if self.updates.transfer:
            received, total = self.updates.transfer
            self.progress.setRange(0, total)
            self.progress.setValue(received)
        self.progress.setVisible(self.updates.transfer is not None)
        ready = self.updates.downloaded_path is not None
        self.primary.setText("打开下载目录" if ready else "下载安装包")
        self.primary.setEnabled(ready or not self.updates.busy)

    def _activate(self):
        if self.updates.downloaded_path is not None:
            self.host.open_path(self.updates.downloaded_path.parent)
            self.accept()
        else:
            self.updates.download()
