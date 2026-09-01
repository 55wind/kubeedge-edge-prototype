"""SQLite-backed device registry and telemetry store for the Manager service.

Three tables live in one sqlite3 database file:

* ``devices``  - one row per registered device (pending/approved/revoked).
* ``telemetry``- one row per accepted telemetry submission.
* ``seen_jti`` - one row per accepted telemetry JWS nonce (``jti``), used to
  reject replays of an already-accepted message within the freshness window.

Thread-safe: a single sqlite3 connection is opened with
``check_same_thread=False`` and all access is serialized through an internal
``threading.Lock``, matching the pattern used by ``eam.common.audit.AuditLog``
so the store is safe to share across FastAPI's threadpool-executed requests.

Certificate serial numbers are stored as ``TEXT`` (not ``INTEGER``) because
``cryptography.x509.random_serial_number()`` can produce values that exceed
SQLite's signed 64-bit ``INTEGER`` range; Python's arbitrary-precision ``int``
round-trips cleanly through text.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Union

PathLike = Union[str, "os.PathLike[str]"]

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REVOKED = "revoked"


@dataclass(frozen=True)
class DeviceRecord:
    """One row of the ``devices`` table."""

    device_id: str
    site: str
    group_name: str
    status: str
    cert_serial: Optional[int]
    cert_pem: Optional[str]
    csr_pem: Optional[str]
    registered_at: str
    last_seen: Optional[str]


@dataclass(frozen=True)
class TelemetryRecord:
    """One row of the ``telemetry`` table."""

    id: int
    device_id: str
    ts: str
    payload_json: str
    verified: bool


class DeviceStore:
    """Device registry + telemetry log backed by sqlite3."""

    def __init__(self, db_path: PathLike):
        self.db_path = Path(db_path)
        if str(self.db_path.parent) not in ("", "."):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    site TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cert_serial TEXT,
                    cert_pem TEXT,
                    csr_pem TEXT,
                    registered_at TEXT NOT NULL,
                    last_seen TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    verified INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_jti (
                    device_id TEXT NOT NULL,
                    jti TEXT NOT NULL,
                    exp_epoch REAL NOT NULL,
                    PRIMARY KEY (device_id, jti)
                )
                """
            )
            self._conn.commit()

    # -- devices ----------------------------------------------------------

    def register_device(
        self,
        device_id: str,
        site: str,
        group_name: str,
        csr_pem: str,
        status: str = STATUS_PENDING,
        cert_serial: Optional[int] = None,
        cert_pem: Optional[str] = None,
    ) -> DeviceRecord:
        """Insert a new device row. Raises sqlite3.IntegrityError if device_id exists."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO devices "
                "(device_id, site, group_name, status, cert_serial, cert_pem, csr_pem, "
                " registered_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    device_id,
                    site,
                    group_name,
                    status,
                    str(cert_serial) if cert_serial is not None else None,
                    cert_pem,
                    csr_pem,
                    now,
                ),
            )
            self._conn.commit()
        record = self.get_device(device_id)
        assert record is not None
        return record

    def get_device(self, device_id: str) -> Optional[DeviceRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT device_id, site, group_name, status, cert_serial, cert_pem, "
                "csr_pem, registered_at, last_seen FROM devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def list_devices(self) -> List[DeviceRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT device_id, site, group_name, status, cert_serial, cert_pem, "
                "csr_pem, registered_at, last_seen FROM devices ORDER BY registered_at ASC"
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def approve_device(self, device_id: str, cert_serial: int, cert_pem: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE devices SET status = ?, cert_serial = ?, cert_pem = ? "
                "WHERE device_id = ?",
                (STATUS_APPROVED, str(cert_serial), cert_pem, device_id),
            )
            self._conn.commit()

    def revoke_device(self, device_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE devices SET status = ? WHERE device_id = ?",
                (STATUS_REVOKED, device_id),
            )
            self._conn.commit()

    def touch_last_seen(self, device_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE devices SET last_seen = ? WHERE device_id = ?", (now, device_id)
            )
            self._conn.commit()

    # -- telemetry ----------------------------------------------------------

    def insert_telemetry(self, device_id: str, payload_json: str, verified: bool) -> int:
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO telemetry (device_id, ts, payload_json, verified) "
                "VALUES (?, ?, ?, ?)",
                (device_id, ts, payload_json, 1 if verified else 0),
            )
            self._conn.commit()
            row_id = cur.lastrowid
        return int(row_id)

    def list_telemetry(
        self, device_id: Optional[str] = None, limit: int = 100
    ) -> List[TelemetryRecord]:
        with self._lock:
            if device_id is not None:
                rows = self._conn.execute(
                    "SELECT id, device_id, ts, payload_json, verified FROM telemetry "
                    "WHERE device_id = ? ORDER BY id DESC LIMIT ?",
                    (device_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, device_id, ts, payload_json, verified FROM telemetry "
                    "ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            TelemetryRecord(
                id=row[0], device_id=row[1], ts=row[2], payload_json=row[3],
                verified=bool(row[4]),
            )
            for row in rows
        ]

    # -- replay protection (telemetry JWS nonce) ---------------------------

    def record_jti(
        self, device_id: str, jti: str, exp_epoch: float, now_epoch: float
    ) -> bool:
        """Atomically record a telemetry ``jti`` for ``device_id``.

        Returns ``True`` if this ``(device_id, jti)`` pair is *fresh* (never
        seen before within the window) and was recorded, ``False`` if it is a
        replay of an already-accepted message. Expired rows (``exp_epoch`` in
        the past relative to ``now_epoch``) are pruned opportunistically so the
        table stays bounded to roughly one window's worth of traffic.
        """
        with self._lock:
            self._conn.execute("DELETE FROM seen_jti WHERE exp_epoch < ?", (now_epoch,))
            try:
                self._conn.execute(
                    "INSERT INTO seen_jti (device_id, jti, exp_epoch) VALUES (?, ?, ?)",
                    (device_id, jti, exp_epoch),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                # Primary-key clash -> this jti was already accepted -> replay.
                self._conn.commit()
                return False

    @staticmethod
    def _row_to_record(row) -> DeviceRecord:
        cert_serial = int(row[4]) if row[4] is not None else None
        return DeviceRecord(
            device_id=row[0],
            site=row[1],
            group_name=row[2],
            status=row[3],
            cert_serial=cert_serial,
            cert_pem=row[5],
            csr_pem=row[6],
            registered_at=row[7],
            last_seen=row[8],
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
