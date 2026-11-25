"""Find the device a person means, search-first.

Order of evidence, cheapest first:
  1. remembered associations (we connected to Daniel's XM5s before)
  2. devices registered to the owner's Redtooth profile (Daniel registered them)
  3. devices paired with this computer
  4. discoverable devices nearby (scan)

Outcomes:
  known         -> connect right away, verify it became the audio output
  confirm       -> one plausible new device: "Are Daniel's the Sony XM5s?"
  choose        -> several plausible devices
  pairing_mode  -> nothing plausible: ask the owner to enter pairing mode, keep scanning
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from .bt.base import BluetoothPlatform, norm_addr
from .metrics import Metrics
from .models import AUDIO_KINDS, Device, Person
from .store import Store
from .util import now_ms

KIND_GROUPS = {"headphones": {"headphones", "earbuds"}, "earbuds": {"headphones", "earbuds"}, "speaker": {"speaker"}}


def kind_ok(dev: Device, kind: Optional[str]) -> bool:
    if not kind:
        return dev.kind in AUDIO_KINDS
    return dev.kind in KIND_GROUPS.get(kind, {kind})


def hint_ok(dev: Device, hint: Optional[str]) -> bool:
    if not hint:
        return False
    h = hint.lower()
    return h in (dev.name or "").lower() or h in (dev.model or "").lower() or h in (dev.short_model or "").lower()


@dataclass
class Survey:
    remembered: list[Device] = field(default_factory=list)
    registered: list[Device] = field(default_factory=list)
    paired: list[Device] = field(default_factory=list)
    nearby: list[Device] = field(default_factory=list)
    scanned: bool = False


@dataclass
class Outcome:
    kind: str  # known | confirm | choose | pairing_mode | connected | failed
    device: Optional[Device] = None
    candidates: list[Device] = field(default_factory=list)
    verified: bool = False
    reason: str = ""
    survey: Optional[Survey] = None
    ms: float = 0.0


class DeviceResolver:
    def __init__(self, bt: BluetoothPlatform, store: Store, metrics: Metrics, me: Person, scan_seconds: float = 4.0):
        self.bt, self.store, self.metrics, self.me = bt, store, metrics, me
        self.scan_seconds = scan_seconds

    def _owner_phone(self, owner: Person) -> Optional[str]:
        return self.me.phone if owner.is_self else owner.phone

    @staticmethod
    def _merge(dev: Device, *others: Device) -> Device:
        for o in others:
            for s in o.sources:
                if s not in dev.sources:
                    dev.sources.append(s)
            dev.paired = dev.paired or o.paired
            dev.connected = dev.connected or o.connected
            dev.discoverable = dev.discoverable or o.discoverable
            dev.rssi = dev.rssi if dev.rssi is not None else o.rssi
        return dev

    def _same(self, a: Device, b: Device) -> bool:
        return a.id == b.id or (bool(a.address) and norm_addr(a.address) == norm_addr(b.address))

    def _find_in(self, dev: Device, pool: list[Device]) -> Optional[Device]:
        return next((p for p in pool if self._same(dev, p)), None)

    async def find(self, owner: Person, kind: Optional[str], hint: Optional[str] = None) -> Outcome:
        t0 = now_ms()
        phone = self._owner_phone(owner)
        survey = Survey()
        scan_task = asyncio.create_task(self.bt.scan(self.scan_seconds))
        survey.paired = await self.bt.paired()
        survey.remembered = [d for d in self.store.remembered(phone) if kind_ok(d, kind)] if phone else []
        survey.registered = [d for d in (owner.profile.devices if owner.profile else []) if kind_ok(d, kind)]

        # 1-2. Known and already paired: connect without waiting for the scan.
        for src in (survey.remembered, survey.registered):
            for d in src:
                p = self._find_in(d, survey.paired)
                if p:
                    scan_task.cancel()
                    return Outcome("known", self._merge(d, p), survey=survey, ms=now_ms() - t0)
        if owner.is_self:
            own = [p for p in survey.paired if kind_ok(p, kind) and not self._owned_by_other(p) and (not hint or hint_ok(p, hint))]
            if own:
                scan_task.cancel()
                own.sort(key=lambda d: (not d.connected, -(d.rssi or -100)))
                return Outcome("known", own[0], survey=survey, ms=now_ms() - t0)

        # 3. Scan for discoverable devices.
        try:
            survey.nearby = await scan_task
        except Exception:
            survey.nearby = []
        survey.scanned = True
        for src in (survey.registered, survey.remembered):
            for d in src:
                n = self._find_in(d, survey.nearby)
                if n:
                    return Outcome("known", self._merge(d, n), survey=survey, ms=now_ms() - t0)

        # 4. Plausible new matches.
        cands = self.plausible(owner, kind, hint, survey)
        ms = now_ms() - t0
        if not cands:
            return Outcome("pairing_mode", survey=survey, ms=ms)
        if len(cands) == 1 or cands[0][0] - cands[1][0] >= 0.25:
            return Outcome("confirm", cands[0][1], [c for _, c in cands], survey=survey, ms=ms)
        return Outcome("choose", None, [c for _, c in cands[:4]], survey=survey, ms=ms)

    def _owned_by_other(self, dev: Device, owner_phone: Optional[str] = None) -> bool:
        for r in self.store.remembered():
            if self._same(r, dev) and r.owner_phone and r.owner_phone != owner_phone:
                return True
        return False

    def plausible(self, owner: Person, kind: Optional[str], hint: Optional[str], survey: Survey) -> list[tuple[float, Device]]:
        phone = self._owner_phone(owner)
        pool = [d for d in survey.nearby if d.discoverable and kind_ok(d, kind) and not self._owned_by_other(d, phone)]
        registered_models = {(d.model or d.name).lower() for d in survey.registered}
        scored = []
        for d in pool:
            s = 0.1
            if owner.first.lower() in d.name.lower():
                s += 0.6
            if hint_ok(d, hint):
                s += 0.3
            if (d.model or d.name).lower() in registered_models:
                s += 0.5
            if d.rssi is not None and d.rssi > -65:
                s += 0.1
            scored.append((s, d))
        if len(scored) == 1:
            scored[0] = (scored[0][0] + 0.2, scored[0][1])
        # Names that clearly belong to someone else ("Priya's AirPods") are not plausible.
        scored = [(s, d) for s, d in scored if not ("'s " in d.name and owner.first.lower() not in d.name.lower())]
        scored.sort(key=lambda x: -x[0])
        return scored

    async def connect(self, dev: Device, owner: Person) -> Outcome:
        t0 = now_ms()
        if not dev.paired:
            if not await self.bt.pair(dev):
                return Outcome("failed", dev, reason="pair", ms=now_ms() - t0)
            dev.paired = True
        if not await self.bt.connect(dev):
            return Outcome("failed", dev, reason="connect", ms=now_ms() - t0)
        dev.connected = True
        verified = await self.bt.verify_audio_output(dev) if dev.kind in AUDIO_KINDS else True
        self.store.remember(dev, self._owner_phone(owner), owner.name)
        ms = now_ms() - t0
        self.metrics.record("connect_ms", ms, verified=verified)
        return Outcome("connected", dev, verified=verified, ms=ms)

    async def watch(self, owner: Person, kind: Optional[str], hint: Optional[str], timeout: float = 90, interval: float = 2.0) -> AsyncIterator[Outcome]:
        """Keep scanning after asking for pairing mode; yield once something plausible shows up."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = await self.find(owner, kind, hint)
            if out.kind != "pairing_mode":
                yield out
                return
            await asyncio.sleep(interval)
