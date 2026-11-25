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

    async def scan(self, seconds: float = 5.0) -> list[Device]:
        await run("bluetoothctl", "--timeout", str(int(max(seconds, 2))), "scan", "on", timeout=seconds + 5)
        devs = await self._list("", "nearby")
        return [d for d in devs if d.rssi is not None or not d.paired]

    async def pair(self, dev: Device) -> bool:
        code, out = await run("bluetoothctl", "--timeout", "25", "pair", dev.address or "", timeout=30)
        ok = "Pairing successful" in out or "AlreadyExists" in out
        if ok:
            await run("bluetoothctl", "trust", dev.address or "", timeout=5)
        return ok

    async def connect(self, dev: Device) -> bool:
        code, out = await run("bluetoothctl", "--timeout", "15", "connect", dev.address or "", timeout=20)
        return "Connection successful" in out or "AlreadyConnected" in out

    async def disconnect(self, dev: Device) -> bool:
        _, out = await run("bluetoothctl", "disconnect", dev.address or "", timeout=10)
        return "Successful disconnected" in out or "not connected" in out.lower()

    @staticmethod
    def _sink_addr(sink: str) -> Optional[str]:
        m = re.search(r"bluez_(?:output|sink)\.([0-9A-F_]{17})", sink, re.I)
        return norm_addr(m.group(1).replace("_", ":")) if m else None

    async def audio_output(self) -> AudioOutput:
        code, sink = await run("pactl", "get-default-sink", timeout=3)
        if code != 0:
            return AudioOutput("System default")
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
