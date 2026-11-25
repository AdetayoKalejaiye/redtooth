"""Tray/desktop helpers and the panel's device API."""
import sys

import pytest
from aiohttp.test_utils import TestClient, TestServer

from redtooth import desktop
from redtooth.demo import build_demo
from redtooth.server import make_app


def test_background_command_and_icon():
    cmd = desktop.background_command()
    assert cmd[-3:] == ["redtooth.cli", "serve", "--background"]
    assert desktop.tray_icon_image(32, "green").size == (32, 32)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="checks the XDG autostart file")
def test_autostart_toggle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert not desktop.autostart_enabled()
    assert desktop.set_autostart(True) and desktop.autostart_enabled()
    assert "serve --background" in (tmp_path / "autostart/redtooth.desktop").read_text()
    desktop.set_autostart(False)
    assert not desktop.autostart_enabled()


@pytest.mark.asyncio
async def test_panel_device_api(tmp_path):
    d = await build_demo(str(tmp_path), daniel_auto_accept=0.2, embedder="hash", radio_delay=0.01)
    client = TestClient(TestServer(make_app(d.agent, demo=d)))
    await client.start_server()
    H = {"X-Redtooth": "1"}
    try:
        found = await (await client.post("/api/scan", json={"seconds": 1}, headers=H)).json()
        assert "JBL Flip 6" in [x["name"] for x in found["devices"]]
        st = await (await client.get("/api/state")).json()
        kinds = [n["kind"] for n in st["nearby_devices"]]
        assert st["radio"] == "on" and "phone" in kinds  # a normal Bluetooth scan: phones too
        assert kinds.index("phone") > kinds.index("speaker")  # audio first
        r = await (await client.post("/api/pair", json={"device_id": "bt:jbl"}, headers=H)).json()
        assert r["ok"], r
        r = await (await client.post("/api/my-devices", json={"device_id": "bt:jbl", "mine": True, "alias": "Alex's speaker"}, headers=H)).json()
        assert r["ok"] and any(x["id"] == "bt:jbl" and x["mine"] and x["label"] == "Alex's speaker" for x in r["devices"])
        r = await (await client.post("/api/disconnect", json={"device_id": "bt:jbl"}, headers=H)).json()
        assert r["ok"] and "Disconnected" in r["text"]
        nav = await (await client.post("/api/navigate", json={"to": "settings-devices"}, headers=H)).json()
        assert nav["ok"]
    finally:
        await client.close()
