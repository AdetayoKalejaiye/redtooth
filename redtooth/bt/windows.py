"""Windows backend.

- Discovery and pairing: WinRT `Windows.Devices.Enumeration` through the
  `winrt-Windows.Devices.*` Python packages (pip install winrt-runtime
  winrt-Windows.Devices.Enumeration winrt-Windows.Devices.Bluetooth).
  Pairing goes through `DeviceInformation.Pairing.PairAsync`, so the user
  never has to open Settings or put this PC in a "receive" mode.
- Connecting an already-paired headset: toggling its audio services with
  Win32 `BluetoothSetServiceState` (bthprops.cpl) makes Windows reconnect it.
- Output routing: Windows has no public API to change the default render
  device; we use the AudioDeviceCmdlets PowerShell module if present
  (Install-Module AudioDeviceCmdlets), which wraps IPolicyConfig.
- Wi-Fi Direct: `Windows.Devices.WiFiDirect` (WiFiDirectDevice.FromIdAsync).

Not exercised on hardware in this build.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import re
from typing import Optional

from ..models import Device
from .base import AudioOutput, BluetoothPlatform, enrich, norm_addr, run

A2DP_SINK = "{0000110B-0000-1000-8000-00805F9B34FB}"
HFP = "{0000111E-0000-1000-8000-00805F9B34FB}"


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
        out = await self._ps("Import-Module AudioDeviceCmdlets; Get-AudioDevice -Playback | Select-Object Name,ID | ConvertTo-Json", timeout=8)
        try:
            d = json.loads(out)
            return AudioOutput(d["Name"], None, "other")
        except Exception:
            return AudioOutput("Unknown")

    async def set_audio_output(self, dev: Device) -> bool:
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
