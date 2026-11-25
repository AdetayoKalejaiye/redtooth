"""Local HTTP API + live events for the panel. Binds to 127.0.0.1.

Every POST must carry the `X-Redtooth: 1` header. Browsers can't add a
custom header cross-origin without a CORS preflight (which this server never
approves), so other websites can't drive the agent.
"""
from __future__ import annotations

import asyncio
import json
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


def make_app(agent: Agent, broker: Optional[AcceptBroker] = None, demo=None, extra_routes=None) -> web.Application:
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

    @r.post("/api/my-devices/register")
    async def register_device(request):
        """Register one of your paired devices so friends' requests resolve to it."""
        b = await body(request)
        dev = next((d for d in await agent.bt.paired() if d.id == b.get("device_id")), None)
        if not dev or not agent.me.phone:
            raise web.HTTPNotFound()
        agent.shared_devices = [d for d in agent.shared_devices if d.id != dev.id] + [dev]
        if agent.persist_shared:
            agent.persist_shared()
        if hasattr(agent.directory, "upsert_device"):
            await agent.directory.upsert_device(agent.me.phone, dev)
        agent.bus.publish("state", await agent.state())
        return web.json_response({"ok": True})

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


def wire_incoming(agent: Agent):
    """Event sink for this device's own Receiver."""

    def emit(kind: str, data: dict) -> None:
        agent.bus.publish(kind, data)

    return emit
