#!/usr/bin/env python3
"""Create a private runtime, install dependencies, then register the existing workspace."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

from docbot_core.storage import FileLock, home_path


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
    with FileLock(home / "bootstrap.lock"):
        if not python.exists():
            venv.EnvBuilder(with_pip=True).create(runtime)
        else:
            check = subprocess.run([str(python), "-m", "pip", "--version"], capture_output=True)
            if check.returncode:
                subprocess.run([str(python), "-m", "ensurepip", "--upgrade"], check=True)
        requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(requirements)], check=True)
    return subprocess.call([str(python), str(Path(__file__).with_name("document_bot.py")), "--home", str(home), "setup", "--workspace", str(workspace)])


if __name__ == "__main__":
    raise SystemExit(main())
