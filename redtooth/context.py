"""Conversation memory: carries the selected file, person and device forward
so follow-ups like "send it to his laptop instead" just work."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .models import Device, FileHit, Person, Question


@dataclass
class Context:
    person: Optional[Person] = None  # last person mentioned / acted on
    file: Optional[FileHit] = None  # currently selected file
    files: list[FileHit] = field(default_factory=list)  # last result list
    device: Optional[Device] = None  # last destination / audio device
    pending: Optional[Question] = None
    last_transfer_id: Optional[str] = None
    updated: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.updated = time.time()

    def set_person(self, p: Optional[Person]) -> None:
        if p:
            self.person = p
            self.touch()

    def set_file(self, f: Optional[FileHit]) -> None:
        if f:
            self.file = f
            self.touch()

    def stale(self, seconds: float = 30 * 60) -> bool:
        return time.time() - self.updated > seconds
