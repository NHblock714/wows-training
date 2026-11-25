"""Read the maintained, regional WoWS Numbers leaderboard feed."""

from datetime import date, datetime, timezone
from http.client import HTTPException
import json
import re
import time
from urllib.parse import parse_qsl, unquote, urlsplit
from urllib.request import Request, build_opener

from .public_stats import _NoRedirect, _cancelled, _positive_id


_FEED_ROOT = "https://raw.githubusercontent.com/NHblock714/wows-training/main/data/numbers"
_NUMBERS_HOSTS = {
    "asia": "asia.wows-numbers.com",
    "eu": "wows-numbers.com",
    "na": "na.wows-numbers.com",
}
_SHIP_PATH = re.compile(r"(?:/[a-z]{2}(?:-[a-z]{2})?)?/ship/([0-9]+),[^/\\]+/")
_UNSAFE_URL = re.compile(r"[\x00-\x20\x7f-\x9f\ud800-\udfff]|%(?![0-9A-Fa-f]{2})")
_DEFAULT_ORDER_QUERY = {"order": "rating__desc", "p": "1"}
_MAX_BYTES = 2 * 1024 * 1024
_MAX_SHIPS = 2048
_MAX_ROWS = 100
_SOCKET_TIMEOUT = 4
_DEADLINE_SECONDS = 6


def feed_url(region):
    """Return the single published feed URL for a canonical region."""
    if not isinstance(region, str) or region not in _NUMBERS_HOSTS:
        raise ValueError("Unsupported WoWS Numbers feed region")
    return f"{_FEED_ROOT}/{region}.json"


def _check_cancel(cancel):
    if _cancelled(cancel):
        raise InterruptedError("WoWS Numbers feed request cancelled")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate key in WoWS Numbers feed")
        value[key] = item
    return value


def _reject_constant(_constant):
    raise ValueError("Invalid number in WoWS Numbers feed")


def fetch_numbers_feed(region, cancel=None):
    """Fetch bounded JSON from the published feed, without credentials or redirects."""
    request = Request(feed_url(region), headers={
        "Accept": "application/json", "User-Agent": "WOWSTraining leaderboard feed",
    }, method="GET")
    return _fetch_feed(request, cancel)


def _fetch_feed(request, cancel=None):
    _check_cancel(cancel)
    deadline = time.monotonic() + _DEADLINE_SECONDS
    raw = bytearray()
    try:
        with build_opener(_NoRedirect()).open(request, timeout=_SOCKET_TIMEOUT) as response:
            while True:
                _check_cancel(cancel)
                if time.monotonic() > deadline:
                    raise TimeoutError("WoWS Numbers feed read timed out")
                block = response.read(min(65536, _MAX_BYTES + 1 - len(raw)))
                if not block:
                    break
                raw.extend(block)
                if len(raw) > _MAX_BYTES:
                    raise ValueError("WoWS Numbers feed exceeds the response limit")
            # A sized HTTPResponse.read can return EOF before Content-Length without raising.
            unread = getattr(response, "length", None)
            if type(unread) is int and unread > 0:
                raise OSError("Incomplete WoWS Numbers feed response")
    except HTTPException as error:
        _check_cancel(cancel)
        raise OSError("Invalid WoWS Numbers feed response") from error
    _check_cancel(cancel)
    if time.monotonic() > deadline:
        raise TimeoutError("WoWS Numbers feed read timed out")
    return _decode_feed(raw)


def _decode_feed(raw):
    if len(raw) > _MAX_BYTES:
        raise ValueError("WoWS Numbers feed exceeds the response limit")
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except (UnicodeError, RecursionError) as error:
        raise ValueError("Invalid WoWS Numbers feed JSON") from error


def _canonical_id(value):
    return isinstance(value, str) and _positive_id(value) == value


def _source_url(value, region, ship_id):
    if not isinstance(value, str) or len(value) > 512 or _UNSAFE_URL.search(value):
        raise ValueError("Invalid WoWS Numbers leaderboard URL")
    url = urlsplit(value)
    path = unquote(url.path, errors="strict")
    match = _SHIP_PATH.fullmatch(path)
    query = parse_qsl(url.query, strict_parsing=True, max_num_fields=2)
    if (url.scheme != "https" or url.netloc != _NUMBERS_HOSTS[region]
            or not match or match[1] != ship_id or any(ord(char) < 32 for char in path)
            or url.fragment not in ("", "leaderboard")
            or url.query and (len(query) != 2 or dict(query) != _DEFAULT_ORDER_QUERY)):
        raise ValueError("Leaderboard URL does not match the regional default PR table")
    return value


def _observed_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Invalid WoWS Numbers observation date")
    if date.fromisoformat(value) > datetime.now(timezone.utc).date():
        raise ValueError("WoWS Numbers observation date is in the future")
    return value


def account_rankings(document, region, account_id):
    """Validate a feed and retain only one account's ranks and complete-board evidence."""
    feed_url(region)
    account_id = _positive_id(account_id)
    if account_id is None:
        raise ValueError("Invalid WoWS Numbers account ID")
    if (not isinstance(document, dict) or type(document.get("schema_version")) is not int
            or document["schema_version"] != 1 or document.get("source") != "wows-numbers"
            or document.get("region") != region or document.get("mode") != "pvp"
            or document.get("order") != "pr_desc"):
        raise ValueError("Invalid WoWS Numbers feed schema, region, mode, or ranking order")
    observed_on = _observed_date(document.get("observed_on"))
    ships = document.get("ships")
    if not isinstance(ships, dict) or len(ships) > _MAX_SHIPS:
        raise ValueError("Invalid WoWS Numbers ship list")

    ship_ranks, checked_ship_ids, urls = {}, [], {}
    for ship_id, board in ships.items():
        if not _canonical_id(ship_id) or not isinstance(board, dict):
            raise ValueError("Invalid WoWS Numbers ship board")
        complete = board.get("complete_top10")
        rows = board.get("rows")
        if (type(complete) is not bool or not isinstance(rows, list)
                or len(rows) > _MAX_ROWS or complete and not rows):
            raise ValueError("Invalid WoWS Numbers leaderboard rows or coverage")
        source = _source_url(board.get("source_url"), region, ship_id)
        seen = set()
        for row in rows:
            identifier = row.get("account_id") if isinstance(row, dict) else None
            if not _canonical_id(identifier) or identifier in seen:
                raise ValueError("Invalid or duplicate WoWS Numbers leaderboard account")
            rank = row.get("rank")
            if type(rank) is not int or not 1 <= rank <= 10:
                raise ValueError("Invalid WoWS Numbers leaderboard rank")
            seen.add(identifier)
            if identifier == account_id:
                ship_ranks[ship_id] = rank
        if complete:
            checked_ship_ids.append(ship_id)
        if complete or ship_id in ship_ranks:
            urls[ship_id] = source
    return {"source": "wows-numbers", "region": region, "account_id": account_id,
            "observed_on": observed_on, "ship_ranks": ship_ranks,
            "checked_ship_ids": checked_ship_ids, "source_urls": urls}
