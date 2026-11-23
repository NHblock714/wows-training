"""An account-free projection of one pinned public career snapshot for AI."""
from collections import Counter
from datetime import date
from functools import lru_cache
import math

from .career_rating import metrics, project_ships, ratio, summarize_ships
from .career_service import _identity
from .public_stats import _positive_id
from .single_battle_metrics import _sample as _regional_random_sample


LEGACY_CAREER_PROFILE_CONTRACT = "career_profile_v1"
CAREER_PROFILE_CONTRACT = "career_profile_v2"
CAREER_PROFILE_CONTRACTS = (LEGACY_CAREER_PROFILE_CONTRACT, CAREER_PROFILE_CONTRACT)
PROFILE_QUESTION = "根据这份公开战绩快照，简要评价选手的用船偏好、可观察的战斗表现和参战积累。"
PROFILE_KINDS = {
    "career_snapshot_scope": ("P01", "资料范围"),
    "career_random_summary": ("P02", "随机战生涯"),
    "career_mode_activity": ("P03", "各模式参战"),
    "career_ship_classes": ("P04", "舰种偏好"),
    "career_ship_tiers": ("P05", "等级分布"),
    "career_favorite_ships": ("P06", "常用舰船"),
    "career_pr_coverage": ("P07", "公开 PR 覆盖"),
    "career_mode_summaries": ("P08", "各模式表现"),
    "career_mode_fleets": ("P09", "各模式船仓"),
    "career_server_comparison": ("P10", "同服同舰基准"),
}
REGION_NAMES = {"asia": "亚服", "eu": "欧服", "na": "北美服"}
CLASS_NAMES = {"AUX": "辅助舰", "BB": "战列舰", "CA": "巡洋舰", "CV": "航空母舰", "DD": "驱逐舰",
               "SS": "潜艇", "Auxiliary": "辅助舰", "Battleship": "战列舰", "Cruiser": "巡洋舰",
               "AirCarrier": "航空母舰", "AircraftCarrier": "航空母舰", "Destroyer": "驱逐舰", "Submarine": "潜艇"}
MODE_NAMES = {"pvp": "随机战", "pvp_solo": "随机战单排", "pvp_div2": "随机战双排", "pvp_div3": "随机战三排",
              "rank_solo": "排位战", "pve": "联合作战", "club": "公会战",
              "oper_solo": "行动单排", "oper_div": "行动组队"}
METRIC_LABELS = {"battles": "场次", "win_rate": "胜率", "avg_damage": "场均伤害",
                 "avg_frags": "场均击沉", "survival_rate": "存活率",
                 "avg_scouting_damage": "场均侦察伤害", "main_battery_hit_rate": "主炮命中率",
                 "avg_potential_damage": "场均潜在承伤", "avg_capture_points": "场均占领分",
                 "avg_dropped_capture_points": "场均防御占领分", "torpedo_hit_rate": "鱼雷命中率",
                 "avg_xp": "场均经验", "kd_ratio": "击沉／被击沉比", "avg_ships_spotted": "场均发现敌舰",
                 "avg_planes_killed": "场均击落飞机", "wins": "胜场", "draws": "平场", "losses": "负场",
                 "damage_dealt": "累计伤害", "frags": "累计击沉", "survived_battles": "存活场次",
                 "xp": "累计经验", "damage_scouting": "累计侦察伤害", "ships_spotted": "累计发现敌舰",
                 "planes_killed": "累计击落飞机", "capture_points": "累计占领分",
                 "dropped_capture_points": "累计防御占领分", "art_agro": "累计炮弹潜在伤害",
                 "torpedo_agro": "累计鱼雷潜在伤害", "max_damage_dealt": "单场最高伤害",
                 "max_frags_battle": "单场最多击沉", "max_xp": "单场最高经验",
                 "max_planes_killed": "单场最多击落飞机", "max_damage_scouting": "单场最高侦察伤害",
                 "survived_wins": "存活胜场", "battles_since_510": "0.5.10 以来场次", "battles_since_512": "0.5.12 以来场次",
                 "control_captured_points": "占领控制点分", "control_dropped_points": "防守控制点分",
                 "team_capture_points": "队伍占领分", "team_dropped_capture_points": "队伍防守分",
                 "damage_to_buildings": "累计对建筑伤害", "max_damage_dealt_to_buildings": "单场最高对建筑伤害",
                 "suppressions_count": "累计压制", "max_suppressions_count": "单场最多压制",
                 "max_ships_spotted": "单场最多发现敌舰", "max_total_agro": "单场最高潜在伤害"}
RATIO_FIELDS = {"win_rate", "survival_rate", "main_battery_hit_rate", "torpedo_hit_rate"}
BATTLE_COUNTS = {"battles", "wins", "draws", "losses", "survived_battles", "survived_wins", "battles_since_510", "battles_since_512"}
_RANDOM_SUMMARY_FIELDS = tuple(METRIC_LABELS)[:10]
_RAW_MODE_FIELDS = METRIC_LABELS.keys() - metrics(None).keys()
_WEAPONS = {"main_battery": "主炮", "second_battery": "副炮", "torpedoes": "鱼雷", "aircraft": "舰载机", "ramming": "撞击"}
_WEAPON_METRICS = {"frags": "击沉", "max_frags_battle": "单场最多击沉", "hits": "命中", "shots": "发射"}
for weapon, weapon_label in _WEAPONS.items():
    METRIC_LABELS.update({f"{weapon}_{key}": weapon_label + label for key, label in _WEAPON_METRICS.items()})
METRIC_LABELS.update({f"wins_by_tasks_{n}": f"达成 {n} 项任务的行动胜场" for n in range(6)})
BATTLE_COUNTS.update(f"wins_by_tasks_{n}" for n in range(6))
_RANDOM_DIVISION_MODES = frozenset({"pvp_solo", "pvp_div2", "pvp_div3"})


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _date_label(value):
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _finite(value, upper=10**12):
    return type(value) in (int, float) and 0 <= value <= upper and math.isfinite(value)


def _battles(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _metrics_rows(values, fields):
    result = []
    for field in fields:
        value = values.get(field)
        ratio = field in RATIO_FIELDS
        if _finite(value, 1 if ratio else 10**12):
            result.append({"metric": field, "label": METRIC_LABELS[field],
                           "value": round(value, 4 if ratio else 2),
                           "unit": "比例" if ratio else "场" if field in BATTLE_COUNTS else
                           "场均" if field.startswith("avg_") else "比值" if field == "kd_ratio" else "数值"})
    return result


def _metrics_text(rows):
    return "、".join(f"{row['label']}{row['value']:.1%}" if row["unit"] == "比例"
                    else f"{row['label']}{row['value']:,.2f}".rstrip("0").rstrip(".") for row in rows)


@lru_cache(maxsize=1)
def _verified_ship_catalog():
    from .runtime_bundle import load_bundle
    return load_bundle(verify=False).ship_catalog()


def _scope(region, raw):
    source = raw.get("source_type")
    time_type = raw.get("time_kind")
    day = _date_label(raw.get("timestamp"))
    alias = "官方公开查询" if source == "official_query" else "本地历史快照"
    if source == "official_query" and time_type == "fetched_at_utc" and day:
        timing = f"采集日（UTC）{day}"
    elif source != "official_query" and day:
        timing = f"历史资料参考日 {day}（文件时间，采集日未知）"
    else:
        timing = "采集日未知"
    return f"{REGION_NAMES[region]}，{alias}，{timing}。累计公开战绩反映账号参战记录。", {
        "region": region, "scope": "public_career_snapshot",
        "source": alias, "sampled_at": day, "time_basis": timing,
        "observed": "公开累计战绩", "unknown": "入坑年份、个人动机、单局打法"}


def _snapshot(profile, target, record):
    if not isinstance(target, dict) or target.get("kind") != "player":
        raise ValueError("请先选择一个公开玩家档案。")
    if not isinstance(profile, dict) or not profile.get("snapshot_id"):
        raise ValueError("当前档案暂无可分析的公开快照。")
    if not isinstance(record, dict) or record.get("kind") != "career_snapshot" or record.get("id") != profile["snapshot_id"]:
        raise ValueError("当前档案与公开快照不一致，请重新打开账号。")
    raw = _mapping(record.get("payload"))
    identities = (_identity(target.get("region"), target.get("id")),
                  _identity(profile.get("region"), profile.get("account_id")),
                  _identity(raw.get("region"), raw.get("account_id")))
    if (len(set(identities)) != 1 or raw.get("schema_version") != 1
            or profile.get("source_type") != raw.get("source_type") or profile.get("timestamp") != raw.get("timestamp")):
        raise ValueError("当前档案与公开快照不一致，请重新打开账号。")
    if raw.get("source_type") not in ("official_query", "legacy_cache") or raw.get("account_status") != "ok":
        raise ValueError("账号公开资料隐藏或缺失，暂时无法分析。")
    return raw, identities[0][0]


def _fleet(entry, mode, references):
    fleet = _mapping(entry.get("ships"))
    entries = fleet.get("ships")
    if fleet.get("status") not in ("ok", "no_battles") or not isinstance(entries, list):
        return "unknown", []
    seen, result = set(), []
    catalog = _verified_ship_catalog()
    encyclopedia = _mapping(references.get("encyclopedia"))
    local = _mapping(references.get("local_catalog"))
    for entry in entries:
        item = _mapping(entry)
        sid = _positive_id(item.get("ship_id"))
        stats = _mapping(item.get("statistics"))
        count = _battles(stats.get("battles"))
        if sid is None or sid in seen or count is None or item.get("mode", mode) != mode:
            return "unknown", []
        seen.add(sid)
        if not count:
            continue
        native = _mapping(catalog.get(sid))
        pinned = _mapping(local.get(sid))
        official = _mapping(encyclopedia.get(sid))
        result.append({**_scoped_metrics(stats), "ship_id": sid, "statistics": stats,
                       "ship_name": native.get("display_name") or "未识别名称的舰船",
                       "species": official.get("type") or pinned.get("species") or native.get("species"),
                       "tier": official.get("tier") or pinned.get("tier") or native.get("tier")})
    if not result:
        return ("no_battles" if fleet.get("status") == "no_battles" else "unknown"), []
    if fleet.get("status") == "no_battles":
        return "unknown", []
    return "available", sorted(result, key=lambda row: (-row["battles"], row["ship_id"]))


def _distribution(fleet, key, labeler):
    counts = Counter()
    total = sum(row["battles"] for row in fleet)
    for row in fleet:
        counts[labeler(row.get(key))] += row["battles"]
    return [{"label": label, "count": count, "denominator": total, "share": round(count/total, 4), "unit": "场"}
            for label, count in counts.most_common()]


def _tier_label(value):
    text = str(value)
    return f"{text} 级" if text in tuple(map(str, range(1, 12))) else "等级未知"


def _class_label(value):
    return CLASS_NAMES.get(str(value), "舰种未知")


def _mode_stats(entry):
    account = _mapping(entry.get("account"))
    stats = _mapping(account.get("statistics"))
    if account.get("status") not in ("ok", "no_battles") or _battles(stats.get("battles")) is None:
        return {}
    return stats


def _tracked_battles(stats, field):
    tracked, all_battles = _battles(stats.get(field)), _battles(stats.get("battles"))
    return tracked if tracked is not None and all_battles is not None and tracked <= all_battles else None


def _scoped_metrics(stats):
    values = metrics(stats)
    if "battles_since_510" in stats:
        values["avg_scouting_damage"] = ratio(stats.get("damage_scouting"), _tracked_battles(stats, "battles_since_510"))
    return values


def _account_metrics(stats):
    values = _scoped_metrics(stats)
    values.update({key: stats.get(key) for key in _RAW_MODE_FIELDS})
    for key in _WEAPONS:
        weapon = _mapping(stats.get(key))
        values.update({f"{key}_{metric}": weapon.get(metric) for metric in _WEAPON_METRICS})
    tasks = _mapping(stats.get("wins_by_tasks"))
    values.update({f"wins_by_tasks_{n}": tasks.get(str(n)) for n in range(6)})
    return values


def _favorites(fleet, total, *, flat=False):
    result = []
    for row in fleet[:5]:
        battles = row["battles"]
        item = {"ship_name": row["ship_name"], "species": _class_label(row.get("species")),
                "label": _tier_label(row.get("tier")), "count": battles, "share": round(battles / total, 4)}
        recorded = _metrics_rows(row, ("win_rate", "avg_damage", "avg_frags"))
        if flat:
            item.update({metric["metric"]: metric["value"] for metric in recorded})
        else:
            item["support"] = recorded[:2]
        result.append(item)
    return result


def _pr_rating(profile, fleet, references, entry):
    if not fleet:
        return {}
    if _mapping(references.get("expected")):
        catalog = _mapping(references.get("local_catalog")) or _verified_ship_catalog()
        rows = project_ships(entry["ships"]["ships"], "pvp", references, catalog)
        return _mapping(summarize_ships(rows, "pvp", references).get("rating"))
    projected = profile.get("ships")
    if profile.get("mode") == "pvp" and isinstance(projected, list):
        counts = {str(row.get("ship_id")): row.get("battles") for row in projected
                  if isinstance(row, dict) and _battles(row.get("battles"))}
        if counts == {row["ship_id"]: row["battles"] for row in fleet}:
            return _mapping(profile.get("rating"))
    return {}


def _server_comparison(region, fleet, account_stats):
    data = _regional_random_sample()
    server = _mapping(_mapping(data.get("regions")).get(region))
    fields = data.get("fields", [])
    records = _mapping(server.get("ships"))
    support = []
    comparisons = (("avg_damage", "damage_dealt", "damage", "battles", "games"),
                   ("avg_frags", "frags", "frags", "battles", "games"),
                   ("avg_scouting_damage", "damage_scouting", "spot", "battles_since_510", "games_510"))
    if data.get("mode") == "pvp" and records:
        for metric, personal_key, baseline_key, player_games, server_games in comparisons:
            if baseline_key not in fields or server_games not in fields:
                continue
            cohort = [(row, _tracked_battles(row["statistics"], player_games)) for row in fleet]
            total = sum(count for _, count in cohort if count is not None)
            account_count = _tracked_battles(account_stats, player_games)
            denominator = account_count if account_count is not None and account_count >= total else total
            actual, expected, covered, reference_count = 0, 0.0, 0, 0
            for row, battles in cohort:
                values = records.get(row["ship_id"])
                if not battles or not isinstance(values, list) or len(values) != len(fields):
                    continue
                baseline = dict(zip(fields, values))
                games, ref = _battles(baseline.get(server_games)), baseline.get(baseline_key)
                all_games = _battles(baseline.get("games"))
                own = row["statistics"].get(personal_key)
                if not games or not all_games or games > all_games or not _finite(ref) or not _finite(own):
                    continue
                covered += battles
                reference_count += games
                actual += own
                expected += ref / games * battles
            if covered:
                value, reference = actual / covered, expected / covered
                scope = "随机战 0.5.10 以来" if player_games == "battles_since_510" else "随机战"
                scope += "账号场次" if denominator == account_count else "已知逐舰场次"
                support.append({"metric": metric, "label": METRIC_LABELS[metric], "value": round(value, 2),
                                "reference_value": round(reference, 2), "difference": round(value - reference, 2),
                                "count": covered, "denominator": denominator, "battle_count": reference_count,
                                "coverage_ratio": round(covered / denominator, 4),
                                "unit": "场均", "scope": scope})
    detail = {"mode": "pvp", "region": region, "scope": "生涯累计与历史版本同服同舰随机战公开样本",
              "status": "available" if support else "unknown", "support": support,
              "unknown": "其他模式、服务器胜率与百分位缺少同口径样本。"}
    if not support:
        return "本快照缺少可与同服历史随机战参照匹配的逐舰场次与指标。", detail
    period = data["version"].removeprefix("0.")
    detail.update(provider=data["provider"], period=period, snapshot_date=data["snapshot_date"],
                  population=data["population"], source_url=server["source_url"],
                  source_fields=["mode_results.pvp.ships", "mode_results.pvp.account.statistics",
                                 "regional_random_15_9.json"])
    comparison = "、".join(f"{r['label']} {r['value']:,.2f} 对 {r['reference_value']:,.2f}"
                          f"（个人覆盖 {r['count']:,}/{r['denominator']:,} 场，参照 {r['battle_count']:,} 次出场）"
                          for r in support)
    text = (f"按个人实际出场配比加权，{REGION_NAMES[region]}同舰随机战{period}版历史参照：{comparison}。"
            f"Ship Tool {data['snapshot_date']} 快照，覆盖至少 200 场随机战的公开账号。个人数据为生涯累计。")
    return text, detail


def build_career_profile_evidence(profile, target, snapshot_record):
    """Build a named-source/ID-free, tier- and mode-aware report input."""
    raw, region = _snapshot(profile, target, snapshot_record)
    modes = _mapping(raw.get("mode_results"))
    random_entry = _mapping(modes.get("pvp"))
    references = _mapping(raw.get("rating_references"))
    random_stats = _mode_stats(random_entry)
    random_status, fleet = _fleet(random_entry, "pvp", references)
    facts = []

    def add(kind, text, details):
        key, title = PROFILE_KINDS[kind]
        facts.append({"id": key, "kind": kind, "label": title, "text": text, "details": details})

    text, context = _scope(region, raw)
    add("career_snapshot_scope", text, context)
    summary = _metrics_rows(_scoped_metrics(random_stats), _RANDOM_SUMMARY_FIELDS)
    if summary:
        add("career_random_summary", "随机战公开累计：" + _metrics_text(summary) + "。",
            {"mode": "pvp", "support": summary, "source_fields": ["mode_results.pvp.account.statistics"]})

    activity, summaries, fleets = [], [], []
    for mode, label in MODE_NAMES.items():
        entry = _mapping(modes.get(mode))
        stats = _mode_stats(entry)
        count = _battles(stats.get("battles"))
        scope = "随机战分项" if mode in _RANDOM_DIVISION_MODES else "独立统计字段"
        account = {"mode": mode, "label": label, "count": count, "scope": scope,
                   "status": "unknown" if count is None else "known"}
        activity.append(account)
        summaries.append({**account, "support": _metrics_rows(_account_metrics(stats), METRIC_LABELS)})
        status, rows = (random_status, fleet) if mode == "pvp" else _fleet(entry, mode, references)
        if status == "no_battles" and count:
            status = "unknown"
        total = sum(row["battles"] for row in rows)
        fleets.append({"mode": mode, "label": label, "scope": scope, "status": status,
                       "count": len(rows) if status != "unknown" else None,
                       "denominator": total if status != "unknown" else None, "account_battles": count,
                       "coverage_ratio": round(total / count, 4) if status != "unknown" and count and total <= count else None,
                       "class_distribution": _distribution(rows, "species", _class_label),
                       "tier_distribution": _distribution(rows, "tier", _tier_label),
                       "favorite_ships": _favorites(rows, total, flat=True)})
    if any(row["status"] == "known" for row in activity):
        add("career_mode_activity", "各战斗模式按公开字段分别计数。单排、双排和三排属于随机战分项。",
            {"support": activity, "source_fields": ["mode_results.*.account.statistics.battles"]})

    if fleet:
        total = sum(row["battles"] for row in fleet)
        for kind, key, labeler in (("career_ship_classes", "species", _class_label), ("career_ship_tiers", "tier", _tier_label)):
            rows = _distribution(fleet, key, labeler)
            add(kind, f"本快照可见随机战逐舰样本共 {total:,} 场，占比以逐舰已记录场次为分母。",
                {"mode": "pvp", "denominator": total, "support": rows, "source_fields": ["mode_results.pvp.ships"]})
        add("career_favorite_ships", "按已记录随机战场次选取最常使用的舰船。场次代表熟悉度线索与使用偏好。",
            {"mode": "pvp", "denominator": total, "support": _favorites(fleet, total), "source_fields": ["mode_results.pvp.ships"]})
    else:
        add("career_ship_classes", "当前公开快照缺少可核对的逐舰场次，舰种、等级和常用舰偏好未知。",
            {"mode": "pvp", "status": "unknown"})

    pr = _pr_rating(profile, fleet, references, random_entry)
    total = sum(row["battles"] for row in fleet)
    covered, value = pr.get("covered_battles"), pr.get("pr")
    if (_finite(value) and _finite(covered, total) and covered > 0
            and pr.get("total_battles") == total):
        ratio = covered/total
        add("career_pr_coverage", f"随机战 PR {value:,.0f}；逐舰样本中可计算 {covered:,.0f}/{total:,.0f} 场（{ratio:.1%}）。",
            {"mode": "pvp", "value": round(value, 2), "count": covered, "denominator": total,
             "coverage_ratio": round(ratio, 4), "scope": "已具备同舰参考值的随机战样本"})
    else:
        add("career_pr_coverage", "当前资料的随机战 PR 覆盖未知。",
            {"mode": "pvp", "status": "unknown"})
    if not any(row["status"] == "known" for row in activity) and not any(row["status"] == "available" for row in fleets):
        raise ValueError("此公开快照没有可核对的参战场次或表现指标。")
    add("career_mode_summaries", "按模式列出快照提供的账号累计表现。随机战单排、双排和三排包含在随机战总场次内。",
        {"support": summaries, "source_fields": ["mode_results.*.account.statistics"]})
    add("career_mode_fleets", "船仓按各模式实际收录的全部等级和出场统计。舰种、等级及常用舰占比以该模式逐舰场次为分母。",
        {"support": fleets, "source_fields": ["mode_results.*.ships"],
         "unknown": "缺少逐舰数据的模式保留未知；舰种或等级未识别的出场仍计入逐舰分母。"})
    text, comparison = _server_comparison(region, fleet, random_stats)
    add("career_server_comparison", text, comparison)
    return {"kind": "career_profile", "report_contract": CAREER_PROFILE_CONTRACT,
            "source_name": "公开账号资料", "evidence": facts}
