import os

import pytest

from redtooth.files.index import FileIndex
from redtooth.intent import parse
from redtooth.samples import make_samples


@pytest.mark.parametrize("backend", ["sqlite-vec", "numpy"])
def test_filtered_match_is_not_crowded_out(tmp_path, monkeypatch, backend):
    if backend == "numpy":
        monkeypatch.setenv("REDTOOTH_NO_SQLITE_VEC", "1")
    make_samples(tmp_path)
    notes = tmp_path / "Notes"
    notes.mkdir()
    for i in range(400):  # many near-duplicate notes that out-rank the deck when unfiltered
        (notes / f"n{i}.md").write_text(("revenue pipeline hiring " * 40 + "\n\n") * 3)
    ix = FileIndex(tmp_path / "ix.db")
    if backend == "sqlite-vec" and not ix.sqlite_vec:
        pytest.skip("sqlite-vec unavailable")
    ix.scan([tmp_path])
    hits = ix.search(parse("find the deck about revenue pipeline hiring").file)
    assert hits and hits[0].name == "Q4 Board Update.pptx"


def test_backends_rank_the_same(tmp_path, monkeypatch):
    make_samples(tmp_path)
    a = FileIndex(tmp_path / "a.db")
    if not a.sqlite_vec:
        pytest.skip("sqlite-vec unavailable")
    a.scan([tmp_path])
    monkeypatch.setenv("REDTOOTH_NO_SQLITE_VEC", "1")
    b = FileIndex(tmp_path / "b.db")
    b.scan([tmp_path])
    for q in ["Find the deck about customer churn", "send the PRJ-2041 checklist", "find the retention plan", "find the offsite agenda"]:
        fq = parse(q).file
        assert [h.name for h in a.search(fq)] == [h.name for h in b.search(fq)], q


def _library(tmp_path, notes=300):
    make_samples(tmp_path)
    d = tmp_path / "Notes"
    d.mkdir()
    topics = ["vendor contract renewal", "security audit findings", "office move logistics", "quarterly forecast", "hiring loop feedback", "launch retro"]
    for i in range(notes):
        t = topics[i % len(topics)]
        (d / f"note-{i:03d}.md").write_text(f"# {t} {i}\n\n" + (f"{t} details and follow ups for item {i}. " * 30 + "\n\n") * 3)
    return tmp_path


QUERIES = [
    "Find the deck about customer churn",
    "send the PRJ-2041 checklist",
    "find the deck about revenue pipeline hiring",
    "find notes about the security audit",
    "find the doc about vendor contract renewal",
    "find the offsite agenda",
]


def test_ivf_matches_exact(tmp_path, monkeypatch):
    import redtooth.files.vecindex as vecindex

    lib = _library(tmp_path)
    exact = FileIndex(tmp_path / "exact.db")
    exact.scan([lib])
    assert not exact.ivf_status()["ivf"]
    monkeypatch.setattr(vecindex, "IVF_MIN", 500)
    ivf = FileIndex(tmp_path / "ivf.db")
    ivf.scan([lib])
    st = ivf.ivf_status()
    assert st["ivf"] and st["clusters"] >= 16 and st["tuning"]["recall_by_nprobe"][st["nprobe"]] >= 0.95, st
    for q in QUERIES:  # the test notes are near-duplicates, so allow ties below the top result
        fq = parse(q).file
        a, b = [h.name for h in ivf.search(fq)][:3], [h.name for h in exact.search(fq)][:3]
        assert a[:1] == b[:1] and len(set(a) & set(b)) >= min(2, len(b)), (q, a, b)


def test_ivf_keeps_up_with_changes(tmp_path, monkeypatch):
    import redtooth.files.vecindex as vecindex

    lib = _library(tmp_path)
    monkeypatch.setattr(vecindex, "IVF_MIN", 500)
    ix = FileIndex(tmp_path / "ivf.db")
    ix.scan([lib])
    assert ix.ivf_status()["ivf"]
    new = lib / "Notes" / "Kubernetes upgrade runbook.md"
    new.write_text("# Kubernetes upgrade runbook\n\nDrain nodes, upgrade the control plane, then the workers.")
    ix.index_file(new)
    assert ix.search(parse("find the kubernetes upgrade runbook").file)[0].name == new.name
    assert ix.ivf_status()["pending_changes"] > 0  # served from the in-memory delta until the next build
    new.unlink()
    ix.scan([lib])
    assert all(h.name != new.name for h in ix.search(parse("find the kubernetes upgrade runbook").file))
    # A rebuild folds everything in and keeps it searchable; reopening loads the saved index.
    assert ix.rebuild_vectors()
    assert ix.ivf_status()["pending_changes"] == 0
    again = FileIndex(tmp_path / "ivf.db")
    assert again.ivf_status()["ivf"]
    assert again.search(parse("Find the deck about customer churn").file)[0].name == "Retention Plan.pptx"


def test_embedder_input_is_cleaned():
    """Windows file names can carry lone surrogates; the tokenizer rejects them, so they're replaced first."""
    from redtooth.files.embed import _clean

    assert _clean("caf\udce9 deck") == "caf? deck" and _clean(None) == "" and _clean(3) == "3"
