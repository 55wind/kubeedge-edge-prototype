"""Role-based access control matrix for the Manager API.

The matrix maps ``(HTTP method, path template)`` to the set of roles allowed
to call it. Paths carrying a ``device_id`` path parameter are matched via
``_normalize_path``, which collapses the variable segment back to the
literal placeholder ``{id}`` used as the matrix key.

Endpoints not present in the matrix (``/devices/register``, ``/auth/token``,
``/auth/operator``, ``/healthz``) are intentionally open by default here:
they are either unauthenticated by design or gate access with their own
business-logic checks (bootstrap token, certificate chain, operator
credentials) rather than a bearer-token role.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Tuple

RBAC_MATRIX: Dict[Tuple[str, str], FrozenSet[str]] = {
    ("POST", "/api/v1/telemetry"): frozenset({"device", "admin"}),
    ("GET", "/api/v1/devices"): frozenset({"operator", "admin"}),
    ("POST", "/api/v1/devices/{id}/approve"): frozenset({"admin"}),
    ("POST", "/api/v1/devices/{id}/revoke"): frozenset({"admin"}),
    ("GET", "/api/v1/audit"): frozenset({"admin"}),
}


def _normalize_path(path: str) -> str:
    """Collapse ``/api/v1/devices/{device_id}/<action>`` to the ``{id}`` template.

    e.g. ``/api/v1/devices/dev-1/approve`` -> ``/api/v1/devices/{id}/approve``.
    Paths that don't match this shape (e.g. ``/api/v1/devices`` with no id
    segment) are returned unchanged.
    """
    parts = path.split("/")
    if (
        len(parts) >= 6
        and parts[1:4] == ["api", "v1", "devices"]
    ):
        parts[4] = "{id}"
    return "/".join(parts)


def authorize(role: str, method: str, path: str) -> bool:
    """Return True if ``role`` may call ``method path`` per the RBAC matrix.

    Paths absent from the matrix default to allowed (see module docstring).
    """
    key = (method.upper(), _normalize_path(path))
    allowed_roles = RBAC_MATRIX.get(key)
    if allowed_roles is None:
        return True
    return role in allowed_roles
