"""Profile directory: who uses Redtooth, keyed by phone number, and which
devices they have registered (headphones with their Bluetooth address, phones
and laptops with their transfer endpoints).

Contact discovery sends SHA-256 hashes of numbers, never raw contacts.
Registration is verified with a one-time code (SMS delivery is pluggable;
the default sender logs the code).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional, Protocol

from .models import Device, Profile
from .util import new_id, normalize_phone, phone_hash

log = logging.getLogger("redtooth.directory")

NudgeHandler = Callable[[dict], Awaitable[None] | None]


class Directory(Protocol):
    async def register(self, phone: str, name: str) -> str: ...
    async def set_devices(self, phone: str, devices: list[Device]) -> None: ...
    async def lookup(self, phones: list[str]) -> dict[str, Profile]: ...
    async def nudge(self, to_phone: str, payload: dict) -> bool: ...


class InMemoryDirectory:
    """Directory state. Used directly in tests/demo and behind the HTTP API."""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.profiles: dict[str, Profile] = {}  # by phone hash
        self.tokens: dict[str, str] = {}  # token -> phone
        self.pending_codes: dict[str, tuple[str, float]] = {}
        self.inboxes: dict[str, list[dict]] = {}
        self.listeners: dict[str, list[NudgeHandler]] = {}
        # With a path, registrations survive restarts (no need to set up every device again).
        self.path = Path(path) if path else None
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.profiles = {h: Profile.from_dict(p) for h, p in data.get("profiles", {}).items()}
                self.tokens = dict(data.get("tokens", {}))
                self.inboxes = {h: list(v) for h, v in data.get("inboxes", {}).items()}
            except Exception as e:
                log.warning("couldn't read %s (%s); starting empty", self.path, e)

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"profiles": {h: p.to_dict() for h, p in self.profiles.items()}, "tokens": self.tokens,
                                   "inboxes": self.inboxes}), encoding="utf-8")
        os.replace(tmp, self.path)

    # --- registration -----------------------------------------------------
    def start_verification(self, phone: str, sender: Callable[[str, str], None] | None = None) -> None:
        phone = normalize_phone(phone)
        code = f"{secrets.randbelow(10**6):06d}"
        self.pending_codes[phone] = (code, time.time() + 600)
        (sender or (lambda p, c: log.warning("Verification code for %s: %s", p, c)))(phone, code)

    def verify(self, phone: str, code: str, name: str) -> Optional[str]:
        phone = normalize_phone(phone)
        want = self.pending_codes.get(phone)
        if not want or want[1] < time.time() or not secrets.compare_digest(want[0], code):
            return None
        del self.pending_codes[phone]
        return self._issue(phone, name)

    def _issue(self, phone: str, name: str) -> str:
        h = phone_hash(phone)
        prof = self.profiles.get(h)
        if prof:
            prof.name = name
        else:
            self.profiles[h] = Profile(phone=phone, name=name)
        token = secrets.token_urlsafe(24)
        self.tokens[token] = phone
        self._save()
        return token

    async def register(self, phone: str, name: str) -> str:
        """Trusted registration (tests/demo, or after verification)."""
        return self._issue(normalize_phone(phone), name)

    async def set_devices(self, phone: str, devices: list[Device]) -> None:
        h = phone_hash(phone)
        prof = self.profiles.setdefault(h, Profile(phone=normalize_phone(phone), name=phone))
        for d in devices:
            d.owner_phone, d.owner_name = prof.phone, prof.name
            if "directory" not in d.sources:
                d.sources.append("directory")
            d.last_seen = time.time()
        prof.devices = devices
        self._save()

    async def upsert_device(self, phone: str, device: Device) -> None:
        prof = self.profiles.get(phone_hash(phone))
        devices = [d for d in (prof.devices if prof else []) if d.id != device.id] + [device]
        await self.set_devices(phone, devices)

    async def lookup(self, phones: list[str]) -> dict[str, Profile]:
        """Map each given phone number to a profile, for those that have one."""
        out = {}
        for p in phones:
            prof = self.profiles.get(phone_hash(p))
            if prof:
                out[normalize_phone(p)] = prof
        return out

    def lookup_hashes(self, hashes: list[str]) -> dict[str, Profile]:
        return {h: self.profiles[h] for h in hashes if h in self.profiles}

    # --- nudges ("Alex wants to connect to your headphones") ---------------
    async def nudge(self, to_phone: str, payload: dict) -> bool:
        h = phone_hash(to_phone)
        if h not in self.profiles:
            return False
        msg = {"id": new_id("n_"), "at": time.time(), **payload}
        self.inboxes.setdefault(h, []).append(msg)
        self._save()
        for fn in self.listeners.get(h, []):
            res = fn(msg)
            if asyncio.iscoroutine(res):
                await res
        return True

    def subscribe(self, phone: str, fn: NudgeHandler) -> None:
        self.listeners.setdefault(phone_hash(phone), []).append(fn)

    def drain_inbox(self, phone: str) -> list[dict]:
        out = self.inboxes.pop(phone_hash(phone), [])
        if out:
            self._save()
        return out


class HTTPDirectory:
    """Client for a directory hosted with `redtooth directory` (or by any
    `redtooth serve --host-directory` instance)."""

    def __init__(self, base_url: str, token: Optional[str] = None):
        self.base = base_url.rstrip("/")
        self.token = token

    async def _req(self, method: str, path: str, json=None):
        import aiohttp

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.request(method, self.base + path, json=json, headers=headers) as r:
                r.raise_for_status()
                return await r.json()

    async def start_verification(self, phone: str) -> None:
        await self._req("POST", "/v1/verify/start", {"phone": phone})

    async def verify(self, phone: str, code: str, name: str) -> str:
        res = await self._req("POST", "/v1/verify/finish", {"phone": phone, "code": code, "name": name})
        self.token = res["token"]
        return self.token

    async def register(self, phone: str, name: str) -> str:  # only allowed by open/demo directories
        res = await self._req("POST", "/v1/register", {"phone": phone, "name": name})
        self.token = res["token"]
        return self.token

    async def set_devices(self, phone: str, devices: list[Device]) -> None:
        await self._req("PUT", "/v1/me/devices", {"devices": [d.to_dict() for d in devices]})

    async def lookup(self, phones: list[str]) -> dict[str, Profile]:
        by_hash = {phone_hash(p): normalize_phone(p) for p in phones}
        res = await self._req("POST", "/v1/lookup", {"hashes": list(by_hash)})
        return {by_hash[h]: Profile.from_dict(p) for h, p in res["profiles"].items()}

    async def nudge(self, to_phone: str, payload: dict) -> bool:
        res = await self._req("POST", "/v1/nudge", {"to": phone_hash(to_phone), "payload": payload})
        return bool(res.get("ok"))

    async def inbox(self) -> list[dict]:
        return (await self._req("GET", "/v1/me/inbox"))["messages"]


def directory_routes(state: InMemoryDirectory, allow_open_register: bool = False):
    """aiohttp routes that host the directory."""
    from aiohttp import web

    routes = web.RouteTableDef()

    def me(request) -> str:
        tok = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        phone = state.tokens.get(tok)
        if not phone:
            raise web.HTTPUnauthorized()
        return phone

    @routes.post("/v1/verify/start")
    async def _start(request):
        body = await request.json()
        state.start_verification(body["phone"])
        return web.json_response({"ok": True})

    @routes.post("/v1/verify/finish")
    async def _finish(request):
        body = await request.json()
        tok = state.verify(body["phone"], body["code"], body.get("name", ""))
        if not tok:
            raise web.HTTPForbidden(text="bad code")
        return web.json_response({"token": tok})

    @routes.post("/v1/register")
    async def _register(request):
        if not allow_open_register:
            raise web.HTTPForbidden(text="verification required")
        body = await request.json()
        return web.json_response({"token": await state.register(body["phone"], body["name"])})

    @routes.put("/v1/me/devices")
    async def _devices(request):
        phone = me(request)
        body = await request.json()
        await state.set_devices(phone, [Device.from_dict(d) for d in body["devices"]])
        return web.json_response({"ok": True})

    @routes.post("/v1/lookup")
    async def _lookup(request):
        me(request)
        body = await request.json()
        found = state.lookup_hashes(body.get("hashes", [])[:2000])
        return web.json_response({"profiles": {h: p.to_dict() for h, p in found.items()}})

    @routes.post("/v1/nudge")
    async def _nudge(request):
        sender = me(request)
        body = await request.json()
        target = next((p for h, p in state.profiles.items() if h == body["to"]), None)
        ok = bool(target) and await state.nudge(target.phone, {**body.get("payload", {}), "from_phone": sender})
        return web.json_response({"ok": ok})

    @routes.get("/v1/me/inbox")
    async def _inbox(request):
        return web.json_response({"messages": state.drain_inbox(me(request))})

    return routes
