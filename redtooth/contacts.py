"""The requester's contacts, and resolving a spoken name to a person.

Sources, all merged into one book (deduplicated by phone number):
- Redtooth's own contact store (~/.redtooth/contacts.json), filled by importing in the
  panel: drop a .vcf or .csv export (Google, iCloud, Outlook, Android, iPhone), add a
  person by hand, or send a .vcf from your own phone with Redtooth;
- the macOS Contacts database; Outlook contacts on Windows, when Outlook is installed;
- any extra files listed in the config. Resolution ranks by name match, then uses relation ("my friend
Daniel"), whether the person uses Redtooth, and proximity (one of their
devices is nearby) to break ties.
"""
from __future__ import annotations

import difflib
import csv
import glob
import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .models import Contact, Person, Profile
from .util import new_id, normalize_phone

log = logging.getLogger("redtooth.contacts")


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
        return cls.from_records(json.loads(_read_text(path)))

    @classmethod
    def from_vcard(cls, path: str | Path) -> "ContactBook":
        return cls.parse_vcard(_read_text(path))

    @classmethod
    def parse_vcard(cls, text: str) -> "ContactBook":
        """vCard 2.1/3.0/4.0 as exported by iPhone/iCloud, Android, Google and Outlook."""
        book = cls()
        # Unfold continuation lines (RFC 6350: a line starting with a space or tab continues the previous one).
        lines: list[str] = []
        for raw in text.replace("\r\n", "\n").split("\n"):
            if raw[:1] in (" ", "\t") and lines:
                lines[-1] += raw[1:]
            elif raw.strip():
                lines.append(raw.strip())
        cur: dict | None = None
        for line in lines:
            key, _, val = line.partition(":")
            params = key.split(";")
            k = params[0].split(".")[-1].upper()  # "item1.TEL" (Apple) -> "TEL"
            if any(p.upper() in ("ENCODING=QUOTED-PRINTABLE", "QUOTED-PRINTABLE") for p in params[1:]):
                val = _unquote_printable(val)
            val = val.replace("\\,", ",").replace("\\;", ";").strip()
            if k == "BEGIN":
                cur = {"phones": [], "raw": [], "nick": [], "name": "", "n": ""}
            elif k == "END" and cur is not None:
                name = cur["name"] or cur["n"]
                if name and cur["phones"]:
                    book.add(Contact(new_id("c_"), name, cur["phones"], cur["nick"], raw_phones=cur["raw"]))
                cur = None
            elif cur is None:
                continue
            elif k == "FN":
                cur["name"] = val
            elif k == "N" and val.replace(";", ""):
                parts = val.split(";") + ["", ""]
                cur["n"] = " ".join(x for x in (parts[1], parts[0]) if x)  # given, family
            elif k == "TEL":
                raw = val.removeprefix("tel:").strip()
                num = normalize_phone(raw)
                if len(num) > 4 and num not in cur["phones"]:
                    cur["phones"].append(num)
                    cur["raw"].append(raw)
            elif k == "NICKNAME":
                cur["nick"] += [n.strip() for n in val.split(",") if n.strip()]
        return book

    @classmethod
    def from_csv(cls, path: str | Path) -> "ContactBook":
        return cls.parse_csv(_read_text(path))

    @classmethod
    def parse_csv(cls, text: str) -> "ContactBook":
        """Google Contacts and Outlook CSV exports (any CSV with a name and phone columns)."""
        book = cls()
        for row in csv.DictReader(io.StringIO(text)):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            name = row.get("name") or row.get("display name") or row.get("full name") or " ".join(
                x for x in (row.get("first name") or row.get("given name"), row.get("middle name") or row.get("additional name"),
                            row.get("last name") or row.get("family name")) if x)
            phones: list[str] = []
            raws: list[str] = []
            for col, val in row.items():
                if "phone" in col and "type" not in col and "label" not in col and val:
                    for part in val.split(":::"):  # Google puts several numbers in one cell
                        num = normalize_phone(part.strip())
                        if len(num) > 4 and num not in phones:
                            phones.append(num)
                            raws.append(part.strip())
            nick = [n for n in (row.get("nickname"),) if n]
            if name and phones:
                book.add(Contact(new_id("c_"), name, phones, nick, raw_phones=raws))
        return book

    @classmethod
    def from_file(cls, path: str | Path) -> "ContactBook":
        p = Path(path)
        suffix = p.suffix.lower()
        if suffix in (".vcf", ".vcard"):
            return cls.from_vcard(p)
        if suffix == ".csv":
            return cls.from_csv(p)
        return cls.from_json(p)

    @classmethod
    def from_bytes(cls, name: str, data: bytes) -> "ContactBook":
        text = _decode(data)
        if name.lower().endswith(".csv"):
            return cls.parse_csv(text)
        if name.lower().endswith(".json"):
            return cls.from_records(json.loads(text))
        return cls.parse_vcard(text)

    @classmethod
    def from_records(cls, data: list[dict]) -> "ContactBook":
        """Records from Redtooth's store or a JSON file. Numbers as written ("phones_raw") are
        re-read with the current country; normalized ones ("phones") are kept for anything else."""
        out = cls()
        for c in data:
            if not c.get("name"):
                continue
            raws = [str(p) for p in c.get("phones_raw") or c.get("phones", [])]
            phones: list[str] = []
            for r in raws:
                n = normalize_phone(r)
                if len(n) > 4 and n not in phones:
                    phones.append(n)
            out.add(Contact(id=c.get("id") or new_id("c_"), name=c["name"], phones=phones, nicknames=c.get("nicknames", []),
                            relation=c.get("relation"), last_interaction=c.get("last_interaction"), raw_phones=raws))
        return out

    def to_records(self) -> list[dict]:
        return [{"id": c.id, "name": c.name, "phones": c.phones, "phones_raw": c.raw_phones or c.phones, "nicknames": c.nicknames,
                 "relation": c.relation, "last_interaction": c.last_interaction} for c in self.contacts]

    def merge(self, other: "ContactBook") -> int:
        """Add contacts not already present; a known phone number or exact name gains the new numbers. Returns added."""
        by_phone = {p: c for c in self.contacts for p in c.phones}
        by_name = {c.name.lower(): c for c in self.contacts}
        added = 0
        for c in other.contacts:
            hit = next((by_phone[p] for p in c.phones if p in by_phone), None) or by_name.get(c.name.lower())
            if hit:
                for i, p in enumerate(c.phones):
                    if p not in hit.phones:
                        hit.phones.append(p)
                        if c.raw_phones and len(c.raw_phones) == len(c.phones):
                            hit.raw_phones = (hit.raw_phones or list(hit.phones[:-1])) + [c.raw_phones[i]]
                        by_phone[p] = hit
                for n in c.nicknames:
                    if n not in hit.nicknames:
                        hit.nicknames.append(n)
                continue
            self.contacts.append(c)
            by_name[c.name.lower()] = c
            for p in c.phones:
                by_phone[p] = c
            added += 1
        return added

    @classmethod
    def from_outlook(cls, timeout: float = 20) -> "ContactBook":
        """Windows: the default Outlook contacts folder, read through Outlook's COM interface (if Outlook is installed)."""
        if not sys.platform.startswith("win"):
            return cls()
        script = (
            "$ErrorActionPreference='Stop'; $o = New-Object -ComObject Outlook.Application; "
            "$f = $o.GetNamespace('MAPI').GetDefaultFolder(10); "
            "@($f.Items | Where-Object { $_.Class -eq 40 } | ForEach-Object { [pscustomobject]@{ name = $_.FullName; nick = $_.NickName; "
            "phones = @($_.MobileTelephoneNumber, $_.HomeTelephoneNumber, $_.BusinessTelephoneNumber, $_.PrimaryTelephoneNumber) "
            "| Where-Object { $_ } } }) | ConvertTo-Json -Depth 3 -Compress"
        )
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                 capture_output=True, text=True, timeout=timeout,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
            rows = json.loads(out) if out else []
        except Exception as e:
            log.info("Outlook contacts unavailable: %s", e)
            return cls()
        rows = [rows] if isinstance(rows, dict) else rows
        return cls.from_records([{"name": r.get("name"), "phones": r.get("phones") or [], "nicknames": [r["nick"]] if r.get("nick") else []}
                                 for r in rows if r.get("name") and r.get("phones")])

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


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            text = data.decode(enc)
            if enc == "utf-16" or "\x00" not in text:
                return text
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _read_text(path: str | Path) -> str:
    return _decode(Path(path).read_bytes())


def _unquote_printable(v: str) -> str:
    import quopri

    try:
        return quopri.decodestring(v.encode()).decode("utf-8", "replace")
    except Exception:
        return v


class ContactStore:
    """Redtooth's own copy of your contacts: what you imported in the panel or sent from your phone."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> ContactBook:
        try:
            return ContactBook.from_records(json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return ContactBook()
        except Exception as e:
            log.warning("contacts store unreadable (%s); starting empty", e)
            return ContactBook()

    def save(self, book: ContactBook) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(book.to_records(), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


EXPORT_NAMES = re.compile(r"(contact|vcard|people|adressbuch|contactos|kontakte)", re.I)


def find_exports(dirs: Optional[list[Path]] = None, limit: int = 5) -> list[dict]:
    """Contact exports sitting in Downloads/Desktop/Documents, newest first, so the panel can offer them."""
    home = Path.home()
    dirs = dirs or [home / "Downloads", home / "Desktop", home / "Documents"]
    found = []
    for d in dirs:
        try:
            for p in d.iterdir():
                suf = p.suffix.lower()
                if p.is_file() and (suf in (".vcf", ".vcard") or (suf == ".csv" and EXPORT_NAMES.search(p.name))) and p.stat().st_size < 50 * 1024**2:
                    found.append(p)
        except OSError:
            continue
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in found[:limit]:
        try:
            n = len(ContactBook.from_file(p).contacts)
        except Exception:
            n = 0
        if n:
            out.append({"path": str(p), "name": p.name, "count": n})
    return out


def load_all(store: ContactStore, extra_files: Iterable[str] = (), outlook: bool = True) -> ContactBook:
    """Everything Redtooth knows, merged. A missing or broken file is skipped with a warning, never fatal."""
    book = store.load()
    for f in extra_files:
        p = Path(f).expanduser()
        if not p.exists():
            log.warning("contacts file not found, skipping: %s", p)
            continue
        try:
            book.merge(ContactBook.from_file(p))
        except Exception as e:
            log.warning("couldn't read contacts from %s: %s", p, e)
    if sys.platform == "darwin":
        book.merge(ContactBook.from_macos())
    elif outlook and sys.platform.startswith("win"):
        book.merge(ContactBook.from_outlook())
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
