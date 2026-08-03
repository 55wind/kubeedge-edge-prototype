"""Tests for eam.common.audit: sqlite audit log, JSONL mirror, thread-safety."""

from __future__ import annotations

import json
import threading

from eam.common.audit import AuditLog, AuditRecord


def test_record_and_query_basic(tmp_path):
    log = AuditLog(tmp_path / "audit.db")

    id1 = log.record("register", "dev-1", "success", "site=A")
    id2 = log.record("auth_success", "dev-1", "success")
    id3 = log.record("register", "dev-2", "pending", "site=B")

    assert id1 < id2 < id3

    all_rows = log.query(limit=10)
    assert len(all_rows) == 3
    assert all(isinstance(r, AuditRecord) for r in all_rows)
    # Newest first.
    assert [r.event for r in all_rows] == ["register", "auth_success", "register"]

    dev1_rows = log.query(device_id="dev-1")
    assert len(dev1_rows) == 2
    assert all(r.device_id == "dev-1" for r in dev1_rows)

    register_rows = log.query(event="register")
    assert len(register_rows) == 2
    assert all(r.event == "register" for r in register_rows)

    dev2_register = log.query(device_id="dev-2", event="register")
    assert len(dev2_register) == 1
    assert dev2_register[0].detail == "site=B"

    log.close()


def test_query_limit_and_default_detail(tmp_path):
    log = AuditLog(tmp_path / "audit.db")
    for i in range(5):
        log.record("http", None, "success")

    limited = log.query(limit=2)
    assert len(limited) == 2

    row = log.query(limit=1)[0]
    assert row.device_id is None
    assert row.detail == ""
    log.close()


def test_jsonl_mirror_written(tmp_path):
    db_path = tmp_path / "audit.db"
    jsonl_path = tmp_path / "audit.jsonl"
    log = AuditLog(db_path, jsonl_path=jsonl_path)

    log.record("register", "dev-1", "success", "site=A")
    log.record("telemetry_reject", "dev-2", "fail", "bad signature")

    assert jsonl_path.exists()
    lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["event"] == "register"
    assert first["device_id"] == "dev-1"
    assert first["outcome"] == "success"
    assert first["detail"] == "site=A"

    second = json.loads(lines[1])
    assert second["event"] == "telemetry_reject"
    assert second["outcome"] == "fail"

    log.close()


def test_no_jsonl_mirror_by_default(tmp_path):
    log = AuditLog(tmp_path / "audit.db")
    log.record("register", "dev-1", "success")

    assert list(tmp_path.glob("*.jsonl")) == []
    log.close()


def test_thread_safe_concurrent_writes(tmp_path):
    log = AuditLog(tmp_path / "audit.db")
    n_threads = 8
    writes_per_thread = 25

    def worker(idx: int) -> None:
        for i in range(writes_per_thread):
            log.record("http", f"dev-{idx}", "success", detail=str(i))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = log.query(limit=n_threads * writes_per_thread + 10)
    assert len(rows) == n_threads * writes_per_thread

    ids = [r.id for r in rows]
    assert len(ids) == len(set(ids))  # no duplicate/corrupted ids

    log.close()
