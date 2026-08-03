"""Tests for eam.common.pki: CA/CSR issuance, chain verification, SAN roundtrip."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from eam.common import pki


def test_ca_creation_is_self_signed_rsa2048():
    cert_pem, key_pem = pki.create_ca("EAM Root CA")
    cert = x509.load_pem_x509_certificate(cert_pem)
    key = serialization.load_pem_private_key(key_pem, password=None)

    assert isinstance(key, rsa.RSAPrivateKey)
    assert key.key_size == 2048
    # Self-signed: issuer == subject.
    assert cert.issuer == cert.subject
    basic_constraints = cert.extensions.get_extension_for_class(
        x509.BasicConstraints
    ).value
    assert basic_constraints.ca is True

    validity_days = (cert.not_valid_after_utc - cert.not_valid_before_utc).days
    assert validity_days >= 3650  # ~10 years


def test_csr_has_spiffe_san_uri():
    csr_pem, key_pem = pki.create_csr("dev-001")
    csr = x509.load_pem_x509_csr(csr_pem)
    key = serialization.load_pem_private_key(key_pem, password=None)

    assert isinstance(key, rsa.RSAPrivateKey)
    assert key.key_size == 2048
    assert csr.is_signature_valid

    san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    uris = san.get_values_for_type(x509.UniformResourceIdentifier)
    assert uris == ["spiffe://sangmyung/eam/dev-001"]


def test_full_roundtrip_ca_csr_sign_verify_device_id():
    ca_cert_pem, ca_key_pem = pki.create_ca("EAM Root CA")
    csr_pem, _dev_key_pem = pki.create_csr("device-42")

    leaf_cert_pem = pki.sign_csr(ca_cert_pem, ca_key_pem, csr_pem, days=365)

    assert pki.verify_chain(ca_cert_pem, leaf_cert_pem) is True
    assert pki.device_id_from_cert(leaf_cert_pem) == "device-42"

    serial = pki.cert_serial(leaf_cert_pem)
    assert isinstance(serial, int)
    leaf_cert = x509.load_pem_x509_certificate(leaf_cert_pem)
    assert leaf_cert.serial_number == serial

    # Leaf must not itself be a CA, and validity should be ~1 year.
    basic_constraints = leaf_cert.extensions.get_extension_for_class(
        x509.BasicConstraints
    ).value
    assert basic_constraints.ca is False
    validity_days = (leaf_cert.not_valid_after_utc - leaf_cert.not_valid_before_utc).days
    assert 360 <= validity_days <= 370


def test_sign_csr_custom_days():
    ca_cert_pem, ca_key_pem = pki.create_ca("EAM Root CA")
    csr_pem, _ = pki.create_csr("device-short-lived")

    leaf_cert_pem = pki.sign_csr(ca_cert_pem, ca_key_pem, csr_pem, days=30)
    leaf_cert = x509.load_pem_x509_certificate(leaf_cert_pem)
    validity_days = (leaf_cert.not_valid_after_utc - leaf_cert.not_valid_before_utc).days
    assert 25 <= validity_days <= 31


def test_verify_chain_rejects_cert_signed_by_different_ca():
    ca1_cert_pem, ca1_key_pem = pki.create_ca("EAM Root CA 1")
    ca2_cert_pem, _ca2_key_pem = pki.create_ca("EAM Root CA 2")

    csr_pem, _ = pki.create_csr("device-x")
    leaf_cert_pem = pki.sign_csr(ca1_cert_pem, ca1_key_pem, csr_pem, days=365)

    # Signed by CA1, but verified against CA2 -> must be rejected.
    assert pki.verify_chain(ca2_cert_pem, leaf_cert_pem) is False
    # Verified against the correct CA still succeeds.
    assert pki.verify_chain(ca1_cert_pem, leaf_cert_pem) is True


def test_verify_chain_rejects_expired_certificate():
    ca_cert_pem, ca_key_pem = pki.create_ca("EAM Root CA")
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)

    csr_pem, _ = pki.create_csr("device-expired")
    csr = x509.load_pem_x509_csr(csr_pem)

    now = datetime.now(timezone.utc)
    expired_cert = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca_cert.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=10))
        .not_valid_after(now - timedelta(days=1))
        .sign(ca_key, hashes.SHA256())
    )
    expired_cert_pem = expired_cert.public_bytes(serialization.Encoding.PEM)

    assert pki.verify_chain(ca_cert_pem, expired_cert_pem) is False


def test_device_id_from_cert_returns_none_when_no_san():
    ca_cert_pem, _ca_key_pem = pki.create_ca("EAM Root CA")
    # A CA cert has no SPIFFE SAN, so extraction should return None.
    assert pki.device_id_from_cert(ca_cert_pem) is None


def test_save_and_load_pem_roundtrip(tmp_path):
    cert_pem, key_pem = pki.create_ca("EAM Root CA")
    cert_path = tmp_path / "certs" / "ca.pem"
    key_path = tmp_path / "certs" / "ca-key.pem"

    pki.save_pem(cert_path, cert_pem)
    pki.save_pem(key_path, key_pem)

    assert cert_path.exists()
    assert key_path.exists()
    assert pki.load_pem(cert_path) == cert_pem
    assert pki.load_pem(key_path) == key_pem
