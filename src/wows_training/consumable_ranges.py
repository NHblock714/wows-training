"""Exact-build base ship-detection radius for an observed radar or hydro activation."""
from collections import Counter
from functools import lru_cache
import hashlib
import json

from .ordinary_replay import CURRENT, _root, current_combat_verified


RANGE_PROFILE_SHA = "15bd8eaaa59918a33cfda8efebe55c01d8967d8efd5ccb28573bbd78ea2de1e1"


@lru_cache(maxsize=1)
def _profile():
    raw = (_root() / "consumable_ranges-15.10.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != RANGE_PROFILE_SHA:
        raise ValueError("消耗品探测距离资料校验失败。")
    return json.loads(raw)


def attach_consumable_ranges(report):
    """Only upgrade genuine activations with matching current ship/client provenance."""
    if not current_combat_verified(report):
        return 0
    uses = [r for r in (report["combat"].get("consumables") or [])
            if type(r.get("type")) is int and r["type"] in (10, 12)]
    if not uses:
        return 0
    rows = report.get("roster") or []
    counts = Counter(r["shipId"] for r in rows if type(r.get("shipId")) is int)
    ships = {r["shipId"]: r["shipParamsId"] for r in rows
             if type(r.get("shipId")) is int and counts[r["shipId"]] == 1 and type(r.get("shipParamsId")) is int}
    profile = _profile()
    updated = 0
    for event in uses:
        ship = ships.get(event.get("entity_id"))
        radius = profile["ships"].get(str(ship), {}).get(str(event["type"]))
        if radius is not None:
            event["range_engine"] = radius
            event["range_source"] = {"client_version": CURRENT, "ship_params_id": ship, "field": "logic.distShip",
                                     "scope": "base_ship_detection_range", "profile_sha256": RANGE_PROFILE_SHA}
            updated += 1
    return updated
