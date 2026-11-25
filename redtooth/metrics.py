"""Latency metrics. Three are measured separately, as the spec asks:

  find_match_ms        request received -> ranked file/device candidates ready
  accept_to_first_byte_ms  receiver accepts -> first payload byte arrives (measured on the receiver)
  first_byte_to_saved_ms   first byte -> file verified and saved (measured on the receiver)

Plus supporting ones: resolve_person_ms, route_probe_ms, connect_ms, transfer_total_ms.
"""
from __future__ import annotations

import statistics
from typing import Optional

from .store import Store

LABELS = {
    "find_match_ms": "Time to find a match",
    "accept_to_first_byte_ms": "Acceptance → first byte",
    "first_byte_to_saved_ms": "First byte → saved",
    "find_device_ms": "Time to find the device",
    "resolve_person_ms": "Resolve recipient",
    "route_probe_ms": "Route check",
    "connect_ms": "Connect audio device",
    "transfer_total_ms": "Offer → delivered",
}


class Metrics:
    def __init__(self, store: Store):
        self.store = store
        self.last: dict[str, float] = {}

    def record(self, name: str, ms: float, **tags) -> None:
        self.last[name] = ms
        self.store.add_metric(name, ms, tags)

    def summary(self) -> list[dict]:
        out = []
        names = sorted(set(self.store.metric_names()) | set(LABELS), key=lambda n: list(LABELS).index(n) if n in LABELS else 99)
        for name in names:
            vals = self.store.metric_values(name)
            if not vals:
                continue
            vals_sorted = sorted(vals)
            p95: Optional[float] = vals_sorted[min(len(vals_sorted) - 1, int(0.95 * len(vals_sorted)))]
            out.append(
                {
                    "name": name,
                    "label": LABELS.get(name, name),
                    "count": len(vals),
                    "last": round(vals[0], 1),
                    "p50": round(statistics.median(vals), 1),
                    "p95": round(p95, 1),
                }
            )
        return out
