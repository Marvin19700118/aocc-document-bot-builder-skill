from __future__ import annotations

import hashlib
import json
import time
import unicodedata
import uuid

from .storage import BotError, FileLock


def entity_key(name, kind):
    return hashlib.sha256((unicodedata.normalize("NFKC", kind).strip().casefold() + "\0" + unicodedata.normalize("NFKC", name).strip().casefold()).encode()).hexdigest()


def checked_string(value, field, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise BotError("invalid_graph", f"圖譜欄位 {field} 無效。")
    return value.strip()


class GraphMixin:
    def graph_next(self, owner):
        if not owner:
            raise BotError("graph_owner_required", "需要助手處理者 ID。")
        now = time.time()
        with self.store.transaction():
            job = self.db.execute("SELECT * FROM jobs WHERE state IN ('queued','indexing','indexed','awaiting_assistant','graphing') ORDER BY seq LIMIT 1").fetchone()
            if not job or job["state"] not in {"awaiting_assistant", "graphing", "indexed"}:
                return {"status": "no_graph_work"}
            doc = self.db.execute("SELECT * FROM documents WHERE id=?", (job["doc_id"],)).fetchone()
            if doc["delete_requested"] or doc["retired"]:
                return {"status": "cancelled"}
            pending = self.db.execute("SELECT * FROM graph_batches WHERE job_id=? AND state='pending' ORDER BY created LIMIT 1", (job["id"],)).fetchone()
            if pending:
                if pending["owner"] != owner and pending["lease_until"] > now:
                    raise BotError("graph_busy", "圖譜批次已由另一個助手領取，請稍後再試。")
                batch_id, chunk_ids = pending["id"], json.loads(pending["chunk_ids"])
                self.db.execute("UPDATE graph_batches SET owner=?,lease_until=? WHERE id=?", (owner, now+900, batch_id))
                chunks = [self.db.execute("SELECT * FROM chunks WHERE id=?", (cid,)).fetchone() for cid in chunk_ids]
            else:
                chunks = self.db.execute("SELECT * FROM chunks WHERE doc_id=? AND ordinal>=? ORDER BY ordinal LIMIT 3", (doc["id"], job["graph_cursor"])).fetchall()
                if not chunks:
                    self._finish_graph(job, doc)
                    return {"status": "completed", "job_id": job["id"]}
                batch_id = uuid.uuid4().hex
                self.db.execute("INSERT INTO graph_batches(id,job_id,chunk_ids,start_cursor,end_cursor,owner,lease_until,created) VALUES(?,?,?,?,?,?,?,?)", (batch_id, job["id"], json.dumps([c["id"] for c in chunks]), job["graph_cursor"], chunks[-1]["ordinal"]+1, owner, now+900, now))
            self.db.execute("UPDATE jobs SET state='graphing',updated=? WHERE id=?", (now, job["id"]))
            self.db.execute("UPDATE documents SET graph_status='processing' WHERE id=?", (doc["id"],))
            return {"status": "batch", "batch_id": batch_id, "job_id": job["id"], "owner": owner, "name": doc["name"], "doc_id": doc["id"], "lease_seconds": 900, "chunks": [{"chunk_id": c["id"], "text": c["text"], "locator": json.loads(c["locator"])} for c in chunks]}

    def _finish_graph(self, job, doc):
        self.db.execute("UPDATE jobs SET state='done',error=NULL,updated=? WHERE id=?", (time.time(), job["id"]))
        self.db.execute("UPDATE documents SET status='ready',graph_status='ready',error=NULL WHERE id=?", (doc["id"],))
        self.store.event("completed", job["id"], doc["id"], name=doc["name"], graph_completed=True)

    def graph_commit(self, payload, owner):
        if not isinstance(payload, dict) or not isinstance(payload.get("batch_id"), str):
            raise BotError("invalid_graph", "需要 batch_id 與圖譜資料。")
        with FileLock(self.store.home / "worker.lock", wait_seconds=2), self.store.transaction():
            batch = self.db.execute("SELECT * FROM graph_batches WHERE id=?", (payload["batch_id"],)).fetchone()
            if not batch:
                raise BotError("stale_graph_batch", "圖譜批次不存在或文件已刪除。")
            if batch["state"] == "done":
                return {"status": "already_committed", "batch_id": batch["id"]}
            if batch["owner"] != owner or batch["lease_until"] < time.time():
                raise BotError("graph_lease_expired", "圖譜批次租約已過期或屬於其他助手，請重新領取。")
            job = self.db.execute("SELECT * FROM jobs WHERE id=?", (batch["job_id"],)).fetchone()
            doc = self.db.execute("SELECT * FROM documents WHERE id=?", (job["doc_id"],)).fetchone()
            if job["state"] != "graphing" or doc["delete_requested"] or doc["retired"] or batch["start_cursor"] != job["graph_cursor"]:
                raise BotError("stale_graph_batch", "圖譜工作已取消或進度已變更。")
            texts = {cid: self.db.execute("SELECT text FROM chunks WHERE id=? AND doc_id=?", (cid, doc["id"])).fetchone()[0] for cid in json.loads(batch["chunk_ids"])}
            entities, relations = payload.get("entities"), payload.get("relations")
            if not isinstance(entities, list) or not isinstance(relations, list) or len(entities)>200 or len(relations)>300:
                raise BotError("invalid_graph", "entities/relations 必須是有限大小的陣列。")

            def evidence(item):
                if not isinstance(item, dict):
                    raise BotError("invalid_graph", "每筆圖譜資料必須是物件。")
                cid = item.get("chunk_id")
                quote = checked_string(item.get("evidence"), "evidence", 4000)
                if type(cid) is not int or cid not in texts or quote not in texts[cid]:
                    raise BotError("unsupported_graph_evidence", "圖譜證據必須逐字存在於本批次來源段落。")
                return cid, quote

            for item in entities:
                cid, quote = evidence(item)
                name, kind = checked_string(item.get("name"), "name"), checked_string(item.get("type"), "type", 80)
                if name.casefold() not in texts[cid].casefold():
                    raise BotError("unsupported_entity", "實體名稱必須出現在引用段落中。")
                key = entity_key(name, kind)
                self.db.execute("INSERT OR IGNORE INTO entities VALUES(?,?,?)", (key, name, kind))
                self.db.execute("INSERT OR IGNORE INTO mentions VALUES(?,?,?)", (key, cid, quote))
            for item in relations:
                cid, quote = evidence(item)
                endpoints = []
                for role in ("subject", "object"):
                    endpoint = item.get(role)
                    if not isinstance(endpoint, dict):
                        raise BotError("invalid_graph", "關係端點需要 name/type。")
                    key = entity_key(checked_string(endpoint.get("name"), "name"), checked_string(endpoint.get("type"), "type", 80))
                    if not self.db.execute("SELECT 1 FROM mentions WHERE entity_id=? AND chunk_id=?", (key, cid)).fetchone():
                        raise BotError("unsupported_relation", "關係兩端都需要同一來源段落的實體證據。")
                    endpoints.append(key)
                predicate = checked_string(item.get("predicate"), "predicate", 100)
                key = hashlib.sha256((endpoints[0]+"\0"+predicate+"\0"+endpoints[1]).encode()).hexdigest()
                self.db.execute("INSERT OR IGNORE INTO relations VALUES(?,?,?,?)", (key, endpoints[0], predicate, endpoints[1]))
                self.db.execute("INSERT OR IGNORE INTO relation_sources VALUES(?,?,?)", (key, cid, quote))
            self.db.execute("UPDATE graph_batches SET state='done' WHERE id=?", (batch["id"],))
            self.db.execute("UPDATE jobs SET graph_cursor=?,state='awaiting_assistant',updated=? WHERE id=?", (batch["end_cursor"], time.time(), job["id"]))
            remaining = self.db.execute("SELECT count(*) FROM chunks WHERE doc_id=? AND ordinal>=?", (doc["id"], batch["end_cursor"])).fetchone()[0]
            if not remaining:
                self._finish_graph(job, doc)
            return {"status": "completed" if not remaining else "batch_committed", "job_id": job["id"], "remaining_chunks": remaining}

    def graph_fail(self, job_id, owner, reason):
        reason = checked_string(reason, "reason", 2000)
        with FileLock(self.store.home / "worker.lock", wait_seconds=2), self.store.transaction():
            job = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["state"] not in {"graphing", "awaiting_assistant", "indexed"}:
                raise BotError("not_graph_work", "工作不在等待圖譜的狀態。")
            pending = self.db.execute("SELECT * FROM graph_batches WHERE job_id=? AND state='pending'", (job_id,)).fetchone()
            if pending and pending["owner"] != owner and pending["lease_until"] > time.time():
                raise BotError("graph_busy", "另一個助手持有此圖譜批次。")
            doc = self.db.execute("SELECT * FROM documents WHERE id=?", (job["doc_id"],)).fetchone()
            self._fail_job(job, doc, reason)
        return {"status": "failed", "job_id": job_id, "vector_available": True}
