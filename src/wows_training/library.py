"""Thread-safe local replay library with immutable reports and scoped mappings."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile


SCHEMA_VERSION = 2
UNASSIGNED_EVENT = "unassigned"
UNCLASSIFIED_LABEL = "未分类"
_VEHICLE_CODE = re.compile(r"[A-Za-z]{4}\d{3,}[-_][\w-]+")
_JSON_FIELD_START = re.compile(r"[\s,]*")
_JSON_SEPARATOR = re.compile(r"\s*:\s*")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value, field, required=False):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    if required and not value.strip():
        raise ValueError(f"{field} must not be empty")
    return value


def _sha(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise ValueError("Report sha256 must be a complete SHA256 hexadecimal digest")
    return value.lower()


def _positive_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value) and int(value) > 0:
        return str(int(value))
    return None


def _version(report):
    metadata = report.get("metadata") or {}
    value = report.get("replay_version") or metadata.get("clientVersionFromExe") or "unknown"
    if isinstance(value, (list, tuple)):
        value = ".".join(str(part) for part in value)
    if not isinstance(value, str):
        return "unknown", False
    parts = re.split(r"[.,_]", value.strip())
    if len(parts) in (3, 4) and all(part.strip().isdigit() for part in parts):
        return ".".join(str(int(part.strip())) for part in parts), len(parts) == 4
    return value, False


def _duration(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _event_key(report, names):
    explicit = report.get("event_key")
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    if any(isinstance(name, str) and name.endswith("_KOTS19AS") for name in names):
        return "KOTS19AS"
    return UNASSIGNED_EVENT


def _recorder_ship(report):
    metadata = report.get("metadata") or {}
    if not isinstance(metadata, dict) or report.get("multiview"):
        return None, None
    recorder = metadata.get("playerName")
    if not isinstance(recorder, str) or not recorder:
        return None, None

    def named_records(rows):
        return [row for row in rows if isinstance(row, dict) and row.get("name") == recorder] if isinstance(rows, list) else []

    if named_records(report.get("observers")):
        return None, None
    roster = named_records(report.get("roster"))
    if any(row.get("_player_kind") == 3 for row in roster):
        return None, None
    if roster:
        field = "shipParamsId"
        if len(roster) != 1:
            roster = [row for row in roster if type(row.get("relation")) is int and row["relation"] == 0]
    else:
        field = "shipId"  # in the raw header, shipId is the ship's parameters ID
        roster = [row for row in named_records(metadata.get("vehicles"))
                  if type(row.get("relation")) is int and row["relation"] == 0]
    if len(roster) != 1 or roster[0].get("_player_kind") == 2 or roster[0].get("relation") not in (None, 0):
        return None, None
    vehicle = metadata.get("playerVehicle")
    if not isinstance(vehicle, str) or not _VEHICLE_CODE.fullmatch(vehicle):
        vehicle = None
    return _positive_id(roster[0].get(field)), vehicle


def _summary(report):
    metadata = report.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("Report metadata must be an object")
    roster = report.get("roster") or metadata.get("vehicles") or []
    if not isinstance(roster, list):
        raise ValueError("Report roster must be a list")
    recorder = str(metadata.get("playerName") or "")
    names = [record.get("name") for record in roster if isinstance(record, dict)]
    names = [name for name in names if isinstance(name, str)]
    count = report.get("parsed_roster_count")
    if type(count) is not int or count < 0:
        count = len(roster)
    common = report.get("post_battle_common") or {}
    if not isinstance(common, dict):
        common = {}
    arena = _positive_id(report.get("arena_id"))
    result_arena = _positive_id(common.get("arena_id"))
    if arena and result_arena and arena != result_arena:
        raise ValueError("Report contains conflicting arena IDs")
    arena = arena or result_arena
    version, exact_version = _version(report)
    map_name = metadata.get("mapName") or ""
    map_id = str(map_name).replace("\\", "/").rsplit("/", 1)[-1]
    map_label = str(metadata.get("mapDisplayName") or map_id)
    time_analysis = report.get("time_analysis") or {}
    coverage = time_analysis.get("coverage") or {} if isinstance(time_analysis, dict) else {}
    if not isinstance(coverage, dict):
        coverage = {}
    result_duration = _duration(coverage.get("result_duration_sec"))
    if result_duration is None:
        result_duration = _duration(common.get("duration_sec"))
    quality = report.get("quality")
    if not isinstance(quality, str):
        quality = str(report.get("status") or "unknown")
    return {
        "arena_id": arena, "version": version, "exact_version": exact_version,
        "map_id": map_id, "map_label": map_label, "date_raw": str(metadata.get("dateTime") or ""),
        "recorder": recorder, "outcome": str(report.get("outcome") or "unknown"),
        "quality": quality, "participant_count": count,
        "participant_names": "\n".join(names), "event_key": _event_key(report, [recorder, *names]),
        "observed_duration": _duration(coverage.get("last_observed_elapsed_sec")),
        "result_duration": result_duration,
    }


class Library:
    """Every operation owns a short SQLite connection, usable by UI and workers.

    SHA256 is the parser-provided input identity. The parser verifies inputs;
    this layer records source paths without requiring an original replay to
    remain present. Reports are immutable and independently hashed on storage.
    """
    def __init__(self, root: Path):
        requested_root = Path(root).expanduser().absolute()
        requested_root.mkdir(parents=True, exist_ok=True)
        # Packaged Windows applications can redirect a newly created directory.
        # Resolve after creation so the database and report containment checks
        # use the same physical root on both initial and subsequent launches.
        self.root = requested_root.resolve()
        self.reports_dir = self.root / "reports"
        self.reports_dir.mkdir(exist_ok=True)
        self.database_path = self.root / "library.sqlite3"
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, SCHEMA_VERSION):
                raise ValueError(f"Unsupported library database version: {version}")
            connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS battles (
                    id TEXT PRIMARY KEY, arena_id TEXT, version TEXT NOT NULL,
                    primary_sha TEXT NOT NULL, event_key TEXT NOT NULL,
                    label TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, last_imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS replays (
                    sha256 TEXT PRIMARY KEY, battle_id TEXT NOT NULL REFERENCES battles(id),
                    report_relative_path TEXT NOT NULL, report_sha256 TEXT NOT NULL,
                    version TEXT NOT NULL, arena_id TEXT, map_id TEXT NOT NULL,
                    map_label TEXT NOT NULL, date_raw TEXT NOT NULL, recorder TEXT NOT NULL,
                    outcome TEXT NOT NULL, quality TEXT NOT NULL, participant_count INTEGER NOT NULL,
                    participant_names TEXT NOT NULL, observed_duration REAL, result_duration REAL,
                    imported_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_replays_battle ON replays(battle_id);
                CREATE TABLE IF NOT EXISTS replay_modes (
                    replay_sha TEXT PRIMARY KEY REFERENCES replays(sha256), label TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS replay_ships (
                    replay_sha TEXT PRIMARY KEY REFERENCES replays(sha256),
                    ship_params_id TEXT, vehicle_name TEXT
                );
                CREATE TABLE IF NOT EXISTS replay_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, replay_sha TEXT NOT NULL REFERENCES replays(sha256),
                    path TEXT NOT NULL, canonical_path TEXT NOT NULL, source_name TEXT NOT NULL,
                    added_at TEXT NOT NULL, UNIQUE(replay_sha, canonical_path)
                );
                CREATE TABLE IF NOT EXISTS mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL,
                    tournament_name TEXT NOT NULL, personal_nickname TEXT NOT NULL,
                    region TEXT NOT NULL, account_id TEXT, notes TEXT NOT NULL,
                    version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    deleted_at TEXT, UNIQUE(event_key, tournament_name)
                );
                CREATE TABLE IF NOT EXISTS mapping_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, mapping_id INTEGER NOT NULL REFERENCES mappings(id),
                    version INTEGER NOT NULL, action TEXT NOT NULL, changed_at TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL, UNIQUE(mapping_id, version)
                );
                CREATE TABLE IF NOT EXISTS analysis_history (
                    replay_sha TEXT NOT NULL REFERENCES replays(sha256), revision INTEGER NOT NULL,
                    report_relative_path TEXT NOT NULL, report_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL, source_path TEXT, action TEXT NOT NULL,
                    product_version TEXT, analysis_version TEXT, runtime_bundle TEXT,
                    PRIMARY KEY(replay_sha, revision)
                );
            """)
            self._migrate_analysis_history(connection)
            self._index_legacy_modes(connection)
            self._index_legacy_ships(connection)
            connection.execute("PRAGMA user_version=2")
            connection.commit()

    def _report_prefix_fields(self, relative, keys):
        fields = {}
        try:
            with self._report_path(relative).open('rb') as stream:
                # The parser writes header and roster ahead of the large track arrays.
                prefix = stream.read(256 * 1024).decode('utf8', errors='ignore')
            decoder = json.JSONDecoder()
            pos = prefix.index('{') + 1
            while pos < len(prefix) and keys - fields.keys():
                pos = _JSON_FIELD_START.match(prefix, pos).end()
                key, pos = decoder.raw_decode(prefix, pos)
                separator = _JSON_SEPARATOR.match(prefix, pos)
                if not isinstance(key, str) or not separator:
                    break
                value, pos = decoder.raw_decode(prefix, separator.end())
                if key in keys:
                    fields[key] = value
        except (OSError, ValueError, RecursionError):
            pass
        return fields

    def _index_legacy_modes(self, connection):
        from .ordinary_replay import battle_mode_label
        missing = connection.execute("""SELECT r.sha256,r.report_relative_path FROM replays r
            LEFT JOIN replay_modes m ON m.replay_sha=r.sha256 WHERE m.replay_sha IS NULL""").fetchall()
        for row in missing:
            metadata = self._report_prefix_fields(row['report_relative_path'], {'metadata'}).get('metadata')
            connection.execute('INSERT INTO replay_modes VALUES(?,?)', (row['sha256'], battle_mode_label(metadata)))

    def _index_legacy_ships(self, connection):
        missing = connection.execute("""SELECT r.sha256,r.recorder,r.report_relative_path,r.report_sha256 FROM replays r
            LEFT JOIN replay_ships v ON v.replay_sha=r.sha256 WHERE v.replay_sha IS NULL""").fetchall()
        for row in missing:
            report = self._report_prefix_fields(row['report_relative_path'], {'metadata', 'roster', 'observers', 'multiview'})
            if not {'metadata', 'roster'} <= report.keys():
                try:
                    report = json.loads(self._verified_report_bytes(row))
                except (OSError, ValueError, RecursionError):
                    report = {}
            metadata = report.get('metadata') if isinstance(report, dict) else None
            if not isinstance(metadata, dict) or metadata.get('playerName') != row['recorder']:
                report = {}
            self._index_ship(connection, row['sha256'], report)

    @staticmethod
    def _index_ship(connection, sha, report):
        ship_id, vehicle = _recorder_ship(report)
        connection.execute("""INSERT INTO replay_ships VALUES(?,?,?) ON CONFLICT(replay_sha)
            DO UPDATE SET ship_params_id=excluded.ship_params_id,vehicle_name=excluded.vehicle_name""",
                           (sha, ship_id, vehicle))

    @staticmethod
    def _index_mode(connection, sha, report):
        from .ordinary_replay import battle_mode_label
        connection.execute("""INSERT INTO replay_modes VALUES(?,?)
            ON CONFLICT(replay_sha) DO UPDATE SET label=excluded.label""",
            (sha, battle_mode_label(report.get('metadata') or {})))

    @staticmethod
    def _migrate_analysis_history(connection):
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(replays)")}
        if "analysis_revision" not in columns:
            connection.execute("ALTER TABLE replays ADD COLUMN analysis_revision INTEGER NOT NULL DEFAULT 1")
        connection.execute("""
            INSERT OR IGNORE INTO analysis_history(
                replay_sha,revision,report_relative_path,report_sha256,created_at,source_path,action)
            SELECT r.sha256,r.analysis_revision,r.report_relative_path,r.report_sha256,r.imported_at,
                   (SELECT s.path FROM replay_sources s WHERE s.replay_sha=r.sha256 ORDER BY s.id LIMIT 1),
                   'legacy_import'
            FROM replays r
        """)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    def _report_path(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.reports_dir.resolve()):
            raise ValueError("Stored report path escapes the library reports directory")
        return path

    def _write_report(self, replay_sha, payload, revision=None):
        filename = f"{replay_sha}.json" if revision is None else f"{replay_sha}.r{revision:04d}.{hashlib.sha256(payload).hexdigest()[:16]}.json"
        target = self._report_path(f"reports/{filename}")
        if target.exists():
            if target.read_bytes() != payload:
                raise ValueError("An unregistered report already exists with different contents")
            return target
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.reports_dir, prefix=f".{replay_sha}.", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return target

    @staticmethod
    def _record_analysis(connection, replay_sha, revision, relative_path, report_hash, source, created_at, action, report):
        marker = lambda key: str(report[key]) if report.get(key) is not None else None
        connection.execute("""INSERT INTO analysis_history(
            replay_sha,revision,report_relative_path,report_sha256,created_at,source_path,action,
            product_version,analysis_version,runtime_bundle) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                           (replay_sha, revision, relative_path, report_hash, created_at, str(source), action,
                            marker("product_version"), marker("analysis_version"), marker("runtime_bundle")))

    def _verified_report_bytes(self, record):
        payload = self._report_path(record["report_relative_path"]).read_bytes()
        if hashlib.sha256(payload).hexdigest() != record["report_sha256"]:
            raise ValueError("Stored report integrity check failed")
        return payload

    def ingest(self, report: dict, source_path: Path) -> dict:
        if not isinstance(report, dict):
            raise ValueError("Report must be an object")
        replay_sha = _sha(report.get("sha256"))
        summary = _summary(report)
        payload = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        source = Path(source_path).expanduser().resolve()
        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM replays WHERE sha256=?", (replay_sha,)).fetchone()
            if existing is not None:
                self._verified_report_bytes(existing)
                battle_id = existing["battle_id"]
                status = "duplicate"
            else:
                battle_id = f"arena:{summary['version']}:{summary['arena_id']}" if summary["arena_id"] and summary["exact_version"] else f"replay:{replay_sha}"
                battle = connection.execute("SELECT * FROM battles WHERE id=?", (battle_id,)).fetchone()
                if battle is None:
                    connection.execute("INSERT INTO battles(id,arena_id,version,primary_sha,event_key,label,created_at,last_imported_at) VALUES(?,?,?,?,?,?,?,?)",
                                       (battle_id, summary["arena_id"], summary["version"], replay_sha, summary["event_key"], UNCLASSIFIED_LABEL, now, now))
                else:
                    primary = connection.execute("SELECT map_id FROM replays WHERE sha256=?", (battle["primary_sha"],)).fetchone()
                    if primary and primary["map_id"] and summary["map_id"] and primary["map_id"] != summary["map_id"]:
                        raise ValueError("The same arena and client version have conflicting map IDs")
                target = self._write_report(replay_sha, payload)
                columns = ("version", "arena_id", "map_id", "map_label", "date_raw", "recorder", "outcome", "quality", "participant_count", "participant_names", "observed_duration", "result_duration")
                connection.execute("INSERT INTO replays(sha256,battle_id,report_relative_path,report_sha256," + ",".join(columns) + ",imported_at) VALUES(" + ",".join("?" for _ in range(len(columns) + 5)) + ")",
                                   (replay_sha, battle_id, target.relative_to(self.root).as_posix(), hashlib.sha256(payload).hexdigest(), *(summary[column] for column in columns), now))
                connection.execute("UPDATE battles SET last_imported_at=? WHERE id=?", (now, battle_id))
                status = "imported"
            if status == 'imported':
                self._index_mode(connection, replay_sha, report)
                self._index_ship(connection, replay_sha, report)
            cursor = connection.execute("INSERT OR IGNORE INTO replay_sources(replay_sha,path,canonical_path,source_name,added_at) VALUES(?,?,?,?,?)",
                                        (replay_sha, str(source), os.path.normcase(str(source)), source.name, now))
            source_added = cursor.rowcount == 1
            if status == "imported":
                self._record_analysis(connection, replay_sha, 1, target.relative_to(self.root).as_posix(),
                                      hashlib.sha256(payload).hexdigest(), source, now, "import", report)
            connection.commit()
        result = self.get_battle(battle_id)
        result.update({"replay_sha": replay_sha, "ingest_status": status, "source_added": source_added})
        return result

    def update_analysis(self, report: dict, source_path: Path) -> dict:
        """Atomically select a new immutable analysis for the same replay identity.

        Existing JSON files and analysis revisions remain available. A failed
        transaction may leave an unreferenced new JSON, but never replaces the
        currently selected report or any annotation or account mapping.
        """
        if not isinstance(report, dict):
            raise ValueError("Report must be an object")
        replay_sha = _sha(report.get("sha256"))
        summary = _summary(report)
        payload = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        report_hash = hashlib.sha256(payload).hexdigest()
        source = Path(source_path).expanduser().resolve()
        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM replays WHERE sha256=?", (replay_sha,)).fetchone()
            if existing is None:
                raise KeyError("Replay is not in the library; refresh cannot create a new entry")
            self._verified_report_bytes(existing)
            if not summary["exact_version"] or summary["version"] != existing["version"] or summary["arena_id"] != existing["arena_id"]:
                raise ValueError("Analysis refresh must preserve SHA256, arena ID and full client version")
            if summary["map_id"] != existing["map_id"] or summary["recorder"] != existing["recorder"]:
                raise ValueError("Analysis refresh must preserve map and recorder identity")
            battle_id = existing["battle_id"]
            revision = existing["analysis_revision"]
            changed = report_hash != existing["report_sha256"]
            if changed:
                revision += 1
                target = self._write_report(replay_sha, payload, revision=revision)
                relative_path = target.relative_to(self.root).as_posix()
                self._record_analysis(connection, replay_sha, revision, relative_path, report_hash, source, now, "refresh", report)
                columns = ("map_label", "date_raw", "outcome", "quality", "participant_count", "participant_names", "observed_duration", "result_duration")
                assignment = ",".join(f"{column}=?" for column in columns)
                connection.execute(f"UPDATE replays SET report_relative_path=?,report_sha256=?,analysis_revision=?,{assignment} WHERE sha256=?",
                                   (relative_path, report_hash, revision, *(summary[column] for column in columns), replay_sha))
            self._index_mode(connection, replay_sha, report)
            self._index_ship(connection, replay_sha, report)
            cursor = connection.execute("INSERT OR IGNORE INTO replay_sources(replay_sha,path,canonical_path,source_name,added_at) VALUES(?,?,?,?,?)",
                                        (replay_sha, str(source), os.path.normcase(str(source)), source.name, now))
            source_added = cursor.rowcount == 1
            connection.commit()
        result = self.get_battle(battle_id)
        result.update({"replay_sha": replay_sha, "ingest_status": "updated", "source_added": source_added,
                       "analysis_revision": revision, "analysis_changed": changed})
        return result

    @staticmethod
    def _battle_query():
        return """SELECT b.id,b.arena_id,b.version,b.primary_sha,b.event_key,b.label,b.notes,
                         b.created_at,b.last_imported_at,r.map_id,r.map_label,r.date_raw,m.label AS battle_mode,
                         r.recorder,r.outcome,r.quality,r.participant_count,r.observed_duration,r.result_duration,r.analysis_revision,
                         v.ship_params_id AS recorder_ship_id,v.vehicle_name AS recorder_vehicle,
                         (SELECT COUNT(*) FROM replays rr WHERE rr.battle_id=b.id) AS replay_count,
                         (SELECT source_name FROM replay_sources s WHERE s.replay_sha=b.primary_sha ORDER BY s.id LIMIT 1) AS source_name
                  FROM battles b JOIN replays r ON r.sha256=b.primary_sha
                  LEFT JOIN replay_modes m ON m.replay_sha=b.primary_sha
                  LEFT JOIN replay_ships v ON v.replay_sha=b.primary_sha"""

    @staticmethod
    def _battle_row(row):
        result = dict(row)
        result["merged"] = False
        result["view_policy"] = "separate_replay_views_not_fused"
        return result

    def list_battles(self, search="", map_id="", event_key="") -> list[dict]:
        for value, name in ((search, "search"), (map_id, "map_id"), (event_key, "event_key")):
            _text(value, name)
        conditions, params = [], []
        if map_id:
            conditions.append("r.map_id=?")
            params.append(map_id)
        if event_key:
            conditions.append("b.event_key=?")
            params.append(event_key)
        if search:
            pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses = [f"{field} LIKE ? ESCAPE '\\'" for field in ("b.id", "b.event_key", "b.label", "b.notes", "r.map_id", "r.map_label", "r.date_raw")]
            clauses.append("EXISTS(SELECT 1 FROM replays sr WHERE sr.battle_id=b.id AND (sr.recorder LIKE ? ESCAPE '\\' OR sr.participant_names LIKE ? ESCAPE '\\'))")
            clauses.append("EXISTS(SELECT 1 FROM replay_sources ss JOIN replays sr ON sr.sha256=ss.replay_sha WHERE sr.battle_id=b.id AND ss.source_name LIKE ? ESCAPE '\\')")
            conditions.append("(" + " OR ".join(clauses) + ")")
            params.extend([pattern] * 10)
        query = self._battle_query() + (" WHERE " + " AND ".join(conditions) if conditions else "") + " ORDER BY b.last_imported_at DESC,b.id"
        with self._connection() as connection:
            return [self._battle_row(row) for row in connection.execute(query, params)]

    def get_battle(self, battle_id) -> dict:
        with self._connection() as connection:
            connection.execute("BEGIN")
            row = connection.execute(self._battle_query() + " WHERE b.id=?", (str(battle_id),)).fetchone()
            if row is None:
                raise KeyError(f"Battle is absent from the library: {battle_id}")
            result = self._battle_row(row)
            result["replays"] = self._list_replays(connection, str(battle_id))
            return result

    def _list_replays(self, connection, battle_id):
        rows = connection.execute("""SELECT r.*,b.primary_sha,v.ship_params_id AS recorder_ship_id,v.vehicle_name AS recorder_vehicle
            FROM replays r JOIN battles b ON b.id=r.battle_id LEFT JOIN replay_ships v ON v.replay_sha=r.sha256
            WHERE r.battle_id=? ORDER BY (r.sha256=b.primary_sha) DESC,r.imported_at,r.sha256""", (battle_id,))
        result = []
        for row in rows:
            record = dict(row)
            sources = [dict(source) for source in connection.execute("SELECT id,path,source_name,added_at FROM replay_sources WHERE replay_sha=? ORDER BY id", (record["sha256"],))]
            record.update({"sources": sources, "source_path": sources[0]["path"] if sources else "",
                           "source_name": sources[0]["source_name"] if sources else "",
                           "is_primary": record["sha256"] == record.pop("primary_sha"), "merged": False})
            record.pop("participant_names", None)
            result.append(record)
        return result

    def list_replays(self, battle_id) -> list[dict]:
        with self._connection() as connection:
            connection.execute("BEGIN")
            if connection.execute("SELECT 1 FROM battles WHERE id=?", (str(battle_id),)).fetchone() is None:
                raise KeyError(f"Battle is absent from the library: {battle_id}")
            return self._list_replays(connection, str(battle_id))

    def find_replay(self, replay_sha) -> dict | None:
        """Look up an existing input SHA without parsing or opening the replay."""
        replay_sha = _sha(replay_sha)
        with self._connection() as connection:
            connection.execute("BEGIN")
            row = connection.execute("SELECT battle_id FROM replays WHERE sha256=?", (replay_sha,)).fetchone()
            if row is None:
                return None
            return next(record for record in self._list_replays(connection, row["battle_id"]) if record["sha256"] == replay_sha)

    def get_report(self, replay_sha, analysis_revision=None) -> dict:
        replay_sha = _sha(replay_sha)
        with self._connection() as connection:
            row = connection.execute("SELECT r.*,b.event_key,b.label,b.notes FROM replays r JOIN battles b ON b.id=r.battle_id WHERE r.sha256=?", (replay_sha,)).fetchone()
            if row is None:
                raise KeyError(f"Replay is absent from the library: {replay_sha}")
            selected = row
            if analysis_revision is not None:
                if type(analysis_revision) is not int or analysis_revision < 1:
                    raise ValueError("analysis_revision must be a positive integer")
                selected = connection.execute("SELECT * FROM analysis_history WHERE replay_sha=? AND revision=?", (replay_sha, analysis_revision)).fetchone()
                if selected is None:
                    raise KeyError(f"Analysis revision does not exist: {analysis_revision}")
            result = json.loads(self._verified_report_bytes(selected))
            result["event_key"] = row["event_key"]
            result["battle_annotation"] = {"battle_id": row["battle_id"], "label": row["label"], "notes": row["notes"]}
            return result

    def list_analysis_versions(self, replay_sha) -> list[dict]:
        replay_sha = _sha(replay_sha)
        with self._connection() as connection:
            connection.execute("BEGIN")
            current = connection.execute("SELECT analysis_revision FROM replays WHERE sha256=?", (replay_sha,)).fetchone()
            if current is None:
                raise KeyError(f"Replay is absent from the library: {replay_sha}")
            return [dict(row) | {"is_current": row["revision"] == current["analysis_revision"]}
                    for row in connection.execute("SELECT * FROM analysis_history WHERE replay_sha=? ORDER BY revision", (replay_sha,))]

    def set_battle_annotation(self, battle_id, event_key, label, notes):
        event_key = _text(event_key, "event_key") or UNASSIGNED_EVENT
        label = _text(label, "label") or UNCLASSIFIED_LABEL
        notes = _text(notes, "notes")
        with self._connection() as connection:
            cursor = connection.execute("UPDATE battles SET event_key=?,label=?,notes=? WHERE id=?", (event_key, label, notes, str(battle_id)))
            if cursor.rowcount != 1:
                raise KeyError(f"Battle is absent from the library: {battle_id}")
        return self.get_battle(battle_id)

    @staticmethod
    def _record_mapping_history(connection, record, action):
        connection.execute("INSERT INTO mapping_history(mapping_id,version,action,changed_at,snapshot_json) VALUES(?,?,?,?,?)",
                           (record["id"], record["version"], action, record["updated_at"], json.dumps(record, ensure_ascii=False)))

    def save_mapping(self, event_key, tournament_name, personal_nickname, region, account_id=None, notes="") -> dict:
        event_key = _text(event_key, "event_key", True)
        tournament_name = _text(tournament_name, "tournament_name", True)
        personal_nickname = _text(personal_nickname, "personal_nickname", True)
        region = _text(region, "region", True).strip().lower()
        notes = _text(notes, "notes")
        normalized_id = None if account_id in (None, "") else _positive_id(account_id)
        if account_id not in (None, "") and normalized_id is None:
            raise ValueError("account_id must be a positive numeric ID or None")
        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM mappings WHERE event_key=? AND tournament_name=?", (event_key, tournament_name)).fetchone()
            if existing is None:
                cursor = connection.execute("INSERT INTO mappings(event_key,tournament_name,personal_nickname,region,account_id,notes,version,created_at,updated_at) VALUES(?,?,?,?,?,?,1,?,?)",
                                            (event_key, tournament_name, personal_nickname, region, normalized_id, notes, now, now))
                mapping_id, action = cursor.lastrowid, "create"
            else:
                mapping_id = existing["id"]
                changed = existing["deleted_at"] is not None or any(existing[key] != value for key, value in (("personal_nickname", personal_nickname), ("region", region), ("account_id", normalized_id), ("notes", notes)))
                if not changed:
                    connection.commit()
                    return dict(existing) | {"changed": False}
                action = "restore" if existing["deleted_at"] is not None else "update"
                connection.execute("UPDATE mappings SET personal_nickname=?,region=?,account_id=?,notes=?,version=version+1,updated_at=?,deleted_at=NULL WHERE id=?",
                                   (personal_nickname, region, normalized_id, notes, now, mapping_id))
            record = dict(connection.execute("SELECT * FROM mappings WHERE id=?", (mapping_id,)).fetchone())
            self._record_mapping_history(connection, record, action)
            connection.commit()
            return record | {"changed": True}

    def list_mappings(self, event_key=None) -> list[dict]:
        params = ()
        query = "SELECT * FROM mappings WHERE deleted_at IS NULL"
        if event_key is not None:
            query += " AND event_key=?"
            params = (_text(event_key, "event_key"),)
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(query + " ORDER BY event_key,tournament_name,id", params)]

    def delete_mapping(self, mapping_id):
        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM mappings WHERE id=?", (mapping_id,)).fetchone()
            if existing is None:
                raise KeyError(f"Mapping is absent: {mapping_id}")
            if existing["deleted_at"] is not None:
                connection.commit()
                return dict(existing) | {"changed": False}
            connection.execute("UPDATE mappings SET deleted_at=?,updated_at=?,version=version+1 WHERE id=?", (now, now, mapping_id))
            record = dict(connection.execute("SELECT * FROM mappings WHERE id=?", (mapping_id,)).fetchone())
            self._record_mapping_history(connection, record, "delete")
            connection.commit()
            return record | {"changed": True}

    def get_mapping_history(self, mapping_id) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute("SELECT version,action,changed_at,snapshot_json FROM mapping_history WHERE mapping_id=? ORDER BY version", (mapping_id,))
            return [{"version": row["version"], "action": row["action"], "changed_at": row["changed_at"], "snapshot": json.loads(row["snapshot_json"])} for row in rows]
