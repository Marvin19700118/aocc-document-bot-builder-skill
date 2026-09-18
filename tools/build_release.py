"""Build a deterministic portable skill package without local libraries or user data."""
import hashlib
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dist" / "document-bot-1.0.0.zip"


def main():
    OUTPUT.parent.mkdir(exist_ok=True)
    files = [ROOT / "install.py", ROOT / "README.md"]
    files += [p for p in (ROOT / "document-bot").rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    files += [p for p in (ROOT / "examples").rglob("*") if p.is_file()]
    files += [p for p in (ROOT / "verification").iterdir() if p.is_file()]
    for folder in ("tests", "tools"):
        files += [p for p in (ROOT / folder).glob("*.py") if p.is_file()]
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(str(path.relative_to(ROOT)).replace("\\", "/"), date_time=(2026, 9, 18, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    sha = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    OUTPUT.with_suffix(".zip.sha256").write_text(f"{sha}  {OUTPUT.name}\n", encoding="ascii")
    print(f"{OUTPUT}\nSHA256 {sha}")


if __name__ == "__main__":
    main()
