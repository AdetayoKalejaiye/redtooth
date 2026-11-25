"""Benchmark nearest-neighbour strategies in sqlite-vec against exact search.

Findings that shaped Redtooth's index (sqlite-vec 0.1.9): its bit[] and int8[]
searches are slower than float search, so binary/int8 prefilters don't pay
off; partition keys do, which makes an IVF (clustered) index possible.

Synthetic vectors imitate E5's geometry: a strong shared direction (unrelated
texts still score ~0.7-0.8 cosine), topic clusters, and noise. Queries are
perturbed copies of stored chunks, like paraphrases. Recall@10 is measured
against exact float cosine search with the same type filter applied.

  python tools/ann_bench.py --sizes 20000 100000 300000
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import time

import numpy as np
import sqlite_vec

DIM = 384


def make_vectors(n: int, rng: np.random.Generator, topics: int = 300, sub: int = 12) -> np.ndarray:
    """Shared direction + topic + subtopic + noise, tuned so mean pairwise cosine is ~0.75 like E5."""
    shared = rng.standard_normal(DIM)
    shared /= np.linalg.norm(shared)

    def unit(m):
        return m / np.linalg.norm(m, axis=-1, keepdims=True)

    topic = unit(rng.standard_normal((topics, DIM)))
    subt = unit(rng.standard_normal((topics, sub, DIM)))
    t = rng.integers(0, topics, n)
    st = rng.integers(0, sub, n)
    x = 1.0 * shared + 0.45 * topic[t] + 0.3 * subt[t, st] + 0.22 * unit(rng.standard_normal((n, DIM)))
    return unit(x).astype(np.float32)


def kmeans(x: np.ndarray, k: int, rng: np.random.Generator, iters: int = 8) -> np.ndarray:
    sample = x[rng.choice(len(x), min(len(x), k * 40), replace=False)]
    c = sample[rng.choice(len(sample), k, replace=False)].copy()
    for _ in range(iters):
        a = np.argmax(sample @ c.T, axis=1)
        for j in range(k):
            m = sample[a == j]
            c[j] = m.mean(0) if len(m) else sample[rng.integers(len(sample))]
        c /= np.linalg.norm(c, axis=1, keepdims=True)
    return c


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    return db


def run(n: int, queries: int, seed: int = 1) -> list[dict]:
    rng = np.random.default_rng(seed)
    x = make_vectors(n, rng)
    ext = rng.choice(np.array([".pptx", ".docx", ".md", ".pdf"]), n, p=[0.15, 0.25, 0.45, 0.15])
    mean = x.mean(0)
    nlist = max(16, int(np.sqrt(n)))
    cent = kmeans(x, nlist, rng)
    cluster = np.argmax(x @ cent.T, axis=1)

    db = connect()
    t0 = time.time()
    db.execute(f"CREATE VIRTUAL TABLE flt USING vec0(id INTEGER PRIMARY KEY, e float[{DIM}] distance_metric=cosine, ext TEXT)")
    db.execute(f"CREATE VIRTUAL TABLE ivf USING vec0(id INTEGER PRIMARY KEY, c INTEGER PARTITION KEY, e float[{DIM}] distance_metric=cosine, ext TEXT)")
    db.execute(f"CREATE VIRTUAL TABLE ivfm USING vec0(id INTEGER PRIMARY KEY, e float[{DIM}] distance_metric=cosine, ext TEXT, c INTEGER)")
    db.executemany("INSERT INTO flt VALUES(?,?,?)", ((i, x[i].tobytes(), ext[i]) for i in range(n)))
    db.executemany("INSERT INTO ivf VALUES(?,?,?,?)", ((i, int(cluster[i]), x[i].tobytes(), ext[i]) for i in range(n)))
    db.executemany("INSERT INTO ivfm VALUES(?,?,?,?)", ((i, x[i].tobytes(), ext[i], int(cluster[i])) for i in range(n)))
    build = time.time() - t0

    qi = rng.choice(n, queries, replace=False)
    noise = rng.standard_normal((queries, DIM)).astype(np.float32)
    qs = x[qi] + 0.25 * noise / np.linalg.norm(noise, axis=1, keepdims=True)
    qs = (qs / np.linalg.norm(qs, axis=1, keepdims=True)).astype(np.float32)
    filt = rng.choice(np.array([".pptx", ".docx", ".md", ".pdf", ""]), queries)

    def where(f):  # same type filter in every strategy
        return (" AND ext = ?", (f,)) if f else ("", ())

    def exact(q, f):
        w, a = where(f)
        return [r[0] for r in db.execute(f"SELECT id FROM flt WHERE e MATCH ? AND k=10{w}", (q.tobytes(), *a))]

    def ivf(q, f, nprobe=8):
        """Probe the nearest clusters; widen if a type filter leaves fewer than 10 results."""
        w, a = where(f)
        order = np.argsort(-(cent @ q))
        while True:
            probe = ",".join(map(str, order[:nprobe]))
            rows = [r[0] for r in db.execute(f"SELECT id FROM ivf WHERE e MATCH ? AND k=10 AND c IN ({probe}){w}", (q.tobytes(), *a))]
            if len(rows) >= 10 or nprobe >= nlist:
                return rows
            nprobe *= 2

    def ivf_meta(q, f, nprobe=8):
        """Cluster stored as an ordinary metadata column: one unpartitioned table."""
        w, a = where(f)
        probe = ",".join(map(str, np.argsort(-(cent @ q))[:nprobe]))
        return [r[0] for r in db.execute(f"SELECT id FROM ivfm WHERE e MATCH ? AND k=10 AND c IN ({probe}){w}", (q.tobytes(), *a))]

    def exact_on_partitioned(q, f):
        w, a = where(f)
        return [r[0] for r in db.execute(f"SELECT id FROM ivf WHERE e MATCH ? AND k=10{w}", (q.tobytes(), *a))]

    def np_exact(q, f):
        m = (ext == f) if f else slice(None)
        idx = np.flatnonzero(m) if f else np.arange(n)
        return idx[np.argsort(-(x[idx] @ q))[:10]].tolist()

    strategies = {
        "exact float (sqlite-vec)": exact,
        "exact float (numpy)": np_exact,
        f"IVF nprobe 4 / {nlist}": lambda q, f: ivf(q, f, 4),
        f"IVF nprobe 8 / {nlist}": ivf,
        f"IVF nprobe 16 / {nlist}": lambda q, f: ivf(q, f, 16),
        f"IVF nprobe 32 / {nlist}": lambda q, f: ivf(q, f, 32),
        "exact scan of partitioned table": exact_on_partitioned,
        f"IVF as metadata col, nprobe 8": ivf_meta,
        f"IVF as metadata col, nprobe 16": lambda q, f: ivf_meta(q, f, 16),
    }
    truth = [exact(q, f) for q, f in zip(qs, filt)]
    out = []
    for name, fn in strategies.items():
        times, rec = [], []
        for q, f, t in zip(qs, filt, truth):
            s = time.perf_counter()
            got = fn(q, f)
            times.append((time.perf_counter() - s) * 1000)
            rec.append(len(set(got) & set(t)) / max(1, len(t)))
        times.sort()
        out.append({"n": n, "strategy": name, "p50_ms": statistics.median(times), "p95_ms": times[int(0.95 * len(times)) - 1], "recall@10": float(np.mean(rec))})
    print(f"\n{n:,} chunks  (build {build:.0f}s, mean pairwise cosine {float((x[:500] @ x[:500].T).mean()):.2f})")
    print(f"  {'strategy':26} {'p50':>8} {'p95':>8} {'recall@10':>10}")
    for r in out:
        print(f"  {r['strategy']:26} {r['p50_ms']:7.1f}ms {r['p95_ms']:7.1f}ms {r['recall@10']:9.3f}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="+", default=[20000, 100000])
    ap.add_argument("--queries", type=int, default=60)
    a = ap.parse_args()
    for n in a.sizes:
        run(n, a.queries)
