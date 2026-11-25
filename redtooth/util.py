"""Small shared helpers: phone numbers, time labels, ids."""
from __future__ import annotations

import hashlib
import re
from typing import Optional
import time
import uuid
from datetime import datetime, timedelta

def normalize_phone(raw: str, country: Optional[str] = None) -> str:
    """E.164 (+15550142000). Numbers without a country code are read as the user's country
    (see phones.py; the phone apps use the same rules so lookups hash the same string)."""
    from .phones import normalize

    return normalize(raw, country)


def phone_hash(phone: str) -> str:
    """Contact discovery sends hashes, not raw numbers."""
    return hashlib.sha256(("redtooth:" + normalize_phone(phone)).encode()).hexdigest()


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


def now_ms() -> float:
    return time.perf_counter() * 1000.0


def initials(name: str) -> str:
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    return "".join(p[0].upper() for p in parts[:2]) or "?"


def first_name(name: str) -> str:
    return name.strip().split()[0] if name.strip() else name


def possessive(name: str) -> str:
    return name + ("'" if name.endswith("s") else "'s")


def when_label(ts: float, now: datetime | None = None) -> str:
    """'9:12 AM', 'Yesterday 4:05 PM', 'Mon', 'Nov 3'."""
    now = now or datetime.now().astimezone()
    dt = datetime.fromtimestamp(ts).astimezone()
    t = dt.strftime("%I:%M %p").lstrip("0")
    if dt.date() == now.date():
        return t
    if dt.date() == (now - timedelta(days=1)).date():
        return f"Yesterday {t}"
    if (now - dt).days < 7:
        return dt.strftime("%a ") + t
    return dt.strftime("%b %d").replace(" 0", " ")


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
