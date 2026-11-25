"""Find the fastest reachable path to a device, ahead of time.

Probing starts as soon as the recipient is known (while the user is still
picking a file, or while the recipient decides whether to accept). Each
candidate endpoint gets a PING; the reply must carry the expected device id,
so a stale address that now belongs to some other machine is never used.
Wi-Fi Direct is preferred when both devices support it, then the LAN.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from ..bt.base import BluetoothPlatform
from ..metrics import Metrics
from ..models import Device, Endpoint
from ..util import now_ms
from .protocol import read_frame, send_frame

PREFERENCE = {"wifi_direct": 0, "lan": 1, "loopback": 1}
TTL = 45.0


@dataclass
class Route:
    device_id: str
    endpoint: Endpoint
    rtt_ms: float
    checked_at: float

    def label(self) -> str:
        return {"wifi_direct": "Wi-Fi Direct", "lan": "Local network", "loopback": "This computer"}.get(self.endpoint.kind, self.endpoint.kind)

    def to_dict(self) -> dict:
        return {"kind": self.endpoint.kind, "label": self.label(), "host": self.endpoint.host, "port": self.endpoint.port, "rtt_ms": round(self.rtt_ms, 1)}


async def ping(ep: Endpoint, device_id: str, timeout: float = 1.5) -> Optional[float]:
    t0 = now_ms()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ep.host, ep.port), timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        await send_frame(writer, {"type": "PING"})
        hdr, _ = await read_frame(reader, timeout=timeout)
        if hdr.get("type") == "PONG" and hdr.get("device_id") == device_id:
            return now_ms() - t0
        return None
    except Exception:
        return None
    finally:
        writer.close()


class Router:
    def __init__(self, bt: Optional[BluetoothPlatform], metrics: Optional[Metrics] = None):
        self.bt = bt
        self.metrics = metrics
        self.cache: dict[str, Route] = {}
        self.inflight: dict[str, asyncio.Task] = {}

    def prefetch(self, device: Device) -> asyncio.Task:
        """Start checking routes now; `get` will await the same task."""
        cached = self.cache.get(device.id)
        if cached and time.time() - cached.checked_at < TTL:
            fut = asyncio.get_running_loop().create_future()
            fut.set_result(cached)
            return fut  # type: ignore[return-value]
        task = self.inflight.get(device.id)
        if not task or task.done():
            task = self.inflight[device.id] = asyncio.create_task(self._probe(device))
        return task

    async def get(self, device: Device, fresh: bool = False) -> Optional[Route]:
        if fresh:
            self.cache.pop(device.id, None)
        return await self.prefetch(device)

    async def _probe(self, device: Device) -> Optional[Route]:
        t0 = now_ms()
        endpoints = list(device.endpoints)
        if device.wifi_direct and self.bt and self.bt.wifi_direct and not any(e.kind == "wifi_direct" for e in endpoints):
            try:
                host = await asyncio.wait_for(self.bt.wifi_direct_connect({"device_id": device.id}), 8)
                if host and endpoints:
                    endpoints.insert(0, Endpoint("wifi_direct", host, endpoints[0].port))
            except Exception:
                pass
        rtts = await asyncio.gather(*(ping(e, device.id) for e in endpoints))
        ok = [(PREFERENCE.get(e.kind, 5), rtt, e) for e, rtt in zip(endpoints, rtts) if rtt is not None]
        if self.metrics:
            self.metrics.record("route_probe_ms", now_ms() - t0, reachable=len(ok))
        if not ok:
            return None
        ok.sort(key=lambda x: (x[0], x[1]))
        route = Route(device.id, ok[0][2], ok[0][1], time.time())
        self.cache[device.id] = route
        return route
