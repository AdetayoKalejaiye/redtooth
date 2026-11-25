"""The requester's contacts, and resolving a spoken name to a person.

Sources: a JSON file, vCard (.vcf) exports, and on macOS the local Contacts
database. Resolution ranks by name match, then uses relation ("my friend
Daniel"), whether the person uses Redtooth, and proximity (one of their
devices is nearby) to break ties.
"""
from __future__ import annotations

import difflib
import glob
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .models import Contact, Person, Profile
from .util import new_id, normalize_phone


class ContactBook:
    def __init__(self, contacts: Iterable[Contact] = ()):
        self.contacts: list[Contact] = list(contacts)

    def add(self, c: Contact) -> None:
        self.contacts.append(c)

    def by_id(self, cid: str) -> Optional[Contact]:
        return next((c for c in self.contacts if c.id == cid), None)

    def by_phone(self, phone: str) -> Optional[Contact]:
        p = normalize_phone(phone)
        return next((c for c in self.contacts if p in c.phones), None)

    # ---- loading --------------------------------------------------------
    @classmethod
    def from_json(cls, path: str | Path) -> "ContactBook":
        data = json.loads(Path(path).read_text())
        return cls(
            Contact(
                id=c.get("id") or new_id("c_"),
                name=c["name"],
                phones=[normalize_phone(p) for p in c.get("phones", [])],
                nicknames=c.get("nicknames", []),
                relation=c.get("relation"),
                last_interaction=c.get("last_interaction"),
            )
            for c in data
        )

    @classmethod
    def from_vcard(cls, path: str | Path) -> "ContactBook":
        book = cls()
        cur: dict | None = None
        for raw in Path(path).read_text(errors="ignore").splitlines():
            line = raw.strip()
            key, _, val = line.partition(":")
            k = key.split(";")[0].upper()
            if k == "BEGIN":
                cur = {"phones": [], "nick": []}
            elif k == "END" and cur is not None:
                if cur.get("name"):
                    book.add(Contact(new_id("c_"), cur["name"], cur["phones"], cur["nick"]))
                cur = None
            elif cur is not None and k == "FN":
                cur["name"] = val
            elif cur is not None and k == "TEL":
                cur["phones"].append(normalize_phone(val))
            elif cur is not None and k == "NICKNAME":
                cur["nick"] += [n.strip() for n in val.split(",") if n.strip()]
        return book

    @classmethod
    def from_macos(cls) -> "ContactBook":
        """Read ~/Library/Application Support/AddressBook (needs Contacts access)."""
        book = cls()
        pattern = os.path.expanduser("~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb")
        for dbp in glob.glob(pattern):
            try:
                db = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
                rows = db.execute(
                    """SELECT r.Z_PK, r.ZFIRSTNAME, r.ZLASTNAME, r.ZNICKNAME, p.ZFULLNUMBER
                       FROM ZABCDRECORD r LEFT JOIN ZABCDPHONENUMBER p ON p.ZOWNER = r.Z_PK"""
                ).fetchall()
            except sqlite3.Error:
                continue
            seen: dict[int, Contact] = {}
            for pk, first, last, nick, num in rows:
                name = " ".join(x for x in (first, last) if x)
                if not name:
                    continue
                c = seen.get(pk)
                if not c:
                    c = seen[pk] = Contact(f"mac_{pk}", name, [], [nick] if nick else [])
                    book.add(c)
                if num:
                    c.phones.append(normalize_phone(num))
        return book


@dataclass
class Candidate:
    person: Person
    score: float
    name_score: float


SELF_WORDS = {"my", "mine", "me", "i", "myself", "our"}


class PeopleResolver:
    def __init__(self, book: ContactBook, me: Person):
        self.book = book
        self.me = me
        self.profiles: dict[str, Profile] = {}  # phone -> profile (refreshed from directory)
        self.nearby_phones: set[str] = set()

    def person_for(self, c: Contact) -> Person:
        prof = next((self.profiles[p] for p in c.phones if p in self.profiles), None)
        near = any(p in self.nearby_phones for p in c.phones)
        return Person(contact=c, profile=prof, nearby=near)

    def all_people(self) -> list[Person]:
        people = [self.person_for(c) for c in self.book.contacts]
        people.sort(key=lambda p: (not p.nearby, not p.has_app, -(p.contact.last_interaction or 0), p.name))
        return people

    @staticmethod
    def _name_score(query: str, c: Contact) -> float:
        q = query.lower().strip()
        full = c.name.lower()
        parts = full.split()
        if q == full:
            return 1.0
        if q in (n.lower() for n in c.nicknames):
            return 0.9
        if parts and q == parts[0]:
            return 0.88
        if q in parts:  # last name
            return 0.75
        if full.startswith(q) and len(q) >= 3:
            return 0.7
        best = max([difflib.SequenceMatcher(None, q, w).ratio() for w in parts + [full]] or [0])
        return 0.6 * best if best >= 0.8 else 0.0

    def resolve(self, query: str, relation: Optional[str] = None) -> list[Candidate]:
        if query.lower() in SELF_WORDS:
            return [Candidate(self.me, 1.0, 1.0)]
        out = []
        for c in self.book.contacts:
            ns = self._name_score(query, c)
            if ns <= 0:
                continue
            p = self.person_for(c)
            s = ns
            if relation and c.relation and relation.lower() == c.relation.lower():
                s += 0.15
            if p.nearby:
                s += 0.12
            if p.has_app:
                s += 0.08
            if c.last_interaction:
                s += 0.02
            out.append(Candidate(p, s, ns))
        out.sort(key=lambda x: -x.score)
        return out

    @staticmethod
    def is_ambiguous(cands: list[Candidate]) -> bool:
        if len(cands) < 2:
            return False
        a, b = cands[0], cands[1]
        return b.name_score >= 0.85 * a.name_score and (a.score - b.score) < 0.12


_REL = re.compile(r"\bmy (friend|coworker|colleague|brother|sister|mom|dad|partner|roommate|boss)\b", re.I)


def relation_hint(text: str) -> Optional[str]:
    m = _REL.search(text)
    if not m:
        return None
    rel = m.group(1).lower()
    return "coworker" if rel == "colleague" else rel
