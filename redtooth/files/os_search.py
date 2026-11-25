"""Ask the operating system's own file index for likely matches.

Used only while Redtooth's first indexing pass is still running, so requests
like "the deck I edited this morning" work immediately on a new install:
the OS already knows file types and edit times. Candidates are then indexed
by Redtooth on the spot and ranked normally.

macOS: Spotlight (`mdfind`). Windows: Windows Search (SystemIndex via ADODB).
Linux has no standard equivalent; returns nothing. Both are written to each
platform's documented query syntax but have not been run on those systems.
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime, timezone

from .extract import KIND_BY_EXT

LIMIT = 50


def _words(fq) -> list[str]:
    return [w for w in re.findall(r"[\w\-]{3,}", " ".join([fq.topic or ""] + list(fq.names or []))) if not w.isdigit()][:4]


def _exts(fq) -> list[str]:
    return sorted(e for e in (fq.exts or set()) if e in KIND_BY_EXT)


def spotlight_query(fq) -> str:
    parts = []
    exts = _exts(fq)
    if exts:
        parts.append("(" + " || ".join(f'kMDItemFSName == "*{e}"c' for e in exts) + ")")
    if fq.since:
        iso = datetime.fromtimestamp(fq.since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        parts.append(f"kMDItemContentModificationDate >= $time.iso({iso})")
    if fq.until:
        iso = datetime.fromtimestamp(fq.until, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        parts.append(f"kMDItemContentModificationDate < $time.iso({iso})")
    words = _words(fq)
    if words:
        parts.append("(" + " || ".join(f'kMDItemFSName == "*{w}*"cd || kMDItemTextContent == "{w}*"cd' for w in words) + ")")
    return " && ".join(parts)


def windows_query(fq, root: str) -> str:
    conds = [f"SCOPE='file:{root.replace(chr(92), '/')}'"]
    exts = _exts(fq)
    if exts:
        conds.append("(" + " OR ".join(f"System.FileExtension = '{e}'" for e in exts) + ")")
    if fq.since:
        conds.append(f"System.DateModified >= '{datetime.fromtimestamp(fq.since, timezone.utc):%Y-%m-%d %H:%M:%S}'")
    if fq.until:
        conds.append(f"System.DateModified < '{datetime.fromtimestamp(fq.until, timezone.utc):%Y-%m-%d %H:%M:%S}'")
    words = [w.replace("'", "") for w in _words(fq)]
    if words:
        conds.append("(" + " OR ".join(f"CONTAINS(System.FileName, '\"{w}*\"') OR CONTAINS('\"{w}*\"')" for w in words) + ")")
    return f"SELECT TOP {LIMIT} System.ItemPathDisplay FROM SYSTEMINDEX WHERE " + " AND ".join(conds) + " ORDER BY System.DateModified DESC"


def candidates(fq, roots: list[str]) -> list[str]:
    if fq is None:
        return []
    out: list[str] = []
    try:
        if sys.platform == "darwin":
            q = spotlight_query(fq)
            if not q:
                return []
            for r in roots:
                res = subprocess.run(["mdfind", "-onlyin", r, q], capture_output=True, text=True, timeout=5)
                out += [l for l in res.stdout.splitlines() if l]
        elif sys.platform.startswith("win"):
            for r in roots:
                sql = windows_query(fq, r).replace('"', '`"')
                ps = (
                    "$c = New-Object -ComObject ADODB.Connection; "
                    "$c.Open(\"Provider=Search.CollatorDSO;Extended Properties='Application=Windows';\"); "
                    f"$rs = $c.Execute(\"{sql}\"); "
                    "while (-not $rs.EOF) { $rs.Fields.Item('System.ItemPathDisplay').Value; $rs.MoveNext() }"
                )
                res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=8)
                out += [l.strip() for l in res.stdout.splitlines() if l.strip()]
    except Exception:
        return out
    return out[:LIMIT]
