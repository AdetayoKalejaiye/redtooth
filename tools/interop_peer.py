"""Desktop peer for cross-language tests (Kotlin / Swift clients).

  python tools/interop_peer.py receive INBOX [--hashes s256]   prints {"port": N}, then one JSON line per saved file
  python tools/interop_peer.py send FILE HOST PORT DEVICE_ID   prints {"status": ..., "saved_name": ...}
"""
import asyncio
import json
import sys

from redtooth.metrics import Metrics
from redtooth.models import Contact, Device, Endpoint, Person
from redtooth.store import Store


async def receive(inbox: str, hashes: list[str]) -> None:
    import redtooth.transfer.receiver as rmod
    from redtooth.transfer.protocol import CHUNK_HASHES

    rmod.CHUNK_HASHES = {k: CHUNK_HASHES[k] for k in hashes}

    async def accept(offer):
        return True

    def events(kind, data):
        if data.get("status") == "saved":
            print(json.dumps({"saved": data["saved_path"], "name": data["name"]}), flush=True)

    rx = rmod.Receiver(Device("py_desktop", "Python desktop", "laptop"), inbox, accept, events)
    port = await rx.start("127.0.0.1")
    print(json.dumps({"port": port}), flush=True)
    await asyncio.Event().wait()


async def send(path: str, host: str, port: int, device_id: str) -> None:
    from redtooth.transfer.routes import Router
    from redtooth.transfer.sender import Sender

    m = Metrics(Store())
    s = Sender("Python desktop", "+15550100", Router(None, m), m, lambda t: None)
    dev = Device(device_id, "Kotlin phone", "phone", endpoints=[Endpoint("lan", host, port)])
    t = s.start(path, Person(Contact("c", "Kotlin Tester")), dev)
    await s.tasks[t.id]
    print(json.dumps({"status": t.status, "saved_name": t.saved_name, "error": t.error}), flush=True)


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "receive":
        hashes = sys.argv[sys.argv.index("--hashes") + 1].split(",") if "--hashes" in sys.argv else ["b2", "s256"]
        asyncio.run(receive(sys.argv[2], hashes))
    else:
        asyncio.run(send(sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]))
