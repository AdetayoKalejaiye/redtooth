"""Generate parity fixtures so the Kotlin and Swift parsers stay identical to redtooth/intent.py.

  python tools/make_intent_fixtures.py > android/core/src/test/resources/intent_fixtures.json
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from redtooth.intent import parse

NAMES = ["Daniel", "Daniel Ortiz", "Dan", "Priya", "Priya Shah", "Daniel Kim", "Mom"]
EXTRA = [
    "Hey, connect to my friend Daniel's headphones", "connect to Daniel's headphones", "Send Daniel the deck I edited this morning.",
    "Find the deck about customer churn.", "Send it to his laptop instead.", "yes", "no", "nope, not those", "the second one", "2",
    "the first one", "last", "share the budget spreadsheet with daniel", "play music on daniel's speaker", "connect my airpods",
    "Send the Q4 board deck to daniel's laptop", "send the PRJ-2041 checklist to Priya", "disconnect", "use priya's sony headphones",
    "send priya the photos from last week", "send mom the video I made yesterday afternoon", "find the pdf I got on monday",
    "send it to her phone", "send dan the slides from 2 days ago", "connect to my colleague priya's speaker",
    "Hey Siri, send Daniel the “Offsite agenda”", "could you find the notes I wrote just now", "which deck did I edit this week",
    "play it through daniel's bose", "send the spreadsheet to Daniel Kim", "find documents from last night", "ok", "that one",
]
now = datetime(2026, 11, 24, 15, 30, tzinfo=timezone(timedelta(hours=-6)))
queries = [q["q"] for q in json.loads(Path("redtooth/search_eval/queries.json").read_text())]
out = []
for text in EXTRA + queries:
    i = parse(text, NAMES, now)
    f = i.file
    out.append({
        "text": text, "action": i.action, "person": i.person, "pronoun": i.pronoun, "relation": i.relation,
        "device_kind": i.device_kind, "device_hint": i.device_hint, "choice": i.choice, "instead": i.instead,
        "file": None if f is None else {"kinds": f.kinds, "exts": sorted(f.exts), "topic": f.topic, "since": f.since, "until": f.until,
                                        "time_label": f.time_label, "refers_previous": f.refers_previous, "names": f.names},
    })
print(json.dumps({"now": now.isoformat(), "names": NAMES, "cases": out}, ensure_ascii=False, indent=1))
