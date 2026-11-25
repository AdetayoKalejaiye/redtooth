"""Windows backend.

Everything here uses APIs that ship with Windows 10/11, reached through packages Redtooth installs
(pywinrt for WinRT, comtypes/pycaw for Core Audio):

- Paired devices and their *real* state: WinRT association endpoints (the same records the Settings
  app's Bluetooth page uses), with System.Devices.Aep.IsConnected / IsPresent. A device whose driver
  is installed is not "connected"; only IsConnected says that.
- Scanning nearby: an association-endpoint watcher with Bluetooth inquiry turned on (classic) plus
  connectable Bluetooth LE devices, so headphones in pairing mode show up the way they do in
  Settings › Bluetooth › Add device.
- Pairing: DeviceInformationCustomPairing, accepting the confirm-only / fixed-PIN ("0000") flows
  headphones and speakers use, with no Settings window.
- Connecting an already-paired headset: Bluetooth audio drivers expose a one-shot reconnect /
  disconnect kernel-streaming property (KSPROPSETID_BtAudio) on the audio endpoint's filter. That is
  what the Sound settings "Connect" button does; we call it through IKsControl. Success is only
  reported once Windows says the device is actually connected.
- Audio output: Core Audio (pycaw) for the default endpoint and IPolicyConfig to change it; WinRT
  MediaDevice as a fallback for reading it.

Written against the documented APIs and the pywinrt / pycaw type stubs; `redtooth doctor` prints what
each part sees on a real PC.
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import re
import threading
import time
from typing import Any, Optional

from ..models import Device
from .base import AudioOutput, BluetoothPlatform, enrich, norm_addr

log = logging.getLogger("redtooth.windows")

CLASSIC = "{e0cbf06c-cd8b-4647-bb8a-263b43f0f974}"
LE = "{bb7bb05e-5972-42b5-94fc-76eaa7084d49}"
PROPS = [
    "System.Devices.Aep.DeviceAddress",
    "System.Devices.Aep.IsConnected",
    "System.Devices.Aep.IsPresent",
    "System.Devices.Aep.IsPaired",
    "System.Devices.Aep.SignalStrength",
    "System.Devices.Aep.Bluetooth.Cod.Major",
    "System.Devices.Aep.Bluetooth.Cod.Minor",
    "System.Devices.Aep.Bluetooth.Le.IsConnectable",
    "System.Devices.Aep.ProtocolId",
]
KSPROPSETID_BtAudio = "{7FA06C40-B8F6-4C7E-8556-E8C33A12E54D}"
KSPROPERTY_ONESHOT_RECONNECT, KSPROPERTY_ONESHOT_DISCONNECT = 0, 1

_warned: set[str] = set()


def _warn_once(what: str, e: BaseException) -> None:
    if what not in _warned:
        _warned.add(what)
        log.warning("%s: %s: %s", what, type(e).__name__, e)


def _aqs(protocol: str, paired: bool, inquiry: bool = False) -> str:
    q = f'System.Devices.Aep.ProtocolId:="{protocol}" AND System.Devices.Aep.IsPaired:=System.StructuredQueryType.Boolean#{"True" if paired else "False"}'
    if inquiry:
        q += " AND System.Devices.Aep.Bluetooth.IssueInquiry:=System.StructuredQueryType.Boolean#True"
    return q


def _val(v: Any) -> Any:
    """WinRT property values arrive boxed; unbox whatever type it turns out to be."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    from winrt import system as s

    for fn in (s.unbox_boolean, s.unbox_string, s.unbox_int32, s.unbox_uint32, s.unbox_uint16, s.unbox_int16,
               s.unbox_uint8, s.unbox_int8, s.unbox_uint64, s.unbox_int64, s.unbox_guid):
        try:
            return fn(v)
        except Exception:
            continue
    return None


def _prop(info, key: str) -> Any:
    try:
        props = info.properties
        return _val(props[key]) if key in props else None
    except Exception:
        return None


def _kind_from_cod(major: Optional[int], minor: Optional[int]) -> str:
    if major == 4:  # audio/video
        if minor in (5, 15):
            return "speaker"
        if minor in (1, 2, 6, 7):
            return "headphones"
        if minor in (8, 10):
            return "speaker"
    if major == 2:
        return "phone"
    if major == 1:
        return "laptop"
    return ""


def _addr_from_text(s: str) -> Optional[str]:
    """MAC from an AEP id ("Bluetooth#Bluetooth00:1a:..-a0:e9:db:12:34:56") or a driver path ("…&A0E9DB123456_C00000000…")."""
    m = re.findall(r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})", s)
    if m:
        return norm_addr(m[-1])
    m = re.search(r"[&#_]([0-9a-fA-F]{12})_C[0-9a-fA-F]", s)
    return norm_addr(m.group(1)) if m else None


# ----------------------------------------------------------------- Core Audio (comtypes)

def _com_types():
    """IKsControl / IDeviceTopology / IConnector, declared once (comtypes needs the full vtable order)."""
    global _COM
    try:
        return _COM
    except NameError:
        pass
    from ctypes import POINTER, Structure, c_uint, c_ulong, c_void_p
    from ctypes.wintypes import BOOL, LPWSTR

    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    class KSPROPERTY(Structure):
        _fields_ = [("Set", GUID), ("Id", c_ulong), ("Flags", c_ulong)]

    class IKsControl(IUnknown):
        _iid_ = GUID("{28F54685-06FD-11D2-B27A-00A0C9223196}")
        _methods_ = [
            COMMETHOD([], HRESULT, "KsProperty", (["in"], POINTER(KSPROPERTY), "Property"), (["in"], c_ulong, "PropertyLength"),
                      (["in"], c_void_p, "PropertyData"), (["in"], c_ulong, "DataLength"), (["out"], POINTER(c_ulong), "BytesReturned")),
            COMMETHOD([], HRESULT, "KsMethod", (["in"], c_void_p, "Method"), (["in"], c_ulong, "MethodLength"),
                      (["in"], c_void_p, "MethodData"), (["in"], c_ulong, "DataLength"), (["out"], POINTER(c_ulong), "BytesReturned")),
            COMMETHOD([], HRESULT, "KsEvent", (["in"], c_void_p, "Event"), (["in"], c_ulong, "EventLength"),
                      (["in"], c_void_p, "EventData"), (["in"], c_ulong, "DataLength"), (["out"], POINTER(c_ulong), "BytesReturned")),
        ]

    class IConnector(IUnknown):
        _iid_ = GUID("{9C2C4058-23F5-41DE-877A-DF3AF236A09E}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetType", (["out"], POINTER(c_uint), "pType")),
            COMMETHOD([], HRESULT, "GetDataFlow", (["out"], POINTER(c_uint), "pFlow")),
            COMMETHOD([], HRESULT, "ConnectTo", (["in"], c_void_p, "pConnectTo")),
            COMMETHOD([], HRESULT, "Disconnect"),
            COMMETHOD([], HRESULT, "IsConnected", (["out"], POINTER(BOOL), "pbConnected")),
            COMMETHOD([], HRESULT, "GetConnectedTo", (["out"], POINTER(c_void_p), "ppConTo")),
            COMMETHOD([], HRESULT, "GetConnectorIdConnectedTo", (["out"], POINTER(LPWSTR), "ppwstrConnectorId")),
            COMMETHOD([], HRESULT, "GetDeviceIdConnectedTo", (["out"], POINTER(LPWSTR), "ppwstrDeviceId")),
        ]

    class IDeviceTopology(IUnknown):
        _iid_ = GUID("{2A07407E-6497-4A18-9787-32F79BD0D98F}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetConnectorCount", (["out"], POINTER(c_uint), "pCount")),
            COMMETHOD([], HRESULT, "GetConnector", (["in"], c_uint, "nIndex"), (["out"], POINTER(POINTER(IConnector)), "ppConnector")),
            COMMETHOD([], HRESULT, "GetSubunitCount", (["out"], POINTER(c_uint), "pCount")),
            COMMETHOD([], HRESULT, "GetSubunit", (["in"], c_uint, "nIndex"), (["out"], POINTER(c_void_p), "ppSubunit")),
            COMMETHOD([], HRESULT, "GetPartById", (["in"], c_uint, "nId"), (["out"], POINTER(c_void_p), "ppPart")),
            COMMETHOD([], HRESULT, "GetDeviceId", (["out"], POINTER(LPWSTR), "ppwstrDeviceId")),
            COMMETHOD([], HRESULT, "GetSignalPath", (["in"], c_void_p, "pIPartFrom"), (["in"], c_void_p, "pIPartTo"),
                      (["in"], BOOL, "bRejectMixedPaths"), (["out"], POINTER(c_void_p), "ppParts")),
        ]

    _COM = (KSPROPERTY, IKsControl, IConnector, IDeviceTopology, GUID)
    return _COM


def _audio_endpoints(include_unplugged: bool = True) -> list[dict]:
    """Playback endpoints with: id, name, state, whether Bluetooth, the device address if the driver
    path carries it, and the KS filter id (for reconnect/disconnect). Call with COM initialized."""
    import warnings

    import comtypes
    from pycaw.constants import DEVICE_STATE, EDataFlow
    from pycaw.pycaw import AudioUtilities

    _, _, _, IDeviceTopology, _ = _com_types()
    mask = DEVICE_STATE.MASK_ALL.value if include_unplugged else DEVICE_STATE.ACTIVE.value
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        devs = AudioUtilities.GetAllDevices(data_flow=EDataFlow.eRender.value, device_state=mask)
    out = []
    for d in devs:
        filt = ""
        try:
            topo = d._dev.Activate(IDeviceTopology._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(IDeviceTopology)
            filt = topo.GetConnector(0).GetDeviceIdConnectedTo() or ""
        except Exception:
            pass
        bt = "bth" in filt.lower()
        state = getattr(d.state, "value", d.state)
        out.append({"id": d.id, "name": d.FriendlyName or "", "state": int(state) if isinstance(state, int) else str(state),
                    "active": state == DEVICE_STATE.ACTIVE.value, "bluetooth": bt, "address": _addr_from_text(filt) if bt else None,
                    "filter": filt, "hands_free": "bthhfenum" in filt.lower() or "hands-free" in (d.FriendlyName or "").lower()})
    return out


def _default_render_id() -> Optional[str]:
    from pycaw.pycaw import AudioUtilities

    return getattr(AudioUtilities.GetSpeakers(), "id", None)


def _ks_oneshot(filter_id: str, prop_id: int) -> bool:
    import comtypes
    from pycaw.pycaw import AudioUtilities

    KSPROPERTY, IKsControl, _, _, GUID = _com_types()
    dev = AudioUtilities.GetDeviceEnumerator().GetDevice(filter_id)
    ks = dev.Activate(IKsControl._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(IKsControl)
    p = KSPROPERTY(GUID(KSPROPSETID_BtAudio), prop_id, 1)  # KSPROPERTY_TYPE_GET
    try:
        ks.KsProperty(ctypes.byref(p), ctypes.sizeof(p), None, 0)
    except Exception as e:  # the call can report an error even though the request was taken
        log.debug("KsProperty(%s) on %s: %s", prop_id, filter_id, e)
    return True


def _match_endpoints(eps: list[dict], dev: Device) -> list[dict]:
    """Endpoints that belong to this device: by address from the driver path, else by name."""
    addr = norm_addr(dev.address) if dev.address else None
    hits = [e for e in eps if e["bluetooth"] and addr and e["address"] == addr]
    if not hits:
        names = [n.lower() for n in (dev.name, dev.model, dev.short_model) if n]
        hits = [e for e in eps if e["bluetooth"] and any(n in e["name"].lower() for n in names)]
    hits.sort(key=lambda e: (e["hands_free"], not e["active"]))
    return hits


class _Com:
    """Run Core Audio calls on one dedicated thread with COM initialized."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    async def call(self, fn, *args):
        def run():
            import comtypes

            with self._lock:
                comtypes.CoInitialize()
                try:
                    return fn(*args)
                finally:
                    comtypes.CoUninitialize()

        return await asyncio.get_running_loop().run_in_executor(None, run)


class WindowsBluetooth(BluetoothPlatform):
    name = "windows"

    def __init__(self) -> None:
        self.com = _Com()
        self._aep: dict[str, str] = {}  # address -> association endpoint id (for pairing and status)
        self._paired_cache: tuple[float, list[Device]] = (0.0, [])
        try:
            import winrt.windows.devices.wifidirect  # noqa: F401

            self.wifi_direct = True
        except Exception:
            self.wifi_direct = False

    # ------------------------------------------------------------- radio
    async def radio_state(self) -> str:
        try:
            from winrt.windows.devices.bluetooth import BluetoothAdapter
            from winrt.windows.devices.radios import RadioState

            adapter = await BluetoothAdapter.get_default_async()
            if adapter is None:
                return "missing"
            radio = await adapter.get_radio_async()
            return {RadioState.ON: "on", RadioState.OFF: "off", RadioState.DISABLED: "off"}.get(radio.state, "unknown")
        except Exception as e:
            _warn_once("Bluetooth radio", e)
            return "unknown"

    # ------------------------------------------------------------- devices
    def _device(self, info, source: str) -> Optional[Device]:
        addr = norm_addr(_prop(info, "System.Devices.Aep.DeviceAddress") or "") or _addr_from_text(info.id)
        name = (info.name or "").strip()
        if not addr or not name:
            return None
        self._aep[addr] = info.id
        kind = _kind_from_cod(_prop(info, "System.Devices.Aep.Bluetooth.Cod.Major"), _prop(info, "System.Devices.Aep.Bluetooth.Cod.Minor"))
        rssi = _prop(info, "System.Devices.Aep.SignalStrength")
        d = Device(id=f"bt:{addr}", name=name, kind=kind or "other", address=addr,
                   paired=bool(_prop(info, "System.Devices.Aep.IsPaired")) or source == "paired",
                   connected=bool(_prop(info, "System.Devices.Aep.IsConnected")),
                   discoverable=source == "nearby", rssi=int(rssi) if isinstance(rssi, int) else None, sources=[source])
        d.present = _prop(info, "System.Devices.Aep.IsPresent")
        return enrich(d)

    async def paired(self) -> list[Device]:
        # The panel and the agent ask often; Windows answers in tens of ms, but don't hammer it.
        if time.monotonic() - self._paired_cache[0] < 1.0:
            return [Device.from_dict(d.to_dict()) for d in self._paired_cache[1]]
        try:
            from winrt.windows.devices.enumeration import DeviceInformation, DeviceInformationKind
        except Exception as e:
            _warn_once("WinRT not available (pip install -e . again)", e)
            return []
        devs: dict[str, Device] = {}
        for proto in (CLASSIC, LE):
            try:
                infos = await asyncio.wait_for(
                    DeviceInformation.find_all_async(_aqs(proto, True), PROPS, DeviceInformationKind.ASSOCIATION_ENDPOINT), 8)
            except Exception as e:
                _warn_once(f"listing paired devices ({'classic' if proto == CLASSIC else 'LE'})", e)
                continue
            for info in infos:
                d = self._device(info, "paired")
                if d:
                    prev = devs.get(d.id)
                    if prev:  # dual-mode device listed twice: connected if either side is
                        prev.connected = prev.connected or d.connected
                    else:
                        devs[d.id] = d
        out = list(devs.values())
        self._paired_cache = (time.monotonic(), out)
        return [Device.from_dict(d.to_dict()) for d in out]

    async def scan(self, seconds: float = 6.0) -> list[Device]:
        """Devices in pairing mode nearby (Bluetooth inquiry + connectable LE), like Settings › Add device."""
        try:
            from winrt.windows.devices.enumeration import DeviceInformation, DeviceInformationKind
        except Exception as e:
            _warn_once("WinRT not available", e)
            return []
        loop = asyncio.get_running_loop()
        found: dict[str, Device] = {}

        def on_info(_watcher, info) -> None:
            def add() -> None:
                d = self._device(info, "nearby")
                if not d:
                    return
                if d.address and d.address in found:
                    found[d.address].rssi = d.rssi if d.rssi is not None else found[d.address].rssi
                    return
                if d.address:
                    found[d.address] = d

            loop.call_soon_threadsafe(add)

        def on_update(_watcher, upd) -> None:
            def apply() -> None:
                addr = next((a for a, i in self._aep.items() if i == upd.id), None)
                if addr and addr in found:
                    try:
                        if "System.Devices.Aep.SignalStrength" in upd.properties:
                            found[addr].rssi = _val(upd.properties["System.Devices.Aep.SignalStrength"])
                    except Exception:
                        pass

            loop.call_soon_threadsafe(apply)

        watchers = []
        for aqs in (_aqs(CLASSIC, False, inquiry=True), _aqs(LE, False) + " AND System.Devices.Aep.Bluetooth.Le.IsConnectable:=System.StructuredQueryType.Boolean#True"):
            try:
                w = DeviceInformation.create_watcher(aqs, PROPS, DeviceInformationKind.ASSOCIATION_ENDPOINT)
                w.add_added(on_info)
                w.add_updated(on_update)
                w.start()
                watchers.append(w)
            except Exception as e:
                _warn_once("starting a Bluetooth scan", e)
        try:
            await asyncio.sleep(seconds)
        finally:
            for w in watchers:
                try:
                    w.stop()
                except Exception:
                    pass
        # LE lists every beacon in range; keep the ones that look like something you'd connect to.
        out = [d for d in found.values() if d.kind != "other" or not re.fullmatch(r"[0-9A-F:\-_ ]+|LE-.*", d.name, re.I)]
        out.sort(key=lambda d: (d.kind not in ("headphones", "speaker"), -(d.rssi if d.rssi is not None else -100)))
        return out

    async def _status(self, dev: Device) -> Optional[bool]:
        """Is the device connected right now (fresh, not cached)?"""
        aep = self._aep.get(norm_addr(dev.address) or "")
        try:
            from winrt.windows.devices.enumeration import DeviceInformation, DeviceInformationKind

            if aep:
                info = await DeviceInformation.create_from_id_async(aep, ["System.Devices.Aep.IsConnected"], DeviceInformationKind.ASSOCIATION_ENDPOINT)
                return bool(_prop(info, "System.Devices.Aep.IsConnected"))
        except Exception as e:
            _warn_once("reading connection status", e)
        self._paired_cache = (0.0, [])
        return next((d.connected for d in await self.paired() if d.address == dev.address), None)

    async def _wait_connected(self, dev: Device, want: bool, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self._status(dev) is want:
                self._paired_cache = (0.0, [])
                return True
            await asyncio.sleep(0.4)
        return False

    async def pair(self, dev: Device) -> bool:
        try:
            from winrt.windows.devices.enumeration import DeviceInformation, DevicePairingKinds, DevicePairingResultStatus
        except Exception:
            return False
        aep = self._aep.get(norm_addr(dev.address) or "")
        if not aep:
            log.info("pair: %s wasn't seen in a scan", dev.name)
            return False
        info = await DeviceInformation.create_from_id_async(aep)
        if info.pairing.is_paired:
            return True
        custom = info.pairing.custom

        def on_request(_sender, args) -> None:
            # Headphones and speakers use "just works" (confirm) or a fixed PIN, almost always 0000.
            try:
                if args.pairing_kind == DevicePairingKinds.PROVIDE_PIN:
                    args.accept("0000")
                else:
                    args.accept()
            except Exception as e:
                log.info("pairing request: %s", e)

        token = custom.add_pairing_requested(on_request)
        try:
            kinds = DevicePairingKinds.CONFIRM_ONLY | DevicePairingKinds.PROVIDE_PIN | DevicePairingKinds.CONFIRM_PIN_MATCH
            res = await asyncio.wait_for(custom.pair_async(kinds), 30)
        except Exception as e:
            log.info("pairing %s failed: %s", dev.name, e)
            return False
        finally:
            try:
                custom.remove_pairing_requested(token)
            except Exception:
                pass
        self._paired_cache = (0.0, [])
        ok = res.status in (DevicePairingResultStatus.PAIRED, DevicePairingResultStatus.ALREADY_PAIRED)
        if not ok:
            log.info("pairing %s: %s", dev.name, res.status)
        return ok

    async def connect(self, dev: Device) -> bool:
        """Ask the headset's audio driver to connect, then wait (briefly) for Windows to confirm.
        Headphones that are on and in range answer within a few seconds; anything else is reported
        as not connected, never as connected."""
        if await self._status(dev):
            return True
        try:
            targets = _match_endpoints(await self.com.call(_audio_endpoints), dev)
        except Exception as e:
            _warn_once("listing audio endpoints", e)
            targets = []
        if targets:
            try:
                await self.com.call(_ks_oneshot, targets[0]["filter"], KSPROPERTY_ONESHOT_RECONNECT)
                if await self._wait_connected(dev, True, 7):
                    return True
            except Exception as e:
                _warn_once("reconnecting through the audio driver", e)
        # Fallback: restart the device's audio services, which makes Windows page it again.
        try:
            await asyncio.get_running_loop().run_in_executor(None, _toggle_services, dev)
        except Exception as e:
            _warn_once("restarting Bluetooth audio services", e)
            return False
        return await self._wait_connected(dev, True, 7)

    async def disconnect(self, dev: Device) -> bool:
        try:
            eps = await self.com.call(_audio_endpoints)
            for e in _match_endpoints(eps, dev):
                await self.com.call(_ks_oneshot, e["filter"], KSPROPERTY_ONESHOT_DISCONNECT)
        except Exception as e:
            _warn_once("disconnecting a headset", e)
            return False
        return await self._wait_connected(dev, False, 6)

    # ------------------------------------------------------------- audio output
    async def audio_output(self) -> AudioOutput:
        def read():
            eps = _audio_endpoints(include_unplugged=False)
            default = _default_render_id()
            return next((e for e in eps if e["id"] == default), None)

        try:
            cur = await self.com.call(read)
            if cur:
                return AudioOutput(cur["name"], cur["address"], "bluetooth" if cur["bluetooth"] else "builtin")
        except Exception as e:
            _warn_once("reading the audio output (Core Audio)", e)
        try:
            from winrt.windows.devices.enumeration import DeviceInformation
            from winrt.windows.media.devices import AudioDeviceRole, MediaDevice

            info = await DeviceInformation.create_from_id_async(MediaDevice.get_default_audio_render_id(AudioDeviceRole.DEFAULT))
            return AudioOutput(info.name, None, "other")
        except Exception as e:
            _warn_once("reading the audio output (WinRT)", e)
        return AudioOutput("Speakers")

    async def set_audio_output(self, dev: Device) -> bool:
        def switch() -> bool:
            from pycaw.constants import ERole
            from pycaw.pycaw import AudioUtilities

            targets = [e for e in _match_endpoints(_audio_endpoints(include_unplugged=False), dev) if e["active"]]
            if not targets:
                return False
            AudioUtilities.SetDefaultDevice(targets[0]["id"], roles=[ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])
            return True

        try:
            return await self.com.call(switch)
        except Exception as e:
            _warn_once("switching the audio output", e)
            return False

    def output_matches(self, out: AudioOutput, dev: Device) -> bool:
        if out.address and dev.address:
            return norm_addr(out.address) == norm_addr(dev.address)
        return super().output_matches(out, dev)

    # ------------------------------------------------------------- diagnostics
    async def diagnose(self) -> dict:
        report: dict[str, Any] = {"radio": await self.radio_state()}
        try:
            import winrt  # noqa: F401
            import pycaw  # noqa: F401

            report["packages"] = "ok"
        except Exception as e:
            report["packages"] = f"missing: {e}"
        report["paired"] = [{"name": d.name, "address": d.address, "kind": d.kind, "connected": d.connected, "present": d.present}
                            for d in await self.paired()]
        try:
            eps = await self.com.call(_audio_endpoints)
            report["audio_endpoints"] = [{k: e[k] for k in ("name", "state", "active", "bluetooth", "address", "hands_free")} for e in eps]
        except Exception as e:
            report["audio_endpoints"] = f"error: {type(e).__name__}: {e}"
        report["audio_output"] = (await self.audio_output()).to_dict()
        report["nearby"] = [{"name": d.name, "address": d.address, "kind": d.kind, "rssi": d.rssi} for d in await self.scan(8)]
        return report

    async def wifi_direct_connect(self, peer: dict) -> Optional[str]:
        if not self.wifi_direct or not peer.get("wfd_id"):
            return None
        from winrt.windows.devices.wifidirect import WiFiDirectDevice

        dev = await WiFiDirectDevice.from_id_async(peer["wfd_id"])
        pairs = dev.get_connection_endpoint_pairs()
        return pairs[0].remote_host_name.display_name if pairs else None


def _toggle_services(dev: Device) -> bool:
    """Fallback for devices without an audio endpoint: turn the A2DP/HFP services off and on
    (BluetoothSetServiceState), which makes Windows reconnect them."""
    A2DP_SINK = "{0000110B-0000-1000-8000-00805F9B34FB}"
    HFP = "{0000111E-0000-1000-8000-00805F9B34FB}"

    class BLUETOOTH_DEVICE_INFO(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_uint32), ("Address", ctypes.c_uint64), ("ulClassofDevice", ctypes.c_uint32),
                    ("fConnected", ctypes.c_int), ("fRemembered", ctypes.c_int), ("fAuthenticated", ctypes.c_int),
                    ("stLastSeen", ctypes.c_byte * 16), ("stLastUsed", ctypes.c_byte * 16), ("szName", ctypes.c_wchar * 248)]

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    def guid(s: str) -> GUID:
        import uuid

        u = uuid.UUID(s)
        g = GUID(u.time_low, u.time_mid, u.time_hi_version)
        g.Data4[:] = list(u.bytes[8:])
        return g

    bth = ctypes.WinDLL("bthprops.cpl")  # type: ignore[attr-defined]
    info = BLUETOOTH_DEVICE_INFO()
    info.dwSize = ctypes.sizeof(info)
    info.Address = int((dev.address or "0").replace(":", ""), 16)
    for enable in (0, 1):
        for svc in (A2DP_SINK, HFP):
            g = guid(svc)
            bth.BluetoothSetServiceState(None, ctypes.byref(info), ctypes.byref(g), enable)
        time.sleep(0.5)
    return True
