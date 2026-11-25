"""Background indexing for a whole device.

One pass:
  1. Discover files in the configured folders with os.scandir (sizes and times come
     with the directory listing), skipping system/build/cache folders and types we
     can't search. Folders we aren't allowed to read are reported, not silently empty.
  2. Compare against the index in one query; only new or changed files are queued.
  3. Index the queue newest first: files edited in the last RECENT_DAYS before
     anything else, then the rest. Text extraction runs in low-priority worker
     processes; embeddings are batched across files; writes are committed in batches.
     The vector index is rebuilt after the recent files and every REBUILD_EVERY
     chunks, so search keeps up while a first run is in progress.
  4. Remove an indexed file only when it is proven gone (it no longer exists, or it's
     now excluded) - never just because a scan didn't reach it.

There is no small file cap. MAX_FILES (1,000,000) is a runaway guard: past it the
most recently edited files are kept and the status says how many were left out.

After the first pass the service watches the folders for changes (watchdog: FSEvents
on macOS, inotify on Linux, ReadDirectoryChangesW on Windows) with a full safety
pass every SAFETY_RESCAN seconds; without watchdog it rescans every POLL seconds.
While a first run is incomplete, `prefetch(query)` asks the OS's own index
(Spotlight / Windows Search) for likely matches and indexes those immediately.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import queue
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .extract import KIND_BY_EXT, extract_limited

log = logging.getLogger("redtooth.indexer")


def _safe(t) -> str:
    t = t if isinstance(t, str) else ("" if t is None else str(t))
    return t.encode("utf-8", "replace").decode("utf-8")

MAX_FILES = int(os.environ.get("REDTOOTH_MAX_FILES", 1_000_000))
RECENT_DAYS = 14
REBUILD_EVERY = 25_000  # chunks between vector-index rebuilds during a long pass
BATCH_FILES = 32
SAFETY_RESCAN = 30 * 60
POLL = 120
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".Trash", "Library", "AppData", ".cache", ".venv", "venv", ".redtooth",
             "$RECYCLE.BIN", "System Volume Information", "Pods", "DerivedData", "build", "dist", "target", ".gradle", ".idea"}


@dataclass
class IndexStatus:
    phase: str = "idle"  # discovering | indexing | watching | idle
    roots: list[str] = field(default_factory=list)
    in_scope: int = 0  # searchable files found in the folders
    queued: int = 0  # new or changed files this pass
    done: int = 0
    recent_done: bool = False  # files edited in the last RECENT_DAYS are indexed
    complete: bool = False  # a full pass has finished at least once
    name_only: int = 0  # too large to read; indexed by name
    capped: int = 0  # left out by the runaway guard
    errors: int = 0
    blocked: list[str] = field(default_factory=list)  # folders we can't read
    on_battery: bool = False
    watching: bool = False
    started: float = 0.0
    finished: float = 0.0
    rate_fps: float = 0.0  # files per second this pass

    @property
    def label(self) -> str:
        if self.phase == "discovering":
            return "Looking for files"
        if self.phase == "indexing" and self.queued:
            return f"Indexing · {self.done:,} of {self.queued:,} files"
        if self.blocked:
            return f"Can't read {len(self.blocked)} folder{'s' if len(self.blocked) > 1 else ''}"
        return f"{self.in_scope:,} files indexed" if self.complete else "Not indexed yet"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["label"] = self.label
        d["blocked"] = self.blocked[:10]
        return d


def _lower_priority() -> None:
    """Worker-process initializer: run below normal priority."""
    try:
        if sys.platform.startswith("win"):
            import ctypes

            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL
        else:
            os.nice(10)
    except Exception:
        pass


def _extract_job(job: tuple[str, int]) -> tuple[str, list, Optional[str]]:
    path, size = job
    try:
        return path, extract_limited(path, size), None
    except Exception as e:  # extract() already swallows most errors
        return path, [], str(e)


def on_battery() -> bool:
    try:
        import psutil  # type: ignore

        b = psutil.sensors_battery()
        return bool(b and not b.power_plugged)
    except Exception:
        pass
    if sys.platform.startswith("linux"):
        try:
            sup = Path("/sys/class/power_supply")
            mains = [p for p in sup.iterdir() if (p / "type").read_text().strip() == "Mains"]
            return bool(mains) and all((p / "online").read_text().strip() == "0" for p in mains)
        except Exception:
            return False
    return False


class Indexer:
    def __init__(
        self,
        index,
        roots: Iterable[str | Path],
        workers: Optional[int] = None,
        on_status: Optional[Callable[[dict], None]] = None,
        max_files: int = MAX_FILES,
        recent_days: float = RECENT_DAYS,
        rebuild_every: int = REBUILD_EVERY,
        os_search: Optional[Callable] = None,
    ):
        self.index = index
        self.roots = [str(Path(r).expanduser().resolve()) for r in roots]
        self.workers = max(1, min(4, (os.cpu_count() or 2) - 1)) if workers is None else workers
        self.on_status = on_status or (lambda s: None)
        self.max_files = max_files
        self.recent_days = recent_days
        self.rebuild_every = rebuild_every
        if os_search is None:
            from .os_search import candidates as os_search
        self.os_search = os_search
        self.status = IndexStatus(roots=self.roots)
        self.events: "queue.Queue[str]" = queue.Queue()  # paths reported by the watcher
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._observer = None
        self._last_emit = 0.0
        self._chunks_since_rebuild = 0
        self.order: list[str] = []  # indexing order of the last pass (for tests and diagnostics)

    # ------------------------------------------------------------ status
    def _emit(self, force: bool = False) -> None:
        now = time.time()
        if force or now - self._last_emit > 0.5:
            self._last_emit = now
            try:
                self.on_status(self.status.to_dict())
            except Exception:
                pass

    # ------------------------------------------------------------ discovery
    def in_scope(self, path: str) -> bool:
        p = Path(path)
        if p.suffix.lower() not in KIND_BY_EXT or p.name.startswith("."):
            return False
        if not any(path == r or path.startswith(r.rstrip(os.sep) + os.sep) for r in self.roots):
            return False
        for r in self.roots:
            if path.startswith(r.rstrip(os.sep) + os.sep):
                rel = Path(path[len(r.rstrip(os.sep)) + 1 :]).parts[:-1]
                if any(part in SKIP_DIRS or part.startswith(".") for part in rel):
                    return False
        return True

    def discover(self) -> list[tuple[str, int, int, float]]:
        """[(path, size, mtime_ns, mtime)] for every searchable file under the roots."""
        found: list[tuple[str, int, int, float]] = []
        blocked: list[str] = []
        stack = list(self.roots)
        while stack and not self._stop.is_set():
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        name = e.name
                        if name.startswith("."):
                            continue
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if name not in SKIP_DIRS:
                                    stack.append(e.path)
                            elif e.is_file(follow_symlinks=False) and os.path.splitext(name)[1].lower() in KIND_BY_EXT:
                                st = e.stat(follow_symlinks=False)
                                found.append((e.path, st.st_size, st.st_mtime_ns, st.st_mtime))
                        except OSError:
                            continue
            except PermissionError:
                blocked.append(d)
            except OSError:
                continue
        self.status.blocked = blocked
        return found

    # ------------------------------------------------------------ one pass
    def run_once(self) -> dict:
        t0 = time.time()
        st = self.status
        st.phase, st.started, st.done, st.queued, st.name_only, st.errors = "discovering", t0, 0, 0, 0, 0
        self._emit(force=True)
        files = self.discover()
        if len(files) > self.max_files:  # runaway guard: keep the most recently edited
            files.sort(key=lambda f: -f[3])
            st.capped = len(files) - self.max_files
            files = files[: self.max_files]
        else:
            st.capped = 0
        st.in_scope = len(files)
        known = self.index.signatures()
        todo = [f for f in files if known.get(f[0]) != f"{f[1]}:{f[2]}"]
        todo.sort(key=lambda f: -f[3])  # newest first
        cutoff = time.time() - self.recent_days * 86400
        recent = [f for f in todo if f[3] >= cutoff]
        rest = [f for f in todo if f[3] < cutoff]
        st.queued = len(todo)
        st.phase = "indexing"
        st.recent_done = not recent
        self.order = []
        self._emit(force=True)

        rebuilt = False
        changed = self._process(recent)
        if recent:
            rebuilt |= self._rebuild()  # recent files become fully searchable first
        st.recent_done = True
        self._emit(force=True)
        changed += self._process(rest)

        # Remove only what is proven gone: indexed under our roots, not seen, and missing or excluded now.
        seen = {f[0] for f in files}
        removed = 0
        for path in known:
            if path in seen or not any(path.startswith(r.rstrip(os.sep) + os.sep) for r in self.roots):
                continue
            if not os.path.exists(path) or not self.in_scope(path):
                if any(path.startswith(b.rstrip(os.sep) + os.sep) for b in st.blocked):
                    continue  # can't see that folder right now; keep what we had
                removed += self.index.delete_path(path, commit=False)
        self.index.commit()
        changed += removed
        if self.index.flat is None or self.index.pending_changes() > max(500, 0.02 * max(1, self.index.flat.n)):
            rebuilt |= self._rebuild()
        st.phase, st.complete, st.finished = ("watching" if st.watching else "idle"), True, time.time()
        el = st.finished - t0
        st.rate_fps = round(len(todo) / el, 1) if el > 0 and todo else 0.0
        self._emit(force=True)
        return {"seen": len(files), "changed": changed, "removed": removed, "vectors_rebuilt": rebuilt, "capped": st.capped, "blocked": list(st.blocked), "name_only": st.name_only, "seconds": round(el, 2)}

    def _rebuild(self) -> bool:
        self._chunks_since_rebuild = 0
        try:
            return self.index.rebuild_vectors()
        except Exception as e:
            log.warning("vector rebuild failed: %s", e)
            return False

    def _process(self, files: list[tuple[str, int, int, float]]) -> int:
        """Extract (in worker processes when there's enough work), embed in batches, write in batches."""
        if not files:
            return 0
        n = 0
        pool = None
        if self.workers > 0 and len(files) >= 64:
            pool = ProcessPoolExecutor(max_workers=self.workers, mp_context=mp.get_context("spawn"), initializer=_lower_priority)
        try:
            batches = [files[i : i + BATCH_FILES] for i in range(0, len(files), BATCH_FILES)]

            def submit(batch):  # extraction for the next batch runs while this one is embedded
                return [pool.submit(_extract_job, (f[0], f[1])) for f in batch] if pool else None

            ahead = submit(batches[0]) if batches else None
            for bi, batch in enumerate(batches):
                if self._stop.is_set():
                    break
                current = ahead
                ahead = submit(batches[bi + 1]) if bi + 1 < len(batches) else None
                results = [fu.result() for fu in current] if current is not None else [_extract_job((f[0], f[1])) for f in batch]
                n += self._write_batch(batch, results)
                self._drain_events()
                self.status.on_battery = on_battery()
                if self.status.on_battery:
                    time.sleep(0.25)  # gentle on battery
        finally:
            if pool:
                pool.shutdown(cancel_futures=True)
        return n

    def _write_batch(self, batch, results) -> int:
        st = self.status
        items, metas = [], []
        for f, (path, chunks, err) in zip(batch, results):
            if err:
                st.errors += 1
            if not chunks and f[1] > 0 and KIND_BY_EXT.get(os.path.splitext(path)[1].lower()) not in ("image", "video", "audio"):
                from .extract import content_limit

                if f[1] > content_limit(path):
                    st.name_only += 1
            chunks = [(_safe(lbl), _safe(txt)) for lbl, txt in (chunks or [])]
            items.append((os.path.basename(path), chunks))
            metas.append(f)
        try:
            vecs = self.index.embed_many(items)
        except Exception as e:  # one bad file mustn't stop indexing: retry one by one, then name only
            log.warning("embedding a batch failed (%s); retrying file by file", e)
            vecs = []
            for i, (name, chunks) in enumerate(items):
                try:
                    vecs.append(self.index.embed_many([(name, chunks)])[0])
                except Exception:
                    st.errors += 1
                    items[i] = (name, [])
                    vecs.append(self.index.embed_many([(name.encode("ascii", "replace").decode(), [])])[0])
        for f, (name, chunks), v in zip(metas, items, vecs):
            self.index.write_file(f[0], f[1], f[3], f"{f[1]}:{f[2]}", chunks, v, commit=False)
            self.order.append(f[0])
            self._chunks_since_rebuild += len(chunks) + 1
        self.index.commit()
        st.done += len(batch)
        if self._chunks_since_rebuild >= self.rebuild_every:
            self._rebuild()
        self._emit()
        return len(batch)

    # ------------------------------------------------------------ live changes
    def _drain_events(self) -> int:
        paths = set()
        while True:
            try:
                paths.add(self.events.get_nowait())
            except queue.Empty:
                break
        n = 0
        for p in paths:
            try:
                if os.path.isfile(p) and self.in_scope(p):
                    n += self.index.index_file(p)
                elif not os.path.exists(p) or not self.in_scope(p):
                    n += self.index.delete_path(p)
            except Exception as e:
                log.warning("index %s: %s", p, e)
        if n and self.index.flat is not None and self.index.pending_changes() > max(500, 0.02 * self.index.flat.n):
            self._rebuild()
        return n

    def notify(self, path: str) -> None:
        """Queue a path the OS says changed."""
        self.events.put(str(path))

    def _start_watcher(self) -> bool:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            return False
        idx = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.is_directory:
                    return
                idx.notify(event.src_path)
                if getattr(event, "dest_path", None):
                    idx.notify(event.dest_path)

        obs = Observer()
        for r in self.roots:
            if os.path.isdir(r):
                obs.schedule(Handler(), r, recursive=True)
        obs.daemon = True
        obs.start()
        self._observer = obs
        return True

    # ------------------------------------------------------------ service
    def start(self, first_pass: bool = True) -> None:
        def loop():
            if sys.platform.startswith("linux"):
                try:  # Linux allows per-thread niceness; elsewhere only the extraction workers are lowered
                    os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
                except Exception:
                    pass
            self.status.watching = self._start_watcher()
            if first_pass:
                try:
                    self.run_once()
                except Exception:
                    log.exception("indexing pass failed; will retry")
            else:
                self.status.complete, self.status.recent_done = True, True
                self.status.in_scope = len(self.index.signatures())
            self.status.phase = "watching" if self.status.watching else "idle"
            self._emit(force=True)
            last_full = time.time()
            interval = SAFETY_RESCAN if self.status.watching else POLL
            while not self._stop.is_set():
                time.sleep(1.0)
                try:
                    self._drain_events()
                    if time.time() - last_full >= interval:
                        self.run_once()
                        last_full = time.time()
                except Exception:
                    log.exception("indexing error; continuing")
                    last_full = time.time()

        self._thread = threading.Thread(target=loop, name="redtooth-indexer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._observer:
            self._observer.stop()

    # ------------------------------------------------------------ OS index shortcut
    def prefetch(self, fq) -> int:
        """While a first run is incomplete, index the OS search index's best guesses for this request now."""
        if self.status.complete:
            return 0
        try:
            paths = self.os_search(fq, self.roots)
        except Exception:
            return 0
        n = 0
        for p in paths[:50]:
            if self.in_scope(p):
                try:
                    n += self.index.index_file(p)
                except Exception:
                    pass
        return n
