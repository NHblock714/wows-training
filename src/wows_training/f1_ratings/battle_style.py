"""F1 WOWS offline scoring. Adapted from wows_analyzer/battle_style.py."""



STYLE_METRICS = {
    "抗线型": ["场均抗伤"],
    "输出型": ["场均伤害", "场均击杀"],
    "侦察型": ["场均侦查"],
    "中段怪": ["主炮命中"],
    "冲锋型": ["场均伤害", "场均击杀"],
    "稳健型": ["击杀效率", "胜率"],
    "战术大师": ["胜率", "场均经验"],
    "收割型": ["场均击杀"],
    "全能型": None,
}


def _style_power(name, scores_dict, stats=None):
    """**风格战力贡献** = `Σ(max(0, 该风格指标的分数 - 40)) × 10`, 截断到 [0, 2500]。

    设计:
    - 减 40 是因为 40 分对应"一般"水平, 只算超过及格线的"超额贡献"
    - ×10 把 0-90 的"超额分"放大到 0-900 的"战力数字"
    - 截断 2500 防止极端情况爆表 (一般也到不了)

    用途: 风格卡上小字显示 "+520 战力" 等, 给玩家直观的"这个风格贡献了多少"反馈。
    """
    metrics = STYLE_METRICS.get(name)
    if metrics is None:
        metrics = list(scores_dict.keys())  # 全能型: 所有指标都算
    if not metrics:
        return 0
    total = sum(max(0, scores_dict.get(m, 40) - 40) for m in metrics)
    return min(int(total * 10), 2500)


def compute_class_tendency(ships_df):
    """**舰种倾向** — 固定占据风格列表第 1 位。

    判定逻辑 (优先级从高到低):
    1. 潜艇 ≥ 30% → 🐋 水下巨人
    2. 航母 ≥ 30% → ✈️ 空中小人
    3. 水面舰 ≥ 60% → 🚢 水面庶民
    4. 否则返回 None (混合风格玩家, 不显示倾向)

    power 计算各有公式 (与社区接受的"奇怪比例"成比例)。
    返回 `(name, emoji, desc, power)` 或 None。
    """
    if ships_df is None or ships_df.empty:
        return None
    total = int(ships_df["battles"].sum())
    if total <= 0:
        return None
    sub_b = int(ships_df[ships_df["type"] == "Submarine"]["battles"].sum())
    cv_b = int(ships_df[ships_df["type"] == "AirCarrier"]["battles"].sum())
    surface_b = total - sub_b - cv_b
    sub_pct = sub_b / total
    cv_pct = cv_b / total
    surface_pct = surface_b / total

    if sub_pct >= 0.30:
        power = int(200 + sub_pct * 500)
        return ("水下巨人", "🐋", "族谱单开并不总是代表从龙之功", power)
    if cv_pct >= 0.30:
        power = int(200 + cv_pct * 500)
        return ("空中小人", "✈️", "别急，这组掉光还有下一组", power)
    if surface_pct >= 0.60:
        power = int(100 + (surface_pct - 0.60) * 500)
        return ("水面庶民", "🚢", "我知道我在玩什么，但我别无选择", power)
    return None


def infer_battle_style(scores, stats=None, ships_df=None):
    """**主入口** — 综合 8 个雷达分 + 舰种构成, 输出风格列表。

    参数:
    - `scores`: `{显示名: (raw, 0-130 分)}` (来自 `analysis.compute_scores`)
    - `stats`:  PlayerStats (现版本未直接使用, 预留扩展)
    - `ships_df`: 用于算舰种倾向

    返回 `[(name, emoji, desc, power), ...]`, 第 1 位是舰种倾向 (如有),
    后续是所有命中的战斗风格按置信度降序。若一个风格都没命中, 走兜底分支。

    > **不限制数量**: 早期版本限 3 个, 后改成全展示 — 玩家有多个标签其实更有趣。
    """
    result = []
    tendency = compute_class_tendency(ships_df)
    if tendency:
        result.append(tendency)

    if not scores:
        return result

    # 提取每个指标的"分数"部分 (raw 值不参与风格判定)
    s = {k: v[1] for k, v in scores.items()}
    kd = s.get("击杀效率", 50)
    dmg = s.get("场均伤害", 50)
    frags = s.get("场均击杀", 50)
    tank = s.get("场均抗伤", 50)
    scout = s.get("场均侦查", 50)
    hit = s.get("主炮命中", 50)
    win = s.get("胜率", 50)
    xp = s.get("场均经验", 50)

    candidates = []

    def add(name, emoji, desc, confidence):
        """候选 = (name, emoji, desc, 战力贡献, 排序用置信度)"""
        candidates.append((name, emoji, desc, _style_power(name, s, stats), confidence))

    # === 单维度风格 (分≥80 触发) ===
    if tank >= 80:
        add("抗线型", "🛡️", "爱慕还是耐揍王", tank)

    if dmg >= 80:
        add("输出型", "💥", "你问赢了输了，我说赚了", dmg + frags)

    if scout >= 80:
        # 侦察型 ×1.2 让侦察专精玩家排名靠前 (侦察分一般不会和别的同时高)
        add("侦察型", "👁️", "我会一直视奸你，直到永远...", scout * 1.2)

    if hit >= 80:
        # 中段怪门槛抬高 (历史 70→80), 因为命中分容易刷
        add("中段怪", "🎯", "没关就是没开", hit * 1.1)

    # === 组合风格 (多维度同时满足) ===
    # 冲锋型: 击杀效率低 (容易被秒) 但伤害/击杀产出还不错 — 典型"自爆"玩家
    # 阈值放宽 (击杀效率 35→45) 让中等技术的冲送玩家也能触发
    if kd < 45 and (dmg >= 55 or frags >= 60):
        add("冲锋型", "⚔️", "数据？我只是着急开下一把", dmg + frags - kd)

    # 稳健型: 击杀效率高 + 胜率高 — 不浪费命的好玩家
    if kd >= 70 and win >= 55:
        add("稳健型", "🧘", "我已前压至 22 公里", kd + win)

    # 战术大师: 胜率超神 + 经验稳定 — 真正的指挥型选手
    # 胜率 ≥ 100 是 PR-based 分, 对应实际胜率 ~63%
    if win >= 100 and xp >= 55:
        add("战术大师", "🏆", "等打赢这场仗我就回老家结婚", win + xp)

    # 收割型: 击杀高但伤害相对低 — 抢人头但不主刀
    if frags >= 70 and dmg < frags - 10:
        add("收割型", "🪓", "K 头只是为了防止复活", frags)

    # === 全能型 (最高级标签) ===
    # 要求 8 项至少 6 项在 [55, 90] 区间 (不能太极端), 且最低分 ≥ 40 (不能有明显短板)
    qualifying = [v for v in s.values() if 55 <= v <= 90]
    if len(qualifying) >= 6 and min(s.values()) >= 40:
        add("全能型", "⚖️", "男人，什么罐头我说", sum(s.values()) / len(s))

    # === 兜底 (一个都没命中) ===
    if not candidates:
        avg = sum(s.values()) / len(s) if s else 0
        if avg < 30:
            result.append(("海猴巅峰", "🌱", "已严肃下载战舰世界 （游玩时长2h）", _style_power("海猴巅峰", s)))
        elif avg < 50:
            result.append(("绝望之谷", "📈", "请问有考虑过转会吗", _style_power("绝望之谷", s)))
        else:
            result.append(("混合型", "🌀", "又是一天新的金瓶掣签", _style_power("混合型", s)))
        return result

    # 按置信度降序排列, 全部展示
    candidates.sort(key=lambda x: x[4], reverse=True)
    for name, emoji, desc, power, _ in candidates:
        result.append((name, emoji, desc, power))
    return result
