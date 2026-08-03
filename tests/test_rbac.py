"""Tests for eam.manager.rbac: RBAC authorization matrix."""

from __future__ import annotations

from eam.manager import rbac


def test_device_role_allowed_on_telemetry():
    assert rbac.authorize("device", "POST", "/api/v1/telemetry") is True
    assert rbac.authorize("admin", "POST", "/api/v1/telemetry") is True


def test_operator_denied_on_telemetry():
    assert rbac.authorize("operator", "POST", "/api/v1/telemetry") is False


def test_operator_and_admin_allowed_on_devices_list():
    assert rbac.authorize("operator", "GET", "/api/v1/devices") is True
    assert rbac.authorize("admin", "GET", "/api/v1/devices") is True


def test_device_denied_on_devices_list():
    assert rbac.authorize("device", "GET", "/api/v1/devices") is False


def test_only_admin_allowed_on_approve():
    assert rbac.authorize("admin", "POST", "/api/v1/devices/dev-1/approve") is True
    assert rbac.authorize("operator", "POST", "/api/v1/devices/dev-1/approve") is False
    assert rbac.authorize("device", "POST", "/api/v1/devices/dev-1/approve") is False


def test_only_admin_allowed_on_revoke():
    assert rbac.authorize("admin", "POST", "/api/v1/devices/dev-1/revoke") is True
    assert rbac.authorize("operator", "POST", "/api/v1/devices/dev-1/revoke") is False
    assert rbac.authorize("device", "POST", "/api/v1/devices/dev-1/revoke") is False


def test_only_admin_allowed_on_audit():
    assert rbac.authorize("admin", "GET", "/api/v1/audit") is True
    assert rbac.authorize("operator", "GET", "/api/v1/audit") is False
    assert rbac.authorize("device", "GET", "/api/v1/audit") is False


def test_unmatched_paths_are_open_by_default():
    # Endpoints absent from the matrix (register/auth/healthz) apply their
    # own business-logic checks rather than RBAC, so authorize() allows them.
    assert rbac.authorize("device", "POST", "/api/v1/devices/register") is True
    assert rbac.authorize("anything", "POST", "/api/v1/auth/token") is True
    assert rbac.authorize("anything", "GET", "/api/v1/healthz") is True


def test_device_id_segment_normalizes_regardless_of_value():
    assert rbac.authorize("admin", "POST", "/api/v1/devices/site-01_dev.42/approve") is True
    assert rbac.authorize("device", "POST", "/api/v1/devices/site-01_dev.42/approve") is False


def test_method_mismatch_is_not_confused_with_allowed_route():
    # GET /api/v1/telemetry is not in the matrix at all -> defaults open,
    # but POST /api/v1/telemetry with a non-device/admin role must be denied.
    assert rbac.authorize("operator", "GET", "/api/v1/telemetry") is True
    assert rbac.authorize("operator", "POST", "/api/v1/telemetry") is False
