"""F1 WOWS offline scoring. Adapted from wows_analyzer/personal_rating.py."""
import math


def compute_pr(n_dmg, n_frags, n_wr):
    """PR 原始值 = `700·n_dmg + 300·n_frags + 150·n_wr` (社区标准公式).

    输入是 3 个标准化值. 输出 0 ~ 2500+ (无强制上限, 但大部分玩家落 300 ~ 2200).
    用于内部计算; 玩家看到的是 compute_battle_power 处理后的"战力"数字。
    """
    return 700 * n_dmg + 300 * n_frags + 150 * n_wr


def compute_battle_power(pr):
    """**核心:** 把 PR 通过 sigmoid 压制成"战力"数字 (0 ~ 70000).

        BP = 70000 / (1 + exp(-(PR - 1700) / 250))

    参考点:
    - PR=1000  →  ~500     (新手区)
    - PR=1500  →  ~12500   (一般)
    - PR=1700  →  35000    (sigmoid 中点, 接近全服中位数)
    - PR=2000  →  ~58000   (高手区)
    - PR=2200  →  ~62000   (顶尖)
    - PR=2500+ →  渐近 70000 (天花板)

    设计意图:
    - PR 是线性的, 玩家从 1700 到 2500 看不出量级差距, sigmoid 把高分段拉开
    - 又限制了天花板 — 没有这个的话 PR=2500 直接爆出 200k+ 战力, 视觉上离谱

    上层调用要继续乘 4 个 modifiers (apply_battle_power_modifiers)。
    """
    if pr is None or pr <= 0:
        return 0
    return int(70000 / (1 + math.exp(-(pr - 1700) / 250)))


def compute_ship_battle_power(ship_pr, battles):
    """**单船战力** — 用更陡的 sigmoid (250→300) + 场次投入度收敛因子.

        BP_ship = (30000 / (1 + exp(-(ship_pr - 1700) / 300))) × min(1, sqrt(battles/100))

    场次投入度 `sqrt(battles/100)`:
    - 1 场 → 0.10  (大量打折)
    - 100 场 → 1.0  (满分)
    - 用 sqrt 而非线性是因为前几场提升大、后面边际收益递减

    这是为了防止"1 把超神战绩"显示成本命舰 — 必须先有积累。
    """
    if ship_pr is None or ship_pr <= 0 or battles is None or battles <= 0:
        return 0
    base = 30000 / (1 + math.exp(-(ship_pr - 1700) / 300))
    investment = min(1.0, math.sqrt(battles / 100))
    return int(base * investment)


def compute_battle_power_modifiers(stats, ships_df, n_challenges=0):
    """**4 个修正系数** — 这是"段子感"的来源, 也是项目独有特色.

    返回 `{challenge, horse, variety, veteran}` 四个浮点, 上层乘起来作用到 BP_base。

    - **challenge**: `1.0 + 0.02 × N_完成挑战` (上限 1.40, 21 个全完成)
      鼓励玩家挑战不同条件的"成就"。

    - **horse (🐴)**: 水面舰艇占比的"鄙视链"系数
        - ≥ 80% 水面 → 1.10 (绅士奖)
        - 30% ~ 60%  → 0.95 (有点马)
        - < 30%       → 0.85 (重马)
      CV 和 Sub 因为隐蔽 / 操作不直观, 社区称之为"马"。

    - **variety (🌈 杂食)**: `1.0 + 0.025×(舰种数-2) + 0.012×(等级数-3)` (上限 1.15)
      奖励玩 5 个舰种和多个等级的全栈玩家, 不让 T10 BB 单兵种刷子赢过有广度的玩家。

    - **veteran (🎖️ 老兵)**: `1.0 + log10(1 + total/100) × 0.06` (上限 1.20)
      场次越多越加成. log 增长, 1000 场 ≈ 1.062, 10000 场 ≈ 1.124, 100000 场 ≈ 1.183.

    依赖:
    - stats: 仅用于占位 (PlayerStats 对象), 现版本未直接读取
    - ships_df: 必需, 用于算 horse / variety / veteran
    - n_challenges: 由调用方先算 (challenges.get_completed_challenges) 再传入
    """
    # 默认全部 1.0 (任何条件 不满足 / 数据缺失 → 不加成 不削弱)
    m = {"challenge": 1.0 + 0.02 * max(0, n_challenges),
         "horse": 1.0, "variety": 1.0, "veteran": 1.0}
    if ships_df is None or ships_df.empty:
        return m
    total_b = int(ships_df["battles"].sum())
    if total_b <= 0:
        return m

    # 🐴 含量 — 注意: 这里只看 CV/Sub 的简单比例, 没算 hybrid (航站/航巡/航驱)
    # 因为这个系数偏视觉, 不需要精细; 雷达的"🐴含量"轴才用精细公式
    try:
        cv_b = int(ships_df[ships_df["type"] == "AirCarrier"]["battles"].sum())
        sub_b = int(ships_df[ships_df["type"] == "Submarine"]["battles"].sum())
        surface_pct = (total_b - cv_b - sub_b) / total_b
        if surface_pct >= 0.80:
            m["horse"] = 1.10
        elif surface_pct < 0.30:
            m["horse"] = 0.85
        elif surface_pct < 0.60:
            m["horse"] = 0.95
    except Exception:
        pass

    # 🌈 杂食 — 舰种数最多 5 (CV/BB/CA/DD/Sub), 等级数最多 11 (T1~T11)
    # 减去基线 2 / 3 让"只玩 1-2 种"的玩家拿不到加成
    try:
        n_classes = ships_df["type"].nunique()
        n_tiers = ships_df["tier"].nunique()
        v = max(0, n_classes - 2) * 0.025 + max(0, n_tiers - 3) * 0.012
        m["variety"] = 1.0 + min(0.15, v)
    except Exception:
        pass

    # 🎖️ 老兵 — log10 是凸函数, 后期边际衰减
    # 公式选 0.06 是经验值: 让 1000 场玩家有 +6%, 10000 场有 +12%, 上限 +20%
    try:
        bonus = math.log10(1 + total_b / 100.0) * 0.06
        m["veteran"] = 1.0 + min(0.20, bonus)
    except Exception:
        pass

    return m


def apply_battle_power_modifiers(base_bp, modifiers):
    """把 4 个系数乘到 BP_base 上, 输出最终展示战力。

    顺序无关 (乘法可交换), 但写成固定顺序方便 debug。
    """
    if base_bp <= 0 or modifiers is None:
        return base_bp
    mult = 1.0
    for k in ("challenge", "horse", "variety", "veteran"):
        mult *= modifiers.get(k, 1.0)
    return int(base_bp * mult)
