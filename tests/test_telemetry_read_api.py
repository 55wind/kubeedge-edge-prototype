"""Tests for GET /api/v1/telemetry — in-platform observability of received data.

Closes the gap where telemetry (and whether its JWS was verified) was only
visible in the database, not through the platform API. RBAC mirrors /devices:
operator and admin may read, device may not.
"""
from __future__ import annotations

import time
import uuid

from fastapi.testclient import TestClient

from eam.common import pki
from eam.common.jws import sign_payload
from eam.manager.app import create_app

BOOTSTRAP_TOKEN = "test-bootstrap-token"
ADMIN_USER, ADMIN_PASS = "admin", "admin-pass"
OPERATOR_USER, OPERATOR_PASS = "operator", "operator-pass"


def _make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_TOKEN", BOOTSTRAP_TOKEN)
    monkeypatch.setenv("EAM_ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("EAM_ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setenv("EAM_OPERATOR_USERNAME", OPERATOR_USER)
    monkeypatch.setenv("EAM_OPERATOR_PASSWORD", OPERATOR_PASS)
    monkeypatch.setenv("AUTO_APPROVE", "true")
    monkeypatch.delenv("INSECURE_MODE", raising=False)
    return create_app(
        certs_dir=tmp_path / "certs",
        store_db_path=tmp_path / "eam.db",
        audit_db_path=tmp_path / "eam_audit.db",
    )


def _op_token(client, admin=False):
    u, p = (ADMIN_USER, ADMIN_PASS) if admin else (OPERATOR_USER, OPERATOR_PASS)
    return client.post("/api/v1/auth/operator", json={"username": u, "password": p}).json()[
        "access_token"
    ]


def _submit_one(client, device_id="dev-t"):
    csr_pem, key_pem = pki.create_csr(device_id)
    cert = client.post("/api/v1/devices/register", json={
        "device_id": device_id, "site": "s", "group": "g",
        "csr_pem": csr_pem.decode(), "bootstrap_token": BOOTSTRAP_TOKEN}).json()["cert_pem"]
    token = client.post("/api/v1/auth/token", json={"cert_pem": cert}).json()["access_token"]
    jws = sign_payload({"device_id": device_id, "value": 21,
                        "jti": uuid.uuid4().hex, "iat": int(time.time())}, key_pem)
    r = client.post("/api/v1/telemetry", json={"device_id": device_id, "jws": jws},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return token


def test_operator_can_read_telemetry_with_verified_flag(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    _submit_one(client, "dev-t")

    resp = client.get("/api/v1/telemetry",
                      headers={"Authorization": f"Bearer {_op_token(client)}"})
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["device_id"] == "dev-t"
    assert rows[0]["verified"] is True
    assert rows[0]["payload"]["value"] == 21


def test_admin_can_filter_telemetry_by_device(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    _submit_one(client, "dev-a")
    _submit_one(client, "dev-b")

    resp = client.get("/api/v1/telemetry", params={"device_id": "dev-b"},
                      headers={"Authorization": f"Bearer {_op_token(client, admin=True)}"})
    assert resp.status_code == 200
    rows = resp.json()
    assert rows and all(r["device_id"] == "dev-b" for r in rows)


def test_device_token_forbidden_on_telemetry_read(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    device_token = _submit_one(client, "dev-t")

    resp = client.get("/api/v1/telemetry",
                      headers={"Authorization": f"Bearer {device_token}"})
    assert resp.status_code == 403


def test_telemetry_read_requires_auth(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    assert client.get("/api/v1/telemetry").status_code == 401
