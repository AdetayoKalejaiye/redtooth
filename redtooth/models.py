"""Plain data types shared across Redtooth."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# Device kinds the agent reasons about.
AUDIO_KINDS = {"headphones", "earbuds", "speaker"}
APP_KINDS = {"phone", "laptop", "desktop", "tablet"}

# Friendly model names for common Bluetooth audio products. OS names are often
# terse ("WH-1000XM5", "LE_WH-1000XM5"); questions should read naturally.
MODEL_NAMES = [
    (r"WH-?1000XM5", "Sony WH-1000XM5", "Sony XM5s"),
    (r"WH-?1000XM4", "Sony WH-1000XM4", "Sony XM4s"),
    (r"WF-?1000XM5", "Sony WF-1000XM5", "Sony XM5 earbuds"),
    (r"AirPods Max", "AirPods Max", "AirPods Max"),
    (r"AirPods Pro", "AirPods Pro", "AirPods Pro"),
    (r"AirPods", "AirPods", "AirPods"),
    (r"QC ?45|QuietComfort 45", "Bose QuietComfort 45", "Bose QC45s"),
    (r"QC ?Ultra|QuietComfort Ultra", "Bose QuietComfort Ultra", "Bose QC Ultras"),
    (r"Galaxy Buds", "Samsung Galaxy Buds", "Galaxy Buds"),
    (r"Pixel Buds", "Google Pixel Buds", "Pixel Buds"),
    (r"Beats|Powerbeats|Studio Buds", "Beats", "Beats"),
    (r"JBL ?Flip", "JBL Flip", "JBL Flip"),
    (r"Jabra", "Jabra headset", "Jabra headset"),
]


@dataclass
class Endpoint:
    """A network address where an app device accepts transfers."""

    kind: str  # "lan" | "wifi_direct" | "loopback"
    host: str
    port: int

    def key(self) -> str:
        return f"{self.kind}:{self.host}:{self.port}"


@dataclass
class Device:
    id: str
    name: str
    kind: str = "other"
    address: Optional[str] = None  # Bluetooth MAC, if any
    model: Optional[str] = None  # friendly model, e.g. "Sony WH-1000XM5"
    short_model: Optional[str] = None  # conversational, e.g. "Sony XM5s"
    owner_phone: Optional[str] = None
    owner_name: Optional[str] = None
    paired: bool = False
    connected: bool = False
    discoverable: bool = False
    rssi: Optional[int] = None
    has_app: bool = False
    endpoints: list[Endpoint] = field(default_factory=list)
    wifi_direct: bool = False
    sources: list[str] = field(default_factory=list)  # remembered|paired|nearby|directory
    last_seen: Optional[float] = None

    def label(self) -> str:
        return self.model or self.name

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["label"] = self.label()
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Device":
        d = dict(d)
        d.pop("label", None)
        d["endpoints"] = [Endpoint(**e) if isinstance(e, dict) else e for e in d.get("endpoints", [])]
        return Device(**d)


@dataclass
class Profile:
    """A registered Redtooth user, keyed by phone number."""

    phone: str
    name: str
    devices: list[Device] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"phone": self.phone, "name": self.name, "devices": [d.to_dict() for d in self.devices]}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Profile":
        return Profile(d["phone"], d["name"], [Device.from_dict(x) for x in d.get("devices", [])])


@dataclass
class Contact:
    id: str
    name: str
    phones: list[str] = field(default_factory=list)
    nicknames: list[str] = field(default_factory=list)
    relation: Optional[str] = None  # friend, coworker, family…
    last_interaction: Optional[float] = None
    raw_phones: list[str] = field(default_factory=list)  # as written in the source, so a country change can re-read them


@dataclass
class Person:
    """A resolved recipient/owner: a contact, plus their profile if they use Redtooth."""

    contact: Contact
    profile: Optional[Profile] = None
    nearby: bool = False
    is_self: bool = False

    @property
    def name(self) -> str:
        return self.contact.name

    @property
    def first(self) -> str:
        return self.contact.name.split()[0]

    @property
    def phone(self) -> Optional[str]:
        if self.profile:
            return self.profile.phone
        return self.contact.phones[0] if self.contact.phones else None

    @property
    def has_app(self) -> bool:
        return self.profile is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.contact.id,
            "name": self.name,
            "first": self.first,
            "relation": self.contact.relation,
            "has_app": self.has_app,
            "nearby": self.nearby,
            "devices": [d.to_dict() for d in (self.profile.devices if self.profile else [])],
        }


@dataclass
class FileHit:
    file_id: int
    path: str
    name: str
    kind: str
    ext: str
    size: int
    mtime: float
    score: float = 0.0
    snippet: str = ""
    snippet_label: str = ""  # e.g. "Slide 3"
    why: list[str] = field(default_factory=list)  # "name", "content", "meaning", "time"

    def to_dict(self) -> dict[str, Any]:
        from .util import human_bytes, when_label

        d = asdict(self)
        d["when"] = when_label(self.mtime)
        d["size_label"] = human_bytes(self.size)
        return d


@dataclass
class Option:
    id: str
    label: str
    detail: str = ""
    payload: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "detail": self.detail}


@dataclass
class Question:
    id: str
    kind: str  # choose_file | choose_person | confirm_device | choose_device | pairing_mode
    prompt: str
    options: list[Option]
    state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "prompt": self.prompt, "options": [o.to_dict() for o in self.options]}


@dataclass
class Reply:
    text: str
    status: Optional[str] = None  # short status label, e.g. "Connecting", "Waiting for Daniel"
    question: Optional[Question] = None
    files: list[FileHit] = field(default_factory=list)
    device: Optional[Device] = None
    person: Optional[Person] = None
    transfer_id: Optional[str] = None
    ok: bool = True
    timings: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "status": self.status,
            "question": self.question.to_dict() if self.question else None,
            "files": [f.to_dict() for f in self.files],
            "device": self.device.to_dict() if self.device else None,
            "person": self.person.to_dict() if self.person else None,
            "transfer_id": self.transfer_id,
            "ok": self.ok,
            "timings": {k: round(v, 1) for k, v in self.timings.items()},
        }
