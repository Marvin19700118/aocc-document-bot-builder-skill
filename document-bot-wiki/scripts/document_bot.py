#!/usr/bin/env python3
"""JSON CLI called by the skill. Attachment paths are supplied by the host, not users."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from wikibot_core.engine import Bot
from wikibot_core.storage import BotError, FileLock, home_path


def emit(value):
    print(json.dumps(value, ensure_ascii=False, default=str), flush=True)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def parser():
    root = argparse.ArgumentParser(description="Document Bot local library")
    root.add_argument("--home", help="Internal override for isolated tests or a configured installation")
    commands = root.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup")
    setup.add_argument("--workspace", required=True)
    add = commands.add_parser("add")
    add.add_argument("--manifest", required=True, help="Host-generated chat attachment manifest")
    add.add_argument("--choices", help="JSON decisions keyed by attachment_id")
    commands.add_parser("dir", aliases=["dir."])
    commands.add_parser("status")
    delete = commands.add_parser("delete")
    delete.add_argument("document")
    retry = commands.add_parser("retry")
    retry.add_argument("job")
    ask = commands.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--doc")
    worker = commands.add_parser("worker")
    worker.add_argument("--background", action="store_true")
    worker.add_argument("--max-jobs", type=int, default=0)
    worker.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    events = commands.add_parser("events")
    events.add_argument("--consumer", required=True)
    events.add_argument("--owner", required=True)
    events.add_argument("--from-now", action="store_true", help="Only a new consumer starts at the current event; existing cursors are preserved")
    ack = commands.add_parser("ack")
    ack.add_argument("--consumer", required=True)
    ack.add_argument("--owner", required=True)
    ack.add_argument("--through", type=int, required=True)
    wiki = commands.add_parser("wiki", aliases=["graph"])
    sub = wiki.add_subparsers(dest="action", required=True)
    sub.add_parser("on")
    sub.add_parser("off")
    sub.add_parser("list")
    sub.add_parser("lint")
    build = sub.add_parser("build")
    build.add_argument("document")
    next_batch = sub.add_parser("next")
    next_batch.add_argument("--owner", required=True)
    commit = sub.add_parser("commit")
    commit.add_argument("--owner", required=True)
    commit.add_argument("--payload", required=True)
    fail = sub.add_parser("fail")
    fail.add_argument("--owner", required=True)
    fail.add_argument("--job", required=True)
    fail.add_argument("--reason", required=True)
    commands.add_parser("new-session")
    return root


def background(home, max_jobs):
    home = home_path(home)
    log_path = home / "worker.log"
    args = [sys.executable, str(Path(__file__).resolve()), "--home", str(home), "worker", "--serve", "--max-jobs", str(max_jobs)]
    kwargs = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    with log_path.open("ab") as log:
        process = subprocess.Popen(args, stdout=log, stderr=log, **kwargs)
    return {"status": "worker_started", "pid": process.pid, "log": str(log_path)}


def serve(bot, max_jobs=0):
    """Drain the queue while allowing assistant compilation between files."""
    processed = 0
    with FileLock(bot.store.home / "supervisor.lock"):
        while True:
            try:
                result = bot.work(max_jobs=1)
            except BotError as exc:
                if exc.code != "worker_busy":
                    raise
                time.sleep(.2)
                continue
            processed += result.get("processed", 0)
            if result["status"] == "idle" or (max_jobs and processed >= max_jobs):
                return result | {"processed": processed}
            if result["status"] == "awaiting_assistant":
                deadline = time.monotonic()+120
                while time.monotonic() < deadline:
                    job = bot.db.execute("SELECT state FROM jobs WHERE id=?", (result["job_id"],)).fetchone()
                    doc = bot.db.execute("SELECT delete_requested FROM documents WHERE id=(SELECT doc_id FROM jobs WHERE id=?)", (result["job_id"],)).fetchone()
                    if not job or job["state"] not in {"awaiting_assistant", "compiling", "indexed"} or (doc and doc[0]):
                        break
                    time.sleep(.2)
                else:
                    return result | {"processed": processed, "worker_exited_after_wait_seconds": 120}


def main(argv=None):
    args = parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    bot = None
    try:
        if args.command == "new-session":
            emit({"ok": True, "data": {"session": uuid.uuid4().hex}})
            return 0
        bot = Bot(args.home, initialize=args.command == "setup")
        command = args.command
        if command == "setup":
            result = bot.setup(args.workspace)
        elif command == "add":
            result = bot.add(read_json(args.manifest), read_json(args.choices) if args.choices else None)
        elif command in {"dir", "dir."}:
            result = {"documents": bot.documents()}
        elif command == "status":
            result = bot.status()
        elif command == "delete":
            result = bot.delete(args.document)
        elif command == "retry":
            result = bot.retry(args.job)
        elif command == "ask":
            result = bot.ask(args.question, args.doc)
        elif command == "events":
            result = bot.events(args.consumer, args.owner, from_now=args.from_now)
        elif command == "ack":
            result = bot.ack(args.consumer, args.owner, args.through)
        elif command == "worker":
            if args.max_jobs < 0:
                raise BotError("invalid_limit", "max-jobs 不可為負數。")
            result = background(bot.store.home, args.max_jobs) if args.background else (serve(bot, args.max_jobs) if args.serve else bot.work(args.max_jobs))
        elif command in {"wiki", "graph"}:
            if args.action in {"on", "off"}:
                result = bot.wiki_toggle(args.action == "on")
            elif args.action == "list":
                result = bot.wiki_list()
            elif args.action == "lint":
                result = bot.wiki_lint()
            elif args.action == "build":
                result = bot.wiki_build(args.document)
            elif args.action == "next":
                result = bot.wiki_next(args.owner)
            elif args.action == "commit":
                result = bot.wiki_commit(read_json(args.payload), args.owner)
            elif args.action == "fail":
                result = bot.wiki_fail(args.job, args.owner, args.reason)
        emit({"ok": True, "data": result})
        return 0
    except BotError as exc:
        emit({"ok": False, "error": {"code": exc.code, "message": str(exc), **exc.details}})
        return 2
    except (OSError, ValueError, ImportError, sqlite3.Error) as exc:
        emit({"ok": False, "error": {"code": "runtime_error", "message": str(exc)}})
        return 3
    except Exception as exc:
        emit({"ok": False, "error": {"code": "runtime_error", "type": type(exc).__name__, "message": str(exc)}})
        return 3
    finally:
        if bot:
            bot.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
