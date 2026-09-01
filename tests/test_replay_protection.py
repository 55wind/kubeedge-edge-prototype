"""Tests for telemetry replay/freshness protection (jti + iat).

A valid JWS signature proves integrity and origin but not *freshness*: without
a nonce, an attacker who captures a legitimately signed telemetry message can
resubmit it verbatim. These tests pin the Manager's defense:

* every telemetry JWS must carry a ``jti`` nonce and an ``iat`` issue time,
* a duplicate ``jti`` for the same device is rejected (409, replay),
* an ``iat`` outside the freshness window is rejected (401, stale),
* and store-and-forward buffering keeps a stable ``jti`` so a lost-ack retry
  is de-duplicated rather than double-counted.
"""
from __future__ import annotations

import asyncio
import time
import uuid

from fastapi.testclient import TestClient

from eam.common import pki
from eam.common.jws import sign_payload
from eam.manager.app import create_app

BOOTSTRAP_TOKEN = "test-bootstrap-token"


def _make_app(tmp_path, monkeypatch, replay_window: int = 86400):
    monkeypatch.setenv("BOOTSTRAP_TOKEN", BOOTSTRAP_TOKEN)
    monkeypatch.setenv("EAM_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("EAM_ADMIN_PASSWORD", "admin-pass")
    monkeypatch.setenv("AUTO_APPROVE", "true")
    monkeypatch.setenv("TELEMETRY_REPLAY_WINDOW", str(replay_window))
    monkeypatch.delenv("INSECURE_MODE", raising=False)
    return create_app(
        certs_dir=tmp_path / "certs",
        store_db_path=tmp_path / "eam.db",
        audit_db_path=tmp_path / "eam_audit.db",
    )


def _enroll(client: TestClient, device_id: str):
    csr_pem, key_pem = pki.create_csr(device_id)
    resp = client.post("/api/v1/devices/register", json={
        "device_id": device_id, "site": "seoul", "group": "line-a",
        "csr_pem": csr_pem.decode(), "bootstrap_token": BOOTSTRAP_TOKEN})
    assert resp.status_code == 200, resp.text
    cert_pem = resp.json()["cert_pem"]
    token = client.post("/api/v1/auth/token", json={"cert_pem": cert_pem}).json()["access_token"]
    return key_pem, token


def test_replayed_telemetry_is_rejected(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    key_pem, token = _enroll(client, "dev-replay")

    payload = {"device_id": "dev-replay", "metric": "cpu", "value": 5,
               "jti": uuid.uuid4().hex, "iat": int(time.time())}
    jws = sign_payload(payload, key_pem)
    body = {"device_id": "dev-replay", "jws": jws}
    hdr = {"Authorization": f"Bearer {token}"}

    first = client.post("/api/v1/telemetry", json=body, headers=hdr)
    assert first.status_code == 200, first.text

    # Byte-identical resubmission (captured-and-replayed) must be refused.
    second = client.post("/api/v1/telemetry", json=body, headers=hdr)
    assert second.status_code == 409
    assert "replay" in second.json()["detail"].lower()

    # Only one row stored; the replay attempt is audited as a rejection.
    assert len(app.state.store.list_telemetry(device_id="dev-replay")) == 1
    rejects = app.state.audit.query(device_id="dev-replay", event="telemetry_reject")
    assert any("replay" in r.detail for r in rejects)


def test_stale_telemetry_is_rejected(tmp_path, monkeypatch):
    # Narrow window so an old iat is unambiguously stale.
    app = _make_app(tmp_path, monkeypatch, replay_window=10)
    client = TestClient(app)
    key_pem, token = _enroll(client, "dev-stale")

    payload = {"device_id": "dev-stale", "value": 1,
               "jti": uuid.uuid4().hex, "iat": int(time.time()) - 1000}
    jws = sign_payload(payload, key_pem)
    resp = client.post("/api/v1/telemetry", json={"device_id": "dev-stale", "jws": jws},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "stale" in resp.json()["detail"].lower()


def test_telemetry_without_replay_claims_is_rejected(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    key_pem, token = _enroll(client, "dev-nojti")

    # Correctly signed, but no jti/iat -> cannot be replay-protected -> reject.
    jws = sign_payload({"device_id": "dev-nojti", "value": 1}, key_pem)
    resp = client.post("/api/v1/telemetry", json={"device_id": "dev-nojti", "jws": jws},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "jti" in resp.json()["detail"].lower()


def test_distinct_jti_messages_are_all_accepted(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    key_pem, token = _enroll(client, "dev-multi")
    hdr = {"Authorization": f"Bearer {token}"}

    for i in range(3):
        payload = {"device_id": "dev-multi", "value": i,
                   "jti": uuid.uuid4().hex, "iat": int(time.time())}
        jws = sign_payload(payload, key_pem)
        resp = client.post("/api/v1/telemetry", json={"device_id": "dev-multi", "jws": jws},
                           headers=hdr)
        assert resp.status_code == 200, resp.text
    assert len(app.state.store.list_telemetry(device_id="dev-multi")) == 3


def test_agent_buffered_flush_keeps_stable_jti(tmp_path, monkeypatch):
    """A buffered message reuses its jti on flush; a manual re-flush de-dups.

    This proves the store-and-forward path is compatible with replay
    protection: the same nonce survives buffering, so the Manager accepts the
    delivery exactly once even if the flush is retried.
    """
    from eam.agent.agent import EdgeAgent

    app = _make_app(tmp_path, monkeypatch)
    transport = __import__("httpx").ASGITransport(app=app)

    async def scenario():
        agent = EdgeAgent("dev-buf", site="seoul", group="line-a",
                          manager_url="http://m", certs_dir=tmp_path / "agent",
                          transport=transport)
        await agent.enroll(BOOTSTRAP_TOKEN)
        # Force a transient failure so the message is buffered, then flush.
        orig_post = agent.client.post

        async def failing_post(url, *a, **k):
            if url.endswith("/telemetry"):
                raise __import__("httpx").ConnectError("simulated network drop")
            return await orig_post(url, *a, **k)

        agent.client.post = failing_post  # type: ignore[assignment]
        ok = await agent.send_telemetry({"metric": "cpu", "value": 9})
        assert ok is False  # buffered
        agent.client.post = orig_post  # network restored
        sent = await agent.flush_buffer()
        assert sent == 1
        await agent.aclose()

    asyncio.run(scenario())
    rows = app.state.store.list_telemetry(device_id="dev-buf")
    assert len(rows) == 1  # delivered exactly once
