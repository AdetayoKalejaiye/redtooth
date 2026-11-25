"""A simulated Bluetooth world, for tests and the demo.

Models radios in range, pairing mode, pairing/connection state and the
system audio output, with realistic delays.
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from typing import Optional

from ..models import Device
from .base import AudioOutput, BluetoothPlatform, enrich


@dataclass
class SimRadio:
    device: Device
    in_range: bool = True
    pairing_mode: bool = False  # discoverable to new hosts
    rssi: int = -55
    auto_output: bool = True  # OS switches output on connect (like macOS/Windows usually do)


@dataclass
class SimWorld:
    radios: dict[str, SimRadio] = field(default_factory=dict)
    paired: set[str] = field(default_factory=set)
    connected: set[str] = field(default_factory=set)
    output: AudioOutput = field(default_factory=lambda: AudioOutput("MacBook Pro Speakers"))
    delay: float = 0.15  # base latency for radio operations

    def add(self, dev: Device, **kw) -> SimRadio:
        r = SimRadio(enrich(dev), **kw)
        self.radios[dev.id] = r
        return r

    def set_pairing_mode(self, device_id: str, on: bool = True) -> None:
        self.radios[device_id].pairing_mode = on
        self.radios[device_id].in_range = True


class SimBluetooth(BluetoothPlatform):
    name = "simulated"

    def __init__(self, world: Optional[SimWorld] = None, wifi_direct: bool = True):
        self.world = world or SimWorld()
        self.wifi_direct = wifi_direct

    def _view(self, r: SimRadio, source: str) -> Device:
        d = copy.deepcopy(r.device)
        d.paired = d.id in self.world.paired
        d.connected = d.id in self.world.connected
        d.discoverable = r.pairing_mode and r.in_range
        d.rssi = r.rssi if r.in_range else None
        d.sources = [source]
        # A stranger's OS name is all we see over the air: no owner info.
        d.owner_phone = d.owner_name = None
        d.has_app, d.endpoints = False, []
        return d

    async def paired(self) -> list[Device]:
        await asyncio.sleep(self.world.delay / 3)
        return [self._view(r, "paired") for i, r in self.world.radios.items() if i in self.world.paired]

    async def scan(self, seconds: float = 5.0) -> list[Device]:
        await asyncio.sleep(min(seconds, self.world.delay * 4))
        return [self._view(r, "nearby") for r in self.world.radios.values() if r.in_range and (r.pairing_mode or r.device.kind in ("phone", "laptop"))]

    async def pair(self, dev: Device) -> bool:
        await asyncio.sleep(self.world.delay * 2)
        r = self.world.radios.get(dev.id)
        if not r or not r.in_range or not r.pairing_mode:
            return False
        self.world.paired.add(dev.id)
        r.pairing_mode = False
        return True

    async def connect(self, dev: Device) -> bool:
        await asyncio.sleep(self.world.delay * 2)
        r = self.world.radios.get(dev.id)
        if not r or not r.in_range or dev.id not in self.world.paired:
            return False
        self.world.connected.add(dev.id)
        if r.auto_output and r.device.kind in ("headphones", "speaker"):
            self.world.output = AudioOutput(r.device.name, r.device.address, "bluetooth")
        return True

    async def disconnect(self, dev: Device) -> bool:
        self.world.connected.discard(dev.id)
        if self.world.output.address == dev.address:
            self.world.output = AudioOutput("MacBook Pro Speakers")
        return True

    async def audio_output(self) -> AudioOutput:
        return self.world.output

    async def set_audio_output(self, dev: Device) -> bool:
        if dev.id in self.world.connected:
            self.world.output = AudioOutput(dev.name, dev.address, "bluetooth")
            return True
        return False
