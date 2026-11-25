"""Hybrid local file search: SQLite FTS5 for exact names and project codes,
E5 embeddings for requests that describe a topic in different words, fused
with reciprocal-rank fusion and filtered by type and modification time.

Storage: one SQLite file holds files, chunks, the FTS5 index and the vectors
(a sqlite-vec `vec0` table; a plain BLOB table when the Python build can't load
SQLite extensions). Search: vectors are scored through a derived, memory-mapped
matrix (vecindex.FlatIndex) that is exact for small libraries and clustered (IVF,
self-tuned for recall) for large ones. Files indexed since the last build are
kept in a small in-memory delta, deletions in a tombstone set, until the next
rebuild, which takes seconds.
"""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from ..intent import FileQuery
from ..models import FileHit
from .embed import Embedder, HashEmbedder
from .extract import KIND_BY_EXT, extract_limited, kind_of
from .vecindex import FlatIndex

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL, ext TEXT, kind TEXT,
  size INTEGER, mtime REAL, sig TEXT, indexed_at REAL);
CREATE INDEX IF NOT EXISTS files_kind_mtime ON files(kind, mtime);
CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, label TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name, label, text, tokenize="unicode61 remove_diacritics 2", prefix='2 3');
CREATE TABLE IF NOT EXISTS vecs (chunk_id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, v BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""
VEC_K = 300  # nearest chunks to consider per query
VEC_SCHEMA = "3"  # bump when the vector table layout changes
REBUILD_DELTA = 0.02  # rebuild the speed-up index when >2% of rows changed since the last build
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".Trash", "Library", "AppData", ".cache", ".venv", "venv", ".redtooth"}
RRF_K = 60


def name_words(name: str) -> str:
    stem = Path(name).stem
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem)
    words = re.sub(r"[_.]+", " ", spaced)
    return f"{stem} {words}"


def _load_sqlite_vec(db: sqlite3.Connection) -> bool:
    if os.environ.get("REDTOOTH_NO_SQLITE_VEC"):
        return False
    try:
        import sqlite_vec

        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        return True
    except Exception:  # module missing, or sqlite3 built without extension loading
        return False


def calibration_path() -> Path:
    return Path(os.environ.get("REDTOOTH_HOME", Path.home() / ".redtooth")) / "search_calibration.json"


def load_calibration(embedder_name: str) -> dict:
    try:
        import json

        data = json.loads(calibration_path().read_text())
        return {k: float(v) for k, v in data.get(embedder_name, {}).get("params", {}).items()}
    except Exception:
        return {}


def default_roots() -> list[Path]:
    home = Path.home()
    return [p for p in (home / "Documents", home / "Desktop", home / "Downloads") if p.exists()]


STOP = {"the", "and", "for", "about", "with", "from", "that", "this", "file", "doc", "document", "deck", "my", "our", "your"}


def _safe(t) -> str:
    t = t if isinstance(t, str) else ("" if t is None else str(t))
    return t.encode("utf-8", "replace").decode("utf-8")


class FileIndex:
    def __init__(self, db_path: str | Path = ":memory:", embedder: Optional[Embedder] = None):
        self.embedder = embedder or HashEmbedder()
        self.db_path = str(db_path)
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.lock = threading.RLock()
        self.sqlite_vec = _load_sqlite_vec(self.db)
        self.vector_backend = "sqlite-vec" if self.sqlite_vec else "sqlite blobs"
        with self.lock:
            self.db.executescript(SCHEMA)
            meta = dict(self.db.execute("SELECT k, v FROM meta").fetchall())
            stale = (
                meta.get("embedder") not in (None, self.embedder.name)
                or meta.get("vector_backend") not in (None, self.vector_backend)
                or meta.get("vec_schema") not in (None, VEC_SCHEMA)
            )
            if stale:  # model, backend or layout changed: re-embed everything on the next scan
                self.db.execute("DELETE FROM vecs")
                for t in ("vec_chunks", "vec_chunks_b"):
                    self.db.execute(f"DROP TABLE IF EXISTS {t}")
                self.db.execute("UPDATE files SET sig=NULL")
            if self.sqlite_vec:
                self.db.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0("
                    f"chunk_id INTEGER PRIMARY KEY, embedding float[{self.embedder.dim}] distance_metric=cosine, "
                    f"ext TEXT, mtime FLOAT, +file_id INTEGER)"
                )
            for k, v in (("embedder", self.embedder.name), ("vector_backend", self.vector_backend), ("vec_schema", VEC_SCHEMA)):
                self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))
            self.db.commit()
            if self.db_path != ":memory:":
                self.db.execute("PRAGMA journal_mode=WAL")
        # Ranking parameters (tuned by `redtooth eval --calibrate --save`).
        self.params = {
            "min_sim": getattr(self.embedder, "min_sim", 0.0),  # meaning matches below this don't count
            "band": getattr(self.embedder, "band", 0.08),  # ...nor ones far below the best match
            "w_kw": 1.0,  # weight of the keyword ranking in the fusion
            "w_sem": 1.0,  # weight of the meaning ranking
            "rel_cut": 0.45,  # drop results scoring under this fraction of the best
        }
        self.params.update(load_calibration(self.embedder.name))
        self._qcache: dict[str, np.ndarray] = {}
        # Speed-up index: derived from the database, kept next to it.
        self.vec_dir = Path(tempfile.mkdtemp(prefix="redtooth-vec-")) if self.db_path == ":memory:" else Path(self.db_path).with_suffix(".vec")
        self.delta: dict[int, tuple[np.ndarray, int, str, float]] = {}  # chunk_id -> (vector, file_id, ext, mtime)
        self.tombstones: set[int] = set()
        self._nvec: Optional[int] = None
        self._delta_mat = None  # cached (ids, file ids, exts, mtimes, matrix) for the delta
        self.flat: Optional[FlatIndex] = FlatIndex.load(self.vec_dir, self.embedder.dim, self.embedder.name)
        if self.flat is not None and not self._flat_current():
            self.flat = None
        if self.flat is None and self.vector_count():
            self.rebuild_vectors()

    # ------------------------------------------------------------ indexing
    def signature(self, path: str) -> Optional[str]:
        with self.lock:
            row = self.db.execute("SELECT sig FROM files WHERE path=?", (path,)).fetchone()
        return row[0] if row else None

    def signatures(self) -> dict[str, str]:
        """path -> size:mtime signature for every indexed file (one query, for bulk change detection)."""
        with self.lock:
            return dict(self.db.execute("SELECT path, sig FROM files").fetchall())

    def embed_many(self, items: list[tuple[str, list[tuple[str, str]]]]) -> list[np.ndarray]:
        """Embed several files' chunks in one batch. items: [(file name, chunks)]."""
        texts, spans = [], []
        for name, chunks in items:
            nw = name_words(name)
            start = len(texts)
            texts += [nw] + [f"{nw}. {lbl}. {txt}" for lbl, txt in chunks]
            spans.append((start, len(texts)))
        vecs = self.embedder.embed(texts, "passage").astype(np.float32) if texts else np.zeros((0, self.embedder.dim), np.float32)
        return [vecs[a:b] for a, b in spans]

    def write_file(self, path: str | Path, size: int, mtime: float, sig: str, chunks: list[tuple[str, str]], vecs: np.ndarray, commit: bool = True) -> None:
        """Store one file's chunks, keyword index and vectors (replacing any previous version)."""
        p = Path(path)
        name, ext = p.name, p.suffix.lower()
        with self.lock:
            row = self.db.execute("SELECT id FROM files WHERE path=?", (str(p),)).fetchone()
            if row:
                self._delete(row[0])
            cur = self.db.execute(
                "INSERT INTO files(path,name,ext,kind,size,mtime,sig,indexed_at) VALUES(?,?,?,?,?,?,?,?)",
                (str(p), _safe(name), ext, kind_of(p), size, mtime, sig, time.time()),
            )
            fid = cur.lastrowid
            nw = _safe(name_words(name))
            for i, (lbl, txt) in enumerate([("File name", "")] + chunks):
                lbl, txt = _safe(lbl), _safe(txt)  # PDFs and old documents can contain lone surrogates SQLite won't store
                cid = self.db.execute("INSERT INTO chunks(file_id,label,text) VALUES(?,?,?)", (fid, lbl, txt)).lastrowid
                self.db.execute("INSERT INTO fts(rowid,name,label,text) VALUES(?,?,?,?)", (cid, nw, lbl, txt))
                blob = vecs[i].tobytes()
                if self.sqlite_vec:
                    self.db.execute("INSERT INTO vec_chunks(chunk_id,embedding,ext,mtime,file_id) VALUES(?,?,?,?,?)", (cid, blob, ext, float(mtime), fid))
                else:
                    self.db.execute("INSERT INTO vecs(chunk_id,file_id,v) VALUES(?,?,?)", (cid, fid, blob))
                self.delta[cid] = (vecs[i], fid, ext, float(mtime))
            self._delta_mat = None
            if self._nvec is not None:
                self._nvec += len(chunks) + 1
            if commit:
                self.db.commit()

    def commit(self) -> None:
        with self.lock:
            self.db.commit()

    def index_file(self, path: str | Path, force: bool = False) -> bool:
        """Index one file now (share sheet, file watcher). Returns False if unchanged or unreadable."""
        p = Path(path).resolve()
        try:
            st = p.stat()
        except OSError:
            return False
        sig = f"{st.st_size}:{st.st_mtime_ns}"
        if not force and self.signature(str(p)) == sig:
            return False
        chunks = extract_limited(p, st.st_size)
        (vecs,) = self.embed_many([(p.name, chunks)])
        self.write_file(p, st.st_size, st.st_mtime, sig, chunks, vecs)
        return True

    def delete_path(self, path: str, commit: bool = True) -> bool:
        with self.lock:
            row = self.db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
            if not row:
                return False
            self._delete(row[0])
            if commit:
                self.db.commit()
        return True

    def _delete(self, fid: int) -> None:
        ids = [r[0] for r in self.db.execute("SELECT id FROM chunks WHERE file_id=?", (fid,))]
        if self._nvec is not None:
            self._nvec -= len(ids)
        for i in ids:
            if self.delta.pop(i, None) is None:
                self.tombstones.add(i)
        self._delta_mat = None
        self.db.executemany("DELETE FROM fts WHERE rowid=?", [(i,) for i in ids])
        if self.sqlite_vec:
            self.db.executemany("DELETE FROM vec_chunks WHERE chunk_id=?", [(i,) for i in ids])
        self.db.execute("DELETE FROM vecs WHERE file_id=?", (fid,))
        self.db.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
        self.db.execute("DELETE FROM files WHERE id=?", (fid,))

    def pending_changes(self) -> int:
        return len(self.delta) + len(self.tombstones)

    def scan(self, roots: Iterable[str | Path], max_files: Optional[int] = None) -> dict:
        """One synchronous indexing pass (see files/indexer.py for the background service)."""
        from .indexer import MAX_FILES, Indexer

        return Indexer(self, list(roots), workers=0, max_files=max_files or MAX_FILES).run_once()

    # ------------------------------------------------------------ speed-up index
    def vector_count(self) -> int:
        with self.lock:
            if self._nvec is None:
                self._nvec = self.db.execute("SELECT count(*) FROM chunks").fetchone()[0]
            return self._nvec

    def _flat_current(self) -> bool:
        with self.lock:
            n, mx = self.db.execute("SELECT count(*), coalesce(max(id), 0) FROM chunks").fetchone()
        return self.flat is not None and self.flat.n == n and self.flat.meta.get("max_chunk_id") == mx

    def _blocks(self, conn: sqlite3.Connection):
        if self.sqlite_vec:
            return lambda: self._iter_vectors(conn, "vec_chunks")

        def it():
            cur = conn.execute("SELECT chunk_id, v FROM vecs")
            while rows := cur.fetchmany(4096):
                yield np.array([r[0] for r in rows]), np.vstack([np.frombuffer(r[1], np.float32) for r in rows])

        return it

    def _tuning_queries(self, conn: sqlite3.Connection):
        """Realistic queries for tuning: a few words from random chunks, embedded as queries."""

        def make(nq: int) -> list[np.ndarray]:
            rng = np.random.default_rng(1)
            texts = [r[0] for r in conn.execute("SELECT text FROM chunks WHERE length(text) > 80 ORDER BY random() LIMIT ?", (nq,))]
            out = []
            for t in texts:
                words = re.findall(r"\w{3,}", t)
                if len(words) >= 3:
                    pick = [words[i] for i in sorted(rng.choice(len(words), min(4, len(words)), replace=False))]
                    out.append(self.embedder.embed([" ".join(pick)], "query")[0].astype(np.float32))
            return out

        return make

    def rebuild_vectors(self) -> bool:
        """Rebuild the speed-up index from the database (seconds per 100k chunks).
        Reads through its own connection so searches keep running meanwhile."""
        if self.db_path == ":memory:":
            conn = self.db
        else:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            if self.sqlite_vec:
                _load_sqlite_vec(conn)
        try:
            with self.lock:
                row_meta = {r[0]: (r[1], r[2], float(r[3] or 0)) for r in conn.execute("SELECT c.id, c.file_id, f.ext, f.mtime FROM chunks c JOIN files f ON f.id = c.file_id")}
                mx = max(row_meta, default=0)
            flat = FlatIndex.build(self.vec_dir, self.embedder.dim, self.embedder.name, self._blocks(conn), row_meta, mx, self._tuning_queries(conn))
        finally:
            if conn is not self.db:
                conn.close()
        with self.lock:
            built = set(row_meta)
            self.flat = flat
            # Keep only changes that happened after the snapshot was read.
            self.delta = {k: v for k, v in self.delta.items() if k not in built}
            # A tombstone still matters only if the build contains that row (it was deleted after the snapshot).
            self.tombstones = {t for t in self.tombstones if t in built}
            self._nvec = None
        return True

    def ivf_status(self) -> dict:
        st = self.flat.status() if self.flat else {"vectors": 0, "ivf": False}
        st.update(pending_changes=len(self.delta) + len(self.tombstones), storage=self.vector_backend)
        return st

    def _hit(self, row) -> FileHit:
        fid, path, name, ext, kind, size, mtime = row
        return FileHit(fid, path, name, kind, ext, size, mtime)

    def get(self, path: str | Path) -> Optional[FileHit]:
        p = str(Path(path).resolve())
        self.index_file(p)
        with self.lock:
            row = self.db.execute("SELECT id,path,name,ext,kind,size,mtime FROM files WHERE path=?", (p,)).fetchone()
        return self._hit(row) if row else None

    def get_id(self, fid: int) -> Optional[FileHit]:
        with self.lock:
            row = self.db.execute("SELECT id,path,name,ext,kind,size,mtime FROM files WHERE id=?", (fid,)).fetchone()
        return self._hit(row) if row else None

    def recent(self, limit: int = 8) -> list[FileHit]:
        with self.lock:
            rows = self.db.execute("SELECT id,path,name,ext,kind,size,mtime FROM files ORDER BY mtime DESC LIMIT ?", (limit,)).fetchall()
        return [self._hit(r) for r in rows]

    def _iter_vectors(self, conn, table: str):
        """Yield (chunk_ids, vectors) in blocks. Reads sqlite-vec's block storage directly
        (vec0 0.1.x layout: <t>_rowids maps id -> block/offset, <t>_vector_chunks00 holds
        blocks of vectors), checked against a normal read; falls back to SELECT otherwise."""
        dim = self.embedder.dim
        try:
            layout = {r[0]: r[1] for r in conn.execute(f"SELECT rowid, vectors FROM {table}_vector_chunks00 LIMIT 0")}
            by_block: dict[int, list[tuple[int, int]]] = {}
            for rid, block, off in conn.execute(f"SELECT rowid, chunk_id, chunk_offset FROM {table}_rowids WHERE chunk_id IS NOT NULL"):
                by_block.setdefault(block, []).append((rid, off))
            # Spot-check the layout against vec0 itself before trusting it.
            probe = next(iter(by_block.items()), None)
            if probe:
                block, members = probe
                blob = conn.execute(f"SELECT vectors FROM {table}_vector_chunks00 WHERE rowid=?", (block,)).fetchone()[0]
                arr = np.frombuffer(blob, np.float32).reshape(-1, dim)
                rid, off = members[0]
                ref = conn.execute(f"SELECT embedding FROM {table} WHERE chunk_id=?", (rid,)).fetchone()[0]
                if not np.array_equal(arr[off], np.frombuffer(ref, np.float32)):
                    raise ValueError("unexpected sqlite-vec layout")
            del layout
        except Exception:
            cur = conn.execute(f"SELECT chunk_id, embedding FROM {table}")
            while rows := cur.fetchmany(4096):
                yield np.array([r[0] for r in rows]), np.vstack([np.frombuffer(r[1], np.float32) for r in rows])
            return
        for block, members in by_block.items():
            blob = conn.execute(f"SELECT vectors FROM {table}_vector_chunks00 WHERE rowid=?", (block,)).fetchone()[0]
            arr = np.frombuffer(blob, np.float32).reshape(-1, dim)
            yield np.array([m[0] for m in members]), arr[[m[1] for m in members]]

    def _nearest(self, qv: np.ndarray, q: FileQuery) -> list[tuple[int, int, float]]:
        """(chunk_id, file_id, cosine similarity), best first, filtered by type and time."""
        exts = set(q.exts) if q.exts else None
        with self.lock:
            flat, tomb, delta = self.flat, set(self.tombstones), list(self.delta.items())
        if flat is not None:
            res = [r for r in flat.search(qv, VEC_K + len(tomb), exts, q.since, q.until) if r[0] not in tomb]
        else:
            res = self._nearest_sql(qv, exts, q.since, q.until)
        if delta:  # indexed since the last build: one matrix product, filtered by masks
            ids, fids, exs, mts, mat = self._delta_arrays(delta)
            keep = np.ones(len(ids), bool)
            if exts is not None:
                keep &= np.isin(exs, list(exts))
            if q.since is not None:
                keep &= mts >= q.since
            if q.until is not None:
                keep &= mts < q.until
            if keep.any():
                sims = mat[keep] @ qv
                res += [(int(c), int(f), float(x)) for c, f, x in zip(ids[keep], fids[keep], sims)]
        res.sort(key=lambda r: -r[2])
        return res[:VEC_K]

    def _delta_arrays(self, delta: list):
        cached = self._delta_mat
        if cached is not None and cached[0] == len(delta):
            return cached[1]
        arrs = (
            np.array([c for c, _ in delta], np.int64),
            np.array([v[1] for _, v in delta], np.int64),
            np.array([v[2] for _, v in delta]),
            np.array([v[3] for _, v in delta], np.float64),
            np.vstack([v[0] for _, v in delta]).astype(np.float32),
        )
        self._delta_mat = (len(delta), arrs)
        return arrs

    def _nearest_sql(self, qv: np.ndarray, exts, since, until) -> list[tuple[int, int, float]]:
        """Fallback when the speed-up index isn't available: sqlite-vec's own exact search."""
        if not self.sqlite_vec:
            return []
        where, args = [], []
        if exts:
            where.append(f"ext IN ({','.join('?' * len(exts))})")
            args += sorted(exts)
        if since is not None:
            where.append("mtime >= ?")
            args.append(since)
        if until is not None:
            where.append("mtime < ?")
            args.append(until)
        vw = "".join(" AND " + w for w in where)
        with self.lock:
            rows = self.db.execute(f"SELECT chunk_id, file_id, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ?{vw} ORDER BY distance", (qv.tobytes(), VEC_K, *args)).fetchall()
        return [(int(c), int(f), 1.0 - float(d)) for c, f, d in rows]

    @staticmethod
    def fts_query(text: str) -> str:
        parts = []
        for tok in re.findall(r"[\w][\w\-\.]*", text):
            sub = re.findall(r"\w+", tok)
            if not sub:
                continue
            if len(sub) > 1 or any(ch.isdigit() for ch in tok):  # codes: PRJ-2041, Q4, v2.1
                parts.append('"' + " ".join(sub) + '"')
            else:
                w = sub[0]
                parts.append(f'"{w}"*' if len(w) >= 4 else f'"{w}"')
        return " OR ".join(parts)

    def search(self, q: FileQuery | str, limit: int = 8) -> list[FileHit]:
        if isinstance(q, str):
            q = FileQuery(topic=q)
        where, args = [], []
        if q.exts:
            where.append(f"ext IN ({','.join('?' * len(q.exts))})")
            args += sorted(q.exts)
        if q.since:
            where.append("mtime >= ?")
            args.append(q.since)
        if q.until:
            where.append("mtime < ?")
            args.append(q.until)
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        text = " ".join([q.topic] + q.names).strip()
        if not text:  # type/time only: most recent first, equal standing
            with self.lock:
                rows = self.db.execute(f"SELECT id,path,name,ext,kind,size,mtime FROM files{wsql} ORDER BY mtime DESC LIMIT ?", (*args, limit)).fetchall()
            hits = [self._hit(r) for r in rows]
            for h in hits:
                h.score = 1.0
                h.why = ["type"] + (["time"] if q.since else [])
            return hits

        with self.lock:
            allowed = {r[0] for r in self.db.execute(f"SELECT id FROM files{wsql}", args)} if where else None
        if allowed is not None and not allowed:
            return []

        # Keyword ranking (FTS5 / bm25, file name weighted highest).
        kw_rank: dict[int, int] = {}
        kw_chunk: dict[int, tuple[str, str]] = {}
        expr = self.fts_query(text)
        if expr:
            fw = "".join(" AND f." + w for w in where)
            with self.lock:
                rows = self.db.execute(
                    "SELECT c.file_id, c.label, c.text, bm25(fts, 10.0, 2.0, 1.0) r FROM fts "
                    "JOIN chunks c ON c.id = fts.rowid JOIN files f ON f.id = c.file_id "
                    f"WHERE fts MATCH ?{fw} ORDER BY r LIMIT 400",
                    (expr, *args),
                ).fetchall()
            for fid, lbl, txt, _ in rows:
                if fid not in kw_rank:
                    kw_rank[fid] = len(kw_rank)
                    kw_chunk[fid] = (lbl, txt)

        # Meaning ranking (embeddings), filtered inside the vector search.
        sem_rank: dict[int, int] = {}
        sem_sim: dict[int, float] = {}
        sem_chunk: dict[int, int] = {}
        qv = self._qcache.get(text)
        if qv is None:
            qv = self.embedder.embed([text], "query")[0].astype(np.float32)
            if len(self._qcache) > 256:
                self._qcache.clear()
            self._qcache[text] = qv
        for cid, fid, sim in self._nearest(qv, q):
            if fid not in sem_rank:
                sem_rank[fid] = len(sem_rank)
                sem_sim[fid] = sim
                sem_chunk[fid] = cid
        best_sim = max(sem_sim.values(), default=0.0)

        P = self.params
        scores: dict[int, float] = {}
        for fid, r in kw_rank.items():
            scores[fid] = scores.get(fid, 0) + P["w_kw"] / (RRF_K + r + 1)
        for fid, r in sem_rank.items():
            # Only let meaning matches count when they clear the floor and are close to the best one.
            if sem_sim[fid] >= P["min_sim"] and (sem_sim[fid] >= best_sim - P["band"] or r < 2):
                scores[fid] = scores.get(fid, 0) + P["w_sem"] / (RRF_K + r + 1)
        # Files that keyword search alone missed but whose *name* covers the request ("termpaper", "TermPaper_v2").
        qws = [w for w in (x.lower() for x in re.findall(r"\w+", text)) if len(w) >= 3 and w not in STOP]
        compact_q = "".join(qws)
        if len(qws) >= 2 and len(compact_q) >= 6:
            with self.lock:
                for (fid,) in self.db.execute(f"SELECT id FROM files WHERE replace(replace(replace(lower(name),' ',''),'_',''),'-','') LIKE ?"
                                              + ("".join(" AND " + w for w in where)), (f"%{compact_q}%", *args)).fetchall():
                    scores.setdefault(fid, 0.0)
        codes = [t.lower() for t in re.findall(r"\b[A-Za-z]+-?\d+[\w\-]*\b", text)]
        with self.lock:
            cand = {r[0]: self._hit(r) for r in self.db.execute(
                f"SELECT id,path,name,ext,kind,size,mtime FROM files WHERE id IN ({','.join(map(str, scores)) or 'NULL'})")}
        for fid in list(scores):
            if fid not in cand:  # deleted since ranking
                scores.pop(fid)
                continue
            nm = cand[fid].name.lower()
            if codes and any(c in nm or c.replace("-", "") in nm.replace("-", "") for c in codes):
                scores[fid] += 0.05  # exact project code in the name
            scores[fid] += self._coverage_bonus(qws, compact_q, nm, kw_chunk.get(fid, ("", ""))[1])
            # Newer files win ties: a small prior that halves every ~6 weeks (never outweighs a better match).
            age_days = max(0.0, (time.time() - cand[fid].mtime) / 86400)
            scores[fid] += P.get("w_recent", 0.006) * 0.5 ** (age_days / 42)
        ranked = sorted(scores.items(), key=lambda x: (-x[1], -cand[x[0]].mtime))[:limit]
        top = ranked[0][1] if ranked else 1.0
        ranked = [(fid, s) for fid, s in ranked if s / top >= P["rel_cut"]]
        out = []
        for fid, s in ranked:
            h = cand[fid]
            h.score = round(s / top, 3)
            h.why = (["keywords"] if fid in kw_rank else []) + (["meaning"] if fid in sem_rank and sem_rank[fid] < 3 and sem_sim[fid] >= P["min_sim"] else []) + (["time"] if q.since else [])
            # Prefer the passage that matched by meaning ("Keeping more customers"), else the keyword hit.
            lbl, txt = self._chunk(sem_chunk.get(fid))
            if lbl == "File name" or not txt:
                lbl, txt = kw_chunk.get(fid, (lbl, txt))
            h.snippet_label = "" if lbl == "File name" else (lbl or "")
            h.snippet = self._snip(txt or "", text)
            out.append(h)
        return out

    @staticmethod
    def _coverage_bonus(qws: list[str], compact_q: str, name: str, chunk: str) -> float:
        """How much of the request a file actually covers. "term paper" should beat a file that only says
        "term" (mid_Term_report) or only "paper" (graphpaper): every word in the name counts most, then
        every word in the matching passage, then the words together as a phrase."""
        if not qws:
            return 0.0

        def stem(w: str) -> str:
            return w[:-1] if len(w) > 4 and w.endswith("s") else w

        nwords = {stem(w) for w in re.findall(r"\w+", name_words(name).lower())}
        compact_name = re.sub(r"[\s_\-.]+", "", name.lower())
        want = [stem(w) for w in qws]
        in_name = sum(1 for w in want if w in nwords) / len(want)
        if len(qws) >= 2 and compact_q in compact_name:
            in_name = 1.0  # "termpaper", "TermPaper"
        text = chunk.lower()
        twords = {stem(w) for w in re.findall(r"\w+", text)}
        in_text = sum(1 for w in want if w in twords) / len(want) if text else 0.0
        phrase = len(qws) >= 2 and (" ".join(qws) in name_words(name).lower() or " ".join(qws) in re.sub(r"\s+", " ", text))
        bonus = 0.015 * in_name + (0.03 if in_name == 1.0 and len(qws) >= 2 else 0.0)
        bonus += 0.008 * in_text + (0.006 if in_text == 1.0 and len(qws) >= 2 else 0.0)
        bonus += 0.01 if phrase else 0.0
        return bonus

    def _chunk(self, cid: Optional[int]) -> tuple[str, str]:
        if cid is None:
            return "", ""
        with self.lock:
            r = self.db.execute("SELECT label, text FROM chunks WHERE id=?", (cid,)).fetchone()
        return (r[0], r[1]) if r else ("", "")

    @staticmethod
    def _snip(txt: str, query: str, width: int = 140) -> str:
        txt = re.sub(r"\s+", " ", txt).strip()
        if len(txt) <= width:
            return txt
        pos = min((m.start() for w in re.findall(r"\w{3,}", query) for m in [re.search(re.escape(w), txt, re.I)] if m), default=0)
        start = max(0, pos - 40)
        return ("…" if start else "") + txt[start : start + width].strip() + "…"
