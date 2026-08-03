"""Tests for eam.common.jws: compact RS256 JWS sign/verify and forgery rejection."""

from __future__ import annotations

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from eam.common import pki
from eam.common.jws import JWSVerificationError, sign_payload, verify_payload


def _rsa_keypair_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def test_sign_and_verify_roundtrip():
    private_pem, public_pem = _rsa_keypair_pem()
    payload = {"device_id": "dev-001", "temperature": 21.5, "seq": 7}

    token = sign_payload(payload, private_pem)
    assert isinstance(token, str)
    assert token.count(".") == 2  # compact JWS: header.payload.signature

    decoded = verify_payload(token, public_pem)
    assert decoded == payload


def test_verify_uses_device_certificate_public_key():
    # Device signs telemetry with its own private key; a verifier holding
    # only the issued certificate should be able to extract the public key
    # and verify the payload.
    ca_cert_pem, ca_key_pem = pki.create_ca("EAM Root CA")
    csr_pem, dev_key_pem = pki.create_csr("dev-jws")
    cert_pem = pki.sign_csr(ca_cert_pem, ca_key_pem, csr_pem)

    cert = x509.load_pem_x509_certificate(cert_pem)
    public_pem = cert.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    payload = {"device_id": "dev-jws", "reading": 1}
    token = sign_payload(payload, dev_key_pem)
    assert verify_payload(token, public_pem) == payload


def test_verify_rejects_tampered_signature():
    private_pem, public_pem = _rsa_keypair_pem()
    token = sign_payload({"a": 1}, private_pem)

    header, payload_seg, signature = token.split(".")
    tampered = f"{header}.{payload_seg}.{signature[:-2]}zz"

    with pytest.raises(JWSVerificationError):
        verify_payload(tampered, public_pem)


def test_verify_rejects_signature_from_wrong_key():
    private_pem, _public_pem = _rsa_keypair_pem()
    _other_private_pem, other_public_pem = _rsa_keypair_pem()

    token = sign_payload({"a": 1}, private_pem)

    with pytest.raises(JWSVerificationError):
        verify_payload(token, other_public_pem)


def test_verify_rejects_malformed_token():
    _private_pem, public_pem = _rsa_keypair_pem()

    with pytest.raises(JWSVerificationError):
        verify_payload("not-a-valid-jws-token", public_pem)


def test_verify_rejects_alg_none_forgery():
    # Classic JWT "alg: none" forgery attempt must not be accepted even
    # though it carries no signature at all.
    forged = jwt.api_jws.encode(
        b'{"a":1}', key=None, algorithm="none", headers={"alg": "none"}
    )
    _private_pem, public_pem = _rsa_keypair_pem()

    with pytest.raises(JWSVerificationError):
        verify_payload(forged, public_pem)
