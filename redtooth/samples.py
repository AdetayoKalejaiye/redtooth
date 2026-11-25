"""Generate a realistic sample folder for the demo and tests."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path


def _deck(path: Path, slides: list[tuple[str, str]], notes: str = "") -> None:
    from pptx import Presentation

    prs = Presentation()
    for i, (title, body) in enumerate(slides):
        s = prs.slides.add_slide(prs.slide_layouts[1 if body else 0])
        s.shapes.title.text = title
        if body and len(s.placeholders) > 1:
            s.placeholders[1].text = body
        if notes and i == 0:
            s.notes_slide.notes_text_frame.text = notes
    prs.save(str(path))


def _doc(path: Path, title: str, paras: list[str]) -> None:
    import docx

    d = docx.Document()
    d.add_heading(title, 0)
    for p in paras:
        d.add_paragraph(p)
    d.save(str(path))


def _touch(path: Path, when: datetime) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def make_samples(root: str | Path, now: datetime | None = None) -> Path:
    now = now or datetime.now().astimezone()
    root = Path(root)
    docs = root / "Documents"
    docs.mkdir(parents=True, exist_ok=True)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def morning(h: int, m: int, back: timedelta) -> datetime:
        # Edited "this morning": a morning time today, never in the future.
        t = today.replace(hour=h, minute=m)
        if t > now:
            t = max(today + timedelta(minutes=5), now - back)
        return t

    files = [
        ("Q4 Board Update.pptx", morning(9, 12, timedelta(minutes=50)), lambda p: _deck(p, [
            ("Q4 Board Update", "Revenue, pipeline and hiring for the quarter"),
            ("Revenue", "ARR grew 18% quarter over quarter; enterprise is 61% of bookings"),
            ("Hiring", "Engineering headcount 42 → 51; two open director roles"),
        ], "Board meeting December 3")),
        ("Sales Kickoff 2027.pptx", morning(10, 41, timedelta(minutes=20)), lambda p: _deck(p, [
            ("Sales Kickoff 2027", "Territories, quotas and the new partner program"),
            ("Partner program", "Resellers get 20% margin and co-marketing funds"),
        ])),
        ("Retention Plan.pptx", now - timedelta(days=3, hours=2), lambda p: _deck(p, [
            ("H1 plan", "Owner: Customer Success"),
            ("Keeping more customers", "Renewal playbook, early warning signals, save offers for accounts at risk of cancelling"),
            ("Targets", "Reduce cancellations from 3.1% to 2.2% monthly"),
        ])),
        ("Design Review — Onboarding.pptx", now - timedelta(days=8), lambda p: _deck(p, [
            ("Onboarding redesign", "New welcome flow, progressive profile setup"),
        ])),
        ("PRJ-2041 Launch Checklist.docx", now - timedelta(days=1, hours=3), lambda p: _doc(p, "PRJ-2041 Launch Checklist", [
            "Owner sign-off, legal review, status page update, support macros.",
        ])),
        ("Plan de retención de clientes.docx", now - timedelta(days=5), lambda p: _doc(p, "Plan de retención", [
            "Cómo reducir la pérdida de clientes: llamadas de renovación y ofertas de permanencia.",
        ])),
        ("Offsite agenda.md", now - timedelta(hours=26), lambda p: p.write_text("# Team offsite\n\nDay 1: strategy. Day 2: hiking and dinner.\n")),
        ("FY27 budget.csv", now - timedelta(days=2), lambda p: p.write_text("dept,q1,q2\nEngineering,1.2M,1.3M\nSales,0.8M,0.9M\n")),
    ]
    for name, when, make in files:
        path = docs / name
        if not path.exists():
            make(path)
        _touch(path, when)
    big = root / "Downloads"
    big.mkdir(exist_ok=True)
    video = big / "Product walkthrough.mp4"
    if not video.exists():
        with open(video, "wb") as f:  # 48 MB of pseudo-random bytes for transfer demos
            seed = os.urandom(1 << 20)
            for i in range(48):
                f.write(seed[i:] + seed[:i])
    _touch(video, now - timedelta(days=1))
    return root
