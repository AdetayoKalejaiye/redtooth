"""Local multilingual E5 embeddings.

Default model: multilingual-e5-small (384-d, ~100 languages), run on-device
with ONNX Runtime. Fetch once with `redtooth models fetch`; files land in
~/.cache/redtooth/models/multilingual-e5-small/. If sentence-transformers is
installed it is used instead. E5 expects "query: " / "passage: " prefixes.

When the model isn't available the index falls back to a hashed character
n-gram embedder so search still works (it catches spelling variants and word
forms, not paraphrases), and `status()` reports which backend is live.
"""
from __future__ import annotations

import hashlib
import zlib
import logging
import os
import re
import urllib.request
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger("redtooth.embed")

MODEL_DIR = Path(os.environ.get("REDTOOTH_MODEL_DIR", Path.home() / ".cache/redtooth/models/multilingual-e5-small"))
HF_BASE = os.environ.get("REDTOOTH_MODEL_URL", "https://huggingface.co/Xenova/multilingual-e5-small/resolve/main")
MODEL_FILES = {"model.onnx": "onnx/model_quantized.onnx", "tokenizer.json": "tokenizer.json"}


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str], kind: str = "passage") -> np.ndarray: ...


def fetch_model(dest: Path = MODEL_DIR) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for local, remote in MODEL_FILES.items():
        target = dest / local
        if target.exists() and target.stat().st_size > 0:
            continue
        url = f"{HF_BASE}/{remote}"
        log.warning("Downloading %s", url)
        tmp = target.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.rename(target)
    return dest


class E5OnnxEmbedder:
    name = "multilingual-e5-small (onnx)"
    dim = 384
    min_sim, band = 0.70, 0.05

    def __init__(self, model_dir: Path = MODEL_DIR):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.tok = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tok.enable_truncation(512)
        self.tok.enable_padding(pad_id=self.tok.token_to_id("<pad>") or 1, pad_token="<pad>")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, (os.cpu_count() or 2) // 2)
        self.sess = ort.InferenceSession(str(model_dir / "model.onnx"), opts, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}

    def embed(self, texts: list[str], kind: str = "passage") -> np.ndarray:
        out = []
        for i in range(0, len(texts), 32):
            batch = [f"{kind}: {t}" for t in texts[i : i + 32]]
            enc = self.tok.encode_batch(batch)
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self.sess.run(None, feeds)[0]
            m = mask[..., None].astype(np.float32)
            vec = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
            out.append(vec / np.linalg.norm(vec, axis=1, keepdims=True))
        return np.vstack(out).astype(np.float32) if out else np.zeros((0, self.dim), np.float32)


class E5SentenceTransformer:
    name = "multilingual-e5-small (sentence-transformers)"
    dim = 384
    min_sim, band = 0.70, 0.05

    def __init__(self, model: str = "intfloat/multilingual-e5-small"):
        from sentence_transformers import SentenceTransformer

        self.m = SentenceTransformer(model)

    def embed(self, texts: list[str], kind: str = "passage") -> np.ndarray:
        return self.m.encode([f"{kind}: {t}" for t in texts], normalize_embeddings=True, batch_size=32).astype(np.float32)


class HashEmbedder:
    """Fallback: hashed word + character n-gram features (no model download).
    Feature hashes are cached (n-grams repeat constantly), so embedding is mostly dict lookups."""

    name = "hashed n-grams v2 (fallback)"
    dim = 512
    min_sim, band = 0.12, 0.08

    def __init__(self) -> None:
        self._cache: dict[str, tuple[int, float]] = {}

    def _feat(self, f: str) -> tuple[int, float]:
        hit = self._cache.get(f)
        if hit is None:
            h = zlib.crc32(f.encode())
            hit = (h % self.dim, (1.0 if f[0] == "w" else 0.35) * (1.0 if h & 0x80000000 else -1.0))
            if len(self._cache) < 2_000_000:
                self._cache[f] = hit
        return hit

    def embed(self, texts: list[str], kind: str = "passage") -> np.ndarray:
        mat = np.zeros((len(texts), self.dim), np.float32)
        feat = self._feat
        for r, t in enumerate(texts):
            row = mat[r]
            idx, val = [], []
            for w in re.findall(r"\w+", t.lower()):
                i, v = feat("w:" + w)
                idx.append(i)
                val.append(v)
                w2 = f"<{w}>"
                for n in (3, 4):
                    for k in range(len(w2) - n + 1):
                        i, v = feat("c:" + w2[k : k + n])
                        idx.append(i)
                        val.append(v)
            if idx:
                np.add.at(row, idx, val)
                nrm = np.linalg.norm(row)
                if nrm:
                    row /= nrm
        return mat


def load_embedder(prefer: str = "auto") -> Embedder:
    if prefer in ("auto", "e5"):
        if (MODEL_DIR / "model.onnx").exists():
            try:
                return E5OnnxEmbedder()
            except Exception as e:
                log.warning("E5 ONNX load failed: %s", e)
        try:
            return E5SentenceTransformer()
        except Exception:
            pass
        if prefer == "e5":
            raise RuntimeError("E5 model not available; run `redtooth models fetch`")
        log.warning("E5 model not found; using fallback embedder. Run `redtooth models fetch` for semantic search.")
    return HashEmbedder()
