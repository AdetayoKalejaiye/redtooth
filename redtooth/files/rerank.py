"""Re-ranking: a cross-encoder reads the request and each top candidate *together* and scores how well
they match, which is much better than comparing two separate rankings at telling "the term paper" from a
file that merely mentions terms or paper.

Model: cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 (Apache-2.0), multilingual (trained on MS MARCO
machine-translated into 14 languages), as its 8-bit ONNX build (~119 MB). Downloaded once in the background
on first start into ~/.cache/redtooth/models/; until it's there, search works exactly as before.
"""
from __future__ import annotations

import logging
import os
import platform
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from .embed import download_files

log = logging.getLogger("redtooth.rerank")

RERANK_DIR = Path(os.environ.get("REDTOOTH_RERANK_DIR", Path.home() / ".cache/redtooth/models/mmarco-mMiniLMv2-L12"))
RERANK_BASE = os.environ.get("REDTOOTH_RERANK_URL", "https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1/resolve/main")


def model_files() -> dict[str, str]:
    arm = platform.machine().lower() in ("arm64", "aarch64")
    return {"model.onnx": "onnx/model_qint8_arm64.onnx" if arm else "onnx/model_quint8_avx2.onnx", "tokenizer.json": "tokenizer.json"}


def ready(dest: Path = RERANK_DIR) -> bool:
    return all((dest / f).exists() and (dest / f).stat().st_size > 0 for f in model_files())


class CrossEncoder:
    name = "mmarco-mMiniLMv2-L12 (onnx)"

    def __init__(self, model_dir: Path = RERANK_DIR, max_len: int = 256):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.tok = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tok.enable_truncation(max_len)
        self.tok.enable_padding(pad_id=self.tok.token_to_id("<pad>") or 1, pad_token="<pad>")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, (os.cpu_count() or 2) // 2)
        self.sess = ort.InferenceSession(str(model_dir / "model.onnx"), opts, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}

    def score(self, query: str, passages: list[str]) -> np.ndarray:
        """Relevance of each passage to the query, 0..1 (sigmoid of the model's logit)."""
        if not passages:
            return np.zeros(0, np.float32)
        from .embed import _clean

        enc = self.tok.encode_batch([(_clean(query), _clean(p)) for p in passages])
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feeds["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
        logits = np.asarray(self.sess.run(None, feeds)[0], dtype=np.float32).reshape(len(passages), -1)[:, 0]
        return 1.0 / (1.0 + np.exp(-logits))


class RerankerHolder:
    """Loads the re-ranker when it's available; downloads it in the background (never blocks a search)."""

    def __init__(self) -> None:
        self.model: Optional[CrossEncoder] = None
        self.error = ""
        self._lock = threading.Lock()
        self._loading = False

    def get(self) -> Optional[CrossEncoder]:
        return self.model

    def load(self, download: bool = True) -> None:
        def run() -> None:
            try:
                if not ready() and download and os.environ.get("REDTOOTH_OFFLINE", "") in ("", "0"):
                    download_files(RERANK_BASE, model_files(), RERANK_DIR, progress=None)
            except Exception as e:
                self.error = f"download: {type(e).__name__}: {e}"
                log.warning("re-ranker download failed (%s); search works without it and retries next start", self.error)
                return
            if not ready():
                return
            try:
                self.model = CrossEncoder()
                log.info("re-ranker ready: %s", CrossEncoder.name)
            except Exception as e:
                self.error = f"load: {type(e).__name__}: {e}"
                log.warning("re-ranker couldn't load (%s); removing the files so they're fetched again next start", self.error)
                for f in model_files():
                    try:
                        (RERANK_DIR / f).unlink()
                    except OSError:
                        pass

        with self._lock:
            if self._loading or self.model:
                return
            self._loading = True
        threading.Thread(target=run, name="redtooth-rerank-load", daemon=True).start()
