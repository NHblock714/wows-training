"""F1 WOWS challenge catalogue evaluated against public account snapshots."""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import math

from ..achievement_ship_rosters import historical_ship_roster
from ..career_catalog import current_ship_catalog, ship_edition
from ..career_rating import ratio, ship_display_name
from ..public_stats import normalize_battle_achievements
from .achievement_catalog import ACHIEVEMENT_COPY


@dataclass(frozen=True)
class _Evidence:
    account: dict
    fleet: list
    fleet_complete: bool
    awards: dict | None
    solo: dict | None
    solo_fleet: dict | None
    created_at: int | None
    as_of: str | None
    region: str | None
    numbers_rankings: dict | None


def _value(record, *keys):
    for key in keys:
        record = record.get(key) if isinstance(record, dict) else None
    return record if type(record) in (int, float) and 0 <= record < 2**64 and math.isfinite(record) else None


def _account(*keys):
    def measure(evidence):
        value = _value(evidence.account, *keys)
        return value, value is not None
    return measure


def _per_battle(field):
    def measure(evidence):
        amount = _value(evidence.account, field)
        battles = _value(evidence.account, "battles")
        if amount is None or not battles:
            return None, battles == 0
        return amount / battles, True
    return measure


def _eligible_ship(ship, metric, *, tier, max_tier, battles, kind, shots):
    stats, metadata = ship
    count = stats["battles"]
    if count < battles:
        return False, None
    field = metric.removesuffix("_hit_rate")
    if metric.endswith("_hit_rate"):
        fired = _value(stats, field, "shots")
        if fired is not None and fired < shots:
            return False, None
    if tier and metadata.get("tier") is not None and metadata["tier"] < tier:
        return False, None
    if max_tier and metadata.get("tier") is not None and metadata["tier"] > max_tier:
        return False, None
    if kind and metadata.get("type") and metadata["type"] != kind:
        return False, None
    if ((tier or max_tier) and metadata.get("tier") is None) or (kind and not metadata.get("type")):
        return True, None
    if metric.endswith("_hit_rate"):
        hits = _value(stats, field, "hits")
        return True, hits / fired * 100 if fired and hits is not None and hits <= fired else None
    if metric == "avg_damage":
        damage = _value(stats, "damage_dealt")
        return True, damage / count if damage is not None else None
    return True, _value(stats, metric)


def _ship(metric, *, tier=0, max_tier=0, battles=1, kind=None, shots=0, minimum=False):
    def measure(evidence):
        complete = evidence.fleet_complete
        values = []
        for ship in evidence.fleet:
            eligible, value = _eligible_ship(ship, metric, tier=tier, max_tier=max_tier,
                                             battles=battles, kind=kind, shots=shots)
            if eligible:
                if value is None:
                    complete = False
                else:
                    values.append(value)
        return (min if minimum else max)(values, default=None), complete
    return measure


def _ship_displayed_average(target):
    def measure(evidence):
        complete = evidence.fleet_complete
        matched_ships = []
        for stats, metadata in evidence.fleet:
            average = ratio(stats.get("damage_dealt"), stats["battles"])
            if average is None:
                complete = False
            # Match the ship table's :,.0f rounding, including half-to-even ties.
            elif round(average) == target:
                matched_ships.append(metadata["name"])
        return (target if matched_ships else None, complete or bool(matched_ships),
                {"matched_ships": matched_ships})
    return measure


def _fleet_share(field, qualifies):
    def measure(evidence):
        total = _value(evidence.account, "battles")
        if not total or sum(stats["battles"] for stats, _ in evidence.fleet) > total:
            return None, total == 0 and evidence.fleet_complete
        complete = evidence.fleet_complete
        count = 0
        for stats, metadata in evidence.fleet:
            value = metadata.get(field)
            if value is None:
                complete = False
            elif qualifies(value):
                count += stats["battles"]
        return count / total * 100, complete
    return measure


def _edition(edition):
    def measure(evidence):
        series = _series_record(evidence.fleet, evidence.fleet_complete, edition)
        return series["ships"], series["complete"]
    return measure


def _series_record(fleet, complete, edition):
    matches = [stats for stats, meta in fleet if meta["edition"] == edition]
    complete = complete and all(meta["edition_known"] for _, meta in fleet)
    return {"id": edition, "ships": len(matches) if matches or complete else None,
            "battles": sum(stats["battles"] for stats in matches) if matches or complete else None,
            "complete": complete}


def _award(key, *, observed_only=False):
    def measure(evidence):
        awards = evidence.awards
        if awards is None:
            return None, False
        value = _value(awards, key)
        if key not in awards and not observed_only:
            value = 0
        return value, value is not None
    return measure


def _win_rate(evidence, *, solo=False):
    record = evidence.solo if solo else evidence.account
    battles, wins = _value(record, "battles"), _value(record, "wins")
    if battles and wins is not None and wins <= battles:
        return wins / battles * 100, True
    return None, battles == 0


def _solo_battles(evidence):
    value = _value(evidence.solo, "battles")
    return value, value is not None


def _named_ship(ship_id, metric="battles"):
    def measure(evidence):
        stats = next((row for row, meta in evidence.fleet if meta["id"] == ship_id), None)
        if stats is None:
            return (0, True) if evidence.fleet_complete else (None, False)
        if metric in ("avg_damage", "avg_scouting"):
            field = "damage_dealt" if metric == "avg_damage" else "damage_scouting"
            total = _value(stats, field)
            value = total / stats["battles"] if total is not None else None
        else:
            value = _value(stats, metric)
        return value, value is not None
    return measure


def _ship_solo_share(ship_id):
    total_battles = _named_ship(ship_id)

    def measure(evidence):
        total, complete = total_battles(evidence)
        if not total:
            return None, complete
        fleet = evidence.solo_fleet or {}
        if fleet.get("status") == "no_battles":
            return 0, True
        rows = fleet.get("ships")
        if fleet.get("status") != "ok" or not isinstance(rows, list):
            return None, False
        matches = [row for row in rows if isinstance(row, dict) and str(row.get("ship_id")) == ship_id]
        if len(matches) == 1:
            solo = _value(matches[0], "statistics", "battles")
            return (100 * solo / total, True) if type(solo) is int and solo <= total else (None, False)
        if not matches:
            # Absence is a known zero only when the solo fleet accounts for all recorded solo battles.
            counts = [_value(row, "statistics", "battles") for row in rows]
            if all(type(count) is int for count in counts) and sum(counts) == _value(evidence.solo, "battles"):
                return 0, True
        return None, False

    return measure


def _combine(measures, combine):
    def measure(evidence):
        observations = [read(evidence) for read in measures]
        values = [value for value, _ in observations if value is not None]
        return (combine(values) if values else None), all(complete for _, complete in observations)
    return measure


def _requires(measure, *criteria):
    def check(evidence):
        unknown = False
        for read, target, comparison in criteria:
            value, complete = read(evidence)
            if value is not None and _COMPARISONS[comparison](value, target):
                continue
            if complete:
                return None, True
            unknown = True
        return (None, False) if unknown else measure(evidence)
    return check


def _at_least(measure, target):
    return measure, target, "at_least"


def _german_battleships(evidence):
    complete = evidence.fleet_complete
    count = 0
    for stats, meta in evidence.fleet:
        kind, nation = meta["type"], meta["nation"]
        if kind == "Battleship" and nation == "germany":
            count += stats["battles"]
        elif kind in (None, "Battleship") and nation in (None, "germany"):
            complete = False
    return count, complete


def _submarine_battles(evidence):
    count = sum(stats["battles"] for stats, meta in evidence.fleet if meta["type"] == "Submarine")
    total = _value(evidence.account, "battles")
    complete = evidence.fleet_complete and all(meta["type"] for _, meta in evidence.fleet)
    if total == 0 and not evidence.fleet:
        return 0, True
    # Zero is meaningful only with complete ship and type coverage.
    return (count, True) if complete or count else (None, False)


def _ship_advantage(left, right):
    def measure(evidence):
        a, _ = left(evidence)
        b, _ = right(evidence)
        if a is None or b is None:
            return None, False
        return max(0, a - b), True
    return measure


def _roster(kind, *, percent=False, every=False):
    def measure(evidence):
        roster = historical_ship_roster(kind, evidence.region)
        members = roster.ship_ids
        if not members or ((percent or every) and not roster.complete):
            return None, False
        battles = {meta["id"]: stats["battles"] for stats, meta in evidence.fleet if meta["id"] in members}
        if every:
            if members <= battles.keys():
                return min(battles.values()), True
            return (0, True) if evidence.fleet_complete else (None, False)
        complete = evidence.fleet_complete and roster.complete
        value = len(battles) / len(members) * 100 if percent else len(battles)
        return (value, complete) if complete or value else (None, False)
    return measure


def _numbers_top10(evidence):
    snapshot = evidence.numbers_rankings
    if (not isinstance(snapshot, dict) or snapshot.get("region") != evidence.region
            or snapshot.get("source") != "wows-numbers"):
        return None, False
    ranks = snapshot.get("ship_ranks")
    if not isinstance(ranks, dict):
        return None, False
    eligible = {meta["id"] for _, meta in evidence.fleet if (meta["tier"] or 0) >= 9}
    valid = {ship_id: rank for ship_id, rank in ranks.items()
             if ship_id in eligible and type(rank) is int and rank >= 1}
    matching = sum(rank <= 10 for rank in valid.values())
    checked = snapshot.get("checked_ship_ids")
    known = set(valid)
    if isinstance(checked, list):
        known.update(ship_id for ship_id in checked if isinstance(ship_id, str) and ship_id in eligible)
    complete = (evidence.fleet_complete and all(meta["tier"] is not None for _, meta in evidence.fleet)
                and eligible <= known)
    return (matching, complete) if complete or matching else (None, False)


def _account_age(evidence):
    try:
        created = datetime.fromtimestamp(evidence.created_at, tz=timezone.utc)
        as_of = datetime.fromisoformat(evidence.as_of).astimezone(timezone.utc) if evidence.as_of else datetime.now(timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None, False
    if not evidence.created_at or as_of < created:
        return None, False
    years = as_of.year - created.year - ((as_of.month, as_of.day) < (created.month, created.day))
    return years, True


@dataclass(frozen=True)
class Challenge:
    id: str
    group: str
    name: str
    requirement: str
    description: str
    target: int
    unit: str
    measure: Callable
    comparison: str = "at_least"
    rarity: str = "普通"


def _challenge(identifier, target, unit, measure, comparison="at_least"):
    group, name, rarity, requirement, description = ACHIEVEMENT_COPY[identifier]
    return Challenge(identifier, group, name, requirement, description, target, unit, measure, comparison, rarity)


_COMPARISONS = {
    "at_least": lambda value, target: value >= target,
    "at_most": lambda value, target: value <= target,
    "equal": lambda value, target: value == target,
}

_KURFURST = "4179572528"
_PREUSSEN = "3969857328"
_MECKLENBURG = "3655284528"
_ESSEX = "4273977328"
_INCOMPARABLE = "3655284688"
_GERMAN_BB_2000 = _at_least(_german_battleships, 2_000)
_TIGER_TRIO = tuple(map(_named_ship, ("3542005712", "3751720912", "3542004944")))
_AZUMA, _YOSHINO, _KEARSARGE = map(_named_ship, ("3760109264", "3749623504", "3750705136"))
_WITHERER, _ARSONIST = _award("WITHERING"), _award("ARSONIST")

CHALLENGES = (
    _challenge("battle_frags", 8, "艘", _account("max_frags_battle")),
    _challenge("battle_agro", 8_000_000, "点", _account("max_total_agro")),
    _challenge("battle_planes", 60, "架", _account("max_planes_killed")),
    _challenge("battle_spots", 12, "艘", _account("max_ships_spotted"), "equal"),
    _challenge("battle_xp", 6_600, "点", _account("max_xp")),
    _challenge("battle_damage", 400_000, "点", _account("max_damage_dealt")),
    _challenge("battle_scouting", 400_000, "点", _account("max_damage_scouting")),
    _challenge("ship_damage_114514", 114_514, "点", _ship_displayed_average(114_514), "equal"),
    _challenge("ship_battles", 10_000, "场", _ship("battles")),
    _challenge("draws", 10, "场", _account("draws")),
    _challenge("ramming", 200, "艘", _account("ramming", "frags")),
    _challenge("bb_scouting", 200_000, "点", _ship("max_damage_scouting", kind="Battleship")),
    _challenge("high_tier_low_damage", 4_000, "点", _ship("avg_damage", tier=8, battles=5, minimum=True), "at_most"),
    _challenge("main_battery_accuracy", 60, "%", _ship("main_battery_hit_rate", tier=6, battles=5, shots=100)),
    _challenge("destroyer_torpedo_accuracy", 15, "%",
               _ship("torpedoes_hit_rate", tier=6, battles=5, kind="Destroyer", shots=50)),
    _challenge("carrier_sub_share", 60, "%", _fleet_share("type", lambda kind: kind in ("AirCarrier", "Submarine"))),
    _challenge("low_tier_share", 30, "%", _fleet_share("tier", lambda tier: tier <= 6)),
    _challenge("avg_planes_killed", 7, "架", _per_battle("planes_killed")),
    _challenge("black_friday", 15, "艘", _edition("black_friday")),
    _challenge("azur_lane", 10, "艘", _edition("azur_lane")),
    _challenge("solo_warrior", 10, "次", _award("SOLO_WARRIOR")),
    _challenge("tiger_59_trio", 59, "场", _requires(_combine(_TIGER_TRIO, lambda values: sum(values) / 3),
               *(_at_least(ship, 1) for ship in _TIGER_TRIO))),
    _challenge("devastating_strike", 500, "次", _award("INSTANT_KILL")),
    _challenge("historical_detonation", 50, "次", _award("DETONATED", observed_only=True)),
    _challenge("submarine_free", 0, "场", _submarine_battles, "equal"),
    _challenge("low_account_damage", 30_000, "点", _per_battle("damage_dealt"), "at_most"),
    _challenge("black_storm_damage", 248_000, "点", _named_ship("3445569264", "max_damage_dealt")),
    _challenge("solo_2k_winrate", 55, "%", _requires(lambda evidence: _win_rate(evidence, solo=True),
                                                   _at_least(_solo_battles, 2_000))),
    _challenge("ship_avg_200k", 200_000, "点", _ship("avg_damage", battles=30)),
    _challenge("roussillon_78", 78, "场", _named_ship("3551475536")),
    _challenge("solo_2k", 2_000, "场", _solo_battles),
    _challenge("richthofen_12000", 12_000, "场", _named_ship("4179605296")),
    _challenge("credit_ship_trio", 300, "场",
               _requires(_combine((_AZUMA, _YOSHINO, _KEARSARGE), sum),
                         _at_least(_combine((_AZUMA, _YOSHINO), max), 1), _at_least(_KEARSARGE, 1))),
    _challenge("arson_combo", 100, "次", _requires(_ARSONIST, _at_least(_WITHERER, 100))),
    _challenge("numbers_top10", 5, "艘", _numbers_top10),
    _challenge("dreadnought_1000", 1_000, "次", _award("DREADNOUGHT")),
    _challenge("low_tier_avg_100k", 100_000, "点", _ship("avg_damage", max_tier=5, battles=10)),
    _challenge("yodo_100", 100, "场", _named_ship("4074682064")),
    _challenge("destroyer_elite", 35, "%", _requires(_fleet_share("type", lambda kind: kind == "Destroyer"),
                                                  _at_least(_account("battles"), 8_000), _at_least(_win_rate, 70))),
    _challenge("die_hard_50", 50, "次", _award("HEADBUTT")),
    _challenge("sea_star_500", 500, "次", _award("PVE_HON_WIN_ALL_DONE", observed_only=True)),
    _challenge("alabama_vl", 1, "场", _named_ship("3741267952")),
    _challenge("christopher_colombo", 1, "场", _ship_advantage(_named_ship("4253956080"), _named_ship("4179572464"))),
    _challenge("high_tier_submarines_all", 10, "场", _roster("submarine", every=True)),
    _challenge("essex_scouting", 200_000, "点",
               _requires(_named_ship(_ESSEX, "avg_scouting"),
                         _at_least(_named_ship(_ESSEX), 40),
                         _at_least(_ship_solo_share(_ESSEX), 50),
                         (_named_ship(_ESSEX, "avg_damage"), 120_000, "at_most"))),
    _challenge("incomparable_69", 180_000, "点",
               _requires(_named_ship(_INCOMPARABLE, "max_damage_dealt"), _at_least(_named_ship(_INCOMPARABLE), 69))),
    _challenge("german_bb_tech", 500, "场",
               _requires(_combine((_named_ship(_KURFURST), _named_ship(_PREUSSEN)), max), _GERMAN_BB_2000)),
    _challenge("german_bb_mecklenburg", 500, "场", _requires(_named_ship(_MECKLENBURG), _GERMAN_BB_2000)),
    *(_challenge(f"premium_{target}", target, "艘", _roster("premium_special")) for target in (1, 10)),
    *(_challenge(f"premium_{target}_pct", target, "%", _roster("premium_special", percent=True))
      for target in (10, 25, 50, 75, 90)),
    _challenge("premium_all", 100, "%", _roster("premium_special", percent=True)),
    *(_challenge(f"steel_{target}", target, "艘", _roster("steel")) for target in (1, 5, 10, 15, 20)),
    _challenge("hildebrand_38", 38, "场", _named_ship("3550394160")),
    _challenge("account_age", 10, "年", _account_age),
    _challenge("thunderer_kurfurst", 325, "场",
               _requires(_named_ship(_KURFURST), _at_least(_named_ship("3760142288"), 325))),
    _challenge("unsinkable_10", 10, "次", _award("UNSINKABLE")),
    _challenge("flesh_wound_200", 200, "次", _award("RETRIBUTION")),
)

def _fleet_records(fleet, account, encyclopedia, local_catalog):
    complete = fleet.get("status") in ("ok", "no_battles")
    current = current_ship_catalog()
    rows = {}
    for row in fleet.get("ships") or ():
        if not isinstance(row, dict) or not isinstance(row.get("statistics"), dict):
            complete = False
            continue
        ship_id = str(row.get("ship_id"))
        stats = row["statistics"]
        battles = stats.get("battles")
        if (len(ship_id) > 20 or not ship_id.isascii() or not ship_id.isdecimal() or int(ship_id) <= 0
                or type(battles) is not int or battles < 0 or ship_id in rows):
            complete = False
            continue
        official = encyclopedia.get(ship_id) or {}
        local = local_catalog.get(ship_id) or current.get(ship_id) or {}
        tier = official.get("tier") if type(official.get("tier")) is int else local.get("tier")
        nation = official.get("nation") or local.get("nation")
        edition = ship_edition(ship_id)
        rows[ship_id] = (stats, {
            "id": ship_id,
            "name": ship_display_name(ship_id, official, local),
            "tier": tier if type(tier) is int and 1 <= tier <= 11 else None,
            "type": official.get("type") or local.get("species"),
            "nation": nation.casefold() if isinstance(nation, str) and nation else None,
            "edition": edition,
            "edition_known": ship_id in current or ship_id in local_catalog or edition is not None,
        })
    active = [row for row in rows.values() if row[0]["battles"] > 0]
    return active, complete and sum(stats["battles"] for stats, _ in active) == _value(account, "battles")


def evaluate_challenges(account, fleet, achievements=None, *, encyclopedia=None, local_catalog=None,
                        solo=None, solo_fleet=None, created_at=None, as_of=None, region=None, numbers_rankings=None):
    """Return rules and series totals from the same set of played ships."""
    active, fleet_complete = _fleet_records(fleet, account, encyclopedia or {}, local_catalog or {})
    evidence = _Evidence(account, active, fleet_complete, normalize_battle_achievements(achievements),
                         solo, solo_fleet, created_at, as_of, region, numbers_rankings)
    report = []
    for challenge in CHALLENGES:
        value, complete, *details = challenge.measure(evidence)
        earned = value is not None and _COMPARISONS[challenge.comparison](value, challenge.target)
        status = "earned" if earned else "pending" if complete else "unknown"
        lower_bound = value is not None and not complete and challenge.comparison == "at_least"
        show_value = value if complete or earned or lower_bound else None
        progress = (min(1.0, value / challenge.target)
                    if value is not None and challenge.comparison == "at_least" else None)
        report.append({"id": challenge.id, "name": challenge.name, "requirement": challenge.requirement,
                       "description": challenge.description, "group": challenge.group, "status": status,
                       "rarity": challenge.rarity,
                       "value": show_value, "target": challenge.target, "unit": challenge.unit,
                       "progress": progress, "is_lower_bound": lower_bound, "complete": complete,
                       **(details[0] if details else {})})
    collections = [dict(_series_record(active, fleet_complete, edition), name=name)
                   for edition, name in (("black_friday", "黑五战舰"), ("azur_lane", "碧蓝航线联动舰"))]
    return report, collections


def get_completed_challenges(gallery):
    """Keep the completed challenge tuples used by the F1 scoring integration."""
    return [(row["name"], row["requirement"], row["description"]) for row in gallery if row["status"] == "earned"]
