"""F1 WOWS offline scoring. Adapted from wows_analyzer/models.py."""



class PlayerStats:
    """**单一模式的玩家战绩** (例: 玩家在 PVP 的累积数据).

    一个 Player 通常有多个 PlayerStats (每个模式一个), 通过 `mode_stats` dict 索引。
    `@property` 系列从累积字段算 win_rate / avg_damage / kd_ratio 等派生值。

    与 ship-level 数据的关系: PlayerStats 是**账号级总和**, 包含玩家所有船的累计;
    单船详情走 `ships_df` 的某一行。`ships.aggregate_to_stats(ships_df)` 可以
    从 DataFrame 反向构造 PlayerStats (用于筛选后重算总览)。
    """

    def __init__(self, battles, wins, damage_dealt, xp, frags, survived_battles,
                 main_battery_hits, main_battery_shots,
                 torpedoes_hits=0, torpedoes_shots=0,
                 planes_killed=0, damage_scouting=0, ships_spotted=0,
                 art_agro=0, torpedo_agro=0,
                 capture_points=0, dropped_capture_points=0,
                 draws=0, ramming_frags=0,
                 max_damage_dealt=0, max_xp=0, max_frags_battle=0,
                 max_total_agro=0, max_planes_killed=0,
                 max_ships_spotted=0, max_damage_scouting=0):
        """直接赋字段, 无业务逻辑. 调用方一般是 from_mode_dict 工厂. achievements 默认 {} 由外层注入."""
        self.battles = battles
        self.wins = wins
        self.damage_dealt = damage_dealt
        self.xp = xp
        self.frags = frags
        self.survived_battles = survived_battles
        self.main_battery_hits = main_battery_hits
        self.main_battery_shots = main_battery_shots
        self.torpedoes_hits = torpedoes_hits
        self.torpedoes_shots = torpedoes_shots
        self.planes_killed = planes_killed
        self.damage_scouting = damage_scouting
        self.ships_spotted = ships_spotted
        self.art_agro = art_agro
        self.torpedo_agro = torpedo_agro
        self.capture_points = capture_points
        self.dropped_capture_points = dropped_capture_points
        # 终极挑战用字段
        self.draws = draws
        self.ramming_frags = ramming_frags
        self.max_damage_dealt = max_damage_dealt
        self.max_xp = max_xp
        self.max_frags_battle = max_frags_battle
        self.max_total_agro = max_total_agro
        self.max_planes_killed = max_planes_killed
        self.max_ships_spotted = max_ships_spotted
        self.max_damage_scouting = max_damage_scouting
        # 成就字典 (账号级别, 由外层在 from_api 之后注入到 PVP stats)
        self.achievements = {}

    @property
    def win_rate(self):
        """胜率 wins/battles, 0 场次 → 0.0 (避免除零)."""
        return self.wins / self.battles if self.battles else 0.0

    @property
    def avg_damage(self):
        """场均伤害 damage_dealt/battles."""
        return self.damage_dealt / self.battles if self.battles else 0.0

    @property
    def avg_xp(self):
        """场均经验 xp/battles."""
        return self.xp / self.battles if self.battles else 0.0

    @property
    def avg_frags(self):
        """场均击杀 frags/battles."""
        return self.frags / self.battles if self.battles else 0.0

    @property
    def survival_rate(self):
        """生存率 survived_battles/battles (含赢了存活 + 输了存活)."""
        return self.survived_battles / self.battles if self.battles else 0.0

    @property
    def kd_ratio(self):
        """**击杀效率** = 击杀数 / 死亡数.

        死亡数 = 总场次 - 存活场次 (注意: "存活"和"赢"不同, 输了的存活也算).
        从未死过 → 返回总击杀数 (避免除零, 也给极少数情况下"全程不死"的玩家
        一个合理的高值, 而不是 inf / NaN)。
        """
        deaths = self.battles - self.survived_battles
        if deaths <= 0:
            return float(self.frags)
        return self.frags / deaths

    @property
    def main_battery_hit_rate(self):
        """主炮命中率 hits/shots, 0 发 → 0.0 (例: 航母/没主炮的潜艇)."""
        return self.main_battery_hits / self.main_battery_shots if self.main_battery_shots else 0.0

    @property
    def torpedo_hit_rate(self):
        """鱼雷命中率 hits/shots, 0 发 → 0.0 (没鱼雷的战列舰多见)."""
        return self.torpedoes_hits / self.torpedoes_shots if self.torpedoes_shots else 0.0

    @property
    def avg_planes_killed(self):
        """场均击落飞机 (主要是 BB/CL 的防空指标; CV 也会击落对方飞机)."""
        return self.planes_killed / self.battles if self.battles else 0.0

    @property
    def avg_scouting_damage(self):
        """场均侦察伤害 — 被你点亮的敌方船被队友打的伤害 (侦察 DD 的关键指标)."""
        return self.damage_scouting / self.battles if self.battles else 0.0

    @property
    def avg_ships_spotted(self):
        """场均侦察舰船数 (有几艘敌人被你先点亮)."""
        return self.ships_spotted / self.battles if self.battles else 0.0

    @property
    def avg_potential_damage(self):
        """场均抗伤 = 主炮 agro + 鱼雷 agro / battles. 衡量 BB/CA 在拉火力 + 不死的能力."""
        return (self.art_agro + self.torpedo_agro) / self.battles if self.battles else 0.0

    @property
    def avg_capture_points(self):
        """场均占领分 — 占点贡献 (DD 的关键指标)."""
        return self.capture_points / self.battles if self.battles else 0.0

    @property
    def avg_dropped_capture_points(self):
        """场均防御分 — 阻止对方占点 (踢出占领圈)."""
        return self.dropped_capture_points / self.battles if self.battles else 0.0

    @classmethod
    def from_mode_dict(cls, mode_dict):
        """从 WG API 的 `statistics.<mode>` 字段构造 PlayerStats.

        mode_dict 是 `info["statistics"]["pvp"]` 这样的子节点。
        若 None 或 battles=0 → 返回 None (该模式没打过 → 玩家页该模式 tab 不显示)。
        """
        if not mode_dict or not mode_dict.get("battles"):
            return None
        mb = mode_dict.get("main_battery") or {}
        torp = mode_dict.get("torpedoes") or {}
        ramming = mode_dict.get("ramming") or {}
        return cls(
            battles=mode_dict.get("battles", 0),
            wins=mode_dict.get("wins", 0),
            damage_dealt=mode_dict.get("damage_dealt", 0),
            xp=mode_dict.get("xp", 0),
            frags=mode_dict.get("frags", 0),
            survived_battles=mode_dict.get("survived_battles", 0),
            main_battery_hits=mb.get("hits", 0),
            main_battery_shots=mb.get("shots", 0),
            torpedoes_hits=torp.get("hits", 0),
            torpedoes_shots=torp.get("shots", 0),
            planes_killed=mode_dict.get("planes_killed", 0),
            damage_scouting=mode_dict.get("damage_scouting", 0),
            ships_spotted=mode_dict.get("ships_spotted", 0),
            art_agro=mode_dict.get("art_agro", 0),
            torpedo_agro=mode_dict.get("torpedo_agro", 0),
            capture_points=mode_dict.get("capture_points", 0),
            dropped_capture_points=mode_dict.get("dropped_capture_points", 0),
            draws=mode_dict.get("draws", 0),
            ramming_frags=ramming.get("frags", 0),
            max_damage_dealt=mode_dict.get("max_damage_dealt", 0),
            max_xp=mode_dict.get("max_xp", 0),
            max_frags_battle=mode_dict.get("max_frags_battle", 0),
            max_total_agro=mode_dict.get("max_total_agro", 0),
            max_planes_killed=mode_dict.get("max_planes_killed", 0),
            max_ships_spotted=mode_dict.get("max_ships_spotted", 0),
            max_damage_scouting=mode_dict.get("max_damage_scouting", 0),
        )
