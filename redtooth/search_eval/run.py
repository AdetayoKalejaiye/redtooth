"""Score file search on a labeled test set, and calibrate its ranking parameters.

  redtooth eval                                  bundled corpus + 52 queries
  redtooth eval --calibrate                      grid-search ranking parameters
  redtooth eval --calibrate --save               ...and use them from now on
  redtooth eval --root ~/Documents --queries mine.json   your files, your queries

Query file format (JSON list):
  {"q": "the deck about lowering our AWS bill",
   "expect": ["Cloud Cost Reduction.pptx"],     # any of these is right; [] = should find nothing
   "category": "paraphrase"}
An expected entry matches a result's file name exactly, or any part of its path.

Metrics: hit@1 (right file first), hit@3, MRR (1/rank of the first right file).
Queries that should find nothing pass when search returns no results.
Calibration reports a 2-fold held-out score (tune on half the queries, score the
other half, both ways), because tuning and scoring on the same 52 queries flatters.
"""
from __future__ import annotations

import argparse
import itertools
import json
import statistics
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..files.embed import load_embedder
from ..files.index import FileIndex, calibration_path
from ..intent import parse

HERE = Path(__file__).parent
NAMES = ["daniel", "daniel ortiz", "priya", "priya shah"]


def load_queries(path: Optional[str | Path] = None) -> list[dict]:
    return json.loads(Path(path or HERE / "queries.json").read_text(encoding="utf-8"))


def _matches(hit, expect: list[str]) -> bool:
    return any(hit.name == e or e in hit.path for e in expect)


def evaluate(ix: FileIndex, queries: list[dict], params: Optional[dict] = None) -> list[dict]:
    saved = dict(ix.params)
    if params:
        ix.params.update(params)
    out = []
    try:
        for qd in queries:
            fq = parse(qd["q"], NAMES).file
            t = time.perf_counter()
            hits = ix.search(fq, limit=5) if fq is not None else []
            ms = (time.perf_counter() - t) * 1000
            names = [h.name for h in hits]
            if not qd["expect"]:
                ok = not hits
                rec = {"rank": None, "rr": 1.0 if ok else 0.0, "hit1": ok, "hit3": ok}
            else:
                rank = next((i + 1 for i, h in enumerate(hits) if _matches(h, qd["expect"])), None)
                rec = {"rank": rank, "rr": 1.0 / rank if rank else 0.0, "hit1": rank == 1, "hit3": bool(rank and rank <= 3)}
            out.append({**qd, **rec, "top": names[:3], "ms": round(ms, 2)})
    finally:
        ix.params = saved
    return out


def summarize(results: list[dict]) -> dict:
    cats: dict[str, list[dict]] = {}
    for r in results:
        cats.setdefault(r["category"], []).append(r)

    def agg(rs):
        return {
            "n": len(rs),
            "hit@1": round(sum(r["hit1"] for r in rs) / len(rs), 3),
            "hit@3": round(sum(r["hit3"] for r in rs) / len(rs), 3),
            "mrr": round(statistics.mean(r["rr"] for r in rs), 3),
        }

    answerable = [r for r in results if r["expect"]]
    return {
        "overall": agg(answerable) if answerable else None,
        "should_find_nothing": agg([r for r in results if not r["expect"]]) if any(not r["expect"] for r in results) else None,
        "by_category": {c: agg(rs) for c, rs in cats.items()},
        "objective": round(statistics.mean(r["rr"] for r in results), 4),
        "latency_ms_p50": round(statistics.median(r["ms"] for r in results), 2),
    }


def grid(ix: FileIndex) -> list[dict]:
    base = getattr(ix.embedder, "min_sim", 0.0)
    floors = sorted({round(min(0.99, max(0.0, base + d)), 3) for d in (-0.12, -0.08, -0.04, 0.0, 0.02, 0.04, 0.06, 0.08, 0.10, 0.14)})
    combos = itertools.product(floors, (0.03, 0.05, 0.08, 0.12, 1.0), (0.5, 1.0, 1.5, 2.0))
    return [{"min_sim": f, "band": b, "w_sem": w, "w_kw": 1.0} for f, b, w in combos]


def _score(ix, queries, params) -> float:
    return summarize(evaluate(ix, queries, params))["objective"]


def calibrate(ix: FileIndex, queries: list[dict]) -> dict:
    configs = grid(ix)
    t0 = time.time()
    full = sorted(((_score(ix, queries, p), i) for i, p in enumerate(configs)), reverse=True)
    best = configs[full[0][1]]
    # 2-fold held-out estimate: tune on one half, score on the other.
    a, b = queries[0::2], queries[1::2]
    held = []
    for train, test in ((a, b), (b, a)):
        p = max(configs, key=lambda c: _score(ix, train, c))
        held.append((_score(ix, test, p), _score(ix, test, None)))
    return {
        "params": best,
        "objective_default": _score(ix, queries, None),
        "objective_tuned": full[0][0],
        "heldout_tuned": round(statistics.mean(h[0] for h in held), 4),
        "heldout_default": round(statistics.mean(h[1] for h in held), 4),
        "configs_tried": len(configs),
        "seconds": round(time.time() - t0, 1),
    }


def save_calibration(ix: FileIndex, cal: dict, n_queries: int) -> Path:
    path = calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text())
    except Exception:
        data = {}
    data[ix.embedder.name] = {"params": cal["params"], "queries": n_queries, "heldout_tuned": cal["heldout_tuned"], "heldout_default": cal["heldout_default"], "saved": datetime.now().isoformat(timespec="seconds")}
    path.write_text(json.dumps(data, indent=2))
    return path


def print_report(title: str, summ: dict, results: list[dict], show_misses: bool = True) -> None:
    print(f"\n{title}")
    o = summ["overall"]
    if o:
        print(f"  overall ({o['n']} answerable)   hit@1 {o['hit@1']:.0%}   hit@3 {o['hit@3']:.0%}   MRR {o['mrr']:.2f}   median {summ['latency_ms_p50']} ms/query")
    if summ["should_find_nothing"]:
        print(f"  should find nothing       passed {summ['should_find_nothing']['hit@1']:.0%} of {summ['should_find_nothing']['n']}")
    print(f"  {'category':22} {'n':>3} {'hit@1':>6} {'hit@3':>6} {'MRR':>5}")
    for c, a in summ["by_category"].items():
        print(f"  {c:22} {a['n']:>3} {a['hit@1']:>6.0%} {a['hit@3']:>6.0%} {a['mrr']:>5.2f}")
    if show_misses:
        misses = [r for r in results if not r["hit1"]]
        if misses:
            print("  misses:")
            for r in misses:
                want = ", ".join(r["expect"]) or "(nothing)"
                print(f"    [{r['category']}] “{r['q']}” → got {r['top'][:2] or '(nothing)'}; wanted {want}")


def run(argv=None) -> dict:
    ap = argparse.ArgumentParser(prog="redtooth eval", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--embedder", default="auto", choices=["auto", "e5", "hash"])
    ap.add_argument("--queries", help="your own queries JSON (default: bundled set)")
    ap.add_argument("--root", help="folder to index instead of the bundled corpus")
    ap.add_argument("--calibrate", action="store_true", help="grid-search ranking parameters")
    ap.add_argument("--save", action="store_true", help="with --calibrate: use the tuned parameters from now on")
    ap.add_argument("--json", help="write full results here")
    a = ap.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="redtooth-eval-"))
    if a.root:
        root = Path(a.root).expanduser()
    else:
        from .corpus import build_corpus

        root = build_corpus(work / "corpus")
    emb = load_embedder(a.embedder)
    queries = load_queries(a.queries)
    t = time.time()
    ix = FileIndex(work / "eval.db", emb)
    ix.scan([root])
    print(f"Indexed {root} with {emb.name} in {time.time() - t:.1f}s · {len(queries)} queries")
    results = evaluate(ix, queries)
    summ = summarize(results)
    print_report("Current ranking parameters " + json.dumps({k: round(v, 3) for k, v in ix.params.items()}), summ, results)
    report = {"embedder": emb.name, "queries": len(queries), "summary": summ, "results": results}
    if a.calibrate:
        cal = calibrate(ix, queries)
        tuned = evaluate(ix, queries, cal["params"])
        print_report(f"Calibrated parameters {json.dumps(cal['params'])}", summarize(tuned), tuned, show_misses=False)
        print(f"\n  objective (MRR incl. nothing-queries): default {cal['objective_default']:.3f} → tuned {cal['objective_tuned']:.3f} on all queries")
        print(f"  held-out (tune on half, score the other half): default {cal['heldout_default']:.3f} → tuned {cal['heldout_tuned']:.3f}")
        print(f"  {cal['configs_tried']} configurations in {cal['seconds']}s")
        report["calibration"] = cal
        if a.save:
            if cal["heldout_tuned"] > cal["heldout_default"]:
                print(f"  saved to {save_calibration(ix, cal, len(queries))}")
            else:
                print("  not saved: tuning didn't beat the defaults on held-out queries")
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


if __name__ == "__main__":
    run()
