"""Pydantic request/response models for the Manager API."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class RegisterRequest(BaseModel):
    device_id: str
    site: str
    group: str
    csr_pem: str
    bootstrap_token: str


class RegisterResponse(BaseModel):
    device_id: str
    status: str
    cert_pem: Optional[str] = None


class TokenRequest(BaseModel):
    cert_pem: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    expires_in: int


class OperatorAuthRequest(BaseModel):
    username: str
    password: str


class TelemetryRequest(BaseModel):
    device_id: str
    jws: str


class TelemetryResponse(BaseModel):
    status: str


class DeviceOut(BaseModel):
    device_id: str
    site: str
    group: str
    status: str
    cert_serial: Optional[int] = None
    registered_at: str
    last_seen: Optional[str] = None


class ApproveRevokeResponse(BaseModel):
    device_id: str
    status: str


class AuditRecordOut(BaseModel):
    id: int
    ts: str
    event: str
    device_id: Optional[str] = None
    outcome: str
    detail: str


class HealthResponse(BaseModel):
    status: str = "ok"


class Identity(BaseModel):
    """Resolved caller identity from a verified bearer JWT (or insecure-mode bypass)."""

    sub: str
    role: str
