"""The agent: turns a request into actions, asks only specific questions,
and carries the selected file, person and device forward."""
from __future__ import annotations

import asyncio
import difflib
import logging
from typing import Any, Optional

from .bt.base import BluetoothPlatform
from .contacts import ContactBook, PeopleResolver
from .context import Context
from .devices import DeviceResolver, Outcome
from .files.index import FileIndex
from .intent import FileQuery, Intent, parse
from .metrics import Metrics
from .models import AUDIO_KINDS, APP_KINDS, Device, FileHit, Option, Person, Question, Reply
from .store import Store
from .transfer.routes import Router
from .transfer.sender import Sender, Transfer
from .util import new_id, now_ms, possessive, when_label

log = logging.getLogger("redtooth.agent")
DEST_ORDER = {"phone": 0, "laptop": 1, "tablet": 2, "desktop": 3}
KIND_NOUN = {"presentation": "deck", "document": "document", "spreadsheet": "spreadsheet", "pdf": "PDF", "image": "photo", "video": "video"}


class EventBus:
    def __init__(self) -> None:
        self.queues: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.queues.discard(q)

    def publish(self, kind: str, data: Any) -> None:
        for q in list(self.queues):
            try:
                q.put_nowait({"kind": kind, "data": data})
            except asyncio.QueueFull:
                pass


class Agent:
    def __init__(self, me: Person, book: ContactBook, directory, bt: BluetoothPlatform, index: FileIndex, store: Store, scan_seconds: float = 4.0):
        self.me = me
        me.is_self = True
        self.book = book
        self.directory = directory
        self.bt = bt
        self.index = index
        self.store = store
        self.bus = EventBus()
        self.metrics = Metrics(store)
        self.people = PeopleResolver(book, me)
        self.devices = DeviceResolver(bt, store, self.metrics, me, scan_seconds)
        self.router = Router(bt, self.metrics)
        self.sender = Sender(me.name, me.phone or "", self.router, self.metrics, self._on_transfer)
        self.ctx = Context()
        self.watch_task: Optional[asyncio.Task] = None
        self.last_scan: list[Device] = []
        self.busy: Optional[str] = None
        self.indexer = None  # files.indexer.Indexer when running as a service
        self.shared_devices: list[Device] = []  # my devices friends may connect to
        self.persist_shared = None

    # ------------------------------------------------------------ plumbing
    def _on_transfer(self, t: Transfer) -> None:
        self.bus.publish("transfer", t.to_dict())

    def status(self, label: str, detail: str = "") -> None:
        self.busy = label
        self.bus.publish("status", {"label": label, "detail": detail})

    def set_contacts(self, book) -> None:
        """Swap in a new contact book (after an import) without restarting."""
        self.book = book
        self.people.book = book

    def known_names(self) -> list[str]:
        names = []
        for c in self.book.contacts:
            names += [c.name, c.name.split()[0]] + c.nicknames
        return names

    async def refresh_people(self) -> None:
        phones = [p for c in self.book.contacts for p in c.phones]
        try:
            self.people.profiles = await self.directory.lookup(phones) if phones else {}
        except Exception as e:  # directory unreachable: keep last known
            log.warning("directory lookup failed: %s", e)

    async def refresh_presence(self, scan: bool = True) -> None:
        """Who is nearby: their registered devices show up over Bluetooth or answer on the LAN."""
        await self.refresh_people()
        seen = []
        if scan:
            try:
                seen = await self.bt.scan(2.0)
            except Exception:
                seen = []
            self.last_scan = seen
        addrs = {d.address for d in seen if d.address}
        near: set[str] = set()
        for phone, prof in self.people.profiles.items():
            for d in prof.devices:
                if d.address in addrs or (d.id in self.router.cache):
                    near.add(phone)
        pings = []
        for phone, prof in self.people.profiles.items():
            for d in prof.devices:
                if d.endpoints and phone not in near:
                    pings.append((phone, self.router.prefetch(d)))
        for phone, task in pings:
            try:
                if await asyncio.wait_for(asyncio.shield(task), 2.5):
                    near.add(phone)
            except Exception:
                pass
        self.people.nearby_phones = near

    # ------------------------------------------------------------ entry points
    async def ask(self, text: str) -> Reply:
        t0 = now_ms()
        if self.ctx.stale():
            self.ctx = Context()
        intent = parse(text, self.known_names())
        if self.ctx.pending:
            r = await self._maybe_answer(intent, text)
            if r:
                return r
        handler = {
            "connect": self._connect,
            "disconnect": self._disconnect,
            "send": self._send,
            "find": self._find,
            "retarget": self._retarget,
        }.get(intent.action)
        if not handler:
            if intent.action in ("confirm", "deny", "choose"):
                return Reply("There's nothing waiting on an answer right now.", ok=False)
            return Reply("Try “Connect to Daniel's headphones”, “Send Daniel the deck I edited this morning”, or “Find the deck about customer churn”.", ok=False)
        reply = await handler(intent)
        reply.timings.setdefault("total_ms", now_ms() - t0)
        return reply

    async def answer(self, question_id: str, option_id: str) -> Reply:
        q = self.ctx.pending
        if not q or q.id != question_id:
            return Reply("That question has expired.", ok=False)
        opt = next((o for o in q.options if o.id == option_id), None)
        if not opt:
            return Reply("That isn't one of the options.", ok=False)
        return await self._resolve_pending(q, opt)

    async def _maybe_answer(self, intent: Intent, text: str) -> Optional[Reply]:
        q = self.ctx.pending
        assert q
        if intent.action == "choose" and intent.choice is not None:
            opts = [o for o in q.options if o.id not in ("no", "none")]
            if opts:
                return await self._resolve_pending(q, opts[intent.choice] if -len(opts) <= intent.choice < len(opts) else opts[-1])
        if intent.action == "confirm":
            opt = next((o for o in q.options if o.id == "yes"), None) or (q.options[0] if len(q.options) == 1 else None)
            if opt:
                return await self._resolve_pending(q, opt)
        if intent.action == "deny":
            opt = next((o for o in q.options if o.id in ("no", "none")), None)
            if opt:
                return await self._resolve_pending(q, opt)
        # "the board one", "Kickoff", "the Sony ones"
        if intent.action in ("unknown", "find", "choose") or (intent.action == "connect" and q.kind in ("choose_device", "confirm_device")):
            low = text.lower()
            best, best_s = None, 0.0
            for o in q.options:
                words = [w for w in o.label.lower().replace("—", " ").split() if len(w) > 2]
                s = sum(1 for w in words if w in low) / max(1, len(words))
                s = max(s, difflib.SequenceMatcher(None, low, o.label.lower()).ratio() * 0.8)
                if s > best_s:
                    best, best_s = o, s
            if best and best_s >= 0.34:
                return await self._resolve_pending(q, best)
        return None

    async def _resolve_pending(self, q: Question, opt: Option) -> Reply:
        self.ctx.pending = None
        st = q.state
        if q.kind == "choose_file":
            hit: FileHit = opt.payload
            self.ctx.set_file(hit)
            if st.get("person"):
                return await self._start_send(hit, st["person"], st.get("device"), st.get("t0"))
            return Reply(f"Selected {hit.name}. Who should I send it to?", files=[hit])
        if q.kind == "choose_person":
            person: Person = opt.payload
            self.ctx.set_person(person)
            intent: Intent = st["intent"]
            intent.person, intent.pronoun = "__resolved__", False
            st["resolved"] = person
            self._forced_person = person
            return await {"connect": self._connect, "send": self._send, "retarget": self._retarget}[intent.action](intent)
        if q.kind == "confirm_device":
            owner: Person = st["owner"]
            if opt.id == "yes":
                return await self._do_connect(st["device"], owner)
            rest = [d for d in st.get("candidates", []) if d.id != st["device"].id]
            if rest:
                return self._ask_device(owner, rest, st)
            return await self._pairing_mode(owner, st.get("kind"), st.get("hint"))
        if q.kind == "choose_device":
            owner = st["owner"]
            if opt.id == "none":
                return await self._pairing_mode(owner, st.get("kind"), st.get("hint"))
            return await self._do_connect(opt.payload, owner)
        return Reply("Okay.")

    # ------------------------------------------------------------ people
    _forced_person: Optional[Person] = None

    async def _person(self, intent: Intent, default_self: bool = False) -> tuple[Optional[Person], Optional[Reply]]:
        if self._forced_person:
            p, self._forced_person = self._forced_person, None
            return p, None
        if intent.pronoun or (not intent.person and self.ctx.person and not default_self):
            if self.ctx.person:
                return self.ctx.person, None
            return None, Reply("Who do you mean?", ok=False)
        if not intent.person:
            return (self.me, None) if default_self else (None, Reply("Who should I send it to?", ok=False))
        if not self.people.profiles:
            await self.refresh_people()
        cands = self.people.resolve(intent.person, intent.relation)
        if not cands:
            if not self.book.contacts:
                return None, Reply(f"I don't have your contacts yet, so I don't know who “{intent.person.title()}” is. "
                                   "Use Add contacts in the panel.", ok=False)
            return None, Reply(f"I couldn't find “{intent.person.title()}” in your contacts.", ok=False)
        if cands[0].person.is_self:
            return self.me, None
        if PeopleResolver.is_ambiguous(cands):
            opts = []
            for c in cands[:3]:
                bits = [c.person.contact.relation or "", "nearby" if c.person.nearby else "", "uses Redtooth" if c.person.has_app else ""]
                opts.append(Option(c.person.contact.id, c.person.name, " · ".join(b for b in bits if b), c.person))
            q = Question(new_id("q_"), "choose_person", f"Which {intent.person.title()}?", opts, {"intent": intent})
            self.ctx.pending = q
            return None, Reply(q.prompt, question=q)
        p = cands[0].person
        self.ctx.set_person(p)
        return p, None

    # ------------------------------------------------------------ connect
    async def _connect(self, intent: Intent) -> Reply:
        owner, err = await self._person(intent, default_self=True)
        if err:
            return err
        assert owner
        kind = intent.device_kind if intent.device_kind in AUDIO_KINDS else ("headphones" if not intent.device_kind else intent.device_kind)
        if kind in APP_KINDS:
            return Reply(f"I can send files to {possessive(owner.first)} {kind}. To play audio, ask for headphones or a speaker.", ok=False)
        noun = "your" if owner.is_self else possessive(owner.first)
        self.status("Searching", f"Looking for {noun} {kind}")
        out = await self.devices.find(owner, kind, intent.device_hint)
        self.metrics.record("find_device_ms", out.ms, outcome=out.kind)
        timings = {"find_device_ms": out.ms}
        if out.kind == "known":
            r = await self._do_connect(out.device, owner)
            r.timings.update(timings)
            return r
        st = {"owner": owner, "kind": kind, "hint": intent.device_hint, "candidates": out.candidates}
        if out.kind in ("confirm", "choose"):
            r = self._ask_device(owner, out.candidates, st)
            r.timings.update(timings)
            return r
        r = await self._pairing_mode(owner, kind, intent.device_hint)
        r.timings.update(timings)
        return r

    def _ask_device(self, owner: Person, cands: list[Device], st: dict) -> Reply:
        whose = "yours" if owner.is_self else possessive(owner.first)
        if len(cands) == 1:
            d = cands[0]
            label = d.short_model or d.model or d.name
            q = Question(new_id("q_"), "confirm_device", f"Are {whose} the {label}?", [Option("yes", "Yes"), Option("no", "No")], {**st, "device": d})
        else:
            opts = [Option(d.id, d.model or d.name, f"{d.name} · signal {'strong' if (d.rssi or -99) > -65 else 'weak'}", d) for d in cands]
            opts.append(Option("none", "None of these"))
            q = Question(new_id("q_"), "choose_device", f"Which are {whose}?", opts, st)
        self.ctx.pending = q
        self.status("Waiting for you")
        return Reply(q.prompt, question=q, person=owner, status="Waiting for you")

    async def _do_connect(self, dev: Device, owner: Person) -> Reply:
        whose = "your" if owner.is_self else possessive(owner.first)
        self.status("Connecting", f"{whose} {dev.label()}")
        out: Outcome = await self.devices.connect(dev, owner)
        self.ctx.device = dev
        self.bus.publish("state", await self.state())
        if out.kind == "failed":
            if out.reason == "pair":
                return Reply(f"I couldn't pair with {dev.label()}. Ask {owner.first if not owner.is_self else 'yourself'} to hold the power button until the light flashes, then try again.", ok=False, device=dev, status="Not connected")
            return Reply(f"{whose.capitalize()} {dev.label()} isn't responding. Make sure they're on and nearby, then try again.", ok=False, device=dev, status="Not connected")
        if out.verified:
            return Reply(f"Connected to {whose} {dev.label()}. Audio is playing through them.", device=dev, person=owner, status="Audio output", timings={"connect_ms": out.ms})
        cur = await self.bt.audio_output()
        return Reply(f"Connected to {whose} {dev.label()}, but audio is still going to {cur.name}. Pick {dev.label()} as the output in Sound settings.", device=dev, person=owner, status="Connected", ok=False, timings={"connect_ms": out.ms})

    async def _pairing_mode(self, owner: Person, kind: Optional[str], hint: Optional[str]) -> Reply:
        kind = kind or "headphones"
        if owner.is_self:
            text = f"I don't see your {kind}. Put them in pairing mode (usually hold the power button until the light flashes) and I'll connect as soon as they appear."
        else:
            nudged = False
            if owner.has_app and owner.phone:
                try:
                    nudged = await self.directory.nudge(owner.phone, {"type": "pairing_mode", "from": self.me.name, "kind": kind, "text": f"{self.me.name} wants to connect to your {kind}. Put them in pairing mode."})
                except Exception:
                    nudged = False
            tail = f"I've sent {owner.first} a notification." if nudged else ""
            text = f"I don't see {possessive(owner.first)} {kind} nearby. Ask {owner.first} to put them in pairing mode. {tail} I'll keep looking and connect when they show up.".replace("  ", " ")
        self._start_watch(owner, kind, hint)
        return Reply(text, person=owner, status=f"Looking for {'your' if owner.is_self else possessive(owner.first)} {kind}")

    def _start_watch(self, owner: Person, kind: str, hint: Optional[str]) -> None:
        if self.watch_task and not self.watch_task.done():
            self.watch_task.cancel()

        async def run() -> None:
            async for out in self.devices.watch(owner, kind, hint):
                if out.kind == "known":
                    r = await self._do_connect(out.device, owner)
                else:
                    r = self._ask_device(owner, out.candidates, {"owner": owner, "kind": kind, "hint": hint, "candidates": out.candidates})
                self.bus.publish("reply", r.to_dict())

        self.watch_task = asyncio.create_task(run())

    async def _disconnect(self, intent: Intent) -> Reply:
        out = await self.bt.audio_output()
        dev = self.ctx.device
        for d in await self.bt.paired():
            if d.connected and (not dev or d.id == dev.id) and d.kind in AUDIO_KINDS:
                await self.bt.disconnect(d)
                self.bus.publish("state", await self.state())
                return Reply(f"Disconnected {d.label()}.", status="Disconnected")
        return Reply(f"Nothing to disconnect. Audio is on {out.name}.")

    async def connect_device(self, device_id: str) -> Reply:
        """The Connect button beside a familiar device."""
        pool = {d.id: d for d in (await self.bt.paired()) + self.store.remembered() + self.last_scan}
        dev = pool.get(device_id)
        if not dev:
            return Reply("That device isn't around anymore.", ok=False)
        rem = next((r for r in self.store.remembered() if r.id == device_id), None)
        owner = self.me
        if rem and rem.owner_phone and rem.owner_phone != self.me.phone:
            c = self.book.by_phone(rem.owner_phone)
            owner = self.people.person_for(c) if c else self.me
        if rem:
            dev = DeviceResolver._merge(rem, dev) if dev is not rem else dev
            dev.paired = any(p.id == dev.id for p in await self.bt.paired())
        return await self._do_connect(dev, owner)

    # ------------------------------------------------------------ files
    def _destinations(self, p: Person) -> list[Device]:
        devs = [d for d in (p.profile.devices if p.profile else []) if d.kind in APP_KINDS and d.endpoints]
        return sorted(devs, key=lambda d: (d.id not in self.router.cache, DEST_ORDER.get(d.kind, 9)))

    def _pick_destination(self, p: Person, kind: Optional[str]) -> Optional[Device]:
        devs = self._destinations(p)
        if kind:
            devs = [d for d in devs if d.kind == kind or (kind == "laptop" and d.kind == "desktop")]
        return devs[0] if devs else None

    async def _search(self, fq: FileQuery) -> list[FileHit]:
        loop = asyncio.get_running_loop()
        if self.indexer is not None and not self.indexer.status.complete:
            # First run still going: let the OS's own index point at likely files and index those now.
            await loop.run_in_executor(None, self.indexer.prefetch, fq)
        return await loop.run_in_executor(None, self.index.search, fq)

    def _index_note(self) -> str:
        """A short caveat while a first indexing pass is still running."""
        ix = self.indexer
        if ix is None or ix.status.complete or not ix.status.queued:
            return ""
        return f" I'm still indexing older files ({ix.status.done:,} of {ix.status.queued:,}), so I may have missed some."

    def _file_ambiguous(self, fq: FileQuery, hits: list[FileHit]) -> bool:
        if len(hits) < 2:
            return False
        if not (fq.topic or fq.names):
            return True
        return hits[1].score >= 0.85

    def _file_question(self, fq: FileQuery, hits: list[FileHit], state: dict) -> Question:
        noun = KIND_NOUN.get(fq.kinds[0], "file") if fq.kinds else "file"
        n = len(hits[:3])
        words = {2: "Two", 3: "Three"}.get(n, str(n))
        prompt = f"Which {noun}? {words} were edited {fq.time_label}." if fq.time_label else f"Which {noun} did you mean?"
        opts = [Option(f"f{h.file_id}", h.name.rsplit(".", 1)[0], f"Edited {when_label(h.mtime)}" + (f" · {h.snippet_label}" if h.snippet_label else ""), h) for h in hits[:3]]
        q = Question(new_id("q_"), "choose_file", prompt, opts, state)
        self.ctx.pending = q
        return q

    async def _send(self, intent: Intent) -> Reply:
        t0 = now_ms()
        fq = intent.file or FileQuery(refers_previous=True)

        async def person_task():
            p, err = await self._person(intent)
            if p and not p.is_self:
                for d in self._destinations(p):
                    self.router.prefetch(d)  # check routes while the user picks a file
            return p, err

        async def file_task():
            if fq.refers_previous or fq.is_empty():
                return [self.ctx.file] if self.ctx.file else []
            return await self._search(fq)

        (person, perr), hits = await asyncio.gather(person_task(), file_task())
        find_ms = now_ms() - t0
        self.metrics.record("find_match_ms", find_ms, what="file", hits=len(hits))
        timings = {"find_match_ms": find_ms}
        if perr:
            if perr.question:  # ask which person; keep the file selection
                if hits and not self._file_ambiguous(fq, hits):
                    self.ctx.set_file(hits[0])
                perr.question.state["intent"] = intent
            perr.timings = timings
            return perr
        assert person
        if person.is_self:
            return Reply("That's you. Who should I send it to?", ok=False)
        if not hits:
            recent = [h for h in self.index.recent(4)]
            msg = f"I couldn't find a {fq.describe()}.{self._index_note()}" if not fq.refers_previous else "Which file? Nothing is selected yet."
            return Reply(msg + (" Here are your most recent files." if recent else ""), files=recent, person=person, ok=False, timings=timings)
        dest = self._pick_destination(person, intent.device_kind)
        if self._file_ambiguous(fq, hits):
            q = self._file_question(fq, hits, {"person": person, "device": dest, "t0": t0})
            return Reply(q.prompt, question=q, files=hits[:3], person=person, timings=timings, status="Waiting for you")
        self.ctx.set_file(hits[0])
        r = await self._start_send(hits[0], person, dest, t0)
        r.timings.update(timings)
        return r

    async def _start_send(self, hit: FileHit, person: Person, dest: Optional[Device], t0: Optional[float] = None) -> Reply:
        self.ctx.set_person(person)
        self.ctx.set_file(hit)
        if not person.has_app:
            return Reply(f"{person.first} doesn't use Redtooth yet, so I can't deliver to their devices. Send them an invite and I'll remember their devices once they join.", person=person, files=[hit], ok=False)
        dest = dest or self._pick_destination(person, None)
        if not dest:
            return Reply(f"None of {possessive(person.first)} devices are reachable right now. I'll keep the file selected.", person=person, files=[hit], ok=False)
        self.ctx.device = dest
        t = self.sender.start(hit.path, person, dest)
        self.ctx.last_transfer_id = t.id
        return Reply(f"Sending {hit.name} to {possessive(person.first)} {dest.label()}.", person=person, files=[hit], transfer_id=t.id, status=f"Waiting for {person.first}")

    async def _retarget(self, intent: Intent) -> Reply:
        if intent.device_kind in AUDIO_KINDS:
            return await self._connect(intent)
        person, err = await self._person(intent)
        if err:
            return err
        assert person
        hit = self.ctx.file
        if not hit:
            return Reply("Which file should I send?", ok=False)
        dest = self._pick_destination(person, intent.device_kind)
        if not dest:
            kind = intent.device_kind or "device"
            return Reply(f"{person.first} doesn't have a {kind} set up with Redtooth.", person=person, ok=False)
        prev = self.sender.transfers.get(self.ctx.last_transfer_id or "")
        if prev and prev.person.contact.id == person.contact.id and prev.status not in ("saved", "declined", "failed", "cancelled"):
            self.sender.cancel(prev.id)
        r = await self._start_send(hit, person, dest)
        if r.ok and intent.instead:
            r.text = r.text.rstrip(".") + " instead."
        return r

    async def _find(self, intent: Intent) -> Reply:
        t0 = now_ms()
        fq = intent.file or FileQuery()
        if fq.is_empty():
            return Reply("What should I look for?", ok=False)
        hits = await self._search(fq)
        ms = now_ms() - t0
        self.metrics.record("find_match_ms", ms, what="file", hits=len(hits))
        if not hits:
            return Reply(f"I couldn't find a {fq.describe()}.{self._index_note()}", ok=False, timings={"find_match_ms": ms})
        self.ctx.files = hits
        if self._file_ambiguous(fq, hits):
            q = self._file_question(fq, hits, {})
            return Reply(f"I found {len(hits[:3])} that could fit. {q.prompt}", question=q, files=hits[:3], timings={"find_match_ms": ms})
        top = hits[0]
        self.ctx.set_file(top)
        where = f" — {top.snippet_label}: “{top.snippet[:80]}”" if top.snippet_label and top.snippet else ""
        return Reply(f"Found {top.name}{where}.{self._index_note()}", files=hits[:3], timings={"find_match_ms": ms})

    async def select_file(self, path: str) -> Reply:
        """Share-sheet / Send To entry: the open file arrives already selected."""
        loop = asyncio.get_running_loop()
        hit = await loop.run_in_executor(None, self.index.get, path)
        if not hit:
            return Reply("I couldn't open that file.", ok=False)
        self.ctx.set_file(hit)
        self.ctx.files = [hit]
        self.bus.publish("selected", hit.to_dict())
        return Reply(f"{hit.name} is ready to send. Who should get it?", files=[hit])

    async def send_file(self, person_id: str, path: Optional[str] = None, device_id: Optional[str] = None) -> Reply:
        """The Send file button beside a person."""
        c = self.book.by_id(person_id)
        if not c:
            return Reply("Unknown person.", ok=False)
        person = self.people.person_for(c)
        hit = self.ctx.file
        if path:
            hit = await asyncio.get_running_loop().run_in_executor(None, self.index.get, path)
        if not hit:
            self.ctx.set_person(person)
            for d in self._destinations(person):
                self.router.prefetch(d)
            return Reply(f"Which file should I send to {person.first}?", person=person)
        dest = None
        if device_id and person.profile:
            dest = next((d for d in person.profile.devices if d.id == device_id), None)
        return await self._start_send(hit, person, dest)

    # ------------------------------------------------------------ panel state
    async def state(self) -> dict:
        out = await self.bt.audio_output()
        paired = await self.bt.paired()
        remembered = self.store.remembered()
        audio: dict[str, dict] = {}
        # Familiar devices only: remembered, paired, or registered by a contact and in range right now.
        registered = {d.address: d for p in self.people.profiles.values() for d in p.devices if d.address}
        near_registered = []
        for d in self.last_scan:
            reg = registered.get(d.address)
            if reg:
                near_registered.append(DeviceResolver._merge(Device.from_dict(reg.to_dict()), d))
        for d in remembered + paired + near_registered:
            if d.kind not in AUDIO_KINDS:
                continue
            cur = audio.get(d.id, {})
            dd = d.to_dict()
            dd["owner_name"] = d.owner_name or cur.get("owner_name")
            dd["paired"] = any(p.id == d.id for p in paired)
            dd["connected"] = any(p.id == d.id and p.connected for p in paired)
            dd["active"] = self.bt.output_matches(out, d)
            audio[d.id] = {**cur, **{k: v for k, v in dd.items() if v not in (None, [], "")}}
        people = [p.to_dict() for p in self.people.all_people() if p.has_app or p.nearby][:8]
        return {
            "me": {"name": self.me.name, "phone": self.me.phone},
            "audio_output": {**out.to_dict(), "device": next((d for d in audio.values() if d.get("active")), None)},
            "audio_devices": sorted(audio.values(), key=lambda d: (not d.get("active"), not d.get("connected"), d.get("owner_name") is not None, d.get("label", ""))),
            "people": people,
            "selected_file": self.ctx.file.to_dict() if self.ctx.file else None,
            "pending": self.ctx.pending.to_dict() if self.ctx.pending else None,
            "transfers": [t.to_dict() for t in sorted(self.sender.transfers.values(), key=lambda t: -t.created)[:5]],
            "shared": [d.id for d in self.shared_devices],
            "embedder": self.index.embedder.name,
            "vector_backend": self.index.vector_backend,
            "indexing": self.indexer.status.to_dict() if self.indexer else None,
            "platform": self.bt.name,
            "wifi_direct": self.bt.wifi_direct,
        }
