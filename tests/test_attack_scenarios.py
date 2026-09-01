"""시나리오 기반 공격 테스트 (red-team) — CI 상시 회귀 버전.

security/attack_scenarios.py 의 대화형 red-team 실행을 pytest 회귀 스위트로
편입한 것. 각 시나리오는 (1) 플랫폼이 공격을 차단하는지 (상태코드)와
(2) 그 시도가 감사 로그에 남는지를 함께 검증한다. 감사 확인은 admin API가
아니라 ``app.state.audit`` 를 직접 조회해 우회 없이 확인한다.
"""
from __future__ import annotations

import base64
import json
import time
import uuid

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from eam.common import pki
from eam.common.jws import sign_payload
from eam.manager.app import create_app

BOOTSTRAP_TOKEN = "redteam-bootstrap"
ADMIN_USER, ADMIN_PASS = "admin", "redteam-admin"
OPERATOR_USER, OPERATOR_PASS = "operator", "redteam-operator"
JWT_ISS, JWT_AUD = "edge-auth-manager", "edge-agents"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Secure-mode Manager + two legit devices + operator/admin tokens."""
    monkeypatch.setenv("BOOTSTRAP_TOKEN", BOOTSTRAP_TOKEN)
    monkeypatch.setenv("EAM_ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("EAM_ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setenv("EAM_OPERATOR_USERNAME", OPERATOR_USER)
    monkeypatch.setenv("EAM_OPERATOR_PASSWORD", OPERATOR_PASS)
    monkeypatch.setenv("AUTO_APPROVE", "true")
    monkeypatch.setenv("JWT_ISS", JWT_ISS)
    monkeypatch.setenv("JWT_AUD", JWT_AUD)
    monkeypatch.delenv("INSECURE_MODE", raising=False)
    app = create_app(certs_dir=tmp_path / "certs",
                     store_db_path=tmp_path / "m.db",
                     audit_db_path=tmp_path / "a.db")
    client = TestClient(app)

    def enroll(device_id):
        csr, key = pki.create_csr(device_id)
        cert = client.post("/api/v1/devices/register", json={
            "device_id": device_id, "site": "s", "group": "g",
            "csr_pem": csr.decode(), "bootstrap_token": BOOTSTRAP_TOKEN}).json()["cert_pem"]
        token = client.post("/api/v1/auth/token", json={"cert_pem": cert}).json()["access_token"]
        return cert, key, token

    certA, keyA, tokenA = enroll("dev-alpha")
    certB, keyB, tokenB = enroll("dev-bravo")
    op = client.post("/api/v1/auth/operator",
                     json={"username": OPERATOR_USER, "password": OPERATOR_PASS}).json()["access_token"]
    admin = client.post("/api/v1/auth/operator",
                        json={"username": ADMIN_USER, "password": ADMIN_PASS}).json()["access_token"]

    def audited(*, event=None, detail_contains="", device_id=None):
        for r in app.state.audit.query(device_id=device_id, event=event, limit=500):
            if detail_contains and detail_contains not in (r.detail or ""):
                continue
            return True
        return False

    return dict(app=app, client=client, audited=audited,
                certA=certA, keyA=keyA, tokenA=tokenA,
                certB=certB, keyB=keyB, tokenB=tokenB, op=op, admin=admin)


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


def _evil_rsa_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())


# --- S1 ---------------------------------------------------------------------
def test_s1_unauthenticated_access_blocked(env):
    c = env["client"]
    for method, path in [("GET", "/api/v1/devices"), ("GET", "/api/v1/audit"),
                         ("GET", "/api/v1/telemetry"),
                         ("POST", "/api/v1/devices/dev-alpha/revoke")]:
        assert c.request(method, path).status_code == 401
    assert env["audited"](event="http", detail_contains="401")


# --- S2 ---------------------------------------------------------------------
def test_s2_jwt_alg_none_forgery_blocked(env):
    forged = pyjwt.encode({"sub": "admin", "role": "admin", "iss": JWT_ISS,
                           "aud": JWT_AUD, "exp": int(time.time()) + 900},
                          key="", algorithm="none")
    r = env["client"].get("/api/v1/audit", headers=_hdr(forged))
    assert r.status_code == 401


# --- S3 ---------------------------------------------------------------------
def test_s3_attacker_signed_admin_jwt_blocked(env):
    forged = pyjwt.encode({"sub": "admin", "role": "admin", "iss": JWT_ISS,
                           "aud": JWT_AUD, "iat": int(time.time()),
                           "exp": int(time.time()) + 900}, _evil_rsa_pem(), algorithm="RS256")
    r = env["client"].get("/api/v1/devices", headers=_hdr(forged))
    assert r.status_code == 401


# --- S4 ---------------------------------------------------------------------
def test_s4_tampered_payload_blocked(env):
    h, p, s = env["tokenA"].split(".")
    payload = json.loads(base64.urlsafe_b64decode(p + "=="))
    payload["role"] = "admin"
    p2 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    r = env["client"].get("/api/v1/audit", headers=_hdr(f"{h}.{p2}.{s}"))
    assert r.status_code == 401


# --- S5 ---------------------------------------------------------------------
def test_s5_expired_jwt_blocked(env):
    expired = pyjwt.encode(
        {"sub": "dev-alpha", "role": "device", "iss": JWT_ISS, "aud": JWT_AUD,
         "iat": int(time.time()) - 2000, "exp": int(time.time()) - 1000},
        env["app"].state.jwt_private_pem, algorithm="RS256")
    r = env["client"].post("/api/v1/telemetry", headers=_hdr(expired),
                           json={"device_id": "dev-alpha", "jws": "x"})
    assert r.status_code == 401


# --- S6 / S7 RBAC -----------------------------------------------------------
def test_s6_operator_to_admin_escalation_blocked(env):
    c = env["client"]
    for method, path in [("GET", "/api/v1/audit"),
                         ("POST", "/api/v1/devices/dev-alpha/revoke"),
                         ("POST", "/api/v1/devices/dev-alpha/approve")]:
        assert c.request(method, path, headers=_hdr(env["op"])).status_code == 403
    assert env["audited"](event="http", detail_contains="403")


def test_s7_device_to_admin_escalation_blocked(env):
    c = env["client"]
    for path in ["/api/v1/devices", "/api/v1/audit", "/api/v1/telemetry"]:
        assert c.get(path, headers=_hdr(env["tokenA"])).status_code == 403


# --- S8 ---------------------------------------------------------------------
def test_s8_cross_device_impersonation_blocked(env):
    jws = sign_payload({"device_id": "dev-bravo", "value": 9,
                        "jti": uuid.uuid4().hex, "iat": int(time.time())}, env["keyB"])
    r = env["client"].post("/api/v1/telemetry", headers=_hdr(env["tokenA"]),
                           json={"device_id": "dev-bravo", "jws": jws})
    assert r.status_code == 403
    assert env["audited"](event="telemetry_reject", device_id="dev-bravo")


# --- S9 ---------------------------------------------------------------------
def test_s9_forged_jws_signature_blocked(env):
    jws = sign_payload({"device_id": "dev-alpha", "value": 1,
                        "jti": uuid.uuid4().hex, "iat": int(time.time())}, _evil_rsa_pem())
    r = env["client"].post("/api/v1/telemetry", headers=_hdr(env["tokenA"]),
                           json={"device_id": "dev-alpha", "jws": jws})
    assert r.status_code == 401
    assert env["audited"](event="telemetry_reject", detail_contains="JWS")


# --- S10 --------------------------------------------------------------------
def test_s10_csr_san_spoofing_blocked(env):
    spoof, _ = pki.create_csr("dev-admin-spoof")
    r = env["client"].post("/api/v1/devices/register", json={
        "device_id": "dev-legit", "site": "x", "group": "y",
        "csr_pem": spoof.decode(), "bootstrap_token": BOOTSTRAP_TOKEN})
    assert r.status_code == 400
    assert env["audited"](event="register", detail_contains="does not")


# --- S11 --------------------------------------------------------------------
def test_s11_malformed_csr_no_500(env):
    r = env["client"].post("/api/v1/devices/register", json={
        "device_id": "dev-junk", "site": "x", "group": "y",
        "csr_pem": "-----BEGIN CERTIFICATE REQUEST-----\nJUNK\n-----END CERTIFICATE REQUEST-----",
        "bootstrap_token": BOOTSTRAP_TOKEN})
    assert r.status_code == 400
    assert env["audited"](event="register", detail_contains="invalid CSR")


# --- S12 --------------------------------------------------------------------
def test_s12_bad_bootstrap_token_blocked(env):
    csr, _ = pki.create_csr("dev-bf")
    r = env["client"].post("/api/v1/devices/register", json={
        "device_id": "dev-bf", "site": "x", "group": "y",
        "csr_pem": csr.decode(), "bootstrap_token": "wrong-guess"})
    assert r.status_code == 401
    assert env["audited"](event="register", detail_contains="invalid bootstrap")


# --- S13 --------------------------------------------------------------------
def test_s13_foreign_ca_cert_blocked(env):
    ca_cert, ca_key = pki.create_ca("EVIL CA")
    csr, _ = pki.create_csr("dev-alpha")
    leaf = pki.sign_csr(ca_cert, ca_key, csr)
    r = env["client"].post("/api/v1/auth/token", json={"cert_pem": leaf.decode()})
    assert r.status_code == 401
    assert env["audited"](event="auth_fail", detail_contains="chain")


# --- S14 --------------------------------------------------------------------
def test_s14_revoked_cert_reuse_blocked(env):
    rr = env["client"].post("/api/v1/devices/dev-bravo/revoke", headers=_hdr(env["admin"]))
    assert rr.status_code == 200
    r = env["client"].post("/api/v1/auth/token", json={"cert_pem": env["certB"]})
    assert r.status_code == 401
    assert env["audited"](event="auth_fail", device_id="dev-bravo", detail_contains="revoked")


# --- S15 (the fix) ----------------------------------------------------------
def test_s15_telemetry_replay_blocked(env):
    jws = sign_payload({"device_id": "dev-alpha", "value": 21,
                        "jti": uuid.uuid4().hex, "iat": int(time.time())}, env["keyA"])
    body = {"device_id": "dev-alpha", "jws": jws}
    first = env["client"].post("/api/v1/telemetry", headers=_hdr(env["tokenA"]), json=body)
    assert first.status_code == 200, first.text
    second = env["client"].post("/api/v1/telemetry", headers=_hdr(env["tokenA"]), json=body)
    assert second.status_code == 409   # replay now blocked (was 200 before the fix)
    assert env["audited"](event="telemetry_reject", detail_contains="replay")
