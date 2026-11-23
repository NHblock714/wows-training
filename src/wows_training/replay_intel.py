"""Shared, time-bounded replay facts for map layers and tactical inspection.

Recorded events are kept separate from optional position animation. An event's
existence proves a client record, not a hit, a target's identity or player intent.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math

from .contracts import parse_version
from .timebase import packet_to_battle_time


CAPABILITIES = ("heading", "health", "visibility", "shells", "torpedoes", "smokes", "consumables", "ranges", "planes")
FRESHNESS = 3.0


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def point(value):
    return isinstance(value, (tuple, list)) and len(value) == 2 and all(number(v) for v in value)


def _ordered(rows, key="clock"):
    return sorted((dict(row) for row in rows or [] if isinstance(row, dict) and number(row.get(key))), key=lambda row: row[key])


def _latest(rows, clock, key="clock"):
    index = bisect_right(rows, clock, key=lambda row: row[key]) - 1
    return rows[index] if index >= 0 else None


class ReplayIntel:
    """One selected recording only; no observation or event fusion."""

    def __init__(self, report, ship_catalog=None):
        self.report = report
        catalog = ship_catalog or {}
        self.catalog = catalog.get("ships", catalog)
        self.roster = {int(row["shipId"]): row for row in report.get("roster", []) if type(row.get("shipId")) is int}
        self.death_clocks={}
        for death in report.get('death_events',[]):
            entity=death.get('victim_entity_id');clock=death.get('clock')
            if type(entity) is int and number(clock) and clock>=0:self.death_clocks[entity]=min(clock,self.death_clocks.get(entity,clock))
        self.end_clock = (report.get("packet_clock_range") or {}).get("max")
        self.capabilities = dict.fromkeys(CAPABILITIES, False)
        combat = report.get("combat") or {}
        try:
            version = report.get("replay_version")
            from .ordinary_replay import current_combat_verified
            valid = ((isinstance(version, str) and parse_version(version) == (15, 9, 0, 13357625)
                      and combat.get("schema_version") == 1) or current_combat_verified(report))
        except ValueError:
            valid = False
        self.available = valid
        self.combat = combat if valid else {}
        self.ships = {}
        for key, ship in self.combat.get("ships", {}).items():
            try:
                entity = int(key)
            except (ValueError, TypeError):
                continue
            self.ships[entity] = dict(ship, samples=_ordered(ship.get("samples")))
        self.shots = _ordered(self.combat.get("shots"), "start_clock")
        self.torpedoes = _ordered(self.combat.get("torpedoes"), "start_clock")
        self.consumables = _ordered(self.combat.get("consumables"), "start_clock")
        self.smoke_entities = [dict(row, samples=_ordered(row.get("samples"))) for row in self.combat.get("smokes", [])]
        self.planes=[dict(row,samples=_ordered(row.get('samples'))) for row in self.combat.get('planes',[]) if number(row.get('start_clock'))] if self.combat.get('plane_observation_schema')==1 else []
        self._consumables_by_ship = defaultdict(list)
        for record in self.consumables:
            self._consumables_by_ship[record.get("entity_id")].append(record)
        self._shot_ends = _ordered([dict(row, clock=row["end_clock"]) for row in self.shots
                                   if number(row.get("end_clock")) and point(row.get("end"))])
        self._projectile_windows = {}
        for kind, rows in (("shell", self.shots), ("torpedo", self.torpedoes)):
            maximum = 1.5
            for row in rows:
                row["_ordered_samples"] = _ordered(row.get("samples"))
                end = row.get("end_clock")
                if number(end):
                    maximum = max(maximum, end + 0.7 - row["start_clock"])
                if row["_ordered_samples"]:
                    maximum = max(maximum, row["_ordered_samples"][-1]["clock"] + FRESHNESS - row["start_clock"])
            self._projectile_windows[kind] = maximum
        self.capabilities.update(
            health=any(number(sample.get("hp")) for ship in self.ships.values() for sample in ship["samples"]),
            heading=any(number(sample.get("heading_radians")) and sample.get("heading_convention") == "east_ccw"
                        for ship in self.ships.values() for sample in ship["samples"]),
            visibility=any(type(sample.get("visible_to_recorder")) is bool for ship in self.ships.values() for sample in ship["samples"]),
            shells=bool(self.shots), torpedoes=bool(self.torpedoes), smokes=bool(self.smoke_entities),
            consumables=bool(self.consumables),
            planes=bool(self.planes),
            ranges=any(number(row.get("range_engine")) and row["range_engine"] > 0 for row in self.consumables),
        )
        self.limitations = [
            "仅代表当前录制视角；在本视角可见与被敌方点亮分别记录。",
            "消耗品未收到记录时为未知；结束倒计时来自客户端报告，不是剩余次数或冷却推断。",
            "炮弹目标坐标不是命中点；终止记录也不等同于命中舰船。",
            "可选轨迹动画使用记录端点插值，不能用于精确命中、速度或规避结论。",
        ]
        if report.get('multiview'):
            self.limitations[0] = '双边记录按比赛计时锚点对齐；融合可见不等于任一方实时视野。事件显示时刻为观测区间上界，原来源与时间区间单独保留。'
        if not valid:
            self.limitations.insert(0, "该回放尚无增强战斗数据，请从原始文件更新复盘数据。")
        self.events = self._build_events()
        self._event_clocks = [event["clock"] for event in self.events]
        self._position_tracks = {}
        geometry = report.get("geometry_analysis") or {}
        projection = geometry.get("png_projection") or {}
        viewport = projection.get("viewport_bounds_engine") or {}
        self.geometry_verified = (valid and geometry.get("png_alignment_verified") is True and not geometry.get("errors")
                                  and projection.get("status") == "source_chain_verified"
                                  and projection.get("mapping_scope") == "single_identity_mapping"
                                  and viewport.get("projection_convention") == "SpaceSizeUtils.native_max_min_return"
                                  and viewport.get("native_viewport_equality_verified") is True)
        if self.geometry_verified:
            for track in geometry.get("projected_tracks", []):
                columns = track.get("columns") or []
                if all(columns.count(key) == 1 for key in ("packet_clock", "x_engine", "z_engine")):
                    indices = [columns.index(key) for key in ("packet_clock", "x_engine", "z_engine")]
                    segments = []
                    for raw in track.get("segments") or []:
                        segment = [tuple(row[i] for i in indices) for row in raw if len(row) > max(indices) and all(number(row[i]) for i in indices)]
                        if segment and all(a[0] <= b[0] for a, b in zip(segment, segment[1:])):
                            segments.append(segment)
                    self._position_tracks[track.get("entity_id")] = segments

    def name(self, entity):
        return self.roster.get(entity, {}).get("name") or (f"实体 {entity}" if entity is not None else "来源未确认")

    def ship_state(self, entity_id, clock):
        roster = self.roster.get(entity_id, {})
        ship = self.ships.get(entity_id, {})
        state = _latest(ship.get("samples", []), clock)
        result = {"entity_id": entity_id, "heading_radians": None, "hp": None,
                  "max_hp": ship.get("max_hp", roster.get("maxHealth")), "health_clock": None,
                  "is_alive": None, "visibility": "unknown", "visibility_label": "观测状态未知",
                  "spotted_by_enemy": None, "spotted_label": "敌方点亮状态未知", "consumables": []}
        if state is not None:
            for key in ("hp", "hp_clock", "is_alive", "visibility_flags_raw", "spotted_by_enemy", "spotted_clock"):
                if key in state:
                    result[{"hp_clock": "health_clock"}.get(key, key)] = state[key]
            if (not number(result["hp"]) or result["hp"] < 0 or not number(result.get("health_clock"))
                    or not 0 <= result["health_clock"] <= clock):
                result["hp"] = None
                result["health_clock"] = None
            result["observation_clock"] = state["clock"]
            heading = state.get("heading_radians")
            if (number(heading) and state.get("heading_convention") == "east_ccw"
                    and number(state.get("heading_clock")) and 0 <= state["heading_clock"] <= clock):
                result["heading_radians"] = heading
                result["heading_clock"] = state.get("heading_clock")
            visible, stamp = state.get("visible_to_recorder"), state.get("visibility_clock")
            if visible is False and number(stamp) and stamp <= clock:
                result.update(visibility="not_visible", visibility_label="本视角已失去观测", visibility_clock=stamp)
            elif visible is True and number(stamp) and 0 <= clock - stamp < FRESHNESS:
                result.update(visibility="visible", visibility_label="本视角可见", visibility_clock=stamp)
            elif visible is True and number(stamp) and stamp <= clock:
                result["visibility_label"] = "观测更新已过期"
            if not number(state.get("spotted_clock")) or not 0 <= state["spotted_clock"] <= clock:
                result["spotted_by_enemy"] = None
                result["spotted_clock"] = None
            if type(result.get("spotted_by_enemy")) is bool:
                result["spotted_label"] = "敌方点亮：是（记录）" if result["spotted_by_enemy"] else "敌方点亮：否（记录）"
                if result["visibility"] != "visible":
                    result["spotted_label"] = result["spotted_label"].replace("（记录）", "（上次记录，当前观测不足）")
        if not number(result["max_hp"]) or result["max_hp"] <= 0:
            result["max_hp"] = None
        for activation in self._consumables_by_ship.get(entity_id, []):
            start = activation["start_clock"]
            if start > clock:
                break
            end = activation.get("end_clock")
            expected = activation.get("expected_end_clock")
            if number(end) and clock >= end:
                continue
            # A later interruption cannot shorten the timer at an earlier query.
            expiry = expected if number(expected) else None
            if expiry is not None and clock >= expiry:
                continue
            if expiry is None and clock - start > FRESHNESS:
                continue
            result["consumables"].append({
                "key": activation.get("id"), "label": activation.get("label") or "未识别消耗品",
                "type": activation.get("type"), "start_clock": start, "end_clock": expiry,
                "remaining_seconds": max(0, expiry - clock) if expiry is not None else None,
                "radius_engine": activation.get("range_engine"), "quality": "reported_timer" if expiry is not None else "activation_only",
                "source": activation.get("source"),
            })
        if entity_id in self.death_clocks and clock>=self.death_clocks[entity_id]:
            result.update(is_alive=False,death_clock=self.death_clocks[entity_id],visibility='not_visible',
                visibility_label='已沉没；以下为击沉前最后记录',consumables=[],spotted_by_enemy=None,
                spotted_label='已沉没；点亮状态不再适用')
        if self.report.get('multiview'):
            result['visibility_label'] = ('融合记录有位置' if result['visibility'] == 'visible' else '此时融合位置未知') + ' · 非单方视野'
            if result.get('death_clock') is not None:result['visibility_label']='已沉没；以下为击沉前最后记录 · 非单方视野'
            if state is not None:
                for key in ('source_sha', 'source_original_packet_clock', 'alignment_provenance', 'visibility_by_source', 'hp_source', 'spotted_source', 'alive_source'):
                    result[key] = deepcopy(state.get(key))
        return result

    def _projectile(self, record, kind, clock):
        start, finish = record.get("start"), record.get("end")
        began, ended = record["start_clock"], record.get("end_clock")
        if not point(start) or clock < began:
            return None
        source = record.get("source")
        base = {"id": record.get("id"), "kind": kind, "owner_entity_id": record.get("owner_entity_id"),
                "team_id": self.roster.get(record.get("owner_entity_id"), {}).get("teamId"), "source": source,
                "params_id":record.get('params_id'),"direction_raw":record.get('direction_raw'),
                "tail_x_engine": None, "tail_z_engine": None, "quality": "recorded"}
        if number(ended) and clock > ended + 0.7:
            return None
        if number(ended) and clock >= ended:
            return dict(base, x_engine=finish[0], z_engine=finish[1], phase="recorded_end", observation_clock=ended,
                        source=record.get("end_source") or source) if point(finish) else None
        observed = _latest(record.get("_ordered_samples", []), clock)
        if observed and point(observed.get("position")) and 0 <= clock - observed["clock"] < FRESHNESS:
            return dict(base, x_engine=observed["position"][0], z_engine=observed["position"][1], phase="recorded_position",
                        observation_clock=observed["clock"], source=observed.get("source") or source)
        if number(ended) and point(finish) and ended > began and not self.report.get('multiview'):
            duration = ended - began
            fraction, tail = (clock - began) / duration, max(0, (clock - began - 0.25) / duration)
            return dict(base, x_engine=start[0] + (finish[0] - start[0]) * fraction,
                        z_engine=start[1] + (finish[1] - start[1]) * fraction,
                        tail_x_engine=start[0] + (finish[0] - start[0]) * tail,
                        tail_z_engine=start[1] + (finish[1] - start[1]) * tail,
                        quality="estimated", phase="recorded_endpoints_animation",
                        interpolation_note="记录发射与终止端点间的线性动画，不是逐帧实测位置")
        # An unconfirmed target/velocity never becomes an impact or a flight path.
        if clock - began <= 1.5:
            return dict(base, x_engine=start[0], z_engine=start[1], phase="recorded_launch", observation_clock=began)
        return None

    def projectiles(self, clock):
        result = []
        for kind, records in (("shell", self.shots), ("torpedo", self.torpedoes)):
            begin = bisect_right(records, clock - self._projectile_windows[kind] - 1e-9, key=lambda row: row["start_clock"])
            finish = bisect_right(records, clock, key=lambda row: row["start_clock"])
            for record in records[begin:finish]:
                event = self._projectile(record, kind, clock)
                if event:
                    result.append(event)
        return result

    def smokes(self, clock):
        result = []
        for smoke in self.smoke_entities:
            if number(smoke.get("end_clock")) and clock >= smoke["end_clock"]:
                continue
            state = _latest(smoke["samples"], clock)
            if state is None:
                continue
            radius = state.get("radius_engine")
            for position in state.get("points", []):
                if point(position):
                    result.append({"entity_id": smoke["entity_id"], "x_engine": position[0], "z_engine": position[1],
                                   "radius_engine": radius if number(radius) and radius > 0 else None,
                                   "quality": "recorded", "source": state.get("source"), "observation_clock": state["clock"]})
        return result

    def aircraft(self,clock):
        result=[]
        for record in self.planes:
            if clock<record['start_clock'] or (number(record.get('end_clock')) and clock>=record['end_clock']):continue
            row=_latest(record['samples'],clock)
            if row is None or not point(row.get('position')) or not 0<=clock-row['clock']<FRESHNESS:continue
            result.append({'id':record.get('id'),'plane_id':record.get('plane_id'),'team_id':record.get('team_id'),
                'params_id':record.get('params_id'),'x_engine':row['position'][0],'z_engine':row['position'][1],
                'observation_clock':row['clock'],'source':deepcopy(row.get('source')),'quality':'recorded'})
        return result

    def _fact(self, kind, clock, entity, label, *, source=None, details=None, quality="recorded"):
        digest = hashlib.sha256(json.dumps([kind, clock, entity, details], sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:16]
        return {"id": f"{kind}:{digest}", "clock": clock, "entity_id": entity, "kind": kind,
                "label": label, "source": source, "quality": quality, "details": details or {},
                "recording_sha256": self.report.get("sha256")}

    def _build_events(self):
        facts = []
        for kind, records in (("artillery", self.shots), ("torpedoes", self.torpedoes)):
            groups = defaultdict(list)
            for record in records:
                groups[(record.get("owner_entity_id"), record.get("salvo_id"), record["start_clock"])].append(record)
            for (entity, _, _), records in groups.items():
                clock = min(row["start_clock"] for row in records)
                label = f"{self.name(entity)} · {'火炮发射' if kind == 'artillery' else '鱼雷记录'} {len(records)} {'枚炮弹' if kind == 'artillery' else '枚'}"
                facts.append(self._fact(kind, clock, entity, label, source=records[0].get("source"),
                             details={"count": len(records), "projectile_ids": [row.get("id") for row in records],
                                      "interpretation": "记录到的弹药事件；不等于开火总数、命中或目标识别"}))
        for record in self.consumables:
            entity = record.get("entity_id")
            duration = record.get("reported_work_time_left")
            label = f"{self.name(entity)} · {record.get('label') or '未识别消耗品'}启用"
            if number(duration):
                label += f" · 报告剩余 {duration:.1f} 秒"
            fields = ("id", "entity_id", "type", "label", "start_clock", "expected_end_clock", "reported_work_time_left", "range_engine", "quality", "source")
            details = {key: deepcopy(value) for key, value in record.items() if key in fields}
            facts.append(self._fact("consumable", record["start_clock"], entity, label,
                         source=record.get("source"), details=details))
            if number(record.get("end_clock")):
                facts.append(self._fact("consumable", record["end_clock"], entity,
                    f"{self.name(entity)} · {record.get('label') or '消耗品'}终止记录", source=record.get("end_source"),
                    details={"activation_id": record.get("id"), "type": record.get("type"), "transition": "observed_end"}))
        for entity, ship in self.ships.items():
            hp, visibility, hp_clock, spotted, spotted_clock = None, None, None, None, None
            for sample in ship["samples"]:
                value = sample.get("hp")
                stamp = sample.get("hp_clock")
                maximum = ship.get("max_hp")
                if number(value) and number(stamp) and stamp != hp_clock:
                    if number(hp) and hp != value:
                        delta = value - hp
                        # Small replicated updates stay in snapshots; expose significant
                        # changes as navigable evidence rather than a damage attribution.
                        threshold = max(1000, maximum * 0.05) if number(maximum) else 1000
                        if abs(delta) >= threshold:
                            facts.append(self._fact("health", stamp, entity,
                                f"{self.name(entity)} · 血量记录 {'增加' if delta > 0 else '降低'} {abs(delta):,.0f}",
                                source=sample.get("hp_source") or {"clock": stamp, "field": "health"},
                                details={"previous_hp": hp, "hp": value, "delta": delta, "previous_hp_clock": hp_clock,
                                         "threshold": threshold, "attribution": "not_available"}))
                    hp, hp_clock = value, stamp
                visible = sample.get("visible_to_recorder")
                if type(visible) is bool and visible != visibility and number(sample.get("visibility_clock")):
                    if visibility is not None:
                        facts.append(self._fact("visibility", sample["visibility_clock"], entity,
                            f"{self.name(entity)} · {'重新获得观测' if visible else '失去观测'}（本视角）",
                            source=sample.get("visibility_source") or {"clock": sample["visibility_clock"], "field": "minimap_visibility"},
                            details={"visible_to_recorder": visible, "enemy_spotting_inferred": False}))
                    visibility = visible
                enemy_spotted = sample.get("spotted_by_enemy")
                enemy_clock = sample.get("spotted_clock")
                if type(enemy_spotted) is bool and number(enemy_clock) and enemy_clock != spotted_clock:
                    if spotted is not None and enemy_spotted != spotted:
                        facts.append(self._fact("visibility", enemy_clock, entity,
                            f"{self.name(entity)} · {'敌方点亮' if enemy_spotted else '敌方点亮解除'}记录",
                            source=sample.get("spotted_source") or {"clock": enemy_clock, "field": "visibilityFlags"},
                            details={"spotted_by_enemy": enemy_spotted, "visibility_flags_raw": sample.get("visibility_flags_raw"),
                                     "scope": "Vehicle.visibilityFlags, independent of recorder minimap visibility"}))
                    spotted, spotted_clock = enemy_spotted, enemy_clock
        for event in self.report.get("death_events", []):
            if number(event.get("clock")):
                entity = event.get("victim_entity_id")
                facts.append(self._fact("death", event["clock"], entity, f"{self.name(entity)} · 击沉记录",
                             source="VehicleDeath", details=deepcopy(event)))
        return sorted(facts, key=lambda row: (row["clock"], row["id"]))

    def events_between(self, start=None, end=None, entity_id=None):
        rows = self.events
        if number(start):
            rows = rows[bisect_right(rows, start - 1e-9, key=lambda row: row["clock"]):]
        if number(end):
            rows = rows[:bisect_right(rows, end, key=lambda row: row["clock"])]
        if entity_id is not None:
            rows = [row for row in rows if row["entity_id"] == entity_id]
        return rows

    def positions(self, clock):
        result = {}
        for entity, segments in self._position_tracks.items():
            if self.ship_state(entity, clock).get("visibility") == "not_visible":
                continue
            if any(event.get("victim_entity_id") == entity and number(event.get("clock")) and event["clock"] <= clock
                   for event in self.report.get("death_events", [])):
                continue
            candidates = []
            for segment in segments:
                if segment[0][0] <= clock <= segment[-1][0]:
                    sample = segment[bisect_right(segment, clock, key=lambda row: row[0]) - 1]
                    candidates.append(sample)
            if len(candidates) == 1:
                row = candidates[0]
                result[entity] = {"x_engine": row[1], "z_engine": row[2], "observation_clock": row[0], "quality": "recorded"}
        return result

    def context(self, clock, window_seconds=60, entity_id=None):
        """Machine-readable facts at a replay instant; no future snapshots leak in."""
        if not number(clock) or clock < 0 or (number(self.end_clock) and clock > self.end_clock):
            raise ValueError("Query clock is outside the observed recording")
        if not number(window_seconds) or window_seconds < 0:
            raise ValueError("A nonnegative evidence window is required")
        positions = self.positions(clock)
        try:
            battle_time = packet_to_battle_time(self.report["time_analysis"], clock)
        except (KeyError, TypeError, ValueError):
            battle_time = {"status": "unknown", "reason": "No usable recorded battle-clock anchors"}
        states = []
        for entity, player in self.roster.items():
            if entity_id is not None and entity != entity_id:
                continue
            state = self.ship_state(entity, clock)
            ship = self.catalog.get(str(player.get("shipParamsId")), {})
            states.append(dict(state, raw_name=player.get("name"), team_id=player.get("teamId"),
                               species=ship.get("species"), position=positions.get(entity)))
        merged = self.report.get('multiview')
        result = {"format_version": 1, "kind": "multiview_tactical_evidence" if merged else "single_view_tactical_evidence", "packet_clock": clock, "battle_time": battle_time,
                "source": {"replay_sha256": self.report.get("sha256"), "arena_id": self.report.get("arena_id"),
                           "version": self.report.get("replay_version"), "combat_schema": self.combat.get("schema_version"),
                           "provenance": deepcopy(self.combat.get("provenance"))},
                "capabilities": self.capabilities, "ships": states,
                "events": self.events_between(max(0, clock - window_seconds), clock, entity_id),
                "window_seconds": window_seconds,
                "recorded_projectile_points": [row for row in self.projectiles(clock) if row["quality"] == "recorded"],
                "smoke_observations": self.smokes(clock),"aircraft_observations":self.aircraft(clock), "limitations": self.limitations,
                "estimated_projectile_positions_excluded": True}
        if merged:
            result['clock_domain'] = 'battle_elapsed_seconds'
            result['multiview'] = {key: deepcopy(merged.get(key)) for key in ('schema_version', 'engine_version', 'battle_id', 'source_shas', 'sources', 'policies')}
            result['source']['replay_sha256'] = None
            result['source']['derived_sha256'] = self.report.get('sha256')
        return result

    def summary(self):
        return {"available": self.available, "capabilities": self.capabilities,
                "event_counts": dict(Counter(event["kind"] for event in self.events)),
                "recorded_shell_count": len(self.shots), "recorded_torpedo_count": len(self.torpedoes),
                "consumable_activations": len(self.consumables), "limitations": self.limitations,
                "sources": deepcopy(self.combat.get("provenance")), "diagnostics": deepcopy(self.combat.get("diagnostics"))}
