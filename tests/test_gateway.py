"""Tests for eam.gateway.gateway.EdgeGateway: gateway enroll + sub-device batch uplink.

The gateway itself enrolls with the Manager as a normal device. Sub-devices
attached via ``attach()`` never talk to the Manager directly (private-IP
scenario) - the gateway collects their readings and uplinks them as ONE
JWS-signed telemetry message from its own device identity:
``{"gateway_id": ..., "batch": [...]}``.

The Manager's API/schema is unchanged, so it necessarily stores this
telemetry under the *sending* device_id (the gateway's), with the batch
content nested inside the verified JWS payload. These tests assert against
that actual storage shape (via ``app.state.store``), matching the brief's
"design the assertion around what Manager actually stores" guidance.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from eam.gateway.gateway import EdgeGateway
from eam.manager.app import create_app

BOOTSTRAP_TOKEN = "test-gateway-bootstrap-token"


def _make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_TOKEN", BOOTSTRAP_TOKEN)
    monkeypatch.setenv("AUTO_APPROVE", "true")
    return create_app(
        certs_dir=tmp_path / "manager_certs",
        store_db_path=tmp_path / "eam.db",
        audit_db_path=tmp_path / "eam_audit.db",
    )


def _gateway(app, tmp_path, gateway_id="gw-001") -> EdgeGateway:
    transport = httpx.ASGITransport(app=app)
    return EdgeGateway(
        gateway_id=gateway_id,
        site="seoul",
        group="gateway",
        manager_url="http://manager.local",
        certs_dir=tmp_path / "gateway_certs" / gateway_id,
        transport=transport,
    )


def test_gateway_enrolls_as_a_regular_device(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    gateway = _gateway(app, tmp_path)

    async def scenario():
        status = await gateway.enroll(BOOTSTRAP_TOKEN)
        assert status == "approved"
        await gateway.aclose()

    asyncio.run(scenario())

    device = app.state.store.get_device("gw-001")
    assert device is not None
    assert device.status == "approved"


def test_attach_registers_sub_devices_locally_without_touching_manager(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    gateway = _gateway(app, tmp_path)

    async def scenario():
        await gateway.enroll(BOOTSTRAP_TOKEN)
        gateway.attach("sub-dev-a")
        gateway.attach("sub-dev-b")
        await gateway.aclose()

    asyncio.run(scenario())

    assert set(gateway.sub_devices.keys()) == {"sub-dev-a", "sub-dev-b"}
    # Sub-devices must never appear as their own registered Manager device -
    # they are only reachable through the gateway (private-IP scenario).
    assert app.state.store.get_device("sub-dev-a") is None
    assert app.state.store.get_device("sub-dev-b") is None


def test_batch_uplink_is_stored_under_the_gateway_device_id_with_batch_verified(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path, monkeypatch)
    gateway = _gateway(app, tmp_path)

    async def scenario():
        await gateway.enroll(BOOTSTRAP_TOKEN)
        gateway.attach("sub-dev-a")
        gateway.attach("sub-dev-b")
        ok = await gateway.send_batch_telemetry()
        assert ok is True
        await gateway.aclose()

    asyncio.run(scenario())

    # Manager stores telemetry under the SENDING device_id: the gateway's,
    # never the sub-devices' (they were never registered with the Manager).
    gateway_rows = app.state.store.list_telemetry(device_id="gw-001")
    assert len(gateway_rows) == 1
    assert gateway_rows[0].verified is True

    payload = json.loads(gateway_rows[0].payload_json)
    assert payload["gateway_id"] == "gw-001"

    batch = payload["batch"]
    # 2 sub-devices x 2 default sensor types (temperature, humidity).
    assert len(batch) == 4
    batch_device_ids = {entry["device_id"] for entry in batch}
    assert batch_device_ids == {"sub-dev-a", "sub-dev-b"}
    sensor_types_seen = {entry["sensor_type"] for entry in batch}
    assert sensor_types_seen == {"temperature", "humidity"}

    # Sub-devices have no telemetry rows of their own on the Manager side.
    assert app.state.store.list_telemetry(device_id="sub-dev-a") == []
    assert app.state.store.list_telemetry(device_id="sub-dev-b") == []

    # The JWS verification audit trail is recorded against the gateway too.
    audit_rows = app.state.audit.query(device_id="gw-001", event="telemetry_accept")
    assert len(audit_rows) == 1


def test_batch_uplink_failure_buffers_then_flush_resends(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    gateway = _gateway(app, tmp_path)

    async def scenario():
        await gateway.enroll(BOOTSTRAP_TOKEN)
        gateway.attach("sub-dev-a")

        async def _boom(*args, **kwargs):
            raise httpx.ConnectError("simulated network outage")

        real_post = gateway.agent.client.post
        monkeypatch.setattr(gateway.agent.client, "post", _boom)

        ok = await gateway.send_batch_telemetry()
        assert ok is False
        assert app.state.store.list_telemetry(device_id="gw-001") == []

        monkeypatch.setattr(gateway.agent.client, "post", real_post)
        sent = await gateway.flush_buffer()
        assert sent == 1

        await gateway.aclose()

    asyncio.run(scenario())

    gateway_rows = app.state.store.list_telemetry(device_id="gw-001")
    assert len(gateway_rows) == 1
    payload = json.loads(gateway_rows[0].payload_json)
    assert payload["gateway_id"] == "gw-001"
    assert len(payload["batch"]) == 2  # 1 sub-device x 2 default sensor types
