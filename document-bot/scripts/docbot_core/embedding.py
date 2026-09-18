from __future__ import annotations

import math
import struct

from .storage import BotError

MODEL = "intfloat/multilingual-e5-small"
DIMENSIONS = 384


def serialize(vector):
    vector = list(map(float, vector))
    if len(vector) != DIMENSIONS or not all(math.isfinite(x) for x in vector):
        raise BotError("invalid_embedding", "向量維度或數值無效。")
    norm = math.sqrt(sum(x*x for x in vector))
    if norm < 1e-12:
        raise BotError("invalid_embedding", "向量不能為零。")
    return struct.pack(f"{DIMENSIONS}f", *(x/norm for x in vector))


class LocalEmbedder:
    def __init__(self, store, download=False):
        from sentence_transformers import SentenceTransformer
        from huggingface_hub import model_info
        model = store.setting("model", MODEL)
        revision = store.setting("model_revision")
        if not revision:
            if not download:
                raise BotError("model_not_initialized", "請執行 setup 下載並固定模型版本。")
            revision = model_info(model).sha
        self.model = SentenceTransformer(model, revision=revision, device="cpu", cache_folder=str(store.home / "models"), local_files_only=not download, trust_remote_code=False)
        self.tokenizer = self.model.tokenizer
        if self.model.get_sentence_embedding_dimension() != DIMENSIONS:
            raise BotError("model_dimension_mismatch", "模型維度與資料庫不同。")
        if not self.tokenizer.is_fast:
            raise BotError("tokenizer_offsets_required", "需要支援原文位置的 fast tokenizer。")
        self.model.max_seq_length = 512
        if download:
            store.set_setting("model", model)
            store.set_setting("model_revision", revision)

    def encode(self, texts, query=False):
        prefix = "query: " if query else "passage: "
        return self.model.encode([prefix + t for t in texts], batch_size=16, normalize_embeddings=True, show_progress_bar=False).tolist()
