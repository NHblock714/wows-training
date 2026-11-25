"""Read the official profile site's public player-insignia layers."""

from dataclasses import dataclass
from gzip import GzipFile
from http.client import HTTPException
from io import BytesIO
import json
import re
from threading import Lock
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from zlib import error as CompressionError

from .public_stats import _NoRedirect, _cancelled, _positive_id


_DOMAINS = {"asia": "worldofwarships.asia", "eu": "worldofwarships.eu", "na": "worldofwarships.com"}
_ICON_HOST = "wows-gloss-icons.wgcdn.co"
_ICON_PATH = re.compile(r"/icons/dog_tags/medium/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.png")
_HEX_COLOUR = re.compile(r"(?:0x|#)([a-f0-9]{6})", re.IGNORECASE)
_GLOSSARY = b'''{"query":"query DogTagGlossary($lang: String) { dogTagComponents(lang: $lang) { id type color isColorizable showClanTag clanTag { y } icons { medium } textureData { id background { medium } border { medium } } } }","variables":{"lang":"en"}}'''
_GLOSSARY_TTL = 6 * 60 * 60
_MAX_JSON = 1 * 1024 * 1024
_MAX_PNG = 512 * 1024
_BUDGET = 7.0
_SOCKET_TIMEOUT = 2.5
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_glossaries = {}
_glossary_lock = Lock()


@dataclass(frozen=True, slots=True)
class DogTagLayer:
    """An official PNG, with tint and vertical alignment when used by its design."""

    role: str
    image: bytes
    color: str | None = None
    # +/-1 reserves the clan-label space and aligns the symbol bottom/top, respectively.
    position_y: float = 0.0


def _check_cancelled(cancel):
    if _cancelled(cancel):
        raise InterruptedError("Player insignia lookup cancelled")


def _check_budget(cancel, deadline):
    _check_cancelled(cancel)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Player insignia request timed out")
    return remaining


def _read_bytes(request, limit, content_type, cancel, deadline):
    timeout = min(_SOCKET_TIMEOUT, _check_budget(cancel, deadline))
    body = bytearray()
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.headers.get_content_type() != content_type:
                raise ValueError("Unexpected player insignia response type")
            encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
            if encoding not in ("identity", "gzip"):
                raise ValueError("Unexpected player insignia response encoding")
            if response.length is not None and response.length > limit:
                raise ValueError("Player insignia response exceeds limit")
            while True:
                _check_budget(cancel, deadline)
                chunk = response.read1(min(32 * 1024, limit - len(body) + 1))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > limit:
                    raise ValueError("Player insignia response exceeds limit")
            if response.length:
                raise ValueError("Incomplete player insignia response")
    except HTTPError as error:
        error.close()
        raise
    data = bytes(body)
    if encoding == "gzip":
        try:
            with GzipFile(fileobj=BytesIO(data)) as source:
                data = source.read(limit + 1)
        except (EOFError, CompressionError) as error:
            raise ValueError("Incomplete compressed player insignia response") from error
        if len(data) > limit:
            raise ValueError("Decoded player insignia response exceeds limit")
    _check_budget(cancel, deadline)
    return data


def _json_for(domain, path, cancel, deadline, *, body=None):
    headers = {"Accept": "application/json", "Origin": f"https://profile.{domain}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(f"https://vortex.{domain}{path}", data=body, headers=headers)
    return json.loads(_read_bytes(request, _MAX_JSON, "application/json", cancel, deadline))


def _get_object(mapping, key):
    value = mapping.get(key) if isinstance(mapping, dict) else None
    return value if isinstance(value, dict) else {}


def _public_selection(domain, account_id, cancel, deadline):
    response = _json_for(domain, f"/api/accounts/{account_id}/", cancel, deadline)
    data = _get_object(response, "data")
    if not isinstance(response, dict) or response.get("status") != "ok" or data.keys() != {account_id}:
        return {}
    record = _get_object(data, account_id)
    if record.get("hidden_profile", False) is not False:
        return {}
    if any(key in record and _positive_id(record[key]) != account_id for key in ("id", "account_id")):
        return {}
    return _get_object(record, "dog_tag")


def _components(domain, cancel, deadline):
    with _glossary_lock:
        cached = _glossaries.get(domain)
        if cached is not None and time.monotonic() - cached[0] < _GLOSSARY_TTL:
            return cached[1]
    response = _json_for(domain, "/api/graphql/glossary/", cancel, deadline, body=_GLOSSARY)
    rows = _get_object(response, "data").get("dogTagComponents")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Player insignia glossary unavailable")
    components = {}
    for row in rows:
        key = _positive_id(row.get("id")) if isinstance(row, dict) else None
        if key is None or key in components:
            raise ValueError("Invalid player insignia glossary")
        components[key] = row
    with _glossary_lock:
        _glossaries[domain] = (time.monotonic(), components)
    return components


def _color(component):
    raw = component.get("color")
    match = _HEX_COLOUR.fullmatch(raw) if isinstance(raw, str) else None
    return f"#{match[1].lower()}" if match else None


def _layer_png(url, cancel, deadline):
    if not isinstance(url, str) or len(url) > 1024 or any(not 32 < ord(char) < 127 for char in url):
        raise ValueError("Invalid official insignia asset URL")
    if url.startswith("//"):
        url = "https:" + url
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc.lower() != _ICON_HOST or "?" in url or "#" in url
            or not _ICON_PATH.fullmatch(parts.path)):
        raise ValueError("Untrusted official insignia asset URL")
    request = Request(url, headers={"Accept": "image/png"})
    raw = _read_bytes(request, _MAX_PNG, "image/png", cancel, deadline)
    width = int.from_bytes(raw[16:20], "big")
    height = int.from_bytes(raw[20:24], "big")
    if (len(raw) < 33 or not raw.startswith(_PNG_SIGNATURE) or raw[12:16] != b"IHDR"
            or not 1 <= width <= 512 or not 1 <= height <= 512):
        raise ValueError("Invalid official player insignia PNG")
    return raw


def _layers(selection, components, cancel, deadline):
    def component(key):
        return components.get(_positive_id(selection.get(key))) or {}

    symbol = _get_object(component("symbol_id"), "icons").get("medium")
    if not symbol:
        return None
    background = component("background_id")
    background_url = _get_object(background, "icons").get("medium")
    colorizable = background.get("isColorizable") is True
    border_url = None
    texture = component("texture_id").get("textureData")
    if colorizable and isinstance(texture, list):
        background_id = _positive_id(selection.get("background_id"))
        pair = next((row for row in texture if isinstance(row, dict) and
                     _positive_id(row.get("id")) == background_id), None)
        background_url = _get_object(pair, "background").get("medium")
        border_url = _get_object(pair, "border").get("medium")
    if colorizable and not background_url:
        return None
    offset = 0.0
    if background.get("showClanTag") is True:
        y = _get_object(background, "clanTag").get("y")
        offset = 1.0 if type(y) in (int, float) and y < 50 else -1.0

    layers = []
    if background_url:
        color = _color(component("background_color_id")) if colorizable else None
        layers.append(DogTagLayer("background", _layer_png(background_url, cancel, deadline), color))
    if border_url:
        layers.append(DogTagLayer("border", _layer_png(border_url, cancel, deadline),
                                  _color(component("border_color_id"))))
    layers.append(DogTagLayer("symbol", _layer_png(symbol, cancel, deadline), position_y=offset))
    return tuple(layers)


def fetch_dog_tag(region, account_id, cancel=None):
    """Return display layers for a public account, or None if unavailable."""
    region = "na" if region == "com" else region
    domain = _DOMAINS.get(region) if isinstance(region, str) else None
    account_id = _positive_id(account_id)
    if domain is None or account_id is None:
        return None
    deadline = time.monotonic() + _BUDGET
    try:
        selection = _public_selection(domain, account_id, cancel, deadline)
        if _positive_id(selection.get("symbol_id")) is None:
            return None
        return _layers(selection, _components(domain, cancel, deadline), cancel, deadline)
    except InterruptedError:
        raise
    except (HTTPException, OSError, RecursionError, ValueError):
        _check_cancelled(cancel)
        return None
