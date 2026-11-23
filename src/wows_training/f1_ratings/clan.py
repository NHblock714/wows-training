"""F1 WOWS offline scoring. Adapted from wows_analyzer/clan.py."""
from .analysis import score_value


ROLE_ZH = {
    "commander": "会长",
    "executive_officer": "副会长",
    "recruitment_officer": "招募官",
    "officer": "官员",
    "recruit": "成员",
    "private": "成员",
}


class ClanMember:
    """**工会成员** — 简化的 PlayerStats 子集, 只保留工会页用得到的字段。

    `hidden=True` 表示:
    - 该玩家在 WG 设置中勾选了"隐藏战绩", 或
    - WG API 没返回此 account_id 的数据
    隐藏成员的所有数值字段都是 0, 不应参与聚合 (`active_members` 过滤掉)。
    """

    def __init__(self, account_id, nickname, role, hidden=False,
                 battles=0, wins=0, damage_dealt=0, xp=0, frags=0,
                 survived_battles=0,
                 main_battery_hits=0, main_battery_shots=0,
                 art_agro=0, torpedo_agro=0,
                 damage_scouting=0, planes_killed=0, ships_spotted=0):
        """直接赋字段, 无业务逻辑. 工厂在 build_clan_data 里."""
        self.account_id = account_id
        self.nickname = nickname
        self.role = role
        self.hidden = hidden
        self.battles = battles
        self.wins = wins
        self.damage_dealt = damage_dealt
        self.xp = xp
        self.frags = frags
        self.survived_battles = survived_battles
        self.main_battery_hits = main_battery_hits
        self.main_battery_shots = main_battery_shots
        self.art_agro = art_agro
        self.torpedo_agro = torpedo_agro
        self.damage_scouting = damage_scouting
        self.planes_killed = planes_killed
        self.ships_spotted = ships_spotted

    @property
    def win_rate(self):
        """胜率 wins/battles."""
        return self.wins / self.battles if self.battles else 0.0

    @property
    def avg_damage(self):
        """场均伤害."""
        return self.damage_dealt / self.battles if self.battles else 0.0

    @property
    def avg_xp(self):
        """场均经验."""
        return self.xp / self.battles if self.battles else 0.0

    @property
    def avg_frags(self):
        """场均击杀."""
        return self.frags / self.battles if self.battles else 0.0

    @property
    def survival_rate(self):
        """生存率."""
        return self.survived_battles / self.battles if self.battles else 0.0

    @property
    def main_battery_hit_rate(self):
        """主炮命中率."""
        return self.main_battery_hits / self.main_battery_shots if self.main_battery_shots else 0.0

    @property
    def avg_potential_damage(self):
        """场均抗伤 = (主炮 agro + 鱼雷 agro) / battles."""
        return (self.art_agro + self.torpedo_agro) / self.battles if self.battles else 0.0

    @property
    def avg_scouting_damage(self):
        """场均侦察伤害."""
        return self.damage_scouting / self.battles if self.battles else 0.0

    @property
    def kd_ratio(self):
        """击杀效率 = 击杀 / 死亡数. 从未死 → 返回 frags (避免除零)."""
        deaths = self.battles - self.survived_battles
        if deaths <= 0:
            return float(self.frags)
        return self.frags / deaths


class ClanData:
    """**工会聚合容器**. 所有 avg_* 属性都用 `_mean` 等权重平均, **不按场次加权**。

    设计意图: 防止 10000 场的"巨人玩家"压住整个工会画像。每个活跃成员等权重,
    更符合"工会代表了什么样的玩家群体"的直觉。

    `total_*` 系列是累计求和, 用于显示 "总场次 X 万" 这种汇总数字。
    `avg_*` 用 _mean 算每人均值 (而不是 total_x / total_battles)。
    """

    def __init__(self, clan_id, tag, name, motto, members):
        """Args: clan_id / tag / name / motto / members (list[ClanMember])."""
        self.clan_id = clan_id
        self.tag = tag
        self.name = name
        self.motto = motto
        self.members = members  # list[ClanMember]

    @property
    def active_members(self):
        """活跃成员 = 非隐藏 + 至少 1 场战斗。聚合都基于这个子集。"""
        return [m for m in self.members if not m.hidden and m.battles > 0]

    @property
    def total_battles(self):
        """活跃成员场次累计 (用于显示 "总场次 X 万")."""
        return sum(m.battles for m in self.active_members)

    @property
    def total_wins(self):
        """活跃成员胜场累计."""
        return sum(m.wins for m in self.active_members)

    @property
    def total_damage(self):
        """活跃成员总伤害."""
        return sum(m.damage_dealt for m in self.active_members)

    @property
    def total_xp(self):
        """活跃成员总经验."""
        return sum(m.xp for m in self.active_members)

    @property
    def total_frags(self):
        """活跃成员总击杀."""
        return sum(m.frags for m in self.active_members)

    @property
    def total_survived(self):
        """活跃成员总存活场次."""
        return sum(m.survived_battles for m in self.active_members)

    @property
    def total_mb_hits(self):
        """活跃成员主炮总命中."""
        return sum(m.main_battery_hits for m in self.active_members)

    @property
    def total_mb_shots(self):
        """活跃成员主炮总射击."""
        return sum(m.main_battery_shots for m in self.active_members)

    @property
    def total_potential(self):
        """活跃成员主炮+鱼雷 agro 累计."""
        return sum(m.art_agro + m.torpedo_agro for m in self.active_members)

    @property
    def total_scouting(self):
        """活跃成员侦察伤害累计."""
        return sum(m.damage_scouting for m in self.active_members)

    def _mean(self, value_fn):
        """**核心聚合函数** — 活跃成员等权重平均 (不按场次加权).

        例: avg_damage = (m1.avg_damage + m2.avg_damage + ...) / N_active
        而**不是** total_damage / total_battles (那种是场次加权).

        这是与移动端 `clan_scores.dart::_mean` 对齐的关键. 改一边必须改另一边。
        """
        active = self.active_members
        if not active:
            return 0.0
        return sum(value_fn(m) for m in active) / len(active)

    @property
    def avg_win_rate(self):
        """活跃成员胜率等权重平均 (注: 不是 total_wins/total_battles)."""
        return self._mean(lambda m: m.win_rate)

    @property
    def avg_damage(self):
        """活跃成员场均伤害等权重平均."""
        return self._mean(lambda m: m.avg_damage)

    @property
    def avg_xp(self):
        """活跃成员场均经验等权重平均."""
        return self._mean(lambda m: m.avg_xp)

    @property
    def avg_frags(self):
        """活跃成员场均击杀等权重平均."""
        return self._mean(lambda m: m.avg_frags)

    @property
    def avg_survival(self):
        """活跃成员生存率等权重平均."""
        return self._mean(lambda m: m.survival_rate)

    @property
    def avg_potential_damage(self):
        """活跃成员场均抗伤等权重平均."""
        return self._mean(lambda m: m.avg_potential_damage)

    @property
    def avg_scouting_damage(self):
        """活跃成员场均侦察伤害等权重平均."""
        return self._mean(lambda m: m.avg_scouting_damage)

    @property
    def main_battery_hit_rate(self):
        """活跃成员主炮命中率等权重平均."""
        return self._mean(lambda m: m.main_battery_hit_rate)

    @property
    def kd_ratio(self):
        """活跃成员击杀效率等权重平均."""
        return self._mean(lambda m: m.kd_ratio)

    @property
    def hidden_horse_ratio(self):
        """**"隐藏🐴" 比例** — 隐藏成员数 / 总成员数. 工会"透明度"指标.

        分母是**全体成员** (包括隐藏), 不是 active_members. 评分时反向 (越透明越高)。
        """
        if not self.members:
            return 0.0
        return sum(1 for m in self.members if m.hidden) / len(self.members)

    @property
    def avg_battles_per_member(self):
        """**活跃成员人均场次** — 老兵工会的活跃度指标.

        雷达轴 "平均场次" 用这个值打分. base 阈值 [1000, 3000, 5000, 7500, 10000]
        意味着 10000 场/人才能 S, 反映"工会成员普遍是老兵"。
        """
        n = len(self.active_members)
        return self.total_battles / n if n else 0.0


def compute_clan_radar_scores(clan, benchmarks):
    """**工会雷达 (不筛选) 9 轴评分** — 直接用 base 阈值, 不做 class/tier 修正.

    9 轴: 胜率 / 场均伤害 / 场均经验 / 击杀效率 / 主炮命中 / 场均抗伤 / 场均侦查
          / 平均场次 / 隐藏🐴

    > 为什么不做 class/tier 修正? 因为成员舰种构成混杂, 没有统一的修正基准。
    > 用 base 阈值算的是"假设是混合舰队的平均水平", 横向粗对比工会强弱够用。
    > 想要更精准 → 用户切换筛选 → 走 `compute_clan_radar_scores_filtered`。

    返回 `{显示名: (raw, 0-130 分)}`.
    """
    if clan.total_battles == 0:
        return {}
    base = benchmarks.get("base", {})
    scores = {}

    def add(label, key, value):
        """便捷闭包: 用 base[key] 阈值给 value 打分, 存到 scores 字典."""
        thresholds = base.get(key)
        if not thresholds:
            return
        scores[label] = (value, score_value(value, thresholds))

    add("胜率", "win_rate", clan.avg_win_rate)
    add("场均伤害", "avg_damage", clan.avg_damage)
    add("场均经验", "avg_xp", clan.avg_xp)
    add("击杀效率", "kd_ratio", clan.kd_ratio)
    add("主炮命中", "main_battery_hit_rate", clan.main_battery_hit_rate)
    add("场均抗伤", "avg_potential_damage", clan.avg_potential_damage)
    add("场均侦查", "avg_scouting_damage", clan.avg_scouting_damage)

    # 平均场次: 活跃度指标 (S 阈值 = 10000 场/人)
    avg_b = clan.avg_battles_per_member
    avg_b_score = score_value(avg_b, [1000.0, 3000.0, 5000.0, 7500.0, 10000.0])
    scores["平均场次"] = (avg_b, avg_b_score)

    # 隐藏🐴比例
    hidden = clan.hidden_horse_ratio
    visible = 1 - hidden
    hidden_score = score_value(visible, [0.60, 0.75, 0.85, 0.92, 0.97])
    scores["隐藏🐴"] = (hidden, hidden_score)
    return scores


def compute_clan_fleet_power(clan, benchmarks):
    """**工会综合 PR-like** (0~2500). 雷达 9 轴平均分 → 重新映射到 PR 区间.

        FLEET_POWER = (Σ scores) / N / 130 × 2500

    映射到 0~2500 是为了和玩家 PR (700·n_dmg + 300·n_frags + 150·n_wr) 同尺度,
    可以丢给 `compute_battle_power(pr)` 走同一个 sigmoid 出战力数字。
    """
    s = compute_clan_radar_scores(clan, benchmarks)
    if not s:
        return 0.0
    avg = sum(v[1] for v in s.values()) / len(s)
    return avg / 130.0 * 2500.0


def compute_clan_radar_scores_filtered(clan, member_dfs, benchmarks,
                                       filter_class=None, filter_tier=None):
    """**工会雷达 (筛选模式)** — 按所选 舰种/等级 重新聚合 + 修正阈值.

    流程:
    1. 每个活跃成员从 `member_dfs[aid]` 取出自己的船仓 (懒加载, 见 LoadClanShipsTask)
    2. 按 filter_class / filter_tier 过滤到子集
    3. 子集内 sum 聚合 → 得到该成员在筛选范围的统计 (win_rate, avg_damage, ...)
    4. 跨成员等权重平均 (与不筛选时同理)
    5. **阈值 = base × class_factor[filter_class] × tier_factor[filter_tier]**
       - class_factor=null → 跳过该指标 (例: CV/Sub 不算主炮命中)
       - tier_factor 只对 avg_damage/avg_xp/agro/scouting 有效, 其他指标 cf 决定

    平均场次轴**额外缩放阈值** (筛选后场次自然变少, 不缩放会全员 D 级):
        都选 → 1/15, 仅类 → 1/4, 仅级 → 1/3, 不选 → 1.0
    (经验值: T10 占总约 30-35%, 驱逐占约 25%, T10 驱逐约 6-10%)

    **隐藏🐴 轴在筛选时不显示** — 与筛选无关, 是工会整体属性。
    """
    per_member = []
    for m in clan.active_members:
        df = member_dfs.get(m.account_id)
        if df is None or df.empty:
            continue
        sub = df
        if filter_class is not None:
            sub = sub[sub["type"] == filter_class]
        if filter_tier is not None:
            sub = sub[sub["tier"] == filter_tier]
        if sub.empty:
            continue
        b = int(sub["battles"].sum())
        if b == 0:
            continue
        w = int(sub["wins"].sum())
        d = int(sub["damage_dealt"].sum())
        x = int(sub["xp"].sum())
        f = int(sub["frags"].sum())
        sv = int(sub["survived_battles"].sum())
        mb_h = int(sub["main_battery_hits"].sum())
        mb_s = int(sub["main_battery_shots"].sum())
        pot = int(sub["art_agro"].sum()) + int(sub["torpedo_agro"].sum())
        sc = int(sub["damage_scouting"].sum())
        deaths = b - sv
        per_member.append({
            "battles": b,
            "win_rate": w / b,
            "avg_damage": d / b,
            "avg_xp": x / b,
            "avg_frags": f / b,
            "kd_ratio": float(f) if deaths <= 0 else f / deaths,
            "main_battery_hit_rate": (mb_h / mb_s) if mb_s else 0.0,
            "avg_potential_damage": pot / b,
            "avg_scouting_damage": sc / b,
        })

    if not per_member:
        return {}

    def mean(k):
        """跨成员等权重平均 — 每个活跃成员对工会形象贡献相同, 不按场次加权."""
        return sum(m[k] for m in per_member) / len(per_member)

    base = (benchmarks or {}).get("base", {})
    class_factors = (benchmarks or {}).get("class_factors", {})
    tier_factors = (benchmarks or {}).get("tier_factors", {})
    cls_factor_map = class_factors.get(filter_class, {}) if filter_class else {}
    tier_key = str(int(filter_tier)) if filter_tier else None

    scores = {}

    def add(label, key, val):
        """便捷闭包: 阈值按 class × tier 系数缩放后, 给 val 打分存入 scores."""
        b = base.get(key)
        if not b:
            return
        cf = cls_factor_map.get(key, 1.0) if filter_class else 1.0
        if cf is None:
            return  # 该舰种不适用 (例: CV/Sub 不算主炮命中)
        tf = tier_factors.get(key, {}).get(tier_key, 1.0) if tier_key else 1.0
        thresholds = [v * cf * tf for v in b]
        scores[label] = (val, score_value(val, thresholds))

    add("胜率", "win_rate", mean("win_rate"))
    add("场均伤害", "avg_damage", mean("avg_damage"))
    add("场均经验", "avg_xp", mean("avg_xp"))
    add("击杀效率", "kd_ratio", mean("kd_ratio"))
    add("主炮命中", "main_battery_hit_rate", mean("main_battery_hit_rate"))
    add("场均抗伤", "avg_potential_damage", mean("avg_potential_damage"))
    add("场均侦查", "avg_scouting_damage", mean("avg_scouting_damage"))

    # 平均场次阈值按筛选范围缩放: 选了类/级后场次自然变少, 不缩放会全员 D.
    # 经验值: T10 占总约 30-35%, 驱逐占约 25%, T10 驱逐约 6-10%.
    if filter_class and filter_tier:
        b_scale = 1 / 15.0
    elif filter_class:
        b_scale = 1 / 4.0
    elif filter_tier:
        b_scale = 1 / 3.0
    else:
        b_scale = 1.0
    b_thresholds = [v * b_scale for v in [1000.0, 3000.0, 5000.0, 7500.0, 10000.0]]
    avg_b = mean("battles")
    scores["平均场次"] = (avg_b, score_value(avg_b, b_thresholds))

    # 隐藏🐴 是工会整体指标, 跟筛选范围无关, 筛选时不显示.
    return scores


def compute_member_filtered_score(member_df, benchmarks,
                                  filter_class=None, filter_tier=None):
    """**单成员在筛选范围内的综合分** — 与工会雷达**同一套阈值**和算法.

    流程: 过滤 → 聚合 → 各指标对修正阈值打分 → 求平均.
    用于工会页成员列表筛选时显示的 S/A/B/C/D 评级 + 0-130 分.

    返回 `{battles, win_rate, score}` 或 `None` (该成员在此范围内 0 场).

    > 阈值与 compute_clan_radar_scores_filtered 完全一致, 保证"成员个体分"
    > 和"工会均值分"用同一尺度可比较。
    """
    if member_df is None or member_df.empty:
        return None
    sub = member_df
    if filter_class is not None:
        sub = sub[sub["type"] == filter_class]
    if filter_tier is not None:
        sub = sub[sub["tier"] == filter_tier]
    if sub.empty:
        return None
    b = int(sub["battles"].sum())
    if b == 0:
        return None
    w = int(sub["wins"].sum())
    d = int(sub["damage_dealt"].sum())
    x = int(sub["xp"].sum())
    f = int(sub["frags"].sum())
    sv = int(sub["survived_battles"].sum())
    mb_h = int(sub["main_battery_hits"].sum())
    mb_s = int(sub["main_battery_shots"].sum())
    pot = int(sub["art_agro"].sum()) + int(sub["torpedo_agro"].sum())
    sc = int(sub["damage_scouting"].sum())
    deaths = b - sv
    metrics = {
        "win_rate": w / b,
        "avg_damage": d / b,
        "avg_xp": x / b,
        "kd_ratio": float(f) if deaths <= 0 else f / deaths,
        "main_battery_hit_rate": (mb_h / mb_s) if mb_s else 0.0,
        "avg_potential_damage": pot / b,
        "avg_scouting_damage": sc / b,
    }

    base = (benchmarks or {}).get("base", {})
    class_factors = (benchmarks or {}).get("class_factors", {})
    tier_factors = (benchmarks or {}).get("tier_factors", {})
    cls_map = class_factors.get(filter_class, {}) if filter_class else {}
    tier_key = str(int(filter_tier)) if filter_tier else None

    sc_list = []
    for key, val in metrics.items():
        t = base.get(key)
        if not t:
            continue
        cf = cls_map.get(key, 1.0) if filter_class else 1.0
        if cf is None:
            continue  # 该舰种该指标不适用
        tf = tier_factors.get(key, {}).get(tier_key, 1.0) if tier_key else 1.0
        thresholds = [v * cf * tf for v in t]
        sc_list.append(score_value(val, thresholds))

    avg_score = sum(sc_list) / len(sc_list) if sc_list else 0.0
    return {"battles": b, "win_rate": w / b, "score": avg_score}


def compute_clan_fleet_power_from_scores(scores):
    """筛选模式下的 FLEET POWER, 输入已经计算好的 scores 字典.

    和 `compute_clan_fleet_power` 同样的映射规则 (avg/130 × 2500),
    只是数据源不同 (筛选 vs 总览)。
    """
    if not scores:
        return 0.0
    avg = sum(v[1] for v in scores.values()) / len(scores)
    return avg / 130.0 * 2500.0


def build_clan_data(clan_info, batch_players):
    """**从 WG API 返回构建 ClanData** — 数据组装入口.

    输入:
    - `clan_info`: `/wows/clans/info` 返回的工会详情, 含 `members: {aid: {role}}`
    - `batch_players`: `/wows/account/info` 批量返回, `{aid_str: {nickname, statistics: {pvp: ...}}}`

    每个成员的判定:
    1. batch_players 没这个 aid → 标记 hidden, nickname=`id={aid}`
    2. `hidden_profile=True` 或 `statistics=null` → 标记 hidden, 但保留 nickname
    3. 否则 → 正常 ClanMember, 提取 pvp 战绩字段

    > batch_players 用 str key (因为 API 返回时 JSON object 的 key 总是字符串).
    """
    if not clan_info:
        return None
    clan_id = clan_info.get("clan_id")
    tag = clan_info.get("tag") or ""
    name = clan_info.get("name") or ""
    motto = clan_info.get("motto")
    members_map = clan_info.get("members") or {}

    members = []
    for aid_str, minfo in members_map.items():
        try:
            aid = int(aid_str)
        except (TypeError, ValueError):
            continue
        role = (minfo or {}).get("role") or ""
        pinfo = batch_players.get(str(aid))
        if pinfo is None:
            members.append(ClanMember(aid, f"id={aid}", role, hidden=True))
            continue
        nickname = pinfo.get("nickname") or f"id={aid}"
        hidden = pinfo.get("hidden_profile") or pinfo.get("statistics") is None
        if hidden:
            members.append(ClanMember(aid, nickname, role, hidden=True))
            continue
        pvp = (pinfo.get("statistics") or {}).get("pvp") or {}
        mb = pvp.get("main_battery") or {}
        members.append(ClanMember(
            account_id=aid,
            nickname=nickname,
            role=role,
            hidden=False,
            battles=pvp.get("battles", 0),
            wins=pvp.get("wins", 0),
            damage_dealt=pvp.get("damage_dealt", 0),
            xp=pvp.get("xp", 0),
            frags=pvp.get("frags", 0),
            survived_battles=pvp.get("survived_battles", 0),
            main_battery_hits=mb.get("hits", 0),
            main_battery_shots=mb.get("shots", 0),
            art_agro=pvp.get("art_agro", 0),
            torpedo_agro=pvp.get("torpedo_agro", 0),
            damage_scouting=pvp.get("damage_scouting", 0),
            planes_killed=pvp.get("planes_killed", 0),
            ships_spotted=pvp.get("ships_spotted", 0),
        ))
    return ClanData(clan_id, tag, name, motto, members)
