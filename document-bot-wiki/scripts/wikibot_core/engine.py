from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from .qmd import QmdIndex
from .markdown import split_passages, MarkdownMixin
from .parsing import SUPPORTED, parse
from .storage import ACTIVE, BotError, FileLock, Store
from .wiki import WikiMixin
from .retrieval import RetrievalMixin


def uid():
    return uuid.uuid4().hex


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


class Bot(WikiMixin, RetrievalMixin, MarkdownMixin):
    def __init__(self, home=None, initialize=False, qmd=None):
        self.store = Store(home, initialize=initialize)
        self.db = self.store.db
        self.qmd = qmd or QmdIndex(self.store.home)

    def setup(self, workspace, download=True):
        workspace = Path(workspace).resolve()
        if not workspace.is_dir():
            raise BotError("workspace_missing", "setup 需要既有工作目錄。")
        with FileLock(self.store.home / "setup.lock"), FileLock(self.store.home / "worker.lock", wait_seconds=2):
            registered = self.store.setting("workspace")
            if registered and Path(registered) != workspace:
                raise BotError("workspace_already_registered", "知識庫已有工作目錄，不會自動改綁。", workspace=registered)
            root = workspace / "bot documents"
            if root.is_symlink() or root.resolve() != root:
                raise BotError("unsafe_document_root", "bot documents 不可指向工作目錄外的位置。")
            root.mkdir(exist_ok=True)
            with self.store.transaction():
                self.store.set_setting("schema_version", "wiki-1")
                self.store.set_setting("workspace", workspace)
                if self.store.setting("wiki_enabled") is None:
                    self.store.set_setting("wiki_enabled", "1")
            self.store.set_setting("projection_dirty", "1")
            self._sync_projection()
        return self.status()

    def root(self):
        workspace = self.store.setting("workspace")
        if not workspace:
            raise BotError("not_configured", "請先執行 setup。")
        return Path(workspace) / "bot documents"

    def owned_path(self, doc):
        root = self.root()
        path = Path(doc["path"])
        expected = root / doc["id"] / doc["name"]
        if path != expected or root.is_symlink() or path.parent.is_symlink() or path.is_symlink():
            raise BotError("unsafe_document_path", "文件路徑不是知識庫管理的實體副本。")
        if path.resolve() != expected or root.resolve() != root or path.resolve().parent.parent != root:
            raise BotError("unsafe_document_path", "文件路徑超出已登記的 bot documents。")
        return path

    def add(self, manifest, choices=None):
        if manifest.get("source") != "chat_attachment" or manifest.get("host") not in {"claude", "codex"}:
            raise BotError("unsupported_attachment", "只能由助手提供可讀取原檔的聊天附件清單。")
        attachments = manifest.get("attachments")
        if not isinstance(attachments, list) or not attachments:
            raise BotError("unsupported_attachment", "沒有可存取的附件原檔，無法匯入。")
        for a in attachments:
            if not isinstance(a, dict) or not a.get("attachment_id") or not a.get("path") or not a.get("name"):
                raise BotError("unsupported_attachment", "附件缺少原檔位置、名稱或宿主附件識別。")
        if not manifest.get("order_known", False):
            attachments = sorted(attachments, key=lambda a: (a["name"].casefold(), a["attachment_id"]))
        results = []
        for attachment in attachments:
            try:
                results.append(self._enqueue_attachment(attachment, (choices or {}).get(attachment["attachment_id"])))
            except (BotError, OSError) as exc:
                result = {"name": attachment["name"], "attachment_id": attachment["attachment_id"], "status": "not_queued", "error": str(exc), "code": getattr(exc, "code", "file_error")}
                result.update(getattr(exc, "details", {}))
                results.append(result)
        return {"files": results}

    def _enqueue_attachment(self, attachment, choice):
        source, name = Path(attachment["path"]), attachment["name"]
        if not source.is_absolute() or not source.is_file():
            raise BotError("unsupported_attachment", "宿主未提供可讀取的附件原檔。")
        if not isinstance(name, str) or name in {".", ".."} or any(c in name for c in '/\\\x00:') or name != Path(name).name:
            raise BotError("invalid_filename", "附件名稱不是有效的單一檔名。")
        if Path(name).suffix.lower() not in SUPPORTED:
            raise BotError("unsupported_format", "只支援 PDF、DOCX、Markdown、TXT。")
        doc_id, job_id = uid(), uid()
        target = self.root() / doc_id / name
        # Copy first: the host's temporary attachment may disappear after this turn.
        # A registry lock coordinates staging with crash cleanup, never with indexing.
        with FileLock(self.store.home / "ingest.lock", wait_seconds=20):
            self._remove_abandoned_staging()
            self.owned_path({"id": doc_id, "name": name, "path": str(target)})
            target.parent.mkdir(parents=True)
            marker = target.parent / ".wiki-ingest.json"
            marker.write_text(json.dumps({"id": doc_id, "name": name}), encoding="utf-8")
            registered = False
            try:
                shutil.copyfile(source, target)
                with target.open("r+b") as stream:
                    os.fsync(stream.fileno())
                fingerprint = digest(target)
                with self.store.transaction():
                    existing = self.db.execute("SELECT * FROM documents WHERE sha256=? AND retired=0", (fingerprint,)).fetchone()
                    if existing:
                        if existing["delete_requested"]:
                            raise BotError("deletion_pending", "同內容文件正在刪除，請完成清理後再加入。")
                        job = self.db.execute("SELECT * FROM jobs WHERE doc_id=? ORDER BY seq DESC LIMIT 1", (existing["id"],)).fetchone()
                        return {"status": "duplicate", "name": name, "doc_id": existing["id"], "job_id": job["id"] if job else None, "state": job["state"] if job else existing["status"], "position": self.position(job["seq"]) if job and job["state"] in ACTIVE else None}
                    collisions = self.db.execute("SELECT id,name,status FROM documents WHERE name=? AND retired=0 AND delete_requested=0", (name,)).fetchall()
                    replacement = None
                    if collisions:
                        if not choice or choice.get("action") not in {"keep", "replace"}:
                            raise BotError("name_conflict", "同名內容不同，請選擇保留兩份或取代。", candidates=[dict(r) for r in collisions])
                        if choice["action"] == "replace":
                            replacement = choice.get("document_id")
                            if replacement not in {r["id"] for r in collisions}:
                                raise BotError("replacement_target_required", "取代必須指定同名舊文件 ID。")
                            active = self.db.execute("SELECT id FROM jobs WHERE doc_id=? AND state IN ('queued','indexing','indexed','awaiting_assistant','compiling')", (replacement,)).fetchone()
                            if active:
                                raise BotError("replacement_busy", "舊文件仍在處理，請等完成後再取代。")
                            pending = self.db.execute("SELECT id FROM jobs WHERE replace_id=? AND state IN ('queued','indexing','indexed','awaiting_assistant','compiling')", (replacement,)).fetchone()
                            if pending:
                                raise BotError("replacement_busy", "已有取代工作正在排隊。")
                    now = time.time()
                    wiki = self.store.setting("wiki_enabled", "1") == "1"
                    self.db.execute("INSERT INTO documents(id,name,sha256,path,size,created,wiki_status) VALUES(?,?,?,?,?,?,?)", (doc_id, name, fingerprint, str(target), target.stat().st_size, now, "pending" if wiki else "disabled"))
                    cursor = self.db.execute("INSERT INTO jobs(id,doc_id,kind,wiki_enabled,created,updated,replace_id) VALUES(?,?,?,?,?,?,?)", (job_id, doc_id, "ingest", int(wiki), now, now, replacement))
                    position = self.position(cursor.lastrowid)
                    self.store.event("queued", job_id, doc_id, name=name, position=position)
                registered = True
                return {"status": "queued", "name": name, "doc_id": doc_id, "job_id": job_id, "position": position, "path": str(target)}
            finally:
                marker.unlink(missing_ok=True)
                if not registered:
                    target.unlink(missing_ok=True)
                    target.parent.rmdir()

    def _remove_abandoned_staging(self):
        # Only files in our UUID directories; never recurse into user-added folders.
        for folder in self.root().iterdir():
            if not folder.is_dir() or folder.is_symlink() or len(folder.name) != 32 or any(c not in "0123456789abcdef" for c in folder.name):
                continue
            if self.db.execute("SELECT 1 FROM documents WHERE id=?", (folder.name,)).fetchone():
                continue
            marker = folder / ".wiki-ingest.json"
            if not marker.is_file() or marker.is_symlink():
                continue
            try:
                metadata = json.loads(marker.read_text(encoding="utf-8"))
                if metadata.get("id") != folder.name or not isinstance(metadata.get("name"), str):
                    continue
                path = folder / metadata["name"]
                self.owned_path({"id": folder.name, "name": metadata["name"], "path": str(path)})
                path.unlink(missing_ok=True)
                marker.unlink()
            except (ValueError, OSError, BotError):
                continue
            if not any(folder.iterdir()):
                folder.rmdir()

    def position(self, seq):
        return self.db.execute("SELECT count(*) FROM jobs WHERE seq<=? AND state IN ('queued','indexing','indexed','awaiting_assistant','compiling')", (seq,)).fetchone()[0]

    def documents(self):
        rows = self.db.execute("SELECT d.*, (SELECT count(*) FROM chunks c WHERE c.doc_id=d.id) AS chunks FROM documents d WHERE retired=0 ORDER BY created,id").fetchall()
        return [dict(r) | {"markdown_directory": str(self.artifact("knowledge", "sources", r["id"]))} for r in rows]

    def status(self):
        jobs = [dict(r) for r in self.db.execute("SELECT j.*,d.name FROM jobs j JOIN documents d ON d.id=j.doc_id WHERE j.state IN ('queued','indexing','indexed','awaiting_assistant','compiling') OR (j.state='failed' AND NOT EXISTS (SELECT 1 FROM jobs newer WHERE newer.doc_id=j.doc_id AND newer.seq>j.seq)) ORDER BY j.seq")]
        counts = dict(self.db.execute("SELECT state,count(*) FROM jobs GROUP BY state").fetchall())
        cleanup = [dict(r) for r in self.db.execute("SELECT id,name,path,error FROM documents WHERE delete_requested=1")]
        return {"workspace": self.store.setting("workspace"), "documents_directory": str(self.root()) if self.store.setting("workspace") else None, "home": str(self.store.home), "wiki_enabled": self.store.setting("wiki_enabled", "1") == "1", "backend": "qmd-bm25", "vectors_enabled": False, "document_count": self.db.execute("SELECT count(*) FROM documents WHERE retired=0").fetchone()[0], "jobs": jobs, "counts": counts, "pending_cleanup": cleanup}

    def wiki_toggle(self, enabled):
        with self.store.transaction():
            self.store.set_setting("wiki_enabled", int(enabled))
        return {"wiki_enabled": enabled, "existing_jobs_unchanged": True}

    def _erase_index(self, doc_id):
        removed = self.db.execute("DELETE FROM wiki_claims WHERE chunk_id IN (SELECT id FROM chunks WHERE doc_id=?)", (doc_id,)).rowcount
        self.db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        if removed:
            self.store.set_setting("projection_dirty", "1")

    def delete(self, selector):
        with self.store.transaction():
            doc = self.store.resolve_doc(selector)
            self.owned_path(doc)
            self.db.execute("UPDATE documents SET delete_requested=1,status='deleting' WHERE id=?", (doc["id"],))
            self.db.execute("UPDATE jobs SET state='cancelled',updated=? WHERE doc_id=? AND state IN ('queued','awaiting_assistant','compiling','indexed','failed')", (time.time(), doc["id"]))
            self.store.event("delete_requested", doc=doc["id"], name=doc["name"])
        try:
            with FileLock(self.store.home / "worker.lock"):
                self._cleanup_deletions()
        except BotError as exc:
            if exc.code != "worker_busy":
                raise
        current = self.db.execute("SELECT status FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
        return {"doc_id": doc["id"], "status": current}

    def _cleanup_deletions(self):
        for doc in self.db.execute("SELECT * FROM documents WHERE delete_requested=1").fetchall():
            try:
                path = self.owned_path(doc)
                path.unlink(missing_ok=True)
                if path.parent.exists() and not any(path.parent.iterdir()):
                    path.parent.rmdir()
                self._remove_source_files(doc["id"])
                with self.store.transaction():
                    self._erase_index(doc["id"])
                    self.store.set_setting("projection_dirty", "1")
                    self._sync_projection()
                    self.db.execute("DELETE FROM wiki_batches WHERE job_id IN (SELECT id FROM jobs WHERE doc_id=?)", (doc["id"],))
                    self.db.execute("UPDATE jobs SET state='cancelled',updated=? WHERE doc_id=? AND state IN ('queued','indexing','indexed','awaiting_assistant','compiling','failed')", (time.time(), doc["id"]))
                    self.db.execute("UPDATE documents SET retired=1,status='deleted',delete_requested=0,error=NULL WHERE id=?", (doc["id"],))
                    self.store.event("deleted", doc=doc["id"], name=doc["name"])
            except (OSError, BotError) as exc:
                with self.store.transaction():
                    old = self.db.execute("SELECT error FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
                    self.db.execute("UPDATE documents SET error=? WHERE id=?", (str(exc), doc["id"]))
                    if old != str(exc):
                        self.store.event("cleanup_failed", doc=doc["id"], name=doc["name"], error=str(exc))

    def retry(self, job_id):
        with self.store.transaction():
            job = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["state"] != "failed":
                raise BotError("not_retryable", "只能重試失敗工作；中斷工作由 worker 自動恢復。")
            doc = self.store.resolve_doc(job["doc_id"])
            if doc["delete_requested"]:
                raise BotError("deletion_pending", "文件正在刪除。")
            active = self.db.execute("SELECT id FROM jobs WHERE doc_id=? AND state IN ('queued','indexing','indexed','awaiting_assistant','compiling')", (doc["id"],)).fetchone()
            if active:
                return {"job_id": active[0], "status": "already_queued"}
            new_id, now = uid(), time.time()
            has_index = self.db.execute("SELECT 1 FROM chunks WHERE doc_id=?", (doc["id"],)).fetchone()
            kind = "wiki" if has_index and job["wiki_enabled"] else job["kind"]
            self.db.execute("INSERT INTO jobs(id,doc_id,kind,wiki_enabled,created,updated,replace_id,attempt,wiki_cursor) VALUES(?,?,?,?,?,?,?,?,?)", (new_id, doc["id"], kind, job["wiki_enabled"], now, now, job["replace_id"] if not has_index else None, job["attempt"]+1, job["wiki_cursor"] if has_index else 0))
            self.db.execute("UPDATE documents SET error=NULL,status=? WHERE id=?", ("indexed" if has_index else "queued", doc["id"]))
            self.store.event("queued", new_id, doc["id"], name=doc["name"], retry_of=job_id)
        return {"job_id": new_id, "status": "queued", "retry_of": job_id}

    def wiki_build(self, selector):
        with self.store.transaction():
            docs = self.db.execute("SELECT * FROM documents WHERE retired=0 AND delete_requested=0 AND status IN ('indexed','ready') ORDER BY created,id").fetchall() if selector == "all" else [self.store.resolve_doc(selector)]
            results = []
            for doc in docs:
                if doc["delete_requested"] or doc["status"] not in {"indexed", "ready"}:
                    raise BotError("index_required", "Wiki補建需要已完成的 Markdown 索引。")
                existing = self.db.execute("SELECT id FROM jobs WHERE doc_id=? AND state IN ('queued','indexing','indexed','awaiting_assistant','compiling')", (doc["id"],)).fetchone()
                if existing:
                    results.append({"doc_id": doc["id"], "job_id": existing[0], "status": "already_queued"})
                    continue
                job_id, now = uid(), time.time()
                self.db.execute("INSERT INTO jobs(id,doc_id,kind,wiki_enabled,created,updated) VALUES(?,?,?,?,?,?)", (job_id, doc["id"], "wiki", 1, now, now))
                self.db.execute("UPDATE documents SET wiki_status='pending' WHERE id=?", (doc["id"],))
                self.store.event("queued", job_id, doc["id"], name=doc["name"], work_kind="wiki")
                results.append({"doc_id": doc["id"], "job_id": job_id, "status": "queued"})
        return {"jobs": results}

    def work(self, max_jobs=0):
        processed = 0
        with FileLock(self.store.home / "worker.lock"):
            self._cleanup_deletions()
            self._sync_projection()
            # The OS lock proves an indexing owner is gone. Uncommitted chunks were rolled back.
            with self.store.transaction():
                self.db.execute("UPDATE jobs SET state='queued',updated=? WHERE state='indexing'", (time.time(),))
            while not max_jobs or processed < max_jobs:
                self._cleanup_deletions()
                job = self.db.execute("SELECT * FROM jobs WHERE state IN ('queued','indexing','indexed','awaiting_assistant','compiling') ORDER BY seq LIMIT 1").fetchone()
                if not job:
                    return {"status": "idle", "processed": processed, "counts": self.status()["counts"]}
                doc = self.db.execute("SELECT * FROM documents WHERE id=?", (job["doc_id"],)).fetchone()
                if doc["delete_requested"] or doc["retired"]:
                    with self.store.transaction():
                        self.db.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job["id"],))
                    continue
                if job["state"] in {"indexed", "awaiting_assistant", "compiling"}:
                    return {"status": "awaiting_assistant", "job_id": job["id"], "processed": processed}
                try:
                    if job["kind"] == "ingest":
                        self._index(job, doc)
                    else:
                        with self.store.transaction():
                            self.db.execute("UPDATE jobs SET state='awaiting_assistant',updated=? WHERE id=?", (time.time(), job["id"]))
                            self.store.event("wiki_waiting", job["id"], doc["id"], name=doc["name"])
                    processed += 1
                    self._cleanup_deletions()
                    self._sync_projection()
                except Exception as exc:
                    with self.store.transaction():
                        current = self.db.execute("SELECT delete_requested FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
                        if current:
                            self.db.execute("UPDATE jobs SET state='cancelled',updated=? WHERE id=?", (time.time(), job["id"]))
                        else:
                            self._fail_job(job, doc, str(exc))
                    processed += 1
            return {"status": "yielded", "processed": processed}

    def _fail_job(self, job, doc, error):
        indexed = bool(self.db.execute("SELECT 1 FROM chunks WHERE doc_id=?", (doc["id"],)).fetchone())
        self.db.execute("UPDATE jobs SET state='failed',error=?,updated=? WHERE id=?", (error, time.time(), job["id"]))
        self.db.execute("UPDATE documents SET status=?,wiki_status=?,error=? WHERE id=?", ("indexed" if indexed else "failed", "failed" if job["wiki_enabled"] else "disabled", error, doc["id"]))
        self.store.event("failed", job["id"], doc["id"], name=doc["name"], error=error, index_available=indexed)

    def _check_cancelled(self, doc_id):
        if self.db.execute("SELECT delete_requested OR retired FROM documents WHERE id=?", (doc_id,)).fetchone()[0]:
            raise BotError("cancelled", "文件已要求刪除。")

    def _index(self, job, doc):
        with self.store.transaction():
            self.db.execute("UPDATE jobs SET state='indexing',updated=? WHERE id=?", (time.time(), job["id"]))
            self.db.execute("UPDATE documents SET status='indexing' WHERE id=?", (doc["id"],))
            self.store.event("indexing", job["id"], doc["id"], name=doc["name"])
        started = time.monotonic()
        path = self.owned_path(doc)
        if digest(path) != doc["sha256"]:
            raise BotError("source_changed", "管理副本已變更，請重新上傳。")
        passages, warnings = parse(path)
        chunks = split_passages(passages)
        self._check_cancelled(doc["id"])
        # Deterministic Markdown is a recoverable staging artifact until metadata publishes.
        self._write_source_files(doc, chunks)
        self.qmd.update()
        with self.store.transaction():
            self._check_cancelled(doc["id"])
            self._erase_index(doc["id"])
            for ordinal, passage in enumerate(chunks):
                self.db.execute("INSERT INTO chunks(doc_id,ordinal,text,locator) VALUES(?,?,?,?)", (doc["id"], ordinal, passage.text, json.dumps(passage.locator, ensure_ascii=False)))
            if job["replace_id"]:
                old = self.db.execute("SELECT * FROM documents WHERE id=? AND retired=0", (job["replace_id"],)).fetchone()
                if not old or old["delete_requested"]:
                    raise BotError("replacement_target_changed", "取代目標已變更，新文件未切換。")
                self.db.execute("UPDATE documents SET delete_requested=1,status='deleting' WHERE id=?", (old["id"],))
                self._erase_index(old["id"])
            state = "awaiting_assistant" if job["wiki_enabled"] else "done"
            self.db.execute("UPDATE jobs SET state=?,updated=? WHERE id=?", (state, time.time(), job["id"]))
            self.db.execute("UPDATE documents SET status=?,error=NULL WHERE id=?", ("indexed" if job["wiki_enabled"] else "ready", doc["id"]))
            waiting = self.db.execute("SELECT count(*) FROM jobs WHERE seq>? AND state='queued'", (job["seq"],)).fetchone()[0]
            self.store.event("indexed", job["id"], doc["id"], name=doc["name"], chunks=len(chunks), wiki_pending=bool(job["wiki_enabled"]), waiting=waiting, seconds=round(time.monotonic()-started,3), warnings=warnings)
            if not job["wiki_enabled"]:
                self.store.event("completed", job["id"], doc["id"], name=doc["name"])

    def events(self, consumer, owner, limit=100, from_now=False):
        if not consumer or not owner:
            raise BotError("event_consumer_required", "需要穩定的對話 ID 與此次處理者 ID。")
        now = time.time()
        with self.store.transaction():
            initial_cursor = self.db.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0] if from_now else 0
            self.db.execute("INSERT OR IGNORE INTO consumers(id,cursor,delivered_through) VALUES(?,?,?)", (consumer, initial_cursor, initial_cursor))
            row = self.db.execute("SELECT * FROM consumers WHERE id=?", (consumer,)).fetchone()
            if row["owner"] and row["owner"] != owner and row["lease_until"] > now:
                raise BotError("notifications_busy", "同一對話的通知正由另一處理者傳送。")
            rows = self.db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (row["cursor"], limit)).fetchall()
            through = rows[-1]["id"] if rows else row["cursor"]
            self.db.execute("UPDATE consumers SET owner=?,lease_until=?,delivered_through=? WHERE id=?", (owner, now+300, through, consumer))
        return {"events": [dict(r) | {"payload": json.loads(r["payload"])} for r in rows], "ack_through": through}

    def ack(self, consumer, owner, through):
        with self.store.transaction():
            row = self.db.execute("SELECT * FROM consumers WHERE id=?", (consumer,)).fetchone()
            if not row or row["owner"] != owner or not row["cursor"] <= through <= row["delivered_through"]:
                raise BotError("invalid_ack", "不能確認尚未讀取或屬於其他處理者的通知。")
            self.db.execute("UPDATE consumers SET cursor=?,owner=NULL,lease_until=0 WHERE id=?", (through, consumer))
        return {"acknowledged_through": through}
