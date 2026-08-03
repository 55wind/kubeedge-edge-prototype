"""Compact JWS payload signing/verification (RS256) built on PyJWT.

This is used to sign arbitrary application payloads (e.g. telemetry) with a
device's private key so a receiver holding the matching public key (from the
device's certificate) can verify integrity and origin. This is distinct from
the manager-issued bearer JWT used for API authentication.
"""

from __future__ import annotations

from typing import Any, Dict, Union

import jwt

JWS_ALGORITHM = "RS256"

PemKey = Union[str, bytes]


class JWSVerificationError(Exception):
    """Raised when a JWS token fails signature verification or is malformed."""


def sign_payload(payload: Dict[str, Any], private_key_pem: PemKey) -> str:
    """Sign ``payload`` as a compact RS256 JWS using ``private_key_pem``."""
    return jwt.encode(payload, private_key_pem, algorithm=JWS_ALGORITHM)


def verify_payload(token: str, public_key_pem: PemKey) -> Dict[str, Any]:
    """Verify a compact RS256 JWS and return its decoded payload.

    Raises:
        JWSVerificationError: if the signature is invalid, the token is
            malformed, or any registered claim (e.g. exp) fails validation.
    """
    try:
        return jwt.decode(token, public_key_pem, algorithms=[JWS_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise JWSVerificationError(str(exc)) from exc
