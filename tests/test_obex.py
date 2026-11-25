"""OBEX Object Push against a fake phone: packet layout, chunking to the phone's max size, accept and decline."""
import struct

import pytest

from redtooth.transfer.obex import ObexError, push


class FakePhone:
    """Speaks the receiving side of OBEX OPP well enough to check what we send."""

    def __init__(self, max_len=1024, decline=False):
        self.max_len, self.decline = max_len, decline
        self.inbox, self.out, self.name, self.length, self.body, self.saved = b"", b"", None, None, b"", False

    def send(self, b):
        self.inbox += b
        while len(self.inbox) >= 3:
            n = struct.unpack(">H", self.inbox[1:3])[0]
            if len(self.inbox) < n:
                return
            pkt, self.inbox = self.inbox[:n], self.inbox[n:]
            assert n <= max(self.max_len, 7), "packet bigger than the phone allows"
            self.handle(pkt)

    def handle(self, pkt):
        op = pkt[0]
        if op == 0x80:
            self.out += struct.pack(">BHBBH", 0xA0, 7, 0x10, 0, self.max_len)
            return
        if op == 0x81:
            self.out += struct.pack(">BH", 0xA0, 3)
            return
        i = 3
        while i < len(pkt):
            hi = pkt[i]
            if hi == 0xC3:
                self.length = struct.unpack(">I", pkt[i + 1:i + 5])[0]
                i += 5
                continue
            ln = struct.unpack(">H", pkt[i + 1:i + 3])[0]
            data = pkt[i + 3:i + ln]
            if hi == 0x01:
                self.name = data[:-2].decode("utf-16-be")
            elif hi in (0x48, 0x49):
                self.body += data
            i += ln
        if self.decline:
            self.out += struct.pack(">BH", 0xC3, 3)
        elif op == 0x82:
            self.saved = True
            self.out += struct.pack(">BH", 0xA0, 3)
        else:
            self.out += struct.pack(">BH", 0x90, 3)

    def recv(self, n):
        b, self.out = self.out[:n], self.out[n:]
        return b

    def close(self):
        pass


def test_push_accepted(tmp_path):
    p = tmp_path / "Term Paper — final.pdf"
    data = bytes(range(256)) * 40  # 10 KB, several packets at max 1024
    p.write_bytes(data)
    phone, stages = FakePhone(max_len=1024), []
    push(phone, str(p), lambda n, s: stages.append(s))
    assert phone.saved and phone.body == data and phone.length == len(data) and phone.name == "Term Paper — final.pdf"
    assert stages[0] == "waiting" and stages[-1] == "saved" and "sending" in stages


def test_push_declined(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello")
    with pytest.raises(ObexError) as e:
        push(FakePhone(decline=True), str(p))
    assert e.value.kind == "declined"
