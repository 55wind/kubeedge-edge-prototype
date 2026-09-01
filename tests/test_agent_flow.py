"""Tests for eam.agent.agent.EdgeAgent: enroll->token->telemetry, buffer/flush.

No pytest-asyncio/anyio is available (not on the allowed external-package
list for this project), so each async scenario is driven with
``asyncio.run(...)`` from an ordinary sync test function. ``asyncio.run``
creates a fresh event loop and tears it down when it returns, so there is no
event-loop leakage between tests.

The Manager is exercised entirely in-process via ``httpx.ASGITransport`` -
no real sockets/servers involved (mirrors ``tests/test_manager_api.py``'s
in-process testing guidance, and is exactly the mechanism Task 4's
benchmark/simulator reuse).
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from eam.agent.agent import AgentError, EdgeAgent, PermanentTelemetryError
from eam.manager.app import create_app

BOOTSTRAP_TOKEN = "test-agent-bootstrap-token"


def _make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_TOKEN", BOOTSTRAP_TOKEN)
    monkeypatch.setenv("AUTO_APPROVE", "true")
    return create_app(
        certs_dir=tmp_path / "manager_certs",
        store_db_path=tmp_path / "eam.db",
        audit_db_path=tmp_path / "eam_audit.db",
    )


def _agent(app, tmp_path, device_id="agent-dev-001") -> EdgeAgent:
    transport = httpx.ASGITransport(app=app)
    return EdgeAgent(
        device_id=device_id,
        site="seoul",
        group="line-a",
        manager_url="http://manager.local",
        certs_dir=tmp_path / "agent_certs" / device_id,
        transport=transport,
    )


# ---------------------------------------------------------------------------
# enroll
# ---------------------------------------------------------------------------


def test_enroll_registers_and_saves_cert_and_key(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    agent = _agent(app, tmp_path)

    async def scenario():
        status = await agent.enroll(BOOTSTRAP_TOKEN)
        assert status == "approved"
        await agent.aclose()

    asyncio.run(scenario())

    cert_path = tmp_path / "agent_certs" / "agent-dev-001" / "agent-dev-001.cert.pem"
    key_path = tmp_path / "agent_certs" / "agent-dev-001" / "agent-dev-001.key.pem"
    assert cert_path.exists()
    assert key_path.exists()
    assert cert_path.read_bytes()  # non-empty PEM
    assert key_path.read_bytes()

    # Registered on the Manager side under the same device_id.
    device = app.state.store.get_device("agent-dev-001")
    assert device is not None
    assert device.status == "approved"


def test_enroll_rejects_bad_bootstrap_token(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    agent = _agent(app, tmp_path)

    async def scenario():
        with pytest.raises(httpx.HTTPStatusError):
            await agent.enroll("wrong-token")
        await agent.aclose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# get_token
# ---------------------------------------------------------------------------


def test_get_token_before_enroll_raises_agent_error(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    agent = _agent(app, tmp_path)

    async def scenario():
        with pytest.raises(AgentError):
            await agent.get_token()
        await agent.aclose()

    asyncio.run(scenario())


def test_get_token_is_cached_until_near_expiry(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    agent = _agent(app, tmp_path)

    async def scenario():
        await agent.enroll(BOOTSTRAP_TOKEN)

        call_count = {"token_requests": 0}
        original_post = agent.client.post

        async def counting_post(url, *args, **kwargs):
            if url == "/api/v1/auth/token":
                call_count["token_requests"] += 1
            return await original_post(url, *args, **kwargs)

        monkeypatch.setattr(agent.client, "post", counting_post)

        token1 = await agent.get_token()
        token2 = await agent.get_token()
        assert token1 == token2
        assert call_count["token_requests"] == 1, "cached token must not re-request"

        # Force the cached token to look like it is about to expire
        # (< 60s margin) -> get_token() must refresh it.
        agent._token_exp = time.time() + 30
        token3 = await agent.get_token()
        assert token3 is not None
        assert call_count["token_requests"] == 2, "near-expiry token must be refreshed"

        await agent.aclose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# sensors
# ---------------------------------------------------------------------------


def test_read_sensor_temperature_and_humidity_shapes(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    agent = _agent(app, tmp_path)

    temp = agent.read_sensor("temperature")
    assert temp["sensor_type"] == "temperature"
    assert temp["unit"] == "celsius"
    assert 15.0 <= temp["value"] <= 35.0

    humidity = agent.read_sensor("humidity")
    assert humidity["sensor_type"] == "humidity"
    assert humidity["unit"] == "percent"
    assert 20.0 <= humidity["value"] <= 90.0

    with pytest.raises(ValueError):
        agent.read_sensor("pressure")

    asyncio.run(agent.aclose())


# ---------------------------------------------------------------------------
# send_telemetry roundtrip
# ---------------------------------------------------------------------------


def test_send_telemetry_roundtrip_is_verified_and_stored_by_manager(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    agent = _agent(app, tmp_path)

    async def scenario():
        await agent.enroll(BOOTSTRAP_TOKEN)
        ok = await agent.send_telemetry({"metric": "cpu", "value": 42})
        assert ok is True
        await agent.aclose()

    asyncio.run(scenario())

    rows = app.state.store.list_telemetry(device_id="agent-dev-001")
    assert len(rows) == 1
    assert rows[0].verified is True
    payload = json.loads(rows[0].payload_json)
    assert payload["metric"] == "cpu"
    assert payload["value"] == 42

    # Also visible via the JWS-verification audit trail.
    audit_rows = app.state.audit.query(device_id="agent-dev-001", event="telemetry_accept")
    assert len(audit_rows) == 1


# ---------------------------------------------------------------------------
# failure -> buffer -> flush resend
# ---------------------------------------------------------------------------


def test_send_telemetry_failure_buffers_then_flush_resends(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    agent = _agent(app, tmp_path)

    async def scenario():
        await agent.enroll(BOOTSTRAP_TOKEN)

        real_post = agent.client.post

        async def _boom(*args, **kwargs):
            raise httpx.ConnectError("simulated network outage")

        # Simulate a transport-level failure (network outage) for telemetry.
        monkeypatch.setattr(agent.client, "post", _boom)
        ok = await agent.send_telemetry({"metric": "cpu", "value": 1})
        assert ok is False

        assert agent.buffer_path.exists()
        buffered_lines = [
            line
            for line in agent.buffer_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(buffered_lines) == 1
        # jti/iat are stamped before buffering so the flushed retry reuses the
        # same nonce (the Manager de-dups a lost-ack retry rather than
        # double-counting it).
        buffered = json.loads(buffered_lines[0])
        assert buffered["metric"] == "cpu" and buffered["value"] == 1
        assert isinstance(buffered["jti"], str) and isinstance(buffered["iat"], int)

        # Manager must not have received anything yet.
        assert app.state.store.list_telemetry(device_id="agent-dev-001") == []

        # Network recovers -> flush_buffer() re-sends the buffered payload.
        monkeypatch.setattr(agent.client, "post", real_post)
        sent = await agent.flush_buffer()
        assert sent == 1
        assert not agent.buffer_path.exists()

        await agent.aclose()

    asyncio.run(scenario())

    rows = app.state.store.list_telemetry(device_id="agent-dev-001")
    assert len(rows) == 1
    payload = json.loads(rows[0].payload_json)
    # The agent injects jti/iat replay-protection claims alongside the app data.
    assert payload["metric"] == "cpu" and payload["value"] == 1
    assert isinstance(payload["jti"], str) and isinstance(payload["iat"], int)


def test_flush_buffer_with_nothing_buffered_is_a_noop(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    agent = _agent(app, tmp_path)

    async def scenario():
        await agent.enroll(BOOTSTRAP_TOKEN)
        sent = await agent.flush_buffer()
        assert sent == 0
        await agent.aclose()

    asyncio.run(scenario())


def test_flush_buffer_keeps_only_still_failing_entries(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    agent = _agent(app, tmp_path)

    async def scenario():
        await agent.enroll(BOOTSTRAP_TOKEN)

        async def _boom(*args, **kwargs):
            raise httpx.ConnectError("simulated network outage")

        monkeypatch.setattr(agent.client, "post", _boom)
        await agent.send_telemetry({"metric": "cpu", "value": 1})
        await agent.send_telemetry({"metric": "cpu", "value": 2})

        buffered_lines = [
            line
            for line in agent.buffer_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(buffered_lines) == 2

        # Still failing on flush -> buffer must be preserved untouched.
        sent = await agent.flush_buffer()
        assert sent == 0
        assert agent.buffer_path.exists()
        still_buffered = [
            line
            for line in agent.buffer_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(still_buffered) == 2

        await agent.aclose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# permanent (4xx) vs. transient failure classification
# ---------------------------------------------------------------------------


def test_send_telemetry_on_revoked_device_is_permanent_and_not_buffered(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    agent = _agent(app, tmp_path)

    async def scenario():
        await agent.enroll(BOOTSTRAP_TOKEN)

        # Revoke directly on the store (equivalent to an admin calling
        # POST /devices/{id}/revoke) so this test doesn't need an admin token.
        app.state.store.revoke_device("agent-dev-001")

        with pytest.raises(PermanentTelemetryError):
            await agent.send_telemetry({"metric": "cpu", "value": 999})

        # Permanent (4xx) failures must NOT be buffered - retrying a revoked
        # device's telemetry will fail identically forever.
        assert not agent.buffer_path.exists()
        assert app.state.store.list_telemetry(device_id="agent-dev-001") == []

        await agent.aclose()

    asyncio.run(scenario())


def test_flush_buffer_drops_entries_that_become_permanently_rejected(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    agent = _agent(app, tmp_path)

    async def scenario():
        await agent.enroll(BOOTSTRAP_TOKEN)

        real_post = agent.client.post

        async def _boom(*args, **kwargs):
            raise httpx.ConnectError("simulated network outage")

        # First: a transient network failure buffers the payload normally.
        monkeypatch.setattr(agent.client, "post", _boom)
        ok = await agent.send_telemetry({"metric": "cpu", "value": 1})
        assert ok is False
        monkeypatch.setattr(agent.client, "post", real_post)
        assert agent.buffer_path.exists()

        # The device gets revoked while the payload is sitting in the buffer -
        # the buffered entry is now permanently unrecoverable.
        app.state.store.revoke_device("agent-dev-001")

        sent = await agent.flush_buffer()
        assert sent == 0
        # Dropped (dead-lettered), not kept forever: buffer file is now empty/gone.
        assert not agent.buffer_path.exists()
        assert app.state.store.list_telemetry(device_id="agent-dev-001") == []

        await agent.aclose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# credential resume from certs_dir (no re-enroll needed after "restart")
# ---------------------------------------------------------------------------


def test_new_agent_instance_resumes_credentials_from_certs_dir_without_enroll(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path, monkeypatch)
    certs_dir = tmp_path / "agent_certs" / "resume-dev"
    transport = httpx.ASGITransport(app=app)

    def _make(device_id="resume-dev"):
        return EdgeAgent(
            device_id=device_id,
            site="seoul",
            group="line-a",
            manager_url="http://manager.local",
            certs_dir=certs_dir,
            transport=transport,
        )

    async def scenario():
        agent1 = _make()
        await agent1.enroll(BOOTSTRAP_TOKEN)
        await agent1.aclose()

        # Simulate a process restart: a brand new EdgeAgent instance pointed
        # at the same certs_dir, with enroll() never called on it.
        agent2 = _make()
        ok = await agent2.send_telemetry({"metric": "cpu", "value": 7})
        assert ok is True
        await agent2.aclose()

    asyncio.run(scenario())

    rows = app.state.store.list_telemetry(device_id="resume-dev")
    assert len(rows) == 1
    payload = json.loads(rows[0].payload_json)
    assert payload["metric"] == "cpu" and payload["value"] == 7
    assert isinstance(payload["jti"], str) and isinstance(payload["iat"], int)


def test_enroll_short_circuits_when_credentials_already_loaded_from_disk(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path, monkeypatch)
    certs_dir = tmp_path / "agent_certs" / "resume-dev-2"
    transport = httpx.ASGITransport(app=app)

    def _make():
        return EdgeAgent(
            device_id="resume-dev-2",
            site="seoul",
            group="line-a",
            manager_url="http://manager.local",
            certs_dir=certs_dir,
            transport=transport,
        )

    async def scenario():
        agent1 = _make()
        await agent1.enroll(BOOTSTRAP_TOKEN)
        await agent1.aclose()

        # A second instance re-calling enroll() must short-circuit (return
        # the already-approved status) rather than re-register and hit the
        # Manager's 409 "device already registered".
        agent2 = _make()
        status = await agent2.enroll(BOOTSTRAP_TOKEN)
        assert status == "approved"
        await agent2.aclose()

    asyncio.run(scenario())
