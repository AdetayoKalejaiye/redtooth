"""Classic Win32 Bluetooth API (bthprops.cpl / BluetoothApis.dll), through ctypes.

Built into every Windows since XP SP2 and needs no Python packages, so it's Redtooth's primary way
to list paired devices with their real connected state (fConnected), scan for discoverable devices
(device inquiry), pair, and restart a device's audio services. Bluetooth LE-only devices aren't covered
here; the WinRT layer adds those when it's available.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
from dataclasses import dataclass
from typing import Optional


class SYSTEMTIME(ctypes.Structure):
    _fields_ = [(n, wt.WORD) for n in ("wYear", "wMonth", "wDayOfWeek", "wDay", "wHour", "wMinute", "wSecond", "wMilliseconds")]


class BLUETOOTH_DEVICE_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("Address", ctypes.c_ulonglong),
        ("ulClassofDevice", wt.ULONG),
        ("fConnected", wt.BOOL),
        ("fRemembered", wt.BOOL),
        ("fAuthenticated", wt.BOOL),
        ("stLastSeen", SYSTEMTIME),
        ("stLastUsed", SYSTEMTIME),
        ("szName", wt.WCHAR * 248),
    ]


class BLUETOOTH_DEVICE_SEARCH_PARAMS(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("fReturnAuthenticated", wt.BOOL),
        ("fReturnRemembered", wt.BOOL),
        ("fReturnUnknown", wt.BOOL),
        ("fReturnConnected", wt.BOOL),
        ("fIssueInquiry", wt.BOOL),
        ("cTimeoutMultiplier", ctypes.c_ubyte),
        ("hRadio", wt.HANDLE),
    ]


class BLUETOOTH_FIND_RADIO_PARAMS(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


A2DP_SINK = "{0000110B-0000-1000-8000-00805F9B34FB}"
HFP = "{0000111E-0000-1000-8000-00805F9B34FB}"
HSP = "{00001108-0000-1000-8000-00805F9B34FB}"
AVRCP = "{0000110E-0000-1000-8000-00805F9B34FB}"


@dataclass
class BtInfo:
    address: str
    name: str
    cod: int
    connected: bool
    remembered: bool
    authenticated: bool

    @property
    def paired(self) -> bool:
        return self.authenticated or self.remembered


def _guid(s: str) -> GUID:
    import uuid

    u = uuid.UUID(s)
    g = GUID(u.time_low, u.time_mid, u.time_hi_version)
    g.Data4[:] = list(u.bytes[8:])
    return g


def addr_str(a: int) -> str:
    h = f"{a:012X}"
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def addr_int(s: str) -> int:
    return int((s or "0").replace(":", "").replace("-", ""), 16)


def cod_kind(cod: int) -> str:
    """Class of Device → headphones / speaker / phone / laptop."""
    major, minor = (cod >> 8) & 0x1F, (cod >> 2) & 0x3F
    if major == 4:  # audio/video
        if minor in (5, 15, 8, 10):
            return "speaker"
        if minor in (1, 2, 6, 7):
            return "headphones"
    if major == 2:
        return "phone"
    if major == 1:
        return "laptop"
    return ""


class Win32Bluetooth:
    def __init__(self) -> None:
        if not sys.platform.startswith("win"):
            raise OSError("Windows only")
        try:
            self.dll = ctypes.WinDLL("BluetoothApis.dll")
        except OSError:
            self.dll = ctypes.WinDLL("bthprops.cpl")
        d = self.dll
        d.BluetoothFindFirstRadio.restype = wt.HANDLE
        d.BluetoothFindFirstRadio.argtypes = [ctypes.POINTER(BLUETOOTH_FIND_RADIO_PARAMS), ctypes.POINTER(wt.HANDLE)]
        d.BluetoothFindRadioClose.argtypes = [wt.HANDLE]
        d.BluetoothFindFirstDevice.restype = wt.HANDLE
        d.BluetoothFindFirstDevice.argtypes = [ctypes.POINTER(BLUETOOTH_DEVICE_SEARCH_PARAMS), ctypes.POINTER(BLUETOOTH_DEVICE_INFO)]
        d.BluetoothFindNextDevice.restype = wt.BOOL
        d.BluetoothFindNextDevice.argtypes = [wt.HANDLE, ctypes.POINTER(BLUETOOTH_DEVICE_INFO)]
        d.BluetoothFindDeviceClose.argtypes = [wt.HANDLE]
        d.BluetoothGetDeviceInfo.restype = wt.DWORD
        d.BluetoothGetDeviceInfo.argtypes = [wt.HANDLE, ctypes.POINTER(BLUETOOTH_DEVICE_INFO)]
        d.BluetoothSetServiceState.restype = wt.DWORD
        d.BluetoothSetServiceState.argtypes = [wt.HANDLE, ctypes.POINTER(BLUETOOTH_DEVICE_INFO), ctypes.POINTER(GUID), wt.DWORD]
        d.BluetoothAuthenticateDeviceEx.restype = wt.DWORD
        d.BluetoothAuthenticateDeviceEx.argtypes = [wt.HWND, wt.HANDLE, ctypes.POINTER(BLUETOOTH_DEVICE_INFO), ctypes.c_void_p, ctypes.c_int]
        self.kernel32 = ctypes.WinDLL("kernel32")
        self.kernel32.CloseHandle.argtypes = [wt.HANDLE]

    # ------------------------------------------------------------- radio
    def radio(self) -> Optional[int]:
        """Handle of the first Bluetooth radio, or None if there isn't one (or it's switched off)."""
        p = BLUETOOTH_FIND_RADIO_PARAMS(ctypes.sizeof(BLUETOOTH_FIND_RADIO_PARAMS))
        h = wt.HANDLE()
        find = self.dll.BluetoothFindFirstRadio(ctypes.byref(p), ctypes.byref(h))
        if not find:
            return None
        self.dll.BluetoothFindRadioClose(find)
        return h.value

    def has_radio(self) -> bool:
        h = self.radio()
        if h:
            self.kernel32.CloseHandle(h)
            return True
        return False

    # ------------------------------------------------------------- devices
    @staticmethod
    def _info(i: BLUETOOTH_DEVICE_INFO) -> BtInfo:
        return BtInfo(addr_str(i.Address), i.szName.strip(), i.ulClassofDevice, bool(i.fConnected), bool(i.fRemembered), bool(i.fAuthenticated))

    def devices(self, inquiry: bool = False, seconds: float = 0) -> list[BtInfo]:
        """Paired/remembered devices; with inquiry, also discoverable ones nearby (blocks for `seconds`)."""
        p = BLUETOOTH_DEVICE_SEARCH_PARAMS()
        p.dwSize = ctypes.sizeof(p)
        p.fReturnAuthenticated = p.fReturnRemembered = p.fReturnConnected = True
        p.fReturnUnknown = bool(inquiry)
        p.fIssueInquiry = bool(inquiry)
        p.cTimeoutMultiplier = max(1, min(48, round(seconds / 1.28))) if inquiry else 0
        p.hRadio = None
        info = BLUETOOTH_DEVICE_INFO()
        info.dwSize = ctypes.sizeof(info)
        out: list[BtInfo] = []
        find = self.dll.BluetoothFindFirstDevice(ctypes.byref(p), ctypes.byref(info))
        if not find:
            return out
        try:
            while True:
                out.append(self._info(info))
                info = BLUETOOTH_DEVICE_INFO()
                info.dwSize = ctypes.sizeof(info)
                if not self.dll.BluetoothFindNextDevice(find, ctypes.byref(info)):
                    break
        finally:
            self.dll.BluetoothFindDeviceClose(find)
        return out

    def info(self, address: str) -> Optional[BtInfo]:
        """Fresh state of one device (fConnected is live)."""
        h = self.radio()
        if not h:
            return None
        try:
            i = BLUETOOTH_DEVICE_INFO()
            i.dwSize = ctypes.sizeof(i)
            i.Address = addr_int(address)
            if self.dll.BluetoothGetDeviceInfo(h, ctypes.byref(i)) != 0:
                return None
            return self._info(i)
        finally:
            self.kernel32.CloseHandle(h)

    def set_services(self, address: str, enable: bool, services=(A2DP_SINK, HFP)) -> bool:
        h = self.radio()
        if not h:
            return False
        try:
            i = BLUETOOTH_DEVICE_INFO()
            i.dwSize = ctypes.sizeof(i)
            i.Address = addr_int(address)
            self.dll.BluetoothGetDeviceInfo(h, ctypes.byref(i))
            ok = False
            for svc in services:
                g = _guid(svc)
                ok |= self.dll.BluetoothSetServiceState(h, ctypes.byref(i), ctypes.byref(g), 1 if enable else 0) == 0
            return ok
        finally:
            self.kernel32.CloseHandle(h)

    def authenticate(self, address: str) -> int:
        """Pair. Headphones use "just works", so this usually completes without any window; if the device
        wants a PIN, Windows shows its own prompt. Returns the Win32 error code (0 = paired)."""
        h = self.radio()
        if not h:
            return 1
        try:
            i = BLUETOOTH_DEVICE_INFO()
            i.dwSize = ctypes.sizeof(i)
            i.Address = addr_int(address)
            self.dll.BluetoothGetDeviceInfo(h, ctypes.byref(i))
            return self.dll.BluetoothAuthenticateDeviceEx(None, h, ctypes.byref(i), None, 0)  # MITMProtectionNotRequired
        finally:
            self.kernel32.CloseHandle(h)
