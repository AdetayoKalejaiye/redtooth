"""Pull searchable text out of files, as labelled chunks ("Slide 3", "Page 2")."""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

KIND_BY_EXT = {
    **dict.fromkeys([".pptx", ".ppt", ".key", ".odp"], "presentation"),
    **dict.fromkeys([".docx", ".doc", ".odt", ".pages", ".rtf", ".md", ".txt"], "document"),
    **dict.fromkeys([".xlsx", ".xls", ".csv", ".numbers", ".ods", ".tsv"], "spreadsheet"),
    ".pdf": "pdf",
    **dict.fromkeys([".jpg", ".jpeg", ".png", ".heic", ".gif", ".webp"], "image"),
    **dict.fromkeys([".mp4", ".mov", ".mkv", ".webm"], "video"),
    **dict.fromkeys([".mp3", ".m4a", ".wav", ".flac"], "audio"),
}
MAX_CHARS = 200_000
CHUNK = 900
# Bigger files are indexed by name only: their text rarely helps and extraction would stall the queue.
MAX_TEXT_BYTES = 20 * 1024 * 1024  # .txt, .md, .csv…
MAX_DOC_BYTES = 150 * 1024 * 1024  # .pptx, .docx, .pdf…
TEXT_EXTS = {".txt", ".md", ".csv", ".tsv", ".rtf", ".json", ".html"}


def kind_of(path: str | Path) -> str:
    return KIND_BY_EXT.get(Path(path).suffix.lower(), "other")


def _split(text: str, label: str) -> list[tuple[str, str]]:
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return []
    if len(text) <= CHUNK:
        return [(label, text)]
    out, buf = [], ""
    for para in re.split(r"\n\s*\n|(?<=[.!?])\s+", text):
        if len(buf) + len(para) > CHUNK and buf:
            out.append((label, buf.strip()))
            buf = ""
        buf += para + " "
    if buf.strip():
        out.append((label, buf.strip()))
    return out


def _pptx(p: Path) -> list[tuple[str, str]]:
    from pptx import Presentation

    chunks = []
    for i, slide in enumerate(Presentation(str(p)).slides, 1):
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                texts.append(shape.text_frame.text)
            if getattr(shape, "has_table", False) and shape.has_table:
                texts += [c.text for r in shape.table.rows for c in r.cells]
        if slide.has_notes_slide:
            texts.append(slide.notes_slide.notes_text_frame.text)
        chunks += _split("\n".join(texts), f"Slide {i}")
    return chunks


def _docx(p: Path) -> list[tuple[str, str]]:
    import docx

    d = docx.Document(str(p))
    text = "\n\n".join(par.text for par in d.paragraphs)
    for t in d.tables:
        text += "\n\n" + "\n".join(c.text for r in t.rows for c in r.cells)
    return _split(text, "")


def _pdf(p: Path) -> list[tuple[str, str]]:
    from pypdf import PdfReader

    out = []
    for i, page in enumerate(PdfReader(str(p)).pages[:200], 1):
        out += _split(page.extract_text() or "", f"Page {i}")
    return out


def _xlsx(p: Path) -> list[tuple[str, str]]:
    with zipfile.ZipFile(p) as z:
        try:
            xml = z.read("xl/sharedStrings.xml").decode("utf8", "ignore")
        except KeyError:
            return []
    return _split(" ".join(re.findall(r"<t[^>]*>([^<]*)</t>", xml)), "")


def _odf(p: Path) -> list[tuple[str, str]]:
    with zipfile.ZipFile(p) as z:
        xml = z.read("content.xml").decode("utf8", "ignore")
    return _split(re.sub(r"<[^>]+>", " ", xml), "")


def _iwork(p: Path) -> list[tuple[str, str]]:
    # Keynote/Pages/Numbers are zip bundles; their text is in protobuf blobs. Pull printable runs.
    try:
        with zipfile.ZipFile(p) as z:
            raw = b" ".join(z.read(n) for n in z.namelist() if n.endswith(".iwa"))
    except Exception:
        return []
    return _split(" ".join(re.findall(rb"[\x20-\x7e]{6,}", raw)[:2000]).decode(), "")


def extract(path: str | Path) -> list[tuple[str, str]]:
    """Return [(label, text)] chunks. Never raises; unknown/binary types give []."""
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext == ".pptx":
            return _pptx(p)
        if ext == ".docx":
            return _docx(p)
        if ext == ".pdf":
            return _pdf(p)
        if ext == ".xlsx":
            return _xlsx(p)
        if ext in (".odt", ".odp", ".ods"):
            return _odf(p)
        if ext in (".key", ".pages", ".numbers"):
            return _iwork(p)
        if ext in (".txt", ".md", ".csv", ".tsv", ".rtf", ".json", ".html"):
            return _split(p.read_text(errors="ignore")[:MAX_CHARS], "")
    except Exception:
        return []
    return []


def content_limit(path: str | Path) -> int:
    return MAX_TEXT_BYTES if Path(path).suffix.lower() in TEXT_EXTS else MAX_DOC_BYTES


def extract_limited(path: str | Path, size: int) -> list[tuple[str, str]]:
    """extract(), but files over the size limit contribute only their name."""
    if size > content_limit(path) or kind_of(path) in ("image", "video", "audio"):
        return []
    return extract(path)
