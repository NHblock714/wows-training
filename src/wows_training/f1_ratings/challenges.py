"""F1 WOWS challenge catalogue evaluated against public account snapshots."""
from collections.abc import Callable
from dataclasses import dataclass
import math

from ..career_catalog import current_ship_catalog, ship_edition


def _value(record, *keys):
    for key in keys:
        record = record.get(key) if isinstance(record, dict) else None
    return record if type(record) in (int, float) and 0 <= record < 2**64 and math.isfinite(record) else None


def _account(*keys):
    def measure(account, _fleet, _complete, _awards):
        value = _value(account, *keys)
        return value, value is not None
    return measure


def _per_battle(field):
    def measure(account, _fleet, _complete, _awards):
        amount, battles = _value(account, field), _value(account, "battles")
        if amount is None or not battles:
            return None, amount is not None and battles == 0
        return amount / battles, True
    return measure


def _eligible_ship(ship, metric, *, tier, battles, kind, shots):
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
    if kind and metadata.get("type") and metadata["type"] != kind:
        return False, None
    if (tier and metadata.get("tier") is None) or (kind and not metadata.get("type")):
        return True, None
    if metric.endswith("_hit_rate"):
        hits = _value(stats, field, "hits")
        return True, hits / fired * 100 if fired and hits is not None and hits <= fired else None
    if metric == "avg_damage":
        damage = _value(stats, "damage_dealt")
        return True, damage / count if damage is not None else None
    return True, _value(stats, metric)


def _ship(metric, *, tier=0, battles=1, kind=None, shots=0, minimum=False, closest=None):
    def measure(_account, fleet, complete, _awards):
        values = []
        for ship in fleet:
            eligible, value = _eligible_ship(ship, metric, tier=tier, battles=battles, kind=kind, shots=shots)
            if eligible:
                if value is None:
                    complete = False
                else:
                    values.append(value)
        if closest is not None and values:
            return min((round(value) for value in values), key=lambda value: abs(value - closest)), complete
        return (min if minimum else max)(values, default=None), complete
    return measure


def _fleet_share(field, qualifies):
    def measure(account, fleet, complete, _awards):
        total = _value(account, "battles")
        if not total or sum(stats["battles"] for stats, _ in fleet) > total:
            return None, total == 0 and complete
        count = 0
        for stats, metadata in fleet:
            value = metadata.get(field)
            if value is None:
                complete = False
            elif qualifies(value):
                count += stats["battles"]
        return count / total * 100, complete
    return measure


def _edition(edition):
    def measure(_account, fleet, complete, _awards):
        series = _series_record(fleet, complete, edition)
        return series["ships"], series["complete"]
    return measure


def _series_record(fleet, complete, edition):
    matches = [stats for stats, meta in fleet if meta["edition"] == edition]
    complete = complete and all(meta["edition_known"] for _, meta in fleet)
    return {"id": edition, "ships": len(matches) if matches or complete else None,
            "battles": sum(stats["battles"] for stats in matches) if matches or complete else None,
            "complete": complete}


def _solo_warrior(_account, _fleet, _complete, awards):
    if not isinstance(awards, dict):
        return None, False
    value = _value(awards, "SOLO_WARRIOR") if "SOLO_WARRIOR" in awards else 0
    return value, value is not None


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


CHALLENGES = (
    Challenge("battle_frags", "record", "摧枯拉朽", "最高单场击杀战舰 ≥ 8", "优势在我", 8, "艘",
              _account("max_frags_battle")),
    Challenge("battle_agro", "record", "铁壁防线", "最高单场潜在伤害 ≥ 800 万", "顶住前线的火力", 8_000_000, "",
              _account("max_total_agro")),
    Challenge("battle_planes", "record", "亡牌飞行员", "最高单场击毁飞机 ≥ 60", "来自海面的防空警告", 60, "架",
              _account("max_planes_killed")),
    Challenge("battle_spots", "record", "天空之眼", "最高单场侦察舰船 ≥ 18 艘", "从迷雾中找出对手", 18, "艘",
              _account("max_ships_spotted")),
    Challenge("battle_xp", "record", "冒险模式", "最高单场经验 ≥ 6,600", "一场打出超额贡献", 6_600, "",
              _account("max_xp")),
    Challenge("battle_damage", "record", "创造模式", "最高单场伤害 ≥ 40 万", "火力全开", 400_000, "",
              _account("max_damage_dealt")),
    Challenge("battle_scouting", "record", "观察模式", "最高单场侦察伤害 ≥ 40 万", "为队友照亮战场", 400_000, "",
              _account("max_damage_scouting")),
    Challenge("ship_damage_114514", "mastery", "野兽先辈", "任意战舰四舍五入后的场均伤害 = 114,514", "精确到最后一位", 114_514, "",
              _ship("avg_damage", closest=114_514), "equal"),
    Challenge("ship_battles", "mastery", "真爱粉", "任意战舰 ≥ 10,000 场", "一舰万场的航海生涯", 10_000, "场",
              _ship("battles")),
    Challenge("draws", "career", "真正的和平", "平局累计 ≥ 10 场", "海面回归平静", 10, "场",
              _account("draws")),
    Challenge("ramming", "career", "碰碰船", "冲撞击沉累计 ≥ 200 艘", "舰艏也是武器", 200, "艘",
              _account("ramming", "frags")),
    Challenge("bb_scouting", "mastery", "面壁者", "任意战列舰最高单场侦察伤害 ≥ 20 万", "战列舰也有侦察的一面", 200_000, "",
              _ship("max_damage_scouting", kind="Battleship")),
    Challenge("high_tier_low_damage", "mastery", "CN_70", "T8+ 战舰 ≥ 5 场且场均伤害 ≤ 4,000", "一次特别的舰船纪录", 4_000, "",
              _ship("avg_damage", tier=8, battles=5, minimum=True), "at_most"),
    Challenge("main_battery_accuracy", "mastery", "PAC", "T6+ 战舰 ≥ 5 场且主炮发射 ≥ 100 发，命中率 ≥ 60%", "炮术专精", 60, "%",
              _ship("main_battery_hit_rate", tier=6, battles=5, shots=100)),
    Challenge("destroyer_torpedo_accuracy", "mastery", "水雷魂", "T6+ 驱逐舰 ≥ 5 场且施放 ≥ 50 枚鱼雷，命中率 ≥ 15%", "鱼雷专精", 15, "%",
              _ship("torpedoes_hit_rate", tier=6, battles=5, kind="Destroyer", shots=50)),
    Challenge("carrier_sub_share", "fleet", "全家福", "航母与潜艇随机战场次占比 ≥ 60%", "制空与水下舰队", 60, "%",
              _fleet_share("type", lambda kind: kind in ("AirCarrier", "Submarine"))),
    Challenge("low_tier_share", "fleet", "刮宫圣手", "T6 及以下随机战场次占比 ≥ 30%", "中低等级舰队常客", 30, "%",
              _fleet_share("tier", lambda tier: tier <= 6)),
    Challenge("avg_planes_killed", "career", "舰队防空", "场均击落飞机 ≥ 7", "舰队的防空屏障", 7, "架",
              _per_battle("planes_killed")),
    Challenge("black_friday", "collection", "林翩翩", "≥ 15 艘黑五战舰有随机战记录", "黑色涂装舰队", 15, "艘",
              _edition("black_friday")),
    Challenge("azur_lane", "collection", "天青车道", "≥ 10 艘碧蓝航线联动舰有随机战记录", "碧蓝航线联动舰队", 10, "艘",
              _edition("azur_lane")),
    Challenge("solo_warrior", "battle", "吸鼠坝王", "获得以一当十成就 ≥ 10 次", "孤舰带回胜利", 10, "次",
              _solo_warrior),
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
        edition = ship_edition(ship_id)
        rows[ship_id] = (stats, {
            "tier": tier if type(tier) is int and 1 <= tier <= 11 else None,
            "type": official.get("type") or local.get("species"),
            "edition": edition,
            "edition_known": ship_id in current or ship_id in local_catalog or edition is not None,
        })
    active = [row for row in rows.values() if row[0]["battles"] > 0]
    return active, complete and sum(stats["battles"] for stats, _ in active) == _value(account, "battles")


def evaluate_challenges(account, fleet, achievements=None, *, encyclopedia=None, local_catalog=None):
    """Return rules and series totals from the same set of played ships."""
    active, fleet_complete = _fleet_records(fleet, account, encyclopedia or {}, local_catalog or {})
    report = []
    for challenge in CHALLENGES:
        value, complete = challenge.measure(account, active, fleet_complete, achievements)
        earned = value is not None and {
            "at_least": value >= challenge.target,
            "at_most": value <= challenge.target,
            "equal": value == challenge.target,
        }[challenge.comparison]
        status = "earned" if earned else "pending" if complete else "unknown"
        lower_bound = value is not None and not complete and challenge.comparison == "at_least"
        show_value = value if complete or earned or lower_bound else None
        progress = (min(1.0, value / challenge.target)
                    if value is not None and challenge.comparison == "at_least" else None)
        report.append({"id": challenge.id, "name": challenge.name, "requirement": challenge.requirement,
                       "description": challenge.description, "group": challenge.group, "status": status,
                       "value": show_value, "target": challenge.target, "unit": challenge.unit,
                       "progress": progress, "is_lower_bound": lower_bound, "complete": complete})
    collections = [dict(_series_record(active, fleet_complete, edition), name=name)
                   for edition, name in (("black_friday", "黑五战舰"), ("azur_lane", "碧蓝航线联动舰"))]
    return report, collections


def get_completed_challenges(gallery):
    """Keep the completed challenge tuples used by the F1 scoring integration."""
    return [(row["name"], row["requirement"], row["description"]) for row in gallery if row["status"] == "earned"]
