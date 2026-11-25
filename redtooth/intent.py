"""Understand what the user asks for.

A deterministic parser covers the everyday phrasings (connect, send, find,
retarget with "instead", yes/no, picking an option). It is given the user's
contact names so it can spot a recipient anywhere in the sentence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from .contacts import relation_hint
from .timeparse import parse_time

DEVICE_WORDS: dict[str, list[str]] = {
    "headphones": ["headphones", "headphone", "headset", "cans", "earphones", "earbuds", "buds", "airpods", "xm5s", "xm5", "xm4s", "xm4", "qc45s", "qc45", "beats"],
    "speaker": ["speaker", "speakers", "soundbar", "homepod", "sonos"],
    "laptop": ["laptop", "macbook", "notebook", "computer", "pc", "mac"],
    "desktop": ["desktop", "imac", "workstation"],
    "phone": ["phone", "iphone", "pixel", "galaxy", "mobile", "cell"],
    "tablet": ["ipad", "tablet"],
}
MODEL_HINTS = ["airpods max", "airpods pro", "airpods", "xm5", "xm4", "sony", "bose", "qc45", "beats", "jabra", "galaxy buds", "pixel buds", "jbl"]

FILE_KINDS: dict[str, tuple[list[str], set[str]]] = {
    "presentation": (["deck", "decks", "presentation", "presentations", "slides", "slide deck", "keynote", "powerpoint", "pitch"], {".pptx", ".ppt", ".key", ".odp"}),
    "document": (["doc", "docs", "document", "documents", "memo", "write-up", "writeup", "draft", "letter", "word doc"], {".docx", ".doc", ".odt", ".pages", ".md", ".txt", ".rtf", ".pdf"}),
    "spreadsheet": (["spreadsheet", "sheet", "sheets", "excel", "budget", "model", "csv"], {".xlsx", ".xls", ".csv", ".numbers", ".ods"}),
    "pdf": (["pdf", "pdfs"], {".pdf"}),
    "image": (["photo", "photos", "picture", "pictures", "image", "images", "screenshot", "screenshots", "pic", "pics"], {".jpg", ".jpeg", ".png", ".heic", ".gif", ".webp"}),
    "video": (["video", "videos", "recording", "clip", "movie"], {".mp4", ".mov", ".mkv", ".webm"}),
    "audio": (["song", "track", "audio file", "voice memo"], {".mp3", ".m4a", ".wav", ".flac"}),
}

TOPICAL_KIND_WORDS = {"budget", "pitch", "memo", "letter", "recording", "model", "screenshot", "keynote"}
PRONOUN_PEOPLE = {"he", "him", "his", "she", "her", "hers", "they", "them", "their"}
PRONOUN_FILES = {"it", "that", "this", "that file", "this file", "the file", "the same file", "the same thing"}
NOT_NAMES = {"it", "that", "what", "let", "there", "here", "who", "he", "she", "where", "today", "everyone", "someone", "one"}
STOP = set(
    """a an the my me i to for of on in at with and or is was were be this that these those it its from by about
    please can could would you hey ok okay just some file files thing stuff one i'd i'm send share find search look
    for show open get give edited modified updated changed worked made created saved wrote last latest recent new
    over across which where whats what's called named titled regarding re related topic copy version instead
    connect friend friends mine our his her their him them up are those these yes no""".split()
)

CONFIRM = re.compile(r"^(yes|yeah|yep|yup|ya|sure|correct|right|ok|okay|exactly|that'?s (right|them|it|correct|the one)|those|that one|affirmative|do it|go ahead)\b", re.I)
DENY = re.compile(r"^(no|nope|nah|wrong|not (those|that|them|it|these)|neither|cancel)\b", re.I)
ORDINALS = {"first": 0, "1st": 0, "one": 0, "second": 1, "2nd": 1, "two": 1, "third": 2, "3rd": 2, "three": 2, "fourth": 3, "last": -1}


@dataclass
class FileQuery:
    kinds: list[str] = field(default_factory=list)
    exts: set[str] = field(default_factory=set)
    topic: str = ""
    since: Optional[float] = None
    until: Optional[float] = None
    time_label: Optional[str] = None
    refers_previous: bool = False
    names: list[str] = field(default_factory=list)  # quoted / "called X" exact names

    def describe(self) -> str:
        noun = {"presentation": "deck", "spreadsheet": "spreadsheet", "image": "photo"}.get(self.kinds[0], self.kinds[0]) if self.kinds else "file"
        bits = [noun]
        if self.topic:
            bits.append(f"about “{self.topic}”")
        if self.time_label:
            bits.append(f"edited {self.time_label}")
        return " ".join(bits)

    def is_empty(self) -> bool:
        return not (self.kinds or self.topic or self.since or self.names)


@dataclass
class Intent:
    action: str  # connect | disconnect | send | find | retarget | confirm | deny | choose | unknown
    text: str
    person: Optional[str] = None
    pronoun: bool = False
    relation: Optional[str] = None
    device_kind: Optional[str] = None
    device_hint: Optional[str] = None
    file: Optional[FileQuery] = None
    choice: Optional[int] = None
    instead: bool = False


def _norm(text: str) -> str:
    t = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"^\s*(hey|hi|ok|okay)?\s*(redtooth|claude|siri)?[,!]?\s*", "", t, flags=re.I)
    t = re.sub(r"^\s*(please|can you|could you|would you|i want to|i'd like to|let's|i need to|help me)\s+", "", t, flags=re.I)
    t = re.sub(r"^\s*(please)\s+", "", t, flags=re.I)
    return t.strip().rstrip("?.!")


def _find_word(t: str, words: Iterable[str]) -> Optional[str]:
    for w in sorted(words, key=len, reverse=True):
        if re.search(rf"\b{re.escape(w)}\b", t):
            return w
    return None


def parse(text: str, known_names: Iterable[str] = (), now: Optional[datetime] = None) -> Intent:
    raw = text
    t = _norm(text)
    low = t.lower()
    names = {n.lower() for n in known_names if n}

    # Short answers to a pending question.
    words = low.split()
    if len(words) <= 4 and CONFIRM.match(low) and not re.search(r"\b(send|connect|find)\b", low):
        return Intent("confirm", raw)
    if len(words) <= 4 and DENY.match(low):
        return Intent("deny", raw)
    m = re.match(r"^(?:the |number |option |#)?(\d|first|second|third|fourth|last|1st|2nd|3rd)(?: one)?$", low)
    if m:
        v = m.group(1)
        return Intent("choose", raw, choice=int(v) - 1 if v.isdigit() else ORDINALS[v])

    # Device kind and model hint.
    device_kind = None
    for kind, ws in DEVICE_WORDS.items():
        if _find_word(low, ws):
            device_kind = kind
            break
    device_hint = _find_word(low, MODEL_HINTS)

    # Person.
    person, pronoun = None, False
    rel = relation_hint(low)
    if rel:
        m = re.search(rf"\bmy {rel}s? ([a-z][\w\-]+?)(?:'s?\b|\b)", low.replace("colleague", "coworker"))
        if m:
            person = m.group(1)
    if not person:
        for m in re.finditer(r"\b([a-z][\w\-]*)'s?\b", low):
            cand = m.group(1)
            if cand not in NOT_NAMES and (not names or cand in names or not _find_word(cand, sum(DEVICE_WORDS.values(), []))):
                person = cand
                break
    if not person and names:
        for n in sorted(names, key=len, reverse=True):
            if re.search(rf"\b{re.escape(n)}\b", low):
                person = n
                break
    if not person:
        m = re.search(r"\b(?:send|share|give|forward|airdrop|text)\s+([a-z][\w\-]+)\s+(?:the|my|a|an|that|this|those|these|it|some|our)\b", low) or re.search(
            r"\b(?:to|with)\s+([a-z][\w\-]+)\b(?!\s*(?:laptop|phone|mac|ipad))", low
        )
        if m and m.group(1) not in STOP and m.group(1) not in PRONOUN_PEOPLE:
            person = m.group(1)
    if not person:
        pm = re.search(r"\b(his|her|their|him|them)\b", low)
        if pm:
            person, pronoun = pm.group(1), True
        elif re.search(r"\bmy (own )?(headphones|airpods|earbuds|buds|speaker|laptop|phone)\b", low):
            person = "my"

    instead = bool(re.search(r"\binstead\b|\brather\b", low))

    # Action.
    if re.search(r"\bdisconnect\b", low):
        action = "disconnect"
    elif re.search(r"^(send|share|airdrop|give|forward|transfer|drop|text|email)\b", low) or re.search(r"\b(send|share|airdrop)\b", low):
        action = "send"
    elif re.search(r"^(find|search|look for|look up|where'?s|where is|locate|show me|pull up|open|get me|which)\b", low):
        action = "find"
    elif re.search(r"\b(connect|pair|hook up|switch (?:audio|sound|output) to|play (?:it |this |music )?(?:on|through)|listen (?:on|with)|use)\b", low):
        action = "connect"
    elif instead or (pronoun and device_kind):
        action = "retarget"
    elif device_kind in ("headphones", "speaker"):
        action = "connect"
    else:
        action = "unknown"
    if action == "send" and instead and re.search(r"\b(it|that|this)\b", low):
        action = "retarget"

    # File query (send / find / retarget).
    fq: Optional[FileQuery] = None
    if action in ("send", "find", "retarget", "unknown"):
        fq = FileQuery()
        since, until, label, rest = parse_time(t, now)
        fq.since, fq.until, fq.time_label = since, until, label
        rl = rest.lower()
        for kind, (ws, exts) in FILE_KINDS.items():
            if _find_word(rl, ws):
                fq.kinds.append(kind)
                fq.exts |= exts
        fq.names = re.findall(r'"([^"]+)"', rest)
        m = re.search(r"\b(?:about|regarding|on the topic of|related to|re:|covering|on)\s+(.+)$", rest, re.I)
        topic_src = m.group(1) if m else rest
        # Strip trailing recipient/device clauses: "... to Daniel", "... to his laptop".
        recipients = "|".join(re.escape(n) for n in (names | {"him", "her", "them", "his", "their"}))
        topic_src = re.split(rf"\b(?:to|with|for)\s+(?:my |his |her |their )?(?:{recipients})\b", topic_src, flags=re.I)[0]
        if person and person not in PRONOUN_PEOPLE and person != "my":
            topic_src = re.sub(rf"\b{re.escape(person)}('s)?\b", " ", topic_src, flags=re.I)
        all_kind_words = ({w for ws, _ in FILE_KINDS.values() for w in ws} | {w for ws in DEVICE_WORDS.values() for w in ws}) - TOPICAL_KIND_WORDS
        toks = [w for w in re.findall(r"[\w][\w\-\.]*", topic_src) if w.lower() not in STOP and w.lower() not in all_kind_words and w.lower() not in PRONOUN_PEOPLE]
        fq.topic = " ".join(toks).strip()
        if not fq.topic and not fq.kinds and not fq.names and (re.search(r"\b(it|that|this|the file|same)\b", low) or action == "retarget"):
            fq.refers_previous = True
        if action == "unknown" and not fq.is_empty() and person:
            action = "send"
    if action == "retarget" and not device_kind:
        device_kind = None

    return Intent(
        action=action,
        text=raw,
        person=person,
        pronoun=pronoun or (person in PRONOUN_PEOPLE if person else False),
        relation=rel,
        device_kind=device_kind,
        device_hint=device_hint,
        file=fq,
        instead=instead,
    )
