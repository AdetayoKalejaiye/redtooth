"""Speed-up index for vector search: a memory-mapped, cluster-sorted matrix.

Why not search inside sqlite-vec? Measured at 120k chunks (tools/ann_bench.py and
the notes in README): sqlite-vec's exact scan took ~112 ms per query versus ~18 ms
for a NumPy matrix product over a memory-mapped copy, and its partition-key IVF
(~95 ms at 95% recall) lost to NumPy IVF (~9 ms). sqlite-vec stays the durable
store; this file is derived from it and rebuilt in seconds.

Layout (one build = one id, swapped atomically through meta.json):
  vectors-<id>.f32   float32 [n, dim], rows grouped by cluster
  rows-<id>.npz      chunk ids, file ids, extension codes, mtimes, cluster offsets, centroids
  meta.json          build id, dims, counts, extension vocabulary, tuned nprobe and the tuning report

Below IVF_MIN rows the matrix is one cluster and every search is exact (~10-15 ms
per 100k rows). Above it, k-means clusters the rows and a query scores only the
nearest clusters; the number probed is tuned on the user's own library to keep
recall@10 >= TARGET_RECALL against exact search, with realistic short queries.
Type/time filters are array masks. When they leave few rows, search is exact over
just those rows, so selective filters are both fast and exact.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

import numpy as np

IVF_MIN = int(os.environ.get("REDTOOTH_IVF_MIN", 200_000))  # exact below this: ~25 ms per 200k E5 vectors
TARGET_RECALL = 0.95
SELECTIVE = 0.10  # filters leaving <= 10% of rows -> exact over those rows
NPROBE_STEPS = (4, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256)


def kmeans(x: np.ndarray, k: int, rng: np.random.Generator, iters: int = 10) -> np.ndarray:
    """Spherical k-means (cosine) on unit vectors."""
    k = min(k, len(x))
    c = x[rng.choice(len(x), k, replace=False)].copy()
    for _ in range(iters):
        a = np.argmax(x @ c.T, axis=1)
        for j in range(k):
            m = x[a == j]
            c[j] = m.mean(0) if len(m) else x[rng.integers(len(x))]
        c /= np.linalg.norm(c, axis=1, keepdims=True)
    return c.astype(np.float32)


def _topk(scores: np.ndarray, k: int) -> np.ndarray:
    if len(scores) <= k:
        return np.argsort(-scores)
    part = np.argpartition(-scores, k)[:k]
    return part[np.argsort(-scores[part])]


class FlatIndex:
    def __init__(self, folder: Path, meta: dict, vectors: np.ndarray, rows: dict):
        self.folder, self.meta, self.vectors = folder, meta, vectors
        self.ids = rows["ids"]
        self.file_ids = rows["file_ids"]
        self.ext = rows["ext"]
        self.mtime = rows["mtime"]
        self.offsets = rows["offsets"]
        self.centroids = rows["centroids"] if rows["centroids"].size else None
        self.ext_vocab: dict[str, int] = meta["ext_vocab"]
        self.nprobe = int(meta.get("nprobe") or 0)

    @property
    def n(self) -> int:
        return len(self.ids)

    @property
    def nlist(self) -> int:
        return len(self.offsets) - 1

    def status(self) -> dict:
        return {
            "vectors": self.n,
            "ivf": self.nlist > 1,
            "clusters": self.nlist if self.nlist > 1 else 0,
            "nprobe": self.nprobe if self.nlist > 1 else None,
            "tuning": self.meta.get("tuning"),
            "built_at": self.meta.get("built_at"),
            "build_s": self.meta.get("build_s"),
        }

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, folder: str | Path, dim: int, embedder: str) -> Optional["FlatIndex"]:
        folder = Path(folder)
        try:
            meta = json.loads((folder / "meta.json").read_text())
            if meta["dim"] != dim or meta["embedder"] != embedder:
                return None
            bid = meta["build_id"]
            n = meta["n"]
            vec = np.memmap(folder / f"vectors-{bid}.f32", np.float32, "r", shape=(n, dim)) if n else np.zeros((0, dim), np.float32)
            with np.load(folder / f"rows-{bid}.npz") as z:
                rows = {k: z[k] for k in z.files}
            return cls(folder, meta, vec, rows)
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            return None

    # ----------------------------------------------------------------- build
    @classmethod
    def build(
        cls,
        folder: str | Path,
        dim: int,
        embedder: str,
        blocks: Callable[[], Iterable[tuple[np.ndarray, np.ndarray]]],
        row_meta: dict[int, tuple[int, str, float]],
        max_chunk_id: int,
        tuning_queries: Optional[Callable[[int], list[np.ndarray]]] = None,
        seed: int = 0,
    ) -> "FlatIndex":
        """Stream vectors from `blocks()` (called up to three times) into a new build.
        row_meta: chunk_id -> (file_id, ext, mtime). Rows without metadata are skipped."""
        t0 = time.time()
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        rng = np.random.default_rng(seed)

        # Pass 1: ids (+ a sample for k-means).
        ids_l: list[np.ndarray] = []
        for ids, _ in blocks():
            ids_l.append(np.asarray(ids, np.int64))
        ids_all = np.concatenate(ids_l) if ids_l else np.zeros(0, np.int64)
        keep = np.array([int(i) in row_meta for i in ids_all], bool) if len(ids_all) else np.zeros(0, bool)
        n = int(keep.sum())
        nlist = max(16, int(np.sqrt(n))) if n >= IVF_MIN else 1

        centroids = np.zeros((0, dim), np.float32)
        if nlist > 1:
            want = min(n, nlist * 30)
            p = want / n
            sample = []
            for ids, mat in blocks():
                m = np.array([int(i) in row_meta for i in ids], bool) & (rng.random(len(ids)) < p)
                if m.any():
                    sample.append(np.asarray(mat, np.float32)[m])
            centroids = kmeans(np.vstack(sample), nlist, rng)

        # Pass 2: cluster per row -> position in the sorted matrix.
        cluster_l = []
        for ids, mat in blocks():
            m = np.array([int(i) in row_meta for i in ids], bool)
            if not m.any():
                continue
            mat = np.asarray(mat, np.float32)[m]
            cluster_l.append(np.argmax(mat @ centroids.T, axis=1) if nlist > 1 else np.zeros(len(mat), np.int64))
        clusters = np.concatenate(cluster_l) if cluster_l else np.zeros(0, np.int64)
        order = np.argsort(clusters, kind="stable")
        pos = np.empty(n, np.int64)
        pos[order] = np.arange(n)
        offsets = np.searchsorted(clusters[order], np.arange(nlist + 1)) if n else np.zeros(nlist + 1, np.int64)

        # Pass 3: write vectors and row metadata in sorted order.
        bid = f"{int(time.time() * 1000)}-{os.getpid()}"
        vpath = folder / f"vectors-{bid}.f32"
        out = np.memmap(vpath, np.float32, "w+", shape=(max(n, 1), dim))
        ids_s = np.empty(n, np.int64)
        fids_s = np.empty(n, np.int64)
        mt_s = np.empty(n, np.float64)
        ext_codes = np.empty(n, np.int16)
        vocab: dict[str, int] = {}
        r = 0
        for ids, mat in blocks():
            m = np.array([int(i) in row_meta for i in ids], bool)
            if not m.any():
                continue
            mat = np.asarray(mat, np.float32)[m]
            for i, v in zip(np.asarray(ids)[m], mat):
                fid, ext, mt = row_meta[int(i)]
                j = pos[r]
                out[j] = v
                ids_s[j], fids_s[j], mt_s[j] = int(i), fid, mt
                ext_codes[j] = vocab.setdefault(ext or "", len(vocab))
                r += 1
        out.flush()
        del out
        np.savez(folder / f"rows-{bid}.npz", ids=ids_s, file_ids=fids_s, ext=ext_codes, mtime=mt_s, offsets=offsets.astype(np.int64), centroids=centroids)
        meta = {"build_id": bid, "dim": dim, "n": n, "embedder": embedder, "ext_vocab": vocab, "max_chunk_id": int(max_chunk_id), "built_at": time.time(), "nprobe": 0}
        idx = cls(folder, meta, np.memmap(vpath, np.float32, "r", shape=(n, dim)) if n else np.zeros((0, dim), np.float32), {"ids": ids_s, "file_ids": fids_s, "ext": ext_codes, "mtime": mt_s, "offsets": offsets, "centroids": centroids})
        if nlist > 1:
            qs = tuning_queries(120) if tuning_queries else []
            idx.nprobe, meta["tuning"] = idx.tune(qs)
            meta["nprobe"] = idx.nprobe
        meta["build_s"] = round(time.time() - t0, 2)
        tmp = folder / "meta.json.tmp"
        tmp.write_text(json.dumps(meta))
        os.replace(tmp, folder / "meta.json")  # commit point
        for f in folder.iterdir():  # drop older builds
            if f.name.startswith(("vectors-", "rows-")) and bid not in f.name:
                try:
                    f.unlink()
                except OSError:
                    pass
        return idx

    def tune(self, queries: list[np.ndarray], target: float = TARGET_RECALL) -> tuple[int, dict]:
        """Smallest nprobe with recall@10 >= target versus exact search on this library."""
        default = max(8, round(0.03 * self.nlist))
        if not queries:
            return default, {"note": "no tuning queries; default used"}
        # Tie-aware recall: a result counts if it scores at least the exact 10th-best score.
        kth = []
        for q in queries:
            sc = self.vectors @ q
            kth.append(float(sc[_topk(sc, 10)[-1]]) - 1e-6)
        tried = {}
        for npb in [s for s in NPROBE_STEPS if s < self.nlist] + [self.nlist]:
            rec = []
            for q, t in zip(queries, kth):
                rows = self._probe_rows(q, npb, 10, None)
                rec.append(sum(float(self.vectors[r] @ q) >= t for r in rows) / 10)
            tried[npb] = round(float(np.mean(rec)), 3)
            if tried[npb] >= target:
                return npb, {"target_recall@10": target, "queries": len(queries), "recall_by_nprobe": tried, "chosen": npb}
        return self.nlist, {"target_recall@10": target, "queries": len(queries), "recall_by_nprobe": tried, "chosen": self.nlist}

    # ---------------------------------------------------------------- search
    def _probe_rows(self, q: np.ndarray, nprobe: int, k: int, mask: Optional[np.ndarray]) -> list[int]:
        """Row positions of the best k among the nprobe nearest clusters."""
        cl = np.argsort(-(self.centroids @ q))[:nprobe]
        rows, scores = [], []
        for c in cl:
            a, b = int(self.offsets[c]), int(self.offsets[c + 1])
            if a == b:
                continue
            s = self.vectors[a:b] @ q
            if mask is not None:
                mm = mask[a:b]
                if not mm.any():
                    continue
                s = s[mm]
                rows.append(np.arange(a, b)[mm])
            else:
                rows.append(np.arange(a, b))
            scores.append(s)
        if not scores:
            return []
        rows_a, s_a = np.concatenate(rows), np.concatenate(scores)
        return rows_a[_topk(s_a, k)].tolist()

    def search(self, q: np.ndarray, k: int, exts: Optional[set[str]] = None, since: Optional[float] = None, until: Optional[float] = None) -> list[tuple[int, int, float]]:
        """(chunk_id, file_id, cosine similarity), best first, filtered."""
        if not self.n:
            return []
        mask = None
        if exts is not None:
            codes = [self.ext_vocab[e] for e in exts if e in self.ext_vocab]
            mask = np.isin(self.ext, np.array(codes, np.int16)) if codes else np.zeros(self.n, bool)
        if since is not None:
            mask = (self.mtime >= since) if mask is None else mask & (self.mtime >= since)
        if until is not None:
            mask = (self.mtime < until) if mask is None else mask & (self.mtime < until)
        if mask is not None:
            sel = int(mask.sum())
            if sel == 0:
                return []
            if self.nlist == 1 or sel <= SELECTIVE * self.n:
                idx = np.flatnonzero(mask)  # few rows: exact over just these
                s = self.vectors[idx] @ q
                top = _topk(s, k)
                return [(int(self.ids[idx[t]]), int(self.file_ids[idx[t]]), float(s[t])) for t in top]
        if self.nlist == 1:
            s = self.vectors @ q
            top = _topk(s, k)
            return [(int(self.ids[t]), int(self.file_ids[t]), float(s[t])) for t in top]
        nprobe = self.nprobe or max(8, round(0.03 * self.nlist))
        rows = self._probe_rows(q, nprobe, k, mask)
        if mask is not None and len(rows) < min(k, 20):  # filters thinned the probed clusters: widen once
            rows = self._probe_rows(q, min(self.nlist, nprobe * 4), k, mask)
        return [(int(self.ids[r]), int(self.file_ids[r]), float(self.vectors[r] @ q)) for r in rows]
