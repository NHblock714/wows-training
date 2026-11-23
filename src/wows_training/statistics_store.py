"""Hashed, immutable statistics caches separate from replay analyses."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import os


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')
def digest(value):return hashlib.sha256(canonical(value)).hexdigest()


class StatisticsStore:
    def __init__(self,root):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.objects=self.root/'statistics/objects';self.objects.mkdir(parents=True,exist_ok=True)
        self.db=self.root/'statistics.sqlite3'
        with self.connection() as db:
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0,1):raise ValueError('Unsupported statistics database schema')
            db.executescript('CREATE TABLE IF NOT EXISTS objects(kind TEXT,cache_key TEXT,content_sha TEXT,relative_path TEXT,PRIMARY KEY(kind,cache_key));PRAGMA user_version=1;')

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.db,timeout=30);db.row_factory=sqlite3.Row
        try:yield db;db.commit()
        finally:db.close()

    def get(self,kind,key):
        with self.connection() as db:row=db.execute('SELECT * FROM objects WHERE kind=? AND cache_key=?',(kind,key)).fetchone()
        if row is None:return None
        path=(self.root/row['relative_path']).resolve()
        if not path.is_relative_to(self.objects.resolve()) or path.stat().st_size>64*1024*1024:raise ValueError('Statistics object path or size is invalid')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=row['content_sha']:raise ValueError('Statistics object integrity check failed')
        return json.loads(raw)

    def save(self,kind,key,value):
        raw=canonical(value);sha=hashlib.sha256(raw).hexdigest()
        if len(raw)>64*1024*1024:raise ValueError('Statistics object is too large')
        target=self.objects/(sha+'.json')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT content_sha FROM objects WHERE kind=? AND cache_key=?',(kind,key)).fetchone()
            if prior and prior['content_sha']!=sha:raise ValueError('The same statistics input produced conflicting results')
            if target.exists():
                if hashlib.sha256(target.read_bytes()).hexdigest()!=sha:raise ValueError('Statistics object changed')
            else:
                with tempfile.NamedTemporaryFile(dir=self.objects,delete=False,suffix='.writing') as stream:
                    stream.write(raw);temporary=Path(stream.name)
                os.replace(temporary,target)
            db.execute('INSERT OR IGNORE INTO objects VALUES(?,?,?,?)',(kind,key,sha,target.relative_to(self.root).as_posix()))
        return value
