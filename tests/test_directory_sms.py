import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from redtooth.directory import InMemoryDirectory, TwilioVerify, directory_routes


class FakeVerify(TwilioVerify):
    def __init__(self):
        self.sent = []

    async def start(self, phone):
        self.sent.append(phone)

    async def check(self, phone, code):
        return code == "424242"


def run(coro):
    return asyncio.run(coro)


def test_codes_are_texted_checked_and_rate_limited(tmp_path):
    async def go():
        fake = FakeVerify()
        app = web.Application()
        app.add_routes(directory_routes(InMemoryDirectory(tmp_path / "d.json"), verifier=fake))
        async with TestClient(TestServer(app)) as c:
            r = await c.post("/v1/verify/start", json={"phone": "+1 415 555 0100"})
            assert r.status == 200 and fake.sent == ["+14155550100"]
            r = await c.post("/v1/verify/finish", json={"phone": "+14155550100", "code": "000000", "name": "Sam"})
            assert r.status == 403
            r = await c.post("/v1/verify/finish", json={"phone": "+14155550100", "code": "424242", "name": "Sam"})
            assert r.status == 200 and (await r.json())["token"]
            # 3 codes per number per 10 minutes
            for _ in range(2):
                assert (await c.post("/v1/verify/start", json={"phone": "+14155550100"})).status == 200
            assert (await c.post("/v1/verify/start", json={"phone": "+14155550100"})).status == 429
            # no country code: refused before anything is sent
            assert (await c.post("/v1/verify/start", json={"phone": "12"})).status == 400
    run(go())


def test_from_env_needs_all_three(monkeypatch):
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_VERIFY_SID"):
        monkeypatch.delenv(k, raising=False)
    assert TwilioVerify.from_env() is None
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC1"); monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t")
    assert TwilioVerify.from_env() is None
    monkeypatch.setenv("TWILIO_VERIFY_SID", "VA1")
    assert TwilioVerify.from_env() is not None


def test_open_register_first_claim_wins_and_link_codes(tmp_path):
    async def go():
        app = web.Application()
        app.add_routes(directory_routes(InMemoryDirectory(tmp_path / "d.json"), allow_open_register=True))
        async with TestClient(TestServer(app)) as c:
            r = await c.post("/v1/register", json={"phone": "+14155550100", "name": "Sam"})
            assert r.status == 200
            tok = (await r.json())["token"]
            # someone else can't take the number
            r = await c.post("/v1/register", json={"phone": "+1 (415) 555-0100", "name": "Mallory"})
            assert r.status == 409
            # Sam adds a second device with a link code from the first
            assert (await c.post("/v1/me/link")).status == 401
            code = (await (await c.post("/v1/me/link", headers={"Authorization": f"Bearer {tok}"})).json())["code"]
            assert (await c.post("/v1/verify/finish", json={"phone": "+14155550100", "code": "000000", "name": "Sam"})).status == 403
            r = await c.post("/v1/verify/finish", json={"phone": "+14155550100", "code": code, "name": "Sam"})
            assert r.status == 200 and (await r.json())["token"] != tok
            # a code works once
            assert (await c.post("/v1/verify/finish", json={"phone": "+14155550100", "code": code, "name": "Sam"})).status == 403
    run(go())
