"""F1 WOWS offline scoring. Adapted from wows_analyzer/analysis.py."""
import math
import numpy as np
import pandas as pd
from .personal_rating import compute_pr, compute_battle_power


SCORE_POINTS = [10, 40, 70, 100, 130]


METRIC_KEYS = {
    "胜率": "win_rate",
    "场均伤害": "avg_damage",
    "场均击杀": "avg_frags",
    "场均经验": "avg_xp",
    "击杀效率": "kd_ratio",
    "主炮命中": "main_battery_hit_rate",
    "场均抗伤": "avg_potential_damage",
    "场均侦查": "avg_scouting_damage",
    "🐴含量": "_horse_ratio",  # 动态轴: 多舰种=马含量, 单舰种=该舰种场次
}


HORSE_CLASSES = {"AirCarrier", "Submarine"}


CLASS_ABBREV = {
    "AirCarrier": "航母", "Battleship": "战列",
    "Cruiser": "巡洋", "Destroyer": "驱逐", "Submarine": "潜艇",
}


def _battle_confidence(tier, battles):
    """**单艘船**评分的可信度系数 (0.5 - 1.0). 用于单船详情页, 不进入总览。

    场次太少时数据噪声大 (1 把超神 / 拉胯), 把分数往中间拉:
    - T10-T11: 80 场达到满分 1.0 (高等级船单局更慢, 样本应该更多)
    - T9 及以下: 40 场满分
    - 1 场: 0.5 (打 5 折)
    - 中间线性插值
    """
    threshold = 80 if (tier or 0) >= 10 else 40
    if battles >= threshold:
        return 1.0
    if battles <= 1:
        return 0.5
    return 0.5 + 0.5 * (battles - 1) / (threshold - 1)


def _battle_count_score(tier, battles):
    """**单艘船**的"场次"维度评分 (0-150). 雷达额外第 7 轴。

    设计:
    - 0 ~ threshold (40 / 80) 场: 线性 0→100, 鼓励玩到满意场次
    - threshold 以上: log 增长, 1000 场 ≈ 110, 10000 场 ≈ 120

    > 不像主指标那样有 130 上限, 但增长极慢防止刷场玩家爆分。
    """
    threshold = 80 if (tier or 0) >= 10 else 40
    if battles <= 0:
        return 0.0
    if battles <= threshold:
        return battles / threshold * 100.0
    over = battles - threshold
    return 100.0 + 4.3 * math.log(1.0 + over / 100.0)


def compute_single_ship_scores(ships_subset_df, benchmarks):
    """**单舰雷达**的 6 维评分 (玩家页 → 战舰 tab → 点击某船).

    与总览雷达的差异:
    - PR 三轴 取 (PR-based 分, class+tier 绝对阈值分) 的**最大值**
      原因: 精品船 (Yamato/Ohio) 全服期望值很高, 直接用 PR 容易压制大佬玩家的"绝对水平"
    - 全部分数乘 `_battle_confidence` (场次置信度)
    - 增加"场次"作为第 7 维 (`_battle_count_score`)
    - 不显示 🐴 含量 (单船 0/100, 无意义)

    返回 `(scores_dict, confidence)`. confidence 用于 UI 显示警告 (例: "< 40 场不可信")。
    """
    if ships_subset_df is None or ships_subset_df.empty:
        return None, 1.0

    from .ships import aggregate_to_stats
    row = ships_subset_df.iloc[0]
    tier = int(row["tier"]) if pd.notna(row.get("tier")) else 10
    battles = int(row["battles"])
    confidence = _battle_confidence(tier, battles)

    stats = aggregate_to_stats(ships_subset_df)
    pr_scores = compute_scores(stats, benchmarks, ships_df=ships_subset_df)

    # PR 三轴 (胜率/伤害/击杀): 取 PR-based 与 class+tier 绝对阈值的最大值
    # 避免精品船因全服期望高而压制大佬玩家的"绝对水平"视觉冲击
    for display_name in PR_METRIC_MAPPING:
        if display_name not in pr_scores:
            continue
        stat_attr = METRIC_KEYS.get(display_name)
        if not stat_attr or stat_attr == "_horse_ratio":
            continue
        thresholds = compute_weighted_thresholds(stat_attr, ships_subset_df, benchmarks)
        if thresholds is None:
            continue
        raw_val = getattr(stats, stat_attr)
        abs_score = score_value(raw_val, thresholds)
        cur_raw, cur_sc = pr_scores[display_name]
        if abs_score > cur_sc:
            pr_scores[display_name] = (cur_raw, abs_score)

    adjusted = {}
    for k, (raw, sc) in pr_scores.items():
        if k == "🐴含量":  # 单艘船 0 或 100, 无意义
            continue
        adjusted[k] = (raw, sc * confidence)

    adjusted["场次"] = (battles, _battle_count_score(tier, battles))
    return adjusted, confidence


def compute_horse_or_class_score(ships_df):
    """**动态第 9 轴**: 多舰种显示🐴含量, 单舰种 (筛选状态) 显示该舰种场次。

    多舰种 (默认): "🐴含量" 轴
        显示值 = 纯水面舰占比 (CV + Sub + hybrid 都不算)
        评分用 purity = 1 - (CV×1 + Sub×2 + hybrid×0.5) / total
            - CV: 标准权重 1, 已经是公认的"马"
            - Sub: 权重 2 (社区认为更难受 — 看不见摸不着的潜艇)
            - hybrid: 权重 0.5 (航站/航巡/航驱, 半马)
        阈值 `[0.40, 0.65, 0.85, 0.95, 1.00]`: 大部分玩家在 0.7~0.95

    单舰种 (筛选驱逐/战列/巡洋/航母/潜艇): "{舰种}场次" 轴
        显示值 = 该舰种总场次, 阈值 `[100, 500, 1500, 3000, 5000]`
        鼓励玩家把单一舰种玩深。

    返回 `(metric_name, (raw, score))` 或 None (无数据)。
    """
    if ships_df is None or ships_df.empty:
        return None
    types = ships_df["type"].dropna().unique().tolist()

    # 单舰种 (筛选状态): 显示该舰种场次, 用 [100, 500, 1500, 3000, 5000] 阈值
    if len(types) == 1:
        cls = types[0]
        battles = int(ships_df["battles"].sum())
        if battles <= 0:
            return None
        score = score_value(battles, [100, 500, 1500, 3000, 5000])
        abbrev = CLASS_ABBREV.get(cls, cls)
        return (f"{abbrev}场次", (float(battles), score))

    # 多舰种: 🐴 含量
    total = int(ships_df["battles"].sum())
    if total <= 0:
        return None
    cv_b = int(ships_df[ships_df["type"] == "AirCarrier"]["battles"].sum())
    sub_b = int(ships_df[ships_df["type"] == "Submarine"]["battles"].sum())
    # hybrid (航站/航巡/航驱): 非 CV/Sub 但有 dive_bomber/torpedo_bomber, 半马
    if "is_hybrid" in ships_df.columns:
        hybrid_b = int(ships_df[ships_df["is_hybrid"] == True]["battles"].sum())  # noqa: E712
    else:
        hybrid_b = 0
    # 显示值: 纯水面舰占比 (直观)
    surface_ratio = (total - cv_b - sub_b - hybrid_b) / total
    # 评分用 purity (加权), CV ×1, Sub ×2 (更"马"), 航 X ×0.5
    purity = 1 - (cv_b + sub_b * 2 + hybrid_b * 0.5) / total
    score = score_value(purity, [0.40, 0.65, 0.85, 0.95, 1.00])
    return ("🐴含量", (surface_ratio, score))


def compute_horse_score(ships_df):
    """**旧 API 兼容包装** (仅返回 🐴含量, 单舰种返回 None). 新代码用 compute_horse_or_class_score。"""
    info = compute_horse_or_class_score(ships_df)
    if info is None or info[0] != "🐴含量":
        return None
    return info[1]


def score_value(value, thresholds):
    """**核心打分函数**: value → 0~150 分.

    `thresholds` 是 5 个递增临界点, 对应分数 [10, 40, 70, 100, 130]:
    - value ≤ 0:           返回 0
    - value < thresholds[0]: 线性 [0, thresholds[0]] → [0, 10] (避免悬崖)
    - thresholds 区间内:   np.interp 线性插值
    - value > thresholds[-1] × 1.5: 截断到 130 × 1.15 = 149.5 (防止极端值爆表)

    > 底部不直接归 0 是为了让"几乎能打到底线"的玩家也有 1-9 分的连续反馈, 而不是全部归 0。
    """
    if value <= 0:
        return 0.0
    if value < thresholds[0]:
        # 线性: 0 -> 0 分; thresholds[0] -> SCORE_POINTS[0]=10
        return SCORE_POINTS[0] * (value / thresholds[0])
    if value >= thresholds[-1] * 1.5:
        return SCORE_POINTS[-1] * 1.15
    return float(np.interp(value, thresholds, SCORE_POINTS))


def compute_weighted_thresholds(metric, ships_df, benchmarks):
    """根据玩家舰船构成 (按场次加权) 计算指标的**有效阈值**。

        thresholds_i = Σ(b × base_i × class_factor × tier_factor) / Σ(b)
                       (for each ship in ships_df)

    含义: 阈值是"如果玩家的舰队全是这种构成, 应该达到的分数线"。这是 PR 之外
    另一套打分法的基础, 用于:
    1. 单船评分的"绝对阈值"参考 (compute_single_ship_scores 取 max)
    2. 主炮命中等 5 轴的总览评分基线
    3. 工会筛选时的修正阈值 (clan.compute_clan_radar_scores_filtered)

    特殊处理:
    - `class_factor=null` → 跳过该船 (例: 航母不算主炮命中)
    - `metric=main_battery_hit_rate` → 额外乘 hit_rate_factor (该船的机械精度)

    返回 None 表示该指标在当前舰队下不适用 (所有船都被跳过)。
    """
    base = benchmarks["base"][metric]
    class_factors = benchmarks.get("class_factors", {})
    metric_tier_factors = benchmarks.get("tier_factors", {}).get(metric, {})

    if ships_df is None or ships_df.empty:
        return base

    total_battles = 0
    weighted = [0.0] * 5
    for _, row in ships_df.iterrows():
        if pd.isna(row.get("tier")) or row.get("type") is None:
            continue
        cls_factors = class_factors.get(row["type"], {})
        cf = cls_factors.get(metric, 1.0)
        if cf is None:
            continue  # 该舰种不适用该指标 (例: CV/Sub 主炮命中)
        # 主炮命中率特殊: 还要乘该船的机械精度因子 (mechanical accuracy → class mean)
        # 苏巡 (高初速 + 大口径) 阈值更高, 美巡 (慢初速) 阈值更低
        if metric == "main_battery_hit_rate":
            sf = row.get("hit_rate_factor")
            if sf is not None and not pd.isna(sf) and sf > 0:
                cf = cf * sf
        tf = metric_tier_factors.get(str(int(row["tier"])), 1.0)
        factor = cf * tf
        b = int(row["battles"])
        total_battles += b
        for i in range(5):
            weighted[i] += b * base[i] * factor

    if total_battles == 0:
        return None
    return [w / total_battles for w in weighted]


def compute_tier_difficulty(ships_df, benchmarks):
    """**场次加权的等级难度系数** (T1≈0.55 → T10=1.00 → T11≈1.03).

        difficulty = Σ(b × tier_diff[tier]) / Σ(b)
                     (for each ship in ships_df)

    用途:
    1. 总览雷达 overall 分: 用作整体乘数, 让低等级大佬不抢高等级排名
    2. PR adjusted: pr_adjusted = pr_raw × difficulty, 间接影响战力数字

    > 数据源: `benchmarks.json::tier_difficulty`. 设计原则是 T10 = 1.0 基准,
    > 低等级 (新手多) 打折, T11 (全 premium 高难) 略加成。
    """
    if ships_df is None or ships_df.empty:
        return 1.0
    table = (benchmarks or {}).get("tier_difficulty") or {}
    if not table:
        return 1.0
    total = 0
    weighted = 0.0
    for _, row in ships_df.iterrows():
        if pd.isna(row.get("tier")):
            continue
        d = table.get(str(int(row["tier"])), 1.0)
        b = int(row.get("battles", 0))
        if b > 0:
            total += b
            weighted += b * d
    return weighted / total if total > 0 else 1.0


def compute_tier_weighted_score(metric, ships_df, benchmarks):
    """**避免「平均的平均」陷阱**: 每等级独立打分, 再按场次×难度加权聚合。

        for each tier_group in ships_df.groupby('tier'):
            actual = aggregate_to_stats(group).metric_value
            thresholds = compute_weighted_thresholds(...)
            score_t = score_value(actual, thresholds)
            weight_t = battles_t × tier_difficulty_t
        result = Σ(score_t × weight_t) / Σ(weight_t)

    用于经验 / 击杀效率 / 主炮命中 / 抗伤 / 侦查 这 5 个非 PR 指标。

    > 为什么不直接按场次平均所有船的指标? 因为 T1 60% 胜率 和 T10 60% 胜率 含金量
    > 完全不同。先在 tier 内打分 (用对应 tier 的阈值), 再 difficulty 加权,
    > 才能让 T10 的高分真正"值钱"。

    自动跳过不适用的舰种 (cf=null), 例如纯航母组算主炮命中会返回 None。
    """
    if ships_df is None or ships_df.empty:
        return None

    from .ships import aggregate_to_stats  # 避免循环 import

    # 1. 过滤到适用舰种 (class_factor=null 的被排除)
    class_factors = (benchmarks or {}).get("class_factors", {})

    def _is_applicable(ship_type):
        """该舰种对此指标是否适用 — class_factors 里该格为 null 表示不算 (e.g. CV 不算主炮命中)."""
        return class_factors.get(ship_type, {}).get(metric, 1.0) is not None

    applicable = ships_df[ships_df["type"].apply(_is_applicable)]
    if applicable.empty:
        return None

    diff_table = (benchmarks or {}).get("tier_difficulty") or {}
    weighted_sum = 0.0
    weight_total = 0.0
    any_applicable = False

    # 2. 按 tier 分组, 每组独立打分
    for tier_val, group in applicable.groupby("tier"):
        if pd.isna(tier_val):
            continue
        b_t = int(group["battles"].sum())
        if b_t <= 0:
            continue
        agg_stats = aggregate_to_stats(group)
        if agg_stats is None:
            continue
        actual = getattr(agg_stats, metric, None)
        if actual is None:
            continue
        thresholds = compute_weighted_thresholds(metric, group, benchmarks)
        if thresholds is None:
            continue  # 该等级该指标不适用 (例: 纯航母组的主炮命中)
        any_applicable = True
        score_t = score_value(actual, thresholds)
        # 3. 权重 = 场次 × 等级难度
        tier_diff = diff_table.get(str(int(tier_val)), 1.0)
        weight = b_t * tier_diff
        weighted_sum += weight * score_t
        weight_total += weight

    if not any_applicable or weight_total <= 0:
        return None
    return weighted_sum / weight_total


def compute_pr_aggregate(ships_df, benchmarks=None):
    """**按场次加权聚合每艘船的 PR 子分**, 输出玩家级 PR 及战力。

        n_dmg_overall   = Σ(ship.n_dmg × ship.battles) / Σ(ship.battles)
        n_frags_overall = ... (同上)
        n_wr_overall    = ... (同上)
        pr_raw          = compute_pr(n_dmg, n_frags, n_wr)
        pr_adjusted     = pr_raw × tier_difficulty
        battle_power    = compute_battle_power(pr_adjusted)

    返回 dict 含所有关键中间值, 给上层 (徽章 / 雷达) 各自使用。

    > 注意聚合是基于每艘船**已经标准化**的 n_* 而非原始指标。这样能避免
    > "高期望值的精品船拖低聚合"问题 — 因为 n_* 已经把"相对全服期望"的差异消化掉了。
    """
    if ships_df is None or ships_df.empty or "n_dmg" not in ships_df.columns:
        return None

    # 只保留有 PR 数据的船 (新船 / wows-numbers 没收录的 fallback 时 n_* 为 NaN)
    sub = ships_df.dropna(subset=["n_dmg", "n_frags", "n_wr"])
    if sub.empty:
        return None
    total = int(sub["battles"].sum())
    if total <= 0:
        return None

    # 场次加权平均归一化值 (不是分数!)
    n_dmg = float((sub["n_dmg"] * sub["battles"]).sum() / total)
    n_frags = float((sub["n_frags"] * sub["battles"]).sum() / total)
    n_wr = float((sub["n_wr"] * sub["battles"]).sum() / total)
    pr_raw = compute_pr(n_dmg, n_frags, n_wr)
    tier_diff = compute_tier_difficulty(ships_df, benchmarks) if benchmarks else 1.0
    pr_adjusted = pr_raw * tier_diff
    return {
        "pr": pr_raw,
        "pr_adjusted": pr_adjusted,
        "tier_difficulty": tier_diff,
        "battle_power": compute_battle_power(pr_adjusted),
        "n_dmg": n_dmg,
        "n_frags": n_frags,
        "n_wr": n_wr,
        "covered_battles": total,  # 有 PR 数据的船的总场次
        "total_battles": int(ships_df["battles"].sum()),  # 全部场次
    }


PR_METRIC_MAPPING = {
    "胜率": "n_wr",
    "场均伤害": "n_dmg",
    "场均击杀": "n_frags",
}


def compute_scores(stats, benchmarks, ships_df=None):
    """**总览雷达的核心入口**. 返回 `{显示名: (原始值, 0-150 分)}`.

    分支:
    - **🐴含量轴** (动态): 调 compute_horse_or_class_score
    - **PR 三轴** (胜率/伤害/击杀): 用 compute_pr_aggregate 的 n_* 经公式转分
        - 胜率: `90·n_wr - 50` (公式陡, 60% 已经是 top 20%)
        - 伤害/击杀: `60·n - 20`
        - 上限 130×1.15 = 149.5
        - 总场次 < 40 时线性衰减 (0.5 + 0.5·(b-1)/39, 防 1 场冲分)
    - **其他 5 轴**: tier-weighted (compute_tier_weighted_score)
    - **无 ships_df 时**: 退化到单阈值法 (compute_weighted_thresholds + score_value)
    """
    pr_agg = compute_pr_aggregate(ships_df, benchmarks)

    result = {}
    for display_name, attr in METRIC_KEYS.items():
        # 🐴含量 / 单舰种场次 动态计算 (本身不在 stats 上)
        if attr == "_horse_ratio":
            info = compute_horse_or_class_score(ships_df)
            if info is not None:
                key, val = info
                result[key] = val
            continue

        raw = getattr(stats, attr)

        # PR 三轴优先用 PR-based 标准化分
        if pr_agg and display_name in PR_METRIC_MAPPING:
            n_val = pr_agg[PR_METRIC_MAPPING[display_name]]
            # 胜率公式更陡: 60% 胜率 → ~94 分, 65% → ~99 分; 伤害/击杀同梯度则只到 76 分
            if display_name == "胜率":
                sc = max(0.0, 90.0 * n_val - 50.0)
            else:
                sc = max(0.0, 60.0 * n_val - 20.0)
            # 与 score_value 一致的硬上限 (130 × 1.15 = 149.5), 防 1 场 100% 冲爆
            sc = min(sc, SCORE_POINTS[-1] * 1.15)
            # 小样本置信度: 总场次 < 40 时按线性打折. 1 场 → 0.5x, 40 场 → 1.0x
            total_b = pr_agg.get("total_battles", 0)
            if total_b < 40:
                sc *= 0.5 + 0.5 * max(0, total_b - 1) / 39
            result[display_name] = (raw, sc)
            continue

        # 其他 5 轴: tier-weighted score
        if ships_df is not None and not ships_df.empty:
            sc = compute_tier_weighted_score(attr, ships_df, benchmarks)
            if sc is not None:
                result[display_name] = (raw, sc)
                continue

        # Fallback: 单阈值打分 (无 ships_df 或 tier-weighted 不适用时)
        thresholds = compute_weighted_thresholds(attr, ships_df, benchmarks)
        if thresholds is None:
            continue
        result[display_name] = (raw, score_value(raw, thresholds))
    return result


def analyze_strengths(scores, n=2):
    """**强弱项分析**: 取分数最高的 n 项作为优势, 最低的 n 项作为劣势。

    返回 `(strengths_list, weaknesses_list)`, 每项是 `(name, raw, score)` 元组。
    用于 OverviewTab 右侧的"🔥 优势 / 💧 劣势"文案。
    """
    items = [(name, raw, sc) for name, (raw, sc) in scores.items()]
    sorted_desc = sorted(items, key=lambda x: x[2], reverse=True)
    return sorted_desc[:n], sorted(sorted_desc[-n:], key=lambda x: x[2])
