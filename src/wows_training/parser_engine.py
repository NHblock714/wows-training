"""P0 replay probe with exact schema provenance and explicit experimental semantics.

Run one replay per process: the legacy collector uses class-level subscriptions.
No application UI, network client, or old static ship database is loaded.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
from io import BytesIO
import json
import logging
import math
from pathlib import Path
import statistics
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from wows_training.contracts import (
    decode_minimap, outcome, packet_frames, parse_version,
    position_status, require_exact_version,
    classify_probe, verify_schema_manifest,
)
from wows_training.player_info import ClientPlayersInfo, load_player_schema
from wows_training.battle_results import decode_common_result, load_result_schema
from wows_training.evidence import BattleLogicEvidence, validate_client_archive_evidence, validate_native_executable, standard_map_scope
from wows_training.packets import decode_packet, UPSTREAM_COMMIT
from wows_training.timebase import analyze_timebase, load_time_schema, packet_to_battle_time
from wows_training.geometry import load_geometry_schema, resolve_playable_bounds, normalize_world, contains_world
from wows_training.background import load_background_schema, background_bounds, world_to_png, png_to_world, viewport_from_configured_bounds, png_crop_for_viewport
from wows_training.units import load_units_schema, engine_distance_to_meters
from wows_training.packet_semantics import load_packet_semantics, annotate_packet
from wows_training.replay_battle_state import BATTLE_STATE_SCHEMA_VERSION, capture_logic_fields, score_limit_fields


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * fraction))]


def probe(args):
    started = time.perf_counter()
    portable = getattr(args, "portable_bundle", None)
    if portable is not None:
        from wows_training.runtime_bundle import load_bundle
        bundle = load_bundle(Path(portable))
        sys.path.insert(0, str(bundle.root / "legacy"))
    else:
        bundle = None
        sys.path.insert(0, str(Path(args.legacy_root).resolve()))
    from replay.parser import TimelineCollectorPlayer
    from wows_training.container import read_replay
    from replay_unpack.core.entity_def.definitions import Definitions
    from replay_unpack.clients.wows.helper import get_controller
    from replay_unpack.clients.wows.network.packets import Map, BattleStats, EntityCreate, Position

    path = Path(args.replay).resolve()
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Replay exceeds the P0 64 MiB input limit")
    data = read_replay(path)
    header = data.engine_data
    version_string = header.get("clientVersionFromExe", "")
    if bundle is not None:
        require_exact_version(version_string, bundle.version)
        schema = bundle.root / "resources/schema"
        resource_manifest = bundle.json("resource_manifest.json")
        schema_provenance = bundle.provenance("resources/schema/scripts/entities.xml")
        player_mappings, player_schema_provenance = load_player_schema(bundle.path("player_schema.json"), bundle.path("resource_manifest.json"))
        result_names, result_schema_provenance = load_result_schema(bundle.path("result_schema.json"), bundle.path("resource_manifest.json"))
        stages, time_schema_provenance = load_time_schema(bundle.path("time_schema.json"), bundle.path("resource_manifest.json"))
        geometry_profile = load_geometry_schema(bundle.path("geometry_schema.json"), bundle.version)
        geometry_schema_provenance = bundle.provenance("geometry_schema.json")
        background_profile = load_background_schema(bundle.path("background_schema.json"), bundle.version)
        background_schema_provenance = bundle.provenance("background_schema.json")
        native_provenance = bundle.native_provenance()
        units_profile = load_units_schema(bundle.path("units_schema.json"), bundle.version,
                                         expected_executable_sha256=native_provenance["sha256"],
                                         expected_archive_sha256=resource_manifest["scripts_zip_audit"]["sha256"])
        units_schema_provenance = bundle.provenance("units_schema.json")
        semantics_profile, semantics_schema_provenance = load_packet_semantics(bundle.path("packet_semantics.json"), bundle.path("resource_manifest.json"))
        bundle.relocate_background(background_profile)
        map_id = str(header.get('mapName') or '').rsplit('/',1)[-1]
        if map_id not in background_profile['maps']:
            from .ordinary_replay import extra_map_record
            extra = extra_map_record(map_id)
            if extra is not None:
                background_profile['maps'][map_id] = extra
        args.controller_version = "15_3_0"
    elif args.schema_root:
        require_exact_version(version_string, args.resource_version)
        schema = Path(args.schema_root).resolve()
        schema_provenance = verify_schema_manifest(schema, Path(args.resource_manifest), version_string)
        player_mappings, player_schema_provenance = load_player_schema(Path(args.player_schema), Path(args.resource_manifest))
        result_names, result_schema_provenance = load_result_schema(Path(args.result_schema), Path(args.resource_manifest))
        stages, time_schema_provenance = load_time_schema(Path(args.time_schema), Path(args.resource_manifest))
        _, geometry_schema_provenance = validate_client_archive_evidence(Path(args.geometry_schema), Path(args.resource_manifest))
        geometry_profile = load_geometry_schema(Path(args.geometry_schema), args.resource_version)
        resource_manifest = json.loads(Path(args.resource_manifest).read_text(encoding="utf-8"))
        _, background_schema_provenance = validate_client_archive_evidence(Path(args.background_schema), Path(args.resource_manifest))
        background_profile = load_background_schema(Path(args.background_schema), args.resource_version)
        native_provenance = validate_native_executable(Path(background_profile["source_executable"]),
                                                       background_profile["source_executable_sha256"], resource_manifest)
        _, units_schema_provenance = validate_client_archive_evidence(Path(args.units_schema), Path(args.resource_manifest))
        units_profile = load_units_schema(Path(args.units_schema), args.resource_version,
                                         expected_executable_sha256=native_provenance["sha256"],
                                         expected_archive_sha256=resource_manifest["scripts_zip_audit"]["sha256"])
        for key, provenance in (("background_binding", background_schema_provenance), ("units_binding", units_schema_provenance)):
            if geometry_profile.get(key, {}).get("sha256") != provenance["sha256"]:
                raise ValueError(f"Geometry profile uses stale {key} evidence")
        semantics_profile, semantics_schema_provenance = load_packet_semantics(Path(args.packet_semantics), Path(args.resource_manifest))
        if semantics_profile.get("native_camera_word", {}).get("executable_sha256") != native_provenance["sha256"]:
            raise ValueError("Camera semantics and geometry/unit evidence use different executables")
        gameparam_sources = [r for r in resource_manifest["resources"] if r["category"] == "game_params_raw"]
        if len(gameparam_sources) != 1 or gameparam_sources[0]["sha256"] != geometry_profile["gameparams_source"]["sha256"]:
            raise ValueError("Geometry and entity resources reference different GameParams payloads")
    else:
        import replay_unpack.clients.wows.helper as helper
        ver = parse_version(version_string)
        schema = Path(helper.BASE_DIR) / "versions" / "_".join(map(str, ver[:3]))
        if not schema.is_dir():
            raise ValueError(f"No exact bundled schema for {version_string}; fallback disabled")
        if args.controller_version is None:
            args.controller_version = "_".join(map(str, ver[:3]))
        schema_provenance = {"source": "exact bundled version; dependency commit recorded separately"}
        player_mappings, player_schema_provenance = None, {"source": "bundled exact-version mappings"}
        result_names, result_schema_provenance = None, None
        stages, time_schema_provenance, geometry_profile, geometry_schema_provenance = None, None, None, None
        background_profile = background_schema_provenance = units_profile = units_schema_provenance = None
        semantics_profile = semantics_schema_provenance = native_provenance = None
    if not (schema / "scripts/entities.xml").is_file():
        raise ValueError(f"Missing exact schema: {schema / 'scripts/entities.xml'}")
    controller_version = args.controller_version or "15_3_0"
    errors = Counter()
    error_examples = []
    packet_counts = Counter()
    unknown_packets = Counter()
    decoded_additional_packets = Counter()
    opaque_packets = Counter()
    effective_opaque_packets = Counter()
    semantic_annotations = {}
    packet_layout_evidence = {}
    swallowed = Counter()
    world_updates = {}
    observed_errors = []
    arena_packets = []
    map_layouts = []
    raw_result_packets = []
    result_errors = []
    post_battle_common = None
    snapshots = {}
    wire_minimap_events = Counter()
    unread_payloads = Counter()
    stopped_early = False
    logic_evidence = BattleLogicEvidence()
    logic_entity = None
    packet_clock_min = None
    packet_clock_max = None
    current_frame = None
    death_sources = []
    cap_states = {}
    score_limits = []

    class Collector(TimelineCollectorPlayer):
        def _get_definitions(self, version):
            return Definitions(str(schema))

        def _get_controller(self, version):
            controller = get_controller(controller_version)
            if player_mappings is not None:
                controller._players = ClientPlayersInfo(player_mappings)
            return controller

        def _on_minimap_vision_info(self, arr):
            # Keep exact wire fields and avoid swallowing invalid values.
            for update in arr or []:
                vid = int(update.get("vehicleID", 0))
                packed = int(update.get("packedData", 0))
                decoded = decode_minimap(packed)
                wire_minimap_events["total"] += 1
                if decoded["sentinel"]:
                    wire_minimap_events["sentinel"] += 1
                    continue
                visible = decoded["wire_visible_candidate"]
                if decoded["disappearing"]:
                    wire_minimap_events["disappearing"] += 1
                x, z = decoded["x_engine"], decoded["z_engine"]
                heading = decoded["heading_radians"]
                previous = self.minimap_pos.get(vid)
                if not visible and previous:
                    x, z, heading = previous[:3]
                self.minimap_pos[vid] = (x, z, heading, visible, self.current_clock)

        def _on_vehicle_death(self, killed, fragger, type_id):
            super()._on_vehicle_death(killed, fragger, type_id)
            death_sources.append({"packet_offset": current_frame.offset, "packet_type": current_frame.type,
                                  "packet_clock": current_frame.clock})

        def record_cap_state(self, entity):
            if entity is None or entity.id not in self.cap_zones:
                return
            props = entity.properties.get("client", {})
            components = props.get("componentsState")
            logic = components.get("captureLogic") if isinstance(components, dict) else None
            fields = capture_logic_fields(logic if isinstance(logic, dict) else {})
            control = components.get("controlPoint") if isinstance(components, dict) else None
            enabled = control.get('isEnabled') if isinstance(control, dict) else None
            fields['capture_enabled'] = bool(enabled) if type(enabled) in (bool,int) and enabled in (0,1) else None
            rows = cap_states.setdefault(str(entity.id), [])
            if len(rows) < 10_000 and (not rows or any(rows[-1][key] != value for key, value in fields.items())):
                rows.append({"packet_clock":current_frame.clock, **fields})

        def _on_cap_logic_change(self, entity):
            super()._on_cap_logic_change(entity)
            self.record_cap_state(entity)

        def _snapshot(self, clock):
            # Record missing HP as None, and source age independently of visibility.
            for eid, entity in self._battle_controller.entities.items():
                if entity.get_name() != "Vehicle":
                    continue
                mm = self.minimap_pos.get(eid)
                world = world_updates.get(eid)
                if mm is None and world is None:
                    continue
                props = entity.properties.get("client", {})
                row = {"clock": clock, "hp": props.get("health"),
                       "is_alive": props.get("isAlive"),
                       "visibility_flags_raw": props.get("visibilityFlags"),
                       "world": world,
                       "minimap": None if mm is None else {
                           "x_engine": mm[0], "z_engine": mm[1],
                           "heading_radians": mm[2], "wire_visible_candidate": mm[3],
                           "last_update_clock": mm[4],
                           "position_status": position_status(mm[3], mm[4], clock)}}
                snapshots.setdefault(eid, []).append(row)
                # Compare only recently updated pairs; do not infer unseen movement.
                if mm and world and mm[3] and 0 <= clock - mm[4] < 0.2 and 0 <= clock - world["clock"] < 0.2:
                    observed_errors.append(math.hypot(world["x_engine"] - mm[0], world["z_engine"] - mm[1]))

    collector = Collector(tuple(map(str, parse_version(version_string))))
    combat_capture = None
    statistics_capture = None
    if bundle is not None:
        from wows_training.combat_capture import CombatCapture
        from replay_unpack.core import Entity
        combat_capture = CombatCapture(profile=bundle.json("combat_schema.json"))
        combat_capture.attach(Entity)
        if 'statistics_schema.json' in bundle.manifest.get('files',{}):
            from wows_training.statistics_capture import StatisticsCapture,load_profile
            statistics_capture=StatisticsCapture(load_profile(bundle)).attach(Entity)

    def trace_callback(frame, event, arg):
        if event == "exception":
            exc_type, value, _ = arg
            if exc_type not in (StopIteration, GeneratorExit):
                name = frame.f_code.co_name
                key = f"{name}:{exc_type.__name__}:{str(value)[:180]}"
                swallowed[key] += 1
        return trace_callback

    def trace_calls(frame, event, arg):
        if event == "call" and frame.f_code.co_filename.endswith("replay\\parser.py"):
            if frame.f_code.co_name.startswith(("_on_", "_maybe_")):
                return trace_callback
        return None

    framing_error = None
    sys.settrace(trace_calls)
    try:
        for frame in packet_frames(data.decrypted_data):
            current_frame = frame
            if combat_capture is not None:
                combat_capture.begin_packet(frame)
            if statistics_capture is not None:
                statistics_capture.begin_packet(frame)
            packet_clock_min = frame.clock if packet_clock_min is None else min(packet_clock_min, frame.clock)
            packet_clock_max = frame.clock if packet_clock_max is None else max(packet_clock_max, frame.clock)
            packet_counts[f"0x{frame.type:02x}"] += 1
            factory = collector._mapping.get(frame.type)
            layout = None
            if factory is None or frame.type == 0x28:
                layout = decode_packet(frame.type, frame.payload, version_string)
                if semantics_profile is not None and frame.type in (0x25, 0x27, 0x30, 0x32, 0x33):
                    layout = annotate_packet(layout, semantics_profile, replay_version=version_string)
                kind_key = f"0x{frame.type:02x}"
                classification = layout["classification"]
                counters = {"decoded": decoded_additional_packets, "known_opaque": opaque_packets, "unknown": unknown_packets}
                counters[classification][kind_key] += 1
                semantic_status = layout.get("semantics", {}).get("status", "not_applicable")
                if "semantics" in layout:
                    counts = semantic_annotations.setdefault(kind_key, {})
                    counts[semantic_status] = counts.get(semantic_status, 0) + 1
                if classification == "known_opaque" and semantic_status != "verified":
                    effective_opaque_packets[kind_key] += 1
                evidence = packet_layout_evidence.setdefault(kind_key, {
                    "name": layout["name"], "counts": {}, "payload_lengths": {},
                    "sources": layout["sources"], "examples": [], "last_occurrence": None})
                evidence["counts"][classification] = evidence["counts"].get(classification, 0) + 1
                length_key = str(len(frame.payload))
                evidence["payload_lengths"][length_key] = evidence["payload_lengths"].get(length_key, 0) + 1
                occurrence = {"packet_offset": frame.offset, "packet_clock": frame.clock,
                              "payload_length": len(frame.payload), "payload_sha256": layout["payload_sha256"]}
                if len(evidence["examples"]) < 2:
                    evidence["examples"].append(occurrence | {"decoded": layout})
                evidence["last_occurrence"] = occurrence
            if factory is None:
                continue
            try:
                payload_stream = BytesIO(frame.payload)
                packet = factory(payload_stream)
                remaining = len(frame.payload) - payload_stream.tell()
                complete_map_layout = (frame.type == 0x28 and layout is not None
                                       and layout["classification"] != "unknown"
                                       and layout["consumed_bytes"] == len(frame.payload))
                if remaining and not complete_map_layout:
                    unread_payloads[f"0x{frame.type:02x}:{remaining}"] += 1
                if isinstance(packet, Map):
                    if complete_map_layout and layout["fields"]["map_name"] != packet.name:
                        raise ValueError("Independent Map decoders disagree on map identity")
                    arena_packets.append({"clock": frame.clock, "legacy_map_arena_field_raw": str(packet.arenaId), "map": packet.name})
                    if complete_map_layout:
                        map_layouts.append({"packet_offset": frame.offset, "packet_clock": frame.clock,
                                            "fields": layout["fields"]})
                if isinstance(packet, BattleStats):
                    raw_result_packets.append({"clock": frame.clock, "present": True})
                    if result_names is not None:
                        try:
                            common = decode_common_result(packet.serverData, result_names, collector._battle_controller._arena_id)
                            if post_battle_common is not None and common != post_battle_common:
                                raise ValueError("Conflicting post-battle common results")
                            post_battle_common = common
                            if statistics_capture is not None:
                                try:statistics_capture.observe_result(packet.serverData,common)
                                except Exception as error:statistics_capture.errors.append({'kind':'result_decode','message':str(error),'source':dict(statistics_capture.source)})
                        except ValueError as exc:
                            result_errors.append(str(exc))
                if isinstance(packet, Position):
                    world_updates[packet.entityId] = {
                        "clock": frame.clock, "x_engine": packet.position.x, "z_engine": packet.position.z}
                if type(packet).__name__ == "_PlayerOrientationPacket" and packet.entity_id2 == 0:
                    if packet.entity_id1 and not (abs(packet.x) < .01 and abs(packet.z) < .01 and abs(packet.yaw) < .001):
                        world_updates[packet.entity_id1] = {
                            "clock": frame.clock, "x_engine": packet.x, "z_engine": packet.z}
                collector._process_packet(frame.clock, packet)
                if isinstance(packet, EntityCreate) and packet.entityID in collector.cap_zones:
                    collector.record_cap_state(collector._battle_controller.entities.get(packet.entityID))
                if combat_capture is not None:
                    combat_capture.after_packet(packet, collector._battle_controller.entities)
                if logic_entity is None:
                    logic_entity = next((e for e in collector._battle_controller.entities.values()
                                         if e.get_name() == "BattleLogic"), None)
                if logic_entity is not None:
                    props = logic_entity.properties.get("client", {})
                    logic_evidence.observe(logic_entity.id, props,
                                           frame.clock, frame.offset, frame.type)
                    state = props.get("state")
                    missions = state.get("missions") if isinstance(state, dict) else None
                    if isinstance(missions, dict):
                        fields = score_limit_fields(missions)
                        if not score_limits or any(score_limits[-1][key] != value for key, value in fields.items()):
                            score_limits.append({"packet_clock":frame.clock, **fields})
            except Exception as exc:
                key = f"0x{frame.type:02x}:{type(exc).__name__}:{str(exc)[:180]}"
                errors[key] += 1
                if len(error_examples) < 12:
                    error_examples.append({"offset": frame.offset, "clock": frame.clock,
                                           "type": frame.type, "error": key,
                                           "traceback": traceback.format_exc(limit=7)})
                if args.fail_fast:
                    stopped_early = True
                    break
    except (ValueError, EOFError) as exc:
        framing_error = str(exc)
    finally:
        sys.settrace(None)
        if combat_capture is not None:
            combat_capture.detach()
        if statistics_capture is not None:
            statistics_capture.detach()

    bc = collector._battle_controller
    players = bc._players.get_info()
    roster = []
    for avatar_id, p in players.items():
        roster.append({"avatar_id": avatar_id, **{k: p.get(k) for k in (
            "name", "accountDBID", "shipId", "shipParamsId", "teamId", "maxHealth", "_player_kind")}})
    participants = [p for p in roster if p.get("_player_kind") != 3 and p.get("shipParamsId")]
    observers = [p for p in roster if p.get("_player_kind") == 3]
    combat = combat_capture.finish(participants, packet_clock_max or 0.0) if combat_capture is not None else None
    if combat is not None:
        combat["provenance"]["profile"] = bundle.provenance("combat_schema.json")
    header_roster = {(p.get("name"), p.get("shipId")) for p in header.get("vehicles", [])}
    parsed_roster = {(p.get("name"), p.get("shipParamsId")) for p in participants}
    host = next((p for p in players.values() if p.get("name") == header.get("playerName")), {})
    result = bc._battle_result or {}
    winner = result.get("winner_team_id")
    winner_source = "onBattleEnd" if winner in (-1, 0, 1) else None
    if post_battle_common:
        final_winner = post_battle_common["winner_team_id"]
        if winner in (-1, 0, 1) and winner != final_winner:
            result_errors.append("Battle-end event conflicts with post-battle result")
            winner, winner_source = None, "conflict"
        else:
            winner, winner_source = final_winner, "verified_15.9_common_results"
    try:
        logic_properties = bc.battle_logic.properties.get("client", {})
        battle_logic_result = logic_properties.get("battleResult")
        map_geometry = {k: v for k, v in logic_properties.items() if any(token in k.lower() for token in ("mapborder", "mapsize", "mapinfo"))}
    except (StopIteration, KeyError):
        battle_logic_result = None
        map_geometry = {}
    track_summaries = []
    for eid, rows in snapshots.items():
        states = Counter(r["minimap"]["position_status"] if r["minimap"] else "unknown" for r in rows)
        track_summaries.append({"entity_id": eid, "samples": len(rows),
                                "minimap_state_samples": dict(states),
                                "first": rows[0], "middle": rows[len(rows)//2], "last": rows[-1]})
    time_analysis = None
    normalized_death_times = []
    geometry_analysis = None
    if stages is not None:
        time_analysis = analyze_timebase(logic_evidence.time_samples,
                                         result_duration_sec=post_battle_common.get("duration_sec") if post_battle_common else None,
                                         packet_clock_max=packet_clock_max, stage_values=stages)
        for event, source in zip(collector.death_events, death_sources):
            normalized_death_times.append({"event": asdict(event), "source": source,
                                          "battle_time": packet_to_battle_time(time_analysis, event.clock,
                                                                               packet_offset=source["packet_offset"])})
    if geometry_profile is not None:
        periods, geometry_errors = [], []
        for sample in logic_evidence.border_samples:
            try:
                bounds = resolve_playable_bounds(geometry_profile, sample["mapBorder"])
                periods.append({"source": sample, "bounds": bounds})
            except (ValueError, TypeError, KeyError) as exc:
                geometry_errors.append({"source": sample, "error": str(exc)})
        normalized_caps = []
        png_projection = None
        projected_tracks = []
        observed_positions = outside_positions = 0
        if len(periods) == 1 and not geometry_errors:
            bounds = periods[0]["bounds"]
            for cap in collector.cap_zones.values():
                normalized_caps.append({"name": cap.name, "entity_id": cap.entity_id,
                                        "x_engine": cap.x, "z_engine": cap.z,
                                        "radius_engine": cap.radius,
                                        "configured_canvas_uv": normalize_world(cap.x, cap.z, bounds),
                                        "center_within_configured_bounds": contains_world(cap.x, cap.z, bounds)})
            for rows in snapshots.values():
                for row in rows:
                    mm = row["minimap"]
                    if mm is not None and mm["position_status"] == "observed":
                        observed_positions += 1
                        outside_positions += not contains_world(mm["x_engine"], mm["z_engine"], bounds)
            map_name = str(header.get("mapName", "")).rsplit("/", 1)[-1]
            standard_mapping = standard_map_scope(map_name, packet_counts.get("0x28", 0), arena_packets, map_layouts,
                                                   stream_complete=framing_error is None and not stopped_early,
                                                   map_error_count=sum(count for key, count in errors.items() if key.startswith("0x28:")))
            if standard_mapping:
                try:
                    texture = background_bounds(background_profile, map_name)
                    viewport = viewport_from_configured_bounds(background_profile, map_name, bounds)
                    record = background_profile["maps"][map_name]
                    for category, source_path in record["source_paths"].items():
                        if hashlib.sha256(Path(source_path).read_bytes()).hexdigest() != record["source_hashes"][category]:
                            raise ValueError(f"Map source hash changed: {category}")
                    round_trip_errors = []
                    for cap in normalized_caps:
                        px, py = world_to_png(cap["x_engine"], cap["z_engine"], background_profile, map_name)
                        recovered = png_to_world(px, py, background_profile, map_name)
                        round_trip_errors.append(math.hypot(recovered[0] - cap["x_engine"], recovered[1] - cap["z_engine"]))
                        cap["png_pixel_xy"] = [px, py]
                        cap["radius_display_meters"] = engine_distance_to_meters(units_profile, cap["radius_engine"])
                    for eid, rows in snapshots.items():
                        segments, segment, previous_clock = [], [], None
                        for row in rows:
                            mm = row["minimap"]
                            if mm is None or mm["position_status"] != "observed":
                                if segment:
                                    segments.append(segment)
                                segment, previous_clock = [], None
                                continue
                            if previous_clock is not None and row["clock"] - previous_clock > 2.0:
                                if segment:
                                    segments.append(segment)
                                segment = []
                            px, py = world_to_png(mm["x_engine"], mm["z_engine"], background_profile, map_name)
                            segment.append([row["clock"], mm["x_engine"], mm["z_engine"], px, py])
                            previous_clock = row["clock"]
                        if segment:
                            segments.append(segment)
                        if segments:
                            projected_tracks.append({"entity_id": eid, "columns": ["packet_clock", "x_engine", "z_engine", "png_x", "png_y"],
                                                     "segments": segments})
                    png_projection = {"status": "source_chain_verified", "mapping_scope": "single_identity_mapping",
                                      "map_id": map_name, "texture_bounds_engine": texture,
                                      "viewport_bounds_engine": viewport,
                                      "viewport_crop_pixels": png_crop_for_viewport(background_profile, map_name, bounds),
                                      "dimensions_pixels": record["minimap_dimensions_pixels"],
                                      "source_paths": record["source_paths"], "source_hashes": record["source_hashes"],
                                      "width_display_meters": engine_distance_to_meters(units_profile, texture["max_x"] - texture["min_x"], coordinate_kind="map_extent"),
                                      "height_display_meters": engine_distance_to_meters(units_profile, texture["max_z"] - texture["min_z"], coordinate_kind="map_extent"),
                                      "cap_round_trip_max_error_engine": max(round_trip_errors, default=None),
                                      "independent_screenshot_validation": False,
                                      "alpha_policy": "land RGBA over water; never flatten hidden transparent RGB"}
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    geometry_errors.append({"stage": "background_projection", "error": str(exc)})
            else:
                geometry_errors.append({"stage": "background_projection", "error": "Single identity map packet was not verified"})
        geometry_analysis = {
            "scope": "Pinned native max/min order, grid/PNG extent, GUI transform and gameplay-distance exports",
            "periods": periods, "errors": geometry_errors,
            "normalized_capture_centers": normalized_caps,
            "observed_minimap_samples_checked": observed_positions,
            "observed_minimap_samples_outside_configured_bounds": outside_positions,
            "png_alignment_verified": png_projection is not None,
            "png_projection": png_projection, "projected_tracks": projected_tracks,
            "meters_per_engine_unit": units_profile["world_distance"]["meters_per_engine_unit"],
            "distance_domain": "client_displayed_world_distance",
            "decoded_minimap_step_display_meters": engine_distance_to_meters(units_profile, 5000.0 / 2047.0, coordinate_kind="decoded_minimap_position"),
            "comparison_policy": "Preserve outside points; split unobserved trajectory gaps; no unsupported mapping transform or unit domain"}
    status, limitations = classify_probe(
        packet_errors=sum(errors.values()), callback_errors=sum(swallowed.values()),
        framing_error=framing_error, roster_matches=header_roster == parsed_roster,
        track_count=len(snapshots), battle_end=bc._battle_result is not None or post_battle_common is not None,
        unknown_packets=sum(unknown_packets.values()), unread_payloads=sum(unread_payloads.values()),
        stopped_early=stopped_early, opaque_packets=sum(effective_opaque_packets.values()))
    unknown_player_fields = getattr(bc._players, "unknown_fields", [])
    if unknown_player_fields:
        status = "partial"
        limitations.append("unknown_arena_player_fields")
    if result_errors:
        status = "partial"
        limitations.append("post_battle_result_errors")
    schema_digest = hashlib.sha256()
    for file in sorted((schema / "scripts").rglob("*")):
        if file.is_file():
            schema_digest.update(file.relative_to(schema).as_posix().encode())
            schema_digest.update(file.read_bytes())
    return {
        "replay": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "replay_version": version_string, "schema_root": str(schema),
        "schema_tree_sha256": schema_digest.hexdigest(),
        "semantic_controller": controller_version,
        "player_schema_provenance": player_schema_provenance,
        "result_schema_provenance": result_schema_provenance,
        "time_schema_provenance": time_schema_provenance,
        "geometry_schema_provenance": geometry_schema_provenance,
        "background_schema_provenance": background_schema_provenance,
        "units_schema_provenance": units_schema_provenance,
        "native_executable_provenance": native_provenance,
        "packet_semantics_provenance": semantics_schema_provenance,
        "unknown_player_fields": unknown_player_fields,
        "status": status, "limitations": limitations,
        "schema_provenance": schema_provenance,
        "container_diagnostics": data.diagnostics,
        "support_claim": "P0 probe only; not validated for tournament or complete semantic coverage",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "metadata": {k: header.get(k) for k in (
            "mapName", "mapDisplayName", "mapId", "gameMode", "gameType", "matchGroup",
            "scenario", "playersPerTeam", "dateTime", "playerName", "playerVehicle")},
        "arena_id": str(bc._arena_id) if bc._arena_id is not None else None,
        "map_packets": arena_packets,
        "packet_counts": dict(packet_counts), "unknown_packet_counts": dict(unknown_packets),
        "additional_decoded_packet_counts": dict(decoded_additional_packets),
        "known_opaque_packet_counts": dict(opaque_packets),
        "effective_opaque_packet_counts": dict(effective_opaque_packets),
        "packet_semantic_annotation_counts": semantic_annotations,
        "packet_layout_evidence": packet_layout_evidence,
        "packet_layout_reference_commit": UPSTREAM_COMMIT,
        "unread_packet_payloads": dict(unread_payloads),
        "packet_errors": dict(errors), "error_examples": error_examples,
        "handled_callback_exceptions": dict(swallowed), "framing_error": framing_error,
        "header_roster_count": len(header.get("vehicles", [])),
        "parsed_roster_count": len(participants), "roster_matches_header": header_roster == parsed_roster,
        "roster": participants, "observers": observers, "host_absolute_team": host.get("teamId"),
        "battle_logic_result_raw": battle_logic_result,
        "map_geometry_raw": map_geometry,
        "battle_logic_time_samples": logic_evidence.time_samples,
        "map_border_samples": logic_evidence.border_samples,
        "packet_clock_range": {"min": packet_clock_min, "max": packet_clock_max},
        "time_analysis": time_analysis,
        "normalized_death_times": normalized_death_times,
        "geometry_analysis": geometry_analysis,
        "winner_absolute_team": winner, "outcome": outcome(winner, host.get("teamId")),
        "winner_source": winner_source, "post_battle_common": post_battle_common,
        "post_battle_result_errors": result_errors,
        "duration_clock": collector.last_clock, "battle_end_event": bc._battle_result is not None,
        "battle_stats_packet_count": len(raw_result_packets),
        "post_battle_semantics": "common results validated against exact client schema; private/public statistics unvalidated",
        "death_events": [asdict(x) for x in collector.death_events],
        "capture_points": [asdict(x) for x in collector.cap_zones.values()],
        "cap_team_changes": collector.cap_team_change_samples,
        "team_score_samples": collector.team_score_samples,
        "team_score_limit_samples": score_limits,
        "cap_state_samples": cap_states,
        "battle_state_schema_version": BATTLE_STATE_SCHEMA_VERSION,
        "event_counts": {"shots": len(collector.shots), "torpedoes": len(collector.torpedoes),
                         "planes": len(collector.planes), "consumables": len(collector.consumable_activations),
                         "wards": len(collector.wards), "smoke_entities": len(collector.smoke_snapshots)},
        "wire_minimap_events": dict(wire_minimap_events),
        "coordinate_units": "Source positions retained in BigWorld engine units; derived gameplay distances use the pinned world-domain scale",
        "freshness_policy_seconds": 3.0,
        "paired_coordinate_checks": {"count": len(observed_errors),
                                      "median_engine_units": statistics.median(observed_errors) if observed_errors else None,
                                      "p95_engine_units": percentile(observed_errors, .95)},
        "track_summaries": track_summaries,
        "runtime_bundle": bundle.manifest["bundle_id"] if bundle is not None else None,
        "analysis_schema_version": 2 if combat is not None else 1,
        "combat": combat,
        "statistics": statistics_capture.finish(participants,header,str(bc._arena_id)) if statistics_capture is not None else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replay")
    parser.add_argument("--legacy-root", default=str(ROOT.parent / "wows_analyzer"))
    parser.add_argument("--schema-root")
    parser.add_argument("--resource-version", default="15.9.0.13357625")
    parser.add_argument("--resource-manifest", default=str(ROOT / "artifacts/p0/resource_manifest.json"))
    parser.add_argument("--player-schema", default=str(ROOT / "artifacts/p0/player_schema.json"))
    parser.add_argument("--result-schema", default=str(ROOT / "artifacts/p0/result_schema.json"))
    parser.add_argument("--time-schema", default=str(ROOT / "artifacts/p0/time_schema.json"))
    parser.add_argument("--geometry-schema", default=str(ROOT / "artifacts/p0/geometry_schema.json"))
    parser.add_argument("--background-schema", default=str(ROOT / "artifacts/p0/background_schema.json"))
    parser.add_argument("--units-schema", default=str(ROOT / "artifacts/p0/units_schema.json"))
    parser.add_argument("--packet-semantics", default=str(ROOT / "artifacts/p0/packet_semantics.json"))
    parser.add_argument("--controller-version")
    parser.add_argument("--output", required=True)
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    try:
        result = probe(args)
    except Exception as exc:
        result = {"replay": str(Path(args.replay).resolve()), "status": "failed",
                  "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({k: result.get(k) for k in (
        "status", "elapsed_seconds", "header_roster_count", "parsed_roster_count",
        "roster_matches_header", "arena_id", "outcome", "packet_errors", "error")}, ensure_ascii=True))
    return 0 if result["status"] == "structurally_decoded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
