import hashlib
from pathlib import Path
import sys
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "document-bot" / "scripts"))
sys.path.insert(0, str(ROOT))

from docbot_core.engine import Bot


class TestTokenizer:
    def __call__(self, text, **kwargs):
        result = {"input_ids": list(range(len(text)))}
        if kwargs.get("return_offsets_mapping"):
            result["offset_mapping"] = [(i, i+1) for i in range(len(text))]
        return result


class TestEmbedder:
    """Deterministic vectors for lifecycle tests, never a production/relevance fallback."""
    tokenizer = TestTokenizer()
    __test__ = False

    def __init__(self, callback=None):
        self.callback = callback

    def encode(self, texts, query=False):
        if self.callback:
            callback, self.callback = self.callback, None
            callback()
        vectors = []
        for text in texts:
            if "INJECT_EMBED_FAILURE" in text:
                raise RuntimeError("injected embedding failure")
            vector = [0.0]*384
            for word in text.lower().split():
                vector[int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "little") % 384] += 1
            if not any(vector):
                vector[0] = 1
            vectors.append(vector)
        return vectors


@pytest.fixture
def tmp_path():
    path = ROOT / ".test-data" / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path


@pytest.fixture
def bot(tmp_path):
    workspace = tmp_path / "existing workspace"
    workspace.mkdir()
    instance = Bot(tmp_path / "home", initialize=True, embedder=TestEmbedder())
    instance.setup(workspace, download=False)
    yield instance
    instance.store.close()


def attachment(tmp_path, name="文件.txt", content="Aurora supports local document search.", identity=None):
    folder = tmp_path / uuid.uuid4().hex
    folder.mkdir()
    path = folder / name
    path.write_text(content, encoding="utf-8")
    return {"attachment_id": identity or uuid.uuid4().hex, "name": name, "path": str(path)}


def manifest(*files, ordered=True):
    return {"source": "chat_attachment", "host": "codex", "order_known": ordered, "attachments": list(files)}


def add_one(bot, tmp_path, name="文件.txt", content="Aurora supports local document search."):
    result = bot.add(manifest(attachment(tmp_path, name, content)))["files"][0]
    assert result["status"] == "queued", result
    return result


def drain_graph(bot, owner="test"):
    while True:
        batch = bot.graph_next(owner)
        if batch["status"] != "batch":
            break
        bot.graph_commit({"batch_id": batch["batch_id"], "entities": [], "relations": []}, owner)


def payload_for(batch):
    cid = batch["chunks"][0]["chunk_id"]
    quote = "星河公司開發 Aurora。"
    return {"batch_id": batch["batch_id"], "entities": [
        {"name": "星河公司", "type": "organization", "chunk_id": cid, "evidence": quote},
        {"name": "Aurora", "type": "product", "chunk_id": cid, "evidence": quote}],
        "relations": [{"subject": {"name": "星河公司", "type": "organization"}, "predicate": "開發", "object": {"name": "Aurora", "type": "product"}, "chunk_id": cid, "evidence": quote}]}
