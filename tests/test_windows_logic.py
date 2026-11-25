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
