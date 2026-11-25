"""A realistic, deliberately tricky corpus for the search test set.

Traps built in:
  * paraphrase-only matches: titles and text avoid the words people will use
    ("Cloud Cost Reduction" talks about "spending less on infrastructure")
  * near-miss distractors: "Retention Bonus Policy" (HR) vs the customer
    "Retention Plan"; a churn-model notebook vs the churn deck
  * other languages: Spanish, German, French, Japanese
  * project codes, edit times relative to now, and personal files as noise
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

# (file name, (days ago, hour, minute), content)
#   decks: list of (title, body) slides
#   docs / pdf: list of paragraphs
#   md / txt / csv: a string
CORPUS: list[tuple[str, tuple[int, int, int], object]] = [
    # ----------------------------------------------------------------- decks
    ("Q4 Board Update.pptx", (0, 9, 12), [
        ("Q4 Board Update", "Revenue, pipeline and hiring for the quarter"),
        ("Revenue", "ARR grew 18% quarter over quarter; enterprise is 61% of new bookings"),
        ("Hiring", "Engineering headcount went from 42 to 51; two director roles open"),
    ]),
    ("Sales Kickoff 2027.pptx", (0, 10, 41), [
        ("Sales Kickoff 2027", "Territories, quotas and the new partner program"),
        ("Partner program", "Resellers receive a 20% margin and co-marketing funds"),
    ]),
    ("Retention Plan.pptx", (3, 14, 5), [
        ("H1 plan", "Owner: Customer Success"),
        ("Keeping more customers", "Renewal playbook, early warning signals, save offers for accounts at risk of cancelling"),
        ("Targets", "Reduce monthly cancellations from 3.1% to 2.2%"),
    ]),
    ("Design Review — Onboarding.pptx", (8, 11, 0), [
        ("Onboarding redesign", "New welcome flow and progressive profile setup for first-time users"),
    ]),
    ("Cloud Cost Reduction.pptx", (20, 16, 30), [
        ("Spending less on infrastructure", "Our compute and storage invoice doubled in a year"),
        ("Levers", "Reserved instances, rightsizing idle machines, moving cold data to archival storage tiers"),
        ("Goal", "Cut the monthly hosting bill by 35% by June"),
    ]),
    ("Hiring Plan H1.pptx", (15, 10, 0), [
        ("Headcount by team", "Platform +4, Mobile +3, Sales +6"),
        ("Recruiting pipeline", "Referrals convert best; agency spend paused"),
    ]),
    ("Brand Refresh.pptx", (30, 15, 0), [
        ("A new look", "Updated logo, typography and color palette"),
        ("Rollout", "Website first, then product UI and printed materials"),
    ]),
    ("Security Incident Postmortem.pptx", (6, 13, 20), [
        ("What happened on March 3", "A leaked access key allowed reads from one storage bucket for 40 minutes"),
        ("Response", "Keys rotated, bucket policies tightened, affected customers notified within 24 hours"),
    ]),
    ("Pricing Change Proposal.pptx", (2, 17, 10), [
        ("Proposal", "Move from per-seat licenses to billing based on usage"),
        ("Why", "Large customers with occasional users feel overcharged; consumption tracks value"),
    ]),
    # ------------------------------------------------------------------ docs
    ("PRJ-2041 Launch Checklist.docx", (1, 11, 30), ["PRJ-2041 Launch Checklist", "Owner sign-off, legal review, status page update, support macros."]),
    ("PRJ-2177 Data Migration Plan.docx", (9, 9, 0), ["PRJ-2177", "Moving customer records from the legacy Postgres cluster into the new analytics warehouse over three weekends, with checksums after every batch."]),
    ("Retention Bonus Policy.docx", (40, 10, 0), ["Retention Bonus Policy", "Employees in critical roles may receive a bonus for staying through the acquisition. Paid in two installments; repayable if the employee resigns within 12 months."]),
    ("Vendor Contract — Acme Logistics.docx", (25, 12, 0), ["Master services agreement with Acme Logistics", "Freight and parcel delivery for all warehouses. Renews automatically on January 1 unless either party gives 60 days' notice. Termination for missed delivery targets."]),
    ("Parental Leave Policy.docx", (60, 9, 0), ["Parental Leave", "Birthing and non-birthing parents receive 16 weeks of fully paid leave, which can be taken within the first year after a birth or adoption."]),
    ("Office Move FAQ.docx", (5, 15, 45), ["Office Move FAQ", "We relocate to 200 Harbor Street on February 2. Desks are assigned by team, the garage has 80 spaces, and the old building closes the same week."]),
    ("Plan de retención de clientes.docx", (5, 12, 0), ["Plan de retención", "Cómo reducir la pérdida de clientes: llamadas de renovación, alertas tempranas y ofertas de permanencia."]),
    ("Datenschutz-Richtlinie.docx", (50, 10, 0), ["Datenschutz-Richtlinie", "Personenbezogene Daten werden nur zweckgebunden verarbeitet, nach 24 Monaten gelöscht und Auskunftsersuchen innerhalb von 30 Tagen beantwortet."]),
    ("Compte rendu réunion fournisseurs.docx", (7, 16, 0), ["Compte rendu de la réunion avec les fournisseurs", "Retards de livraison sur les emballages, hausse des prix de 6 % annoncée pour avril, nouveau calendrier de commandes."]),
    ("Quarterly OKRs.docx", (14, 9, 30), ["Objectives and key results", "Grow enterprise ARR to $40M; reduce cancellations to 2.2% monthly; ship the mobile app to the App Store."]),
    ("Incident Response Runbook.docx", (90, 9, 0), ["Incident response", "When you're paged: acknowledge within 5 minutes, open an incident channel, assign a commander, post status updates every 30 minutes, and write a postmortem within a week."]),
    ("Customer Interview Notes — Northwind.docx", (4, 14, 0), ["Northwind interview", "They need SAML sign-in through Okta before rolling out company-wide. Exports over 10,000 rows time out."]),
    # ------------------------------------------------------------------- pdf
    ("Employee Handbook 2026.pdf", (120, 9, 0), ["Employee Handbook", "Working hours are flexible between 7am and 7pm. Equipment is provided on the first day. Report harassment to People Ops or the anonymous hotline."]),
    ("Invoice INV-88312.pdf", (11, 10, 0), ["Invoice INV-88312", "Acme Logistics — freight services for October — amount due $18,420 within 30 days."]),
    # ------------------------------------------------------------- md / txt
    ("Offsite agenda.md", (1, 8, 0), "# Team offsite\n\nDay 1: strategy and roadmap. Day 2: hiking and dinner."),
    ("Churn prediction notebook notes.md", (12, 18, 0), "# Churn model\n\nGradient boosted trees on usage and support tickets predict which accounts will cancel. AUC 0.81. Top features: seats used, ticket volume, login frequency."),
    ("Team structure changes draft.md", (3, 19, 0), "# Changes to how our teams are organized\n\nThe platform and infrastructure groups merge under one VP. Mobile moves under Product. Announcement planned for Monday's all-hands."),
    ("Book club picks.md", (45, 21, 0), "# Book club\n\nNext: a novel about a lighthouse keeper; then a history of the spice trade."),
    ("Wedding toast ideas.md", (70, 22, 0), "# Toast\n\nOpen with the camping story, thank both families, keep it under three minutes."),
    ("1-1 notes with Priya.md", (2, 16, 0), "# 1:1 with Priya\n\nCareer growth: path to staff engineer, mentoring two new hires, lead the search project next quarter."),
    ("Standup notes.md", (1, 9, 45), "# Standup\n\nBlocked on API keys for the staging environment. Release candidate cut Thursday."),
    ("Kubernetes upgrade runbook.md", (33, 11, 0), "# Kubernetes upgrade\n\nDrain nodes one at a time, upgrade the control plane, then the worker pools; verify workloads after each step."),
    ("Grocery list.txt", (1, 20, 0), "oat milk, eggs, spinach, coffee filters, lemons"),
    ("Expense report guidelines.md", (100, 9, 0), "# Expenses\n\nSubmit receipts within 30 days. Flights, hotels and meals while traveling are reimbursed; the meal per diem is $60."),
    ("顧客維持計画.md", (6, 10, 0), "# 顧客維持計画\n\n解約率を下げるため、更新前の電話、早期警告、特別オファーを実施します。"),
    # ---------------------------------------------------------- spreadsheets
    ("FY27 budget.csv", (2, 10, 0), "dept,q1,q2\nEngineering,1.2M,1.3M\nSales,0.8M,0.9M\n"),
    ("Headcount tracker.csv", (6, 9, 0), "team,current,plan\nPlatform,12,16\nMobile,7,10\n"),
    ("Customer health scores.csv", (1, 14, 0), "account,health,risk_of_cancelling\nNorthwind,62,high\nContoso,88,low\nFabrikam,40,high\n"),
    ("Travel bookings Q1.csv", (20, 10, 0), "traveler,destination,dates\nAlex,Berlin,Feb 3-6\nPriya,Austin,Mar 10-12\n"),
]


def _when(now: datetime, days: int, hour: int, minute: int) -> datetime:
    t = (now - timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if t > now:  # "edited this morning" must not be in the future
        t = max(now.replace(hour=0, minute=5, second=0, microsecond=0), now - timedelta(minutes=30 + minute))
    return t


def _deck(path: Path, slides) -> None:
    from pptx import Presentation

    prs = Presentation()
    for title, body in slides:
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = title
        s.placeholders[1].text = body
    prs.save(str(path))


def _doc(path: Path, paras) -> None:
    import docx

    d = docx.Document()
    d.add_heading(paras[0], 0)
    for p in paras[1:]:
        d.add_paragraph(p)
    d.save(str(path))


def _pdf(path: Path, paras) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=letter)
    y = 720
    for p in paras:
        for i in range(0, len(p), 90):
            c.drawString(72, y, p[i : i + 90])
            y -= 16
        y -= 8
    c.save()


def build_corpus(root: str | Path, now: datetime | None = None) -> Path:
    now = now or datetime.now().astimezone()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for name, (days, h, m), content in CORPUS:
        path = root / name
        ext = path.suffix.lower()
        if ext == ".pptx":
            _deck(path, content)
        elif ext == ".docx":
            _doc(path, content)
        elif ext == ".pdf":
            _pdf(path, content)
        else:
            path.write_text(content, encoding="utf-8")
        ts = _when(now, days, h, m).timestamp()
        os.utime(path, (ts, ts))
    return root
