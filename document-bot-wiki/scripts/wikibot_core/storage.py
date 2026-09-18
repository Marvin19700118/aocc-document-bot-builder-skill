from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path


class BotError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def home_path(value=None):
    return Path(value or os.environ.get("DOCUMENT_BOT_WIKI_HOME") or Path.home() / ".document-bot-wiki").expanduser().resolve()


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, sha256 TEXT NOT NULL, path TEXT NOT NULL,
 size INTEGER NOT NULL, created REAL NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
 wiki_status TEXT NOT NULL DEFAULT 'pending', delete_requested INTEGER NOT NULL DEFAULT 0,
 error TEXT, retired INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS document_content ON documents(sha256) WHERE retired=0;
CREATE TABLE IF NOT EXISTS jobs(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 doc_id TEXT NOT NULL REFERENCES documents(id), kind TEXT NOT NULL,
 wiki_enabled INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
 created REAL NOT NULL, updated REAL NOT NULL, error TEXT, wiki_cursor INTEGER NOT NULL DEFAULT 0,
 replace_id TEXT, attempt INTEGER NOT NULL DEFAULT 1);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(doc_id)
 WHERE state IN ('queued','indexing','indexed','awaiting_assistant','compiling');
CREATE TABLE IF NOT EXISTS chunks(
 id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT NOT NULL REFERENCES documents(id),
 ordinal INTEGER NOT NULL, text TEXT NOT NULL, locator TEXT NOT NULL,
 UNIQUE(doc_id,ordinal));
CREATE TABLE IF NOT EXISTS wiki_batches(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), chunk_ids TEXT NOT NULL,
 start_cursor INTEGER NOT NULL, end_cursor INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 owner TEXT NOT NULL, lease_until REAL NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS wiki_claims(
 id TEXT PRIMARY KEY, topic TEXT NOT NULL, title TEXT NOT NULL, claim TEXT NOT NULL,
 chunk_id INTEGER NOT NULL REFERENCES chunks(id), quote TEXT NOT NULL,
 batch_id TEXT NOT NULL REFERENCES wiki_batches(id));
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, job_id TEXT, doc_id TEXT,
 payload TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS consumers(
 id TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0, owner TEXT, lease_until REAL NOT NULL DEFAULT 0,
 delivered_through INTEGER NOT NULL DEFAULT 0);
"""


class Store:
    def __init__(self, home, initialize=False):
        self.home = home_path(home)
        if not initialize and not (self.home / "library.sqlite3").exists():
            raise BotError("not_configured", "請先執行 setup。")
        self.home.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.home / "library.sqlite3", timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        has_settings = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
        if has_settings and self.setting("schema_version") not in (None, "wiki-1"):
            self.db.close()
            raise BotError("schema_mismatch", "不可使用向量版資料庫；請使用獨立的 Wiki home。")
        if initialize:
            self.db.executescript(SCHEMA)
        version = self.setting("schema_version")
        if version and version != "wiki-1":
            self.db.close()
            raise BotError("schema_mismatch", "資料庫版本不相容；請使用對應版本的 skill。")

    def close(self):
        self.db.close()

    def setting(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        self.db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def event(self, kind, job=None, doc=None, **payload):
        self.db.execute("INSERT INTO events(kind,job_id,doc_id,payload,created) VALUES(?,?,?,?,?)", (kind, job, doc, json.dumps(payload, ensure_ascii=False), time.time()))

    def resolve_doc(self, selector):
        rows = self.db.execute("SELECT * FROM documents WHERE retired=0 AND (id=? OR name=?)", (selector, selector)).fetchall()
        if not rows:
            raise BotError("document_not_found", "找不到文件。")
        if len(rows) != 1:
            raise BotError("ambiguous_document", "有同名文件，請指定文件 ID。", candidates=[dict(r) for r in rows])
        return rows[0]


class FileLock:
    """OS-owned lock: released by the kernel on crash; never unlink lock files."""
    def __init__(self, path, wait_seconds=0):
        self.path, self.file, self.wait_seconds = Path(path), None, wait_seconds

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        deadline = time.monotonic() + self.wait_seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() < deadline:
                    time.sleep(.05)
                    continue
                self.file.close()
                self.file = None
                raise BotError("worker_busy", "另一個處理者正在執行，此操作尚未取得鎖定。") from exc
        return self

    def __exit__(self, *_):
        if self.file:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()


ACTIVE = ('queued', 'indexing', 'indexed', 'awaiting_assistant', 'compiling')
