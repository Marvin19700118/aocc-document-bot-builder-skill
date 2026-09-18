from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unicodedata

from .storage import BotError

QMD_VERSION = '2.8.3'


def terms(text):
    """CJK bigrams are lexical terms, not vectors; evidence keeps original text."""
    text = unicodedata.normalize('NFKC', text).casefold()
    tokens = re.findall(r'[a-z0-9_]+|[\u3400-\u9fff]+', text)
    output = []
    for token in tokens:
        if re.fullmatch(r'[\u3400-\u9fff]+', token):
            output.extend(token[i:i+2] for i in range(max(1, len(token)-1)))
        else:
            output.append(token)
    return list(dict.fromkeys(output))


class QmdIndex:
    def __init__(self, home):
        self.home = Path(home)
        self.root = self.home / 'search'
        for folder in ('sources', 'wiki'):
            (self.root / folder).mkdir(parents=True, exist_ok=True)

    def call(self, action, **kwargs):
        prefix = Path(os.environ.get('DOCUMENT_BOT_QMD_PREFIX', self.home / 'qmd-runtime'))
        module = prefix / 'node_modules/@tobilu/qmd/dist/index.js'
        package = prefix / 'node_modules/@tobilu/qmd/package.json'
        node = shutil.which('node')
        if not node or not module.is_file():
            raise BotError('qmd_unavailable', '需要 Node 22+ 與 QMD；請執行新版 bootstrap。')
        if json.loads(package.read_text(encoding='utf-8'))['version'] != QMD_VERSION:
            raise BotError('qmd_version_mismatch', f'需要已驗證的 QMD {QMD_VERSION}。')
        request = dict(action=action, module=str(module), database=str(self.home/'qmd.sqlite'),
                       sources=str(self.root/'sources'), wiki=str(self.root/'wiki'), **kwargs)
        env = dict(os.environ, QMD_FORCE_CPU='1', QMD_LLAMA_GPU='false',
                   XDG_CACHE_HOME=str(self.home/'qmd-cache'), QMD_CONFIG_DIR=str(self.home/'qmd-config'))
        try:
            result = subprocess.run([node, str(Path(__file__).parents[1]/'qmd_bridge.mjs')],
                input=json.dumps(request), text=True, encoding='utf-8', capture_output=True,
                env=env, timeout=180, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except subprocess.TimeoutExpired as exc:
            raise BotError('qmd_timeout', 'QMD 超時；工作保留，可重試。') from exc
        if result.returncode:
            raise BotError('qmd_failed', result.stderr[-3000:] or 'QMD 執行失敗。')
        try:
            return json.loads(result.stdout)['data']
        except (ValueError, KeyError) as exc:
            raise BotError('qmd_protocol', 'QMD 回傳格式不相容。') from exc

    def update(self):
        return self.call('update')

    def search(self, question, limit):
        tokens = terms(question)[:64]
        if not tokens:
            return []
        # QMD parses ordinary terms as AND. Search independently, then fuse lexical ranks.
        return self.call('search', terms=tokens, limit=limit)
