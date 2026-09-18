"""Package only the independent, vector-free Wiki skill and its validation assets."""
import hashlib
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'dist/document-bot-wiki-2.0.0-alpha.1.zip'


def main():
    files=[ROOT/name for name in ('README.md','spec.md','install-wiki.py','README-vector-v1.md','spec-vector-v1.md')]
    for folder in ('document-bot-wiki','examples','tests_wiki','verification/wiki'):
        files.extend(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc')
    files.extend(ROOT/'tools'/name for name in ('live_wiki_check.py','live_wiki_queue_check.py','build_wiki_release.py'))
    OUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            info=zipfile.ZipInfo(path.relative_to(ROOT).as_posix(),date_time=(2026,9,18,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,path.read_bytes())
    sha=hashlib.sha256(OUT.read_bytes()).hexdigest()
    OUT.with_suffix('.zip.sha256').write_text(f'{sha}  {OUT.name}\n',encoding='ascii')
    print(f'{OUT}\nSHA256 {sha}')


if __name__=='__main__': main()
