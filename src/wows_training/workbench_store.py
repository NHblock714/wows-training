"""Versioned workbench documents and immutable analysis runs, separate from P1."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ValueError(f"{field} must be nonempty text of at most 1000 characters")
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


class WorkbenchStore:
    """Short SQLite transactions are safe for UI and worker callers.

    Documents contain JSON object payloads. Lists return the current document
    revision only; older revisions remain retrievable. Runs and reviews append.
    Credentials belong in SecretStore, never in these unencrypted payloads.
    """

    def __init__(self, root):
        root = Path(root).expanduser().absolute()
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()
        self.database_path = self.root / "workbench.sqlite3"
        with self._connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unsupported workbench database version")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS documents(
                    id TEXT NOT NULL, revision INTEGER NOT NULL, kind TEXT NOT NULL,
                    name TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(id,revision));
                CREATE INDEX IF NOT EXISTS document_kind ON documents(kind,id,revision);
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
                    payload_json TEXT NOT NULL, input_fingerprint_json TEXT,
                    created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS run_kind ON runs(kind,created_at);
                CREATE TABLE IF NOT EXISTS reviews(
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    sequence INTEGER NOT NULL, decision TEXT NOT NULL, comment TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(run_id,sequence));
                PRAGMA user_version=1;
            """)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _decode(row):
        value = dict(row)
        if "payload_json" in value:
            value["payload"] = json.loads(value.pop("payload_json"))
        if "input_fingerprint_json" in value:
            encoded = value.pop("input_fingerprint_json")
            value["input_fingerprint"] = json.loads(encoded) if encoded is not None else None
        return value

    def save_document(self, kind, name, payload, document_id=None):
        return self.save_documents([dict(kind=kind, name=name, payload=payload, document_id=document_id)])[0]

    def save_documents(self, items):
        """Atomically save a reviewed batch. Explicit new UUIDs need create=True.

        Each item has kind/name/payload and optional document_id/create. This
        lets a player and assignments reference a preallocated stable UUID in
        one transaction. Any invalid item or write failure rolls back the batch.
        """
        if not isinstance(items, list) or not items or len(items) > 2000:
            raise ValueError("Document batch must contain 1 to 2000 items")
        prepared, seen = [], set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
                raise ValueError("Document payload must be a JSON object")
            kind, name = _text(item.get("kind"), "kind"), _text(item.get("name"), "name")
            document_id = item.get("document_id")
            if document_id is not None:
                _text(document_id, "document_id")
                if document_id in seen:
                    raise ValueError("A batch cannot modify the same document twice")
                seen.add(document_id)
            prepared.append((kind, name, _json(item["payload"]), document_id, item.get("create") is True))
        records = []
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for kind, name, encoded, document_id, create in prepared:
                revision = 1
                if document_id is not None:
                    row = db.execute("SELECT kind,MAX(revision) AS revision FROM documents WHERE id=? GROUP BY kind", (document_id,)).fetchone()
                    if row is None and not create:
                        raise KeyError(document_id)
                    if row is not None:
                        if create:
                            raise ValueError("New document ID already exists")
                        if row["kind"] != kind:
                            raise ValueError("A document's kind cannot change")
                        revision = row["revision"] + 1
                else:
                    document_id = str(uuid4())
                record = dict(id=document_id, kind=kind, name=name, revision=revision, payload=json.loads(encoded), created_at=_now())
                db.execute("INSERT INTO documents VALUES(?,?,?,?,?,?)", (document_id, revision, kind, name, encoded, record["created_at"]))
                records.append(record)
            db.commit()
        return records

    def list_documents(self, kind=None):
        query = "SELECT d.* FROM documents d JOIN (SELECT id,MAX(revision) AS revision FROM documents GROUP BY id) c USING(id,revision)"
        params = ()
        if kind is not None:
            query += " WHERE d.kind=?"
            params = (_text(kind, "kind"),)
        with self._connection() as db:
            return [self._decode(row) for row in db.execute(query + " ORDER BY d.created_at,d.id", params)]

    def get_document(self, document_id, revision=None):
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ValueError("Revision must be a positive integer")
        query, params = "SELECT * FROM documents WHERE id=?", (document_id,)
        if revision is not None:
            query += " AND revision=?"
            params += (revision,)
        with self._connection() as db:
            row = db.execute(query + " ORDER BY revision DESC LIMIT 1", params).fetchone()
            if row is None:
                raise KeyError(document_id)
            return self._decode(row)

    def save_run(self, kind, name, payload, input_fingerprint=None):
        kind, name = _text(kind, "kind"), _text(name, "name")
        if not isinstance(payload, dict):
            raise ValueError("Run payload must be a JSON object")
        encoded, fingerprint = _json(payload), _json(input_fingerprint)
        run_id, created = str(uuid4()), _now()
        with self._connection() as db:
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)", (run_id, kind, name, encoded, fingerprint, created))
        return self.get_run(run_id)

    def list_runs(self, kind=None):
        query, params = "SELECT * FROM runs", ()
        if kind is not None:
            query += " WHERE kind=?"
            params = (_text(kind, "kind"),)
        with self._connection() as db:
            return [self._decode(row) for row in db.execute(query + " ORDER BY created_at,id", params)]

    def get_run(self, run_id):
        with self._connection() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            return self._decode(row)

    def run_index(self, kinds, *, require_evidence=False):
        """List menu entries without deserializing unrelated snapshots or full battle timelines."""
        if not kinds:
            return []
        where = 'kind IN (' + ','.join('?' for _ in kinds) + ')'
        if require_evidence:
            where += " AND EXISTS(SELECT 1 FROM json_each(runs.payload_json,'$.evidence') LIMIT 1)"
        columns = "id,kind,name,created_at,json_extract(payload_json,'$.report_context.layout.title') AS report_title"
        with self._connection() as db:
            return [dict(row) for row in db.execute(
                f"SELECT {columns} FROM runs WHERE {where} ORDER BY created_at DESC,id DESC", tuple(kinds))]

    def add_review(self, run_id, decision, comment):
        decision = _text(decision, "decision")
        if not isinstance(comment, str):
            raise ValueError("Review comment must be text")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
                raise KeyError(run_id)
            sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM reviews WHERE run_id=?", (run_id,)).fetchone()[0]
            record = dict(id=str(uuid4()), run_id=run_id, sequence=sequence, decision=decision, comment=comment, created_at=_now())
            db.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?)", tuple(record[key] for key in ("id", "run_id", "sequence", "decision", "comment", "created_at")))
            db.commit()
            return record

    def list_reviews(self, run_id):
        with self._connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM reviews WHERE run_id=? ORDER BY sequence", (run_id,))]
