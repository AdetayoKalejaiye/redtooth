"""Fixtures for the ranking and clarifying-question rules, so the Kotlin and Swift ports match the desktop.

    python tools/make_ranking_fixtures.py > ranking_fixtures.json
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from redtooth.clarify import ambiguous, distinguish, option_detail  # noqa: E402
from redtooth.files.index import STOP, FileIndex  # noqa: E402
from redtooth.models import FileHit  # noqa: E402

os.environ["TZ"] = "UTC"
NOW = datetime(2026, 11, 25, 15, 30, tzinfo=timezone.utc)
DAY = 86400


def qws(text):
    import re

    return [w for w in (x.lower() for x in re.findall(r"\w+", text)) if len(w) >= 3 and w not in STOP]


coverage = []
for text, name, chunk in [
    ("term paper", "HIST 201 Term Paper - final.docx", "The economic effects of the railroad."),
    ("term paper", "mid_Term_report.docx", "Midterm progress report."),
    ("term paper", "graphpaper.pdf", "Printable grid."),
    ("term paper", "termpaper.docx", ""),
    ("term paper", "DocuSign_d9a0.pdf", "These terms survive termination. Paper copies on request."),
    ("customer churn", "Retention Plan.pptx", "Keeping more customers: churn fell 12%."),
    ("board update", "Q4 Board Update.pptx", "Revenue and hiring."),
    ("invoices", "Invoice INV-88312.pdf", "Amount due"),
]:
    q = qws(text)
    coverage.append({"text": text, "qws": q, "name": name, "chunk": chunk,
                     "bonus": round(FileIndex._coverage_bonus(q, "".join(q), name, chunk), 6)})


def hit(name, days, path=None, conf=None, score=1.0, reasons=()):
    return FileHit(1, path or f"/Users/r/Documents/{name}", name, "document", os.path.splitext(name)[1], 1,
                   NOW.timestamp() - days * DAY, score, confidence=conf, reasons=list(reasons))


def hd(h):
    return {"name": h.name, "path": h.path, "ext": h.ext, "mtime": h.mtime, "score": h.score, "confidence": h.confidence, "reasons": h.reasons}


questions = []
for hs in [
    [hit("HIST 201 Term Paper - final.docx", 12), hit("HIST 201 Term Paper - draft.docx", 50)],
    [hit("Term Paper.docx", 12), hit("Term Paper.pdf", 12)],
    [hit("Retention Plan.pptx", 13), hit("Pricing Change Proposal.pptx", 19)],
    [hit("Budget.xlsx", 15, "/Users/r/Documents/Work/Budget.xlsx"), hit("Budget.xlsx", 15, "/Users/r/Downloads/Budget.xlsx")],
    [hit("mid_Term_report.docx", 200), hit("graphpaper.pdf", 60), hit("Term paper outline.docx", 20)],
    [hit("Report.docx", 10), hit("Report (1).docx", 30)],
]:
    q, phr = distinguish(hs, NOW)
    questions.append({"hits": [hd(h) for h in hs], "question": q, "details": [option_detail(h, NOW) for h in hs]})

amb = []
for hs, topic in [
    ([hit("a.docx", 1, conf=0.95, score=1.0), hit("b.docx", 1, conf=0.2, score=0.4)], True),
    ([hit("a.docx", 1, conf=0.8, score=1.0), hit("b.docx", 1, conf=0.75, score=0.93)], True),
    ([hit("a.docx", 1, conf=0.8, score=1.0, reasons=["you picked it for this before"]), hit("b.docx", 1, conf=0.8, score=0.99)], True),
    ([hit("a.docx", 1, conf=0.1, score=1.0), hit("b.docx", 1, conf=0.05, score=0.5)], True),
    ([hit("a.docx", 1), hit("b.docx", 1, score=0.9)], True),
    ([hit("a.docx", 1), hit("b.docx", 1, score=0.5)], True),
    ([hit("a.docx", 1), hit("b.docx", 1)], False),
    ([hit("a.docx", 1)], True),
]:
    amb.append({"hits": [hd(h) for h in hs], "topic": topic, "ask": ambiguous(hs, topic)})

json.dump({"now": NOW.timestamp(), "stop": sorted(STOP), "coverage": coverage, "questions": questions, "ambiguous": amb}, sys.stdout, indent=1, ensure_ascii=False)
