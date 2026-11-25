"""macOS backend: `blueutil` for discovery/pairing/connection and
`SwitchAudioSource` for the output device (both via Homebrew:
`brew install blueutil switchaudio-osx`).

macOS exposes no public Wi-Fi Direct API; Apple devices reach each other
over the LAN here, and a native client can use Network.framework's
peer-to-peer (AWDL) option instead.

Not exercised on hardware in this build.
"""
from __future__ import annotations

import json

from ..models import Device
from .base import AudioOutput, BluetoothPlatform, enrich, norm_addr, run


class MacBluetooth(BluetoothPlatform):
    name = "macos"
    wifi_direct = False

    @staticmethod
    def _dev(d: dict, source: str) -> Device:
        addr = norm_addr(d.get("address", ""))
        rssi = d.get("RSSI") or d.get("rawRSSI")
        return enrich(
            Device(
                id=f"bt:{addr}",
                name=d.get("name") or addr,
                address=addr,
                paired=bool(d.get("paired")),
                connected=bool(d.get("connected")),
                rssi=int(rssi) if isinstance(rssi, (int, float)) and rssi < 0 else None,
                sources=[source],
            )
        )

    async def _json(self, *args: str, timeout: float = 15) -> list[dict]:
        code, out = await run("blueutil", "--format", "json", *args, timeout=timeout)
        if code != 0:
            return []
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return []

    async def paired(self) -> list[Device]:
        return [self._dev(d, "paired") for d in await self._json("--paired")]

    async def scan(self, seconds: float = 5.0) -> list[Device]:
        return [self._dev(d, "nearby") for d in await self._json("--inquiry", str(int(max(seconds, 2))), timeout=seconds + 10)]

    async def pair(self, dev: Device) -> bool:
        code, _ = await run("blueutil", "--pair", dev.address or "", timeout=30)
        return code == 0

    async def connect(self, dev: Device) -> bool:
        code, _ = await run("blueutil", "--connect", dev.address or "", timeout=20)
        if code != 0:
            return False
        code, out = await run("blueutil", "--is-connected", dev.address or "", timeout=5)
        return out.strip() == "1"

    async def disconnect(self, dev: Device) -> bool:
        code, _ = await run("blueutil", "--disconnect", dev.address or "", timeout=10)
        return code == 0

    async def audio_output(self) -> AudioOutput:
        code, out = await run("SwitchAudioSource", "-c", "-t", "output", timeout=3)
        if code == 0:
            return AudioOutput(out.strip(), None, "other")
        code, out = await run("system_profiler", "SPAudioDataType", "-json", timeout=8)
        try:
            for item in json.loads(out)["SPAudioDataType"][0]["_items"]:
                if item.get("coreaudio_default_audio_output_device") == "spaudio_yes":
                    return AudioOutput(item["_name"], None, item.get("coreaudio_device_transport", "other"))
        except Exception:
            pass
        return AudioOutput("Unknown")

    async def set_audio_output(self, dev: Device) -> bool:
        code, _ = await run("SwitchAudioSource", "-t", "output", "-s", dev.name, timeout=3)
        return code == 0
