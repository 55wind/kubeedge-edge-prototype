"""X.509 PKI helpers: CA creation, device CSR/cert issuance, chain verification.

All keys are RSA 2048. The CA certificate validity is 10 years; leaf (device)
certificates default to 1 year (365 days). Device certificates carry the
device identity in the Subject Alternative Name as a SPIFFE-style URI:
``spiffe://sangmyung/eam/{device_id}``.

All PEM values (certs, CSRs, keys) are represented as ``bytes``.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple, Union

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

RSA_KEY_SIZE = 2048
RSA_PUBLIC_EXPONENT = 65537
CA_VALIDITY_DAYS = 365 * 10
DEFAULT_LEAF_VALIDITY_DAYS = 365
SPIFFE_TRUST_DOMAIN = "sangmyung"
SPIFFE_URI_PREFIX = f"spiffe://{SPIFFE_TRUST_DOMAIN}/eam/"

# Clock skew tolerance so certificates are valid immediately after issuance
# even if the verifying machine's clock is slightly behind the issuer's.
_NOT_BEFORE_SKEW = timedelta(minutes=5)

PathLike = Union[str, "os.PathLike[str]"]


def _generate_rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(
        public_exponent=RSA_PUBLIC_EXPONENT, key_size=RSA_KEY_SIZE
    )


def _key_to_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _spiffe_uri(device_id: str) -> str:
    return f"{SPIFFE_URI_PREFIX}{device_id}"


def create_ca(cn: str) -> Tuple[bytes, bytes]:
    """Create a self-signed CA certificate and its RSA-2048 private key.

    Returns:
        (cert_pem, key_pem)
    """
    key = _generate_rsa_key()
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(timezone.utc)

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _NOT_BEFORE_SKEW)
        .not_valid_after(now + timedelta(days=CA_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
    )
    cert = builder.sign(key, hashes.SHA256())
    return cert.public_bytes(serialization.Encoding.PEM), _key_to_pem(key)


def create_csr(device_id: str) -> Tuple[bytes, bytes]:
    """Create a CSR + fresh RSA-2048 key pair for a device.

    The CSR's Subject Alternative Name carries the SPIFFE URI
    ``spiffe://sangmyung/eam/{device_id}``.

    Returns:
        (csr_pem, key_pem)
    """
    key = _generate_rsa_key()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, device_id)])
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(subject)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier(_spiffe_uri(device_id))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM), _key_to_pem(key)


def sign_csr(
    ca_cert_pem: bytes, ca_key_pem: bytes, csr_pem: bytes, days: int = DEFAULT_LEAF_VALIDITY_DAYS
) -> bytes:
    """Sign a CSR with the CA key/cert, producing a leaf certificate.

    The CSR signature is verified before issuance, and the CSR's Subject
    Alternative Name (device SPIFFE URI) is carried over onto the issued
    certificate unchanged.

    Returns:
        cert_pem
    """
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)
    csr = x509.load_pem_x509_csr(csr_pem)

    if not csr.is_signature_valid:
        raise ValueError("CSR signature verification failed")

    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca_cert.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _NOT_BEFORE_SKEW)
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )

    try:
        san_ext = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        builder = builder.add_extension(san_ext.value, critical=san_ext.critical)
    except x509.ExtensionNotFound:
        pass

    cert = builder.sign(ca_key, hashes.SHA256())
    return cert.public_bytes(serialization.Encoding.PEM)


def verify_chain(ca_cert_pem: bytes, cert_pem: bytes) -> bool:
    """Verify that ``cert_pem`` was issued by the CA in ``ca_cert_pem``.

    Checks the CA's cryptographic signature over the certificate, that the
    certificate's issuer matches the CA's subject, and that the certificate
    is currently within its validity period. Returns False (never raises)
    for any structurally invalid input or verification failure.
    """
    try:
        ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
        cert = x509.load_pem_x509_certificate(cert_pem)

        if cert.issuer != ca_cert.subject:
            return False

        ca_public_key = ca_cert.public_key()
        if not isinstance(ca_public_key, rsa.RSAPublicKey):
            return False

        ca_public_key.verify(
            cert.signature,
            cert.tbs_certificate_bytes,
            padding.PKCS1v15(),
            cert.signature_hash_algorithm,
        )

        now = datetime.now(timezone.utc)
        if now < cert.not_valid_before_utc or now > cert.not_valid_after_utc:
            return False

        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def cert_serial(cert_pem: bytes) -> int:
    """Return the certificate's serial number."""
    return x509.load_pem_x509_certificate(cert_pem).serial_number


def device_id_from_cert(cert_pem: bytes) -> Optional[str]:
    """Extract the device_id from a certificate's SPIFFE SAN URI, if present."""
    cert = x509.load_pem_x509_certificate(cert_pem)
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None

    for uri in san.get_values_for_type(x509.UniformResourceIdentifier):
        if uri.startswith(SPIFFE_URI_PREFIX):
            return uri[len(SPIFFE_URI_PREFIX):]
    return None


def device_id_from_csr(csr_pem: bytes) -> Optional[str]:
    """Extract the device_id from a CSR's SPIFFE SAN URI, if present.

    Mirrors :func:`device_id_from_cert` but reads the identity a device is
    *requesting* (from its CSR) rather than the identity a CA already issued.
    Callers that trust a caller-supplied ``device_id`` alongside a CSR should
    cross-check it against this function's result before signing, so a CSR
    cannot smuggle in a different identity than the one it was registered
    under. Raises ``ValueError`` if ``csr_pem`` is not a well-formed CSR.
    """
    csr = x509.load_pem_x509_csr(csr_pem)
    try:
        san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None

    for uri in san.get_values_for_type(x509.UniformResourceIdentifier):
        if uri.startswith(SPIFFE_URI_PREFIX):
            return uri[len(SPIFFE_URI_PREFIX):]
    return None


def save_pem(path: PathLike, pem_data: bytes) -> None:
    """Write PEM bytes to ``path``, creating parent directories as needed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(pem_data)


def load_pem(path: PathLike) -> bytes:
    """Read PEM bytes from ``path``."""
    return Path(path).read_bytes()
