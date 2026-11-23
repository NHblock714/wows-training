"""Orchestrate versioned offline analyses without tying engines to Qt."""
from __future__ import annotations
import hashlib
import json
from .library import Library
from .workbench_store import WorkbenchStore
from .identity_rules import effective_mappings, parse_kots_name
from .multiview_store import BattleRecordingStore
from .multiview_selection import current_recording


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def attach_identities(report, store, legacy_mappings):
    rows = effective_mappings(report, legacy_mappings, store.list_documents("assignment"), store.list_documents("player"))
    report["effective_identity_mappings"] = rows
    by_name = {row["tournament_name"]: row for row in rows}
    for player in report.get("roster", []):
        row = by_name.get(player.get("name"), {})
        player["resolved_identity"] = row.get("resolved_identity", {})
        if row.get("confirmed"):
            player["personal_account"] = {key: row.get(key) for key in ("personal_nickname", "region", "account_id")}
    return report


def compact_analysis_input(report):
    """Keep needed observations in memory; immutable source JSON is untouched."""
    keep = ("sha256", "replay_version", "arena_id", "metadata", "roster", "event_key", "battle_annotation",
            "time_analysis", "geometry_analysis", "packet_clock_range", "capture_points", "cap_team_changes",
            "team_score_samples", "team_score_limit_samples", "cap_state_samples", "battle_state_schema_version",
            "death_events", "winner_absolute_team", "outcome", "host_absolute_team", "analysis_schema_version",
            "runtime_bundle", "quality", "status", "effective_identity_mappings", "analysis_context", "multiview")
    result = {key: report[key] for key in keep if key in report}
    combat = report.get("combat") or {}
    ships = {}
    for key, ship in combat.get("ships", {}).items():
        samples = [{field: row[field] for field in ("clock", "visible_to_recorder", "visibility_clock") if field in row}
                   for row in ship.get("samples", [])]
        ships[key] = {"entity_id": ship.get("entity_id"), "max_hp": ship.get("max_hp"), "samples": samples}
    result["combat"] = {"schema_version": combat.get("schema_version"), "ships": ships,
                        "consumables": combat.get("consumables", []), "provenance": combat.get("provenance"),
                        "diagnostics": combat.get("diagnostics")}
    return result


class AnalysisWorkspace:
    def __init__(self, root, bundle):
        self.library, self.store, self.bundle = Library(root), WorkbenchStore(root), bundle
        self.recordings = BattleRecordingStore(root)

    def fingerprint(self, battle_ids, options):
        inputs = []
        for battle_id in sorted(set(battle_ids)):
            battle = self.library.get_battle(battle_id)
            inputs.append({"battle_id": battle_id, "event_key": battle["event_key"], "label": battle["label"], "notes": battle["notes"],
                           "views": [{"sha256": row["sha256"], "revision": row["analysis_revision"]} for row in battle["replays"]]})
        identities = {kind: [{"id": row["id"], "revision": row["revision"]} for row in self.store.list_documents(kind)]
                      for kind in ("assignment", "player")}
        legacy = [{key: row.get(key) for key in ("id", "event_key", "tournament_name", "version", "personal_nickname", "region", "account_id")}
                  for row in self.library.list_mappings()]
        value = {"inputs": inputs, "options": options, "identities": identities, "legacy_mappings": legacy,
                 "engine_version": "p2p3-1", "runtime_bundle": self.bundle.manifest["bundle_id"]}
        merged = [record for battle_id in sorted(set(battle_ids)) if (record := current_recording(self.library, self.recordings, battle_id))]
        if merged:
            value['merged_recordings'] = [{key: r[key] for key in ('id', 'battle_id', 'merged_report_sha256', 'engine_version')} for r in merged]
        return {"hash": digest(value), "data": value}

    def reports(self, battle_ids, team_selector=None, cancel=None, progress=None, compact=True):
        result, legacy = [], self.library.list_mappings()
        ids = list(dict.fromkeys(battle_ids))
        for index, battle_id in enumerate(ids):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("操作已取消")
            if progress:
                progress(f"载入比赛 {index + 1}/{len(ids)}")
            merged = current_recording(self.library, self.recordings, battle_id)
            views = [dict(sha256=None, analysis_revision=None)] if merged else self.library.list_replays(battle_id)
            for view in views:
                if merged:
                    report = self.recordings.load_report(merged['id'])
                    battle = self.library.get_battle(battle_id)
                    report['event_key'] = battle['event_key']
                    report['battle_annotation'] = {key: battle.get(key) for key in ('label', 'notes')}
                    report.setdefault('analysis_context', {}).update(source_recording_id=merged['id'], source_analysis_revisions={s['sha256']: s['analysis_revision'] for s in merged['sources']})
                else:
                    report = self.library.get_report(view['sha256'])
                report = self.bundle.presentation_report(report)
                attach_identities(report, self.store, legacy)
                context = report.setdefault("analysis_context", {})
                context["source_analysis_revision"] = view["analysis_revision"]
                if (team_selector or {}).get("clan_tag"):
                    for player in report.get("roster", []):
                        candidate = parse_kots_name(player.get("name", ""), report.get("event_key"))
                        if candidate.get("clan") and candidate.get("event_matches_suffix") is True:
                            player["clan_tag"] = candidate["clan"]
                    context["team_rule_source"] = "user_selected_literal_tournament_prefix"
                result.append(compact_analysis_input(report) if compact else report)
        return result

    def analyze(self, battle_ids, *, team_selector, zones=None, rules=None, document_versions=None, cancel=None, progress=None):
        from .analysis_engine import analyze_collection
        options = {"team_selector": team_selector, "zones": zones, "rules": rules,
                   "document_versions": document_versions or [], "replay_context_revision": 4}
        fingerprint = self.fingerprint(battle_ids, options)
        for run in reversed(self.store.list_runs("collection")):
            if (run.get("input_fingerprint") or {}).get("hash") == fingerprint["hash"]:
                return dict(run, reused=True)
        reports = self.reports(battle_ids, team_selector, cancel, progress)
        if cancel is not None and cancel.is_set():
            raise InterruptedError("操作已取消")
        if progress:
            progress("计算阵容、空间行为与可比较分组…")
        result = analyze_collection(reports, ship_catalog=self.bundle.ship_catalog(), team_selector=team_selector, zones=zones, rules=rules)
        catalog = self.bundle.ship_catalog()
        self._append_replay_context(result, reports, catalog, cancel=cancel, progress=progress)
        used = {str(row.get("shipParamsId")) for report in reports for row in report.get("roster", [])}
        result["presentation"] = {"maps": self.bundle.json("labels.json").get("maps", {}),
            "ships": {key: catalog.get(key, {}).get("display_name") or catalog.get(key, {}).get("internal_name") or key for key in used}}
        result["input_snapshot"] = fingerprint["data"]
        if cancel is not None and cancel.is_set():
            raise InterruptedError("操作已取消")
        if self.fingerprint(battle_ids, options)["hash"] != fingerprint["hash"]:
            raise ValueError("计算期间回放或身份资料发生变化，请重试以保持来源一致。")
        return self.store.save_run("collection", f"跨局分析 · {len(set(battle_ids))} 场", result, fingerprint)

    def _append_replay_context(self, result, reports, catalog, *, cancel=None, progress=None):
        from .battle_review import build_review, replay_context_rows

        selected = {report['sha256']: report for report in reports}
        battles = result.get('battles', [])
        for index, battle in enumerate(battles, 1):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("操作已取消")
            if progress:
                progress(f"整理录像细节 {index}/{len(battles)}")
            sha = battle['source_sha']
            context = selected[sha].get('analysis_context') or {}
            recording_id = context.get('source_recording_id')
            if recording_id:
                report = self.recordings.load_report(recording_id)
            else:
                report = self.library.get_report(sha, context.get('source_analysis_revision'))
            if report.get('sha256') != sha:
                raise ValueError("代表录像身份已变化，请重新运行分析。")
            report = self.bundle.presentation_report(report)
            review = build_review(report, catalog)
            entities = [row['entity_id'] for row in battle['participants'] if type(row.get('entity_id')) is int]
            rows = replay_context_rows(report, review, battle['battle_id'], focus_team_entity_ids=entities,
                                       recording_id=recording_id)
            result['evidence'].extend(rows)
            battle['evidence_ids'].extend(row['id'] for row in rows)

    def stale_reasons(self, run):
        snapshot = (run.get("input_fingerprint") or {}).get("data")
        if not snapshot:
            return []
        reasons = []
        ids = [row["battle_id"] for row in snapshot["inputs"]]
        try:
            if self.fingerprint(ids, snapshot["options"])["hash"] != run["input_fingerprint"]["hash"]:
                reasons.append("回放分析、分类或身份关联已有更新")
            for ref in snapshot["options"].get("document_versions") or []:
                if self.store.get_document(ref["id"])["revision"] != ref["revision"]:
                    reasons.append("区域或规则配置已有新版本")
        except KeyError:
            reasons.append("原输入已不在当前资料库中")
        return list(dict.fromkeys(reasons))
