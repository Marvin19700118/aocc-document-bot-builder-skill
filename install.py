#!/usr/bin/env python3
"""Install only Document Bot's owned skill directory; never rewrite host configuration."""
import argparse
import json
from pathlib import Path
import shutil
import sys

VERSION = "1.0.0"


def install(host, user_home, update=False):
    source = Path(__file__).resolve().parent / "document-bot"
    target = user_home / (".claude/skills" if host == "claude" else ".agents/skills") / "document-bot"
    marker = target / ".document-bot-install.json"
    if target.is_symlink():
        raise ValueError(f"Refusing to overwrite a symlink: {target}")
    if target.exists() and (not update or not marker.is_file()):
        raise ValueError(f"Skill already exists: {target}. Use --update only for a previous Document Bot installation.")
    if target.exists() and marker.is_file():
        metadata = json.loads(marker.read_text(encoding="utf-8"))
        if metadata.get("package") != "document-bot":
            raise ValueError("Existing directory is not owned by this installer.")
    for item in source.rglob("*"):
        if "__pycache__" in item.parts or item.suffix == ".pyc":
            continue
        destination = target / item.relative_to(source)
        if item.is_symlink() or destination.is_symlink():
            raise ValueError("Symlinks inside the install tree are not supported.")
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
    marker.write_text(json.dumps({"package": "document-bot", "version": VERSION}), encoding="utf-8")
    return {"host": host, "path": str(target), "version": VERSION}


def main():
    parser = argparse.ArgumentParser(description="Install Document Bot into Claude Code and/or Codex")
    parser.add_argument("--host", choices=["claude", "codex", "both"], default="both")
    parser.add_argument("--user-home", type=Path, default=Path.home(), help="Override for portable installations or smoke tests")
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    try:
        results = [install(host, args.user_home.expanduser().resolve(), args.update) for host in (["claude", "codex"] if args.host == "both" else [args.host])]
        print(json.dumps({"installed": results, "next": "在既有工作目錄使用 document-bot setup。"}, ensure_ascii=False))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
