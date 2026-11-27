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

# The shared directory everyone uses by default (setup offers it; Enter accepts). Set after deploying it, e.g.
# "https://redtooth-directory.onrender.com". REDTOOTH_DIRECTORY overrides it.
PUBLIC_DIRECTORY = ""


def public_directory() -> str:
    return (os.environ.get("REDTOOTH_DIRECTORY") or PUBLIC_DIRECTORY).strip().rstrip("/")
from .util import new_id, normalize_phone, phone_hash

log = logging.getLogger("redtooth.directory")

NudgeHandler = Callable[[dict], Awaitable[None] | None]


class Directory(Protocol):
    async def register(self, phone: str, name: str) -> str: ...
    async def set_devices(self, phone: str, devices: list[Device]) -> None: ...
    async def lookup(self, phones: list[str]) -> dict[str, Profile]: ...
    async def nudge(self, to_phone: str, payload: dict) -> bool: ...


class PostgresStore:
    """Keeps the directory in Postgres (one JSON row), for hosts whose disk doesn't survive restarts
    (Render's free web services). Writes happen on a background thread, latest snapshot wins, so a
    database waking from sleep never slows a request."""

    def __init__(self, url: str) -> None:
        import threading

        import psycopg  # pip install "redtooth[server]"

        self._psycopg, self.url = psycopg, url
        with psycopg.connect(url, autocommit=True) as c:
            c.execute("CREATE TABLE IF NOT EXISTS redtooth_directory (id int PRIMARY KEY, data jsonb NOT NULL, saved_at timestamptz DEFAULT now())")
        self._pending, self._cv = None, threading.Condition()
        threading.Thread(target=self._writer, daemon=True, name="directory-writer").start()

    def load(self) -> Optional[dict]:
        with self._psycopg.connect(self.url) as c:
            row = c.execute("SELECT data FROM redtooth_directory WHERE id = 1").fetchone()
        return row[0] if row else None

    def save(self, data: dict) -> None:
        with self._cv:
            self._pending = json.dumps(data)
            self._cv.notify()

    def _writer(self) -> None:
        while True:
            with self._cv:
                while self._pending is None:
                    self._cv.wait()
                blob, self._pending = self._pending, None
            for attempt in range(5):
                try:
                    with self._psycopg.connect(self.url, autocommit=True) as c:
                        c.execute("INSERT INTO redtooth_directory (id, data, saved_at) VALUES (1, %s::jsonb, now()) "
                                  "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, saved_at = now()", (blob,))
                    break
                except Exception as e:  # database asleep or briefly unreachable: retry, keep the newest snapshot
                    log.warning("couldn't save the directory (%s); retrying", e)
                    time.sleep(2 ** attempt)

    def flush(self, timeout: float = 10) -> None:
        """Waits until the latest snapshot is written (tests, shutdown)."""
        end = time.time() + timeout
        while time.time() < end:
            with self._cv:
                if self._pending is None:
                    break
            time.sleep(.05)
        time.sleep(.3)


class InMemoryDirectory:
    """Directory state. Used directly in tests/demo and behind the HTTP API."""

    def __init__(self, path: Optional[str | Path] = None, store: Optional["PostgresStore"] = None) -> None:
        self.profiles: dict[str, Profile] = {}  # by phone hash
        self.tokens: dict[str, str] = {}  # token -> phone
        self.pending_codes: dict[str, tuple[str, float]] = {}
        self.inboxes: dict[str, list[dict]] = {}
        self.listeners: dict[str, list[NudgeHandler]] = {}
        self.code_sink: Optional[Callable[[str, str], None]] = None  # where verification codes go (no SMS yet)
        # With a path, registrations survive restarts (no need to set up every device again).
        self.path = Path(path) if path else None
        self.store = store
        if store is not None:
            try:
                self._restore(store.load() or {})
            except Exception as e:
                raise SystemExit(f"Couldn't read the directory from the database: {e}")
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.profiles = {h: Profile.from_dict(p) for h, p in data.get("profiles", {}).items()}
                self.tokens = dict(data.get("tokens", {}))
                self.inboxes = {h: list(v) for h, v in data.get("inboxes", {}).items()}
            except Exception as e:
                log.warning("couldn't read %s (%s); starting empty", self.path, e)

    def _snapshot(self) -> dict:
        return {"profiles": {h: p.to_dict() for h, p in self.profiles.items()}, "tokens": self.tokens, "inboxes": self.inboxes}

    def _restore(self, data: dict) -> None:
        self.profiles = {h: Profile.from_dict(p) for h, p in data.get("profiles", {}).items()}
        self.tokens = dict(data.get("tokens", {}))
        self.inboxes = {h: list(v) for h, v in data.get("inboxes", {}).items()}

    def _save(self) -> None:
        if self.store is not None:
            self.store.save(self._snapshot())
            return
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
        (sender or self.code_sink or (lambda p, c: log.warning("Verification code for %s: %s", p, c)))(phone, code)

    def verify(self, phone: str, code: str, name: str) -> Optional[str]:
        phone = normalize_phone(phone)
        want = self.pending_codes.get(phone)
        if not want or want[1] < time.time() or not secrets.compare_digest(want[0], code):
            return None
        del self.pending_codes[phone]
        return self._issue(phone, name)

    def claim(self, phone: str, name: str) -> Optional[str]:
        """Register a number nobody has registered yet (first claim wins). None if it's taken."""
        phone = normalize_phone(phone)
        if phone_hash(phone) in self.profiles:
            return None
        return self._issue(phone, name)

    def link_code(self, phone: str) -> str:
        """A one-time code (10 min) a registered device shows so you can add another device of yours."""
        phone = normalize_phone(phone)
        code = f"{secrets.randbelow(10**6):06d}"
        self.pending_codes[phone] = (code, time.time() + 600)
        return code

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

    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = 20):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout  # a free host that's asleep takes up to a minute to answer the first request

    async def _req(self, method: str, path: str, json=None):
        import aiohttp

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as s:
            async with s.request(method, self.base + path, json=json, headers=headers) as r:
                r.raise_for_status()
                return await r.json()

    async def start_verification(self, phone: str) -> None:
        await self._req("POST", "/v1/verify/start", {"phone": phone})

    async def verify(self, phone: str, code: str, name: str) -> str:
        res = await self._req("POST", "/v1/verify/finish", {"phone": phone, "code": code, "name": name})
        self.token = res["token"]
        return self.token

    async def register(self, phone: str, name: str) -> str:  # directories without phone verification (first claim wins)
        res = await self._req("POST", "/v1/register", {"phone": phone, "name": name})
        self.token = res["token"]
        return self.token

    async def link_code(self) -> str:
        """A code for adding another device of yours (enter it in setup on that device)."""
        return (await self._req("POST", "/v1/me/link"))["code"]

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


class TwilioVerify:
    """Texts verification codes with Twilio Verify (it makes, sends, expires and checks the code).

    Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_VERIFY_SID (the Verify service, "VA…")."""

    BASE = "https://verify.twilio.com/v2/Services"

    def __init__(self, account_sid: str, auth_token: str, service_sid: str) -> None:
        self.auth = (account_sid, auth_token)
        self.service = service_sid

    @classmethod
    def from_env(cls) -> Optional["TwilioVerify"]:
        sid, tok, svc = (os.environ.get(k, "").strip() for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_VERIFY_SID"))
        return cls(sid, tok, svc) if sid and tok and svc else None

    async def _post(self, path: str, data: dict) -> dict:
        import aiohttp

        async with aiohttp.ClientSession(auth=aiohttp.BasicAuth(*self.auth), timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.post(f"{self.BASE}/{self.service}/{path}", data=data) as r:
                body = await r.json(content_type=None)
                if r.status >= 400:
                    raise RuntimeError(f"Twilio {r.status}: {body.get('message', body)}")
                return body

    async def start(self, phone: str) -> None:
        await self._post("Verifications", {"To": phone, "Channel": "sms"})

    async def check(self, phone: str, code: str) -> bool:
        try:
            return (await self._post("VerificationCheck", {"To": phone, "Code": code})).get("status") == "approved"
        except RuntimeError as e:  # 404 = expired or already used
            log.info("verification check failed: %s", e)
            return False


class RateLimit:
    """At most `n` events per key in `window` seconds (in memory; fine for one directory instance)."""

    def __init__(self, n: int, window: float) -> None:
        self.n, self.window, self.hits = n, window, {}

    def allow(self, key: str) -> bool:
        now = time.time()
        recent = [t for t in self.hits.get(key, []) if now - t < self.window]
        if len(recent) >= self.n:
            self.hits[key] = recent
            return False
        self.hits[key] = recent + [now]
        return True


def directory_routes(state: InMemoryDirectory, allow_open_register: bool = False, verifier: Optional[TwilioVerify] = None):
    """aiohttp routes that host the directory."""
    from aiohttp import web

    routes = web.RouteTableDef()

    def me(request) -> str:
        tok = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        phone = state.tokens.get(tok)
        if not phone:
            raise web.HTTPUnauthorized()
        return phone

    @routes.get("/v1/health")
    async def health(request):
        return web.json_response({"ok": True})

    # Each code we text costs money, and an open endpoint can be used to spam numbers: limit per number and per address.
    per_phone, per_ip, checks = RateLimit(3, 600), RateLimit(10, 3600), RateLimit(8, 600)

    def client_ip(request) -> str:
        return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote or "?")

    @routes.post("/v1/verify/start")
    async def _start(request):
        body = await request.json()
        phone = normalize_phone(body["phone"])
        if not phone.startswith("+") or len(phone) < 8:
            raise web.HTTPBadRequest(text="phone number needs a country code")
        if not per_phone.allow(phone) or not per_ip.allow(client_ip(request)):
            raise web.HTTPTooManyRequests(text="too many codes requested; try again in a few minutes")
        if verifier:
            try:
                await verifier.start(phone)
            except RuntimeError as e:
                log.warning("couldn't text %s: %s", phone, e)
                raise web.HTTPBadGateway(text="couldn't send the text message")
        else:
            state.start_verification(phone)
        return web.json_response({"ok": True})

    @routes.post("/v1/verify/finish")
    async def _finish(request):
        body = await request.json()
        phone = normalize_phone(body["phone"])
        if not checks.allow(phone):
            raise web.HTTPTooManyRequests(text="too many tries; request a new code in a few minutes")
        code = str(body["code"]).strip()
        tok = state.verify(phone, code, body.get("name", ""))  # a link code from your other device, or a logged code
        if not tok and verifier and await verifier.check(phone, code):
            tok = state._issue(phone, body.get("name", ""))
        if not tok:
            raise web.HTTPForbidden(text="bad code")
        return web.json_response({"token": tok})

    claims = RateLimit(10, 3600)

    @routes.post("/v1/register")
    async def _register(request):
        """Without phone verification: first claim wins; a taken number needs a link code from one of its devices."""
        if not allow_open_register:
            raise web.HTTPForbidden(text="verification required")
        body = await request.json()
        phone = normalize_phone(body["phone"])
        if not phone.startswith("+") or len(phone) < 8:
            raise web.HTTPBadRequest(text="phone number needs a country code")
        if not claims.allow(client_ip(request)):
            raise web.HTTPTooManyRequests(text="too many registrations from here; try again later")
        tok = state.claim(phone, body.get("name", ""))
        if not tok:
            raise web.HTTPConflict(text="this number is already registered; add this device with a link code from one that's set up")
        return web.json_response({"token": tok})

    @routes.post("/v1/me/link")
    async def _link(request):
        return web.json_response({"code": state.link_code(me(request)), "expires_in": 600})

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
