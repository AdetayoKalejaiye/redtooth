"""Windows backend.

- Discovery and pairing: WinRT `Windows.Devices.Enumeration` through the
  `winrt-Windows.Devices.*` Python packages (installed with Redtooth on Windows).
  Pairing goes through `DeviceInformation.Pairing.PairAsync`, so the user
  never has to open Settings or put this PC in a "receive" mode.
- Connecting an already-paired headset: toggling its audio services with
  Win32 `BluetoothSetServiceState` (bthprops.cpl) makes Windows reconnect it.
- Output routing: Core Audio through pycaw (installed with Redtooth): list the
  playback endpoints, read the default, and set it with IPolicyConfig (the
  interface the Sound settings page uses; undocumented but stable since Vista).
  The AudioDeviceCmdlets PowerShell module is used only if pycaw can't load.
- Wi-Fi Direct: `Windows.Devices.WiFiDirect` (WiFiDirectDevice.FromIdAsync).

Not exercised on hardware in this build.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import re
from typing import Optional

from ..models import Device
from .base import AudioOutput, BluetoothPlatform, enrich, norm_addr, run

log = logging.getLogger("redtooth.windows")

A2DP_SINK = "{0000110B-0000-1000-8000-00805F9B34FB}"
HFP = "{0000111E-0000-1000-8000-00805F9B34FB}"


def _render_endpoints() -> tuple[list[dict], Optional[str]]:
    """Active playback endpoints (id, friendly name, whether it's Bluetooth) and the current default's id."""
    import warnings

    import comtypes
    from pycaw.constants import DEVICE_STATE, EDataFlow
    from pycaw.pycaw import AudioUtilities

    comtypes.CoInitialize()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            devs = AudioUtilities.GetAllDevices(data_flow=EDataFlow.eRender.value, device_state=DEVICE_STATE.ACTIVE.value)
            default = AudioUtilities.GetSpeakers()
        eps = []
        for d in devs:
            props = getattr(d, "properties", {}) or {}
            bt = any(isinstance(v, str) and v.upper().startswith(("BTHENUM", "BTHHFENUM", "BTHLE")) for v in props.values()) \
                or any(isinstance(v, str) and "BTHENUM" in v.upper() for v in props.values())
            eps.append({"id": d.id, "name": d.FriendlyName or "", "bluetooth": bt})
        return eps, getattr(default, "id", None)
    finally:
        comtypes.CoUninitialize()


def _pick_endpoint(eps: list[dict], names: list[str]) -> Optional[dict]:
    """Windows names endpoints "Headphones (WH-1000XM5)"; match the Bluetooth name or model either way round."""
    wanted = [n.lower() for n in names if n]

    def hit(e: dict) -> bool:
        full = e["name"].lower()
        m = re.search(r"\((.+?)\)", full)
        inner = (m.group(1) if m else full).replace("hands-free ag audio", "").replace("stereo", "").strip()
        return any(w in full or (inner and inner in w) for w in wanted)

    matches = [e for e in eps if hit(e)]
    matches.sort(key=lambda e: ("hands-free" in e["name"].lower(), not e["bluetooth"], len(e["name"])))
    return matches[0] if matches else None


def _set_default_render(names: list[str]) -> bool:
    """Make the playback endpoint whose name matches the device the default, for music and calls.
    Stereo endpoints win over hands-free ones ("Headphones (WH-1000XM5)" over "Headset (… Hands-Free)")."""
    import comtypes
    from pycaw.constants import ERole
    from pycaw.pycaw import AudioUtilities

    eps, _ = _render_endpoints()
    pick = _pick_endpoint(eps, names)
    if not pick:
        return False
    comtypes.CoInitialize()
    try:
        AudioUtilities.SetDefaultDevice(pick["id"], roles=[ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])
        return True
    finally:
        comtypes.CoUninitialize()


def _addr_from_id(s: str) -> Optional[str]:
    m = re.search(r"([0-9A-F]{12})(?:[^0-9A-F]|$)", s.upper())
    return norm_addr(m.group(1)) if m else None


class WindowsBluetooth(BluetoothPlatform):
    name = "windows"

    def __init__(self) -> None:
        try:
            import winrt.windows.devices.wifidirect  # noqa: F401

            self.wifi_direct = True
        except Exception:
            self.wifi_direct = False

    async def _ps(self, script: str, timeout: float = 15) -> str:
        _, out = await run("powershell", "-NoProfile", "-NonInteractive", "-Command", script, timeout=timeout)
        return out

    async def paired(self) -> list[Device]:
        out = await self._ps(
            "Get-PnpDevice -Class Bluetooth | Where-Object {$_.InstanceId -like 'BTHENUM\\DEV_*' -or $_.InstanceId -like 'BTHLE\\DEV_*'} | "
            "Select-Object FriendlyName,InstanceId,Status | ConvertTo-Json"
        )
        try:
            rows = json.loads(out or "[]")
        except json.JSONDecodeError:
            return []
        rows = rows if isinstance(rows, list) else [rows]
        devs = []
        for r in rows:
            addr = _addr_from_id(r.get("InstanceId", ""))
            if not addr:
                continue
            devs.append(enrich(Device(id=f"bt:{addr}", name=r.get("FriendlyName", addr), address=addr, paired=True, connected=r.get("Status") == "OK", sources=["paired"])))
        return devs

    async def scan(self, seconds: float = 5.0) -> list[Device]:
        try:
            from winrt.windows.devices.bluetooth import BluetoothDevice
            from winrt.windows.devices.enumeration import DeviceInformation
        except Exception:
            return []
        selector = BluetoothDevice.get_device_selector_from_pairing_state(False)
        infos = await asyncio.wait_for(DeviceInformation.find_all_async_aqs_filter(selector), seconds + 10)
        devs = []
        for info in infos:
            addr = _addr_from_id(info.id)
            d = enrich(Device(id=f"bt:{addr}", name=info.name or addr or "Unknown", address=addr, discoverable=True, sources=["nearby"]))
            d.__dict__["_winrt_id"] = info.id
            devs.append(d)
        return devs

    async def pair(self, dev: Device) -> bool:
        try:
            from winrt.windows.devices.enumeration import DeviceInformation, DevicePairingResultStatus
        except Exception:
            return False
        wid = dev.__dict__.get("_winrt_id")
        if not wid:
            return False
        info = await DeviceInformation.create_from_id_async(wid)
        res = await info.pairing.pair_async()
        return res.status in (DevicePairingResultStatus.PAIRED, DevicePairingResultStatus.ALREADY_PAIRED)

    def _set_services(self, dev: Device, enable: bool) -> bool:
        """BluetoothSetServiceState on A2DP + HFP: Windows' own trick for (re)connecting audio."""

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
        ok = True
        for svc in (A2DP_SINK, HFP):
            g = guid(svc)
            ok &= bth.BluetoothSetServiceState(None, ctypes.byref(info), ctypes.byref(g), 1 if enable else 0) == 0
        return ok

    async def connect(self, dev: Device) -> bool:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._set_services, dev, False)
        await asyncio.sleep(0.5)
        ok = await loop.run_in_executor(None, self._set_services, dev, True)
        for _ in range(20):
            for d in await self.paired():
                if d.address == dev.address and d.connected:
                    return True
            await asyncio.sleep(0.5)
        return ok

    async def disconnect(self, dev: Device) -> bool:
        return await asyncio.get_running_loop().run_in_executor(None, self._set_services, dev, False)

    async def audio_output(self) -> AudioOutput:
        loop = asyncio.get_running_loop()
        try:
            eps, default_id = await loop.run_in_executor(None, _render_endpoints)
            cur = next((e for e in eps if e["id"] == default_id), None)
            if cur:
                return AudioOutput(cur["name"], None, "bluetooth" if cur["bluetooth"] else "other")
        except ImportError:
            pass
        except Exception as e:
            log.info("Core Audio read failed: %s", e)
        out = await self._ps("Import-Module AudioDeviceCmdlets; Get-AudioDevice -Playback | Select-Object Name,ID | ConvertTo-Json", timeout=8)
        try:
            d = json.loads(out)
            return AudioOutput(d["Name"], None, "other")
        except Exception:
            return AudioOutput("Unknown")

    async def set_audio_output(self, dev: Device) -> bool:
        names = [n for n in (dev.name, dev.model, dev.short_model) if n]
        try:
            return await asyncio.get_running_loop().run_in_executor(None, _set_default_render, names)
        except ImportError:
            pass
        except Exception as e:
            log.info("Core Audio switch failed: %s", e)
        short = dev.name.replace("'", "''")
        out = await self._ps(
            f"Import-Module AudioDeviceCmdlets; $d = Get-AudioDevice -List | Where-Object {{$_.Type -eq 'Playback' -and $_.Name -like '*{short}*'}} | Select-Object -First 1; "
            "if ($d) { Set-AudioDevice -ID $d.ID | Out-Null; 'ok' }",
            timeout=8,
        )
        return "ok" in out

    async def wifi_direct_connect(self, peer: dict) -> Optional[str]:
        if not self.wifi_direct or not peer.get("wfd_id"):
            return None
        from winrt.windows.devices.wifidirect import WiFiDirectDevice

        dev = await WiFiDirectDevice.from_id_async(peer["wfd_id"])
        pairs = dev.get_connection_endpoint_pairs()
        return pairs[0].remote_host_name.display_name if pairs else None
