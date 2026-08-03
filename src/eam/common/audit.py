"""SQLite-backed audit (accounting) log with optional JSONL mirror.

Thread-safe: a single sqlite3 connection is opened with
``check_same_thread=False`` and all access is serialized through an internal
``threading.Lock`` so concurrent callers (e.g. FastAPI request threads) do
not corrupt state.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Union

PathLike = Union[str, "os.PathLike[str]"]


@dataclass(frozen=True)
class AuditRecord:
    """One row of the audit log."""

    id: int
    ts: str
    event: str
    device_id: Optional[str]
    outcome: str
    detail: str


class AuditLog:
    """Accounting log backed by sqlite3, with an optional JSONL mirror file."""

    def __init__(self, db_path: PathLike, jsonl_path: Optional[PathLike] = None):
        self.db_path = Path(db_path)
        if str(self.db_path.parent) not in ("", "."):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    event TEXT NOT NULL,
                    device_id TEXT,
                    outcome TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._conn.commit()

        self.jsonl_path = Path(jsonl_path) if jsonl_path else None
        if self.jsonl_path and str(self.jsonl_path.parent) not in ("", "."):
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self, event: str, device_id: Optional[str], outcome: str, detail: str = ""
    ) -> int:
        """Insert one audit row (and JSONL mirror line, if configured).

        Returns:
            The autoincrement id of the inserted row.
        """
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO audit (ts, event, device_id, outcome, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, event, device_id, outcome, detail),
            )
            self._conn.commit()
            row_id = cur.lastrowid
            if self.jsonl_path:
                line = json.dumps(
                    {
                        "id": row_id,
                        "ts": ts,
                        "event": event,
                        "device_id": device_id,
                        "outcome": outcome,
                        "detail": detail,
                    },
                    ensure_ascii=False,
                )
                with open(self.jsonl_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        return int(row_id)

    def query(
        self,
        device_id: Optional[str] = None,
        event: Optional[str] = None,
        limit: int = 100,
    ) -> List[AuditRecord]:
        """Return matching audit rows, newest first."""
        clauses = []
        params: List[Any] = []
        if device_id is not None:
            clauses.append("device_id = ?")
            params.append(device_id)
        if event is not None:
            clauses.append("event = ?")
            params.append(event)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT id, ts, event, device_id, outcome, detail FROM audit "
            f"{where} ORDER BY id DESC LIMIT ?"
        )
        params.append(limit)

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [AuditRecord(*row) for row in rows]

    def close(self) -> None:
        """Close the underlying sqlite3 connection."""
        with self._lock:
            self._conn.close()
