"""What you actually use: the strongest clue for "my term paper".

Signals, each fading with a two-week half-life:
- files the OS lists as recently opened (Windows: the Recent Items folder of shortcuts;
  Linux desktops: recently-used.xbel),
- files you sent or picked in Redtooth,
- which file you picked for a request ("term paper" -> HIST 201 Term Paper.docx), so the same request
  goes straight there next time.
"""
from __future__ import annotations

import os
import re
import sqlite3
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

HALF_LIFE_DAYS = 14
WEIGHTS = {"sent": 1.0, "picked": 0.8, "opened": 0.6}


def norm_path(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def norm_query(q: str) -> str:
    words = re.findall(r"\w+", q.lower())
    return " ".join(w[:-1] if len(w) > 4 and w.endswith("s") else w for w in words)


# ----------------------------------------------------------------- Windows shortcut (.lnk) targets
def lnk_target(data: bytes) -> Optional[str]:
    """The local path a Windows shortcut points to (MS-SHLLINK: LinkInfo › LocalBasePath)."""
    try:
        if len(data) < 0x4C or struct.unpack_from("<I", data, 0)[0] != 0x4C:
            return None
        flags = struct.unpack_from("<I", data, 0x14)[0]
        pos = 0x4C
        if flags & 0x01:  # HasLinkTargetIDList
            pos += 2 + struct.unpack_from("<H", data, pos)[0]
        if not flags & 0x02:  # HasLinkInfo
            return None
        size, hsize, liflags, _vol, base_off, _net, suffix_off = struct.unpack_from("<7I", data, pos)
        if not liflags & 0x01:  # VolumeIDAndLocalBasePath
            return None

        def cstr(off: int, wide: bool) -> str:
            start = pos + off
            if wide:
                end = start
                while end + 1 < len(data) and data[end:end + 2] != b"\x00\x00":
                    end += 2
                return data[start:end].decode("utf-16-le", "replace")
            end = data.index(b"\x00", start)
            return data[start:end].decode("mbcs" if sys.platform.startswith("win") else "cp1252", "replace")

        if hsize >= 0x24:
            base_u, suffix_u = struct.unpack_from("<2I", data, pos + 28)
            base, suffix = cstr(base_u, True), (cstr(suffix_u, True) if suffix_u else "")
        else:
            base, suffix = cstr(base_off, False), cstr(suffix_off, False)
        return base + suffix if base else None
    except Exception:
        return None


def os_recent(limit: int = 500) -> dict[str, float]:
    """{path: last opened (epoch)} from the OS's recent-files list."""
    out: dict[str, float] = {}
    if sys.platform.startswith("win"):
        folder = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Recent"
        try:
            links = sorted(folder.glob("*.lnk"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
        except OSError:
            links = []
        for lnk in links:
            try:
                target = lnk_target(lnk.read_bytes())
                if target:
                    out[norm_path(target)] = lnk.stat().st_mtime
            except OSError:
                continue
    elif sys.platform.startswith("linux"):
        xbel = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "recently-used.xbel"
        try:
            import xml.etree.ElementTree as ET

            for b in ET.parse(xbel).getroot().iter("bookmark"):
                u = urlparse(b.get("href", ""))
                if u.scheme != "file":
                    continue
                stamp = b.get("visited") or b.get("modified") or ""
                try:
                    from datetime import datetime

                    t = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    t = 0.0
                out[norm_path(unquote(u.path))] = t
        except Exception:
            pass
    return out


class Usage:
    def __init__(self, db_path: str | Path = ":memory:"):
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(
                """CREATE TABLE IF NOT EXISTS usage(path TEXT, kind TEXT, at REAL);
                   CREATE INDEX IF NOT EXISTS usage_path ON usage(path);
                   CREATE TABLE IF NOT EXISTS picks(query TEXT, path TEXT, n INTEGER, at REAL, PRIMARY KEY(query, path));"""
            )
        self._recent: tuple[float, dict[str, float]] = (0.0, {})
        self.recent_source = os_recent

    def record(self, path: str, kind: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO usage VALUES(?,?,?)", (norm_path(path), kind, time.time()))
            self.db.commit()

    def record_pick(self, query: str, path: str) -> None:
        q = norm_query(query)
        if not q:
            return
        with self.lock:
            self.db.execute("INSERT INTO picks VALUES(?,?,1,?) ON CONFLICT(query,path) DO UPDATE SET n=n+1, at=excluded.at",
                            (q, norm_path(path), time.time()))
            self.db.commit()
        self.record(path, "picked")

    def _os_recent(self) -> dict[str, float]:
        if time.time() - self._recent[0] > 60:
            try:
                self._recent = (time.time(), self.recent_source())
            except Exception:
                self._recent = (time.time(), {})
        return self._recent[1]

    def signals(self, paths: list[str], query: str = "") -> dict[str, tuple[float, float, list[str]]]:
        """{path: (usage score 0..1, picked-for-this-request 0..1, reasons)} for the candidates."""
        now = time.time()
        keys = {norm_path(p): p for p in paths}
        out = {p: (0.0, 0.0, []) for p in paths}
        if not keys:
            return out
        raw: dict[str, float] = {k: 0.0 for k in keys}
        reasons: dict[str, list[str]] = {k: [] for k in keys}
        marks = ",".join("?" * len(keys))
        with self.lock:
            rows = self.db.execute(f"SELECT path, kind, at FROM usage WHERE path IN ({marks})", list(keys)).fetchall()
            picks = self.db.execute(f"SELECT path, n FROM picks WHERE query=? AND path IN ({marks})", [norm_query(query), *keys]).fetchall() if query else []
        for path, kind, at in rows:
            raw[path] += WEIGHTS.get(kind, 0.5) * 0.5 ** (max(0.0, now - at) / 86400 / HALF_LIFE_DAYS)
            if kind == "sent" and "sent before" not in reasons[path]:
                reasons[path].append("sent before")
        recent = self._os_recent()
        for k in keys:
            if k in recent:
                raw[k] += WEIGHTS["opened"] * 0.5 ** (max(0.0, now - recent[k]) / 86400 / HALF_LIFE_DAYS)
                reasons[k].append("opened recently")
        picked = {p: n for p, n in picks}
        import math

        for k, p in keys.items():
            pick = 1.0 - 0.5 ** picked.get(k, 0)
            if pick:
                reasons[k].insert(0, "you picked it for this before")
            out[p] = (1.0 - math.exp(-raw[k]), pick, reasons[k])
        return out
