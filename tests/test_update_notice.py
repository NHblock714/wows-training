from hashlib import sha256
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from wows_training.client_preferences import DEFAULTS
from wows_training.desktop.main_window import MainWindow
from wows_training.desktop.update_controls import UpdateController
from wows_training.release_updates import ReleaseInstaller, UpdateError


_CONTENT = b"test setup"
_INSTALLER = ReleaseInstaller("0.35.0", len(_CONTENT), sha256(_CONTENT).hexdigest())


class _Window(QWidget):
    start_update_check = MainWindow.start_update_check
    _show_update_notice = MainWindow._show_update_notice

    def __init__(self, directory):
        super().__init__()
        self.library = SimpleNamespace(root=directory)
        self.client_preferences = dict(DEFAULTS)
        self._closing = False
        self._update_dialog = None
        self._notified_versions = set()
        self.updates = UpdateController(self)
        self.updates.update_available.connect(self._show_update_notice)
        self.opened = []

    def open_path(self, path):
        self.opened.append(path)


class UpdateNoticeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = TemporaryDirectory()
        self.window = _Window(Path(self.folder.name))
        self.window.show()

    def tearDown(self):
        self.window.updates.cancel()
        self.wait_for(lambda: not self.window.updates.busy)
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.folder.cleanup()

    def wait_for(self, predicate):
        for _ in range(250):
            if predicate():
                return
            QTest.qWait(10)
        self.fail("update worker did not reach the expected state")

    def test_default_startup_prompts_then_shows_progress_and_verified_download(self):
        release_transfer = Event()

        def download(installer, directory, *, cancel, progress):
            progress(3, installer.size)
            if not release_transfer.wait(3):
                raise UpdateError("verification did not release the download")
            directory.mkdir()
            path = directory / installer.name
            path.write_bytes(_CONTENT)
            return path

        with patch("wows_training.desktop.update_controls.find_update", return_value=_INSTALLER), \
             patch("wows_training.desktop.update_controls.download_installer", side_effect=download):
            self.window.start_update_check()
            try:
                self.wait_for(lambda: bool(self.window._update_dialog and self.window.updates.transfer))
                dialog = self.window._update_dialog
                self.assertTrue(dialog.isVisible())
                self.assertFalse(dialog.isModal())
                self.assertTrue(dialog.progress.isVisible())
                self.assertEqual(dialog.progress.value(), 3)
            finally:
                release_transfer.set()
            self.wait_for(lambda: not self.window.updates.busy)
            self.assertEqual(dialog.primary.text(), "打开下载目录")
            dialog.primary.click()
            self.assertEqual(self.window.opened, [self.window.updates.directory])
            self.assertIsNone(self.window._update_dialog)

    def test_manual_download_preference_and_one_notice_per_version(self):
        self.window.client_preferences["download_updates"] = False
        with patch("wows_training.desktop.update_controls.find_update", return_value=_INSTALLER), \
             patch("wows_training.desktop.update_controls.download_installer") as download:
            self.window.start_update_check()
            self.wait_for(lambda: not self.window.updates.busy)
            self.assertEqual(self.window._update_dialog.primary.text(), "下载安装包")
            download.assert_not_called()
            self.window._update_dialog.reject()
            self.window.start_update_check()
            self.wait_for(lambda: not self.window.updates.busy)
            self.assertIsNone(self.window._update_dialog)

    def test_no_startup_popup_when_disabled_offline_or_already_current(self):
        self.window.client_preferences["check_updates"] = False
        with patch("wows_training.desktop.update_controls.find_update") as check:
            self.window.start_update_check()
            check.assert_not_called()
        self.window.client_preferences["check_updates"] = True
        with patch("wows_training.desktop.update_controls.find_update", side_effect=UpdateError("网络不可用")):
            self.window.start_update_check()
            self.wait_for(lambda: not self.window.updates.busy)
            self.assertIsNone(self.window._update_dialog)
            self.assertEqual(self.window.updates.message, "网络不可用")
        with patch("wows_training.desktop.update_controls.find_update", return_value=None):
            self.window.start_update_check()
            self.wait_for(lambda: not self.window.updates.busy)
            self.assertIsNone(self.window._update_dialog)
