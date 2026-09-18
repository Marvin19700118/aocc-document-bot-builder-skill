#!/usr/bin/env python3
"""Create a private runtime, install dependencies, then register the existing workspace."""
import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import venv

from wikibot_core.storage import FileLock, home_path
from wikibot_core.qmd import QmdIndex


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--home")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise SystemExit("需要 Python 3.11 或更新版本。")
    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        raise SystemExit("workspace 必須是已存在的工作目錄。")
    home = home_path(args.home)
    home.mkdir(parents=True, exist_ok=True)
    runtime = home / "runtime"
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    node = shutil.which("node")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not node or not npm:
        raise SystemExit("需要 Node.js 22+（含 npm）。請安裝後重試 setup。")
    version = subprocess.check_output([node, "--version"], text=True).strip()
    if int(version.lstrip("v").split(".")[0]) < 22:
        raise SystemExit("QMD 需要 Node.js 22+。")
    with FileLock(home / "bootstrap.lock"):
        if not python.exists():
            venv.EnvBuilder(with_pip=True).create(runtime)
        else:
            check = subprocess.run([str(python), "-m", "pip", "--version"], capture_output=True)
            if check.returncode:
                subprocess.run([str(python), "-m", "ensurepip", "--upgrade"], check=True)
        qmd = home / "qmd-runtime"
        qmd.mkdir(exist_ok=True)
        package = Path(__file__).resolve().parents[1] / "qmd-package"
        for name in ("package.json", "package-lock.json"):
            shutil.copy2(package / name, qmd / name)
        installed = qmd / "node_modules/@tobilu/qmd/package.json"
        stamp = qmd / ".installed-lock.sha256"
        fingerprint = hashlib.sha256((qmd / "package-lock.json").read_bytes()).hexdigest()
        if not installed.exists() or not stamp.exists() or stamp.read_text() != fingerprint:
            # Pinned better-sqlite3 includes native prebuilts. No postinstall builds,
            # GPU probes, embedding downloads or unused code-parser builds are needed.
            subprocess.run([npm, "ci", "--prefix", str(qmd), "--ignore-scripts", "--no-audit", "--no-fund"], check=True)
            QmdIndex(home).call("status")
            stamp.write_text(fingerprint, encoding="ascii")
        requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(requirements)], check=True)
    return subprocess.call([str(python), str(Path(__file__).with_name("document_bot.py")), "--home", str(home), "setup", "--workspace", str(workspace)])


if __name__ == "__main__":
    raise SystemExit(main())
