"""Local HTTP API + live events for the panel. Binds to 127.0.0.1.

Every POST must carry the `X-Redtooth: 1` header. Browsers can't add a
custom header cross-origin without a CORS preflight (which this server never
approves), so other websites can't drive the agent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import os
from pathlib import Path
from typing import Optional

from aiohttp import web

from .agent import Agent
from .notify import AcceptBroker

WEB = Path(__file__).parent / "web"
OUTBOX = Path(os.environ.get("REDTOOTH_HOME", Path.home() / ".redtooth")) / "outbox"


@web.middleware
async def guard(request: web.Request, handler):
    if request.method == "POST" and request.path.startswith("/api/") and request.headers.get("X-Redtooth") != "1":
        raise web.HTTPForbidden(text="missing X-Redtooth header")
    return await handler(request)


def make_app(agent: Agent, broker: Optional[AcceptBroker] = None, demo=None, extra_routes=None, contacts=None, app_info=None) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=4 * 1024**3)
    r = web.RouteTableDef()
    incoming: dict[str, dict] = {}

    async def body(request) -> dict:
        try:
            return await request.json()
        except Exception:
            return {}

    @r.get("/")
    async def index(request):
        return web.FileResponse(WEB / "panel.html", headers={"Cache-Control": "no-store"})

    @r.get("/api/state")
    async def state(request):
        s = await agent.state()
        s["incoming"] = list(incoming.values())[-5:]
        s["demo"] = {"scenario": demo.scenario, "auto_accept": demo.daniel_auto_accept, "xm5_pairing": demo.world.radios["bt:xm5"].pairing_mode} if demo else None
        return web.json_response(s)

    @r.post("/api/ask")
    async def ask(request):
        b = await body(request)
        reply = await agent.ask(str(b.get("text", ""))[:500])
        return web.json_response(reply.to_dict())

    @r.post("/api/answer")
    async def answer(request):
        b = await body(request)
        return web.json_response((await agent.answer(b.get("question_id", ""), b.get("option_id", ""))).to_dict())

    @r.post("/api/connect")
    async def connect(request):
        b = await body(request)
        return web.json_response((await agent.connect_device(b.get("device_id", ""))).to_dict())

    @r.post("/api/send")
    async def send(request):
        b = await body(request)
        return web.json_response((await agent.send_file(b.get("person_id", ""), b.get("path"), b.get("device_id"))).to_dict())

    @r.post("/api/select")
    async def select(request):
        b = await body(request)
        return web.json_response((await agent.select_file(b.get("path", ""))).to_dict())

    @r.post("/api/stage")
    async def stage(request):
        """Drag-and-drop from the browser: stage the bytes locally, then select."""
        reader = await request.multipart()
        part = await reader.next()
        if part is None or not part.filename:
            raise web.HTTPBadRequest()
        OUTBOX.mkdir(parents=True, exist_ok=True)
        dest = OUTBOX / os.path.basename(part.filename)
        with open(dest, "wb") as f:
            while chunk := await part.read_chunk(1 << 20):
                f.write(chunk)
        return web.json_response((await agent.select_file(str(dest))).to_dict())

    @r.post("/api/cancel")
    async def cancel(request):
        b = await body(request)
        return web.json_response({"ok": agent.sender.cancel(b.get("transfer_id", ""))})

    @r.get("/api/files")
    async def files(request):
        q = request.query.get("q", "").strip()
        loop = asyncio.get_running_loop()
        hits = await loop.run_in_executor(None, (lambda: agent.index.search(q)) if q else (lambda: agent.index.recent(8)))
        return web.json_response([h.to_dict() for h in hits])

    @r.post("/api/incoming/{oid}")
    async def respond(request):
        b = await body(request)
        ok = bool(broker and broker.resolve(request.match_info["oid"], bool(b.get("accept"))))
        return web.json_response({"ok": ok})

    @r.post("/api/quit")
    async def quit_(request):
        asyncio.get_running_loop().call_later(0.3, os._exit, 0)
        return web.json_response({"ok": True})

    @r.post("/api/navigate")
    async def navigate(request):
        """The tray menu asks the open panel to show a view."""
        agent.bus.publish("navigate", {"to": str((await body(request)).get("to", "home"))})
        return web.json_response({"ok": True})

    @r.get("/api/app")
    async def app_get(request):
        from . import desktop

        info = dict(app_info() if app_info else {})
        info.update(autostart_supported=desktop.autostart_supported(), autostart=desktop.autostart_enabled() if desktop.autostart_supported() else False)
        return web.json_response(info)

    @r.post("/api/app")
    async def app_set(request):
        from . import desktop

        b = await body(request)
        if "autostart" in b and desktop.autostart_supported():
            desktop.set_autostart(bool(b["autostart"]))
        return web.json_response({"ok": True, "autostart": desktop.autostart_enabled() if desktop.autostart_supported() else False})

    @r.post("/api/link")
    async def link(request):
        """A one-time code for adding another of your devices (Settings › Link another device)."""
        from .directory import HTTPDirectory, InMemoryDirectory

        d = agent.directory
        try:
            if isinstance(d, HTTPDirectory):
                return web.json_response({"ok": True, "code": await d.link_code()})
            if isinstance(d, InMemoryDirectory) and agent.me.phone:  # this computer hosts the directory
                return web.json_response({"ok": True, "code": d.link_code(agent.me.phone)})
            return web.json_response({"ok": False, "error": "This directory doesn't use link codes."})
        except Exception as e:
            return web.json_response({"ok": False, "error": f"Couldn't reach the directory ({e})."})

    @r.post("/api/doctor")
    async def doctor(request):
        from .cli import format_doctor

        report = await agent.bt.diagnose()
        return web.json_response({"ok": True, "text": format_doctor(report), "report": report})

    @r.post("/api/scan")
    async def scan(request):
        found = await agent.scan_nearby(float((await body(request)).get("seconds", 8)))
        return web.json_response({"ok": True, "devices": [d.to_dict() for d in found]})

    @r.post("/api/pair")
    async def pair(request):
        return web.json_response((await agent.pair_device(str((await body(request)).get("device_id", "")))).to_dict())

    @r.post("/api/disconnect")
    async def disconnect(request):
        return web.json_response((await agent.disconnect_device(str((await body(request)).get("device_id", "")))).to_dict())

    @r.get("/api/my-devices")
    async def my_devices(request):
        return web.json_response({"devices": await agent.my_devices(), "me": agent.me.first})

    @r.post("/api/my-devices")
    async def set_mine(request):
        """Mark one of your paired devices as yours (or not), with the name friends will use."""
        b = await body(request)
        if not agent.me.phone:
            raise web.HTTPNotFound()
        try:
            await agent.set_mine(str(b.get("device_id", "")), bool(b.get("mine", True)), b.get("alias"))
        except KeyError:
            return web.json_response({"ok": False, "error": "That device isn't paired with this computer."})
        return web.json_response({"ok": True, "devices": await agent.my_devices()})

    @r.get("/api/contacts")
    async def contacts_status(request):
        if not contacts:
            return web.json_response({"count": len(agent.book.contacts), "managed": False})
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, contacts.status))

    @r.post("/api/contacts/import")
    async def contacts_import(request):
        if not contacts:
            raise web.HTTPNotFound()
        try:
            if request.content_type.startswith("multipart/"):
                reader = await request.multipart()
                part = await reader.next()
                data = await part.read(decode=False)
                added = contacts.import_bytes(part.filename or "contacts.vcf", bytes(data))
            else:
                added = contacts.import_path(str((await body(request)).get("path", "")))
        except Exception as e:
            return web.json_response({"ok": False, "error": f"Couldn't read that file: {e}"})
        await agent.refresh_people()
        agent.bus.publish("state", await agent.state())
        return web.json_response({"ok": True, "added": added, "count": len(agent.book.contacts)})

    @r.get("/api/countries")
    async def countries(request):
        from .phones import COUNTRIES, flag

        return web.json_response([{"iso": c.iso, "name": c.name, "cc": c.cc, "flag": flag(c.iso)} for c in sorted(COUNTRIES, key=lambda c: c.name)])

    @r.post("/api/settings/country")
    async def set_country(request):
        if not contacts:
            raise web.HTTPNotFound()
        from .phones import BY_ISO

        iso = str((await body(request)).get("country", "")).upper()
        if iso not in BY_ISO:
            return web.json_response({"ok": False, "error": "Unknown country."})
        await asyncio.get_running_loop().run_in_executor(None, contacts.set_country, iso)
        await agent.refresh_people()
        agent.bus.publish("state", await agent.state())
        return web.json_response({"ok": True, "country": iso, "count": len(agent.book.contacts)})

    @r.post("/api/contacts/add")
    async def contacts_add(request):
        if not contacts:
            raise web.HTTPNotFound()
        b = await body(request)
        name, phone = str(b.get("name", "")).strip(), str(b.get("phone", "")).strip()
        if not name or sum(ch.isdigit() for ch in phone) < 5:
            return web.json_response({"ok": False, "error": "Enter a name and a phone number."})
        contacts.add(name, phone)
        await agent.refresh_people()
        agent.bus.publish("state", await agent.state())
        return web.json_response({"ok": True, "count": len(agent.book.contacts)})

    @r.get("/api/metrics")
    async def metrics(request):
        return web.json_response(agent.metrics.summary())

    @r.get("/api/events")
    async def events(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        q = agent.bus.subscribe()
        try:
            await resp.write(b": hi\n\n")
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
                    continue
                if ev["kind"] == "incoming":
                    incoming[ev["data"]["id"]] = ev["data"]
                await resp.write(f"event: {ev['kind']}\ndata: {json.dumps(ev['data'])}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            agent.bus.unsubscribe(q)
        return resp

    if demo:

        @r.post("/api/demo/scenario")
        async def scenario(request):
            b = await body(request)
            await demo.set_scenario(b.get("name", "registered"))
            agent.bus.publish("state", await agent.state())
            return web.json_response({"ok": True, "scenario": demo.scenario})

        @r.post("/api/demo/pairing")
        async def pairing(request):
            b = await body(request)
            demo.set_pairing_mode(bool(b.get("on", True)))
            return web.json_response({"ok": True})

        @r.post("/api/demo/peer-accept")
        async def peer_accept(request):
            b = await body(request)
            return web.json_response({"ok": demo.daniel_broker.resolve(b.get("id", ""), bool(b.get("accept", True)))})

        @r.post("/api/demo/auto-accept")
        async def auto_accept(request):
            b = await body(request)
            demo.daniel_auto_accept = float(b.get("seconds", 0))
            return web.json_response({"ok": True})

        @r.post("/api/demo/interrupt")
        async def interrupt(request):
            """Drop live transfer connections to show resume."""
            n = 0
            for t in asyncio.all_tasks():
                if "Receiver._handle" in repr(t.get_coro()):
                    t.cancel()
                    n += 1
            return web.json_response({"ok": True, "dropped": n})

    app.add_routes(r)
    if extra_routes:
        app.add_routes(extra_routes)
    app.router.add_static("/static/", WEB)
    return app


def wire_incoming(agent: Agent, contacts=None):
    """Event sink for this device's own Receiver. A contacts file sent from your own phone is imported."""

    def emit(kind: str, data: dict) -> None:
        agent.bus.publish(kind, data)
        if (contacts and kind == "incoming" and data.get("status") == "saved" and data.get("sender_phone") == agent.me.phone
                and str(data.get("name", "")).lower().endswith((".vcf", ".vcard"))):
            try:
                added = contacts.import_path(data["saved_path"])
                agent.bus.publish("reply", {"ok": True, "text": f"Imported contacts from your phone ({added} new).", "timings": {}})
                asyncio.get_running_loop().create_task(agent.refresh_people())
            except Exception as e:
                logging.getLogger("redtooth").warning("contacts from phone: %s", e)

    return emit


class ContactsService:
    """Imports into Redtooth's contact store and swaps the agent's book live."""

    def __init__(self, agent: Agent, store, extra_files=(), outlook: bool = True):
        from .contacts import ContactBook

        self.agent, self.store, self.extra_files = agent, store, list(extra_files)
        self.outlook = outlook
        self._outlook_book = ContactBook()
        self._exports: tuple[float, list] = (0.0, [])

    def load(self) -> None:
        """Build the full book. Outlook (Windows) is read once here; it can take a few seconds."""
        from .contacts import ContactBook, load_all

        book = load_all(self.store, self.extra_files, outlook=False)
        if self.outlook and sys.platform.startswith("win"):
            self._outlook_book = ContactBook.from_outlook()
        book.merge(self._outlook_book)
        self.agent.set_contacts(book)

    def _reload(self) -> None:
        from .contacts import load_all

        book = load_all(self.store, self.extra_files, outlook=False)
        book.merge(self._outlook_book)
        self.agent.set_contacts(book)

    def import_book(self, book) -> int:
        own = self.store.load()
        added = own.merge(book)
        self.store.save(own)
        self._reload()
        return added

    def import_bytes(self, name: str, data: bytes) -> int:
        from .contacts import ContactBook

        book = ContactBook.from_bytes(name, data)
        if not book.contacts:
            raise ValueError("no contacts with phone numbers found")
        return self.import_book(book)

    def import_path(self, path: str) -> int:
        from .contacts import ContactBook

        p = Path(path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(str(p))
        return self.import_bytes(p.name, p.read_bytes()) if p.suffix.lower() != ".json" else self.import_book(ContactBook.from_file(p))

    def add(self, name: str, phone: str) -> None:
        from .contacts import ContactBook
        from .models import Contact
        from .util import new_id, normalize_phone

        self.import_book(ContactBook([Contact(new_id("c_"), name, [normalize_phone(phone)])]))

    on_country = None  # callback to persist the setting

    def set_country(self, iso: str) -> str:
        """Re-read every number that was written without a country code."""
        from .phones import set_country

        iso = set_country(iso)
        if self.on_country:
            self.on_country(iso)
        self.load()
        return iso

    def status(self) -> dict:
        import time

        from .contacts import find_exports
        from .phones import current_country, flag

        n = len(self.agent.book.contacts)
        if not n and time.time() - self._exports[0] > 30:
            self._exports = (time.time(), find_exports())
        c = current_country()
        return {"count": n, "managed": True, "exports": [] if n else self._exports[1],
                "country": {"iso": c.iso, "name": c.name, "cc": c.cc, "flag": flag(c.iso)},
                "platform": sys.platform, "with_app": len(self.agent.people.profiles)}
