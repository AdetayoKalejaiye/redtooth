"""Local state: remembered device associations, settings, metrics."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .models import Device

SCHEMA = """
CREATE TABLE IF NOT EXISTS remembered (
    device_id TEXT PRIMARY KEY,
    address TEXT,
    owner_phone TEXT,
    owner_name TEXT,
    kind TEXT,
    data TEXT NOT NULL,
    use_count INTEGER DEFAULT 0,
    last_used REAL
);
CREATE INDEX IF NOT EXISTS remembered_owner ON remembered(owner_phone);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    ms REAL NOT NULL,
    at REAL NOT NULL,
    tags TEXT
);
CREATE INDEX IF NOT EXISTS metrics_name ON metrics(name, at);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")

    # Remembered devices: "Daniel's headphones are the XM5 at 88:C9:…"
    def remember(self, device: Device, owner_phone: Optional[str], owner_name: Optional[str]) -> None:
        d = device.to_dict()
        d["owner_phone"], d["owner_name"] = owner_phone, owner_name
        with self._lock:
            self._db.execute(
                """INSERT INTO remembered(device_id,address,owner_phone,owner_name,kind,data,use_count,last_used)
                   VALUES(?,?,?,?,?,?,1,?)
                   ON CONFLICT(device_id) DO UPDATE SET owner_phone=excluded.owner_phone,
                     owner_name=excluded.owner_name, kind=excluded.kind, data=excluded.data,
                     address=excluded.address, use_count=use_count+1, last_used=excluded.last_used""",
                (device.id, device.address, owner_phone, owner_name, device.kind, json.dumps(d), time.time()),
            )
            self._db.commit()

    def remembered(self, owner_phone: Optional[str] = None) -> list[Device]:
        q, args = "SELECT data FROM remembered", ()
        if owner_phone is not None:
            q, args = q + " WHERE owner_phone=?", (owner_phone,)
        with self._lock:
            rows = self._db.execute(q + " ORDER BY last_used DESC", args).fetchall()
        out = []
        for r in rows:
            dev = Device.from_dict(json.loads(r["data"]))
            dev.sources = ["remembered"]
            out.append(dev)
        return out

    def forget(self, device_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM remembered WHERE device_id=?", (device_id,))
            self._db.commit()

    # Settings
    def get(self, key: str, default=None):
        with self._lock:
            row = self._db.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
        return json.loads(row["v"]) if row else default

    def set(self, key: str, value) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (key, json.dumps(value)))
            self._db.commit()

    # Metrics
    def add_metric(self, name: str, ms: float, tags: dict | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO metrics(name,ms,at,tags) VALUES(?,?,?,?)", (name, ms, time.time(), json.dumps(tags or {}))
            )
            self._db.commit()

    def metric_values(self, name: str, limit: int = 500) -> list[float]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ms FROM metrics WHERE name=? ORDER BY at DESC LIMIT ?", (name, limit)
            ).fetchall()
        return [r["ms"] for r in rows]

    def metric_names(self) -> list[str]:
        with self._lock:
            return [r["name"] for r in self._db.execute("SELECT DISTINCT name FROM metrics").fetchall()]
