"""Send a file: offer, wait for acceptance, stream with per-chunk integrity,
resume after interruptions, and report delivery only after the receiver
confirms the save."""
from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..metrics import Metrics
from ..models import Device, Person
from ..util import drop_owner, new_id, now_ms, possessive
from .protocol import CHUNK, CHUNK_HASHES, read_frame, send_frame, tune
from .routes import Route, Router

STATUS_LABELS = {
    "preparing": "Checking route",
    "waiting": "Waiting for {first}",
    "sending": "Sending",
    "interrupted": "Interrupted · resuming",
    "verifying": "Verifying",
    "saved": "Saved",
    "declined": "Declined",
    "failed": "Couldn't send",
    "cancelled": "Cancelled",
}


@dataclass
class Transfer:
    id: str
    path: str
    name: str
    size: int
    person: Person
    device: Device
    status: str = "preparing"
    sent: int = 0
    rate_bps: float = 0.0
    route: Optional[Route] = None
    error: str = ""
    saved_name: str = ""
    resumes: int = 0
    timings: dict = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    via: str = "redtooth"  # redtooth (app to app) | bluetooth (OBEX push to a phone without the app)

    @property
    def label(self) -> str:
        whose = "your" if self.person.is_self else possessive(self.person.first)
        dev = drop_owner(self.device.label(), self.person.first)
        if self.status == "sending" and self.size:
            return f"Sending · {int(100 * self.sent / self.size)}%"
        if self.status == "saved":
            return f"Saved to {whose} {dev}"
        if self.via == "bluetooth" and self.status == "waiting":
            return f"Tap Accept on {whose} {dev}"
        if self.via == "bluetooth" and self.status == "preparing":
            return f"Connecting to {whose} {dev}"
        return STATUS_LABELS.get(self.status, self.status).format(first=self.person.first)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "sent": self.sent,
            "progress": (self.sent / self.size) if self.size else 1.0,
            "status": self.status,
            "label": self.label,
            "rate_mbps": round(self.rate_bps * 8 / 1e6, 1),
            "person": {"id": self.person.contact.id, "name": self.person.name, "first": self.person.first},
            "device": {"id": self.device.id, "label": self.device.label(), "kind": self.device.kind},
            "route": self.route.to_dict() if self.route else None,
            "error": self.error,
            "saved_name": self.saved_name,
            "resumes": self.resumes,
            "timings": {k: round(v, 1) for k, v in self.timings.items()},
            "via": self.via,
        }


def fingerprint(path: str) -> str:
    st = os.stat(path)
    return hashlib.sha1(f"{os.path.basename(path)}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for buf in iter(lambda: f.read(4 << 20), b""):
            h.update(buf)
    return h.hexdigest()


class Sender:
    def __init__(self, me_name: str, me_phone: str, router: Router, metrics: Metrics, on_update: Callable[[Transfer], None], max_attempts: int = 8):
        self.me_name, self.me_phone = me_name, me_phone
        self.router = router
        self.metrics = metrics
        self.on_update = on_update
        self.max_attempts = max_attempts
        self.transfers: dict[str, Transfer] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    def start(self, path: str, person: Person, device: Device) -> Transfer:
        st = os.stat(path)
        t = Transfer(new_id("t_"), path, os.path.basename(path), st.st_size, person, device)
        self.transfers[t.id] = t
        self.tasks[t.id] = asyncio.create_task(self._run(t))
        return t

    def start_bluetooth(self, path: str, person: Person, device: Device, push: Callable) -> Transfer:
        """Plain-Bluetooth file push (OBEX) to a paired phone that doesn't run Redtooth."""
        st = os.stat(path)
        t = Transfer(new_id("t_"), path, os.path.basename(path), st.st_size, person, device, via="bluetooth")
        self.transfers[t.id] = t
        self.tasks[t.id] = asyncio.create_task(self._run_bluetooth(t, push))
        return t

    async def _run_bluetooth(self, t: Transfer, push: Callable) -> None:
        import threading

        from .obex import ObexError

        loop = asyncio.get_running_loop()
        cancel = threading.Event()
        t.__dict__["_cancel"] = cancel
        t0, accepted = now_ms(), [0.0]

        def apply(n: int, stage: str) -> None:
            if stage == "waiting":
                self._set(t, "waiting")
            elif stage == "sending":
                if not accepted[0]:
                    accepted[0] = now_ms()
                    t.timings["accept_to_first_byte_ms"] = 0.0
                el = max(1e-3, (now_ms() - accepted[0]) / 1000)
                self._set(t, "sending", sent=n, rate_bps=n / el)
            elif stage == "saved":
                if accepted[0]:
                    t.timings["first_byte_to_saved_ms"] = now_ms() - accepted[0]
                self._set(t, "saved", sent=t.size)

        def progress(n: int, stage: str) -> None:
            loop.call_soon_threadsafe(apply, n, stage)

        self._set(t, "preparing")
        try:
            await loop.run_in_executor(None, push, t.device.address, t.path, progress, cancel)
            await asyncio.sleep(0)  # let the last progress callback land
            if t.status != "saved":
                self._set(t, "saved", sent=t.size)
            self.metrics.record("bluetooth_push_ms", now_ms() - t0, size=t.size)
        except ObexError as e:
            self._set(t, {"declined": "declined", "cancelled": "cancelled"}.get(e.kind, "failed"), error=str(e))
        except asyncio.CancelledError:
            cancel.set()
            self._set(t, "cancelled")
            raise
        except Exception as e:
            self._set(t, "failed", error=str(e))

    def cancel(self, tid: str) -> bool:
        task = self.tasks.get(tid)
        t = self.transfers.get(tid)
        if t is not None and "_cancel" in t.__dict__:
            t.__dict__["_cancel"].set()
        if task and not task.done():
            task.cancel()
            if t:
                t.status = "cancelled"
                self.on_update(t)
            return True
        return False

    def _set(self, t: Transfer, status: str, **kw) -> None:
        t.status = status
        for k, v in kw.items():
            setattr(t, k, v)
        self.on_update(t)

    async def _run(self, t: Transfer) -> None:
        loop = asyncio.get_running_loop()
        t0 = now_ms()
        full_hash = loop.run_in_executor(None, sha256_file, t.path)  # hashed while we wait for acceptance
        fp = fingerprint(t.path)
        delay = 0.3
        for attempt in range(self.max_attempts):
            try:
                route = await self.router.get(t.device, fresh=attempt > 0)
                if not route:
                    raise ConnectionError("no route")
                t.route = route
                done = await self._attempt(t, route, fp, full_hash, t0)
                if done:
                    return
                return  # declined
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError, asyncio.IncompleteReadError, asyncio.TimeoutError, RetryFrom) as e:
                if t.status in ("declined", "failed", "cancelled"):
                    return
                t.resumes += 1 if t.sent else 0
                self._set(t, "interrupted", error=str(e) or e.__class__.__name__)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5)
        self._set(t, "failed", error=t.error or "unreachable")

    async def _attempt(self, t: Transfer, route: Route, fp: str, full_hash, t0: float) -> bool:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(route.endpoint.host, route.endpoint.port, limit=1 << 20), 5)
        tune(writer)
        try:
            mime = mimetypes.guess_type(t.name)[0] or "application/octet-stream"
            await send_frame(writer, {"type": "OFFER", "id": t.id, "name": t.name, "size": t.size, "mime": mime, "fingerprint": fp, "chunk": CHUNK, "from": {"name": self.me_name, "phone": self.me_phone}})
            while True:
                hdr, _ = await read_frame(reader, timeout=300)
                typ = hdr.get("type")
                if typ == "WAITING":
                    self._set(t, "waiting")
                    t.timings.setdefault("offered_ms", now_ms() - t0)
                elif typ == "DECLINE":
                    self._set(t, "declined")
                    return False
                elif typ == "ACCEPT":
                    break
                else:
                    raise ConnectionError(f"unexpected {typ}")
            offset = int(hdr.get("offset", 0))
            alg = next((a for a in hdr.get("hashes", ["b2"]) if a in CHUNK_HASHES), None)
            if alg is None:
                raise ConnectionError("no common chunk hash")
            hash_fn = CHUNK_HASHES[alg]
            t_accept = now_ms()
            self._set(t, "sending", sent=offset)
            last_evt, rate_t, rate_b = 0.0, now_ms(), offset
            with open(t.path, "rb") as f:
                f.seek(offset)
                pos = offset
                while pos < t.size:
                    buf = f.read(CHUNK)
                    if not buf:
                        break
                    await send_frame(writer, {"type": "DATA", "offset": pos, alg: hash_fn(buf)}, buf)
                    pos += len(buf)
                    t.sent = pos
                    if now_ms() - last_evt > 120:
                        el = (now_ms() - rate_t) / 1000
                        t.rate_bps = (pos - rate_b) / el if el > 0 else 0
                        last_evt = now_ms()
                        self.on_update(t)
            self._set(t, "verifying")
            await send_frame(writer, {"type": "END", "sha256": await full_hash})
            hdr, _ = await read_frame(reader, timeout=120)
            if hdr.get("type") == "ERROR":
                raise RetryFrom(int(hdr.get("resume_offset", 0)))
            if hdr.get("type") != "SAVED":
                raise ConnectionError("no save confirmation")
            el = (now_ms() - t_accept) / 1000
            t.rate_bps = (t.size - offset) / el if el > 0 else 0
            t.timings.update(
                accept_to_first_byte_ms=float(hdr.get("accept_to_first_byte_ms", 0)),
                first_byte_to_saved_ms=float(hdr.get("first_byte_to_saved_ms", 0)),
                transfer_total_ms=now_ms() - t0,
            )
            for k in ("accept_to_first_byte_ms", "first_byte_to_saved_ms", "transfer_total_ms"):
                self.metrics.record(k, t.timings[k], bytes=t.size, route=route.endpoint.kind)
            self._set(t, "saved", saved_name=hdr.get("name", t.name), sent=t.size)
            return True
        finally:
            writer.close()


class RetryFrom(Exception):
    def __init__(self, offset: int):
        super().__init__(f"receiver asked to resume from {offset}")
        self.offset = offset
