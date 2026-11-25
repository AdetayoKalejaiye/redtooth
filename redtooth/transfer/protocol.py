"""Wire format for Redtooth transfers (see docs/PROTOCOL.md).

Every frame: 4-byte big-endian header length, a UTF-8 JSON header, then
`len` payload bytes for DATA frames.

  sender -> OFFER {id, name, size, mime, fingerprint, chunk, from}
  recv   -> WAITING {}                          (shown as "Waiting for Daniel")
  recv   -> ACCEPT {offset, hashes} | DECLINE {reason}  (offset > 0 resumes; hashes = chunk
            hashes the receiver can verify, in preference order; missing means ["b2"])
  sender -> DATA {offset, len, b2|s256} + bytes (chunk hash: BLAKE2b-128 or SHA-256, hex)
  sender -> END {sha256}
  recv   -> SAVED {name, sha256, accept_to_first_byte_ms, first_byte_to_saved_ms}
  recv   -> ERROR {reason, resume_offset}
  either -> CANCEL {}
  probe:    PING {} -> PONG {device_id, name}
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import struct
from typing import Optional

MAX_HEADER = 64 * 1024
MAX_CHUNK = 8 * 1024 * 1024
CHUNK = 1024 * 1024


class ProtocolError(Exception):
    pass


def b2(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def s256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Chunk hashes this implementation can verify, best first. Phones (Swift/Kotlin) use "s256":
# neither platform ships BLAKE2.
CHUNK_HASHES = {"b2": b2, "s256": s256}


def chunk_ok(hdr: dict, payload: bytes) -> bool:
    for name, fn in CHUNK_HASHES.items():
        if name in hdr:
            return fn(payload) == hdr[name]
    return False


async def send_frame(writer: asyncio.StreamWriter, header: dict, payload: bytes = b"") -> None:
    if payload:
        header = {**header, "len": len(payload)}
    raw = json.dumps(header, separators=(",", ":")).encode()
    writer.write(struct.pack(">I", len(raw)) + raw)
    if payload:
        writer.write(payload)
    await writer.drain()


async def read_frame(reader: asyncio.StreamReader, timeout: Optional[float] = None) -> tuple[dict, bytes]:
    async def _read():
        n = struct.unpack(">I", await reader.readexactly(4))[0]
        if n > MAX_HEADER:
            raise ProtocolError("header too large")
        hdr = json.loads(await reader.readexactly(n))
        ln = int(hdr.get("len", 0))
        if ln > MAX_CHUNK:
            raise ProtocolError("chunk too large")
        return hdr, (await reader.readexactly(ln) if ln else b"")

    return await (asyncio.wait_for(_read(), timeout) if timeout else _read())


def tune(writer: asyncio.StreamWriter) -> None:
    import socket

    sock = writer.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        except OSError:
            pass
    writer.transport.set_write_buffer_limits(high=8 * 1024 * 1024)
