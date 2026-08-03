"""Manager-side CA wrapper around ``eam.common.pki``.

On first start (when ``CERTS_DIR`` has no CA material yet) the Manager
auto-generates a root CA and persists it under ``CERTS_DIR``; on subsequent
starts the existing CA is loaded so previously issued device certificates
remain valid.

Revocation is tracked two ways, per the task brief ("serial 목록 파일+DB"):

* a file-backed serial list under ``CERTS_DIR`` (``revoked_serials.txt``,
  authoritative for the CA/PKI layer and durable across restarts), and
* the device's ``status`` column in the SQLite device store (authoritative
  for "is this device allowed to operate at all", checked by the Manager
  API alongside the serial list for defense in depth).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Set, Union

from eam.common import pki

PathLike = Union[str, "os.PathLike[str]"]

CA_CERT_FILENAME = "ca.pem"
CA_KEY_FILENAME = "ca-key.pem"
REVOKED_SERIALS_FILENAME = "revoked_serials.txt"
CA_COMMON_NAME = "EAM Root CA"


class ManagerCA:
    """Owns the Manager's CA key pair and revocation list for a CERTS_DIR."""

    def __init__(self, certs_dir: PathLike):
        self.certs_dir = Path(certs_dir)
        self.certs_dir.mkdir(parents=True, exist_ok=True)

        self._cert_path = self.certs_dir / CA_CERT_FILENAME
        self._key_path = self.certs_dir / CA_KEY_FILENAME
        self._revoked_path = self.certs_dir / REVOKED_SERIALS_FILENAME
        self._lock = threading.Lock()

        if self._cert_path.exists() and self._key_path.exists():
            self.ca_cert_pem = pki.load_pem(self._cert_path)
            self.ca_key_pem = pki.load_pem(self._key_path)
        else:
            self.ca_cert_pem, self.ca_key_pem = pki.create_ca(CA_COMMON_NAME)
            pki.save_pem(self._cert_path, self.ca_cert_pem)
            pki.save_pem(self._key_path, self.ca_key_pem)

        self._revoked: Set[int] = set()
        if self._revoked_path.exists():
            for line in self._revoked_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._revoked.add(int(line))

    def sign_csr(self, csr_pem: bytes, days: int = 365) -> bytes:
        """Sign a device CSR with the Manager's CA, returning the leaf cert PEM."""
        return pki.sign_csr(self.ca_cert_pem, self.ca_key_pem, csr_pem, days=days)

    def verify_chain(self, cert_pem: bytes) -> bool:
        """True if ``cert_pem`` was issued by this CA and is currently valid."""
        return pki.verify_chain(self.ca_cert_pem, cert_pem)

    def is_revoked(self, serial: int) -> bool:
        with self._lock:
            return serial in self._revoked

    def revoke(self, serial: int) -> None:
        """Add ``serial`` to the revocation list (idempotent, durable)."""
        with self._lock:
            if serial in self._revoked:
                return
            self._revoked.add(serial)
            with open(self._revoked_path, "a", encoding="utf-8") as f:
                f.write(f"{serial}\n")
