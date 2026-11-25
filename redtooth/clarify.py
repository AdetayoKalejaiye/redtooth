"""Deciding when to ask "which file?", and asking about what actually differs.

"Which file did you mean?" makes you do the comparison. Better: "The final (edited Nov 23) or the draft
(Oct 1)?" — versions are told apart by their version words or dates, different files by the words only
they have, the same name in different formats by type, the same file in two places by folder.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from .models import FileHit
from .util import when_label

VERSION_WORDS = ["final", "draft", "revised", "edited", "signed", "submitted", "old", "new", "copy", "backup", "v1", "v2", "v3", "v4",
                 "notes", "outline", "rough"]
TYPE_NAMES = {".docx": "Word doc", ".doc": "Word doc", ".pdf": "PDF", ".pptx": "deck", ".key": "Keynote deck", ".xlsx": "spreadsheet",
              ".csv": "CSV", ".txt": "text file", ".md": "notes file", ".pages": "Pages doc", ".odt": "document", ".rtf": "document"}


def ambiguous(hits: list[FileHit], has_topic: bool) -> bool:
    """Ask only when it's genuinely unclear."""
    if len(hits) < 2:
        return False
    if not has_topic:  # "the deck from this morning": type/time alone can't choose
        return True
    a, b = hits[0], hits[1]
    if a.reasons and a.reasons[0].startswith("you picked it"):
        return False  # you've answered this exact request before
    if a.confidence is not None and b.confidence is not None:
        if a.confidence < 0.15:
            return True  # nothing clearly matches: show the best few
        return b.confidence >= 0.3 and (a.score - b.score) < 0.15
    return b.score >= 0.85


def _words(name: str, keep_case: bool = False) -> list[str]:
    stem = os.path.splitext(name)[0]
    stem = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem)
    ws = [w for w in re.split(r"[\s_\-.()\[\]]+", stem) if w]
    return ws if keep_case else [w.lower() for w in ws]


def _core(name: str) -> str:
    """The name without version words, dates and copy numbers: same core = versions of one thing."""
    ws = [w for w in _words(name) if w not in VERSION_WORDS and not re.fullmatch(r"\d{1,4}|v\d+|copy", w)]
    return " ".join(ws)


def _folder(path: str) -> str:
    parts = os.path.normpath(path).split(os.sep)
    return " › ".join(parts[-3:-1]) if len(parts) >= 3 else (parts[-2] if len(parts) >= 2 else "")


def distinguish(hits: list[FileHit]) -> tuple[str, list[str]]:
    """(question, one short distinguishing phrase per file)."""
    hs = hits[:3]
    exts = [h.ext.lower() for h in hs]
    cores = [_core(h.name) for h in hs]
    words = [set(_words(h.name)) for h in hs]
    dates = [when_label(h.mtime) for h in hs]

    def join(parts: list[str]) -> str:
        return parts[0] + " or " + parts[1] if len(parts) == 2 else ", ".join(parts[:-1]) + " or " + parts[-1]

    # Same file name in different formats: by type.
    if len(set(cores)) == 1 and len(set(exts)) == len(hs):
        phr = [f"the {TYPE_NAMES.get(e, e.lstrip('.').upper())}" for e in exts]
        return f"Which one: {join(phr)}?", phr
    # Versions of one thing: by their version words, else by date.
    if len(set(cores)) == 1 or all(len(w & words[0]) >= max(1, len(words[0]) // 2) for w in words[1:]):
        tags = []
        for h, w in zip(hs, words):
            v = next((x for x in VERSION_WORDS if x in w), None)
            tags.append(v)
        if all(tags) and len(set(tags)) == len(tags):
            phr = [f"the {t} ({'edited ' if i == 0 else ''}{d})" for i, (t, d) in enumerate(zip(tags, dates))]
        elif len(set(dates)) == len(dates):
            phr = [f"the one from {d}" for d in dates]
        else:
            folders = [_folder(h.path) for h in hs]
            if len(set(folders)) == len(folders):
                phr = [f"the one in {f}" for f in folders]
            else:
                phr = [os.path.splitext(h.name)[0] for h in hs]
        return f"Which one: {join(phr)}?", phr
    # Different files: by the words only each one has.
    common = set.intersection(*words) if words else set()
    phr = []
    for h, w in zip(hs, words):
        own = [x for x in _words(h.name, keep_case=True) if x.lower() not in common]
        label = " ".join(dict.fromkeys(own))[:40].strip() or os.path.splitext(h.name)[0]
        phr.append(f"“{label}”")
    return f"Which one: {join(phr)}?", phr


def option_detail(h: FileHit) -> str:
    bits = [f"Edited {when_label(h.mtime)}"]
    folder = _folder(h.path)
    if folder:
        bits.append(folder)
    if h.reasons:
        bits.append(h.reasons[0])
    elif h.snippet_label:
        bits.append(h.snippet_label)
    return " · ".join(bits)
