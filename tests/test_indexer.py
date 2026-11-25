import os
import time
from pathlib import Path

import pytest

from redtooth.files.index import FileIndex
from redtooth.files.indexer import Indexer
from redtooth.intent import parse


def touch(p: Path, text: str, days_ago: float) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    t = time.time() - days_ago * 86400
    os.utime(p, (t, t))
    return p


def names(ix: FileIndex) -> set[str]:
    return {r[0] for r in ix.db.execute("SELECT name FROM files")}


def test_no_silent_cap_keeps_newest_and_reports(tmp_path):
    for d in range(3):
        for i in range(10):
            touch(tmp_path / f"d{d}" / f"n{d}{i}.md", f"note {d} {i}", days_ago=d * 10 + i)
    ix = FileIndex()
    idx = Indexer(ix, [tmp_path], workers=0, max_files=10)
    r = idx.run_once()
    assert r["capped"] == 20 and idx.status.capped == 20
    assert names(ix) == {f"n0{i}.md" for i in range(10)}  # the 10 most recently edited
    # A file indexed some other way (share sheet) is not dropped by the next pass.
    ix.index_file(tmp_path / "d2" / "n29.md")
    idx.run_once()
    assert "n29.md" in names(ix)


def test_recent_files_first_newest_first(tmp_path):
    old = [touch(tmp_path / f"old{i}.md", "old", days_ago=100 + i) for i in range(5)]
    new = [touch(tmp_path / f"new{i}.md", "new", days_ago=i * 0.5) for i in range(5)]
    idx = Indexer(FileIndex(), [tmp_path], workers=0)
    idx.run_once()
    order = [Path(p).name for p in idx.order]
    assert order[:5] == [p.name for p in new]  # newest first, recent before old
    assert set(order[5:]) == {p.name for p in old}
    assert idx.status.recent_done and idx.status.complete


def test_deletes_only_what_is_proven_gone(tmp_path, monkeypatch):
    a = touch(tmp_path / "keep" / "a.md", "alpha", 1)
    b = touch(tmp_path / "keep" / "b.md", "beta", 1)
    c = touch(tmp_path / "locked" / "c.md", "gamma", 1)
    ix = FileIndex()
    idx = Indexer(ix, [tmp_path], workers=0)
    idx.run_once()
    assert names(ix) == {"a.md", "b.md", "c.md"}
    b.unlink()
    real = os.scandir

    def scandir(path):  # the OS now refuses to list one folder (e.g. a revoked macOS permission)
        if str(path).endswith("locked"):
            raise PermissionError(path)
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    idx.run_once()
    assert names(ix) == {"a.md", "c.md"}  # b is gone; c is unreadable right now but kept
    assert any(p.endswith("locked") for p in idx.status.blocked)
    assert "Can't read" in idx.status.label


def test_skips_system_folders_and_unknown_types(tmp_path):
    touch(tmp_path / "node_modules" / "pkg" / "readme.md", "dependency docs", 1)
    touch(tmp_path / ".hidden" / "x.md", "hidden", 1)
    touch(tmp_path / "app.exe", "binary", 1)
    touch(tmp_path / "plan.md", "the plan", 1)
    ix = FileIndex()
    Indexer(ix, [tmp_path], workers=0).run_once()
    assert names(ix) == {"plan.md"}


def test_large_files_are_indexed_by_name_only(tmp_path, monkeypatch):
    import redtooth.files.extract as ex

    monkeypatch.setattr(ex, "MAX_TEXT_BYTES", 1000)
    touch(tmp_path / "Server logs archive.txt", "zebrafish " * 500, 1)
    ix = FileIndex()
    idx = Indexer(ix, [tmp_path], workers=0)
    idx.run_once()
    assert idx.status.name_only == 1
    assert ix.search(parse("find the server logs archive").file)[0].name == "Server logs archive.txt"
    assert not ix.search(parse("find zebrafish").file)


def test_parallel_extraction_matches_inline(tmp_path):
    for i in range(80):
        touch(tmp_path / f"doc{i:02d}.md", f"# Doc {i}\n\ntopic number {i} about subject{i % 7}", days_ago=i)
    a, b = FileIndex(tmp_path / "a.db"), FileIndex(tmp_path / "b.db")
    Indexer(a, [tmp_path], workers=0).run_once()
    Indexer(b, [tmp_path], workers=2).run_once()
    assert a.signatures() == b.signatures() and len(a.signatures()) == 80
    q = parse("find the doc about subject3").file
    assert [h.name for h in a.search(q)] == [h.name for h in b.search(q)]


def test_watcher_picks_up_new_and_deleted_files(tmp_path):
    pytest.importorskip("watchdog")
    touch(tmp_path / "start.md", "start", 1)
    ix = FileIndex()
    idx = Indexer(ix, [tmp_path], workers=0)
    idx.start()
    try:
        for _ in range(100):
            if idx.status.complete:
                break
            time.sleep(0.05)
        assert idx.status.watching
        new = tmp_path / "Quarterly roadmap.md"
        new.write_text("roadmap for the mobile app")
        for _ in range(100):
            if "Quarterly roadmap.md" in names(ix):
                break
            time.sleep(0.1)
        assert ix.search(parse("find the quarterly roadmap").file)[0].name == "Quarterly roadmap.md"
        new.unlink()
        for _ in range(100):
            if "Quarterly roadmap.md" not in names(ix):
                break
            time.sleep(0.1)
        assert "Quarterly roadmap.md" not in names(ix)
    finally:
        idx.stop()


def test_os_search_shortcut_while_first_run_incomplete(tmp_path):
    deck = touch(tmp_path / "Board deck.md", "board meeting agenda", 0.1)
    asked = []

    def fake_os_search(fq, roots):
        asked.append(fq)
        return [str(deck)]

    ix = FileIndex()
    idx = Indexer(ix, [tmp_path], workers=0, os_search=fake_os_search)
    assert idx.prefetch(parse("find the board deck").file) == 1  # first run hasn't happened yet
    assert "Board deck.md" in names(ix)
    idx.run_once()
    assert idx.prefetch(parse("find the board deck").file) == 0 and len(asked) == 1  # not needed once complete


@pytest.mark.asyncio
async def test_agent_uses_shortcut_and_says_when_index_is_incomplete(tmp_path):
    from redtooth.demo import build_demo

    d = await build_demo(str(tmp_path), daniel_auto_accept=0.1, embedder="hash", radio_delay=0.01)
    try:
        idx = d.agent.indexer
        hidden = touch(tmp_path / "Documents" / "Vendor scorecard.md", "on-time delivery rates by supplier", 0.01)
        idx.stop()  # simulate a first run still in progress, watcher not catching it
        idx.status.complete, idx.status.queued, idx.status.done = False, 48000, 12400
        idx.os_search = lambda fq, roots: [str(hidden)]
        r = await d.agent.ask("find the vendor scorecard")
        assert r.files and r.files[0].name == "Vendor scorecard.md"
        assert "still indexing older files (12,400 of 48,000)" in r.text
        idx.status.complete = True
        r2 = await d.agent.ask("find the deck about the holiday party")
        assert "still indexing" not in r2.text
    finally:
        d.agent.indexer.stop()
