"""Always-listening receiver: no "receive mode" to turn on.

Incoming offers raise a notification (Accept / Decline) and an event for the
panel. Bytes are verified chunk by chunk as they arrive, journaled so an
interrupted transfer resumes where it stopped, and the whole file is checked
against the sender's SHA-256 before it is saved and the save is confirmed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..models import Device
from ..util import now_ms
from .protocol import CHUNK_HASHES, ProtocolError, chunk_ok, read_frame, send_frame, tune

SYNC_EVERY = 8 * 1024 * 1024


@dataclass
class Offer:
    id: str
    name: str
    size: int
    mime: str
    fingerprint: str
    sender_name: str
    sender_phone: str = ""
    to_device: str = ""
    received: int = 0
    status: str = "waiting"  # waiting | receiving | saved | declined | failed | interrupted
    saved_path: str = ""
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


Acceptor = Callable[[Offer], Awaitable[bool]]
EventSink = Callable[[str, dict], None]


def _safe_name(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/")).strip().lstrip(".") or "file"
    return "".join(c for c in name if c not in '<>:"|?*\x00')[:200]


def _unique(dirpath: Path, name: str) -> Path:
    p = dirpath / name
    stem, suf, i = p.stem, p.suffix, 1
    while p.exists():
        p = dirpath / f"{stem} ({i}){suf}"
        i += 1
    return p


class Receiver:
    def __init__(self, device: Device, inbox: str | Path, acceptor: Acceptor, events: Optional[EventSink] = None, accept_timeout: float = 120):
        self.device = device
        self.inbox = Path(inbox)
        self.partial = self.inbox / ".redtooth-partial"
        self.partial.mkdir(parents=True, exist_ok=True)
        self.acceptor = acceptor
        self.events = events or (lambda kind, data: None)
        self.accept_timeout = accept_timeout
        self.offers: dict[str, Offer] = {}
        self.server: Optional[asyncio.base_events.Server] = None
        self.port = 0

    async def start(self, host: str = "0.0.0.0", port: int = 0) -> int:
        self.server = await asyncio.start_server(self._handle, host, port, limit=16 * 1024 * 1024)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    # journal ------------------------------------------------------------
    def _journal(self, tid: str) -> Path:
        return self.partial / f"{tid}.json"

    def _part(self, tid: str) -> Path:
        return self.partial / f"{tid}.part"

    def _load_journal(self, tid: str) -> Optional[dict]:
        try:
            return json.loads(self._journal(tid).read_text())
        except Exception:
            return None

    def _save_journal(self, tid: str, data: dict) -> None:
        tmp = self._journal(tid).with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self._journal(tid))

    # connection ---------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tune(writer)
        try:
            hdr, _ = await read_frame(reader, timeout=10)
            if hdr.get("type") == "PING":
                await send_frame(writer, {"type": "PONG", "device_id": self.device.id, "name": self.device.name})
            elif hdr.get("type") == "OFFER":
                await self._receive(hdr, reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError, ProtocolError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _decide(self, offer: Offer, reader: asyncio.StreamReader) -> Optional[bool]:
        """Ask the user; give up if the sender hangs up or cancels meanwhile."""
        decide = asyncio.create_task(asyncio.wait_for(self.acceptor(offer), self.accept_timeout))
        watch = asyncio.create_task(read_frame(reader))
        done, _ = await asyncio.wait({decide, watch}, return_when=asyncio.FIRST_COMPLETED)
        if decide in done:
            watch.cancel()
            try:
                await watch  # must finish before anyone else reads this stream
            except (asyncio.CancelledError, Exception):
                pass
            try:
                return bool(decide.result())
            except asyncio.TimeoutError:
                return False
        decide.cancel()
        return None  # sender left

    async def _receive(self, hdr: dict, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tid = str(hdr["id"])[:64]
        size = int(hdr["size"])
        frm = hdr.get("from", {})
        offer = self.offers.get(tid) or Offer(
            tid, _safe_name(hdr["name"]), size, hdr.get("mime", ""), hdr.get("fingerprint", ""), frm.get("name", "Someone"), frm.get("phone", ""), self.device.id
        )
        self.offers[tid] = offer
        journal = self._load_journal(tid)
        resuming = bool(journal and journal.get("fingerprint") == offer.fingerprint and journal.get("accepted"))
        if resuming:
            offset = int(journal["verified"])
            offer.status = "receiving"
        else:
            offer.status = "waiting"
            self.events("incoming", offer.to_dict())
            await send_frame(writer, {"type": "WAITING"})
            ok = await self._decide(offer, reader)
            if ok is None:
                offer.status = "cancelled"
                self.events("incoming", offer.to_dict())
                return
            if not ok:
                offer.status = "declined"
                self.events("incoming", offer.to_dict())
                await send_frame(writer, {"type": "DECLINE", "reason": "declined"})
                return
            offset = 0
            self._save_journal(tid, {"fingerprint": offer.fingerprint, "accepted": True, "verified": 0, "name": offer.name, "size": size})

        part = self._part(tid)
        part.touch()
        sha = hashlib.sha256()
        with open(part, "r+b") as f:
            f.truncate(offset)
            f.seek(0)
            left = offset
            while left:  # re-hash the verified prefix so the full-file check still covers it
                buf = f.read(min(left, 4 << 20))
                sha.update(buf)
                left -= len(buf)
            f.seek(offset)
            t_accept = now_ms()
            await send_frame(writer, {"type": "ACCEPT", "offset": offset, "hashes": list(CHUNK_HASHES)})
            offer.status, offer.received = "receiving", offset
            self.events("incoming", offer.to_dict())
            t_first: Optional[float] = None
            expected, unsynced, last_evt = offset, 0, 0.0
            while True:
                try:
                    h, payload = await read_frame(reader, timeout=30)
                except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
                    self._checkpoint(tid, f, expected)  # everything written so far is verified
                    offer.status = "interrupted"
                    self.events("incoming", offer.to_dict())
                    raise
                typ = h.get("type")
                if typ == "DATA":
                    if int(h["offset"]) != expected or not chunk_ok(h, payload):
                        await send_frame(writer, {"type": "ERROR", "reason": "chunk_mismatch", "resume_offset": self._checkpoint(tid, f, expected)})
                        return
                    if t_first is None:
                        t_first = now_ms()
                    f.write(payload)
                    sha.update(payload)
                    expected += len(payload)
                    unsynced += len(payload)
                    if unsynced >= SYNC_EVERY:
                        self._checkpoint(tid, f, expected)
                        unsynced = 0
                    offer.received = expected
                    if now_ms() - last_evt > 150:
                        last_evt = now_ms()
                        self.events("incoming", offer.to_dict())
                elif typ == "END":
                    if expected != size or sha.hexdigest() != h.get("sha256"):
                        part.unlink(missing_ok=True)
                        self._journal(tid).unlink(missing_ok=True)
                        offer.status = "failed"
                        self.events("incoming", offer.to_dict())
                        await send_frame(writer, {"type": "ERROR", "reason": "hash_mismatch", "resume_offset": 0})
                        return
                    f.flush()
                    os.fsync(f.fileno())
                    break
                elif typ == "CANCEL":
                    offer.status = "cancelled"
                    self.events("incoming", offer.to_dict())
                    return
        final = _unique(self.inbox, offer.name)
        os.replace(part, final)
        self._journal(tid).unlink(missing_ok=True)
        t_saved = now_ms()
        offer.status, offer.saved_path = "saved", str(final)
        self.events("incoming", offer.to_dict())
        await send_frame(
            writer,
            {
                "type": "SAVED",
                "name": final.name,
                "sha256": sha.hexdigest(),
                "accept_to_first_byte_ms": round((t_first or t_saved) - t_accept, 2),
                "first_byte_to_saved_ms": round(t_saved - (t_first or t_saved), 2),
                "resumed_from": offset,
            },
        )

    def _checkpoint(self, tid: str, f, verified: int) -> int:
        f.flush()
        os.fsync(f.fileno())
        j = self._load_journal(tid) or {}
        j["verified"] = verified
        self._save_journal(tid, j)
        return verified
