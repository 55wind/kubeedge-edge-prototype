"""Tests for eam.simulator.{vdevice,fleet}: virtual-device lifecycle + fleet stats.

Runs entirely in-process against a Manager app via ``httpx.ASGITransport``
(no real sockets). Async scenarios are driven with ``asyncio.run(...)`` since
pytest-asyncio/anyio are not on this project's allowed-package list.
"""

from __future__ import annotations

import asyncio

import httpx

from eam.manager.app import create_app
from eam.simulator.fleet import FleetResult, PhaseStats, run_fleet
from eam.simulator.vdevice import VirtualDevice, VirtualDeviceResult

BOOTSTRAP_TOKEN = "test-sim-bootstrap-token"


def _make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_TOKEN", BOOTSTRAP_TOKEN)
    monkeypatch.setenv("AUTO_APPROVE", "true")
    return create_app(
        certs_dir=tmp_path / "manager_certs",
        store_db_path=tmp_path / "eam.db",
        audit_db_path=tmp_path / "eam_audit.db",
    )


# ---------------------------------------------------------------------------
# VirtualDevice
# ---------------------------------------------------------------------------


def test_virtual_device_lifecycle_records_per_phase_latency(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    vdevice = VirtualDevice(
        device_id="vdev-001",
        site="seoul",
        group="line-a",
        manager_url="http://manager.local",
        certs_dir=tmp_path / "vdev_certs" / "vdev-001",
        transport=transport,
    )

    result = asyncio.run(vdevice.run_lifecycle(BOOTSTRAP_TOKEN, telemetry_count=3))

    assert isinstance(result, VirtualDeviceResult)
    assert result.success is True
    assert result.error is None
    assert result.enroll_ms is not None and result.enroll_ms >= 0
    assert result.auth_ms is not None and result.auth_ms >= 0
    assert len(result.telemetry_ms) == 3
    assert all(ms >= 0 for ms in result.telemetry_ms)

    rows = app.state.store.list_telemetry(device_id="vdev-001")
    assert len(rows) == 3


def test_virtual_device_lifecycle_reports_failure_without_raising(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    vdevice = VirtualDevice(
        device_id="vdev-bad-token",
        site="seoul",
        group="line-a",
        manager_url="http://manager.local",
        certs_dir=tmp_path / "vdev_certs" / "vdev-bad-token",
        transport=transport,
    )

    result = asyncio.run(vdevice.run_lifecycle("wrong-bootstrap-token", telemetry_count=1))

    assert result.success is False
    assert result.error is not None
    assert result.enroll_ms is None  # failed during enroll, before it completed


# ---------------------------------------------------------------------------
# run_fleet
# ---------------------------------------------------------------------------


def test_run_fleet_of_ten_all_succeed_with_latency_stats(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)

    result = asyncio.run(
        run_fleet(
            n=10,
            manager_url="http://manager.local",
            concurrency=4,
            telemetry_per_device=2,
            transport=transport,
            bootstrap_token=BOOTSTRAP_TOKEN,
            certs_dir_root=tmp_path / "fleet_certs",
        )
    )

    assert isinstance(result, FleetResult)
    assert result.n == 10
    assert result.success_count == 10
    assert result.fail_count == 0
    assert result.errors == []
    assert result.wall_time_s >= 0

    for phase in ("enroll", "auth", "telemetry"):
        stats = result.phase_stats[phase]
        assert isinstance(stats, PhaseStats)
        assert stats.count > 0
        assert stats.p50 >= 0
        assert stats.p95 >= stats.p50
        assert stats.p99 >= stats.p95
        assert stats.max >= stats.mean >= 0

    # telemetry phase saw 10 devices x 2 sends each.
    assert result.phase_stats["telemetry"].count == 20
    assert result.phase_stats["enroll"].count == 10
    assert result.phase_stats["auth"].count == 10

    # All 10 devices actually registered and sent telemetry on the Manager side.
    devices = app.state.store.list_devices()
    assert len(devices) == 10
    assert all(d.status == "approved" for d in devices)


def test_fleet_result_to_dict_is_json_serializable(tmp_path, monkeypatch):
    import json

    app = _make_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)

    result = asyncio.run(
        run_fleet(
            n=3,
            manager_url="http://manager.local",
            concurrency=3,
            telemetry_per_device=1,
            transport=transport,
            bootstrap_token=BOOTSTRAP_TOKEN,
            certs_dir_root=tmp_path / "fleet_certs_small",
        )
    )

    as_json = json.dumps(result.to_dict(), ensure_ascii=False)
    reloaded = json.loads(as_json)
    assert reloaded["n"] == 3
    assert reloaded["success_count"] == 3
    assert "p95" in reloaded["phase_stats"]["telemetry"]
