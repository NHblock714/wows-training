"""F1 WOWS offline scoring. Adapted from wows_analyzer/ships.py."""
import numpy as np
import pandas as pd
from .models import PlayerStats
from .personal_rating import compute_ship_battle_power


def _mechanical_accuracy(artillery):
    """从百科火炮参数算出"机械精度"原始值。

        accuracy = (max_range_m / max_dispersion_m) × (bullet_speed / 800)

    - `max_range / max_dispersion`: 射程 / 散布 = "射程下的散布相对值", 越大越准
    - `bullet_speed / 800`: 800 m/s 是 14"-16" 战列主炮初速基准, 弹速越快前导越准

    artillery 是百科的 `default_profile.artillery` 子节点, 含 `max_dispersion`, `distance`
    (km, 注意单位!), `shells.AP/HE/SAP.bullet_speed`。

    CV / Sub / 无主炮的船返回 None。
    """
    if not artillery or not isinstance(artillery, dict):
        return None
    max_disp = artillery.get("max_dispersion")
    distance_km = artillery.get("distance")
    if not max_disp or not distance_km or max_disp <= 0:
        return None
    shells = artillery.get("shells") or {}
    # 优先 AP, 没有再用 HE / SAP — 大部分船三种弹至少有一种
    velocity = None
    for shell_type in ("AP", "HE", "SAP"):
        s = shells.get(shell_type)
        if isinstance(s, dict) and s.get("bullet_speed"):
            velocity = s["bullet_speed"]
            break
    if not velocity:
        return None
    range_m = distance_km * 1000  # 单位: km → m
    return (range_m / max_disp) * (velocity / 800.0)


def compute_class_accuracy_means(encyclopedia):
    """计算每个舰种的**平均机械精度**, 用于把单船精度归一到 ~1.0。

    跑一遍百科, 给每艘有主炮的船算 `_mechanical_accuracy`, 按舰种分组取均值。
    返回 `{BB: ..., CA: ..., DD: ...}`。CV / Sub 通常拿不到值。

    用途: 单船的 `hit_rate_factor = ship_accuracy / class_mean[ship.type]`,
    然后在 analysis 里乘到 main_battery_hit_rate 阈值上。

    > 注意: 这是全船库的平均, 不是玩家舰队的平均。所以同一艘船在所有玩家身上的
    > hit_rate_factor 是固定的, 是船的固有属性。
    """
    by_class = {}
    for sid, meta in (encyclopedia or {}).items():
        if not isinstance(meta, dict):
            continue
        cls = meta.get("type")
        art = (meta.get("default_profile") or {}).get("artillery")
        acc = _mechanical_accuracy(art)
        if cls and acc:
            by_class.setdefault(cls, []).append(acc)
    return {cls: (sum(vals) / len(vals)) for cls, vals in by_class.items() if vals}


SHIP_SUM_COLUMNS = [
    "battles", "wins", "damage_dealt", "xp", "frags", "survived_battles",
    "main_battery_hits", "main_battery_shots",
    "torpedoes_hits", "torpedoes_shots",
    "planes_killed", "damage_scouting", "ships_spotted",
    "art_agro", "torpedo_agro",
    "capture_points", "dropped_capture_points",
]


def _sub(d, *keys):
    """安全的嵌套字典访问. `_sub(meta, "images", "contour")` 等于 `meta["images"]["contour"]`,
    但中间任意一层 None / 缺失就返回 {}。避免 KeyError / TypeError 链式爆炸。"""
    for k in keys:
        d = (d or {}).get(k)
        if d is None:
            return {}
    return d


def build_dataframe(ships_stats, encyclopedia, pr_data=None, mode="pvp", *, mechanical_means=None):
    """**核心构建函数** — 合并 per-ship 战绩 + 百科 + 全服 PR 期望, 返回带派生指标的 DataFrame。

    参数:
    - `ships_stats`: WG `/wows/ships/stats` 返回的 list, 每项 `{ship_id, pvp: {...}, rank_solo: {...}}`
    - `encyclopedia`: `{ship_id_str: {tier, type, name, default_profile, images, ...}}`
    - `pr_data`: wows-numbers 期望值, `{ship_id_str: {expected_damage, expected_frags, expected_winrate}}`
    - `mode`: 取哪个模式的战绩 (默认 pvp; 其他: rank_solo / club / oper_solo / oper_div)

    跳过条件:
    - 百科没收录该船 (玩家玩过的"绝版船")
    - 该模式 0 场 (玩家没在这个模式打过这条船)

    派生流程: 累计 sum → 百分率/平均/PR 标准化 → ship_pr → ship battle_power。
    """
    pr_data = pr_data or {}
    class_means = compute_class_accuracy_means(encyclopedia) if mechanical_means is None else mechanical_means
    rows = []
    for ship in ships_stats:
        sid = str(ship.get("ship_id"))
        meta = encyclopedia.get(sid)
        if not meta:
            continue  # 百科未收录 (老船 / 测试服船)
        stats = ship.get(mode)
        if not stats or not stats.get("battles"):
            continue  # 该模式 0 场
        mb = stats.get("main_battery") or {}
        torp = stats.get("torpedoes") or {}
        pr_exp = pr_data.get(sid, {})

        # 该船的机械精度系数 (相对该舰种平均). 1.0 = 该舰种平均水平
        art = (meta.get("default_profile") or {}).get("artillery")
        ship_acc = _mechanical_accuracy(art)
        cls = meta.get("type")
        if ship_acc and cls in class_means and class_means[cls] > 0:
            hit_factor = ship_acc / class_means[cls]
        else:
            hit_factor = None

        # 航站/航巡/航驱检测: 非 CV/Sub 但 default_profile 有飞机 (dive_bomber / torpedo_bomber)
        # WG API 的 type 不区分这些, 必须自己探测 (encyclopedia v3 起 fetch 时显式列出这俩字段)
        profile = meta.get("default_profile") or {}
        is_hybrid = (cls != "AirCarrier" and cls != "Submarine"
                     and (profile.get("dive_bomber") or profile.get("torpedo_bomber")))

        rows.append({
            "ship_id": sid,
            "name": meta.get("name") or f"ship_{sid}",
            "tier": meta.get("tier"),
            "type": meta.get("type"),
            "nation": meta.get("nation"),
            "is_premium": meta.get("is_premium", False),
            "is_hybrid": bool(is_hybrid),
            "image_contour": _sub(meta, "images", "contour"),
            "image_small": _sub(meta, "images", "small"),
            "hit_rate_factor": hit_factor,
            "battles": stats["battles"],
            "wins": stats.get("wins", 0),
            "damage_dealt": stats.get("damage_dealt", 0),
            "xp": stats.get("xp", 0),
            "frags": stats.get("frags", 0),
            "survived_battles": stats.get("survived_battles", 0),
            "main_battery_hits": mb.get("hits", 0),
            "main_battery_shots": mb.get("shots", 0),
            "torpedoes_hits": torp.get("hits", 0),
            "torpedoes_shots": torp.get("shots", 0),
            "planes_killed": stats.get("planes_killed", 0),
            "damage_scouting": stats.get("damage_scouting", 0),
            "max_damage_scouting": stats.get("max_damage_scouting", 0),  # 单舰最高侦伤, 面壁者挑战用
            "ships_spotted": stats.get("ships_spotted", 0),
            "art_agro": stats.get("art_agro", 0),
            "torpedo_agro": stats.get("torpedo_agro", 0),
            "capture_points": stats.get("capture_points", 0),
            "dropped_capture_points": stats.get("dropped_capture_points", 0),
            "expected_damage": pr_exp.get("expected_damage"),
            "expected_frags": pr_exp.get("expected_frags"),
            "expected_winrate": pr_exp.get("expected_winrate"),
        })
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    for column in ("expected_damage", "expected_frags", "expected_winrate"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    # === 派生指标 (列向量化, 而非逐行 apply) ===
    df["win_rate"] = df["wins"] / df["battles"]
    df["avg_damage"] = df["damage_dealt"] / df["battles"]
    df["avg_xp"] = df["xp"] / df["battles"]
    df["avg_frags"] = df["frags"] / df["battles"]
    df["survival_rate"] = df["survived_battles"] / df["battles"]
    # 命中率: shots=0 会产生 inf, 替换为 NaN 后填 0
    df["main_battery_hit_rate"] = (
        (df["main_battery_hits"] / df["main_battery_shots"])
        .replace([np.inf, -np.inf], np.nan).fillna(0)
    )
    df["torpedo_hit_rate"] = (
        (df["torpedoes_hits"] / df["torpedoes_shots"])
        .replace([np.inf, -np.inf], np.nan).fillna(0)
    )
    df["avg_planes_killed"] = df["planes_killed"] / df["battles"]
    df["avg_scouting_damage"] = df["damage_scouting"] / df["battles"]
    df["avg_potential_damage"] = (df["art_agro"] + df["torpedo_agro"]) / df["battles"]

    # PR 标准化 (无期望值的船 → NaN, 后续聚合时被 dropna 跳过)
    # 公式: n = max(0, (actual / expected - lower) / (1 - lower))
    exp_dmg = df["expected_damage"].where(df["expected_damage"] > 0)
    exp_frg = df["expected_frags"].where(df["expected_frags"] > 0)
    exp_wr = df["expected_winrate"].where(df["expected_winrate"] > 0)
    df["n_dmg"] = ((df["avg_damage"] / exp_dmg - 0.4) / 0.6).clip(lower=0)
    df["n_frags"] = ((df["avg_frags"] / exp_frg - 0.1) / 0.9).clip(lower=0)
    df["n_wr"] = ((df["win_rate"] / exp_wr - 0.7) / 0.3).clip(lower=0)
    df["ship_pr"] = 700 * df["n_dmg"] + 300 * df["n_frags"] + 150 * df["n_wr"]
    # 单船战力 = sigmoid(ship_pr) × 投入度 sqrt(battles/100)
    df["battle_power"] = df.apply(
        lambda r: compute_ship_battle_power(r["ship_pr"], r["battles"])
                  if pd.notna(r["ship_pr"]) and r["ship_pr"] > 0 else 0,
        axis=1,
    )
    return df


def aggregate_to_stats(df):
    """**DataFrame → PlayerStats 反向聚合**, 用于"筛选后重算总览指标"。

    对所有 SUM 列求和, 然后 PlayerStats 的 @property 会自动算 win_rate / avg_damage 等。
    用例:
    - 总览雷达筛选舰种/等级 → 用 aggregate_to_stats(filtered_df) 替换玩家原 stats
    - tier_weighted_score 在每个 tier 内: agg = aggregate_to_stats(tier_group)
    """
    if df is None or df.empty:
        return None
    return PlayerStats(
        battles=int(df["battles"].sum()),
        wins=int(df["wins"].sum()),
        damage_dealt=int(df["damage_dealt"].sum()),
        xp=int(df["xp"].sum()),
        frags=int(df["frags"].sum()),
        survived_battles=int(df["survived_battles"].sum()),
        main_battery_hits=int(df["main_battery_hits"].sum()),
        main_battery_shots=int(df["main_battery_shots"].sum()),
        torpedoes_hits=int(df["torpedoes_hits"].sum()),
        torpedoes_shots=int(df["torpedoes_shots"].sum()),
        planes_killed=int(df["planes_killed"].sum()),
        damage_scouting=int(df["damage_scouting"].sum()),
        ships_spotted=int(df["ships_spotted"].sum()),
        art_agro=int(df["art_agro"].sum()),
        torpedo_agro=int(df["torpedo_agro"].sum()),
        capture_points=int(df["capture_points"].sum()),
        dropped_capture_points=int(df["dropped_capture_points"].sum()),
    )
