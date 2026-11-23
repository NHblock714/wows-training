import hashlib
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from wows_training.release_updates import LATEST_URL, ReleaseInstaller, UpdateCancelled, UpdateError
from wows_training.release_updates import download_installer, find_update


_PAYLOAD = b"example installer bytes"
_INSTALLER = ReleaseInstaller("0.34.10", len(_PAYLOAD), hashlib.sha256(_PAYLOAD).hexdigest())


def _release(**changes):
    asset = {"name": _INSTALLER.name, "size": _INSTALLER.size, "state": "uploaded",
             "digest": "sha256:" + _INSTALLER.sha256, "browser_download_url": _INSTALLER.url}
    return {"tag_name": "v" + _INSTALLER.version, "draft": False, "prerelease": False, "assets": [asset], **changes}


class UpdateTests(unittest.TestCase):
    def test_update_requires_a_newer_stable_release_and_matching_asset(self):
        def open_url(url, **kwargs):
            self.assertEqual(url, LATEST_URL)
            self.assertFalse(kwargs)
            return BytesIO(json.dumps(_release()).encode())

        self.assertEqual(find_update("0.34.9", open_url=open_url), _INSTALLER)
        self.assertIsNone(find_update("0.34.10", open_url=open_url))
        draft = _release(draft=True)
        self.assertIsNone(find_update("0.34.9", open_url=lambda *_: BytesIO(json.dumps(draft).encode())))

        for key, value in (("browser_download_url", "https://example.com/setup.exe"), ("digest", None)):
            with self.subTest(key=key):
                release = _release()
                release["assets"][0][key] = value
                with self.assertRaises(UpdateError):
                    find_update("0.34.9", open_url=lambda *_: BytesIO(json.dumps(release).encode()))

    def test_verified_download_is_reused_and_failed_or_cancelled_download_leaves_no_temp_file(self):
        requested = []

        def open_url(url, *, binary):
            self.assertTrue(binary)
            self.assertEqual(url, _INSTALLER.url)
            requested.append(url)
            response = BytesIO(_PAYLOAD)
            response.headers = {"Content-Length": str(len(_PAYLOAD))}
            return response

        with TemporaryDirectory() as folder:
            path = download_installer(_INSTALLER, folder, open_url=open_url)
            self.assertEqual(path.read_bytes(), _PAYLOAD)
            self.assertEqual(download_installer(_INSTALLER, folder, open_url=open_url), path)
            self.assertEqual(len(requested), 1)
        with TemporaryDirectory() as folder:
            def corrupted(_url, *, binary):
                response = BytesIO(b"!" * len(_PAYLOAD))
                response.headers = {"Content-Length": str(len(_PAYLOAD))}
                return response
            with self.assertRaises(UpdateError):
                download_installer(_INSTALLER, folder, open_url=corrupted)
            self.assertEqual(list(Path(folder).iterdir()), [])
            cancellation = Event()
            with self.assertRaises(UpdateCancelled):
                download_installer(_INSTALLER, folder, open_url=open_url, cancel=cancellation,
                                   progress=lambda *_: cancellation.set())
            self.assertEqual(list(Path(folder).iterdir()), [])
