"""Exercise real background workers, polling, FIFO and graph gating with generated attachments."""
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "document-bot" / "scripts"))
from docbot_core.engine import Bot
from document_bot import background


def main():
    base = ROOT / ".test-runtime" / "live"
    bot = Bot(base / "home")
    bot.graph_toggle(True)
    run = uuid.uuid4().hex[:8]
    source = base / "generated-queue" / run
    source.mkdir(parents=True)
    consumer = "live-queue:"+run
    owner = "codex-queue-check:"+run
    initial = bot.events(consumer, owner)
    bot.ack(consumer, owner, initial["ack_through"])
    jobs = []

    def enqueue(letter):
        name = f"queue-{run}-{letter}.txt"
        path = source / name
        path.write_text(f"This is queue test fixture {letter} for run {run}. It contains no domain relationships.", encoding="utf-8")
        receipt = bot.add({"source": "chat_attachment", "host": "codex", "order_known": True, "attachments": [{"attachment_id": "test-fixture:"+name, "name": name, "path": str(path)}]})["files"][0]
        assert receipt["status"] == "queued", receipt
        jobs.append(receipt)
        return receipt

    for letter in "ABC":
        enqueue(letter)
    processes = [background(bot.store.home, 0), background(bot.store.home, 0)]
    records, inserted_d, started = [], False, time.monotonic()
    try:
        while time.monotonic()-started < 180:
            status = bot.status()
            current = [j for j in status["jobs"] if j["id"] in {r["job_id"] for r in jobs}]
            if not inserted_d and any(j["state"] in {"indexing", "awaiting_assistant", "graphing"} for j in current):
                receipt = enqueue("D")
                assert receipt["position"] == 4
                inserted_d = True
            events = bot.events(consumer, owner)
            for event in events["events"]:
                if event["job_id"] in {r["job_id"] for r in jobs}:
                    records.append(event)
                    if event["kind"] in {"indexed", "completed", "failed"}:
                        print(json.dumps({"notice": event["kind"], "name": event["payload"].get("name"), "elapsed": round(time.monotonic()-started, 2)}, ensure_ascii=False), flush=True)
            bot.ack(consumer, owner, events["ack_through"])
            if any(j["state"] == "awaiting_assistant" for j in current):
                batch = bot.graph_next(owner)
                if batch["status"] == "batch":
                    # Deliberately non-domain fixture: an empty graph is correct here.
                    bot.graph_commit({"batch_id": batch["batch_id"], "entities": [], "relations": []}, owner)
                    processes.append(background(bot.store.home, 0))
            states = [bot.db.execute("SELECT state FROM jobs WHERE id=?", (r["job_id"],)).fetchone()[0] for r in jobs]
            if inserted_d and all(s == "done" for s in states):
                final_events = bot.events(consumer, owner)
                records.extend(e for e in final_events["events"] if e["job_id"] in {r["job_id"] for r in jobs})
                bot.ack(consumer, owner, final_events["ack_through"])
                break
            time.sleep(.2)
        else:
            raise RuntimeError("Background queue check timed out")
        index_order = [e["job_id"] for e in records if e["kind"] == "indexed"]
        assert index_order == [r["job_id"] for r in jobs], index_order
        for receipt in jobs:
            events = [e for e in records if e["job_id"] == receipt["job_id"]]
            assert next(e["id"] for e in events if e["kind"] == "indexed") < next(e["id"] for e in events if e["kind"] == "completed")
        assert bot.events(consumer, owner)["events"] == []
        report = {"source_adapter": "generated fixture manifest, not host UI attachments", "passed": True, "two_background_workers_started": True, "D_added_before_A_completed": inserted_d, "seconds": time.monotonic()-started, "receipts": jobs, "events": records}
        (ROOT / "verification" / "live-queue.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": True, "ordered_files": len(jobs), "seconds": report["seconds"]}), flush=True)
    finally:
        # Only stop child processes launched by this test, with matching command/home.
        import psutil
        for item in processes:
            try:
                process = psutil.Process(item["pid"])
                command = process.cmdline()
                if str(bot.store.home) in command and "worker" in command and any("document_bot.py" in c for c in command):
                    process.wait(timeout=2)
            except psutil.TimeoutExpired:
                process.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        bot.store.close()


if __name__ == "__main__":
    main()
