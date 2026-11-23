"""Current-Windows-user DPAPI storage. No plaintext fallback or key logging."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
from uuid import uuid4


class SecretStoreError(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, entropy, decrypt=False):
    if os.name != "nt":
        raise SecretStoreError("Windows current-user DPAPI is required")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    source_buffer = ctypes.create_string_buffer(data)
    entropy_buffer = ctypes.create_string_buffer(entropy)
    source = _Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    salt = _Blob(len(entropy), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _Blob()
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p if decrypt else wintypes.LPCWSTR,
                         ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN only: deliberately no LOCAL_MACHINE flag.
    success = function(ctypes.byref(source), None if decrypt else "WOWSTraining credential",
                       ctypes.byref(salt), None, None, 1, ctypes.byref(output))
    if not success:
        raise SecretStoreError("Credential encryption/decryption failed for the current Windows user")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


class SecretStore:
    def __init__(self, root):
        root = Path(root).expanduser().absolute()
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()
        self.directory = self.root / "credentials"
        self.directory.mkdir(exist_ok=True)
        if self.directory.resolve().parent != self.root:
            raise SecretStoreError("Credential directory must remain inside the data root")

    def _path(self, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError("Credential name must be nonempty text of at most 200 characters")
        path = self.directory / (hashlib.sha256(name.encode()).hexdigest() + ".dpapi")
        if path.resolve().parent != self.directory.resolve():
            raise SecretStoreError("Credential file must remain inside the credential directory")
        return path

    def set(self, name, value):
        path = self._path(name)
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 65536:
            raise ValueError("Credential must be nonempty text of at most 64 KiB")
        encrypted = b"WOWS-DPAPI-1\x00" + _crypt(value.encode("utf-8"), b"WOWSTraining/" + name.encode("utf-8"))
        temporary = self.directory / (uuid4().hex + ".writing")
        try:
            with temporary.open("xb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, name):
        path = self._path(name)
        if not path.exists():
            return None
        if path.stat().st_size > 128 * 1024:
            raise SecretStoreError("Invalid encrypted credential file")
        data = path.read_bytes()
        prefix = b"WOWS-DPAPI-1\x00"
        if not data.startswith(prefix):
            raise SecretStoreError("Invalid encrypted credential file")
        try:
            return _crypt(data[len(prefix):], b"WOWSTraining/" + name.encode("utf-8"), decrypt=True).decode("utf-8")
        except UnicodeError:
            raise SecretStoreError("Invalid decrypted credential") from None

    def delete(self, name):
        path = self._path(name)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
