"""Version-matched ship characteristics for evidence-backed tactical reviews."""
from functools import lru_cache
import hashlib
import json
import math
import re

from .ordinary_replay import CURRENT, OLD, _root
from .replay_loadout import LOADOUT_SCHEMA_VERSION


SHIP_TRAIT_KIND = "replay_ship_traits"
PROFILE_HASHES = {
    OLD: "97ec2b7ec3e1cd2debbcf8d942bbb9132e008ab34bc0177f6a1f957c962f0644",
    CURRENT: "6b75891658533a17816b42dd597f1c4d5d752eeb2c82ded22340cd10a80205b5",
}
BUILD_PROFILE_HASHES = {
    OLD: "db601268c0ec8645f5e8a405327dbbd131248c8478c30bc48de14bace666c173",
    CURRENT: "b62f5f84f6a35ed678072360760f5f1c6c8d03a02fb1de896a5b5195bd140b37",
}
CLASSES = {"Cruiser": "巡洋舰", "Destroyer": "驱逐舰", "Battleship": "战列舰",
           "AirCarrier": "航空母舰", "Submarine": "潜艇"}
ABILITIES = {
    "speedBoosters": ("引擎增压", "SPEED_BOOSTER"),
    "artilleryBoosters": ("主炮装填助推器", "MB_BOOSTER"),
    "torpedoBoosters": ("鱼雷装填助推器", "TA_RELOADER"),
    "auxTorpBooster": ("辅助鱼雷武器助推器", "AUX_TORP_BOOSTER"),
    "smokeGenerator": ("发烟器", "SMOKE_GENERATOR"),
    "sonar": ("水底搜索", "SONAR"),
    "rls": ("监视雷达", "RLS"),
    "regenCrew": ("维修小组", "REGEN_CREW"),
    "fighter": ("战斗机", "FIGHTER"),
    "scout": ("侦察机", "SCOUT"),
    "submarineLocator": ("潜艇监视设备", "SUBMARINE_LOCATOR"),
    "airDefenseDisp": ("防御型对空火力", "AIR_DEFENSE_DISP"),
}
DISPLAY_FIELDS = {
    "caliber": ("主炮口径", "毫米", "GameParams.ShipUpgradeInfo.artillery.barrelDiameter"),
    "reload": ("主炮基础装填", "秒", "GameParams.ShipUpgradeInfo.artillery.shotDelay"),
    "speed": ("舰体基础极速", "节", "GameParams.ShipUpgradeInfo.hull.maxSpeed"),
    "rudder": ("舰体基础转舵", "秒", "GameParams.ShipUpgradeInfo.hull.rudderTime"),
}
BUILD_METRICS = {
    "reload": ("主炮装填", "秒", ("GMShotDelay",)),
    "range": ("主炮射程", "公里", ("GMMaxDist",)),
    "speed": ("舰体极速", "节", ("speedCoef",)),
    "rudder": ("转舵时间", "秒", ("SGRudderTime",)),
    "detect": ("水面被探测距离", "公里", ("visibilityDistCoeff",)),
    "air_detect": ("空中被探测距离", "公里", ("visibilityDistCoeff", "visibilityFactorByPlane")),
    "max_hp": ("舰体耐久", "HP", ("healthHullCoeff",)),
}
EXTRA_MODIFIERS = {
    "GMRotationSpeed": "主炮旋转速度", "GMIdealRadius": "主炮散布半径",
    "burnTime": "受火灾持续时间", "floodTime": "受进水持续时间",
    "burnProb": "受起火概率系数", "floodProb": "鱼雷命中防雷带时的进水概率系数",
    "vulnerabilityBurn": "所受火灾伤害", "vulnerabilityFlood": "所受进水伤害",
    "burnChanceGMGSMultiplier": "舰炮起火概率系数",
    "crashCrewWorkTimeCoeff": "损管作用时间", "regenCrewWorkTimeCoeff": "维修小组作用时间",
    "sonarWorkTimeCoeff": "水听作用时间", "rlsWorkTimeCoeff": "雷达作用时间",
    "scoutWorkTimeCoeff": "侦察机作用时间", "speedBoostersWorkTimeCoeff": "引擎增压作用时间",
    "smokeGeneratorWorkTimeCoeff": "发烟时间", "smokeGeneratorLifeTime": "烟团持续时间",
    "GTShotDelay": "鱼雷装填时间", "torpedoRangeCoefficient": "鱼雷射程",
    "torpedoSpeedMultiplier": "鱼雷航速", "torpedoDamageCoeff": "鱼雷伤害系数",
    "engineForwardUpTime": "前进至全功率时间", "engineBackwardUpTime": "倒车至全功率时间",
    "ConsumablesWorkTime": "消耗品作用时间系数", "ConsumableReloadTime": "消耗品冷却系数",
    "allConsumableReloadTime": "消耗品冷却系数", "healthRegenPercent": "维修回血系数",
}


@lru_cache(maxsize=2)
def _build_profiles(version):
    if version not in BUILD_PROFILE_HASHES:
        return {}
    try:
        raw = (_root() / f"ship_builds-{version}.json").read_bytes()
    except OSError:
        return {}
    if hashlib.sha256(raw).hexdigest() != BUILD_PROFILE_HASHES[version]:
        return {}
    document = json.loads(raw)
    return document if document["version"] == version else {}


def ship_display_name(ship_id, version, catalog=None):
    """Use the exact client's localized hull name and avoid falling back to an ID."""
    profiles = _build_profiles(version).get("ships", {})
    exact = profiles.get(str(ship_id), {}).get("display_name")
    source = catalog or {}
    display = source.get("ships", source).get(str(ship_id), {}).get("display_name")
    name = exact or display
    if isinstance(name, str) and name.strip() and re.search(r"[^\W\d_]", name):
        return name.strip()
    return "未知舰船"


def recorded_build(report, entity_id, ship_id, species=None):
    """Calculate build stats only where a verified baseline and exact fitted modules are available."""
    version = str(report.get("replay_version") or "").replace(",", ".")
    species = _profiles(version).get(str(ship_id), {}).get("species") or species
    source = report.get("ship_loadouts") or {}
    if source.get("version") != version or source.get("schema_version") != LOADOUT_SCHEMA_VERSION:
        return None
    loadout = (source.get("ships") or {}).get(str(entity_id))
    if not loadout or loadout.get("ship_id") != ship_id:
        return None
    document = _build_profiles(version)
    ship = document.get("ships", {}).get(str(ship_id), {})
    catalogue = document.get("upgrades", {})
    if not ship or not catalogue:
        return None
    units = ship["units"]
    selected = {}
    for unit_id in loadout["unit_ids"]:
        module = units.get(str(unit_id))
        if module is not None:
            group = module["group"]
            selected[group] = module if group not in selected else {}
    base = {key: value for key, value in selected.get("_Hull", {}).items() if key in BUILD_METRICS}
    artillery = selected.get("_Artillery")
    if artillery is not None:
        for field in ("range", "reload"):
            base.pop(field, None)
            if field in artillery:
                base[field] = artillery[field]
    if "range" in base and (factor := selected.get("_Suo", {}).get("range_factor")) is not None:
        base["range"] *= factor
    else:
        base.pop("range", None)

    ids = loadout["upgrade_ids"]
    equipped = [catalogue[str(key)] for key in ids if str(key) in catalogue]
    if len({u["slot"] for u in equipped}) != len(equipped):
        return None
    modifiers = {}
    for upgrade in equipped:
        for key, raw in upgrade["modifiers"].items():
            if isinstance(raw, dict):
                raw = raw.get(species)
            if type(raw) in (int, float) and 0 < raw <= 10:
                modifiers[key] = modifiers.get(key, 1) * raw
    scope = "实装模块与已知配件"
    metrics = []
    for key, (label, unit, fields) in BUILD_METRICS.items():
        factor = math.prod(modifiers.get(field, 1) for field in fields)
        basis = base.get(key)
        if basis is None and factor == 1:
            continue
        metrics.append({"label": label, "base_value": round(basis, 3) if basis is not None else None,
                        "value": round(basis * factor, 3) if basis is not None else None,
                        "unit": unit, "factor": round(factor, 6), "scope": scope})
    effects = [{"label": label, "factor": round(modifiers[key], 6), "scope": "实装配件系数"}
               for key, label in EXTRA_MODIFIERS.items() if key in modifiers and modifiers[key] != 1]
    return {"equipment": [u["display_name"] or f'第 {u["slot"] + 1} 槽已记录配件' for u in equipped],
            "missing_fields": len(ids) - len(equipped), "support": metrics + effects}


def equipped_traits(build):
    """Give the model both numeric values and combat-readable labels."""
    return "、".join(
        f'{row["label"]} {row["value"]:g} {row["unit"]}' if row.get("value") is not None
        else f'{row["label"]} ×{row["factor"]:g}'
        for row in build["support"])


@lru_cache(maxsize=2)
def _profiles(version):
    expected = PROFILE_HASHES.get(version)
    if not expected:
        return {}
    try:
        raw = (_root() / f"ship_traits-{version}.json").read_bytes()
    except OSError:
        return {}
    if hashlib.sha256(raw).hexdigest() != expected:
        return {}
    parsed = json.loads(raw)
    return parsed["ships"] if parsed.get("version") == version else {}


def reviewed_ship_traits(report, participant):
    """Return same-build traits and separately tracked, actually used abilities."""
    version = str(report.get("replay_version") or "").replace(",", ".")
    ship_id = participant.get("ship_id")
    entity_id = participant.get("entity_id")
    profile = _profiles(version).get(str(ship_id))
    if not profile:
        return None
    native_name = participant["ship"]
    ship_class = CLASSES.get(profile.get("species"), "")
    tier = profile.get("tier")
    identity = " ".join(str(part) for part in (native_name, f"{tier} 级" if tier else "", ship_class) if part)
    measured = []
    support = []
    source_fields = []
    for key, (label, unit, field) in DISPLAY_FIELDS.items():
        if (value := profile.get(key)) is None:
            continue
        measured.append(f"{label} {value:g} {unit}")
        support.append({"label": label, "value": value, "unit": unit, "scope": "基础参数"})
        source_fields.append(field)
    if ammo := profile.get("ammo"):
        types = " / ".join("SAP" if shell == "CS" else shell for shell in ammo)
        measured.append(f"主炮弹种 {types}")
        support.append({"label": "主炮弹种", "value": types, "scope": "基础参数"})
        source_fields.append("GameParams.ShipUpgradeInfo.artillery.ammoList")
    observed = {row.get("symbolic_name") for row in (report.get("combat") or {}).get("consumables", ())
                if type(entity_id) is int and row.get("entity_id") == entity_id}
    choices = []
    used = []
    for slot in profile.get("abilities", []):
        names = []
        for kind, duration in slot:
            if kind not in ABILITIES:
                continue
            label, symbolic = ABILITIES[kind]
            names.append(label)
            if symbolic in observed:
                used.append(label)
            entry = {"label": label, "scope": "同槽可选" if len(slot) > 1 else "舰型配置",
                     "observed": symbolic in observed}
            if duration is not None:
                entry.update(duration_sec=duration, unit="秒")
            support.append(entry)
        if names:
            if len(names) < len(slot):
                names.append("其他配置")
            choices.append("或".join(names))
    text = f"{identity}。同版本舰型资料"
    if measured:
        text += "记录了" + "、".join(measured)
    text += "。"
    if choices:
        source_fields.append("GameParams.ShipAbilities")
        text += "配置项包括" + "、".join(choices) + "。同槽选项需结合本局实际部署。"
    if used:
        source_fields.append("combat.consumables")
        text += "本局观测到启用" + "、".join(dict.fromkeys(used)) + "。"
    build = recorded_build(report, entity_id, ship_id, profile.get("species"))
    if build is not None:
        source_fields += ["Vehicle.shipConfig", "GameParams.Modernization.modifiers"]
        support.extend(build["support"])
        if build["equipment"]:
            text += "REP 实装配件 " + "、".join(build["equipment"]) + "。"
        if equipped := equipped_traits(build):
            text += "实装模块和已知配件修正 " + equipped + "。"
        if build["missing_fields"]:
            text += f"有 {build['missing_fields']} 个已记录配件未能匹配修正。"
    else:
        text += "此舰本局实际装配未获核验。"
    return text, {"entity_id": entity_id, "ship_id": ship_id, "ship_name": native_name,
                  "species": profile.get("species"), "nation": profile.get("nation"), "tier": tier,
                  "version": version, "source_fields": [*source_fields, "GameParams.typeinfo", "GameParams.level"],
                  "support": support,
                  **({"equipment": build["equipment"], "missing_fields": build["missing_fields"]} if build is not None else {})}
