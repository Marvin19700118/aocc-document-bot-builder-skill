import json
from pathlib import Path
import time

import pytest

from conftest import TestEmbedder, add_one, attachment, drain_graph, manifest, payload_for
from docbot_core.engine import Bot
from docbot_core.storage import BotError, FileLock


def test_setup_default_and_idempotence(bot, tmp_path):
    assert bot.status()["graph_enabled"] is True
    assert bot.root().is_dir()
    bot.graph_toggle(False)
    bot.setup(bot.store.setting("workspace"), download=False)
    assert bot.status()["graph_enabled"] is False
    with pytest.raises(BotError, match="已有工作目錄"):
        bot.setup(tmp_path, download=False)


def test_attachment_only_and_no_side_effect_before_add(bot, tmp_path):
    file = attachment(tmp_path)
    assert bot.documents() == []
    with pytest.raises(BotError) as error:
        bot.add({"source": "path", "attachments": [file]})
    assert error.value.code == "unsupported_attachment"
    file["path"] = str(tmp_path / "missing.pdf")
    assert bot.add(manifest(file))["files"][0]["code"] == "unsupported_attachment"
    assert bot.documents() == []


def test_saved_original_and_duplicate_pending(bot, tmp_path):
    file = attachment(tmp_path, "中文 含空白.txt")
    receipt = bot.add(manifest(file))["files"][0]
    path = Path(receipt["path"])
    assert path == bot.root() / receipt["doc_id"] / file["name"]
    assert path.read_bytes() == Path(file["path"]).read_bytes()
    again = bot.add(manifest(file))["files"][0]
    assert again["status"] == "duplicate"
    assert again["job_id"] == receipt["job_id"]
    assert again["position"] == 1
    assert len(bot.documents()) == 1


def test_fifo_and_new_file_queued_while_indexing(bot, tmp_path):
    bot.graph_toggle(False)
    files = [attachment(tmp_path, f"{x}.txt", f"document {x}") for x in "ABC"]
    receipts = bot.add(manifest(*files))["files"]
    assert [r["position"] for r in receipts] == [1, 2, 3]
    extra = []

    def enqueue_d():
        other = Bot(bot.store.home, embedder=TestEmbedder())
        try:
            extra.append(add_one(other, tmp_path, "D.txt", "document D"))
            with pytest.raises(BotError) as error:
                other.work()
            assert error.value.code == "worker_busy"
        finally:
            other.store.close()

    bot._embedder = TestEmbedder(enqueue_d)
    assert bot.work()["status"] == "idle"
    assert extra[0]["position"] == 4
    names = [json.loads(r[0])["name"] for r in bot.db.execute("SELECT payload FROM events WHERE kind='indexed' ORDER BY id")]
    assert names == ["A.txt", "B.txt", "C.txt", "D.txt"]


def test_unknown_order_sorted_by_filename(bot, tmp_path):
    files = [attachment(tmp_path, "B.txt", "b"), attachment(tmp_path, "A.txt", "a")]
    assert [r["name"] for r in bot.add(manifest(*files, ordered=False))["files"]] == ["A.txt", "B.txt"]


def test_index_notice_before_graph_and_next_document(bot, tmp_path):
    first = add_one(bot, tmp_path, "A.txt", "A text")
    add_one(bot, tmp_path, "B.txt", "B text")
    result = bot.work()
    assert result["status"] == "awaiting_assistant"
    events = bot.events("conversation", "owner")["events"]
    indexed = [e for e in events if e["kind"] == "indexed"]
    assert len(indexed) == 1 and indexed[0]["doc_id"] == first["doc_id"]
    assert indexed[0]["payload"]["graph_pending"] is True
    assert indexed[0]["payload"]["waiting"] == 1
    assert not any(e["kind"] == "completed" for e in events)
    assert bot.ask("A")["status"] == "evidence"
    drain_graph(bot)
    bot.work()
    assert bot.db.execute("SELECT count(*) FROM events WHERE kind='indexed'").fetchone()[0] == 2


def test_graph_snapshot_for_queued_jobs(bot, tmp_path):
    first = add_one(bot, tmp_path, "A.txt", "A")
    bot.graph_toggle(False)
    second = add_one(bot, tmp_path, "B.txt", "B")
    assert bot.work()["job_id"] == first["job_id"]
    drain_graph(bot)
    assert bot.work()["status"] == "idle"
    assert bot.store.resolve_doc(second["doc_id"])["graph_status"] == "disabled"


def test_failed_file_does_not_block_queue_and_retry_moves_tail(bot, tmp_path):
    bot.graph_toggle(False)
    fail = add_one(bot, tmp_path, "bad.txt", "INJECT_EMBED_FAILURE")
    good = add_one(bot, tmp_path, "good.txt", "works")
    bot.work()
    assert bot.store.resolve_doc(fail["doc_id"])["status"] == "failed"
    assert bot.store.resolve_doc(good["doc_id"])["status"] == "ready"
    tail = add_one(bot, tmp_path, "tail.txt", "tail")
    retried = bot.retry(fail["job_id"])
    queue = bot.status()["jobs"]
    assert [j["id"] for j in queue if j["state"] == "queued"] == [tail["job_id"], retried["job_id"]]
    assert bot.retry(fail["job_id"])["status"] == "already_queued"


def test_name_conflict_keep_and_replace_atomic(bot, tmp_path):
    bot.graph_toggle(False)
    old = add_one(bot, tmp_path, "same.txt", "old text")
    bot.work()
    newfile = attachment(tmp_path, "same.txt", "new text")
    conflict = bot.add(manifest(newfile))["files"][0]
    assert conflict["code"] == "name_conflict"
    assert len(bot.documents()) == 1
    replacement = bot.add(manifest(newfile), {newfile["attachment_id"]: {"action": "replace", "document_id": old["doc_id"]}})["files"][0]
    assert Path(old["path"]).exists()
    bot.work()
    assert not Path(old["path"]).exists()
    assert bot.documents()[0]["id"] == replacement["doc_id"]
    third = attachment(tmp_path, "same.txt", "third text")
    receipt = bot.add(manifest(third), {third["attachment_id"]: {"action": "keep"}})["files"][0]
    assert receipt["status"] == "queued"
    with pytest.raises(BotError) as error:
        bot.delete("same.txt")
    assert error.value.code == "ambiguous_document"


def test_failed_replacement_keeps_original_queryable(bot, tmp_path):
    bot.graph_toggle(False)
    old = add_one(bot, tmp_path, "same.txt", "original usable evidence")
    bot.work()
    bad = attachment(tmp_path, "same.txt", "INJECT_EMBED_FAILURE")
    bot.add(manifest(bad), {bad["attachment_id"]: {"action": "replace", "document_id": old["doc_id"]}})
    bot.work()
    assert Path(old["path"]).exists()
    assert bot.ask("original", old["doc_id"])["evidence"][0]["doc_id"] == old["doc_id"]


def test_delete_queued_preserves_source(bot, tmp_path):
    file = attachment(tmp_path)
    receipt = bot.add(manifest(file))["files"][0]
    assert bot.delete(receipt["doc_id"])["status"] == "deleted"
    assert Path(file["path"]).exists()
    assert not Path(receipt["path"]).exists()
    assert bot.work()["status"] == "idle"


def test_delete_during_embedding_cannot_reappear(bot, tmp_path):
    receipt = add_one(bot, tmp_path)

    def delete_now():
        other = Bot(bot.store.home, embedder=TestEmbedder())
        try:
            assert other.delete(receipt["doc_id"])["status"] == "deleting"
        finally:
            other.store.close()

    bot._embedder = TestEmbedder(delete_now)
    bot.work()
    assert bot.documents() == []
    assert bot.db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    assert bot.db.execute("SELECT count(*) FROM chunk_vectors").fetchone()[0] == 0
    assert bot.db.execute("SELECT count(*) FROM events WHERE kind='indexed'").fetchone()[0] == 0


def test_restart_recovers_indexing_and_waiting_graph(bot, tmp_path):
    receipt = add_one(bot, tmp_path)
    bot.db.execute("UPDATE jobs SET state='indexing' WHERE id=?", (receipt["job_id"],))
    other = Bot(bot.store.home, embedder=TestEmbedder())
    try:
        assert other.work()["status"] == "awaiting_assistant"
        assert other.graph_next("resumed")["status"] == "batch"
        assert other.work()["status"] == "awaiting_assistant"
    finally:
        other.store.close()


def test_notifications_ack_replay_and_conversation_isolation(bot, tmp_path):
    bot.graph_toggle(False)
    add_one(bot, tmp_path)
    bot.work()
    events = bot.events("codex:one", "owner")
    assert any(e["kind"] == "indexed" for e in events["events"])
    assert bot.events("codex:one", "owner")["events"] == events["events"]
    with pytest.raises(BotError):
        bot.events("codex:one", "other-owner")
    with pytest.raises(BotError):
        bot.ack("codex:one", "owner", events["ack_through"] + 1)
    bot.ack("codex:one", "owner", events["ack_through"])
    assert bot.events("codex:one", "owner")["events"] == []
    assert bot.events("claude:two", "owner2")["events"]


def test_graph_evidence_validation_and_idempotent_commit(bot, tmp_path):
    add_one(bot, tmp_path, content="星河公司開發 Aurora。")
    bot.work()
    batch = bot.graph_next("owner")
    with pytest.raises(BotError) as error:
        bot.graph_next("another")
    assert error.value.code == "graph_busy"
    bad = payload_for(batch)
    bad["relations"][0]["evidence"] = "fabricated quote"
    with pytest.raises(BotError):
        bot.graph_commit(bad, "owner")
    assert bot.db.execute("SELECT count(*) FROM entities").fetchone()[0] == 0
    assert bot.graph_commit(payload_for(batch), "owner")["status"] == "completed"
    assert bot.graph_commit(payload_for(batch), "owner")["status"] == "already_committed"
    assert bot.db.execute("SELECT count(*) FROM relations").fetchone()[0] == 1


def test_graph_expired_lease_reclaim_and_delete_invalidates_batch(bot, tmp_path):
    receipt = add_one(bot, tmp_path, content="星河公司開發 Aurora。")
    bot.work()
    batch = bot.graph_next("owner")
    bot.db.execute("UPDATE graph_batches SET lease_until=0")
    with pytest.raises(BotError):
        bot.graph_commit(payload_for(batch), "owner")
    reacquired = bot.graph_next("new-owner")
    assert reacquired["batch_id"] == batch["batch_id"]
    bot.delete(receipt["doc_id"])
    with pytest.raises(BotError):
        bot.graph_commit(payload_for(batch), "new-owner")


def test_shared_relation_survives_one_source_delete(bot, tmp_path):
    first = add_one(bot, tmp_path, "a.txt", "星河公司開發 Aurora。 A版")
    second = add_one(bot, tmp_path, "b.txt", "星河公司開發 Aurora。 B版")
    for _ in range(2):
        bot.work()
        batch = bot.graph_next("owner")
        bot.graph_commit(payload_for(batch), "owner")
    assert bot.db.execute("SELECT count(*) FROM relation_sources").fetchone()[0] == 2
    bot.delete(first["doc_id"])
    assert bot.db.execute("SELECT count(*) FROM relations").fetchone()[0] == 1
    assert bot.db.execute("SELECT count(*) FROM relation_sources").fetchone()[0] == 1
    bot.delete(second["doc_id"])
    assert bot.db.execute("SELECT count(*) FROM relations").fetchone()[0] == 0
    assert bot.db.execute("SELECT count(*) FROM entities").fetchone()[0] == 0


def test_graph_failure_retains_vectors_and_next_file_runs(bot, tmp_path):
    first = add_one(bot, tmp_path, "a.txt", "first text")
    second = add_one(bot, tmp_path, "b.txt", "second text")
    bot.work()
    bot.graph_next("owner")
    bot.graph_fail(first["job_id"], "owner", "extraction failed")
    assert bot.ask("first", first["doc_id"])["evidence"]
    assert bot.work()["job_id"] == second["job_id"]
    drain_graph(bot)
    retry = bot.retry(first["job_id"])
    assert bot.work()["job_id"] == retry["job_id"]
    drain_graph(bot)
    assert bot.store.resolve_doc(first["doc_id"])["graph_status"] == "ready"


def test_graph_build_existing_documents(bot, tmp_path):
    bot.graph_toggle(False)
    add_one(bot, tmp_path)
    bot.work()
    jobs = bot.graph_build("all")["jobs"]
    assert len(jobs) == 1
    assert bot.graph_build("all")["jobs"][0]["status"] == "already_queued"
    bot.work()
    drain_graph(bot)
    assert bot.documents()[0]["graph_status"] == "ready"


def test_empty_library_abstains_without_loading_model(bot):
    bot._embedder = None
    assert bot.ask("anything")["status"] == "no_evidence"


def test_attachment_filename_traversal_rejected(bot, tmp_path):
    file = attachment(tmp_path)
    file["name"] = "../escape.txt"
    assert bot.add(manifest(file))["files"][0]["code"] == "invalid_filename"


def test_doc_filtered_retrieval_does_not_leak_other_document(bot, tmp_path):
    bot.graph_toggle(False)
    first = add_one(bot, tmp_path, "A.txt", "alpha unique")
    second = add_one(bot, tmp_path, "B.txt", "beta unique")
    bot.work()
    evidence = bot.ask("alpha", second["doc_id"])["evidence"]
    assert evidence and all(e["doc_id"] == second["doc_id"] for e in evidence)


def test_disk_cleanup_failure_remains_pending_and_retryable(bot, tmp_path, monkeypatch):
    receipt = add_one(bot, tmp_path)
    real_unlink = Path.unlink

    def locked(path, *args, **kwargs):
        if str(path) == receipt["path"]:
            raise PermissionError("simulated file in use")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)
    assert bot.delete(receipt["doc_id"])["status"] == "deleting"
    assert bot.status()["document_count"] == 1
    monkeypatch.setattr(Path, "unlink", real_unlink)
    bot.work()
    assert bot.documents() == []


def test_staging_cleanup_never_removes_unmarked_user_directory(bot, tmp_path):
    folder = bot.root() / ("a"*32)
    folder.mkdir()
    keep = folder / "keep.txt"
    keep.write_text("user-created file")
    add_one(bot, tmp_path)
    assert keep.read_text() == "user-created file"


def test_modified_database_path_cannot_delete_external_source(bot, tmp_path):
    receipt = add_one(bot, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive")
    bot.db.execute("UPDATE documents SET path=? WHERE id=?", (str(outside), receipt["doc_id"]))
    with pytest.raises(BotError) as error:
        bot.delete(receipt["doc_id"])
    assert error.value.code == "unsafe_document_path"
    assert outside.read_text() == "must survive"


def test_graph_partial_progress_survives_failure_and_retry(bot, tmp_path):
    receipt = add_one(bot, tmp_path, content="\n\n".join(f"段落 {i} 的測試內容。" for i in range(8)))
    bot.work()
    batch = bot.graph_next("owner")
    first_ids = {c["chunk_id"] for c in batch["chunks"]}
    result = bot.graph_commit({"batch_id": batch["batch_id"], "entities": [], "relations": []}, "owner")
    assert result["remaining_chunks"] == 5
    bot.graph_fail(receipt["job_id"], "owner", "interrupted extraction")
    retried = bot.retry(receipt["job_id"])
    bot.work()
    next_batch = bot.graph_next("owner2")
    assert not first_ids.intersection(c["chunk_id"] for c in next_batch["chunks"])
    assert next_batch["job_id"] == retried["job_id"]
    drain_graph(bot, "owner2")
    assert bot.documents()[0]["graph_status"] == "ready"


def test_new_conversation_baseline_does_not_skip_existing_unacked_events(bot, tmp_path):
    add_one(bot, tmp_path, "old.txt", "old content")
    initial = bot.events("new", "owner", from_now=True)
    assert initial["events"] == []
    bot.ack("new", "owner", initial["ack_through"])
    add_one(bot, tmp_path, "new.txt", "new content")
    resumed = bot.events("new", "owner", from_now=True)
    assert [e["payload"]["name"] for e in resumed["events"]] == ["new.txt"]


def test_sql_failure_mid_index_rolls_back_vectors_and_preserves_replacement(bot, tmp_path):
    bot.graph_toggle(False)
    old = add_one(bot, tmp_path, "same.txt", "original stable evidence")
    bot.work()
    file = attachment(tmp_path, "same.txt", "new first paragraph\n\nnew second paragraph")
    new = bot.add(manifest(file), {file["attachment_id"]: {"action": "replace", "document_id": old["doc_id"]}})["files"][0]
    bot.db.execute("CREATE TRIGGER fail_chunk BEFORE INSERT ON chunks WHEN new.ordinal=1 BEGIN SELECT RAISE(ABORT,'simulated commit failure'); END")
    bot.work()
    assert bot.store.resolve_doc(old["doc_id"])["status"] == "ready"
    assert Path(old["path"]).exists()
    assert bot.store.resolve_doc(new["doc_id"])["status"] == "failed"
    assert bot.db.execute("SELECT count(*) FROM chunks WHERE doc_id=?", (new["doc_id"],)).fetchone()[0] == 0
    assert bot.db.execute("SELECT count(*) FROM chunk_vectors").fetchone()[0] == 1
    assert bot.ask("original", old["doc_id"])["evidence"]
