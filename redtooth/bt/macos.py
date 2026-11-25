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
import shutil
import subprocess
from typing import Optional

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
        out = [self._dev(d, "nearby") for d in await self._json("--inquiry", str(int(max(seconds, 2))), timeout=seconds + 10)]
        out = [d for d in out if not d.paired]
        for d in out:
            d.discoverable = True
        return out

    async def radio_state(self) -> str:
        if not shutil.which("blueutil"):
            return "unknown"
        code, out = await run("blueutil", "--power", timeout=4)
        return "on" if out.strip() == "1" else ("off" if out.strip() == "0" else "unknown")

    def setup_issue(self) -> Optional[str]:
        miss = [t for t in ("blueutil", "SwitchAudioSource") if not shutil.which(t)]
        if not miss:
            return None
        return f"Some tools aren't installed ({', '.join(miss)}: Bluetooth control and audio switching). Run: brew install blueutil switchaudio-osx"

    def push_file(self, address: str, path: str, progress, cancel) -> None:
        """macOS has no scriptable Bluetooth file sending; hand the file to Apple's Bluetooth File Exchange."""
        from ..transfer.obex import ObexError

        r = subprocess.run(["open", "-a", "Bluetooth File Exchange", path], capture_output=True, text=True)
        if r.returncode != 0:
            raise ObexError("this Mac has no Bluetooth File Exchange app; use AirDrop or Redtooth on the phone")
        raise ObexError("Finish in Bluetooth File Exchange: pick the phone there", "handoff")

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
        await run("blueutil", "--disconnect", dev.address or "", timeout=10)
        code, out = await run("blueutil", "--is-connected", dev.address or "", timeout=5)
        return out.strip() == "0"

    async def audio_output(self) -> AudioOutput:
        code, out = await run("SwitchAudioSource", "-c", "-t", "output", timeout=3)
        if code == 0 and out.strip():
            name = out.strip()
            bt = {d.name for d in await self.paired() if d.connected}
            return AudioOutput(name, None, "bluetooth" if name in bt else "builtin")
        code, out = await run("system_profiler", "SPAudioDataType", "-json", timeout=8)
        try:
            for item in json.loads(out)["SPAudioDataType"][0]["_items"]:
                if item.get("coreaudio_default_audio_output_device") == "spaudio_yes":
                    transport = item.get("coreaudio_device_transport", "")
                    return AudioOutput(item["_name"], None, "bluetooth" if "bluetooth" in transport else "builtin")
        except Exception:
            pass
        return AudioOutput("an output Redtooth can't read", None, "other", known=False)

    async def set_audio_output(self, dev: Device) -> bool:
        code, _ = await run("SwitchAudioSource", "-t", "output", "-s", dev.name, timeout=3)
        return code == 0
