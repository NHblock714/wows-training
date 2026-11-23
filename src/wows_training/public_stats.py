"""Optional official WG snapshots; default offline and never an identity binder.

Reference: https://developers.wargaming.net/reference/all/wows/account/info/
https://developers.wargaming.net/reference/all/wows/account/list/
https://developers.wargaming.net/reference/eu/wows/ships/stats/
Checked 2026-10-21. Account extras use statistics.<mode>; ship extras use <mode>.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
import time
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPRedirectHandler, Request
from uuid import uuid4


REGIONS = {"asia": "https://api.worldofwarships.asia", "eu": "https://api.worldofwarships.eu", "na": "https://api.worldofwarships.com"}
MODES = frozenset(("pvp", "pve", "pvp_solo", "pvp_div2", "pvp_div3", "rank_solo"))
ACCOUNT_MODES = MODES | {"club", "oper_solo", "oper_div"}
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class _Cancelled(Exception):
    pass


def _cancelled(cancel):
    return bool(cancel and (cancel() if callable(cancel) else cancel.is_set()))


def _positive_id(value):
    if type(value) not in (int, str) or not re.fullmatch(r"[0-9]{1,20}", str(value)) or int(value) <= 0:
        return None
    return str(int(value))


def _utc():
    return datetime.now(timezone.utc).isoformat()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_json(url, params, timeout, cancel):
    """Bound response size and per-socket wait; no redirects carrying credentials."""
    if _cancelled(cancel):
        raise _Cancelled()
    request = Request(url, data=urlencode(params).encode("ascii"), headers={
        "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "WOWSTraining/0.3 public-stats",
    })
    deadline = time.monotonic() + timeout
    chunks, size = [], 0
    with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        while True:
            if _cancelled(cancel):
                raise _Cancelled()
            if time.monotonic() > deadline:
                raise TimeoutError()
            block = response.read(min(65536, MAX_RESPONSE_BYTES + 1 - size))
            if not block:
                break
            chunks.append(block)
            size += len(block)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("Response limit exceeded")
    return json.loads(b"".join(chunks).decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite response")))


class PublicStatsClient:
    """Explicit account queries use at most two requests, without retries.

    A complete encyclopedia refresh uses at most 30 bounded ship pages; the
    separate encyclopedia-info request supplies supported languages and version.

    Cancellation is checked between requests and response reads. An in-flight
    socket operation can take up to timeout seconds to return. No access token,
    old project configuration, process environment secret or network auto-fetch.
    Inject transport(url, params, timeout, cancel)->dict for offline HTTP tests.
    """

    def __init__(self, application_id=None, *, enabled=False, timeout=8, max_results=100, transport=None, cancel=None):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0.1 <= timeout <= 30:
            raise ValueError("timeout must be between 0.1 and 30 seconds")
        if type(max_results) is not int or not 1 <= max_results <= 1000:
            raise ValueError("max_results must be between 1 and 1000")
        self._application_id = application_id if isinstance(application_id, str) else None
        self.enabled, self.timeout, self.max_results = enabled is True, timeout, max_results
        self._transport, self.cancel = transport or _http_json, cancel

    def _base(self, region, kind, account_id=None, mode=None):
        region = str(region).lower()
        region = "na" if region == "com" else region
        endpoint = {"search": "/wows/account/list/", "account": "/wows/account/info/", "account_modes":"/wows/account/info/",
                    "ships": "/wows/ships/stats/","clan_search":"/wows/clans/list/","clan":"/wows/clans/info/",
                    "encyclopedia":"/wows/encyclopedia/ships/","encyclopedia_info":"/wows/encyclopedia/info/",
                    "achievements":"/wows/account/achievements/","accounts_batch":"/wows/account/info/"}[kind]
        return {"id": str(uuid4()), "status": "not_fetched", "kind": kind, "region": region,
                "account_id": _positive_id(account_id), "mode": mode, "created_at_utc": _utc(),
                "fetched_at_utc": None, "data": None, "reason": None,
                "source": {"provider": "Wargaming", "url": REGIONS.get(region, "") + endpoint,
                           "scope": "cumulative_public_snapshot_at_collection", "requests": []},
                "limitations": ["Cumulative public statistics at collection time; not historical match-time ability or competitive performance.",
                                "Search results are candidates only and never confirm a personal identity."]}

    def _ready(self, result, allowed_modes=None):
        if result["region"] not in REGIONS or (allowed_modes is not None and result["mode"] not in allowed_modes):
            result.update(status="unsupported", reason="region_or_mode_not_supported")
            return False
        if not self.enabled or not self._application_id:
            result.update(status="not_fetched", reason="network_disabled" if not self.enabled else "application_id_missing")
            return False
        if _cancelled(self.cancel):
            result.update(status="not_fetched", reason="cancelled")
            return False
        return True

    def _request(self, result, endpoint, params, *, include_meta=False):
        if _cancelled(self.cancel):
            raise _Cancelled()
        url = REGIONS[result["region"]] + endpoint
        result["source"]["requests"].append({"url": url, "parameters": dict(params)})
        result["fetched_at_utc"] = _utc()
        response = self._transport(url, dict(params, application_id=self._application_id), self.timeout, self.cancel)
        if _cancelled(self.cancel):
            raise _Cancelled()
        if not isinstance(response, dict) or response.get("status") != "ok" or "data" not in response:
            # API message/value may contain a credential; only a numeric code is retained.
            code = (response.get("error") or {}).get("code") if isinstance(response, dict) else None
            if type(code) is int:
                result["api_error_code"] = code
            raise ValueError("Invalid API response")
        # Also constrain injected transport data and prohibit non-JSON/nonfinite values.
        if len(json.dumps(response, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
            raise ValueError("Response limit exceeded")
        hidden = (response.get("meta") or {}).get("hidden", [])
        if isinstance(hidden, list) and result.get("account_id") is not None and result.get("account_id") in {_positive_id(value) for value in hidden}:
            result.update(status="private", reason="hidden_profile_meta")
            return None
        return (response['data'],response.get('meta')) if include_meta else response["data"]

    def encyclopedia_info(self,region):
        result=self._base(region,'encyclopedia_info')
        if not self._ready(result):return result
        try:
            data=self._request(result,'/wows/encyclopedia/info/',{'language':'en'})
            if not isinstance(data,dict) or not isinstance(data.get('game_version'),str):raise ValueError('Missing encyclopedia version')
            result.update(status='ok',data={key:data.get(key) for key in ('game_version','ships_updated_at','languages','ship_types')})
        except Exception as error:return self._error(result,error)
        return result

    def encyclopedia(self,region,language='en',progress=None):
        """Fetch a bounded, complete ship directory; partial pages are never published."""
        result=self._base(region,'encyclopedia')
        if not self._ready(result):return result
        if not isinstance(language,str) or not re.fullmatch(r'[a-z]{2}(?:-[a-z]{2})?',language):return dict(result,status='error',reason='invalid_language')
        ships={};page_total=None;ship_total=None
        try:
            for page in range(1,31):
                if progress:progress(f'读取舰船百科 {page}/{page_total or "…"}')
                data,meta=self._request(result,'/wows/encyclopedia/ships/',{'language':language,'page_no':page,'limit':100,
                    'fields':'ship_id,ship_id_str,name,tier,type,nation,default_profile.artillery'},include_meta=True)
                total=meta.get('page_total') if isinstance(meta,dict) else None
                if type(total) is not int or not 1<=total<=30 or (page_total is not None and total!=page_total):raise ValueError('Invalid encyclopedia pagination')
                page_total=total
                if not isinstance(data,dict) or len(data)>100:raise ValueError('Invalid ship directory page')
                count=meta.get('count');all_ships=meta.get('total')
                if type(count) is not int or count!=len(data) or type(all_ships) is not int or not 1<=all_ships<=3000:raise ValueError('Invalid encyclopedia count')
                if (ship_total is not None and ship_total!=all_ships) or (all_ships+99)//100!=page_total:raise ValueError('Inconsistent encyclopedia count')
                ship_total=all_ships
                if len(data)!=(100 if page<page_total else ship_total-100*(page_total-1)):raise ValueError('Incomplete encyclopedia page')
                for key,row in data.items():
                    sid=_positive_id(key)
                    if sid is None or sid in ships or not isinstance(row,dict) or _positive_id(row.get('ship_id'))!=sid:raise ValueError('Invalid encyclopedia ship identity')
                    if not isinstance(row.get('name'),str) or type(row.get('tier')) is not int or not 1<=row['tier']<=11:raise ValueError('Invalid encyclopedia metadata')
                    if not isinstance(row.get('type'),str) or not isinstance(row.get('nation'),str):raise ValueError('Invalid encyclopedia ship category')
                    ships[sid]=row
                if page==page_total:break
            if not ships or len(ships)!=ship_total:raise ValueError('Incomplete ship directory')
            result.update(status='ok',data={'ships':ships,'language':language,'complete':True,'page_total':page_total})
        except Exception as error:return self._error(result,error)
        return result

    @staticmethod
    def _error(result, error):
        result.update(status="not_fetched" if isinstance(error, _Cancelled) else "error",
                      reason="cancelled" if isinstance(error, _Cancelled) else "request_or_response_failed", data=None)
        return result

    def search(self, region, nickname, limit=20):
        result = self._base(region, "search")
        if not self._ready(result):
            return result
        if not isinstance(nickname, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,25}", nickname) or type(limit) is not int or limit < 1:
            return dict(result, status="error", reason="invalid_search")
        limit = min(limit, self.max_results, 100)
        try:
            data = self._request(result, "/wows/account/list/", {"search": nickname, "type": "startswith", "limit": limit})
            if not isinstance(data, list) or len(data) > limit:
                raise ValueError("Invalid candidate response")
            candidates = []
            for row in data:
                account_id = _positive_id(row.get("account_id")) if isinstance(row, dict) else None
                if account_id is None or not isinstance(row.get("nickname"), str):
                    raise ValueError("Invalid account candidate")
                candidates.append({"account_id": account_id, "nickname": row["nickname"], "region": result["region"], "confirmed": False})
            result.update(status="ok" if candidates else "not_found", data={"candidates": candidates, "limit": limit, "complete_directory": False})
        except Exception as error:
            return self._error(result, error)
        return result

    def _account_info(self, result, mode, *, include_statistics=True):
        params = {"account_id": result["account_id"]}
        if include_statistics and mode != "pvp":
            params["extra"] = "statistics." + mode
        if not include_statistics:
            params["fields"] = "account_id,nickname,hidden_profile,updated_at,stats_updated_at"
        data = self._request(result, "/wows/account/info/", params)
        return self._checked_account_info(result,data)

    @staticmethod
    def _checked_account_info(result,data):
        if result["status"] == "private":
            return None
        if not isinstance(data, dict) or result["account_id"] not in data:
            raise ValueError("Missing account response key")
        info = data[result["account_id"]]
        if info is None:
            result.update(status="not_found", reason="account_not_found")
            return None
        if not isinstance(info, dict) or _positive_id(info.get("account_id")) != result["account_id"]:
            raise ValueError("Account response ID mismatch")
        if isinstance(info.get('nickname'),str):result['nickname']=info['nickname']
        if info.get("hidden_profile") is True:
            result.update(status="private", reason="hidden_profile")
            return None
        if info.get("hidden_profile") is not False:
            raise ValueError("Public/private state is not supplied")
        return info

    def account_modes(self,region,account_id,modes=None):
        result=self._base(region,'account_modes',account_id)
        if not self._ready(result):return result
        if result['account_id'] is None:return dict(result,status='error',reason='invalid_account_id')
        modes=list(dict.fromkeys(modes or sorted(ACCOUNT_MODES)))
        if not modes or any(mode not in ACCOUNT_MODES for mode in modes):return dict(result,status='unsupported',reason='mode_not_supported')
        try:
            params={'account_id':result['account_id']};extras=['statistics.'+m for m in modes if m!='pvp']
            if extras:params['extra']=','.join(extras)
            data=self._request(result,'/wows/account/info/',params);info=self._checked_account_info(result,data)
            if info is None:return result
            values={}
            for mode in modes:
                stats=(info.get('statistics') or {}).get(mode)
                if stats is None:values[mode]={'status':'unsupported','statistics':None};continue
                if not isinstance(stats,dict) or type(stats.get('battles')) is not int or stats['battles']<0:raise ValueError('Invalid mode statistics')
                values[mode]={'status':'ok' if stats['battles'] else 'no_battles','statistics':stats}
            result.update(status='ok',data={'nickname':info.get('nickname'),'modes':values,
                'source_updated_at':info.get('updated_at'),'stats_updated_at':info.get('stats_updated_at')})
        except Exception as error:return self._error(result,error)
        return result

    def search_clans(self,region,query,limit=20):
        result=self._base(region,'clan_search')
        if not self._ready(result):return result
        if not isinstance(query,str) or not 2<=len(query.strip())<=100 or any(ord(c)<32 for c in query) or type(limit) is not int or limit<1:
            return dict(result,status='error',reason='invalid_search')
        limit=min(limit,self.max_results,100)
        try:
            data=self._request(result,'/wows/clans/list/',{'search':query.strip(),'limit':limit})
            if not isinstance(data,list) or len(data)>limit:raise ValueError('Invalid clan candidates')
            rows=[]
            for value in data:
                identifier=_positive_id(value.get('clan_id')) if isinstance(value,dict) else None
                if not identifier or not isinstance(value.get('name'),str):raise ValueError('Invalid clan identity')
                rows.append({'clan_id':identifier,'name':value['name'],'tag':value.get('tag'),
                             'members_count':value.get('members_count'),'region':result['region']})
            result.update(status='ok' if rows else 'not_found',data={'candidates':rows,'limit':limit})
        except Exception as error:return self._error(result,error)
        return result

    def achievements(self, region, account_id):
        result = self._base(region, "achievements", account_id)
        if not self._ready(result):
            return result
        identifier = result["account_id"]
        if not identifier:
            return dict(result, status="error", reason="invalid_account_id")
        try:
            data = self._request(result, "/wows/account/achievements/", {"account_id": identifier})
            if result["status"] == "private":
                return result
            record = data.get(identifier) if isinstance(data, dict) else None
            battles = record.get("battle") if isinstance(record, dict) else None
            if not isinstance(battles, dict) or any(type(v) is not int or v < 0 for v in battles.values()):
                return dict(result, status="not_fetched", reason="achievement_data_unavailable")
            result.update(status="ok", data={"battle": battles})
        except Exception as error:
            return self._error(result, error)
        return result

    def accounts_batch(self, region, account_ids):
        """Fetch only public PVP summaries for this selected clan roster."""
        result = self._base(region, "accounts_batch")
        if not self._ready(result):
            return result
        if not isinstance(account_ids, (list, tuple)) or len(account_ids) > 100:
            return dict(result, status="error", reason="invalid_account_id")
        ids = list(dict.fromkeys(_positive_id(i) for i in account_ids))
        if None in ids:
            return dict(result, status="error", reason="invalid_account_id")
        if not ids:
            return dict(result, status="ok", data={"players": {}})
        try:
            data, meta = self._request(result, "/wows/account/info/", {
                "account_id": ",".join(ids), "fields": "account_id,nickname,hidden_profile,statistics.pvp"
            }, include_meta=True)
            if not isinstance(data, dict) or not set(data).issubset(ids):
                raise ValueError("Unexpected accounts in clan result")
            hidden = {_positive_id(i) for i in (meta or {}).get("hidden", [])}
            players = {}
            for aid in ids:
                info = data.get(aid)
                if info is not None and (not isinstance(info, dict) or _positive_id(info.get("account_id")) != aid):
                    raise ValueError("Clan member identity mismatch")
                if aid in hidden or (info or {}).get("hidden_profile") is True:
                    players[aid] = {"account_id": int(aid), "nickname": (info or {}).get("nickname"), "hidden_profile": True}
                    continue
                pvp = ((info or {}).get("statistics") or {}).get("pvp")
                if (info or {}).get("hidden_profile") is not False or not isinstance(pvp, dict):
                    continue
                if type(pvp.get("battles")) is not int or pvp["battles"] < 0:
                    raise ValueError("Invalid clan member statistics")
                players[aid] = {"account_id": int(aid), "nickname": info.get("nickname"),
                                "hidden_profile": False, "statistics": {"pvp": pvp}}
            result.update(status="ok", data={"players": players})
        except Exception as error:
            return self._error(result, error)
        return result

    def clan(self,region,clan_id):
        result=self._base(region,'clan');identifier=_positive_id(clan_id);result['clan_id']=identifier
        if not self._ready(result):return result
        if identifier is None:return dict(result,status='error',reason='invalid_clan_id')
        try:
            data=self._request(result,'/wows/clans/info/',{'clan_id':identifier,'extra':'members'})
            value=data.get(identifier) if isinstance(data,dict) else None
            if value is None:return dict(result,status='not_found',reason='clan_not_found')
            if not isinstance(value,dict) or _positive_id(value.get('clan_id'))!=identifier:raise ValueError('Clan ID mismatch')
            raw=value.get('members') or {};members=[]
            if isinstance(raw,dict):
                if any(not isinstance(v,dict) for v in raw.values()):raise ValueError('Invalid clan member')
                if any(_positive_id(v.get('account_id')) not in (None,_positive_id(k)) for k,v in raw.items()):raise ValueError('Clan member ID mismatch')
                raw=[dict(v,account_id=k) for k,v in raw.items()]
            if not isinstance(raw,list) or len(raw)>100:raise ValueError('Invalid clan member list')
            for member in raw:
                account=_positive_id(member.get('account_id')) if isinstance(member,dict) else None
                if not account:raise ValueError('Invalid member ID')
                members.append({'account_id':account,'nickname':member.get('account_name') or member.get('nickname'),
                                'role':member.get('role'),'region':result['region']})
            result.update(status='ok',data={'clan_id':identifier,'name':value.get('name'),'tag':value.get('tag'),
                'description':value.get('description'),'motto':value.get('motto'),'members_count':value.get('members_count'),
                'members':members,'updated_at':value.get('updated_at')})
        except Exception as error:return self._error(result,error)
        return result

    def account(self, region, account_id, mode="pvp"):
        result = self._base(region, "account", account_id, mode)
        if not self._ready(result, ACCOUNT_MODES):
            return result
        if result["account_id"] is None:
            return dict(result, status="error", reason="invalid_account_id")
        try:
            info = self._account_info(result, mode)
            if info is None:
                return result
            stats = (info.get("statistics") or {}).get(mode)
            if stats is None:
                return dict(result, status="unsupported", reason="mode_data_not_returned")
            if not isinstance(stats, dict) or type(stats.get("battles")) is not int or stats["battles"] < 0:
                raise ValueError("Battle count unavailable")
            result.update(status="no_battles" if stats["battles"] == 0 else "ok", data={
                "nickname": info.get("nickname"), "account_id": result["account_id"], "mode": mode,
                "statistics": stats, "source_updated_at": info.get("updated_at"), "stats_updated_at": info.get("stats_updated_at"),
            })
        except Exception as error:
            return self._error(result, error)
        return result

    def ships(self, region, account_id, mode="pvp", ship_ids=None):
        result = self._base(region, "ships", account_id, mode)
        if not self._ready(result, MODES):
            return result
        if result["account_id"] is None:
            return dict(result, status="error", reason="invalid_account_id")
        if ship_ids is not None and (not isinstance(ship_ids, (list, tuple)) or not ship_ids or len(ship_ids) > min(self.max_results, 100)
                                     or any(_positive_id(value) is None for value in ship_ids)):
            return dict(result, status="error", reason="invalid_ship_filter")
        try:
            info = self._account_info(result, mode, include_statistics=False)
            if info is None:
                return result
            params = {"account_id": result["account_id"]}
            if mode != "pvp":
                params["extra"] = mode
            if ship_ids:
                params["ship_id"] = ",".join(dict.fromkeys(_positive_id(value) for value in ship_ids))
            data = self._request(result, "/wows/ships/stats/", params)
            if result["status"] == "private":
                return result
            rows = data.get(result["account_id"]) if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise ValueError("Ship statistics missing or privacy state changed")
            if len(rows) > self.max_results:
                return dict(result, status="error", reason="result_limit_exceeded_use_ship_filter")
            ships, seen = [], set()
            for row in rows:
                sid = _positive_id(row.get("ship_id")) if isinstance(row, dict) else None
                if sid is None or sid in seen:
                    raise ValueError("Invalid ship identity")
                seen.add(sid)
                if row.get('account_id') is not None and _positive_id(row['account_id'])!=result['account_id']:
                    raise ValueError('Ship account ID mismatch')
                if ship_ids and _positive_id(row["ship_id"]) not in {_positive_id(value) for value in ship_ids}:
                    raise ValueError("Unexpected ship outside requested filter")
                stats = row.get(mode)
                if stats is None:
                    return dict(result, status="unsupported", reason="mode_data_not_returned")
                if not isinstance(stats, dict) or type(stats.get("battles")) is not int or stats["battles"] < 0:
                    raise ValueError("Ship battle count unavailable")
                ships.append({"ship_id": sid, "mode": mode, "statistics": stats,
                              "last_battle_time": row.get("last_battle_time")})
            result.update(status="ok" if any(row["statistics"]["battles"] for row in ships) else "no_battles",
                          data={"nickname": info.get("nickname"), "ships": ships, "complete_for_requested_filter": True,
                                "ship_filter": [_positive_id(value) for value in ship_ids] if ship_ids else None})
        except Exception as error:
            return self._error(result, error)
        return result
