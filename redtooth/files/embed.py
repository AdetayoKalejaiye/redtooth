"""Local multilingual E5 embeddings (the default search model).

Model: multilingual-e5-small (384-d, ~100 languages), run on-device with ONNX
Runtime. It downloads once, automatically, the first time Redtooth needs it
(~120 MB into ~/.cache/redtooth/models/multilingual-e5-small/); `redtooth models
fetch` does the same ahead of time. If sentence-transformers is installed it is
used when the ONNX files aren't there. E5 expects "query: " / "passage: " prefixes.

If the download fails (offline, blocked), search falls back to a hashed character
n-gram embedder so it still works (spelling variants and word forms, not
paraphrases), `status()` reports which backend is live, and the download is tried
again on the next start. Opt out with REDTOOTH_EMBEDDER=hash or REDTOOTH_OFFLINE=1.
"""
from __future__ import annotations

import hashlib
import zlib
import logging
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Protocol

import numpy as np

log = logging.getLogger("redtooth.embed")

MODEL_DIR = Path(os.environ.get("REDTOOTH_MODEL_DIR", Path.home() / ".cache/redtooth/models/multilingual-e5-small"))
HF_BASE = os.environ.get("REDTOOTH_MODEL_URL", "https://huggingface.co/Xenova/multilingual-e5-small/resolve/main")
MODEL_FILES = {"model.onnx": "onnx/model_quantized.onnx", "tokenizer.json": "tokenizer.json"}


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str], kind: str = "passage") -> np.ndarray: ...


def model_ready(dest: Path = MODEL_DIR) -> bool:
    return all((dest / f).exists() and (dest / f).stat().st_size > 0 for f in MODEL_FILES)


def _print_progress(name: str, done: int, total: int) -> None:
    mb = lambda n: f"{n / 1e6:.0f}"
    line = f"\rDownloading search model (one time): {name} {mb(done)}" + (f" / {mb(total)} MB" if total else " MB")
    sys.stderr.write(line + ("\n" if total and done >= total else ""))
    sys.stderr.flush()


def fetch_model(dest: Path = MODEL_DIR, progress: Optional[Callable[[str, int, int], None]] = _print_progress,
                timeout: float = 30) -> Path:
    """Download the model files that are missing. Partial downloads never count as present."""
    dest.mkdir(parents=True, exist_ok=True)
    for local, remote in MODEL_FILES.items():
        target = dest / local
        if target.exists() and target.stat().st_size > 0:
            continue
        url = f"{HF_BASE}/{remote}"
        log.info("Downloading %s", url)
        tmp = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(url, timeout=timeout) as r, open(tmp, "wb") as f:
            total, done = int(r.headers.get("Content-Length") or 0), 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(local, done, total)
        if total and done != total:
            tmp.unlink(missing_ok=True)
            raise IOError(f"{local}: got {done} of {total} bytes")
        os.replace(tmp, target)
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
        # Batch texts of similar length together so short chunks aren't padded to the longest one.
        order = sorted(range(len(texts)), key=lambda j: len(texts[j]))
        texts = [texts[j] for j in order]
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
        if not out:
            return np.zeros((0, self.dim), np.float32)
        mat = np.vstack(out).astype(np.float32)
        res = np.empty_like(mat)
        res[order] = mat
        return res


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


def load_embedder(prefer: Optional[str] = None, download: bool = True) -> Embedder:
    """E5 by default: downloads it on first use. Falls back to the hashed embedder only if that fails."""
    prefer = prefer or os.environ.get("REDTOOTH_EMBEDDER", "auto")
    if prefer in ("auto", "e5"):
        offline = os.environ.get("REDTOOTH_OFFLINE", "") not in ("", "0")
        failure = ""
        if not model_ready() and download and not offline:
            try:
                fetch_model()
            except Exception as e:
                failure = f"{type(e).__name__}: {e}"
        if model_ready():
            try:
                return E5OnnxEmbedder()
            except Exception as e:
                failure = f"couldn't load it ({e})"
                for f in MODEL_FILES:  # damaged download: remove it so the next start fetches it again
                    (MODEL_DIR / f).unlink(missing_ok=True)
        try:
            return E5SentenceTransformer()
        except Exception:
            pass
        if prefer == "e5":
            raise RuntimeError(f"E5 model not available ({failure or 'offline'}); run `redtooth models fetch`")
        log.warning("Search model not available (%s). Using basic keyword search for now; "
                    "Redtooth will try the download again next time it starts.", failure or "offline mode")
    return HashEmbedder()
