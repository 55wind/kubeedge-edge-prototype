"""FastAPI application: Manager 서비스 — 등록·AAA·데이터 수신.

The app is created via the :func:`create_app` factory (never a module-level
``app`` singleton) so tests can spin up isolated instances against temporary
``CERTS_DIR``/db paths (see the task brief's in-process testing guidance).

Endpoints (prefix ``/api/v1`` unless noted):

* ``POST /devices/register``           - bootstrap-token gated device registration.
* ``POST /auth/token``                 - cert -> device bearer JWT (role=device).
* ``POST /auth/operator``              - username/password -> operator|admin JWT.
* ``POST /telemetry``                  - bearer JWT + device-signed JWS payload
                                         (with jti/iat replay protection).
* ``GET  /telemetry``                  - RBAC: operator, admin (read stored data).
* ``GET  /devices``                    - RBAC: operator, admin.
* ``POST /devices/{device_id}/approve``- RBAC: admin.
* ``POST /devices/{device_id}/revoke`` - RBAC: admin.
* ``GET  /audit``                      - RBAC: admin.
* ``GET  /healthz`` (and root ``/healthz``) - unauthenticated liveness probe.

A single HTTP middleware records an ``event="http"`` audit row for every
request and, when ``INSECURE_MODE=true``, stamps every response with
``X-EAM-Mode: insecure`` for the before/after security demo.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Optional, Union

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509 import load_pem_x509_certificate
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer

from eam.common import config as config_module
from eam.common import pki
from eam.common.audit import AuditLog
from eam.common.jws import JWSVerificationError, verify_payload
from eam.manager import rbac, schemas
from eam.manager.ca import ManagerCA
from eam.manager.store import DeviceStore

logger = logging.getLogger("eam.manager")

PathLike = Union[str, "os.PathLike[str]"]

JWT_ALGORITHM = "RS256"
JWT_PRIVATE_KEY_FILENAME = "jwt_rs256.pem"
JWT_PUBLIC_KEY_FILENAME = "jwt_rs256_pub.pem"


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------


def _load_or_create_jwt_keypair(certs_dir: Path) -> "tuple[bytes, bytes]":
    """Load the Manager's RS256 bearer-JWT signing key pair, creating it if absent."""
    key_path = certs_dir / JWT_PRIVATE_KEY_FILENAME
    pub_path = certs_dir / JWT_PUBLIC_KEY_FILENAME
    if key_path.exists() and pub_path.exists():
        return key_path.read_bytes(), pub_path.read_bytes()

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    certs_dir.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(private_pem)
    pub_path.write_bytes(public_pem)
    return private_pem, public_pem


def _load_or_generate_credential(
    env_user: str, env_pass: str, default_username: str, label: str
) -> "tuple[str, str]":
    """Read a username/password pair from env, generating+logging one if missing."""
    username = os.environ.get(env_user)
    password = os.environ.get(env_pass)
    generated = False
    if not username:
        username = default_username
        generated = True
    if not password:
        password = secrets.token_urlsafe(12)
        generated = True
    if generated:
        logger.warning(
            "[EAM] Generated %s credentials for this demo run "
            "(set %s/%s to override): username=%s password=%s",
            label, env_user, env_pass, username, password,
        )
    return username, password


def _load_or_generate_bootstrap_token() -> str:
    token = os.environ.get("BOOTSTRAP_TOKEN")
    if token:
        return token
    token = secrets.token_urlsafe(24)
    logger.warning(
        "[EAM] Generated bootstrap token for this demo run "
        "(set BOOTSTRAP_TOKEN to override): %s",
        token,
    )
    return token


def _issue_jwt(
    private_pem: bytes, sub: str, role: str, issuer: str, audience: str, ttl_seconds: int
) -> "tuple[str, int]":
    now = int(time.time())
    payload = {
        "sub": sub,
        "role": role,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    token = pyjwt.encode(payload, private_pem, algorithm=JWT_ALGORITHM)
    return token, ttl_seconds


def _device_public_key_pem(cert_pem: str) -> bytes:
    cert = load_pem_x509_certificate(cert_pem.encode())
    return cert.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _normalize_header_pem(value: str) -> bytes:
    """Unescape literal ``\\n`` sequences used to smuggle a PEM through a header.

    Reverse-proxy mTLS setups (e.g. nginx ``$ssl_client_cert``) commonly pass
    a forwarded client certificate through a header with real newlines
    replaced by the two-character escape ``\\n`` since raw newlines are not
    valid inside an HTTP header value.
    """
    return value.replace("\\n", "\n").encode()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    certs_dir: Optional[PathLike] = None,
    store_db_path: Optional[PathLike] = None,
    audit_db_path: Optional[PathLike] = None,
) -> FastAPI:
    """Build the Manager FastAPI app.

    All three path overrides exist for test isolation; when omitted they are
    derived from ``eam.common.config.load_config()`` (i.e. ``CERTS_DIR`` /
    ``DB_URL`` env vars).
    """
    cfg = config_module.load_config()

    resolved_certs_dir = Path(certs_dir) if certs_dir is not None else cfg.certs_dir
    resolved_certs_dir.mkdir(parents=True, exist_ok=True)

    if store_db_path is not None:
        resolved_store_db = Path(store_db_path)
    else:
        resolved_store_db = Path(cfg.db_url)

    if audit_db_path is not None:
        resolved_audit_db = Path(audit_db_path)
    else:
        stem_db = Path(cfg.db_url)
        resolved_audit_db = stem_db.with_name(stem_db.stem + "_audit" + (stem_db.suffix or ".db"))

    ca = ManagerCA(resolved_certs_dir)
    store = DeviceStore(resolved_store_db)
    audit = AuditLog(resolved_audit_db)
    jwt_private_pem, jwt_public_pem = _load_or_create_jwt_keypair(resolved_certs_dir)
    bootstrap_token = _load_or_generate_bootstrap_token()
    admin_user, admin_pass = _load_or_generate_credential(
        "EAM_ADMIN_USERNAME", "EAM_ADMIN_PASSWORD", "admin", "admin"
    )
    operator_user, operator_pass = _load_or_generate_credential(
        "EAM_OPERATOR_USERNAME", "EAM_OPERATOR_PASSWORD", "operator", "operator"
    )

    app = FastAPI(title="EAM Manager", version="0.2.0")
    # Declares "Bearer <JWT>" in the OpenAPI security schema without enforcing it
    # (auto_error=False): enforcement stays in ``authenticated`` below.
    bearer_scheme = HTTPBearer(auto_error=False)
    app.state.cfg = cfg
    app.state.ca = ca
    app.state.store = store
    app.state.audit = audit
    app.state.jwt_private_pem = jwt_private_pem
    app.state.jwt_public_pem = jwt_public_pem
    app.state.bootstrap_token = bootstrap_token
    app.state.admin_user = admin_user
    app.state.admin_pass = admin_pass
    app.state.operator_user = operator_user
    app.state.operator_pass = operator_pass
    app.state.insecure_mode = cfg.insecure_mode

    # -- middleware: audit every request, stamp insecure-mode header ----------

    @app.middleware("http")
    async def audit_and_mode_middleware(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception as exc:
            # An unhandled exception raised by a route (e.g. a bare ValueError
            # from a malformed CSR/cert) would otherwise propagate straight
            # past this middleware's post-processing to Starlette's
            # ServerErrorMiddleware, producing a 500 with NO audit row at all
            # ("모든 요청 감사기록" would be violated) and no X-EAM-Mode header.
            # Catch it here so every request — success, handled error, or
            # crash — gets exactly one audit row and a well-formed response.
            logger.exception(
                "unhandled exception while processing %s %s", request.method, request.url.path
            )
            try:
                app.state.audit.record(
                    event="http",
                    device_id=None,
                    outcome="error",
                    detail=(
                        f"{request.method} {request.url.path} 500 "
                        f"unhandled_exception={exc.__class__.__name__}: {exc}"
                    ),
                )
            except Exception:  # pragma: no cover - audit logging must never break the app
                logger.exception("failed to record http audit event for unhandled exception")
            response = JSONResponse(status_code=500, content={"detail": "internal server error"})
            if app.state.insecure_mode:
                response.headers["X-EAM-Mode"] = "insecure"
            return response

        try:
            outcome = "ok" if response.status_code < 400 else "error"
            app.state.audit.record(
                event="http",
                device_id=None,
                outcome=outcome,
                detail=f"{request.method} {request.url.path} {response.status_code}",
            )
        except Exception:  # pragma: no cover - audit logging must never break the app
            logger.exception("failed to record http audit event")
        if app.state.insecure_mode:
            response.headers["X-EAM-Mode"] = "insecure"
        return response

    # -- auth dependency --------------------------------------------------

    async def _bearer_identity(request: Request) -> schemas.Identity:
        auth_header = request.headers.get("authorization")
        if not auth_header or not auth_header.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        token = auth_header.split(" ", 1)[1].strip()
        try:
            payload = pyjwt.decode(
                token,
                app.state.jwt_public_pem,
                algorithms=[JWT_ALGORITHM],
                audience=app.state.cfg.jwt.audience,
                issuer=app.state.cfg.jwt.issuer,
            )
        except pyjwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc
        return schemas.Identity(sub=str(payload.get("sub", "")), role=str(payload.get("role", "")))

    async def authenticated(
        request: Request,
        _scheme: Optional[object] = Depends(bearer_scheme),
    ) -> schemas.Identity:
        """Resolve caller identity and enforce RBAC for the current route.

        ``_scheme`` only declares the bearer requirement in the OpenAPI schema
        (so API consumers and Swagger UI can supply a token); it never enforces
        anything on its own — ``auto_error=False`` keeps the checks below, and
        INSECURE_MODE bypass, as the single source of truth.

        Bypassed entirely when INSECURE_MODE is enabled (before/after demo).
        """
        if app.state.insecure_mode:
            return schemas.Identity(sub="insecure-bypass", role="admin")
        identity = await _bearer_identity(request)
        if not rbac.authorize(identity.role, request.method, request.url.path):
            raise HTTPException(
                status_code=403, detail="forbidden: role not permitted for this operation"
            )
        return identity

    # -- CSR validation -----------------------------------------------------

    def _validate_csr_matches_device(csr_pem_bytes: bytes, device_id: str, *, event: str) -> None:
        """Reject a CSR that is malformed or whose SAN identity is not ``device_id``.

        Used at both /devices/register and /devices/{id}/approve: a CSR is
        the caller's *claim* to an identity, and signing it unchecked would
        let a request register under one device_id while the issued
        certificate actually asserts a different one (a "zombie"
        registration). This also prevents malformed CSRs from ever reaching
        ``cryptography``'s CSR parser inside ``ca.sign_csr``, which raises a
        bare ``ValueError`` that is not an HTTPException.
        """
        try:
            claimed_device_id = pki.device_id_from_csr(csr_pem_bytes)
        except ValueError as exc:
            app.state.audit.record(
                event=event, device_id=device_id, outcome="fail",
                detail=f"invalid CSR: {exc}",
            )
            raise HTTPException(status_code=400, detail="invalid CSR") from exc

        if claimed_device_id != device_id:
            app.state.audit.record(
                event=event, device_id=device_id, outcome="fail",
                detail=(
                    f"CSR SAN device identity {claimed_device_id!r} does not "
                    f"match device_id {device_id!r}"
                ),
            )
            raise HTTPException(
                status_code=400,
                detail="CSR SAN device identity does not match device_id",
            )

    # -- router -------------------------------------------------------------

    router = APIRouter(prefix="/api/v1")

    @router.get("/healthz", response_model=schemas.HealthResponse)
    async def healthz() -> schemas.HealthResponse:
        return schemas.HealthResponse()

    @router.post("/devices/register", response_model=schemas.RegisterResponse)
    async def register_device(body: schemas.RegisterRequest) -> schemas.RegisterResponse:
        if not secrets.compare_digest(body.bootstrap_token, app.state.bootstrap_token):
            app.state.audit.record(
                event="register", device_id=body.device_id, outcome="fail",
                detail="invalid bootstrap token",
            )
            raise HTTPException(status_code=401, detail="invalid bootstrap token")

        if store.get_device(body.device_id) is not None:
            app.state.audit.record(
                event="register", device_id=body.device_id, outcome="fail",
                detail="device already registered",
            )
            raise HTTPException(status_code=409, detail="device already registered")

        csr_pem_bytes = body.csr_pem.encode()
        _validate_csr_matches_device(csr_pem_bytes, body.device_id, event="register")

        if app.state.cfg.auto_approve:
            cert_pem_bytes = ca.sign_csr(csr_pem_bytes)
            cert_serial = pki.cert_serial(cert_pem_bytes)
            store.register_device(
                device_id=body.device_id, site=body.site, group_name=body.group,
                csr_pem=body.csr_pem, status="approved",
                cert_serial=cert_serial, cert_pem=cert_pem_bytes.decode(),
            )
            app.state.audit.record(
                event="register", device_id=body.device_id, outcome="approved",
                detail=f"site={body.site} group={body.group} auto_approve=true",
            )
            return schemas.RegisterResponse(
                device_id=body.device_id, status="approved", cert_pem=cert_pem_bytes.decode(),
            )

        store.register_device(
            device_id=body.device_id, site=body.site, group_name=body.group,
            csr_pem=body.csr_pem, status="pending",
        )
        app.state.audit.record(
            event="register", device_id=body.device_id, outcome="pending",
            detail=f"site={body.site} group={body.group} auto_approve=false",
        )
        return schemas.RegisterResponse(device_id=body.device_id, status="pending", cert_pem=None)

    @router.post("/auth/token", response_model=schemas.TokenResponse)
    async def auth_token(
        body: schemas.TokenRequest, request: Request,
    ) -> schemas.TokenResponse:
        cert_pem_bytes: Optional[bytes] = None
        if body.cert_pem:
            cert_pem_bytes = body.cert_pem.encode()
        else:
            header_cert = request.headers.get("x-client-cert")
            if header_cert:
                cert_pem_bytes = _normalize_header_pem(header_cert)

        if not cert_pem_bytes:
            app.state.audit.record(
                event="auth_fail", device_id=None, outcome="fail",
                detail="missing client certificate",
            )
            raise HTTPException(status_code=400, detail="missing client certificate")

        if not ca.verify_chain(cert_pem_bytes):
            app.state.audit.record(
                event="auth_fail", device_id=None, outcome="fail",
                detail="certificate chain verification failed",
            )
            raise HTTPException(status_code=401, detail="certificate chain verification failed")

        device_id = pki.device_id_from_cert(cert_pem_bytes)
        if not device_id:
            app.state.audit.record(
                event="auth_fail", device_id=None, outcome="fail",
                detail="certificate has no device identity",
            )
            raise HTTPException(status_code=401, detail="certificate has no device identity")

        serial = pki.cert_serial(cert_pem_bytes)

        if ca.is_revoked(serial):
            app.state.audit.record(
                event="auth_fail", device_id=device_id, outcome="fail",
                detail=f"certificate serial {serial} is revoked",
            )
            raise HTTPException(status_code=401, detail="certificate revoked")

        device = store.get_device(device_id)
        if device is None:
            app.state.audit.record(
                event="auth_fail", device_id=device_id, outcome="fail",
                detail="unknown device",
            )
            raise HTTPException(status_code=401, detail="unknown device")
        if device.status == "revoked":
            app.state.audit.record(
                event="auth_fail", device_id=device_id, outcome="fail",
                detail="device revoked",
            )
            raise HTTPException(status_code=401, detail="device revoked")
        if device.status != "approved" or device.cert_serial != serial:
            app.state.audit.record(
                event="auth_fail", device_id=device_id, outcome="fail",
                detail="certificate does not match the device's registered certificate",
            )
            raise HTTPException(status_code=401, detail="certificate/device mismatch")

        token, ttl = _issue_jwt(
            app.state.jwt_private_pem, sub=device_id, role="device",
            issuer=app.state.cfg.jwt.issuer, audience=app.state.cfg.jwt.audience,
            ttl_seconds=app.state.cfg.jwt.ttl_seconds,
        )
        store.touch_last_seen(device_id)
        app.state.audit.record(
            event="auth_success", device_id=device_id, outcome="success", detail="role=device",
        )
        return schemas.TokenResponse(access_token=token, role="device", expires_in=ttl)

    @router.post("/auth/operator", response_model=schemas.TokenResponse)
    async def auth_operator(body: schemas.OperatorAuthRequest) -> schemas.TokenResponse:
        role: Optional[str] = None
        if secrets.compare_digest(body.username, app.state.admin_user) and secrets.compare_digest(
            body.password, app.state.admin_pass
        ):
            role = "admin"
        elif secrets.compare_digest(
            body.username, app.state.operator_user
        ) and secrets.compare_digest(body.password, app.state.operator_pass):
            role = "operator"

        if role is None:
            app.state.audit.record(
                event="auth_fail", device_id=None, outcome="fail",
                detail=f"invalid operator credentials for username={body.username}",
            )
            raise HTTPException(status_code=401, detail="invalid credentials")

        token, ttl = _issue_jwt(
            app.state.jwt_private_pem, sub=body.username, role=role,
            issuer=app.state.cfg.jwt.issuer, audience=app.state.cfg.jwt.audience,
            ttl_seconds=app.state.cfg.jwt.ttl_seconds,
        )
        app.state.audit.record(
            event="auth_success", device_id=None, outcome="success",
            detail=f"username={body.username} role={role}",
        )
        return schemas.TokenResponse(access_token=token, role=role, expires_in=ttl)

    @router.post("/telemetry", response_model=schemas.TelemetryResponse)
    async def submit_telemetry(
        body: schemas.TelemetryRequest,
        identity: schemas.Identity = Depends(authenticated),
    ) -> schemas.TelemetryResponse:
        # A device-role bearer token may only submit telemetry for itself;
        # admin tokens (or insecure-mode's synthetic admin identity) may
        # submit on behalf of any device_id.
        if not app.state.insecure_mode and identity.role == "device" and identity.sub != body.device_id:
            app.state.audit.record(
                event="telemetry_reject", device_id=body.device_id, outcome="fail",
                detail=f"bearer token subject {identity.sub!r} does not match device_id",
            )
            raise HTTPException(status_code=403, detail="token does not authorize this device_id")

        device = None if app.state.insecure_mode else store.get_device(body.device_id)
        if not app.state.insecure_mode:
            if device is None:
                app.state.audit.record(
                    event="telemetry_reject", device_id=body.device_id, outcome="fail",
                    detail="unknown device",
                )
                raise HTTPException(status_code=401, detail="unknown device")
            if device.status == "revoked":
                app.state.audit.record(
                    event="telemetry_reject", device_id=body.device_id, outcome="fail",
                    detail="device revoked",
                )
                raise HTTPException(status_code=401, detail="device revoked")

        if app.state.insecure_mode:
            # No cryptographic verification at all in insecure mode: decode
            # whatever claims are present (if the token even parses) purely
            # for storage, without trusting them for anything security-relevant.
            try:
                payload = pyjwt.decode(body.jws, options={"verify_signature": False})
            except pyjwt.PyJWTError:
                payload = {"raw": body.jws}
            store.insert_telemetry(body.device_id, json.dumps(payload, ensure_ascii=False), verified=False)
            app.state.audit.record(
                event="telemetry_accept", device_id=body.device_id, outcome="success",
                detail="insecure mode: signature not verified",
            )
            return schemas.TelemetryResponse(status="accepted")

        assert device is not None
        if not device.cert_pem:
            app.state.audit.record(
                event="telemetry_reject", device_id=body.device_id, outcome="fail",
                detail="device has no registered certificate",
            )
            raise HTTPException(status_code=401, detail="device has no registered certificate")

        try:
            device_public_pem = _device_public_key_pem(device.cert_pem)
            payload = verify_payload(body.jws, device_public_pem)
        except JWSVerificationError as exc:
            app.state.audit.record(
                event="telemetry_reject", device_id=body.device_id, outcome="fail",
                detail=f"JWS verification failed: {exc}",
            )
            raise HTTPException(status_code=401, detail="JWS verification failed") from exc

        # -- replay/freshness enforcement --------------------------------
        # A valid signature proves integrity+origin but NOT freshness: an
        # attacker who captures a legitimately signed telemetry message can
        # resubmit it verbatim (the signature stays valid). We require every
        # telemetry JWS to carry a unique nonce (``jti``) and an issue time
        # (``iat``), accept it only if ``iat`` is within the configured window,
        # and reject any ``jti`` already accepted for this device.
        jti = payload.get("jti")
        iat = payload.get("iat")
        if not isinstance(jti, str) or not jti or not isinstance(iat, (int, float)):
            app.state.audit.record(
                event="telemetry_reject", device_id=body.device_id, outcome="fail",
                detail="missing replay-protection claims (jti/iat)",
            )
            raise HTTPException(
                status_code=401, detail="telemetry missing replay-protection claims (jti/iat)"
            )
        now = time.time()
        window = app.state.cfg.telemetry_replay_window_seconds
        # Small forward tolerance for clock skew; ``window`` bounds how stale
        # (or how long buffered) an accepted message may be.
        if iat > now + 60 or iat < now - window:
            app.state.audit.record(
                event="telemetry_reject", device_id=body.device_id, outcome="fail",
                detail=f"stale telemetry (iat outside {window}s freshness window)",
            )
            raise HTTPException(status_code=401, detail="stale telemetry (iat outside freshness window)")
        if not store.record_jti(body.device_id, jti, exp_epoch=iat + window, now_epoch=now):
            app.state.audit.record(
                event="telemetry_reject", device_id=body.device_id, outcome="fail",
                detail=f"replay detected (duplicate jti={jti})",
            )
            raise HTTPException(status_code=409, detail="replay detected (duplicate jti)")

        store.insert_telemetry(body.device_id, json.dumps(payload, ensure_ascii=False), verified=True)
        store.touch_last_seen(body.device_id)
        app.state.audit.record(
            event="telemetry_accept", device_id=body.device_id, outcome="success", detail="",
        )
        return schemas.TelemetryResponse(status="accepted")

    @router.get("/telemetry", response_model=list[schemas.TelemetryOut])
    async def list_telemetry(
        device_id: Optional[str] = None,
        limit: int = 100,
        identity: schemas.Identity = Depends(authenticated),
    ) -> list[schemas.TelemetryOut]:
        """Read stored telemetry (RBAC: operator, admin).

        Closes the platform-observability gap: the data a device actually
        delivered — and whether its JWS was cryptographically verified — is
        inspectable in-platform (Swagger UI / API), not only in the DB.
        """
        out: list[schemas.TelemetryOut] = []
        for r in store.list_telemetry(device_id=device_id, limit=limit):
            try:
                payload = json.loads(r.payload_json)
            except json.JSONDecodeError:
                payload = {"raw": r.payload_json}
            out.append(
                schemas.TelemetryOut(
                    id=r.id, device_id=r.device_id, ts=r.ts,
                    verified=r.verified, payload=payload,
                )
            )
        return out

    @router.get("/devices", response_model=list[schemas.DeviceOut])
    async def list_devices(
        identity: schemas.Identity = Depends(authenticated),
    ) -> list[schemas.DeviceOut]:
        return [
            schemas.DeviceOut(
                device_id=d.device_id, site=d.site, group=d.group_name, status=d.status,
                cert_serial=d.cert_serial, registered_at=d.registered_at, last_seen=d.last_seen,
            )
            for d in store.list_devices()
        ]

    @router.post("/devices/{device_id}/approve", response_model=schemas.ApproveRevokeResponse)
    async def approve_device(
        device_id: str, identity: schemas.Identity = Depends(authenticated),
    ) -> schemas.ApproveRevokeResponse:
        device = store.get_device(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="device not found")
        if device.status == "revoked":
            raise HTTPException(status_code=409, detail="device is revoked, cannot approve")
        if device.status == "approved":
            raise HTTPException(status_code=409, detail="device already approved")
        if not device.csr_pem:
            raise HTTPException(status_code=409, detail="device has no CSR on file")

        csr_pem_bytes = device.csr_pem.encode()
        _validate_csr_matches_device(csr_pem_bytes, device_id, event="approve")

        cert_pem_bytes = ca.sign_csr(csr_pem_bytes)
        cert_serial = pki.cert_serial(cert_pem_bytes)
        store.approve_device(device_id, cert_serial, cert_pem_bytes.decode())
        app.state.audit.record(
            event="approve", device_id=device_id, outcome="success",
            detail=f"cert_serial={cert_serial}",
        )
        return schemas.ApproveRevokeResponse(device_id=device_id, status="approved")

    @router.post("/devices/{device_id}/revoke", response_model=schemas.ApproveRevokeResponse)
    async def revoke_device(
        device_id: str, identity: schemas.Identity = Depends(authenticated),
    ) -> schemas.ApproveRevokeResponse:
        device = store.get_device(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="device not found")

        if device.cert_serial is not None:
            ca.revoke(device.cert_serial)
        store.revoke_device(device_id)
        app.state.audit.record(event="revoke", device_id=device_id, outcome="success", detail="")
        return schemas.ApproveRevokeResponse(device_id=device_id, status="revoked")

    @router.get("/audit", response_model=list[schemas.AuditRecordOut])
    async def get_audit(
        device_id: Optional[str] = None,
        event: Optional[str] = None,
        limit: int = 100,
        identity: schemas.Identity = Depends(authenticated),
    ) -> list[schemas.AuditRecordOut]:
        rows = audit.query(device_id=device_id, event=event, limit=limit)
        return [
            schemas.AuditRecordOut(
                id=r.id, ts=r.ts, event=r.event, device_id=r.device_id,
                outcome=r.outcome, detail=r.detail,
            )
            for r in rows
        ]

    app.include_router(router)

    # Root-level alias for k8s-style liveness probes that don't know the API prefix.
    @app.get("/healthz", response_model=schemas.HealthResponse)
    async def healthz_root() -> schemas.HealthResponse:
        return schemas.HealthResponse()

    return app
