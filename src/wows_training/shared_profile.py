"""Load a holder-authorized profile using the official profile site's access-code flow."""

from dataclasses import dataclass, field
from http.client import HTTPException
import json
import re
import time
from unicodedata import category
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from .career_rating import number
from .public_stats import MODES, _NoRedirect, _cancelled, _positive_id


# The official profile page supplies the regional Vortex origin in its bootstrap
# configuration. Its public profile route and account, fleet and achievement GETs
# are used by the visitor-facing page (profile/e13f110d/main.js on wgcdn.co).
_DOMAINS = {"asia": "worldofwarships.asia", "eu": "worldofwarships.eu", "na": "worldofwarships.com"}
_SHARE_PATH = re.compile(r"/statistics/([1-9][0-9]{0,19})/ac/([A-Za-z0-9_-]{1,256})/?")
_MAX_RESPONSE = 8 * 1024 * 1024
_TIMEOUT_SECONDS = 8
_STAT_FIELDS = {
    "wins": "wins", "losses": "losses", "draws": "draws", "damage_dealt": "damage_dealt",
    "frags": "frags", "survived": "survived_battles", "premium_exp": "xp", "planes_killed": "planes_killed",
    "art_agro": "art_agro", "tpd_agro": "torpedo_agro", "scouting_damage": "damage_scouting",
    "ships_spotted": "ships_spotted", "max_ships_spotted": "max_ships_spotted",
    "capture_points": "capture_points", "dropped_capture_points": "dropped_capture_points",
}
_REJECTED = "官方未向此分享链接开放战绩，请确认链接仍有效。"
_BAD_RESPONSE = "官方资料响应缺失或格式已变化。"
_NETWORK_ERROR = "暂时无法连接官方资料服务，请稍后再试。"


class SharedProfileError(ValueError):
    """An actionable error whose message contains no user or server payload."""


@dataclass(frozen=True, slots=True)
class ShareLink:
    region: str
    account_id: str
    access_code: str = field(repr=False)


def parse_share_link(raw_url, region, account_id):
    """Accept only the official HTTPS share path of the selected region and account."""
    region = "na" if region == "com" else region
    domain = _DOMAINS.get(region) if isinstance(region, str) else None
    account_id = _positive_id(account_id)
    if not domain or account_id is None:
        raise SharedProfileError("请先选择需要查看的账号。")
    if not isinstance(raw_url, str) or not 0 < len(raw_url) <= 400:
        raise SharedProfileError("请输入游戏内复制的官方战绩分享链接。")
    raw_url = raw_url.strip()
    if any(not 32 < ord(char) < 127 for char in raw_url):
        raise SharedProfileError("请输入游戏内复制的官方战绩分享链接。")
    try:
        url = urlsplit(raw_url)
    except ValueError:
        raise SharedProfileError("请输入游戏内复制的官方战绩分享链接。") from None
    match = _SHARE_PATH.fullmatch(url.path)
    if (url.scheme != "https" or not match or "?" in raw_url or "#" in raw_url
            or url.netloc.lower() not in {f"profile.{host}" for host in _DOMAINS.values()}):
        raise SharedProfileError("请输入游戏内复制的官方战绩分享链接。")
    if url.netloc.lower() != f"profile.{domain}":
        raise SharedProfileError("分享链接的区服与当前账号不一致。")
    if match[1] != account_id:
        raise SharedProfileError("分享链接的账号与当前账号不一致。")
    return ShareLink(region, account_id, match[2])


def _check_cancel(cancel):
    if _cancelled(cancel):
        raise InterruptedError("分享战绩查询已取消。")


def _request_json(request, cancel):
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    raw = bytearray()
    with build_opener(_NoRedirect()).open(request, timeout=_TIMEOUT_SECONDS) as response:
        if response.headers.get_content_type() != "application/json":
            raise SharedProfileError(_BAD_RESPONSE)
        while True:
            _check_cancel(cancel)
            if time.monotonic() >= deadline:
                raise TimeoutError()
            chunk = response.read(min(65536, _MAX_RESPONSE + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > _MAX_RESPONSE:
                raise SharedProfileError(_BAD_RESPONSE)
        if response.length:
            raise SharedProfileError(_BAD_RESPONSE)
    _check_cancel(cancel)
    if time.monotonic() >= deadline:
        raise TimeoutError()
    return json.loads(raw.decode("utf-8"))


def _fetch_record(link, suffix, transport, cancel):
    _check_cancel(cancel)
    domain = _DOMAINS[link.region]
    query = urlencode({"ac": link.access_code})
    request = Request(f"https://vortex.{domain}/api/accounts/{link.account_id}/{suffix}?{query}", headers={
        "Accept": "application/json", "Origin": f"https://profile.{domain}", "Cache-Control": "no-store",
    })
    try:
        response = transport(request, cancel)
    except InterruptedError:
        raise InterruptedError("分享战绩查询已取消。") from None
    except HTTPError as error:
        error.close()
        if error.code in (400, 401, 403, 404):
            raise SharedProfileError(_REJECTED) from None
        raise SharedProfileError(_NETWORK_ERROR) from None
    except (ValueError, RecursionError):
        raise SharedProfileError(_BAD_RESPONSE) from None
    except (OSError, HTTPException):
        raise SharedProfileError(_NETWORK_ERROR) from None
    _check_cancel(cancel)
    if not isinstance(response, dict) or response.get("status") != "ok":
        raise SharedProfileError(_BAD_RESPONSE)
    data = response.get("data")
    if not isinstance(data, dict) or set(data) != {link.account_id}:
        raise SharedProfileError("官方响应的账号与当前账号不一致。")
    record = data[link.account_id]
    if not isinstance(record, dict) or record.get("hidden_profile", False) is not False:
        raise SharedProfileError(_REJECTED)
    if any(key in record and _positive_id(record[key]) != link.account_id for key in ("id", "account_id")):
        raise SharedProfileError("官方响应的账号与当前账号不一致。")
    return record


def _mode_stats(statistics):
    modes = {}
    for mode in sorted(MODES):
        row = statistics.get(mode)
        battles = row.get("battles_count") if isinstance(row, dict) else None
        if type(battles) is not int or battles < 0:
            continue
        values = {destination: row[source] for source, destination in _STAT_FIELDS.items() if number(row.get(source))}
        for group, weapon in (("main_battery", "main"), ("torpedoes", "tpd")):
            weapon_counts = {kind: row.get(f"{kind}_by_{weapon}") for kind in ("hits", "shots")}
            counts = {kind: count for kind, count in weapon_counts.items() if number(count)}
            if counts:
                values[group] = counts
        modes[mode] = {"battles": battles, **values}
    return modes


def _optional_statistics(link, suffix, transport, cancel):
    try:
        record = _fetch_record(link, suffix, transport, cancel)
    except SharedProfileError:
        return None
    stats = record.get("statistics")
    return stats if isinstance(stats, dict) else None


def load_shared_profile(raw_url, region, account_id, *, cancel=None, transport=None):
    """Return only projected session data. The share code remains in the request scope."""
    link = parse_share_link(raw_url, region, account_id)
    transport = transport or _request_json
    account = _fetch_record(link, "", transport, cancel)
    statistics = account.get("statistics")
    if not isinstance(statistics, dict):
        raise SharedProfileError(_BAD_RESPONSE)
    name = account.get("name")
    if isinstance(name, str):
        name = "".join(char for char in name[:64] if category(char)[0] != "C").strip()
    if not name or not isinstance(name, str) or link.access_code in name:
        name = link.account_id
    created = account.get("created_at")
    created = int(created) if number(created) and 0 < created <= 253402300799 else None

    fleet = _optional_statistics(link, "ships/", transport, cancel)
    ships = None if fleet is None else []
    if fleet is not None:
        for ship_id, stats in fleet.items():
            if ship_id == _positive_id(ship_id) and isinstance(stats, dict):
                modes = _mode_stats(stats)
                if modes:
                    ships.append({"ship_id": ship_id, "modes": modes})
    medals = _optional_statistics(link, "achievements/", transport, cancel)
    achievements = None
    if medals is not None and isinstance(medals.get("achievements"), dict):
        achievements = {key: row["count"] for key, row in medals["achievements"].items()
                        if key == _positive_id(key) and isinstance(row, dict)
                        and type(row.get("count")) is int and row["count"] >= 0}

    return {"region": link.region, "account_id": link.account_id, "nickname": name,
            "account_created_at": created, "modes": _mode_stats(statistics), "ships": ships,
            "achievements": achievements}
