"""Linux backend: BlueZ via `bluetoothctl`, audio via PipeWire/PulseAudio `pactl`,
Wi-Fi Direct via `wpa_cli` P2P.

Not exercised on hardware in this build; command formats follow BlueZ 5.6x and
pactl 16+.
"""
from __future__ import annotations

import asyncio
import re
import shutil
from typing import Optional

from ..models import Device
from .base import AudioOutput, BluetoothPlatform, enrich, norm_addr, run

DEV_LINE = re.compile(r"^Device ([0-9A-F:]{17}) (.+)$", re.M)


class LinuxBluetooth(BluetoothPlatform):
    name = "linux-bluez"

    def __init__(self) -> None:
        self.wifi_direct = bool(shutil.which("wpa_cli"))

    async def _info(self, addr: str, source: str) -> Device:
        _, out = await run("bluetoothctl", "info", addr, timeout=5)
        name = re.search(r"Name: (.+)", out)
        icon = re.search(r"Icon: (.+)", out)
        rssi = re.search(r"RSSI: (?:0x[0-9a-f]+ \()?(-?\d+)", out)
        dev = Device(
            id=f"bt:{addr}",
            name=(name.group(1).strip() if name else addr),
            address=addr,
            paired="Paired: yes" in out,
            connected="Connected: yes" in out,
            rssi=int(rssi.group(1)) if rssi else None,
            sources=[source],
        )
        return enrich(dev, icon.group(1) if icon else "")

    async def _list(self, which: str, source: str) -> list[Device]:
        code, out = await run("bluetoothctl", "devices", which, timeout=5) if which else await run("bluetoothctl", "devices", timeout=5)
        if code != 0 and which == "Paired":  # older BlueZ
            code, out = await run("bluetoothctl", "paired-devices", timeout=5)
        addrs = [m.group(1) for m in DEV_LINE.finditer(out)]
        return list(await asyncio.gather(*(self._info(a, source) for a in addrs)))

    async def paired(self) -> list[Device]:
        return await self._list("Paired", "paired")

    async def radio_state(self) -> str:
        if not shutil.which("bluetoothctl"):
            return "unknown"
        code, out = await run("bluetoothctl", "show", timeout=4)
        if "No default controller" in out or (code != 0 and "Controller" not in out):
            return "missing"
        return "on" if "Powered: yes" in out else "off"

    def setup_issue(self) -> Optional[str]:
        miss = [t for t, why in (("bluetoothctl", "Bluetooth (package bluez)"), ("pactl", "audio outputs (PipeWire/PulseAudio pactl)"),
                                  ("gdbus", "sending files to phones (glib2 gdbus + bluez-obexd)")) if not shutil.which(t)]
        if not miss:
            return None
        return f"Some tools aren't installed ({', '.join(miss)}). Run: sudo apt install bluez bluez-obexd pulseaudio-utils libglib2.0-bin"

    def push_file(self, address: str, path: str, progress, cancel) -> None:
        """OBEX Object Push through BlueZ's obexd over D-Bus (what GNOME's "Send files" uses)."""
        import os
        import subprocess
        import time

        from ..transfer.obex import ObexError

        def gdbus(*args: str, timeout: float = 30) -> str:
            r = subprocess.run(["gdbus", "call", "--session", "--dest", "org.bluez.obex", *args], capture_output=True, text=True, timeout=timeout)
            if r.returncode != 0:
                raise ObexError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "obexd error",
                                "unreachable" if "connect" in r.stderr.lower() or "host is down" in r.stderr.lower() else "failed")
            return r.stdout

        out = gdbus("--object-path", "/org/bluez/obex", "--method", "org.bluez.obex.Client1.CreateSession", address, "{'Target': <'opp'>}", timeout=40)
        m = re.search(r"'(/org/bluez/obex/client/session\d+)'", out)
        if not m:
            raise ObexError("couldn't start a Bluetooth session with the phone", "unreachable")
        session = m.group(1)
        try:
            out = gdbus("--object-path", session, "--method", "org.bluez.obex.ObjectPush1.SendFile", os.path.abspath(path))
            t = re.search(r"'(/org/bluez/obex/client/session\d+/transfer\d+)'", out)
            if not t:
                raise ObexError("the phone didn't accept the transfer")
            transfer, size = t.group(1), os.path.getsize(path)
            progress(0, "waiting")
            while True:
                if cancel.is_set():
                    subprocess.run(["gdbus", "call", "--session", "--dest", "org.bluez.obex", "--object-path", transfer,
                                    "--method", "org.bluez.obex.Transfer1.Cancel"], capture_output=True, timeout=5)
                    raise ObexError("cancelled", "cancelled")
                try:
                    st = gdbus("--object-path", transfer, "--method", "org.freedesktop.DBus.Properties.GetAll", "org.bluez.obex.Transfer1", timeout=5)
                except ObexError:
                    st = "'Status': <'complete'>"  # the transfer object disappears when it's done
                status = re.search(r"'Status': <'(\w+)'>", st)
                done = re.search(r"'Transferred': <uint64 (\d+)>", st)
                s = status.group(1) if status else "active"
                if s == "complete":
                    progress(size, "saved")
                    return
                if s == "error":
                    raise ObexError("declined on the phone, or the connection dropped", "declined")
                if s == "active" and done:
                    progress(int(done.group(1)), "sending")
                time.sleep(0.3)
        finally:
            subprocess.run(["gdbus", "call", "--session", "--dest", "org.bluez.obex", "--object-path", "/org/bluez/obex",
                            "--method", "org.bluez.obex.Client1.RemoveSession", session], capture_output=True, timeout=5)

    async def scan(self, seconds: float = 5.0) -> list[Device]:
        await run("bluetoothctl", "--timeout", str(int(max(seconds, 2))), "scan", "on", timeout=seconds + 5)
        devs = await self._list("", "nearby")
        out = [d for d in devs if not d.paired and (d.rssi is not None)]
        for d in out:
            d.discoverable = True
        return out

    async def pair(self, dev: Device) -> bool:
        code, out = await run("bluetoothctl", "--timeout", "25", "pair", dev.address or "", timeout=30)
        ok = "Pairing successful" in out or "AlreadyExists" in out
        if ok:
            await run("bluetoothctl", "trust", dev.address or "", timeout=5)
        return ok

    async def connect(self, dev: Device) -> bool:
        await run("bluetoothctl", "--timeout", "12", "connect", dev.address or "", timeout=16)
        # Trust what BlueZ reports afterwards, not the command's message.
        return (await self._info(dev.address or "", "paired")).connected

    async def disconnect(self, dev: Device) -> bool:
        await run("bluetoothctl", "disconnect", dev.address or "", timeout=10)
        return not (await self._info(dev.address or "", "paired")).connected

    @staticmethod
    def _sink_addr(sink: str) -> Optional[str]:
        m = re.search(r"bluez_(?:output|sink)\.([0-9A-F_]{17})", sink, re.I)
        return norm_addr(m.group(1).replace("_", ":")) if m else None

    async def audio_output(self) -> AudioOutput:
        code, sink = await run("pactl", "get-default-sink", timeout=3)
        if code != 0:
            return AudioOutput("an output Redtooth can't read", None, "other", known=False)
        sink = sink.strip()
        addr = self._sink_addr(sink)
        name = sink
        _, desc = await run("pactl", "list", "sinks", timeout=3)
        for block in desc.split("Sink #"):
            if f"Name: {sink}" in block:
                m = re.search(r"Description: (.+)", block)
                name = m.group(1).strip() if m else sink
        return AudioOutput(name, addr, "bluetooth" if addr else "builtin")

    async def set_audio_output(self, dev: Device) -> bool:
        target = (dev.address or "").replace(":", "_").upper()
        for _ in range(10):  # the BT sink appears a moment after connecting
            _, out = await run("pactl", "list", "short", "sinks", timeout=3)
            for line in out.splitlines():
                cols = line.split("\t")
                if len(cols) > 1 and target and target in cols[1].upper():
                    await run("pactl", "set-default-sink", cols[1], timeout=3)
                    _, inputs = await run("pactl", "list", "short", "sink-inputs", timeout=3)
                    for inp in inputs.splitlines():
                        await run("pactl", "move-sink-input", inp.split("\t")[0], cols[1], timeout=3)
                    return True
            await asyncio.sleep(0.4)
        return False

    async def wifi_direct_connect(self, peer: dict) -> Optional[str]:
        """Join a P2P group with the peer (push-button method); return its group IP."""
        mac = peer.get("p2p_address")
        if not self.wifi_direct or not mac:
            return None
        await run("wpa_cli", "p2p_find", "5", timeout=8)
        await asyncio.sleep(3)
        code, out = await run("wpa_cli", "p2p_connect", mac, "pbc", "join", timeout=30)
        if "OK" not in out:
            return None
        return peer.get("p2p_ip")  # peer advertises its group-owner address via the directory
