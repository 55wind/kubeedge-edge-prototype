"""Tests for eam.manager.app: Manager service HTTP API (registration, AAA, telemetry).

Uses fastapi.testclient.TestClient (in-process ASGI, no network/sockets) per
the task brief's in-process testing guidance.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from eam.common import pki
from eam.common.jws import sign_payload
from eam.manager.app import create_app

ADMIN_USER = "test-admin"
ADMIN_PASS = "test-admin-pass"
OPERATOR_USER = "test-operator"
OPERATOR_PASS = "test-operator-pass"
BOOTSTRAP_TOKEN = "test-bootstrap-token"


def _set_common_env(monkeypatch, auto_approve: bool = False, insecure_mode: bool = False):
    monkeypatch.setenv("BOOTSTRAP_TOKEN", BOOTSTRAP_TOKEN)
    monkeypatch.setenv("EAM_ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("EAM_ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setenv("EAM_OPERATOR_USERNAME", OPERATOR_USER)
    monkeypatch.setenv("EAM_OPERATOR_PASSWORD", OPERATOR_PASS)
    monkeypatch.setenv("AUTO_APPROVE", "true" if auto_approve else "false")
    monkeypatch.setenv("INSECURE_MODE", "true" if insecure_mode else "false")


def _make_app(tmp_path, monkeypatch, auto_approve: bool = False, insecure_mode: bool = False):
    _set_common_env(monkeypatch, auto_approve=auto_approve, insecure_mode=insecure_mode)
    app = create_app(
        certs_dir=tmp_path / "certs",
        store_db_path=tmp_path / "eam.db",
        audit_db_path=tmp_path / "eam_audit.db",
    )
    return app


def _register(client: TestClient, device_id: str, site="seoul", group="line-a"):
    csr_pem, dev_key_pem = pki.create_csr(device_id)
    resp = client.post(
        "/api/v1/devices/register",
        json={
            "device_id": device_id,
            "site": site,
            "group": group,
            "csr_pem": csr_pem.decode(),
            "bootstrap_token": BOOTSTRAP_TOKEN,
        },
    )
    return resp, dev_key_pem


def _admin_token(client: TestClient) -> str:
    resp = client.post(
        "/api/v1/auth/operator", json={"username": ADMIN_USER, "password": ADMIN_PASS}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _operator_token(client: TestClient) -> str:
    resp = client.post(
        "/api/v1/auth/operator", json={"username": OPERATOR_USER, "password": OPERATOR_PASS}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Full happy-path round trip
# ---------------------------------------------------------------------------


def test_full_roundtrip_register_cert_token_telemetry(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, auto_approve=True)
    client = TestClient(app)

    resp, dev_key_pem = _register(client, "dev-001")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["cert_pem"]
    cert_pem = body["cert_pem"]

    token_resp = client.post("/api/v1/auth/token", json={"cert_pem": cert_pem})
    assert token_resp.status_code == 200, token_resp.text
    token_body = token_resp.json()
    assert token_body["role"] == "device"
    assert token_body["token_type"] == "bearer"
    assert token_body["expires_in"] == 900
    device_token = token_body["access_token"]

    payload = {"device_id": "dev-001", "metric": "cpu", "value": 42}
    jws = sign_payload(payload, dev_key_pem)

    tele_resp = client.post(
        "/api/v1/telemetry",
        json={"device_id": "dev-001", "jws": jws},
        headers=_auth_header(device_token),
    )
    assert tele_resp.status_code == 200, tele_resp.text
    assert tele_resp.json()["status"] == "accepted"

    # Audit trail should show the whole story, verifiable via the admin-only endpoint.
    admin_token = _admin_token(client)
    audit_resp = client.get(
        "/api/v1/audit", params={"device_id": "dev-001"}, headers=_auth_header(admin_token)
    )
    assert audit_resp.status_code == 200
    events = [row["event"] for row in audit_resp.json()]
    assert "register" in events
    assert "auth_success" in events
    assert "telemetry_accept" in events


def test_pending_registration_then_manual_approve(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, auto_approve=False)
    client = TestClient(app)

    resp, dev_key_pem = _register(client, "dev-pending")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["cert_pem"] is None

    # A pending device cannot yet get a token (no cert to present).
    admin_token = _admin_token(client)

    approve_resp = client.post(
        "/api/v1/devices/dev-pending/approve", headers=_auth_header(admin_token)
    )
    assert approve_resp.status_code == 200, approve_resp.text
    assert approve_resp.json()["status"] == "approved"

    devices_resp = client.get("/api/v1/devices", headers=_auth_header(admin_token))
    assert devices_resp.status_code == 200
    devices = {d["device_id"]: d for d in devices_resp.json()}
    assert devices["dev-pending"]["status"] == "approved"
    assert devices["dev-pending"]["cert_serial"] is not None


# ---------------------------------------------------------------------------
# Forged JWS rejected
# ---------------------------------------------------------------------------


def test_forged_jws_is_rejected(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, auto_approve=True)
    client = TestClient(app)

    resp, _real_dev_key_pem = _register(client, "dev-002")
    cert_pem = resp.json()["cert_pem"]

    token_resp = client.post("/api/v1/auth/token", json={"cert_pem": cert_pem})
    device_token = token_resp.json()["access_token"]

    # Sign the payload with an unrelated key, not the device's actual private key.
    _other_csr, other_key_pem = pki.create_csr("someone-else")
    forged_jws = sign_payload({"device_id": "dev-002", "metric": "cpu", "value": 999}, other_key_pem)

    tele_resp = client.post(
        "/api/v1/telemetry",
        json={"device_id": "dev-002", "jws": forged_jws},
        headers=_auth_header(device_token),
    )
    assert tele_resp.status_code == 401

    admin_token = _admin_token(client)
    audit_resp = client.get(
        "/api/v1/audit", params={"device_id": "dev-002"}, headers=_auth_header(admin_token)
    )
    events = [row["event"] for row in audit_resp.json()]
    assert "telemetry_reject" in events


def test_malformed_jws_is_rejected(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, auto_approve=True)
    client = TestClient(app)

    resp, _dev_key_pem = _register(client, "dev-003")
    cert_pem = resp.json()["cert_pem"]
    token = client.post("/api/v1/auth/token", json={"cert_pem": cert_pem}).json()["access_token"]

    tele_resp = client.post(
        "/api/v1/telemetry",
        json={"device_id": "dev-003", "jws": "not-a-valid-jws"},
        headers=_auth_header(token),
    )
    assert tele_resp.status_code == 401


# ---------------------------------------------------------------------------
# Revocation blocks token issuance
# ---------------------------------------------------------------------------


def test_revoked_device_denied_token(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, auto_approve=True)
    client = TestClient(app)

    resp, _dev_key_pem = _register(client, "dev-004")
    cert_pem = resp.json()["cert_pem"]

    # Sanity check: token issuance works before revocation.
    ok_resp = client.post("/api/v1/auth/token", json={"cert_pem": cert_pem})
    assert ok_resp.status_code == 200

    admin_token = _admin_token(client)
    revoke_resp = client.post("/api/v1/devices/dev-004/revoke", headers=_auth_header(admin_token))
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["status"] == "revoked"

    denied_resp = client.post("/api/v1/auth/token", json={"cert_pem": cert_pem})
    assert denied_resp.status_code == 401

    audit_resp = client.get(
        "/api/v1/audit", params={"device_id": "dev-004"}, headers=_auth_header(admin_token)
    )
    events = [row["event"] for row in audit_resp.json()]
    assert "auth_fail" in events
    assert "revoke" in events


def test_revoked_device_denied_telemetry_even_with_stale_token(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, auto_approve=True)
    client = TestClient(app)

    resp, dev_key_pem = _register(client, "dev-005")
    cert_pem = resp.json()["cert_pem"]
    device_token = client.post("/api/v1/auth/token", json={"cert_pem": cert_pem}).json()[
        "access_token"
    ]

    admin_token = _admin_token(client)
    client.post("/api/v1/devices/dev-005/revoke", headers=_auth_header(admin_token))

    jws = sign_payload({"device_id": "dev-005", "metric": "cpu", "value": 1}, dev_key_pem)
    tele_resp = client.post(
        "/api/v1/telemetry",
        json={"device_id": "dev-005", "jws": jws},
        headers=_auth_header(device_token),
    )
    assert tele_resp.status_code == 401


# ---------------------------------------------------------------------------
# RBAC 403 for device role on admin/operator APIs
# ---------------------------------------------------------------------------


def test_device_role_forbidden_on_admin_apis(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, auto_approve=True)
    client = TestClient(app)

    resp, _dev_key_pem = _register(client, "dev-006")
    cert_pem = resp.json()["cert_pem"]
    device_token = client.post("/api/v1/auth/token", json={"cert_pem": cert_pem}).json()[
        "access_token"
    ]

    list_resp = client.get("/api/v1/devices", headers=_auth_header(device_token))
    assert list_resp.status_code == 403

    approve_resp = client.post(
        "/api/v1/devices/dev-006/approve", headers=_auth_header(device_token)
    )
    assert approve_resp.status_code == 403

    audit_resp = client.get("/api/v1/audit", headers=_auth_header(device_token))
    assert audit_resp.status_code == 403


def test_operator_role_forbidden_on_approve_and_revoke(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, auto_approve=True)
    client = TestClient(app)

    resp, _dev_key_pem = _register(client, "dev-007")
    operator_token = _operator_token(client)

    # Operators may list devices...
    list_resp = client.get("/api/v1/devices", headers=_auth_header(operator_token))
    assert list_resp.status_code == 200

    # ...but not approve/revoke, which are admin-only.
    approve_resp = client.post(
        "/api/v1/devices/dev-007/approve", headers=_auth_header(operator_token)
    )
    assert approve_resp.status_code == 403


def test_telemetry_requires_bearer_token(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, auto_approve=True)
    client = TestClient(app)

    resp, dev_key_pem = _register(client, "dev-008")
    jws = sign_payload({"device_id": "dev-008"}, dev_key_pem)

    tele_resp = client.post("/api/v1/telemetry", json={"device_id": "dev-008", "jws": jws})
    assert tele_resp.status_code == 401


def test_device_token_cannot_submit_telemetry_for_another_device(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, auto_approve=True)
    client = TestClient(app)

    resp_a, key_a = _register(client, "dev-a")
    resp_b, key_b = _register(client, "dev-b")
    token_a = client.post(
        "/api/v1/auth/token", json={"cert_pem": resp_a.json()["cert_pem"]}
    ).json()["access_token"]

    # dev-a's token used to submit telemetry claiming to be dev-b, signed with dev-b's key.
    jws = sign_payload({"device_id": "dev-b"}, key_b)
    tele_resp = client.post(
        "/api/v1/telemetry",
        json={"device_id": "dev-b", "jws": jws},
        headers=_auth_header(token_a),
    )
    assert tele_resp.status_code == 403


# ---------------------------------------------------------------------------
# INSECURE_MODE bypass
# ---------------------------------------------------------------------------


def test_insecure_mode_bypasses_auth_and_sets_header(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, auto_approve=True, insecure_mode=True)
    client = TestClient(app)

    # No Authorization header at all, yet the admin-only endpoint succeeds.
    resp = client.get("/api/v1/devices")
    assert resp.status_code == 200
    assert resp.headers.get("X-EAM-Mode") == "insecure"

    # Telemetry with a completely bogus jws is accepted too (no verification).
    tele_resp = client.post(
        "/api/v1/telemetry", json={"device_id": "unregistered-device", "jws": "totally-not-a-jws"}
    )
    assert tele_resp.status_code == 200
    assert tele_resp.json()["status"] == "accepted"
    assert tele_resp.headers.get("X-EAM-Mode") == "insecure"


def test_secure_mode_does_not_set_insecure_header(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, auto_approve=True, insecure_mode=False)
    client = TestClient(app)

    resp = client.get("/api/v1/healthz")
    assert resp.status_code == 200
    assert "X-EAM-Mode" not in resp.headers

    # And the same admin-only endpoint is properly denied without a token.
    forbidden_resp = client.get("/api/v1/devices")
    assert forbidden_resp.status_code == 401


# ---------------------------------------------------------------------------
# Misc: bootstrap token, health, audit-of-everything
# ---------------------------------------------------------------------------


def test_register_rejects_bad_bootstrap_token(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, auto_approve=True)
    client = TestClient(app)

    csr_pem, _key = pki.create_csr("dev-009")
    resp = client.post(
        "/api/v1/devices/register",
        json={
            "device_id": "dev-009",
            "site": "seoul",
            "group": "line-a",
            "csr_pem": csr_pem.decode(),
            "bootstrap_token": "wrong-token",
        },
    )
    assert resp.status_code == 401


def test_healthz_is_unauthenticated(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)

    assert client.get("/api/v1/healthz").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_every_request_is_recorded_in_audit_log(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, auto_approve=True)
    client = TestClient(app)

    client.get("/api/v1/healthz")
    client.get("/api/v1/healthz")

    admin_token = _admin_token(client)
    audit_resp = client.get(
        "/api/v1/audit", params={"event": "http"}, headers=_auth_header(admin_token)
    )
    assert audit_resp.status_code == 200
    http_events = audit_resp.json()
    assert len(http_events) >= 2
    assert all(row["event"] == "http" for row in http_events)


# ---------------------------------------------------------------------------
# Fix report: malformed CSR must never crash unhandled (Important 1)
# ---------------------------------------------------------------------------


def test_register_rejects_malformed_csr(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, auto_approve=True)
    client = TestClient(app)

    resp = client.post(
        "/api/v1/devices/register",
        json={
            "device_id": "dev-bad-csr",
            "site": "seoul",
            "group": "line-a",
            "csr_pem": "-----BEGIN CERTIFICATE REQUEST-----\nnot a real CSR\n-----END CERTIFICATE REQUEST-----\n",
            "bootstrap_token": BOOTSTRAP_TOKEN,
        },
    )
    # Must be a clean 400, never an unhandled 500.
    assert resp.status_code == 400

    admin_token = _admin_token(client)
    audit_resp = client.get(
        "/api/v1/audit", params={"device_id": "dev-bad-csr"}, headers=_auth_header(admin_token)
    )
    events = {row["event"]: row for row in audit_resp.json()}
    assert "register" in events
    assert events["register"]["outcome"] == "fail"


def test_middleware_audits_and_responds_cleanly_on_unhandled_exception(tmp_path, monkeypatch):
    # Simulate a genuinely unexpected (non-HTTPException) exception from deep
    # inside a route to prove the audit middleware records it and still
    # returns a well-formed response (with the insecure-mode header intact)
    # instead of letting it crash past the middleware unaudited.
    app = _make_app(tmp_path, monkeypatch, auto_approve=True, insecure_mode=True)
    client = TestClient(app)

    def _boom(*args, **kwargs):
        raise RuntimeError("boom: simulated unexpected failure")

    monkeypatch.setattr(app.state.store, "list_devices", _boom)

    resp = client.get("/api/v1/devices")
    assert resp.status_code == 500
    assert resp.headers.get("X-EAM-Mode") == "insecure"

    http_events = app.state.audit.query(event="http", limit=10)
    assert any(e.outcome == "error" and "unhandled_exception" in e.detail for e in http_events)


# ---------------------------------------------------------------------------
# Fix report: CSR SAN identity must match the claimed device_id (Important 2)
# ---------------------------------------------------------------------------


def test_register_rejects_csr_with_mismatched_san_identity(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, auto_approve=True)
    client = TestClient(app)

    # CSR is genuinely for "someone-else", but the caller claims "dev-claim".
    csr_pem, _key = pki.create_csr("someone-else")
    resp = client.post(
        "/api/v1/devices/register",
        json={
            "device_id": "dev-claim",
            "site": "seoul",
            "group": "line-a",
            "csr_pem": csr_pem.decode(),
            "bootstrap_token": BOOTSTRAP_TOKEN,
        },
    )
    assert resp.status_code == 400

    # No zombie registration should have been persisted, and the failure is audited.
    admin_token = _admin_token(client)
    devices_resp = client.get("/api/v1/devices", headers=_auth_header(admin_token))
    assert "dev-claim" not in {d["device_id"] for d in devices_resp.json()}

    audit_resp = client.get(
        "/api/v1/audit", params={"device_id": "dev-claim"}, headers=_auth_header(admin_token)
    )
    events = {row["event"]: row for row in audit_resp.json()}
    assert "register" in events
    assert events["register"]["outcome"] == "fail"


def test_approve_rejects_csr_with_mismatched_san_identity(tmp_path, monkeypatch):
    # Bypass the (now-validating) register endpoint to simulate a pending
    # device whose stored CSR does not match its device_id, and confirm
    # /approve independently rejects it too (defense in depth).
    app = _make_app(tmp_path, monkeypatch, auto_approve=False)
    client = TestClient(app)

    csr_pem, _key = pki.create_csr("someone-else")
    app.state.store.register_device(
        device_id="dev-zombie", site="seoul", group_name="line-a",
        csr_pem=csr_pem.decode(), status="pending",
    )

    admin_token = _admin_token(client)
    approve_resp = client.post(
        "/api/v1/devices/dev-zombie/approve", headers=_auth_header(admin_token)
    )
    assert approve_resp.status_code == 400

    devices_resp = client.get("/api/v1/devices", headers=_auth_header(admin_token))
    devices = {d["device_id"]: d for d in devices_resp.json()}
    assert devices["dev-zombie"]["status"] == "pending"

    audit_resp = client.get(
        "/api/v1/audit", params={"device_id": "dev-zombie"}, headers=_auth_header(admin_token)
    )
    events = {row["event"]: row for row in audit_resp.json()}
    assert "approve" in events
    assert events["approve"]["outcome"] == "fail"
