"""Background release checks and download state shared with client settings."""
from threading import Event

from PySide6.QtCore import QObject, QThread, Signal

from ..release_updates import UpdateCancelled, UpdateError, download_installer, find_update


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
                    self.message.emit(f"发现新版 {installer.version}。")
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
    idle = Signal()

    def __init__(self, host):
        super().__init__(host)
        self.directory = host.library.root / "updates"
        self.installer = None
        self.downloaded_path = None
        self.transfer = None
        self.message = ""
        self._worker = None

    @property
    def busy(self):
        return self._worker is not None

    def check(self, automatic_download=False):
        return self._start(check=True, automatic_download=automatic_download)

    def download(self):
        if self.installer is None or self.downloaded_path is not None:
            return False
        return self._start(check=False, automatic_download=False)

    def cancel(self):
        if self._worker:
            self._worker.cancelled.set()

    def _start(self, *, check, automatic_download):
        if self._worker is not None:
            return False
        worker = _UpdateWorker(self, self.directory, check=check, automatic_download=automatic_download,
                               installer=self.installer)
        self.transfer = None
        self._worker = worker
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
        self.changed.emit()

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
        self.changed.emit()
        self.idle.emit()
