"""Read-only 15.9 combat observations, independent of the legacy renderer.

Reference: landaire/wows-toolkit, MIT, commit
9ab2388002f3075d0bc85c2d7c879679f7f55383:
crates/wows-replays/src/analyzer/decoder/decode.rs and
crates/minimap-renderer/src/renderer.rs. Local 15.9 entity definitions and
CommonConsumables.{UsageParams,ConsumableConstants}, Vehicle.visibilityFlags
are the authoritative wire bindings. No old shell/torpedo database is used.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import struct

from .contracts import decode_minimap
from .safe_pickle import restricted_loads


VERSION = "15.9.0.13357625"
REFERENCE_COMMIT = "9ab2388002f3075d0bc85c2d7c879679f7f55383"
# Extracted from the verified idGenerator(0) declaration, not a slot index.
CONSUMABLE_NAMES = (
    "CRASH_CREW", "SCOUT", "AIR_DEFENSE_DISP", "SPEED_BOOSTER", "MB_BOOSTER",
    "HANGAR_BOOSTER", "SMOKE_GENERATOR", "UNUSED", "REGEN_CREW", "FIGHTER",
    "SONAR", "TA_RELOADER", "RLS", "TRIGGER1", "TRIGGER2", "TRIGGER3",
    "TRIGGER4", "TRIGGER5", "TRIGGER6", "INVULNERABLE", "HEAL_FORSAGE",
    "ACTIVE_MANEUVERING", "CALL_FIGHTERS", "REGENERATE_HEALTH",
    "SUBMARINE_OXYGEN_REGEN", "SUBMARINE_WAVEGUN_BOOST", "SUBMARINE_FOURTH_STATE",
    "DEPTH_CHARGES", "TRIGGER7", "TRIGGER8", "TRIGGER9", "BUFF", "BUFFS_SHIFT",
    "CIRCLE_WAVE", "GO_DEEP", "WEAPON_RELOAD_BOOSTER", "HYDROPHONE", "FAST_RUDDERS",
    "ENERGY_FREEZE", "GROUP_AURA_BUFF", "AFFECTED_BUFF_AURA", "INVISIBILITY_EXTRA_BUFF",
    "SUBMARINE_LOCATOR", "PLANE_SMOKE_GENERATOR", "PLANE_MINEFIELD", "MINEFIELD",
    "TACTICAL_TRIGGER1", "TACTICAL_TRIGGER2", "TACTICAL_TRIGGER3", "TACTICAL_TRIGGER4",
    "TACTICAL_TRIGGER5", "TACTICAL_TRIGGER6", "RECON_GROUP", "SMOKE_PLANE",
    "TACTICAL_BUFF", "PLANE_TRIGGER1", "PLANE_TRIGGER2", "PLANE_TRIGGER3", "PLANE_BUFF",
    "PLANE_TACTICAL_FIGHTERS", "TACTICAL_HEAL_CONSUMABLE_RELOAD", "TACTICAL_BUFF_GUN_BOOST",
    "TACTICAL_BUFF_AUX_BOOST", "TACTICAL_FIRE_EXTINGUISHING", "TACTICAL_BUFF_HEAL",
    "AUX_TORP_BOOSTER", "ANY", "ALL", "SPECIAL",
)
LABELS = {
    0: "损害管制小组", 1: "侦察机", 2: "防御型对空火力", 3: "引擎增压",
    4: "主炮装填助推器", 5: "飞机整备助推器", 6: "发烟器", 8: "维修小组",
    9: "战斗机", 10: "水底搜索", 11: "鱼雷装填助推器", 12: "监视雷达",
    22: "呼叫战斗机", 23: "恢复生命值", 36: "水听器", 42: "潜艇监视设备",
}
VISION_FLAGS = (
    "BY_SHIP", "BY_MAIN_PLANE", "BY_COMMON_XRAY", "BY_RLS_PERSONAL", "BY_RLS",
    "BY_SONAR", "IN_SMOKE", "BY_PINGER", "BY_MISC_PLANE", "BY_SUBMARINE_LOCATOR",
    "BY_RECON", "BY_ANTI_MISSILE",
)


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def get(value, key, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def vector(value):
    try:
        result = [getattr(value, axis) for axis in ("x", "y", "z")]
    except AttributeError:
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            return None
        result = list(value)
    return result if all(number(item) for item in result) else None


def planar(value):
    xyz = vector(value)
    return [xyz[0], xyz[2]] if xyz is not None else None


def planar2(value):
    if isinstance(value,(list,tuple)) and len(value)==2:result=list(value)
    else:result=[get(value,'x'),get(value,'y')]
    return result if all(number(v) for v in result) else None


def minimap_heading(raw):
    """North/clockwise wire yaw -> east/counterclockwise world x/z heading."""
    return (math.pi / 2 - raw + math.pi) % math.tau - math.pi


def decode_usage(blob):
    """Decode the client's exact UsageParams formats without running its code."""
    if not isinstance(blob, bytes) or len(blob) < 2:
        raise ValueError("Consumable usage requires a complete two-byte prefix")
    usage, kind = struct.unpack_from("<BB", blob)
    formats = {0: "<BB", 1: "<BB", 2: "<BBff", 3: "<BBbQ"}
    fmt = formats.get(usage)
    if fmt is None or len(blob) != struct.calcsize(fmt):
        raise ValueError("Unknown or incomplete consumable usage layout")
    values = struct.unpack(fmt, blob)
    result = {"usage_type": usage, "type": kind}
    if usage == 2:
        if not all(number(v) for v in values[2:]):
            raise ValueError("Non-finite consumable target")
        result["target_position_raw"] = list(values[2:])
    elif usage == 3:
        result.update(target_type=values[2], target_id=values[3])
    return result


class CombatCapture:
    """Attach after Collector initialization; callbacks never mutate game entities."""

    consumable_names = CONSUMABLE_NAMES
    consumable_labels = LABELS

    def __init__(self, profile=None, max_records=500_000):
        if profile is not None:
            if (profile.get("schema_version") != 1 or profile.get("client_version") != VERSION
                    or profile.get("status") != "source_verified"):
                raise ValueError("Combat evidence profile is not verified for the exact client version")
            mappings = profile.get("consumables", {}).get("type_ids", {})
            if {str(k): v for k, v in mappings.items()} != {str(k): v for k, v in enumerate(CONSUMABLE_NAMES)}:
                raise ValueError("Combat consumable mapping differs from its evidence profile")
            if (profile.get("visibility", {}).get("hidden_mask") != 0xc0000000
                    or profile.get("heading", {}).get("normalized_convention") != "east_ccw"
                    or profile.get("heading", {}).get("raw_formula") !=
                    "code / 255 * 2*(pi+1e-7) - (pi+1e-7)"):
                raise ValueError("Combat heading or visibility mapping differs from its evidence profile")
        self.profile = profile
        self.max_records = max_records
        self._count = 0
        self._source = {"clock": 0.0, "packet_offset": None, "packet_type": None}
        self._hooks = []
        self._states, self._ships, self._smokes = {}, {}, {}
        self._closed_smokes = []
        self._smoke_dirty = {}
        self._projectiles = {}
        self._active_consumables = {}
        self.planes=[];self._active_planes={}
        self.shots, self.torpedoes, self.consumables, self.consumable_states, self.events = [], [], [], [], []
        self.diagnostics = {"errors": [], "counts": Counter(), "truncated": False}

    def _reserve(self):
        if self._count >= self.max_records:
            self.diagnostics["truncated"] = True
            return False
        self._count += 1
        return True

    def source(self, field=None):
        result = dict(self._source)
        result["packet_clock"] = result["clock"]
        if field:
            result["field"] = field
        return result

    def begin_packet(self, frame):
        self._source = {"clock": frame.clock, "packet_offset": frame.offset,
                        "packet_type": frame.type}

    @property
    def clock(self):
        return self._source["clock"]

    def _subscribe(self, entity_class, registry, key, callback):
        def guarded(*args, **kwargs):
            self.diagnostics["counts"][key] += 1
            try:
                callback(*args, **kwargs)
            except Exception as error:
                if len(self.diagnostics["errors"]) < 30:
                    self.diagnostics["errors"].append({
                        "callback": key, "error": f"{type(error).__name__}: {error}",
                        "source": self.source(),
                    })
        table = getattr(entity_class, registry)
        # replay_unpack's public subscriber checks an unqualified key and can
        # erase existing subscribers. Append to the exact key instead.
        table.setdefault(key, []).append(guarded)
        self._hooks.append((table, key, guarded))

    def attach(self, entity_class):
        if self._hooks:
            raise RuntimeError("CombatCapture is already attached")
        methods = {
            "Avatar_updateMinimapVisionInfo": lambda e, arr1, arr2: self._minimap(arr1),
            "Avatar_receiveArtilleryShots": lambda e, packs: self._launch(packs, False),
            "Avatar_receiveTorpedoes": lambda e, packs: self._launch(packs, True),
            "Avatar_receiveShotKills": lambda e, packs: self._kills(packs),
            "Avatar_receiveTorpedoSynchronization": self._torpedo_sync,
            "Avatar_receiveTorpedoDirection": self._torpedo_direction,
            "Avatar_receiveTorpedoManeuverEnd": self._torpedo_maneuver_end,
            "Avatar_receiveTorpedoArmed": self._torpedo_armed,
            "Avatar_receive_addMinimapSquadron":self._plane_add,
            "Avatar_receive_updateMinimapSquadron":self._plane_update,
            "Avatar_receive_removeMinimapSquadron":lambda e,p:self._plane_remove(e,p,'receive_removeMinimapSquadron'),
            "Avatar_receive_removeSquadron":lambda e,p:self._plane_remove(e,p,'receive_removeSquadron'),
            "Avatar_receive_deactivateSquadron":self._plane_deactivate,
            "Vehicle_onConsumableUsed": self._consumable_used,
            "Vehicle_onConsumableInterrupted": self._consumable_end,
            "Vehicle_onConsumablePaused": self._consumable_pause,
            "Vehicle_setConsumables": self._consumable_dump,
        }
        for key, callback in methods.items():
            self._subscribe(entity_class, "_methods_subscriptions", key, callback)
        for prop in ("health", "isAlive", "visibilityFlags"):
            self._subscribe(entity_class, "_properties_subscriptions", f"Vehicle_{prop}",
                            lambda e, value, p=prop: self._property(e, p, value))
        for prop in ("points", "radius", "activePointIndex", "bcRadius", "height"):
            callback = lambda e, *a: self._smoke_dirty.__setitem__(e.id, e)
            self._subscribe(entity_class, "_properties_subscriptions", f"SmokeScreen_{prop}", callback)
            self._subscribe(entity_class, "_nested_properties_subscription", f"SmokeScreen_{prop}", callback)
        return self

    def detach(self):
        for table, key, callback in self._hooks:
            if callback in table.get(key, []):
                table[key].remove(callback)
        self._hooks.clear()

    def _sample(self, entity_id, updates, force=False):
        if self._count >= self.max_records:
            self.diagnostics["truncated"] = True
            return
        state = self._states.setdefault(entity_id, {})
        state.update(updates)
        ship = self._ships.setdefault(str(entity_id), {"entity_id": entity_id, "max_hp": None, "samples": []})
        rows = ship["samples"]
        if not force and rows and 0 <= self.clock - rows[-1]["clock"] < 1.0:
            return
        if self._reserve():
            rows.append(dict(state, **self.source(), quality="recorded"))

    def _plane_add(self,entity,plane_id,team_id,params_id,position,*raw):
        position=planar2(position)
        if type(plane_id) is not int or type(team_id) is not int or type(params_id) is not int or position is None or not self._reserve():return
        if plane_id in self._active_planes:self._plane_remove(entity,plane_id,'receive_addMinimapSquadron.replaced')
        record={'id':f'plane:{plane_id}:{self._source["packet_offset"]}','plane_id':plane_id,'team_id':team_id,
            'params_id':params_id,'start_clock':self.clock,'source':self.source('receive_addMinimapSquadron'),
            'end_clock':None,'end_source':None,'deactivated_clock':None,'extra_add_arguments_raw':list(raw),
            'samples':[{'clock':self.clock,'position':position,'source':self.source('receive_addMinimapSquadron')}]}
        self.planes.append(record);self._active_planes[plane_id]=record

    def _plane_update(self,entity,plane_id,position):
        record=self._active_planes.get(plane_id);position=planar2(position)
        if record is not None and position is not None and self._reserve():
            record['samples'].append({'clock':self.clock,'position':position,'source':self.source('receive_updateMinimapSquadron')})

    def _plane_remove(self,entity,plane_id,field='receive_removeMinimapSquadron'):
        record=self._active_planes.pop(plane_id,None)
        if record is not None:record.update(end_clock=self.clock,end_source=self.source(field))

    def _plane_deactivate(self,entity,plane_id,*raw):
        record=self._active_planes.get(plane_id)
        if record is not None:record.update(deactivated_clock=self.clock,deactivation_raw=list(raw))

    def _property(self, entity, prop, value):
        if prop == "health" and number(value):
            updates = {"hp": value, "hp_clock": self.clock, "hp_source": self.source(prop)}
        elif prop == "isAlive" and type(value) in (bool, int) and value in (0, 1):
            updates = {"is_alive": bool(value), "alive_clock": self.clock, "alive_source": self.source(prop)}
        elif prop == "visibilityFlags" and type(value) is int and 0 <= value <= 0xffffffff:
            # Flags describe visibility to the opposing team, not the recorder's
            # local minimap observation. Unknown bits do not become known causes.
            updates = {"visibility_flags_raw": value,
                       "visibility_flags": [name for bit, name in enumerate(VISION_FLAGS) if value & (1 << bit)],
                       "visibility_unknown_bits": value & ~((1 << len(VISION_FLAGS)) - 1),
                       "spotted_by_enemy": (bool(value) if value < (1 << len(VISION_FLAGS)) else None),
                       "spotted_clock": self.clock,
                       "spotted_source": self.source(prop)}
        else:
            return
        self._sample(entity.id, updates, True)

    def _minimap(self, rows):
        for row in rows or []:
            entity_id, packed = get(row, "vehicleID"), get(row, "packedData")
            if type(entity_id) is not int or not entity_id or type(packed) is not int:
                continue
            decoded = decode_minimap(packed)
            hidden = bool(packed & 0xc0000000)
            if decoded["sentinel"] and not hidden:
                self.diagnostics["counts"]["minimap_sentinel"] += 1
                continue
            visible = not hidden
            previous = self._states.get(entity_id, {}).get("visible_to_recorder")
            updates = {"visible_to_recorder": visible, "visibility_clock": self.clock,
                       "visibility_source": self.source("updateMinimapVisionInfo"),
                       "minimap_bit30_raw": decoded["bit30"],
                       "minimap_visibility_kind": ("ignore" if decoded["bit30"] else
                                                  "invisible" if hidden else "map")}
            if visible:
                # Local DataPacker.unpackValue divides by (1 << bits) - 1.
                # P0's old /256 approximation is retained separately for audit.
                raw = ((packed >> 22) & 255) / 255.0 * (math.tau + 2e-7) - (math.pi + 1e-7)
                updates.update(heading_raw=raw, heading_radians=minimap_heading(raw),
                               heading_legacy_raw=decoded["heading_radians"],
                               heading_code=(packed >> 22) & 255,
                               heading_convention="east_ccw", heading_clock=self.clock,
                               heading_source=self.source("updateMinimapVisionInfo.heading"),
                               minimap_position=[decoded["x_engine"], decoded["z_engine"]])
            self._sample(entity_id, updates, visible != previous)

    def _launch(self, packs, torpedo):
        for pack in packs or []:
            owner, salvo = get(pack, "ownerID"), get(pack, "salvoID")
            for shot in get(pack, "torpedoes" if torpedo else "shots", []) or []:
                if not self._reserve():
                    return
                shot_id = get(shot, "shotID")
                raw_start = vector(get(shot, "pos"))
                start = planar(raw_start) if raw_start != [0, 0, 0] else None
                record = {
                    "id": f"{'torpedo' if torpedo else 'shell'}:{owner}:{shot_id}:{self._source['packet_offset']}:{self._count}",
                    "owner_entity_id": owner, "shot_id": shot_id, "salvo_id": salvo,
                    "params_id": get(pack, "paramsID"), "start_clock": self.clock,
                    "start": start, "start_raw": raw_start,
                    "end_clock": None, "end": None, "end_reason": None, "end_source": None,
                    "quality": "recorded_event", "source": self.source(),
                    "samples": [],
                }
                if torpedo:
                    direction = vector(get(shot, "dir"))
                    record.update(direction_raw=direction, velocity_engine=None,
                                  velocity_units=None,
                                  velocity_quality="pending_client_consumption_chain",
                                  armed_at_observation=get(shot, "armed"),
                                  maneuver_dump_present=get(shot, "maneuverDump") is not None,
                                  acoustic_dump_present=get(shot, "acousticDump") is not None)
                    self.torpedoes.append(record)
                else:
                    record.update(target=planar(get(shot, "tarPos")),
                                  target_semantics="reported_aim_endpoint_not_impact",
                                  server_time_left_raw=get(shot, "serverTimeLeft"),
                                  speed_raw=get(shot, "speed"), pitch_raw=get(shot, "pitch"),
                                  hit_distance_raw=get(shot, "hitDistance"))
                    self.shots.append(record)
                self._projectiles.setdefault((owner, shot_id), []).append(record)

    def _active_projectile(self, owner, shot_id):
        records = [row for row in self._projectiles.get((owner, shot_id), [])
                   if row["end_clock"] is None]
        if len(records) == 1:
            return records[0]
        self.diagnostics["counts"]["unassociated_projectile_update"] += 1
        return None

    def _kills(self, packs):
        for pack in packs or []:
            for kill in get(pack, "kills", []) or []:
                record = self._active_projectile(get(pack, "ownerID"), get(kill, "shotID"))
                if record is not None:
                    record.update(end_clock=self.clock, end=planar(get(kill, "pos")),
                                  end_reason="receiveShotKills", end_source=self.source(),
                                  hit_type_raw=get(pack, "hitType"))
                elif self._reserve():
                    self.events.append({"kind": "unassociated_projectile_end",
                                        "clock": self.clock, "owner_entity_id": get(pack, "ownerID"),
                                        "shot_id": get(kill, "shotID"), "position": planar(get(kill, "pos")),
                                        "hit_type_raw": get(pack, "hitType"), "source": self.source()})

    def _torpedo_sample(self, owner, shot_id, position, extra=None):
        record = self._active_projectile(owner, shot_id)
        if record is not None and record["id"].startswith("torpedo") and self._reserve():
            record["samples"].append(dict(clock=self.clock, position=planar(position),
                                           source=self.source(), **(extra or {})))

    def _torpedo_sync(self, entity, ownerID, shotID, serverPos):
        self._torpedo_sample(ownerID, shotID, serverPos)

    def _torpedo_direction(self, entity, ownerID, shotID, serverPos, *raw):
        self._torpedo_sample(ownerID, shotID, serverPos, {"direction_update_raw": list(raw)})

    def _torpedo_maneuver_end(self, entity, ownerID, shotID, serverPos, *raw):
        self._torpedo_sample(ownerID, shotID, serverPos, {"maneuver_end_raw": list(raw)})

    def _torpedo_armed(self, entity, ownerID, shotID):
        record = self._active_projectile(ownerID, shotID)
        if record is not None and record["id"].startswith("torpedo"):
            record["armed_clock"] = self.clock
            record["armed_source"] = self.source()

    def _consumable_used(self, entity, consumableUsageParams, workTimeLeft):
        try:
            usage = decode_usage(consumableUsageParams)
        except ValueError as error:
            usage = {"type": None, "decode_error": str(error),
                     "raw_sha256": hashlib.sha256(consumableUsageParams).hexdigest()}
        if not self._reserve():
            return
        kind = usage["type"]
        symbolic = self.consumable_names[kind] if type(kind) is int and 0 <= kind < len(self.consumable_names) else None
        duration = workTimeLeft if number(workTimeLeft) and workTimeLeft >= 0 else None
        record = {
            "id": f"consumable:{entity.id}:{kind}:{self._source['packet_offset']}",
            "entity_id": entity.id, "type": kind, "symbolic_name": symbolic,
            "label": self.consumable_labels.get(kind, f"消耗品 {symbolic or kind or '未知'}"),
            "start_clock": self.clock, "end_clock": None, "end_source": None,
            "expected_end_clock": self.clock + duration if duration is not None else None,
            "reported_work_time_left": duration, "work_time_left_raw": workTimeLeft,
            "range_engine": None, "quality": "reported_timer" if duration is not None else "activation_only",
            "source": self.source("onConsumableUsed"), "usage": usage,
        }
        self.consumables.append(record)
        if kind is not None:
            self._active_consumables.setdefault((entity.id, kind), []).append(record)

    def _end_consumable(self, entity, kind, reason):
        records = [r for r in self._active_consumables.get((entity.id, kind), []) if r["end_clock"] is None]
        if records:
            for record in records:
                if record["expected_end_clock"] is None or record["expected_end_clock"] >= self.clock:
                    record.update(end_clock=self.clock, end_source=self.source(reason), end_reason=reason)
        elif self._reserve():
            self.events.append({"kind": reason, "entity_id": entity.id, "type": kind,
                                "clock": self.clock, "source": self.source()})

    def _consumable_end(self, entity, consumableType):
        self._end_consumable(entity, consumableType, "onConsumableInterrupted")

    def _consumable_pause(self, entity, consumableType):
        self._end_consumable(entity, consumableType, "onConsumablePaused")

    def _consumable_dump(self, entity, dumpStates):
        if not isinstance(dumpStates, bytes) or len(dumpStates) > 1024 * 1024:
            raise ValueError("Consumable snapshot exceeds the one-MiB limit")
        dump = restricted_loads(dumpStates, encoding="latin1")
        if not isinstance(dump, dict):
            raise ValueError("Consumable snapshot is not a dictionary")
        for kind, item in dump.get("consumablesDict", []):
            lifecycle = item.get("lifecycleDump") or {}
            context = lifecycle.get("contextDump") or {}
            if self._reserve():
                self.consumable_states.append({
                    "entity_id": entity.id, "type": kind, "clock": self.clock,
                    "state_raw": lifecycle.get("currentStateId"),
                    "last_update_time_raw": lifecycle.get("lastUpdateTime"),
                    "remaining_uses_raw": context.get("numConsumables"),
                    "work_time_left_raw": context.get("workTimeLeft"),
                    "reload_time_left_raw": context.get("reloadTimeLeft"),
                    "source": self.source("setConsumables"),
                })

    def _smoke_sample(self, entity):
        state = entity.properties.get("client", {})
        points = [planar(value) for value in state.get("points", [])]
        points = [value for value in points if value is not None]
        radius = state.get("radius")
        if self._reserve():
            record = self._smokes.setdefault(entity.id, {
                "entity_id": entity.id, "samples": [], "end_clock": None,
                "quality": "recorded_geometry_lifetime_unknown",
            })
            record["end_clock"] = None
            record["samples"].append({
                "clock": self.clock, "points": points,
                "radius_engine": radius if number(radius) and radius > 0 else None,
                "active_point_index_raw": state.get("activePointIndex"),
                "source": self.source("SmokeScreen.properties"),
            })

    def after_packet(self, packet, entities):
        name = type(packet).__name__
        if name == "EntityCreate":
            entity_id = getattr(packet, "entityID", None)
            entity = entities.get(entity_id)
            if entity is not None:
                if entity.get_name() == "Vehicle":
                    for prop in ("health", "isAlive", "visibilityFlags"):
                        if prop in entity.properties.get("client", {}):
                            self._property(entity, prop, entity.properties["client"][prop])
                elif entity.get_name() == "SmokeScreen":
                    self._smoke_dirty[entity_id] = entity
        elif name == "EntityLeave":
            entity_id = getattr(packet, "entityId", None)
            if entity_id in self._smokes:
                self._smokes[entity_id].update(end_clock=self.clock,
                    end_reason="observation_ended_aoi_leave", end_source=self.source())
                self._closed_smokes.append(self._smokes.pop(entity_id))
        for entity in self._smoke_dirty.values():
            self._smoke_sample(entity)
        self._smoke_dirty.clear()

    def finish(self, roster, end_clock):
        roster_ids = set()
        for participant in roster:
            entity_id = participant.get("shipId")
            if type(entity_id) is not int:
                continue
            roster_ids.add(entity_id)
            ship = self._ships.setdefault(str(entity_id), {"entity_id": entity_id, "samples": [], "max_hp": None})
            maximum = participant.get("maxHealth")
            ship["max_hp"] = maximum if number(maximum) and maximum > 0 else None
            ship["max_hp_source"] = "decoded_roster.maxHealth" if ship["max_hp"] is not None else None
        return {
            "schema_version": 1, "client_version": VERSION, "observation_end_clock": end_clock,
            "heading_convention": "east_ccw", "coordinate_units": "engine_units",
            "ships": {key: value for key, value in self._ships.items() if int(key) in roster_ids},
            "shots": self.shots, "torpedoes": self.torpedoes, "consumables": self.consumables,
            "planes":self.planes,"plane_observation_schema":1,
            "consumable_states": self.consumable_states,
            "smokes": self._closed_smokes + list(self._smokes.values()),
            "events": self.events,
            "provenance": {"reference_commit": REFERENCE_COMMIT,
                           "local_definitions": ["Avatar.def", "Vehicle.def", "SmokeScreen.def", "alias.xml"],
                           "semantic_evidence": "combat_schema.json",
                           "profile_verified": self.profile is not None,
                           "profile_canonical_sha256": hashlib.sha256(
                               json.dumps(self.profile, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
                               if self.profile is not None else None,
                           "source_archive_sha256": self.profile["source_archive"]["sha256"]
                               if self.profile is not None else None},
            "limitations": [
                "Missing packets do not prove disappearance, death, inactivity or expiry.",
                "World coordinates use engine units; raw weapon speed is not world metres per second.",
                "A shot target is not a confirmed hit; receiveShotKills may be any terminal collision.",
                "Smoke entity leave ends the observation, not necessarily the physical smoke.",
                "Consumable expected end is the packet clock plus reported workTimeLeft, not an observed expiry.",
            ],
            "diagnostics": dict(self.diagnostics, counts=dict(self.diagnostics["counts"]),
                                records=self._count),
        }
