"""Environment-variable based configuration loader for the eam security core.

Reads all values from ``os.environ`` at call time (no caching) so tests and
callers can freely monkeypatch the environment and observe fresh values.

Environment variables:
    CERTS_DIR     - directory holding CA/leaf certificates and keys (default: "certs")
    DB_URL        - filesystem path used for sqlite3 databases (default: "eam.db")
    JWT_ISS       - JWT issuer claim (default: "edge-auth-manager")
    JWT_AUD       - JWT audience claim (default: "edge-agents")
    JWT_TTL       - JWT time-to-live in seconds (default: 900)
    AUTO_APPROVE  - if true, device registration is auto-approved (default: false)
    INSECURE_MODE - if true, auth/authorization checks are bypassed for demos (default: false)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Fixed security-relevant defaults (Global Constraints #6, #7 of the project plan).
DEFAULT_JWT_ISSUER = "edge-auth-manager"
DEFAULT_JWT_AUDIENCE = "edge-agents"
DEFAULT_JWT_TTL_SECONDS = 900
JWT_ALGORITHM = "RS256"


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean-ish environment variable, e.g. "true"/"1"/"yes"."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class JWTConfig:
    """JWT issuance/verification parameters (RS256, Global Constraint #7)."""

    issuer: str
    audience: str
    ttl_seconds: int
    algorithm: str = JWT_ALGORITHM


@dataclass(frozen=True)
class EAMConfig:
    """Snapshot of eam security-core configuration read from the environment."""

    certs_dir: Path
    db_url: str
    jwt: JWTConfig
    auto_approve: bool
    insecure_mode: bool


def load_config() -> EAMConfig:
    """Load configuration fresh from the current process environment."""
    certs_dir = Path(os.environ.get("CERTS_DIR", "certs"))
    db_url = os.environ.get("DB_URL", "eam.db")
    jwt_cfg = JWTConfig(
        issuer=os.environ.get("JWT_ISS", DEFAULT_JWT_ISSUER),
        audience=os.environ.get("JWT_AUD", DEFAULT_JWT_AUDIENCE),
        ttl_seconds=int(os.environ.get("JWT_TTL", str(DEFAULT_JWT_TTL_SECONDS))),
    )
    return EAMConfig(
        certs_dir=certs_dir,
        db_url=db_url,
        jwt=jwt_cfg,
        auto_approve=_env_bool("AUTO_APPROVE", False),
        insecure_mode=_env_bool("INSECURE_MODE", False),
    )
