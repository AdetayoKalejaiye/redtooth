"""A complete simulated world: you are Alex; Daniel has the app, an iPhone,
a MacBook Air and Sony WH-1000XM5s. Daniel's devices run real receivers on
localhost, so transfers, integrity checks, resume and timings are real;
only the Bluetooth radio is simulated.

Scenarios (switch from the panel's demo menu):
  registered   Daniel registered his headphones in the app and they're in range
  not_pairing  registered, but not in pairing mode: nudge + keep scanning
  no_app       Daniel doesn't use Redtooth: ask "Are Daniel's the Sony XM5s?"
"""
from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .agent import Agent
from .bt.sim import SimBluetooth, SimWorld
from .contacts import ContactBook
from .directory import InMemoryDirectory
from .files.embed import load_embedder
from .files.index import FileIndex
from .models import Contact, Device, Endpoint, Person
from .notify import AcceptBroker
from .samples import make_samples
from .store import Store
from .transfer.receiver import Receiver
from .util import normalize_phone

ALEX = normalize_phone("(555) 010-0100")
DANIEL = normalize_phone("(555) 014-2000")
DANIEL_KIM = normalize_phone("(555) 018-8000")
PRIYA = normalize_phone("(555) 016-7000")
XM5_ADDR = "88:C9:E8:12:34:56"


@dataclass
class DemoWorld:
    agent: Agent
    world: SimWorld
    directory: InMemoryDirectory
    root: Path
    receivers: dict[str, Receiver] = field(default_factory=dict)
    daniel_broker: Optional[AcceptBroker] = None
    daniel_auto_accept: float = 2.5
    scenario: str = "registered"
    daniel_devices: list[Device] = field(default_factory=list)

    async def set_scenario(self, name: str) -> None:
        a, w = self.agent, self.world
        self.scenario = name
        if a.watch_task:
            a.watch_task.cancel()
        a.ctx.pending = None
        # Reset Daniel's headphones: not paired with Alex, forgotten.
        w.paired.discard("bt:xm5")
        w.connected.discard("bt:xm5")
        for r in a.store.remembered():
            if r.owner_phone == DANIEL:
                a.store.forget(r.id)
        if w.output.address == XM5_ADDR:
            from .bt.base import AudioOutput

            w.output = AudioOutput("MacBook Pro Speakers")
        xm5 = w.radios["bt:xm5"]
        if name == "no_app":
            self.directory.profiles.pop(_h(DANIEL), None)
            xm5.pairing_mode = True
        else:
            await self.directory.register(DANIEL, "Daniel Ortiz")
            await self.directory.set_devices(DANIEL, [Device.from_dict(d.to_dict()) for d in self.daniel_devices])
            xm5.pairing_mode = name == "registered"
        await a.refresh_presence(scan=True)

    def set_pairing_mode(self, on: bool) -> None:
        self.world.set_pairing_mode("bt:xm5", on)


def _h(phone: str) -> str:
    from .util import phone_hash

    return phone_hash(phone)


async def build_demo(root: Optional[str] = None, daniel_auto_accept: float = 2.5, embedder: Optional[str] = None, radio_delay: float = 0.15) -> DemoWorld:
    # Alex's files live in ".../Alex/Documents", so questions say "Alex › Documents", not a temp folder name.
    root_p = Path(root) if root else Path(tempfile.mkdtemp(prefix="redtooth-demo-")) / "Alex"
    root_p.mkdir(parents=True, exist_ok=True)
    make_samples(root_p)
    directory = InMemoryDirectory()

    # Bluetooth world around Alex's MacBook.
    world = SimWorld(delay=radio_delay)
    world.add(Device("bt:airpods", "Alex's AirPods Pro", "headphones", address="F0:B3:EC:01:02:03"), pairing_mode=False, rssi=-48)
    world.paired.add("bt:airpods")
    world.add(Device("bt:xm5", "WH-1000XM5", "headphones", address=XM5_ADDR), pairing_mode=True, rssi=-52)
    world.add(Device("bt:jbl", "JBL Flip 6", "speaker", address="00:42:79:AA:BB:CC"), pairing_mode=True, rssi=-80)
    world.add(Device("bt:priya-pods", "Priya's AirPods", "headphones", address="70:F0:87:11:22:33"), pairing_mode=True, rssi=-71)
    world.add(Device("bt:daniel-iphone", "Daniel's iPhone", "phone", address="A4:83:E7:44:55:66"), rssi=-50)
    bt = SimBluetooth(world)

    # Contacts on Alex's phone/computer.
    book = ContactBook(
        [
            Contact("c_daniel", "Daniel Ortiz", [DANIEL], ["Dan"], "friend", 1.0),
            Contact("c_dkim", "Daniel Kim", [DANIEL_KIM], [], "coworker"),
            Contact("c_priya", "Priya Shah", [PRIYA], [], "coworker", 1.0),
            Contact("c_mom", "Mom", [normalize_phone("(555) 019-1111")], [], "family"),
        ]
    )
    me = Person(Contact("c_me", "Alex Rivera", [ALEX]), is_self=True)
    await directory.register(ALEX, "Alex Rivera")
    await directory.register(PRIYA, "Priya Shah")

    index = FileIndex(root_p / "index.db", load_embedder(embedder))
    from .files.usage import Usage

    index.usage = Usage(":memory:")
    index.usage.recent_source = dict  # the demo world has no OS history
    await asyncio.get_running_loop().run_in_executor(None, index.scan, [root_p / "Documents", root_p / "Downloads"])
    store = Store(root_p / "state.db")
    store.remember(world.radios["bt:airpods"].device, ALEX, "Alex Rivera")
    agent = Agent(me, book, directory, bt, index, store, scan_seconds=1.0)

    demo = DemoWorld(agent, world, directory, root_p, daniel_auto_accept=daniel_auto_accept)

    # Daniel's (and Priya's) devices run real receivers.
    broker = AcceptBroker(native=False)
    demo.daniel_broker = broker

    async def daniel_accept(offer):
        if demo.daniel_auto_accept > 0:
            async def later():
                await asyncio.sleep(demo.daniel_auto_accept)
                broker.resolve(offer.id, True)

            asyncio.create_task(later())
        return await broker.ask(offer)

    def peer_events(owner: str):
        def emit(kind: str, data: dict) -> None:
            agent.bus.publish("peer_incoming", {**data, "owner": owner})

        return emit

    daniel_devs = []
    for dev_id, name, kind in (("d_iphone", "Daniel's iPhone", "phone"), ("d_mba", "Daniel's MacBook Air", "laptop")):
        dev = Device(dev_id, name, kind, model={"phone": "iPhone 16", "laptop": "MacBook Air"}[kind], has_app=True, wifi_direct=kind == "laptop")
        rx = Receiver(dev, root_p / "Daniel" / name, daniel_accept, peer_events("Daniel"))
        port = await rx.start("127.0.0.1")
        dev.endpoints = [Endpoint("lan", "127.0.0.1", port)]
        demo.receivers[dev_id] = rx
        daniel_devs.append(dev)
    daniel_devs.append(Device("bt:xm5", "WH-1000XM5", "headphones", address=XM5_ADDR, model="Sony WH-1000XM5", short_model="Sony XM5s"))
    demo.daniel_devices = daniel_devs

    priya_dev = Device("p_iphone", "Priya's iPhone", "phone", model="iPhone 15 Pro", has_app=True)
    prx = Receiver(priya_dev, root_p / "Priya", daniel_accept, peer_events("Priya"))
    priya_dev.endpoints = [Endpoint("lan", "127.0.0.1", await prx.start("127.0.0.1"))]
    demo.receivers["p_iphone"] = prx
    await directory.set_devices(PRIYA, [priya_dev])

    # Daniel's app shows nudges ("put your headphones in pairing mode").
    directory.subscribe(DANIEL, lambda msg: agent.bus.publish("peer_nudge", {**msg, "owner": "Daniel"}))

    # Watch the demo folders so files added or edited there show up in search within seconds.
    from .files.indexer import Indexer

    loop = asyncio.get_running_loop()
    indexer = Indexer(index, [root_p / "Documents", root_p / "Downloads"], workers=0,
                      on_status=lambda s: loop.call_soon_threadsafe(agent.bus.publish, "index", s))
    agent.indexer = indexer
    indexer.start(first_pass=False)

    await demo.set_scenario("registered")
    return demo
