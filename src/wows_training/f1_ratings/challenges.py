"""F1 WOWS offline scoring. Adapted from wows_analyzer/challenges.py."""



def _any_ship_avg_damage(ships_df, target):
    """"野兽先辈"专用: 任意船场均伤害精确等于 target. 用 round 防浮点误差。"""
    if ships_df is None or ships_df.empty:
        return False
    return any(int(round(v)) == target for v in ships_df["avg_damage"])


def _any_ship_battles_at_least(ships_df, n):
    """"真爱粉": 任意一条船场次达到 n (默认 10000)."""
    if ships_df is None or ships_df.empty:
        return False
    return (ships_df["battles"] >= n).any()


def _any_battleship_max_scouting(ships_df, threshold):
    """"面壁者": 任意战列舰单场侦伤 ≥ threshold.
    用 max_damage_scouting 字段 (单艘船的历史最高单场记录, 注意不是总和)."""
    if ships_df is None or ships_df.empty or "max_damage_scouting" not in ships_df.columns:
        return False
    bb = ships_df[ships_df["type"] == "Battleship"]
    if bb.empty:
        return False
    return (bb["max_damage_scouting"] >= threshold).any()


def _any_high_tier_low_avg_damage(ships_df, min_tier, max_avg_damage, min_battles=5):
    """"CN_70": 高等级船 (T8+) 场均伤害极低 (≤4000) — 真的菜或者只打了几把就没动力。
    min_battles 防止 1-2 场翻车导致挑战触发。"""
    if ships_df is None or ships_df.empty:
        return False
    sub = ships_df[(ships_df["tier"] >= min_tier) & (ships_df["battles"] >= min_battles)]
    if sub.empty:
        return False
    return (sub["avg_damage"] <= max_avg_damage).any()


def _any_ship_main_battery_hit_rate(ships_df, threshold, min_battles=5, min_shots=100, min_tier=6):
    """"PAC": 任意 T6+ 船主炮命中率 ≥ threshold (默认 60%).
    min_shots=100 防止打了 5 发都命中导致误触发。
    min_tier=6 防止低等级近身刷命中 (T1-T5 命中率天然高)。"""
    if ships_df is None or ships_df.empty or "main_battery_hit_rate" not in ships_df.columns:
        return False
    sub = ships_df[(ships_df["battles"] >= min_battles)
                   & (ships_df["main_battery_shots"] >= min_shots)
                   & (ships_df["tier"] >= min_tier)]
    if sub.empty:
        return False
    return (sub["main_battery_hit_rate"] >= threshold).any()


def _any_destroyer_torpedo_hit_rate(ships_df, threshold, min_battles=5, min_shots=50, min_tier=6):
    """"水雷魂": 任意 T6+ 驱逐舰鱼雷命中率 ≥ threshold (默认 15%).
    min_tier=6 排除低等级近身高命中场景。"""
    if ships_df is None or ships_df.empty or "torpedo_hit_rate" not in ships_df.columns:
        return False
    dd = ships_df[(ships_df["type"] == "Destroyer")
                  & (ships_df["battles"] >= min_battles)
                  & (ships_df["torpedoes_shots"] >= min_shots)
                  & (ships_df["tier"] >= min_tier)]
    if dd.empty:
        return False
    return (dd["torpedo_hit_rate"] >= threshold).any()


def _cv_sub_share(ships_df, threshold):
    """"全家福": 航母 + 潜艇场次占比 ≥ threshold (默认 60%) — 极重马玩家。"""
    if ships_df is None or ships_df.empty:
        return False
    total = int(ships_df["battles"].sum())
    if total <= 0:
        return False
    cv_b = int(ships_df[ships_df["type"] == "AirCarrier"]["battles"].sum())
    sub_b = int(ships_df[ships_df["type"] == "Submarine"]["battles"].sum())
    return (cv_b + sub_b) / total >= threshold


def _low_tier_share(ships_df, max_tier, threshold):
    """"刮宫圣手": T6 及以下场次占比 ≥ threshold (默认 30%) — 长期低分段玩家。"""
    if ships_df is None or ships_df.empty:
        return False
    total = int(ships_df["battles"].sum())
    if total <= 0:
        return False
    low_b = int(ships_df[ships_df["tier"] <= max_tier]["battles"].sum())
    return low_b / total >= threshold


def _black_friday_count(ships_df, min_count):
    """"林翩翩": ≥ N 条**黑五战舰**.

    判定方式: WG 没有"黑五"标签字段, 用舰名特征识别 — 以 ' B' 结尾的船
    (例: Yoshino B / Iowa B / Massachusetts B). 这是 WG 命名约定。
    """
    if ships_df is None or ships_df.empty or "name" not in ships_df.columns:
        return False
    bf = ships_df[ships_df["name"].astype(str).str.endswith(" B")]
    return len(bf) >= min_count


def _al_collab_count(ships_df, min_count):
    """"天青车道": ≥ N 条**碧蓝航线联动舰**.

    判定方式: 舰名以 'AL ' 开头 (例: AL Shinano / AL Littorio / AL Sovetskaya Rossiya).
    AL = Azur Lane 联动皮肤, WG 的命名约定。
    """
    if ships_df is None or ships_df.empty or "name" not in ships_df.columns:
        return False
    al = ships_df[ships_df["name"].astype(str).str.startswith("AL ")]
    return len(al) >= min_count


CHALLENGES = [
    # === 单场极值类 (依赖 PlayerStats.max_*) ===
    ("摧枯拉朽", "最高单场击杀战舰 ≥ 8",
        "会战兵力是11舰对4舰，优势在我",
        lambda s, df: s.max_frags_battle >= 8),
    ("铁壁防线", "最高单场潜在伤害 ≥ 8000k",
        "这下总算能赢了…吧？",
        lambda s, df: s.max_total_agro >= 8_000_000),
    ("亡牌飞行员", "最高单场击毁飞机 ≥ 60",
        "是谁给你的勇气从空中向我发起进攻",
        lambda s, df: s.max_planes_killed >= 60),
    ("天空之眼", "最高单场侦察舰船 ≥ 18 艘",
        "那年海战，对面只来了6舰，我甩了一下刘海，才发现右边还有12舰",
        lambda s, df: s.max_ships_spotted >= 18),
    ("冒险模式", "最高单场经验 ≥ 6600",
        "其他人都在干什么？",
        lambda s, df: s.max_xp >= 6600),
    ("创造模式", "最高单场伤害 ≥ 400k",
        "你说终于赢了，我说真刷爽了",
        lambda s, df: s.max_damage_dealt >= 400_000),
    ("观察模式", "最高单场侦查伤害 ≥ 400k",
        "我说终于赢了，你说真刷爽了",
        lambda s, df: s.max_damage_scouting >= 400_000),

    # === 单舰特征类 ===
    ("野兽先辈", "任意战舰场均伤害 = 114514",
        "发射出去的不是炮弹，是湿意罢（悲）",
        lambda s, df: _any_ship_avg_damage(df, 114514)),
    ("真爱粉", "任意战舰场次 ≥ 10000",
        "onb真的遇到战舰仙人了",
        lambda s, df: _any_ship_battles_at_least(df, 10000)),

    # === 累积类 ===
    ("真正的和平", "平局累计 ≥ 10 场",
        "20达布隆的活拼什么命啊",
        lambda s, df: s.draws >= 10),
    ("碰碰船", "冲撞击沉累计 ≥ 200 艘",
        "别怕孩子们看我肘他，不耗孩子们我漏水了",
        lambda s, df: s.ramming_frags >= 200),

    # === 单舰高难度类 ===
    ("面壁者", "任意战列舰最高单场侦查伤害 ≥ 200k",
        "我也是计划的一部分？",
        lambda s, df: _any_battleship_max_scouting(df, 200_000)),
    ("CN_70", "任意战斗场次 ≥ 5 场的 T8+ 战舰场均伤害 ≤ 4000",
        "您已被移出小队",
        lambda s, df: _any_high_tier_low_avg_damage(df, 8, 4000)),
    ("PAC", "任意战斗场次 ≥ 5 场的 T6+ 战舰主炮命中率 ≥ 60%",
        "没问题啊，我玩絮库夫和埃尔宾的",
        lambda s, df: _any_ship_main_battery_hit_rate(df, 0.60)),
    ("水雷魂", "任意战斗场次 ≥ 5 场的 T6+ 驱逐舰鱼雷命中率 ≥ 15%",
        "てん こう へい か まん さい!",
        lambda s, df: _any_destroyer_torpedo_hit_rate(df, 0.15)),

    # === 舰队构成类 ===
    ("全家福", "航母 + 潜艇场次占比 ≥ 60%",
        "浮木刚用完，去打会瓦进点货",
        lambda s, df: _cv_sub_share(df, 0.60)),
    ("刮宫圣手", "T6 及以下场次占比 ≥ 30%",
        "太老了，你回去吧",
        lambda s, df: _low_tier_share(df, 6, 0.30)),

    # === 平均量类 ===
    ("舰队防空", "场均击落飞机 ≥ 7",
        "哎我怎么老贝榨啊",
        lambda s, df: (s.avg_planes_killed or 0) >= 7),

    # === 收集类 ===
    ("林翩翩", "拥有 ≥ 15 条黑五战舰",
        "她说是晒黑的",
        lambda s, df: _black_friday_count(df, 15)),
    ("天青车道", "拥有 ≥ 10 条 AL 联动战舰",
        "二次元的钱真好赚",
        lambda s, df: _al_collab_count(df, 10)),

    # === 特殊成就类 (依赖 WG account/achievements 接口) ===
    ("吸鼠坝王", "获得以一当十成就 ≥ 10 次",
        "我草终于重连回来了，唉不是怎么就剩我一个了",
        lambda s, df: (getattr(s, "achievements", None) or {}).get("SOLO_WARRIOR", 0) >= 10),
]


def get_completed_challenges(stats, ships_df):
    """**主入口** — 跑一遍所有挑战, 返回完成列表。

    返回 `[(name, cond_text, desc), ...]` (按 CHALLENGES 原始顺序, 非按"难度"排序)。

    每个 condition lambda 用 try/except 包住, 防止某一个挑战的字段缺失把整个调用炸掉。
    用例: 老 API 数据没 `max_damage_scouting`, 面壁者会抛异常但不影响其他挑战。
    """
    if stats is None:
        return []
    completed = []
    for name, cond_text, desc, condition in CHALLENGES:
        try:
            if condition(stats, ships_df):
                completed.append((name, cond_text, desc))
        except Exception:
            pass  # 字段缺失 / 类型错误等, 跳过该挑战不影响其他
    return completed
