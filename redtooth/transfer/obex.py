"""Send a file to a paired phone over plain Bluetooth (OBEX Object Push), the way Windows'
"Bluetooth File Transfer" does. Android phones show an Accept prompt and save to Downloads; this
needs no app on the phone. (iPhones don't accept Bluetooth file transfers at all.)

Windows: RFCOMM through Winsock (AF_BTH) via ctypes, connecting by service UUID so Windows does the
SDP lookup for the Object Push channel itself.
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
import threading
from typing import Callable, Optional

OPP_UUID = "{00001105-0000-1000-8000-00805F9B34FB}"


class ObexError(Exception):
    def __init__(self, message: str, kind: str = "failed"):
        super().__init__(message)
        self.kind = kind  # failed | declined | unreachable | cancelled


# ----------------------------------------------------------------- OBEX packets (pure, testable)
def connect_packet(max_len: int = 0xFFFF) -> bytes:
    return struct.pack(">BHBBH", 0x80, 7, 0x10, 0x00, max_len)


def disconnect_packet() -> bytes:
    return struct.pack(">BH", 0x81, 3)


def name_header(name: str) -> bytes:
    data = name.encode("utf-16-be") + b"\x00\x00"
    return struct.pack(">BH", 0x01, 3 + len(data)) + data


def length_header(n: int) -> bytes:
    return struct.pack(">BI", 0xC3, n & 0xFFFFFFFF)


def body_header(chunk: bytes, final: bool) -> bytes:
    return struct.pack(">BH", 0x49 if final else 0x48, 3 + len(chunk)) + chunk


def put_packet(headers: bytes, final: bool) -> bytes:
    return struct.pack(">BH", 0x82 if final else 0x02, 3 + len(headers)) + headers


RESPONSES = {0x90: "continue", 0xA0: "ok", 0xC3: "forbidden", 0xC1: "unauthorized", 0xCD: "too large", 0xCF: "unsupported type",
             0xC0: "bad request", 0xC6: "not acceptable", 0xD3: "unavailable", 0xD0: "server error"}


class Channel:
    """A byte stream (RFCOMM socket on Windows; anything with send/recv for tests)."""

    def send(self, b: bytes) -> None: ...
    def recv(self, n: int) -> bytes: ...
    def close(self) -> None: ...


def _read_response(ch: Channel) -> tuple[int, bytes]:
    head = _recv_exact(ch, 3)
    code, length = head[0], struct.unpack(">H", head[1:3])[0]
    return code, _recv_exact(ch, max(0, length - 3))


def _recv_exact(ch: Channel, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        part = ch.recv(n - len(buf))
        if not part:
            raise ObexError("the phone closed the connection")
        buf += part
    return buf


def push(ch: Channel, path: str, progress: Callable[[int, str], None] = lambda n, s: None,
         cancel: Optional[threading.Event] = None, name: Optional[str] = None) -> None:
    """Run one Object Push: CONNECT, PUT (name, length, body…), DISCONNECT. Raises ObexError."""
    size = os.path.getsize(path)
    ch.send(connect_packet())
    code, body = _read_response(ch)
    if code != 0xA0:
        raise ObexError(f"the phone refused the connection ({RESPONSES.get(code, hex(code))})")
    their_max = struct.unpack(">H", body[2:4])[0] if len(body) >= 4 else 255
    max_len = max(255, min(their_max, 0xFFFF))
    first = name_header(name or os.path.basename(path)) + length_header(size)
    progress(0, "waiting")  # the phone shows "Accept file?" now; its answer comes with the first response
    sent = 0
    with open(path, "rb") as f:
        headers = first
        while True:
            if cancel is not None and cancel.is_set():
                raise ObexError("cancelled", "cancelled")
            room = max_len - 3 - len(headers) - 3
            chunk = f.read(max(0, room))
            final = sent + len(chunk) >= size
            ch.send(put_packet(headers + body_header(chunk, final), final))
            code, _ = _read_response(ch)
            if code in (0xC3, 0xC1, 0xC6):
                raise ObexError("declined on the phone", "declined")
            if final:
                if code != 0xA0:
                    raise ObexError(f"the phone didn't save it ({RESPONSES.get(code, hex(code))})")
                progress(size, "saved")
                break
            if code != 0x90:
                raise ObexError(f"the phone stopped the transfer ({RESPONSES.get(code, hex(code))})")
            sent += len(chunk)
            progress(sent, "sending")
            headers = b""
    try:
        ch.send(disconnect_packet())
        _read_response(ch)
    except Exception:
        pass


# ----------------------------------------------------------------- Windows RFCOMM socket
class WinRfcomm(Channel):
    AF_BTH, SOCK_STREAM, BTHPROTO_RFCOMM = 32, 1, 3

    def __init__(self, address: str, timeout_s: float = 90):
        import socket  # noqa: F401 - makes sure Winsock is started (WSAStartup)

        from ..bt.win32bt import GUID, _guid, addr_int

        class SOCKADDR_BTH(ctypes.Structure):
            _pack_ = 1
            _fields_ = [("addressFamily", ctypes.c_ushort), ("btAddr", ctypes.c_ulonglong), ("serviceClassId", GUID), ("port", ctypes.c_ulong)]

        ws = ctypes.WinDLL("ws2_32")
        ws.socket.restype = ctypes.c_size_t
        ws.socket.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        ws.connect.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
        ws.send.argtypes = [ctypes.c_size_t, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        ws.recv.argtypes = [ctypes.c_size_t, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        ws.setsockopt.argtypes = [ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        ws.closesocket.argtypes = [ctypes.c_size_t]
        self.ws = ws
        s = ws.socket(self.AF_BTH, self.SOCK_STREAM, self.BTHPROTO_RFCOMM)
        if s == ctypes.c_size_t(-1).value:
            raise ObexError(f"couldn't open a Bluetooth socket (error {ws.WSAGetLastError()})")
        self.s = s
        ms = ctypes.c_uint32(int(timeout_s * 1000))
        for opt in (0x1005, 0x1006):  # SO_SNDTIMEO, SO_RCVTIMEO
            ws.setsockopt(s, 0xFFFF, opt, ctypes.byref(ms), 4)
        sa = SOCKADDR_BTH(self.AF_BTH, addr_int(address), _guid(OPP_UUID), 0)  # port 0 + UUID: Windows finds the channel
        if ws.connect(s, ctypes.byref(sa), ctypes.sizeof(sa)) != 0:
            err = ws.WSAGetLastError()
            self.close()
            hint = {10060: "it didn't answer (is it nearby with Bluetooth on?)", 10061: "it isn't accepting Bluetooth files",
                    10050: "this computer's Bluetooth is off", 10065: "it's out of range or switched off",
                    10108: "it doesn't offer Bluetooth file transfer"}.get(err, f"error {err}")
            raise ObexError(f"couldn't reach the phone: {hint}", "unreachable")

    def send(self, b: bytes) -> None:
        view = memoryview(b)
        while len(view):
            n = self.ws.send(self.s, bytes(view[:65536]), min(len(view), 65536), 0)
            if n <= 0:
                raise ObexError(f"sending failed (error {self.ws.WSAGetLastError()})")
            view = view[n:]

    def recv(self, n: int) -> bytes:
        buf = ctypes.create_string_buffer(n)
        got = self.ws.recv(self.s, buf, n, 0)
        if got < 0:
            err = self.ws.WSAGetLastError()
            raise ObexError("the phone didn't answer in time" if err == 10060 else f"receive failed (error {err})")
        return buf.raw[:got]

    def close(self) -> None:
        if getattr(self, "s", None) is not None:
            self.ws.closesocket(self.s)
            self.s = None


def send_file(address: str, path: str, progress: Callable[[int, str], None], cancel: threading.Event) -> None:
    if not sys.platform.startswith("win"):
        raise ObexError("Bluetooth file transfer is only wired up on Windows so far")
    ch = WinRfcomm(address)
    try:
        push(ch, path, progress, cancel)
    finally:
        ch.close()
