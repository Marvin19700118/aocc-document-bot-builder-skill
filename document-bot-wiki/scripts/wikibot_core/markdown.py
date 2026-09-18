from __future__ import annotations

import json
import os
from pathlib import Path
import re
import uuid

from .parsing import Passage
from .qmd import terms
from .storage import BotError


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise BotError('unsafe_projection', '衍生檔不可為符號連結。')
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def split_passages(passages, maximum=2400, overlap=200):
    output = []
    for passage in passages:
        for start in range(0, len(passage.text), maximum-overlap):
            text = passage.text[start:start+maximum]
            locator = dict(passage.locator, char_start=start, char_end=start+len(text))
            if locator['type'] == 'text':
                locator['line_start'] += passage.text[:start].count('\n')
                locator['line_end'] = locator['line_start'] + text.count('\n')
            if text.strip():
                output.append(Passage(text, locator))
            if start+maximum >= len(passage.text):
                break
    return output


class MarkdownMixin:
    def artifact(self, *parts):
        path = self.store.home.joinpath(*parts)
        parent = path
        while parent != self.store.home:
            if parent.is_symlink():
                raise BotError('unsafe_projection', '衍生資料路徑不可為符號連結。')
            parent = parent.parent
        if not path.resolve().is_relative_to(self.store.home):
            raise BotError('unsafe_projection', '衍生資料超出管理目錄。')
        return path

    def _remove_source_files(self, doc_id):
        if not re.fullmatch('[a-f0-9]{32}', doc_id):
            raise BotError('unsafe_document_id', '無效的文件 ID。')
        for prefix in ('knowledge', 'search'):
            folder = self.artifact(prefix, 'sources', doc_id)
            if folder.exists():
                for path in folder.iterdir():
                    if not re.fullmatch(r'\d{6}\.md', path.name):
                        raise BotError('unexpected_artifact', '來源衍生目錄含未知檔案，停止清理。')
                    self.artifact(prefix, 'sources', doc_id, path.name).unlink()
                folder.rmdir()

    def _write_source_files(self, doc, passages):
        self._remove_source_files(doc['id'])
        for ordinal, passage in enumerate(passages):
            self._check_cancelled(doc['id'])
            name = f'{ordinal:06d}.md'
            body = f'# {doc["name"]}\n\n來源：{json.dumps(passage.locator, ensure_ascii=False)}\n\n{passage.text}\n'
            atomic_text(self.artifact('knowledge', 'sources', doc['id'], name), body)
            # Search projection excludes filename/locator metadata and preserves CJK boundaries.
            atomic_text(self.artifact('search', 'sources', doc['id'], name), '# Source\n\n'+' '.join(terms(passage.text))+'\n')

    def _sync_projection(self):
        if self.store.setting('projection_dirty', '0') != '1':
            return
        rows = self.db.execute('''SELECT w.*, c.doc_id,c.ordinal,c.locator,d.name FROM wiki_claims w
            JOIN chunks c ON c.id=w.chunk_id JOIN documents d ON d.id=c.doc_id
            WHERE d.retired=0 AND d.delete_requested=0 ORDER BY w.topic,w.id''').fetchall()
        groups = {}
        for row in rows:
            groups.setdefault(row['topic'], []).append(row)
        for prefix in ('knowledge', 'search'):
            folder = self.artifact(prefix, 'wiki')
            folder.mkdir(parents=True, exist_ok=True)
            expected = {topic+'.md' for topic in groups}
            for path in folder.glob('*.md'):
                if path.name not in expected:
                    self.artifact(prefix, 'wiki', path.name).unlink()
        for topic, claims in groups.items():
            body = '# '+claims[0]['title']+'\n\n'
            lexical = []
            for row in claims:
                link = f'../sources/{row["doc_id"]}/{row["ordinal"]:06d}.md'
                body += f'- {row["claim"]}\n  - [{row["name"]}]({link})：{row["quote"]}\n'
                lexical.append(row['title']+' '+row['claim'])
            supporting_docs = {row['doc_id'] for row in claims}
            related = [(other, entries[0]['title']) for other, entries in groups.items()
                       if other != topic and supporting_docs.intersection(row['doc_id'] for row in entries)]
            if related:
                body += '\n## 相關主題（共用來源）\n\n'+'\n'.join(f'- [{title}]({other}.md)' for other,title in related)+'\n'
            # Topic pages accumulate source-attributed claims rather than overwriting other sources.
            atomic_text(self.artifact('knowledge', 'wiki', topic+'.md'), body)
            atomic_text(self.artifact('search', 'wiki', topic+'.md'), '# Wiki\n\n'+' '.join(terms(' '.join(lexical)))+'\n')
        index = '# Wiki 索引\n\n'+'\n'.join(f'- [{claims[0]["title"]}](wiki/{topic}.md)' for topic,claims in groups.items())+'\n'
        atomic_text(self.artifact('knowledge', 'index.md'), index)
        events = self.db.execute("SELECT id,kind,doc_id,created FROM events ORDER BY id DESC LIMIT 200").fetchall()
        log = '# Wiki 作業紀錄（最近 200 筆）\n\n'+'\n'.join(f'- {r["id"]}: {r["kind"]} / {r["doc_id"] or "library"} / {r["created"]}' for r in reversed(events))+'\n'
        atomic_text(self.artifact('knowledge', 'log.md'), log)
        self.qmd.update()
        self.store.set_setting('projection_dirty', '0')
