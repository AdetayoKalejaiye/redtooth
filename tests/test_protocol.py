import asyncio
import hashlib
import os

import pytest

import redtooth.transfer.receiver as receiver_mod
from redtooth.metrics import Metrics
from redtooth.models import Contact, Device, Endpoint, Person
from redtooth.store import Store
from redtooth.transfer.protocol import s256
from redtooth.transfer.receiver import Receiver
from redtooth.transfer.routes import Router
from redtooth.transfer.sender import Sender


@pytest.mark.asyncio
@pytest.mark.parametrize("receiver_hashes", [["b2", "s256"], ["s256"]])
async def test_sender_uses_a_chunk_hash_the_receiver_supports(tmp_path, monkeypatch, receiver_hashes):
    from redtooth.transfer.protocol import CHUNK_HASHES

    monkeypatch.setattr(receiver_mod, "CHUNK_HASHES", {k: CHUNK_HASHES[k] for k in receiver_hashes})  # e.g. a phone: SHA-256 only
    seen = []
    real = receiver_mod.chunk_ok

    def spy(h, payload):
        seen.append(next(k for k in ("b2", "s256") if k in h))
        return real(h, payload)

    monkeypatch.setattr(receiver_mod, "chunk_ok", spy)
    src = tmp_path / "f.bin"
    src.write_bytes(os.urandom(3 << 20))
    dev = Device("d1", "Phone", "phone")

    async def accept(o):
        return True

    rx = Receiver(dev, tmp_path / "in", accept)
    dev.endpoints = [Endpoint("lan", "127.0.0.1", await rx.start("127.0.0.1"))]
    m = Metrics(Store())
    s = Sender("Alex", "+15550100", Router(None, m), m, lambda t: None)
    t = s.start(str(src), Person(Contact("c", "Dan")), dev)
    await s.tasks[t.id]
    assert t.status == "saved"
    assert set(seen) == {receiver_hashes[0]}
    assert hashlib.sha256((tmp_path / "in" / "f.bin").read_bytes()).hexdigest() == hashlib.sha256(src.read_bytes()).hexdigest()
    await rx.stop()
