"""The parts of the Windows backend that don't need Windows: id parsing, device kinds, endpoint matching."""
from redtooth.bt.windows import _addr_from_text, _aqs, _kind_from_cod, _match_endpoints, CLASSIC
from redtooth.models import Device


def test_addresses_from_aep_ids_and_driver_paths():
    assert _addr_from_text("Bluetooth#Bluetooth00:1a:7d:da:71:13-a0:e9:db:12:34:56") == "A0:E9:DB:12:34:56"
    path = r"\\?\BTHENUM#{0000110b-0000-1000-8000-00805f9b34fb}_LOCALMFG&000a#8&2a6c1b2&0&A0E9DB123456_C00000000#{6994ad04-93ef-11d0-a3cc-00a0c9223196}\wave"
    assert _addr_from_text(path) == "A0:E9:DB:12:34:56"
    assert _addr_from_text(r"\\?\HDAUDIO#FUNC_01&VEN_10EC") is None


def test_kinds_from_class_of_device():
    assert _kind_from_cod(4, 6) == "headphones" and _kind_from_cod(4, 1) == "headphones"
    assert _kind_from_cod(4, 5) == "speaker" and _kind_from_cod(2, 3) == "phone" and _kind_from_cod(None, None) == ""


def test_endpoint_matching_prefers_stereo_and_address():
    eps = [
        {"id": "1", "name": "Speakers (Realtek(R) Audio)", "bluetooth": False, "address": None, "active": True, "hands_free": False},
        {"id": "2", "name": "Headset (WH-CH720N Hands-Free)", "bluetooth": True, "address": "AA:BB:CC:DD:EE:FF", "active": False, "hands_free": True},
        {"id": "3", "name": "Headphones (WH-CH720N)", "bluetooth": True, "address": "AA:BB:CC:DD:EE:FF", "active": False, "hands_free": False},
        {"id": "4", "name": "Headphones (Redmi Buds 5)", "bluetooth": True, "address": None, "active": True, "hands_free": False},
    ]
    assert [e["id"] for e in _match_endpoints(eps, Device("bt:x", "WH-CH720N", address="aa:bb:cc:dd:ee:ff"))] == ["3", "2"]
    assert [e["id"] for e in _match_endpoints(eps, Device("bt:y", "Redmi Buds 5", address="11:22:33:44:55:66"))] == ["4"]
    assert _match_endpoints(eps, Device("bt:z", "AZZARO HEADPHONES", address="12:34:56:78:9A:BC")) == []


def test_inquiry_query():
    q = _aqs(CLASSIC, False, inquiry=True)
    assert "IsPaired:=System.StructuredQueryType.Boolean#False" in q and "IssueInquiry" in q


# ---- The Windows backend with the classic API faked: the path that works with no extra packages.
import asyncio

import pytest

from redtooth.bt.win32bt import BtInfo, cod_kind


def test_class_of_device():
    assert cod_kind(0x240404) == "headphones" and cod_kind(0x240418) == "headphones" and cod_kind(0x240414) == "speaker"
    assert cod_kind(0x5A020C) == "phone" and cod_kind(0x1F00) == ""


class FakeW32:
    def __init__(self):
        self.devs = {
            "10:20:30:40:50:01": BtInfo("10:20:30:40:50:01", "Redmi Buds 5", 0x240404, False, True, True),
            "10:20:30:40:50:02": BtInfo("10:20:30:40:50:02", "WH-CH720N", 0x240418, False, True, True),
            "10:20:30:40:50:03": BtInfo("10:20:30:40:50:03", "JBL Tune 510", 0x240404, False, False, False),  # in pairing mode
        }
        self.on = {"10:20:30:40:50:01"}  # only the Redmi Buds are switched on

    def has_radio(self):
        return True

    def devices(self, inquiry=False, seconds=0):
        return [d for d in self.devs.values() if d.paired or (inquiry and d.address in self.on | {"10:20:30:40:50:03"})]

    def info(self, address):
        return self.devs.get(address)

    def set_services(self, address, enable, services=()):
        d = self.devs[address]
        d.connected = enable and address in self.on
        return True

    def authenticate(self, address):
        self.devs[address].authenticated = True
        self.on.add(address)
        return 0


@pytest.fixture
def win(monkeypatch):
    from redtooth.bt import windows

    monkeypatch.setattr(windows, "missing_packages", lambda: ["pycaw", "winrt.windows.devices.enumeration"])
    w = windows.WindowsBluetooth()
    w.w32 = FakeW32()
    return w


def test_paired_state_is_real(win):
    devs = {d.name: d for d in asyncio.run(win.paired())}
    assert set(devs) == {"Redmi Buds 5", "WH-CH720N"} and not any(d.connected for d in devs.values())
    assert devs["WH-CH720N"].kind == "headphones"


def test_connect_succeeds_only_when_the_device_answers(win):
    devs = {d.name: d for d in asyncio.run(win.paired())}
    assert asyncio.run(win.connect(devs["Redmi Buds 5"])) is True
    win._wait_timeout = 1
    import time

    t0 = time.monotonic()
    assert asyncio.run(win.connect(devs["WH-CH720N"])) is False  # switched off: not reported connected
    assert time.monotonic() - t0 < 13


def test_scan_and_pair_without_winrt(win):
    found = asyncio.run(win.scan(0.1))
    assert [d.name for d in found] == ["JBL Tune 510"] and found[0].discoverable
    assert asyncio.run(win.pair(found[0])) is True
    assert any(d.name == "JBL Tune 510" for d in asyncio.run(win.paired()))


def test_missing_packages_message(win):
    msg = win.setup_issue()
    assert "pip install" in msg and "audio outputs" in msg
