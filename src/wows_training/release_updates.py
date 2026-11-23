"""Check the public GitHub release channel and verify downloaded installers."""
from dataclasses import dataclass
from hashlib import file_digest, sha256
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .version import RELEASE_REPOSITORY, VERSION


RELEASES_URL = f"https://github.com/{RELEASE_REPOSITORY}/releases"
LATEST_URL = f"https://api.github.com/repos/{RELEASE_REPOSITORY}/releases/latest"
_VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
                   "github-releases.githubusercontent.com"}
_METADATA_LIMIT = 1024 * 1024
_INSTALLER_LIMIT = 512 * 1024 * 1024
_TRANSFER_ERRORS = (OSError, HTTPException)


class UpdateError(ValueError):
    """An error message suitable for the desktop settings page."""


class UpdateCancelled(UpdateError):
    pass


def _version_number(value):
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise UpdateError("发行版本号无法识别。")
    return tuple(int(part) for part in match.groups())


@dataclass(frozen=True, slots=True)
class ReleaseInstaller:
    version: str
    size: int
    sha256: str

    def __post_init__(self):
        _version_number(self.version)
        if (type(self.size) is not int or not 0 < self.size <= _INSTALLER_LIMIT
                or not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None):
            raise UpdateError("安装包缺少有效的大小或 SHA-256 校验值。")

    @property
    def name(self):
        return f"WOWSTraining-{self.version}-win64-setup.exe"

    @property
    def url(self):
        return f"{RELEASES_URL}/download/v{self.version}/{self.name}"


def _permitted_url(url, hosts):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in hosts and parsed.username is None
                and parsed.password is None and parsed.port in (None, 443))
    except ValueError:
        return False


class _ReleaseRedirect(HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _permitted_url(newurl, self.hosts):
            raise UpdateError("GitHub 返回了无法核对的下载地址。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_update_url(url, *, binary=False):
    hosts = _DOWNLOAD_HOSTS if binary else {"api.github.com"}
    if not _permitted_url(url, hosts):
        raise UpdateError("无法核对 GitHub 发行地址。")
    request = Request(url, headers={"User-Agent": f"WOWSTraining/{VERSION}",
                                    "Accept": "application/octet-stream" if binary else "application/vnd.github+json"})
    return build_opener(_ReleaseRedirect(hosts)).open(request, timeout=15)


def find_update(current_version=VERSION, *, open_url=_open_update_url):
    """Return the newest stable, published Windows installer, if it is newer."""
    current = _version_number(current_version)
    try:
        with open_url(LATEST_URL) as response:
            raw = response.read(_METADATA_LIMIT + 1)
    except HTTPError as error:
        if error.code == 404:
            return None
        if error.code in (403, 429):
            raise UpdateError("GitHub 暂时限制了查询，请稍后重试。") from None
        raise UpdateError("暂时无法读取 GitHub 发行信息。") from None
    except _TRANSFER_ERRORS:
        raise UpdateError("无法连接 GitHub，请检查网络后重试。") from None
    try:
        if len(raw) > _METADATA_LIMIT:
            raise ValueError
        release = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise UpdateError("GitHub 发行信息格式无效。") from None

    if not isinstance(release, dict) or not isinstance(release.get("tag_name"), str):
        raise UpdateError("GitHub 发行信息缺少版本号。")
    if release.get("draft") or release.get("prerelease"):
        return None
    tag = release["tag_name"]
    version = tag.removeprefix("v")
    if _version_number(version) <= current:
        return None
    if tag != "v" + version:
        raise UpdateError("新版发行标签格式无效。")
    name = f"WOWSTraining-{version}-win64-setup.exe"
    assets = release.get("assets")
    matches = [row for row in assets if isinstance(row, dict) and row.get("name") == name
               and row.get("state") == "uploaded"] if isinstance(assets, list) else []
    if len(matches) != 1:
        raise UpdateError("新版暂未提供 Windows 安装包。")
    asset = matches[0]
    digest = asset.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise UpdateError("新版安装包缺少 SHA-256 校验值。")
    installer = ReleaseInstaller(version, asset.get("size"), digest.removeprefix("sha256:"))
    if asset.get("browser_download_url") != installer.url:
        raise UpdateError("新版安装包地址与项目发行页不一致。")
    return installer


def download_installer(installer, directory, *, cancel=None, progress=None, open_url=_open_update_url):
    """Stream to a temporary file and expose it only after checksum verification."""
    directory = Path(directory)
    target = directory / installer.name

    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise UpdateCancelled("已取消更新下载。")

    temporary = None
    try:
        check_cancel()
        directory.mkdir(parents=True, exist_ok=True)
        if target.is_file() and target.stat().st_size == installer.size:
            with target.open("rb") as existing:
                if file_digest(existing, "sha256").hexdigest() == installer.sha256:
                    check_cancel()
                    return target
        count = 0
        digest = sha256()
        with tempfile.NamedTemporaryFile(prefix=".wows-update-", suffix=".part", dir=directory, delete=False) as output:
            temporary = Path(output.name)
            with open_url(installer.url, binary=True) as response:
                length = response.headers.get("Content-Length")
                if length is not None and length != str(installer.size):
                    raise UpdateError("下载的安装包大小与发行信息不一致。")
                while count < installer.size:
                    check_cancel()
                    chunk = response.read(min(512 * 1024, installer.size - count))
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    count += len(chunk)
                    if progress:
                        progress(count, installer.size)
                if response.read(1) or count != installer.size or digest.hexdigest() != installer.sha256:
                    raise UpdateError("安装包校验失败，请重新下载。")
        check_cancel()
        os.replace(temporary, target)
        return target
    except _TRANSFER_ERRORS:
        check_cancel()
        raise UpdateError("安装包下载失败，请检查网络和更新目录后重试。") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
