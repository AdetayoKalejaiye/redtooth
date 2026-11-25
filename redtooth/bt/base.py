"""Platform interface for Bluetooth discovery, pairing, connection and audio routing."""
from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from ..models import MODEL_NAMES, Device


@dataclass
class AudioOutput:
    name: str
    address: Optional[str] = None  # Bluetooth address if the output is a BT device
    transport: str = "builtin"  # builtin | bluetooth | usb | hdmi | airplay | other

    def to_dict(self) -> dict:
        return {"name": self.name, "address": self.address, "transport": self.transport}


def friendly_model(name: str) -> tuple[Optional[str], Optional[str]]:
    for pat, full, short in MODEL_NAMES:
        if re.search(pat, name, re.I):
            return full, short
    return None, None


def classify(name: str, hint: str = "") -> str:
    """Guess a device kind from its name and OS class/icon hint."""
    h = (hint or "").lower()
    n = name.lower()
    if any(k in h for k in ("headset", "headphone", "audio-headset", "audio-headphones")):
        return "headphones"
    if "speaker" in h or "audio-card" in h or "loudspeaker" in h:
        return "speaker"
    if "phone" in h:
        return "phone"
    if "computer" in h or "laptop" in h:
        return "laptop"
    if re.search(r"airpods|buds|wh-|wf-|xm\d|qc ?\d|quietcomfort|headphone|headset|beats|jabra|earbud|momentum|sennheiser", n):
        return "headphones"
    if re.search(r"speaker|soundlink|flip|charge|boom|soundbar|homepod|sonos|bose home", n):
        return "speaker"
    if re.search(r"iphone|pixel|galaxy s|android|phone", n):
        return "phone"
    if re.search(r"macbook|laptop|thinkpad|surface|xps|notebook", n):
        return "laptop"
    if re.search(r"ipad|tablet|tab s", n):
        return "tablet"
    return "other"


def enrich(dev: Device, hint: str = "") -> Device:
    if dev.kind in ("other", "", None):
        dev.kind = classify(dev.name, hint)
    if not dev.model:
        dev.model, dev.short_model = friendly_model(dev.name)
    return dev


def norm_addr(addr: Optional[str]) -> Optional[str]:
    if not addr:
        return addr
    a = re.sub(r"[^0-9A-Fa-f]", "", addr).upper()
    return ":".join(a[i : i + 2] for i in range(0, 12, 2)) if len(a) == 12 else addr.upper()


class BluetoothPlatform(ABC):
    name = "abstract"
    wifi_direct = False

    @abstractmethod
    async def paired(self) -> list[Device]: ...

    @abstractmethod
    async def scan(self, seconds: float = 5.0) -> list[Device]:
        """Discoverable devices in range (headphones in pairing mode, etc.)."""

    @abstractmethod
    async def pair(self, dev: Device) -> bool: ...

    @abstractmethod
    async def connect(self, dev: Device) -> bool: ...

    @abstractmethod
    async def disconnect(self, dev: Device) -> bool: ...

    @abstractmethod
    async def audio_output(self) -> AudioOutput: ...

    @abstractmethod
    async def set_audio_output(self, dev: Device) -> bool: ...

    def output_matches(self, out: AudioOutput, dev: Device) -> bool:
        if out.address and dev.address:
            return norm_addr(out.address) == norm_addr(dev.address)
        return bool(out.name) and (out.name.lower() in dev.name.lower() or dev.name.lower() in out.name.lower())

    async def verify_audio_output(self, dev: Device, timeout: float = 6.0) -> bool:
        """Confirm the device is the active audio output; switch to it if the OS didn't."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        switched = False
        while loop.time() < deadline:
            out = await self.audio_output()
            if self.output_matches(out, dev):
                return True
            if not switched:
                switched = True
                try:
                    await self.set_audio_output(dev)
                except Exception:
                    pass
            await asyncio.sleep(0.3)
        return False

    async def wifi_direct_connect(self, peer: dict) -> Optional[str]:
        """Form a Wi-Fi Direct group with a peer; return the peer's IP on it."""
        return None


async def run(*cmd: str, timeout: float = 20.0) -> tuple[int, str]:
    """Run a command, return (exit code, combined output)."""
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timeout"
    return proc.returncode or 0, out.decode(errors="replace")


def detect_platform() -> BluetoothPlatform:
    import sys

    if sys.platform == "darwin":
        from .macos import MacBluetooth

        return MacBluetooth()
    if sys.platform.startswith("win"):
        from .windows import WindowsBluetooth

        return WindowsBluetooth()
    from .linux import LinuxBluetooth

    return LinuxBluetooth()
