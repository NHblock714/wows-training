"""Offline, self-contained exports of verified basic replay evidence.

The exporter never modifies input reports or game files, queries accounts, or
merges observations from different viewpoints. Missing evidence stays missing.
"""
from __future__ import annotations

import base64
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
from html import escape
import json
import math
from pathlib import Path
import re
import secrets
import struct


SHIP_TYPES = {"Battleship": "战列舰", "Cruiser": "巡洋舰", "Destroyer": "驱逐舰",
              "AirCarrier": "航空母舰", "AircraftCarrier": "航空母舰", "Submarine": "潜艇"}
STAGES = {"WAITING": "等待", "COUNTDOWN": "准备倒计时", "STARTED": "战斗进行", "END_BATTLE_IDLING": "战斗结束"}
CSV_FIELDS = ["记录类型", "赛事", "比赛标识", "版本", "地图", "原始时间", "视角赛号", "原始赛号", "主号昵称",
              "区域", "公开账号ID", "关联状态", "队伍", "舰船参数ID", "舰船", "舰种", "等级", "记录击沉数",
              "记录被击沉数", "事件包时间", "比赛已过时间", "事件类型", "对象赛号", "控点", "旧归属", "新归属",
              "首个观测战斗时间", "末个观测战斗时间", "结算时长", "结算胜方", "录制方结果", "结果来源", "同场来源数", "备注", "SHA256"]
MAX_PNG_BYTES = 12 * 1024 * 1024


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _number(value):
    if type(value) in (int, float) and math.isfinite(value):
        return value
    return None


def _id(value):
    return _text(value) or None


def _arena(report):
    value = _id(report.get("arena_id"))
    return None if value in (None, "0", "-1") else value


def _h(value) -> str:
    return escape(_text(value), quote=True)


def _shown(value) -> str:
    return "未记录" if value is None or value == "" else _h(value)


def _seconds(value) -> str:
    number = _number(value)
    return "未记录" if number is None else f"{number:.2f}".rstrip("0").rstrip(".") + " 秒"


def _team(value) -> str:
    if value == -1:
        return "中立"
    if type(value) is int:
        return f"队伍 {value}"
    return "未记录"


def _outcome_label(value):
    return {"win": "胜（结算）", "loss": "负（结算）", "draw": "平（结算）"}.get(value, "未知（结算）")


def _winner_label(value):
    if value == -1:
        return "平局"
    return f"队伍 {value} 胜" if type(value) is int and value in (0, 1) else "未知"


def _settlement(report, participants, recorder_name):
    common = report.get("post_battle_common") or {}
    source = report.get("winner_source")
    diagnostic = []
    valid = isinstance(common, dict) and source == "verified_15.9_common_results" and _version(report) == "15.9.0.13357625"
    merged = report.get('multiview') or {}
    if (source == 'mapped_source_common_results' and merged.get('schema_version') == 1
            and merged.get('engine_version') == 'wows-multiview-1' and _version(report) == '15.9.0.13357625'
            and len(merged.get('source_shas', [])) >= 2):
        expected = ((merged.get('pairing') or {}).get('checks') or {}).get('winner_absolute_team')
        valid = type(expected) is int and expected in (-1,0,1) and expected == common.get('winner_team_id')
    if not valid:
        diagnostic.append("结算来源未标记为目标版本已验证公共结果")
    if report.get("post_battle_result_errors"):
        valid = False
        diagnostic.append("结算字段存在冲突或解析错误")
    winner = common.get("winner_team_id") if isinstance(common, dict) else None
    if type(winner) is not int or winner not in (-1, 0, 1):
        valid = False
        diagnostic.append("结算胜方缺失或超出已验证枚举")
    common_arena = _id(common.get("arena_id")) if isinstance(common, dict) else None
    if common_arena and _arena(report) and common_arena != _arena(report):
        valid = False
        diagnostic.append("公共结算与当前比赛 arena 不一致")
    reported_winner = report.get("winner_absolute_team")
    if type(reported_winner) is int and reported_winner in (-1, 0, 1) and reported_winner != winner:
        valid = False
        diagnostic.append("记录胜方与公共结算胜方冲突")
    host_candidates = set()
    host = report.get("host_absolute_team")
    if type(host) is int and host in (0, 1):
        host_candidates.add(host)
    for player in participants:
        if player["raw_tournament_name"] == recorder_name and type(player["team"]) is int and player["team"] in (0, 1):
            host_candidates.add(player["team"])
    host_team = next(iter(host_candidates)) if len(host_candidates) == 1 else None
    if len(host_candidates) > 1:
        diagnostic.append("录制方队伍信息冲突")
    outcome = "unknown"
    if valid and host_team is not None:
        outcome = "draw" if winner == -1 else "win" if winner == host_team else "loss"
    elif valid:
        diagnostic.append("未确认录制者所属参赛队伍，保留比赛结算但不推断录制方胜负")
    return {"verified": valid, "winner_absolute_team": winner if valid else None,
            "recorder_absolute_team": host_team, "recorder_outcome": outcome,
            "result_source": source if valid else None, "diagnostic": diagnostic}


def _version(report) -> str:
    return ".".join(part.strip() for part in re.split(r"[.,_]", _text(report.get("replay_version"))) if part.strip())


def _mapping_index(mappings):
    index = defaultdict(list)
    for row in mappings or []:
        if not isinstance(row, dict):
            continue
        event = _text(row.get("event_key")) or "unassigned"
        name = _text(row.get("tournament_name"))
        if row.get("confirmed") is False:
            if name and (row.get("resolved_identity") or {}).get("status") in ("conflict", "candidate"):
                index[(event, name)].append(row)
            continue
        if name and _text(row.get("personal_nickname")):
            index[(event, name)].append(row)
    return index


def _identity(name, event, index):
    rows = index.get((event, name), [])
    identities = {(_text(row.get("personal_nickname")), _text(row.get("region")), _text(row.get("account_id"))) for row in rows}
    result = {"raw_tournament_name": name, "status": "unmapped", "personal_nickname": None,
              "region": None, "account_id": None, "notes": None}
    unresolved = [row.get("resolved_identity") for row in rows if row.get("confirmed") is False]
    if unresolved:
        result["status"] = "conflict" if any(row.get("status") == "conflict" for row in unresolved) else "candidate"
        result["identity_resolution"] = unresolved
        return result
    if len(identities) == 1:
        nickname, region, account_id = next(iter(identities))
        result.update(status="confirmed", personal_nickname=nickname, region=region or None, account_id=account_id or None,
                      notes="\n".join(dict.fromkeys(_text(row.get("notes")) for row in rows if _text(row.get("notes")))) or None)
        result["identity_resolution"] = [row["resolved_identity"] for row in rows if row.get("resolved_identity")]
    elif len(identities) > 1:
        result["status"] = "conflict"
    return result


def _identity_status(value):
    return {"confirmed": "已确认关联", "unmapped": "未关联", "conflict": "关联冲突", "candidate": "候选待确认"}.get(value, "未关联")


def _event_time(value):
    if not isinstance(value, dict):
        return None
    exact = _number(value.get("elapsed_sec"))
    interval = value.get("elapsed_interval_sec")
    if exact is not None:
        return {"status": value.get("status"), "elapsed_sec": exact, "source_offsets": value.get("source_offsets", [])}
    if isinstance(interval, (list, tuple)) and len(interval) == 2 and all(_number(x) is not None for x in interval):
        return {"status": value.get("status"), "elapsed_interval_sec": list(interval), "source_offsets": value.get("source_offsets", [])}
    return None


def _event_time_label(value):
    if not value:
        return None
    if value.get("elapsed_sec") is not None:
        return _seconds(value["elapsed_sec"])
    interval = value.get("elapsed_interval_sec")
    return f"{interval[0]:g}–{interval[1]:g} 秒（观测区间）" if interval else None


def _capture_events(report):
    caps = {str(c.get("entity_id")): c for c in report.get("capture_points") or [] if isinstance(c, dict)}
    changes = report.get("cap_team_changes")
    if not isinstance(changes, dict):
        return []
    result = []
    for entity, samples in changes.items():
        if not isinstance(samples, list):
            continue
        cap = caps.get(str(entity), {})
        previous = cap.get("initial_team") if type(cap.get("initial_team")) is int else None
        for sample in samples:
            if not isinstance(sample, (list, tuple)) or len(sample) < 2 or _number(sample[0]) is None or type(sample[1]) is not int:
                continue
            clock, team = sample[:2]
            if team == previous:
                continue
            if previous is None or clock <= 0:
                previous = team
                continue  # Initial ownership is not a capture event.
            result.append({"packet_clock": clock, "capture_entity_id": str(entity), "capture_name": _text(cap.get("name")) or str(entity),
                           "previous_team": previous, "new_team": team, "source": "cap_team_changes"})
            previous = team
    return sorted(result, key=lambda row: row["packet_clock"])


def _png(path_value, expected_sha, dimensions):
    raw_path = _text(path_value)
    if raw_path.startswith(("\\\\", "//")) or "://" in raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute() or path.suffix.lower() != ".png" or not re.fullmatch(r"[0-9a-fA-F]{64}", _text(expected_sha)):
        return None
    try:
        if path.stat().st_size > MAX_PNG_BYTES:
            return None
        with path.open("rb") as stream:
            raw = stream.read(MAX_PNG_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_PNG_BYTES or len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if hashlib.sha256(raw).hexdigest().lower() != str(expected_sha).lower():
        return None
    if list(struct.unpack_from(">II", raw, 16)) != list(dimensions):
        return None
    return {"mime": "image/png", "sha256": expected_sha, "content_base64": base64.b64encode(raw).decode("ascii")}


def _snapshot(report, participants, selected_clock):
    geometry = report.get("geometry_analysis") or {}
    tracks = geometry.get("projected_tracks") or []
    raw_points = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        columns = track.get("columns") or []
        required = ("packet_clock", "x_engine", "z_engine", "png_x", "png_y")
        if any(columns.count(name) != 1 for name in required):
            continue
        indices = [columns.index(name) for name in required]
        for segment_number, segment in enumerate(track.get("segments") or []):
            if not isinstance(segment, (list, tuple)):
                continue
            clean = []
            for row in segment:
                if isinstance(row, (list, tuple)) and len(row) > max(indices):
                    values = [row[i] for i in indices]
                    if all(_number(value) is not None for value in values):
                        clean.append(values)
            if clean:
                raw_points.append((str(track.get("entity_id")), segment_number, clean))
    if selected_clock is None:
        selected_clock = _number((report.get("packet_clock_range") or {}).get("max"))
        if selected_clock is None:
            selected_clock = max((row[0] for _, _, segment in raw_points for row in segment), default=None)
    result = {"packet_clock": selected_clock, "points": [], "capture_points": [], "background_assets": [],
              "scope": "unavailable", "note": "没有可用的位置与边界证据；未补造坐标。", "outside_point_count": 0}
    segments = {o.get("clock_segment_id") for o in (report.get("time_analysis") or {}).get("observations") or [] if isinstance(o, dict)} - {None}
    if len(segments) > 1:
        result["note"] = "存在多个包时钟片段，单一秒数不足以唯一选择位置；未绘制。"
        return result
    projection = geometry.get("png_projection") or {}
    dimensions = projection.get("dimensions_pixels")
    valid_dimensions = isinstance(dimensions, (list, tuple)) and len(dimensions) == 2 and all(type(x) is int and 0 < x <= 8192 for x in dimensions)
    if (_version(report) != "15.9.0.13357625" or geometry.get("png_alignment_verified") is not True
            or geometry.get("errors") or projection.get("status") != "source_chain_verified"
            or projection.get("mapping_scope") != "single_identity_mapping" or not valid_dimensions):
        result["note"] = "版本或地图投影证据不满足已验证门槛；未绘制点位。"
        return result
    viewport, texture = projection.get("viewport_bounds_engine") or {}, projection.get("texture_bounds_engine") or {}
    if viewport.get("projection_convention") != "SpaceSizeUtils.native_max_min_return" or viewport.get("native_viewport_equality_verified") is not True:
        result["note"] = "地图轴向或视口一致性未验证；未绘制点位。"
        return result
    for bounds in (viewport, texture):
        if (bounds.get("unit") != "engine" or any(_number(bounds.get(k)) is None for k in ("min_x", "max_x", "min_z", "max_z"))
                or bounds["max_x"] <= bounds["min_x"] or bounds["max_z"] <= bounds["min_z"]):
            result["note"] = "地图坐标边界不完整或无效；未绘制点位。"
            return result
    width, height = dimensions
    crop = projection.get("viewport_crop_pixels") or {}
    if (crop.get("coordinate_convention") != "continuous_pixel_edges"
            or any(_number(crop.get(k)) is None for k in ("left", "top", "right", "bottom"))
            or not (0 <= crop["left"] < crop["right"] <= width and 0 <= crop["top"] < crop["bottom"] <= height)):
        result["note"] = "底图裁切证据不完整或无效；未绘制点位。"
        return result
    settings = (projection.get("source_paths") or {}).get("map_settings")
    if settings:
        raw_path = _text(settings)
        settings_path = Path(raw_path)
        expected = (projection.get("source_hashes") or {}).get("map_settings")
        try:
            if (raw_path.startswith(("\\\\", "//")) or "://" in raw_path or not settings_path.is_absolute()
                    or settings_path.suffix.lower() != ".settings" or settings_path.stat().st_size > 1024 * 1024):
                raise ValueError("Invalid local map-settings source")
            if hashlib.sha256(settings_path.read_bytes()).hexdigest() != expected:
                raise ValueError("Map settings hash mismatch")
        except (OSError, ValueError):
            result["note"] = "地图设置文件缺失、来源无效或已变化；未绘制点位。"
            return result
    result.update(width=width, height=height, viewport_crop=dict(crop), scope="verified_png_coordinates",
                  note="截至所选包时间的最近记录；不插值、不外推。空心点是此前记录，不能解释为当前真实位置。")
    for key in ("map_water", "map_minimap"):
        asset = _png((projection.get("source_paths") or {}).get(key), (projection.get("source_hashes") or {}).get(key), dimensions)
        if asset:
            result["background_assets"].append({"role": key, **asset})
    if not result["background_assets"]:
        result["note"] += " 底图未嵌入，仅展示点位画布。"

    def project(x, z, png_x=None, png_y=None):
        return (png_x, png_y) if _number(png_x) is not None and _number(png_y) is not None else (None, None)

    roster = {p["entity_id"]: p for p in participants}
    candidates = {}
    freshness = _number(report.get("freshness_policy_seconds")) or 3.0
    for entity, segment_number, segment in raw_points:
        if selected_clock is None:
            continue
        eligible = [row for row in segment if row[0] <= selected_clock]
        if not eligible:
            continue
        row = max(eligible, key=lambda r: r[0])
        previous = candidates.get(entity)
        if previous and previous["observation_packet_clock"] > row[0]:
            continue
        x, y = project(row[1], row[2], row[3], row[4])
        if x is None:
            continue
        observed = segment[0][0] <= selected_clock <= segment[-1][0] and selected_clock - row[0] < freshness
        player = roster.get(entity, {})
        candidates[entity] = {"entity_id": entity, "raw_tournament_name": player.get("raw_tournament_name") or f"实体 {entity}",
                              "team": player.get("team"), "x": x, "y": y, "x_engine": row[1], "z_engine": row[2],
                              "observation_packet_clock": row[0], "position_status": "observed_segment" if observed else "last_known",
                              "inside_canvas": crop["left"] <= x <= crop["right"] and crop["top"] <= y <= crop["bottom"]}
    result["points"] = list(candidates.values())
    result["outside_point_count"] = sum(not p["inside_canvas"] for p in result["points"])
    cap_rows = geometry.get("normalized_capture_centers") or []
    for cap in cap_rows:
        if not isinstance(cap, dict) or _number(cap.get("x_engine")) is None or _number(cap.get("z_engine")) is None:
            continue
        pixels = cap.get("png_pixel_xy") or [None, None]
        x, y = project(cap["x_engine"], cap["z_engine"], pixels[0] if len(pixels) > 0 else None, pixels[1] if len(pixels) > 1 else None)
        if x is not None:
            result["capture_points"].append({"name": _text(cap.get("name")), "entity_id": _id(cap.get("entity_id")), "x": x, "y": y})
    return result


def _model(report, mappings, ship_catalog, clock=None, snapshot=True):
    metadata = report.get("metadata") or {}
    event = _text(report.get("event_key")) or "unassigned"
    index = _mapping_index(report.get("effective_identity_mappings", mappings))
    deaths = [d for d in report.get("death_events", []) if isinstance(d, dict)] if isinstance(report.get("death_events"), list) else []
    kills = Counter(_id(d.get("killer_entity_id")) for d in deaths if _id(d.get("killer_entity_id")) not in (None, "0"))
    sunk = Counter(_id(d.get("victim_entity_id")) for d in deaths if _id(d.get("victim_entity_id")) not in (None, "0"))
    participants = []
    for player in report.get("roster", []) if isinstance(report.get("roster"), list) else []:
        if not isinstance(player, dict) or player.get("_player_kind") == 3:
            continue
        identity = _identity(_text(player.get("name")), event, index)
        entity = _id(player.get("shipId"))
        ship_id = _id(player.get("shipParamsId"))
        ship = (ship_catalog or {}).get(ship_id, (ship_catalog or {}).get(player.get("shipParamsId"), {}))
        if not isinstance(ship, dict):
            ship = {}
        species = _text(ship.get("species") or ship.get("type"))
        participants.append({**identity, "entity_id": entity, "avatar_id": _id(player.get("avatar_id")),
                             "team": player.get("teamId") if type(player.get("teamId")) is int else None,
                             "ship_params_id": ship_id, "ship_name": _text(ship.get("display_name") or ship.get("name") or ship.get("internal_name")) or None,
                             "ship_species_raw": species or None, "ship_type": SHIP_TYPES.get(species, species) or None,
                             "ship_tier": _number(ship.get("tier")), "recorded_kills": kills.get(entity), "recorded_deaths": sunk.get(entity)})
    by_entity = {p["entity_id"]: p["raw_tournament_name"] for p in participants if p["entity_id"]}
    normalized = report.get("normalized_death_times") or []
    death_rows = []
    for event_row in deaths:
        evidence = next((e for e in normalized if isinstance(e, dict) and e.get("event") == event_row), {})
        source = evidence.get("source") or {}
        victim, killer = _id(event_row.get("victim_entity_id")), _id(event_row.get("killer_entity_id"))
        death_rows.append({"packet_clock": _number(event_row.get("clock")), "victim_entity_id": victim, "killer_entity_id": killer,
                           "victim_raw_name": by_entity.get(victim), "killer_raw_name": by_entity.get(killer),
                           "battle_time": _event_time(evidence.get("battle_time")),
                           "source_packet_offset": _number(source.get("packet_offset"))})
    timing = report.get("time_analysis") or {}
    coverage = timing.get("coverage") or {}
    common = report.get("post_battle_common") or {}
    settlement = _settlement(report, participants, _text(metadata.get("playerName")))
    verified_common = settlement["verified"]
    annotation = report.get("battle_annotation") or {}
    result = {
        "event_key": event, "label": _text(annotation.get("label")) or None, "notes": _text(annotation.get("notes")) or None,
        "source": {"version": _version(report) or None, "sha256": _text(report.get("sha256")) or None,
                   "arena_id": _arena(report), "replay_name": _text(report.get("replay")).replace("\\", "/").rsplit("/", 1)[-1] or None,
                   "recorded_at_raw": _text(metadata.get("dateTime")) or None, "parser_status": _text(report.get("status")) or None,
                   "map_name_raw": _text(metadata.get("mapName")) or None},
        "map_name": _text(report.get("display_map_name") or metadata.get("mapDisplayName") or metadata.get("mapName")) or None,
        "scenario": _text(metadata.get("scenario")) or None,
        "recorder": _identity(_text(metadata.get("playerName")), event, index),
        "durations": {"first_observed_battle_elapsed_sec": _number(coverage.get("first_observed_elapsed_sec")),
                      "last_observed_battle_elapsed_sec": _number(coverage.get("last_observed_elapsed_sec")),
                      "last_observed_remaining_sec": _number(coverage.get("last_observed_remaining_sec")),
                      "packet_clock_min": _number((report.get("packet_clock_range") or {}).get("min")),
                      "packet_clock_max": _number((report.get("packet_clock_range") or {}).get("max")),
                      "verified_result_duration_sec": _number(common.get("duration_sec")) if verified_common else None},
        "winner_absolute_team": settlement["winner_absolute_team"],
        "recorder_absolute_team": settlement["recorder_absolute_team"],
        "recorder_outcome": settlement["recorder_outcome"], "result_source": settlement["result_source"],
        "result_diagnostic": settlement["diagnostic"],
        "roster_matches_header": report.get("roster_matches_header") if isinstance(report.get("roster_matches_header"), bool) else None,
        "participants": participants, "recorded_deaths": death_rows, "capture_team_changes": _capture_events(report),
        "time_anchors": [{"packet_clock": _number(row.get("packet_clock")), "stage": _text(row.get("stage_name")) or None,
                          "duration_raw": _number(row.get("duration")), "time_left_raw": _number(row.get("timeLeft"))}
                         for row in timing.get("stage_transitions") or [] if isinstance(row, dict)],
        "limitations": [_text(x) for x in report.get("limitations") or [] if _text(x)],
        "native_pending": {"unknown_packet_types": list((report.get("unknown_packet_counts") or {}).keys()),
                           "opaque_packet_types": list((report.get("effective_opaque_packet_counts", report.get("known_opaque_packet_counts")) or {}).keys())},
        "source_views": [], "conflicts": [],
    }
    result["source_views"] = [dict(result["source"], recorder_raw_name=result["recorder"]["raw_tournament_name"],
                                  recorder_absolute_team=result["recorder_absolute_team"], recorder_outcome=result["recorder_outcome"],
                                  winner_absolute_team=result["winner_absolute_team"], result_source=result["result_source"])]
    if report.get('multiview'):
        from copy import deepcopy
        merged = report['multiview']
        result['multiview'] = deepcopy(merged)
        result['clock_domain'] = 'battle_elapsed_seconds'
        result['source']['identity_kind'] = 'derived_recording_digest'
        result['source_views'] = [dict(result['source'], sha256=s.get('sha256'), recorder_raw_name=s.get('recorder') or s.get('recorder_name'),
                                     recorder_absolute_team=s.get('canonical_team_id'), recorder_outcome='unknown',
                                     winner_absolute_team=result['winner_absolute_team'], result_source=result['result_source']) for s in merged.get('sources', [])]
    if snapshot:
        result["map_snapshot"] = _snapshot(report, participants, clock)
    if (report.get("combat") or {}).get("schema_version") == 1:
        from .replay_intel import ReplayIntel
        intel = ReplayIntel(report, ship_catalog)
        if intel.available:
            result["combat_summary"] = intel.summary()
            if snapshot:
                instant = clock if clock is not None else result["durations"]["packet_clock_max"]
                if _number(instant) is not None:
                    result["tactical_context"] = intel.context(instant, window_seconds=180)
                result["combat_events"] = intel.events
    return result


def _svg(snapshot):
    if not snapshot or snapshot.get("scope") == "unavailable":
        return f'<p class="muted">{_h((snapshot or {}).get("note") or "未记录可绘制位置。")}</p>'
    width, height = snapshot["width"], snapshot["height"]
    crop = snapshot["viewport_crop"]
    left, top = crop["left"], crop["top"]
    visible_width, visible_height = crop["right"] - left, crop["bottom"] - top
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-label="记录位置示意" viewBox="{left:g} {top:g} {visible_width:g} {visible_height:g}">',
             f'<rect width="{width}" height="{height}" fill="#e8f0f5"/>',
             f'<defs><clipPath id="mapclip"><rect x="{left:g}" y="{top:g}" width="{visible_width:g}" height="{visible_height:g}"/></clipPath></defs>']
    for asset in snapshot["background_assets"]:
        parts.append(f'<image x="0" y="0" width="{width}" height="{height}" href="data:image/png;base64,{asset["content_base64"]}"/>')
    for i in range(1, 10):
        parts.append(f'<path d="M {left+visible_width*i/10:g} {top:g} V {crop["bottom"]:g} M {left:g} {top+visible_height*i/10:g} H {crop["right"]:g}" stroke="#64748b" stroke-opacity=".2" stroke-width="1"/>')
    parts.append('<g clip-path="url(#mapclip)">')
    for cap in snapshot["capture_points"]:
        parts.append(f'<circle cx="{cap["x"]:g}" cy="{cap["y"]:g}" r="18" fill="#ffffffb8" stroke="#475569" stroke-dasharray="4 3"/>')
        parts.append(f'<text x="{cap["x"]:g}" y="{cap["y"]+5:g}" text-anchor="middle" font-size="14" fill="#111827">{_h(cap["name"])}</text>')
    for point in snapshot["points"]:
        color = "#2563eb" if point["team"] == 0 else "#c2410c" if point["team"] == 1 else "#475569"
        fill = color if point["position_status"] == "observed_segment" else "#ffffff"
        title = f'{point["raw_tournament_name"]}；记录包时间 {point["observation_packet_clock"]:.2f} 秒；' + ("可见片段内记录" if point["position_status"] == "observed_segment" else "最后已知位置")
        parts.append(f'<g><title>{_h(title)}</title><circle cx="{point["x"]:g}" cy="{point["y"]:g}" r="5" fill="{fill}" stroke="{color}" stroke-width="2"/>')
        parts.append(f'<text x="{point["x"]+8:g}" y="{point["y"]-6:g}" font-size="10" paint-order="stroke" stroke="white" stroke-width="3" fill="{color}">{_h(point["raw_tournament_name"][:24])}</text></g>')
    parts.append('</g></svg>')
    parts.append(f'<p class="muted">包时间：{_seconds(snapshot["packet_clock"])}。实心：可见片段内记录；空心：最后已知。蓝：队伍 0，橙：队伍 1。字母仅标记控点中心，不表示半径。</p>')
    parts.append(f'<p class="muted">{_h(snapshot["note"])} 画布外记录：{snapshot["outside_point_count"]} 条，原坐标保留在 JSON。</p>')
    return "".join(parts)


def _table(headers, rows):
    if not rows:
        return '<p class="muted">未记录，不能据此认定未发生。</p>'
    return '<div class="table-wrap"><table><thead><tr>' + "".join(f"<th>{_h(h)}</th>" for h in headers) + '</tr></thead><tbody>' + "".join('<tr>' + "".join(f'<td>{_shown(value)}</td>' for value in row) + '</tr>' for row in rows) + '</tbody></table></div>'


def _document(title, body, generated):
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>{_h(title)}</title><style>
body{{margin:0;background:#eef2f6;color:#17263b;font:14px/1.65 "Segoe UI","Microsoft YaHei",sans-serif}}main{{max-width:1180px;margin:auto;padding:32px}}header,section{{background:white;border:1px solid #dbe3ec;border-radius:12px;padding:22px;margin-bottom:18px}}h1{{font-size:27px;margin:0 0 8px}}h2{{font-size:19px;margin:0 0 14px}}.muted{{color:#5b6b80;font-size:12px}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}}.card{{background:#f5f8fb;padding:14px;border-radius:8px}}.card strong{{display:block;font-size:20px}}.table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{padding:9px;border-bottom:1px solid #e2e8f0;text-align:left;vertical-align:top;white-space:pre-wrap;overflow-wrap:anywhere}}th{{background:#f2f6fa}}.sha{{font:11px/1.6 Consolas,monospace;overflow-wrap:anywhere}}.note{{white-space:pre-wrap}}svg{{display:block;max-height:780px;width:100%;border:1px solid #cbd5e1;border-radius:8px}}footer{{font-size:12px;color:#5b6b80}}@media print{{body{{background:white}}main{{padding:0}}section,header{{break-inside:avoid}}.table-wrap{{overflow:visible}}}}
</style></head><body><main><header><h1>{_h(title)}</h1><p class="muted">基础赛训记录报告 · 本地导出 · 自包含文件</p></header>{body}<footer>导出时间（UTC）：{_h(generated)}。本报告只呈现可追溯的基础记录；缺失身份或事件保持未记录，未输出未验证伤害与队伍总分。</footer></main></body></html>'''


def _render_single(model, generated):
    src, durations = model["source"], model["durations"]
    identity = model["recorder"]
    meta = [("赛事", model["event_key"]), ("场次标签", model["label"]), ("地图 / 模式", f'{model["map_name"] or "未记录"} / {model["scenario"] or "未记录"}'),
            ("arena ID", src["arena_id"]), ("版本", src["version"]), ("回放记录时间（原文）", src["recorded_at_raw"]),
            ("录制赛号", identity["raw_tournament_name"]), ("已确认主号", identity["personal_nickname"]), ("关联状态", _identity_status(identity["status"])),
            ("录制方结果", _outcome_label(model["recorder_outcome"])), ("结算胜方", _winner_label(model["winner_absolute_team"])),
            ("结果来源", model["result_source"]), ("结果说明", "；".join(model["result_diagnostic"]) or None)]
    body = '<section><h2>来源与场次</h2>' + _table(["项目", "记录"], meta)
    body += f'<p class="sha">SHA256：{_shown(src["sha256"])}</p><p class="note">{_h(model["notes"] or "")}</p></section>'
    body += '<section><h2>时间范围</h2><div class="cards">'
    for name, value in (("首次观测到的战斗时间", durations["first_observed_battle_elapsed_sec"]), ("最后观测到的战斗时间", durations["last_observed_battle_elapsed_sec"]), ("独立结算时长", durations["verified_result_duration_sec"])):
        body += f'<div class="card">{name}<strong>{_seconds(value)}</strong></div>'
    body += '</div><p class="muted">观测时间来自战斗计时器样本；结算时长独立保存，不能用于补齐未观测尾段。</p></section>'
    body += '<section><h2>记录位置</h2>' + _svg(model.get("map_snapshot")) + '</section>'
    if model.get("combat_summary"):
        summary = model["combat_summary"]
        body += '<section><h2>战斗图层与战术证据</h2>' + _table(["记录项目", "数量"], [
            ["本视角炮弹记录", summary["recorded_shell_count"]], ["本视角鱼雷记录", summary["recorded_torpedo_count"]],
            ["消耗品启用记录", summary["consumable_activations"]]])
        body += '<p class="muted">这些数量只反映收到的客户端记录，不代表完整射击、命中或双方战绩。示意动画坐标不进入战术事实。</p>'
        context = model.get("tactical_context") or {}
        states = [[row["raw_name"], row.get("hp"), row.get("max_hp"), row["visibility_label"], row["spotted_label"],
                   "、".join(item["label"] + (f"（报告剩余 {item['remaining_seconds']:.1f} 秒）" if item["remaining_seconds"] is not None else "（结束未知）")
                             for item in row["consumables"]) or "无活动记录"] for row in context.get("ships", [])]
        body += '<h3>所选时刻的记录状态</h3>' + _table(["赛号", "最后记录 HP", "最大 HP", "本视角观测", "敌方点亮记录", "消耗品"], states)
        evidence = context.get("events", [])
        body += '<h3>此前 180 秒的线索</h3>' + _table(["包时间", "记录", "证据 ID"],
            [[_seconds(event["clock"]), event["label"], event["id"]] for event in evidence[-250:]])
        body += '<p class="muted">HTML 展示该窗口最近 250 条；整局事件与来源保存在 JSON/CSV。HP 变化不归因给具体攻击者，失去观测不等于敌方熄灭。</p></section>'
    roster = [[p["raw_tournament_name"], p["personal_nickname"], _identity_status(p["status"]), _team(p["team"]),
               p["ship_name"] or (f'舰船 ID {p["ship_params_id"]}' if p["ship_params_id"] else None), p["ship_type"], p["ship_tier"],
               p["recorded_kills"], p["recorded_deaths"], p["notes"]] for p in model["participants"]]
    body += '<section><h2>基础阵容</h2>' + _table(["原始赛号", "已确认主号", "关联", "绝对队伍", "舰船", "舰种", "等级", "记录击沉", "记录被击沉", "关联备注"], roster)
    body += '<p class="muted">空白统计表示未记录对应事件，不是完整结算中的零值。比赛内实体 ID 不等同于个人公开账号 ID。</p></section>'
    death_rows = [[_seconds(e["packet_clock"]), _event_time_label(e["battle_time"]), e["killer_raw_name"], e["victim_raw_name"], e["source_packet_offset"]] for e in model["recorded_deaths"]]
    body += '<section><h2>记录击沉事件</h2>' + _table(["包时间", "比赛已过时间", "击沉方赛号", "被击沉方赛号", "来源包偏移"], death_rows) + '</section>'
    caps = [[_seconds(e["packet_clock"]), e["capture_name"], _team(e["previous_team"]), _team(e["new_team"])] for e in model["capture_team_changes"]]
    body += '<section><h2>控点归属变化</h2>' + _table(["包时间", "控点", "原归属", "新归属"], caps) + '<p class="muted">初始化归属不计为一次占领；未导出队伍总分。</p></section>'
    anchors = [[_seconds(e["packet_clock"]), STAGES.get(e["stage"], e["stage"]), e["duration_raw"], e["time_left_raw"]] for e in model["time_anchors"]]
    body += '<section><h2>时间锚点</h2>' + _table(["包时间", "阶段", "阶段 duration 原值", "阶段 timeLeft 原值"], anchors) + '</section>'
    pending = model["native_pending"]
    body += '<section><h2>保留的待办</h2><p>原生包与字段尚未明确的部分仍保留待办状态。</p>'
    body += _table(["类别", "原始记录"], [["未知包", ", ".join(pending["unknown_packet_types"]) or None], ["未完全解释的包", ", ".join(pending["opaque_packet_types"]) or None], ["解析限制", "；".join(model["limitations"]) or None]]) + '</section>'
    if model.get('multiview'):
        source_rows = [[s.get('recorder') or s.get('recorder_name'), s.get('sha256')] for s in model['multiview'].get('sources', [])]
        body = body.replace('包时间', '合并比赛计时').replace('本视角', '融合记录')
        body = '<section><h2>双边合并记录</h2><p>时间轴为比赛已进行秒，不是任何一份原录像的包时间。事件时刻按观测区间上界展示；融合可见不代表任一方当时视野。原始来源与时间区间保存在 JSON 及完整录像包。</p>' + _table(['录制来源', '原回放 SHA256'], source_rows) + '</section>' + body
    return _document(model["label"] or "单局基础赛训报告", body, generated)


def _csv_safe(value, field=None):
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")):
        return "'" + value
    if field in ("比赛标识", "公开账号ID", "舰船参数ID") and isinstance(value, str) and value.isdecimal() and (len(value) > 15 or len(value) > 1 and value.startswith("0")):
        return "'" + value  # Preserve long identifiers when opened in Excel.
    return value


def _csv_rows(model, include_events=True):
    source, duration = model["source"], model["durations"]
    common = {"赛事": model["event_key"], "比赛标识": source["arena_id"], "版本": source["version"], "地图": model["map_name"],
              "原始时间": source["recorded_at_raw"], "视角赛号": model["recorder"]["raw_tournament_name"], "SHA256": source["sha256"],
              "结算胜方": _winner_label(model["winner_absolute_team"]), "录制方结果": _outcome_label(model["recorder_outcome"]),
              "结果来源": model["result_source"]}
    rows = [{**common, "记录类型": "比赛", "首个观测战斗时间": duration["first_observed_battle_elapsed_sec"],
             "末个观测战斗时间": duration["last_observed_battle_elapsed_sec"], "结算时长": duration["verified_result_duration_sec"],
             "同场来源数": len(model["source_views"]), "备注": "；".join([model["notes"] or "", *model["conflicts"]]).strip("；") or None}]
    for p in model["participants"]:
        rows.append({**common, "记录类型": "选手", "原始赛号": p["raw_tournament_name"], "主号昵称": p["personal_nickname"],
                     "区域": p["region"], "公开账号ID": p["account_id"], "关联状态": _identity_status(p["status"]), "队伍": p["team"],
                     "舰船参数ID": p["ship_params_id"], "舰船": p["ship_name"], "舰种": p["ship_type"], "等级": p["ship_tier"],
                     "记录击沉数": p["recorded_kills"], "记录被击沉数": p["recorded_deaths"], "备注": p["notes"]})
    if include_events:
        rows += [{**common, "记录类型": "击沉", "事件包时间": e["packet_clock"], "比赛已过时间": _event_time_label(e["battle_time"]),
                  "原始赛号": e["killer_raw_name"], "对象赛号": e["victim_raw_name"], "事件类型": "记录击沉"} for e in model["recorded_deaths"]]
        rows += [{**common, "记录类型": "控点", "事件包时间": e["packet_clock"], "控点": e["capture_name"],
                  "旧归属": e["previous_team"], "新归属": e["new_team"], "事件类型": "记录归属变化"} for e in model["capture_team_changes"]]
        names = {str(player["entity_id"]): player["raw_tournament_name"] for player in model["participants"]}
        rows += [{**common, "记录类型": "战术证据", "事件包时间": event["clock"], "事件类型": event["kind"],
                  "原始赛号": names.get(str(event["entity_id"])), "备注": event["label"] + " | " + event["id"]}
                 for event in model.get("combat_events", [])]
    return rows


def _write_bundle(kind, payload, html, rows, output_dir):
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f'wows_{kind}_{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")}_{secrets.token_hex(3)}'
    paths = {suffix: output_dir / f"{stem}.{suffix}" for suffix in ("html", "csv", "json")}
    paths["html"].write_text(html, encoding="utf-8")
    paths["json"].write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    with paths["csv"].open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows({name: _csv_safe(row.get(name), name) for name in CSV_FIELDS} for row in rows)
    return {key: str(path) for key, path in paths.items()}


def export_single(report: dict, output_dir: Path, mappings: list[dict] | None = None,
                  ship_catalog: dict | None = None, clock: float | None = None) -> dict:
    if not isinstance(report, dict):
        raise TypeError("report must be a dictionary")
    if clock is not None and _number(clock) is None:
        raise ValueError("clock must be a finite packet-clock number")
    generated = datetime.now(timezone.utc).isoformat()
    model = _model(report, mappings, ship_catalog, clock)
    payload = {"format_version": 1, "kind": "single", "generated_at_utc": generated, "battle": model}
    return _write_bundle("single", payload, _render_single(model, generated), _csv_rows(model), output_dir)


def export_summary(reports: list[dict], output_dir: Path, mappings=None, ship_catalog=None) -> dict:
    if not reports or any(not isinstance(report, dict) for report in reports):
        raise ValueError("Choose at least one valid report")
    groups = {}
    for index, report in enumerate(reports):
        version, arena, sha = _version(report), _arena(report), _text(report.get("sha256"))
        key = ("arena", version, arena) if version and arena else ("sha256", sha) if sha else ("unidentified", index)
        groups.setdefault(key, []).append(report)
    models = []
    for group in groups.values():
        def quality(report):
            return (report.get("roster_matches_header") is True, report.get("winner_source") == "verified_15.9_common_results",
                    (_text(report.get("event_key")) not in ("", "unassigned")),
                    _number(((report.get("time_analysis") or {}).get("coverage") or {}).get("last_observed_elapsed_sec")) or -1,
                    len(report.get("roster") or []))
        selected = max(group, key=quality)
        model = _model(selected, mappings, ship_catalog, snapshot=False)
        views, seen = [], set()
        for report in group:
            view = _model(report, mappings, ship_catalog, snapshot=False)
            fingerprint = view["source"]["sha256"] or f"missing:{id(report)}"
            if fingerprint not in seen:
                seen.add(fingerprint)
                views.extend(view["source_views"])
        model["source_views"] = views
        conflicts = []
        if len({_text((r.get("metadata") or {}).get("mapName")) for r in group} - {""}) > 1:
            conflicts.append("同场来源地图不一致")
        winner_values = {v for r in group for v in ((r.get("post_battle_common") or {}).get("winner_team_id"), r.get("winner_absolute_team")) if type(v) is int and v in (-1, 0, 1)}
        if len(winner_values - {None}) > 1:
            conflicts.append("同场结算胜方冲突")
        if any(r.get("post_battle_result_errors") or r.get("winner_source") == "conflict" for r in group):
            conflicts.append("某来源存在结算冲突或错误")
        if len({_text(r.get("event_key")) for r in group} - {"", "unassigned"}) > 1:
            conflicts.append("同场赛事分类冲突")
        rosters = {tuple(sorted((_text(p.get("name")), _id(p.get("shipParamsId")) or "") for p in r.get("roster") or [] if isinstance(p, dict))) for r in group}
        if len(rosters - {()}) > 1:
            conflicts.append("同场阵容记录不一致")
        if any("结算" in conflict or "地图" in conflict or "阵容" in conflict for conflict in conflicts):
            model["winner_absolute_team"] = None
            model["recorder_outcome"] = "unknown"
            model["result_source"] = None
            model["result_diagnostic"].append("同场来源存在冲突；不选择一方输出胜负结论")
            for view in model["source_views"]:
                view.update(winner_absolute_team=None, recorder_outcome="unknown", result_source=None)
        model["conflicts"] = conflicts
        models.append(model)
    generated = datetime.now(timezone.utc).isoformat()
    payload = {"format_version": 1, "kind": "summary", "generated_at_utc": generated,
               "input_report_count": len(reports), "unique_battle_count": len(models),
               "deduplication_policy": "Complete version plus arena; SHA256 fallback when identity is missing. One representative view; event lists are not added across views.",
               "battles": models}
    body = f'<section><h2>范围</h2><p>{len(reports)} 份输入报告，按版本与 arena 去重后为 {len(models)} 个比赛记录。</p><p class="muted">每场使用一份代表视角，保留其他来源 SHA256；不把多视角事件重复相加。短局或重开仍是输入记录，不自动归类为正式比赛。</p></section>'
    rows = []
    for model in models:
        rows.append([model["event_key"], model["source"]["arena_id"], model["source"]["version"], model["map_name"],
                     model["source"]["recorded_at_raw"], len(model["participants"]) or None,
                     len(model["recorded_deaths"]) or None, len(model["capture_team_changes"]) or None,
                     _seconds(model["durations"]["last_observed_battle_elapsed_sec"]), _seconds(model["durations"]["verified_result_duration_sec"]),
                     model["recorder"]["raw_tournament_name"], _outcome_label(model["recorder_outcome"]), _winner_label(model["winner_absolute_team"]),
                     len(model["source_views"]), "；".join(model["conflicts"]) or None])
    body += '<section><h2>比赛基础汇总</h2>' + _table(["赛事", "arena", "版本", "地图", "原始时间", "阵容记录", "击沉记录", "控点变化", "最后观测战斗时间", "结算时长", "代表视角赛号", "录制方结果", "结算胜方", "来源数", "冲突"], rows) + '</section>'
    ship_counts = Counter(p["ship_type"] for m in models for p in m["participants"] if p["ship_type"])
    body += '<section><h2>舰种出场记录</h2>' + _table(["已识别舰种", "出场记录数"], sorted(ship_counts.items())) + '<p class="muted">仅统计去重后代表视角名单中已识别的舰种。</p></section>'
    source_rows = [[m["source"]["arena_id"], v["version"], v["recorder_raw_name"], _outcome_label(v["recorder_outcome"]), v["result_source"], v["sha256"]] for m in models for v in m["source_views"]]
    body += '<section><h2>来源</h2>' + _table(["arena", "版本", "录制赛号", "该视角结果", "结算来源", "SHA256"], source_rows) + '</section>'
    roster_rows = [[m["source"]["arena_id"], p["raw_tournament_name"], p["personal_nickname"], _identity_status(p["status"]), p["ship_type"], p["notes"]] for m in models for p in m["participants"]]
    body += '<section><h2>选手关联与出场</h2>' + _table(["arena", "原始赛号", "已确认主号", "关联状态", "舰种", "关联备注"], roster_rows) + '</section>'
    body += '<section><h2>能力边界</h2><p>缺少身份、事件和舰种时保留未记录；未验证伤害、队伍总分和原生字段不会转换为成绩结论。</p></section>'
    csv_rows = [row for model in models for row in _csv_rows(model, include_events=False)]
    return _write_bundle("summary", payload, _document("赛训基础汇总报告", body, generated), csv_rows, output_dir)
