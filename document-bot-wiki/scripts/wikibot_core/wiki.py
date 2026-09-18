from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

from .storage import BotError, FileLock


def checked_string(value, field, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise BotError("invalid_wiki", f"Wiki欄位 {field} 無效。")
    return value.strip()


class WikiMixin:
    def wiki_next(self, owner):
        if not owner:
            raise BotError("wiki_owner_required", "需要助手處理者 ID。")
        now = time.time()
        with FileLock(self.store.home / "worker.lock", wait_seconds=2), self.store.transaction():
            self._sync_projection()
            job = self.db.execute("SELECT * FROM jobs WHERE state IN ('queued','indexing','indexed','awaiting_assistant','compiling') ORDER BY seq LIMIT 1").fetchone()
            if not job or job["state"] not in {"awaiting_assistant", "compiling", "indexed"}:
                return {"status": "no_wiki_work"}
            doc = self.db.execute("SELECT * FROM documents WHERE id=?", (job["doc_id"],)).fetchone()
            if doc["delete_requested"] or doc["retired"]:
                return {"status": "cancelled"}
            pending = self.db.execute("SELECT * FROM wiki_batches WHERE job_id=? AND state='pending' ORDER BY created LIMIT 1", (job["id"],)).fetchone()
            if pending:
                if pending["owner"] != owner and pending["lease_until"] > now:
                    raise BotError("wiki_busy", "Wiki批次已由另一個助手領取，請稍後再試。")
                batch_id, chunk_ids = pending["id"], json.loads(pending["chunk_ids"])
                self.db.execute("UPDATE wiki_batches SET owner=?,lease_until=? WHERE id=?", (owner, now+900, batch_id))
                chunks = [self.db.execute("SELECT * FROM chunks WHERE id=?", (cid,)).fetchone() for cid in chunk_ids]
            else:
                chunks = self.db.execute("SELECT * FROM chunks WHERE doc_id=? AND ordinal>=? ORDER BY ordinal LIMIT 3", (doc["id"], job["wiki_cursor"])).fetchall()
                if not chunks:
                    self._finish_wiki(job, doc)
                    return {"status": "completed", "job_id": job["id"]}
                batch_id = uuid.uuid4().hex
                self.db.execute("INSERT INTO wiki_batches(id,job_id,chunk_ids,start_cursor,end_cursor,owner,lease_until,created) VALUES(?,?,?,?,?,?,?,?)", (batch_id, job["id"], json.dumps([c["id"] for c in chunks]), job["wiki_cursor"], chunks[-1]["ordinal"]+1, owner, now+900, now))
            self.db.execute("UPDATE jobs SET state='compiling',updated=? WHERE id=?", (now, job["id"]))
            self.db.execute("UPDATE documents SET wiki_status='processing' WHERE id=?", (doc["id"],))
            return {"status": "batch", "batch_id": batch_id, "job_id": job["id"], "owner": owner, "name": doc["name"], "doc_id": doc["id"], "lease_seconds": 900, "existing_topics": self.wiki_list()["pages"][:50], "chunks": [{"chunk_id": c["id"], "text": c["text"], "locator": json.loads(c["locator"])} for c in chunks]}

    def _finish_wiki(self, job, doc):
        self.db.execute("UPDATE jobs SET state='done',error=NULL,updated=? WHERE id=?", (time.time(), job["id"]))
        self.db.execute("UPDATE documents SET status='ready',wiki_status='ready',error=NULL WHERE id=?", (doc["id"],))
        self.store.event("completed", job["id"], doc["id"], name=doc["name"], wiki_completed=True)

    def wiki_commit(self, payload, owner):
        if not isinstance(payload, dict) or not isinstance(payload.get("batch_id"), str):
            raise BotError("invalid_wiki", "需要 batch_id 與 claims。")
        with FileLock(self.store.home / "worker.lock", wait_seconds=2):
            with self.store.transaction():
                batch = self.db.execute("SELECT * FROM wiki_batches WHERE id=?", (payload["batch_id"],)).fetchone()
                if not batch:
                    raise BotError("stale_wiki_batch", "批次不存在或文件已刪除。")
                job = self.db.execute("SELECT * FROM jobs WHERE id=?", (batch["job_id"],)).fetchone()
                doc = self.db.execute("SELECT * FROM documents WHERE id=?", (job["doc_id"],)).fetchone()
                if doc["delete_requested"] or doc["retired"] or job["state"] not in {"compiling", "awaiting_assistant", "done"}:
                    raise BotError("stale_wiki_batch", "工作已失效。")
                if batch["state"] == "done":
                    already = True
                else:
                    already = False
                    if batch["owner"] != owner or batch["lease_until"] < time.time():
                        raise BotError("wiki_lease_expired", "批次租約過期或屬於另一助手。")
                    if batch["start_cursor"] != job["wiki_cursor"]:
                        raise BotError("stale_wiki_batch", "批次進度已變更。")
                    claims = payload.get("claims")
                    if not isinstance(claims, list) or len(claims)>100:
                        raise BotError("invalid_wiki", "claims 必須為最多 100 筆的陣列。")
                    allowed = json.loads(batch["chunk_ids"])
                    for cid in allowed:
                        self.db.execute("DELETE FROM wiki_claims WHERE chunk_id=?", (cid,))
                    for item in claims:
                        if not isinstance(item, dict):
                            raise BotError("invalid_wiki", "claim 必須為物件。")
                        topic = checked_string(item.get("topic"), "topic", 80)
                        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", topic):
                            raise BotError("invalid_wiki", "topic 必須是安全的英數短橫線識別。")
                        title = checked_string(item.get("title"), "title", 120)
                        claim = checked_string(item.get("claim"), "claim", 2000)
                        quote = checked_string(item.get("quote"), "quote", 2400)
                        cid = item.get("chunk_id")
                        row = self.db.execute("SELECT text FROM chunks WHERE id=? AND doc_id=?", (cid, doc["id"])).fetchone() if type(cid) is int and cid in allowed else None
                        if not row or quote not in row[0]:
                            raise BotError("unsupported_wiki_evidence", "引文必須逐字存在於本批次來源段落。")
                        key = hashlib.sha256(json.dumps([topic, claim, cid, quote], ensure_ascii=False).encode()).hexdigest()
                        self.db.execute("INSERT OR IGNORE INTO wiki_claims VALUES(?,?,?,?,?,?,?)", (key, topic, title, claim, cid, quote, batch["id"]))
                    self.db.execute("UPDATE wiki_batches SET state='done' WHERE id=?", (batch["id"],))
                    self.db.execute("UPDATE jobs SET wiki_cursor=?,state='awaiting_assistant',updated=? WHERE id=?", (batch["end_cursor"], time.time(), job["id"]))
                    self.store.set_setting("projection_dirty", "1")
            # Persist claims before projecting files; a crash leaves a recoverable dirty flag.
            self._sync_projection()
            with self.store.transaction():
                self._check_cancelled(doc["id"])
                current = self.db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
                remaining = self.db.execute("SELECT count(*) FROM chunks WHERE doc_id=? AND ordinal>=?", (doc["id"], current["wiki_cursor"])).fetchone()[0]
                if not remaining and current["state"] != "done":
                    self._finish_wiki(current, doc)
            return {"status": "already_committed" if already else ("completed" if not remaining else "batch_committed"), "job_id": job["id"], "remaining_chunks": remaining}

    def wiki_fail(self, job_id, owner, reason):
        reason = checked_string(reason, "reason", 2000)
        with FileLock(self.store.home / "worker.lock", wait_seconds=2), self.store.transaction():
            job = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["state"] not in {"compiling", "awaiting_assistant", "indexed"}:
                raise BotError("not_wiki_work", "工作不在等待Wiki的狀態。")
            pending = self.db.execute("SELECT * FROM wiki_batches WHERE job_id=? AND state='pending'", (job_id,)).fetchone()
            if pending and pending["owner"] != owner and pending["lease_until"] > time.time():
                raise BotError("wiki_busy", "另一個助手持有此Wiki批次。")
            doc = self.db.execute("SELECT * FROM documents WHERE id=?", (job["doc_id"],)).fetchone()
            self._fail_job(job, doc, reason)
        return {"status": "failed", "job_id": job_id, "index_available": True}

    def wiki_list(self):
        return {"pages": [dict(r) for r in self.db.execute("SELECT topic,min(title) AS title,count(*) AS claims FROM wiki_claims GROUP BY topic ORDER BY topic")]}

    def wiki_lint(self):
        with FileLock(self.store.home / "worker.lock", wait_seconds=2):
            self._sync_projection()
            errors = []
            for row in self.db.execute("SELECT w.*,c.text,d.retired,d.delete_requested FROM wiki_claims w JOIN chunks c ON c.id=w.chunk_id JOIN documents d ON d.id=c.doc_id"):
                if row["quote"] not in row["text"] or row["retired"] or row["delete_requested"]:
                    errors.append({"claim": row["id"], "error": "invalid_source"})
            root = self.artifact("knowledge")
            if root.exists():
                for page in (root/"wiki").glob("*.md"):
                    for target in re.findall(r"\]\(([^)]+)\)", page.read_text(encoding="utf-8")):
                        if "://" not in target and not target.startswith("#"):
                            resolved = (page.parent / target.split("#")[0]).resolve()
                            if not resolved.is_relative_to(root) or not resolved.exists():
                                errors.append({"page": str(page.relative_to(root)), "error": "broken_link", "target": target})
            return {"status": "ok" if not errors else "issues", "errors": errors, **self.wiki_list()}
