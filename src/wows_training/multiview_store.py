"""Immutable multiview recordings and bounded, data-only .wowsbattle packages.

Package hashes establish internal integrity, not authorship or semantic truth.
Raw replay headers are checked without decoding the encrypted packet body.
External reports can be recomputed from the retained raw files by the parser.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import struct
import tempfile
from uuid import uuid4
import zipfile


FORMAT_VERSION = 1
MULTIVIEW_SCHEMA_VERSION = 1
ENGINE_VERSION = "wows-multiview-1"
CLIENT_VERSION = "15.9.0.13357625"
MAX_RAW_BYTES = 64 * 1024 * 1024
MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_SOURCES = 16
MAX_COMPRESSION_RATIO = 200
MAGIC = b"\x12\x32\x34\x11"


class BattleRecordingError(ValueError):
    pass


def _hash(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise BattleRecordingError("Expected a lowercase SHA256 digest")
    return value


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as error:
        raise BattleRecordingError("Invalid JSON data") from error


def _decode(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BattleRecordingError("Duplicate JSON member")
            result[key] = value
        return result
    def invalid(_):
        raise BattleRecordingError("Nonfinite JSON number")
    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)
        _canonical(result)  # Also rejects overflowed 1e999 and nonfinite floats.
        if not isinstance(result, dict):
            raise BattleRecordingError("JSON document must be an object")
        return result
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise BattleRecordingError("Invalid JSON document") from error


def _version(value):
    if not isinstance(value, str):
        raise BattleRecordingError("Missing exact client version")
    parts = re.split(r"[.,_]", value)
    if len(parts) != 4 or not all(part.strip().isdigit() for part in parts):
        raise BattleRecordingError("An exact four-part client version is required")
    result = ".".join(str(int(part.strip())) for part in parts)
    if result != CLIENT_VERSION:
        raise BattleRecordingError("Unsupported replay client version")
    return result


def _identity(report):
    if not isinstance(report, dict) or report.get("status") == "failed":
        raise BattleRecordingError("A successfully parsed report is required")
    version = _version(report.get("replay_version"))
    arena = report.get("arena_id")
    if type(arena) not in (str, int) or not re.fullmatch(r"[0-9]{1,30}", str(arena)) or int(arena) <= 0:
        raise BattleRecordingError("A positive arena ID is required")
    metadata = report.get("metadata")
    if not isinstance(metadata, dict):
        raise BattleRecordingError("Missing report metadata")
    if metadata.get("clientVersionFromExe") is not None:
        _version(metadata["clientVersionFromExe"])
    common = report.get("post_battle_common") or {}
    if not isinstance(common, dict):
        raise BattleRecordingError("Invalid common-result structure")
    common_arena = common.get("arena_id")
    if common_arena is not None and str(common_arena) != str(arena):
        raise BattleRecordingError("Report contains conflicting arena IDs")
    map_id = metadata.get("mapName")
    if not isinstance(map_id, str) or not map_id:
        raise BattleRecordingError("Missing map identity")
    return f"arena:{version}:{int(arena)}", map_id.replace("\\", "/").rsplit("/", 1)[-1]


def _read_bounded(path, limit):
    with Path(path).open("rb") as stream:
        if os.fstat(stream.fileno()).st_size > limit:
            raise BattleRecordingError("File exceeds its size limit")
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise BattleRecordingError("File changed beyond its size limit")
    return value


def _file_hash(path, limit):
    digest, size = hashlib.sha256(), 0
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            size += len(block)
            if size > limit:
                raise BattleRecordingError("File exceeds its size limit")
            digest.update(block)
    return digest.hexdigest(), size


def _copy_checked(stream, target, expected_sha, limit, expected_size=None):
    digest, size = hashlib.sha256(), 0
    with Path(target).open("xb") as output:
        while block := stream.read(1024 * 1024):
            size += len(block)
            if size > limit:
                raise BattleRecordingError("Member exceeds its decoded size limit")
            digest.update(block)
            output.write(block)
    if digest.hexdigest() != expected_sha or (expected_size is not None and size != expected_size):
        raise BattleRecordingError("Member hash or size mismatch")
    return size


def _publish_file(staged, target):
    # Windows rename is atomic and refuses an existing destination, including
    # on filesystems without hardlink support. Unix rename would overwrite.
    if os.name == "nt":
        os.rename(staged, target)
    else:
        os.link(staged, target)


def _raw_header(path, report):
    with Path(path).open("rb") as stream:
        if stream.read(4) != MAGIC:
            raise BattleRecordingError("Invalid replay magic")
        count = stream.read(4)
        if len(count) != 4 or not 1 <= struct.unpack("<I", count)[0] <= 64:
            raise BattleRecordingError("Invalid replay header blocks")
        header, total = None, 0
        for index in range(struct.unpack("<I", count)[0]):
            length = stream.read(4)
            if len(length) != 4:
                raise BattleRecordingError("Truncated replay header length")
            size = struct.unpack("<I", length)[0]
            total += size
            if size > 1024 * 1024 or total > 4 * 1024 * 1024 or (index == 0 and size == 0):
                raise BattleRecordingError("Replay header exceeds limit")
            data = stream.read(size)
            if len(data) != size:
                raise BattleRecordingError("Truncated replay header")
            if index == 0:
                header = _decode(data)
        lengths = stream.read(8)
        if len(lengths) != 8:
            raise BattleRecordingError("Missing encrypted body lengths")
        output_size, encrypted_size = struct.unpack("<II", lengths)
        remainder = os.fstat(stream.fileno()).st_size - stream.tell()
        if output_size > 256 * 1024 * 1024 or encrypted_size != remainder or remainder < 8 or remainder % 8:
            raise BattleRecordingError("Invalid encrypted body framing")
    _version(header.get("clientVersionFromExe"))
    metadata = report["metadata"]
    for key in ("mapName", "playerName", "dateTime"):
        if key in metadata and header.get(key) != metadata[key]:
            raise BattleRecordingError("Raw replay header conflicts with its report")


def _manifest_id(manifest):
    return _digest(_canonical({key: value for key, value in manifest.items() if key != "recording_id"}))


def _validate_manifest(manifest):
    fields = {"format", "format_version", "client_version", "engine_version", "battle_id", "recording_id", "merged", "sources"}
    if set(manifest) != fields or manifest.get("format") != "wowsbattle" or type(manifest.get("format_version")) is not int or manifest["format_version"] != FORMAT_VERSION:
        raise BattleRecordingError("Unsupported battle package format")
    if manifest["engine_version"] != ENGINE_VERSION or manifest["client_version"] != CLIENT_VERSION:
        raise BattleRecordingError("Unsupported merge engine or client version")
    if not re.fullmatch(r"arena:15\.9\.0\.13357625:[1-9][0-9]*", str(manifest["battle_id"])):
        raise BattleRecordingError("Invalid battle identity")
    merged = manifest["merged"]
    if not isinstance(merged, dict) or set(merged) != {"member", "sha256", "size"} or merged["member"] != "merged/report.json":
        raise BattleRecordingError("Invalid merged report descriptor")
    _hash(merged["sha256"])
    if type(merged["size"]) is not int or not 1 <= merged["size"] <= MAX_REPORT_BYTES:
        raise BattleRecordingError("Merged report exceeds size limit")
    sources = manifest["sources"]
    if not isinstance(sources, list) or not 2 <= len(sources) <= MAX_SOURCES:
        raise BattleRecordingError("A battle recording requires 2 to 16 distinct sources")
    seen, total = set(), merged["size"]
    for source in sources:
        keys = {"sha256", "raw_member", "raw_size", "report_member", "report_sha256", "report_size", "analysis_revision", "source_name"}
        if not isinstance(source, dict) or set(source) != keys:
            raise BattleRecordingError("Unexpected source descriptor fields")
        value = _hash(source["sha256"])
        _hash(source["report_sha256"])
        if value in seen:
            raise BattleRecordingError("Duplicate replay source")
        seen.add(value)
        if source["raw_member"] != f"replays/{value}.wowsreplay" or source["report_member"] != f"reports/{value}.json":
            raise BattleRecordingError("Source member path is not permitted")
        for key, limit in (("raw_size", MAX_RAW_BYTES), ("report_size", MAX_REPORT_BYTES)):
            if type(source[key]) is not int or not 1 <= source[key] <= limit:
                raise BattleRecordingError("Source size exceeds its limit")
            total += source[key]
        if type(source["analysis_revision"]) is not int or source["analysis_revision"] < 1:
            raise BattleRecordingError("Invalid source analysis revision")
        name = source["source_name"]
        if not isinstance(name, str) or not 1 <= len(name) <= 255 or any(char in name for char in "\\/:\x00\r\n"):
            raise BattleRecordingError("Invalid source display filename")
    if total > MAX_TOTAL_BYTES:
        raise BattleRecordingError("Battle recording exceeds total size limit")
    if [source["sha256"] for source in sources] != sorted(seen):
        raise BattleRecordingError("Sources must be canonically ordered")
    if _hash(manifest["recording_id"]) != _manifest_id(manifest):
        raise BattleRecordingError("Recording identifier does not match manifest content")


def _validate_reports(merged, reports, manifest):
    multiview = merged.get("multiview")
    if (not isinstance(multiview, dict) or type(multiview.get("schema_version")) is not int
            or multiview["schema_version"] != MULTIVIEW_SCHEMA_VERSION or multiview.get("engine_version") != ENGINE_VERSION):
        raise BattleRecordingError("Unsupported multiview report schema or engine")
    expected = [source["sha256"] for source in manifest["sources"]]
    if multiview.get("source_shas") != expected or multiview.get("battle_id") != manifest["battle_id"]:
        raise BattleRecordingError("Merged source membership conflicts with manifest")
    if "sources" in multiview:
        rows = multiview["sources"]
        if not isinstance(rows, list) or not all(isinstance(row, dict) and isinstance(row.get("sha256"), str) for row in rows) or sorted(row.get("sha256", "") for row in rows) != expected:
            raise BattleRecordingError("Merged source attribution conflicts with membership")
    battle_key, map_id = _identity(merged)
    if battle_key != manifest["battle_id"]:
        raise BattleRecordingError("Merged arena conflicts with manifest")
    for descriptor, report in zip(manifest["sources"], reports):
        if report.get("sha256") != descriptor["sha256"] or report.get("multiview"):
            raise BattleRecordingError("Source report must describe its own original raw replay")
        if _identity(report) != (battle_key, map_id):
            raise BattleRecordingError("Source reports have conflicting arena, version or map")


class BattleRecordingStore:
    def __init__(self, root):
        requested = Path(root).expanduser().absolute()
        requested.mkdir(parents=True, exist_ok=True)
        self.root = requested.resolve()
        self.database_path = self.root / "multiview.sqlite3"
        for relative in ("raw_replays", "multiview/reports", "multiview/manifests", "multiview/staging"):
            self._path(relative).mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise BattleRecordingError("Unsupported multiview database version")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""CREATE TABLE IF NOT EXISTS recordings(
                id TEXT PRIMARY KEY,battle_id TEXT NOT NULL,engine_version TEXT NOT NULL,
                manifest_relative_path TEXT NOT NULL,manifest_sha256 TEXT NOT NULL,
                merged_report_sha256 TEXT NOT NULL,source_count INTEGER NOT NULL,created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS recordings_battle ON recordings(battle_id,created_at);
                PRAGMA user_version=1;""")

    def _path(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise BattleRecordingError("Stored path escapes data root")
        return path

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def _raw_path(self, value):
        return self._path(f"raw_replays/{_hash(value)}.wowsreplay")

    def _report_path(self, value):
        return self._path(f"multiview/reports/{_hash(value)}.json")

    @staticmethod
    def _report_bytes(value):
        if isinstance(value, dict):
            try:
                data = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            except (ValueError, TypeError, RecursionError) as error:
                raise BattleRecordingError("Invalid source report") from error
        elif isinstance(value, (str, Path)):
            data = _read_bounded(value, MAX_REPORT_BYTES)
        else:
            raise BattleRecordingError("Source report must be JSON data or an explicit JSON file path")
        if len(data) > MAX_REPORT_BYTES:
            raise BattleRecordingError("Report exceeds its size limit")
        return data, _decode(data)

    def save(self, report, sources):
        if not isinstance(report, dict) or not isinstance(sources, list) or not 2 <= len(sources) <= MAX_SOURCES:
            raise BattleRecordingError("Merged report and 2 to 16 sources are required")
        merged_bytes = _canonical(report)
        if len(merged_bytes) > MAX_REPORT_BYTES:
            raise BattleRecordingError("Merged report exceeds its size limit")
        manifest = {"format": "wowsbattle", "format_version": FORMAT_VERSION, "client_version": CLIENT_VERSION,
                    "engine_version": ENGINE_VERSION, "battle_id": _identity(report)[0],
                    "merged": {"member": "merged/report.json", "sha256": _digest(merged_bytes), "size": len(merged_bytes)}, "sources": []}
        with tempfile.TemporaryDirectory(dir=self._path("multiview/staging")) as temporary:
            stage = Path(temporary).resolve()
            stage.relative_to(self._path("multiview/staging"))
            members, decoded = {}, {}
            merged_path = stage / "merged.json"
            merged_path.write_bytes(merged_bytes)
            members["merged/report.json"] = merged_path
            total = len(merged_bytes)
            for item in sources:
                if not isinstance(item, dict):
                    raise BattleRecordingError("Invalid source descriptor")
                value = _hash(item.get("sha256"))
                if value in decoded:
                    raise BattleRecordingError("Duplicate source replay")
                data, original = self._report_bytes(item.get("report"))
                _identity(original)
                if original.get("sha256") != value or original.get("multiview"):
                    raise BattleRecordingError("Source report must describe its own original raw replay")
                expected = item.get("report_sha256")
                if expected is not None and _hash(expected) != _digest(data):
                    raise BattleRecordingError("Source report changed from its expected hash")
                path = Path(item["source_path"])
                if path.suffix.lower() != ".wowsreplay" or not path.is_file() or path.stat().st_size > MAX_RAW_BYTES:
                    raise BattleRecordingError("Invalid or oversized raw replay source")
                staged_raw = stage / (value + ".wowsreplay")
                with path.open("rb") as stream:
                    raw_size = _copy_checked(stream, staged_raw, value, MAX_RAW_BYTES)
                _raw_header(staged_raw, original)
                report_file = stage / (value + ".json")
                report_file.write_bytes(data)
                raw_member, report_member = f"replays/{value}.wowsreplay", f"reports/{value}.json"
                original_name = original.get("replay")
                name = str(original_name).replace("\\", "/").rsplit("/", 1)[-1] if isinstance(original_name, str) else path.name
                manifest["sources"].append(dict(sha256=value, raw_member=raw_member, raw_size=raw_size,
                    report_member=report_member, report_sha256=_digest(data), report_size=len(data),
                    analysis_revision=item.get("analysis_revision"), source_name=name))
                members[raw_member], members[report_member], decoded[value] = staged_raw, report_file, original
                total += raw_size + len(data)
                if total > MAX_TOTAL_BYTES:
                    raise BattleRecordingError("Battle recording exceeds total size limit")
            manifest["sources"].sort(key=lambda row: row["sha256"])
            manifest["recording_id"] = _manifest_id(manifest)
            _validate_manifest(manifest)
            _validate_reports(report, [decoded[row["sha256"]] for row in manifest["sources"]], manifest)
            return self._commit(manifest, members)

    def retain_source(self, source):
        """Retain one validated input while waiting for another perspective.

        No merged-record row is created. The returned descriptor can be passed
        to save later, or its raw_path can be registered with the P1 Library.
        """
        if not isinstance(source, dict):
            raise BattleRecordingError("Invalid source descriptor")
        value = _hash(source.get("sha256"))
        data, report = self._report_bytes(source.get("report"))
        _identity(report)
        if report.get("sha256") != value or report.get("multiview"):
            raise BattleRecordingError("Expected an original source report")
        revision = source.get("analysis_revision")
        if type(revision) is not int or revision < 1:
            raise BattleRecordingError("Invalid source analysis revision")
        report_hash = _digest(data)
        if source.get("report_sha256") is not None and _hash(source["report_sha256"]) != report_hash:
            raise BattleRecordingError("Source report changed from its expected hash")
        path = Path(source["source_path"])
        if path.suffix.lower() != ".wowsreplay" or not path.is_file() or path.stat().st_size > MAX_RAW_BYTES:
            raise BattleRecordingError("Invalid or oversized raw replay source")
        with tempfile.TemporaryDirectory(dir=self._path("multiview/staging")) as temporary:
            stage = Path(temporary).resolve()
            stage.relative_to(self._path("multiview/staging"))
            raw, report_file = stage / "raw", stage / "report"
            with path.open("rb") as stream:
                size = _copy_checked(stream, raw, value, MAX_RAW_BYTES)
            _raw_header(raw, report)
            report_file.write_bytes(data)
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                for staged, target, expected, limit in (
                    (raw, self._raw_path(value), value, MAX_RAW_BYTES),
                    (report_file, self._report_path(report_hash), report_hash, MAX_REPORT_BYTES),
                ):
                    if target.exists():
                        if _file_hash(target, limit)[0] != expected:
                            raise BattleRecordingError("Existing immutable object failed integrity check")
                    else:
                        try:
                            _publish_file(staged, target)
                        except FileExistsError:
                            if _file_hash(target, limit)[0] != expected:
                                raise BattleRecordingError("Concurrent immutable object conflict")
                db.commit()
        raw_path = str(self._raw_path(value))
        original_name = report.get("replay")
        name = original_name.replace("\\", "/").rsplit("/", 1)[-1] if isinstance(original_name, str) else path.name
        return dict(sha256=value, analysis_revision=revision, report_sha256=report_hash, raw_path=raw_path,
                    source_path=raw_path, report_path=str(self._report_path(report_hash)), report_size=len(data),
                    raw_size=size, source_name=name)

    def _commit(self, manifest, members):
        manifest_bytes = _canonical(manifest)
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise BattleRecordingError("Manifest exceeds limit")
        recording_id = manifest["recording_id"]
        relative = f"multiview/manifests/{recording_id}.json"
        targets = [(members["merged/report.json"], self._report_path(manifest["merged"]["sha256"]), manifest["merged"]["sha256"], MAX_REPORT_BYTES)]
        for source in manifest["sources"]:
            targets += [(members[source["raw_member"]], self._raw_path(source["sha256"]), source["sha256"], MAX_RAW_BYTES),
                        (members[source["report_member"]], self._report_path(source["report_sha256"]), source["report_sha256"], MAX_REPORT_BYTES)]
        manifest_stage = next(iter(members.values())).parent / "manifest.json"
        manifest_stage.write_bytes(manifest_bytes)
        targets.append((manifest_stage, self._path(relative), _digest(manifest_bytes), MAX_MANIFEST_BYTES))
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM recordings WHERE id=?", (recording_id,)).fetchone()
            if existing and existing["manifest_sha256"] != _digest(manifest_bytes):
                raise BattleRecordingError("Recording ID conflicts with existing manifest")
            # Publish content without replacing existing files. Unindexed objects
            # may remain after an SQL failure; they cannot invalidate prior runs.
            for staged, target, expected, limit in targets:
                if target.exists():
                    if _file_hash(target, limit)[0] != expected:
                        raise BattleRecordingError("Existing immutable object failed integrity check")
                    continue
                try:
                    _publish_file(staged, target)
                except FileExistsError:
                    if _file_hash(target, limit)[0] != expected:
                        raise BattleRecordingError("Concurrent immutable object conflict")
            if existing is None:
                db.execute("INSERT INTO recordings VALUES(?,?,?,?,?,?,?,?)", (recording_id, manifest["battle_id"], ENGINE_VERSION,
                    relative, _digest(manifest_bytes), manifest["merged"]["sha256"], len(manifest["sources"]), datetime.now(timezone.utc).isoformat()))
            db.commit()
        return self.get(recording_id)

    def get(self, recording_id):
        _hash(recording_id)
        with self._connection() as db:
            row = db.execute("SELECT * FROM recordings WHERE id=?", (recording_id,)).fetchone()
        if row is None:
            raise KeyError(recording_id)
        value = dict(row)
        expected_path = f"multiview/manifests/{recording_id}.json"
        if value["manifest_relative_path"] != expected_path:
            raise BattleRecordingError("Stored manifest path is invalid")
        path = self._path(expected_path)
        data = _read_bounded(path, MAX_MANIFEST_BYTES)
        if _digest(data) != value["manifest_sha256"]:
            raise BattleRecordingError("Stored manifest failed hash verification")
        manifest = _decode(data)
        _validate_manifest(manifest)
        if (manifest["recording_id"] != recording_id or manifest["battle_id"] != value["battle_id"]
                or manifest["engine_version"] != value["engine_version"] or len(manifest["sources"]) != value["source_count"]
                or manifest["merged"]["sha256"] != value["merged_report_sha256"]):
            raise BattleRecordingError("Stored record identity conflict")
        sources = []
        for source in manifest["sources"]:
            raw = str(self._raw_path(source["sha256"]))
            sources.append({key: source[key] for key in ("sha256", "analysis_revision", "report_sha256", "source_name", "raw_size", "report_size")}
                           | {"raw_path": raw, "source_path": raw, "report_path": str(self._report_path(source["report_sha256"]))})
        value.update(recording_id=recording_id, format_version=FORMAT_VERSION, manifest_path=str(path),
                     report_path=str(self._report_path(manifest["merged"]["sha256"])), sources=sources)
        value.pop("manifest_relative_path")
        return value

    def list_recordings(self, battle_id=None):
        with self._connection() as db:
            query, params = "SELECT id FROM recordings", ()
            if battle_id is not None:
                query += " WHERE battle_id=?"
                params = (str(battle_id),)
            ids = [row[0] for row in db.execute(query + " ORDER BY created_at,id", params)]
        return [self.get(value) for value in ids]

    def load_report(self, recording_id):
        record = self.get(recording_id)
        data = _read_bounded(record["report_path"], MAX_REPORT_BYTES)
        if _digest(data) != record["merged_report_sha256"]:
            raise BattleRecordingError("Merged report failed hash verification")
        return _decode(data)

    def source_reports(self, recording_id):
        result = []
        for source in self.get(recording_id)["sources"]:
            data = _read_bounded(source["report_path"], MAX_REPORT_BYTES)
            if _digest(data) != source["report_sha256"]:
                raise BattleRecordingError("Source report failed hash verification")
            result.append(dict(source, report=_decode(data)))
        return result

    def export_package(self, recording_id, path):
        record = self.get(recording_id)
        manifest_data = _read_bounded(record["manifest_path"], MAX_MANIFEST_BYTES)
        manifest = _decode(manifest_data)
        self.load_report(recording_id)
        self.source_reports(recording_id)
        files = [("merged/report.json", Path(record["report_path"]), manifest["merged"]["sha256"], MAX_REPORT_BYTES)]
        for source, stored in zip(manifest["sources"], record["sources"]):
            files += [(source["raw_member"], Path(stored["raw_path"]), source["sha256"], MAX_RAW_BYTES),
                      (source["report_member"], Path(stored["report_path"]), source["report_sha256"], MAX_REPORT_BYTES)]
        target = Path(path).expanduser().absolute()
        if target.suffix.lower() != ".wowsbattle":
            raise BattleRecordingError("Export path must end with .wowsbattle")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(target)
        temporary = target.parent / ("." + uuid4().hex + ".writing")
        try:
            with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.writestr("manifest.json", manifest_data)
                for member, source, expected, limit in files:
                    if _file_hash(source, limit)[0] != expected:
                        raise BattleRecordingError("Stored package object changed")
                    archive.write(source, member, compress_type=zipfile.ZIP_STORED if member.endswith(".wowsreplay") else zipfile.ZIP_DEFLATED)
            # Verify the produced archive, including races during ZIP writes.
            with self._package(temporary) as _:
                pass
            _publish_file(temporary, target)
            return str(target.resolve())
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _package(self, path):
        path = Path(path)
        if not path.is_file() or path.stat().st_size > MAX_TOTAL_BYTES + 4 * 1024 * 1024:
            raise BattleRecordingError("Package exceeds archive size limit")
        try:
            with zipfile.ZipFile(path, "r") as archive, tempfile.TemporaryDirectory(dir=self._path("multiview/staging")) as temporary:
                stage = Path(temporary).resolve()
                stage.relative_to(self._path("multiview/staging"))
                infos = archive.infolist()
                if not 6 <= len(infos) <= 2 + 2 * MAX_SOURCES:
                    raise BattleRecordingError("Unexpected package member count")
                names = set()
                for info in infos:
                    name = info.filename
                    if (len(name) > 160 or "\\" in name or ":" in name or "\x00" in name or name.startswith("/")
                            or any(part in (".", "..", "") for part in name.split("/")) or PurePosixPath(name).is_absolute()
                            or name.casefold() in names or info.is_dir()):
                        raise BattleRecordingError("Unsafe or duplicate package member")
                    names.add(name.casefold())
                    mode = stat.S_IFMT(info.external_attr >> 16)
                    if mode not in (0, stat.S_IFREG) or info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        raise BattleRecordingError("Unsupported member type or compression")
                    if info.file_size > MAX_REPORT_BYTES or (info.file_size and info.file_size / max(1, info.compress_size) > MAX_COMPRESSION_RATIO):
                        raise BattleRecordingError("Oversized or excessively compressed member")
                if sum(info.file_size for info in infos) > MAX_TOTAL_BYTES:
                    raise BattleRecordingError("Package exceeds decoded size budget")
                if "manifest.json" not in archive.namelist() or archive.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
                    raise BattleRecordingError("Missing or oversized manifest")
                with archive.open("manifest.json") as stream:
                    manifest_data = stream.read(MAX_MANIFEST_BYTES + 1)
                manifest = _decode(manifest_data)
                _validate_manifest(manifest)
                expected = {"merged/report.json": (manifest["merged"]["sha256"], manifest["merged"]["size"], MAX_REPORT_BYTES)}
                for source in manifest["sources"]:
                    expected[source["raw_member"]] = (source["sha256"], source["raw_size"], MAX_RAW_BYTES)
                    expected[source["report_member"]] = (source["report_sha256"], source["report_size"], MAX_REPORT_BYTES)
                if set(archive.namelist()) != set(expected) | {"manifest.json"}:
                    raise BattleRecordingError("Package members do not match the strict manifest whitelist")
                members = {}
                for index, (member, (expected_sha, size, limit)) in enumerate(expected.items()):
                    if archive.getinfo(member).file_size != size:
                        raise BattleRecordingError("Manifest size disagrees with ZIP metadata")
                    output = stage / str(index)
                    with archive.open(member) as stream:
                        _copy_checked(stream, output, expected_sha, limit, size)
                    members[member] = output
                merged = _decode(_read_bounded(members["merged/report.json"], MAX_REPORT_BYTES))
                reports = []
                for source in manifest["sources"]:
                    original = _decode(_read_bounded(members[source["report_member"]], MAX_REPORT_BYTES))
                    _raw_header(members[source["raw_member"]], original)
                    reports.append(original)
                _validate_reports(merged, reports, manifest)
                yield manifest, members
        except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, EOFError) as error:
            raise BattleRecordingError("Invalid or corrupt battle package") from error

    def import_package(self, path):
        with self._package(path) as (manifest, members):
            return self._commit(manifest, members)
