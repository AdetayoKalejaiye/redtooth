"""Linux (bluetoothctl/pactl) and macOS (blueutil/SwitchAudioSource) backends with their tools faked."""
import asyncio

import pytest

from redtooth.bt import linux, macos
from redtooth.models import Device


def fake_run(table):
    async def run(*cmd, timeout=20.0):
        for key, out in table.items():
            if all(k in cmd for k in key):
                return (0, out() if callable(out) else out)
        return (1, "")
    return run


def test_linux_connect_trusts_bluez_state_not_the_message(monkeypatch):
    info = {"connected": "no"}
    monkeypatch.setattr(linux, "run", fake_run({
        ("connect",): "Attempting to connect to AA:BB:CC:DD:EE:FF\nConnection successful",
        ("info",): lambda: f"Device AA:BB:CC:DD:EE:FF\n\tName: WH-CH720N\n\tPaired: yes\n\tConnected: {info['connected']}\n",
        ("show",): "Controller 00:11:22:33:44:55\n\tPowered: yes\n",
    }))
    monkeypatch.setattr(linux.shutil, "which", lambda t: "/usr/bin/" + t)
    bt = linux.LinuxBluetooth()
    d = Device("bt:x", "WH-CH720N", "headphones", address="AA:BB:CC:DD:EE:FF", paired=True)
    assert asyncio.run(bt.connect(d)) is False  # "Connection successful" but BlueZ says not connected
    info["connected"] = "yes"
    assert asyncio.run(bt.connect(d)) is True
    assert asyncio.run(bt.radio_state()) == "on"


def test_linux_unreadable_output_is_not_a_guess(monkeypatch):
    monkeypatch.setattr(linux, "run", fake_run({}))
    out = asyncio.run(linux.LinuxBluetooth().audio_output())
    assert out.known is False


def test_mac_output_and_radio(monkeypatch):
    monkeypatch.setattr(macos, "run", fake_run({
        ("SwitchAudioSource", "-c"): "WH-CH720N\n",
        ("--paired",): '[{"address":"aa-bb-cc-dd-ee-ff","name":"WH-CH720N","paired":true,"connected":true}]',
        ("--power",): "1\n",
    }))
    monkeypatch.setattr(macos.shutil, "which", lambda t: "/usr/local/bin/" + t)
    bt = macos.MacBluetooth()
    out = asyncio.run(bt.audio_output())
    assert out.transport == "bluetooth" and bt.output_matches(out, Device("bt:x", "WH-CH720N", address="AA:BB:CC:DD:EE:FF"))
    assert asyncio.run(bt.radio_state()) == "on"
