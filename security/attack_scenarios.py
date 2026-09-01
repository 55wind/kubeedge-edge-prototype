"""시나리오 기반 공격 테스트 (red-team) — 실제 Manager 인스턴스 대상.

이 스크립트는 모의(mock)가 아니라 진짜 Manager FastAPI 앱을 secure 모드로
띄우고(httpx ASGITransport, in-process), 아래 공격 시나리오를 실제 HTTP
요청으로 실행한다. 각 시나리오는

  1) 플랫폼이 공격을 올바르게 차단하는가 (상태코드/응답),
  2) 그 시도가 감사(audit) 로그에 기록되는가 (AAA의 Accounting, 수행항목 3)

두 가지를 함께 검증한다. 마지막에 표 형태로 결과를 출력한다.

허용 패키지만 사용: fastapi/httpx/cryptography/PyJWT (전부 기존 의존성).
실행: python security/attack_scenarios.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import rsa

from eam.common import pki
from eam.common.jws import sign_payload
from eam.manager.app import create_app

# ---------------------------------------------------------------------------
# 고정 데모 파라미터 (재현성 위해 환경변수로 주입)
# ---------------------------------------------------------------------------
BOOTSTRAP_TOKEN = "redteam-bootstrap-token"
ADMIN_USER, ADMIN_PASS = "admin", "redteam-admin-pass"
OPERATOR_USER, OPERATOR_PASS = "operator", "redteam-operator-pass"
JWT_ISS = "edge-auth-manager"
JWT_AUD = "edge-agents"


@dataclass
class Result:
    idx: int
    name: str
    target: str            # 노린 자산/취약점
    expectation: str       # 기대되는 방어 동작
    observed: str          # 실제 관찰
    blocked: bool          # 공격이 차단되었는가
    audited: bool          # 감사 로그에 남았는가
    note: str = ""


class RedTeam:
    def __init__(self, base_url: str = "http://manager") -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="eam-redteam-"))
        # secure 모드 (INSECURE_MODE 미설정), 자동승인으로 등록 단순화
        import os
        os.environ.update(
            BOOTSTRAP_TOKEN=BOOTSTRAP_TOKEN,
            EAM_ADMIN_USERNAME=ADMIN_USER, EAM_ADMIN_PASSWORD=ADMIN_PASS,
            EAM_OPERATOR_USERNAME=OPERATOR_USER, EAM_OPERATOR_PASSWORD=OPERATOR_PASS,
            AUTO_APPROVE="true", JWT_ISS=JWT_ISS, JWT_AUD=JWT_AUD,
        )
        os.environ.pop("INSECURE_MODE", None)
        self.app = create_app(
            certs_dir=self.tmp / "certs",
            store_db_path=self.tmp / "m.db",
            audit_db_path=self.tmp / "a.db",
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url=base_url
        )
        self.results: list[Result] = []
        self._n = 0

    async def aclose(self) -> None:
        await self.client.aclose()

    # -- 유틸: 정상 자산 준비 -------------------------------------------------

    async def enroll(self, device_id: str) -> tuple[str, bytes, str]:
        """정상 등록 → (cert_pem, key_pem, device_id) 반환."""
        csr_pem, key_pem = pki.create_csr(device_id)
        r = await self.client.post("/api/v1/devices/register", json={
            "device_id": device_id, "site": "factory-A", "group": "sensor",
            "csr_pem": csr_pem.decode(), "bootstrap_token": BOOTSTRAP_TOKEN,
        })
        r.raise_for_status()
        return r.json()["cert_pem"], key_pem, device_id

    async def device_token(self, cert_pem: str) -> str:
        r = await self.client.post("/api/v1/auth/token", json={"cert_pem": cert_pem})
        r.raise_for_status()
        return r.json()["access_token"]

    async def operator_token(self, admin: bool = False) -> str:
        u, p = (ADMIN_USER, ADMIN_PASS) if admin else (OPERATOR_USER, OPERATOR_PASS)
        r = await self.client.post("/api/v1/auth/operator", json={"username": u, "password": p})
        r.raise_for_status()
        return r.json()["access_token"]

    async def audit_has(self, *, event: Optional[str] = None, detail_contains: str = "",
                        outcome: Optional[str] = None, device_id: Optional[str] = None) -> bool:
        """감사 DB를 직접 조회해 조건에 맞는 행이 있는지 확인 (admin 우회 아님)."""
        rows = self.app.state.audit.query(device_id=device_id, event=event, limit=500)
        for row in rows:
            if outcome and row.outcome != outcome:
                continue
            if detail_contains and detail_contains not in (row.detail or ""):
                continue
            return True
        return False

    def record(self, name, target, expectation, observed, blocked, audited, note=""):
        self._n += 1
        self.results.append(Result(self._n, name, target, expectation, observed,
                                   blocked, audited, note))
        flag = "차단" if blocked else "!! 통과됨 !!"
        astr = "기록됨" if audited else "미기록"
        print(f"[{self._n:02d}] {name}\n     → {observed}  [{flag} / 감사 {astr}]")


# ===========================================================================
# 공격 시나리오
# ===========================================================================


async def run() -> RedTeam:
    rt = RedTeam()

    # 정상 자산: 두 개의 합법 디바이스와 오퍼레이터/관리자
    certA, keyA, idA = await rt.enroll("dev-alpha")
    certB, keyB, idB = await rt.enroll("dev-bravo")
    tokenA = await rt.device_token(certA)      # role=device, sub=dev-alpha
    op_token = await rt.operator_token()
    admin_token = await rt.operator_token(admin=True)

    # --- S1. 인증 없이 보호 엔드포인트 접근 --------------------------------
    hits = []
    for method, path in [("GET", "/api/v1/devices"), ("GET", "/api/v1/audit"),
                         ("POST", "/api/v1/devices/dev-alpha/revoke")]:
        r = await rt.client.request(method, path)
        hits.append(r.status_code)
    blocked = all(c == 401 for c in hits)
    rt.record(
        "S1 무인증 접근 (Broken Auth)",
        "관리자용 API (/devices, /audit, revoke)",
        "베어러 토큰 없으면 401",
        f"상태코드={hits}",
        blocked,
        await rt.audit_has(event="http", detail_contains="401"),
    )

    # --- S2. JWT alg=none 위조 ---------------------------------------------
    forged = pyjwt.encode({"sub": "admin", "role": "admin", "iss": JWT_ISS,
                           "aud": JWT_AUD, "exp": int(time.time()) + 900},
                          key="", algorithm="none")
    r = await rt.client.get("/api/v1/audit", headers={"Authorization": f"Bearer {forged}"})
    rt.record(
        "S2 JWT alg=none 위조",
        "베어러 JWT 서명 검증",
        "알고리즘 혼동 공격 → 401",
        f"상태코드={r.status_code} detail={r.json().get('detail','')[:60]}",
        r.status_code == 401,
        await rt.audit_has(event="http", detail_contains="/audit 401"),
    )

    # --- S3. 공격자 키로 서명한 admin JWT ----------------------------------
    evil_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    evil_pem = evil_key.private_bytes(
        encoding=__import__("cryptography").hazmat.primitives.serialization.Encoding.PEM,
        format=__import__("cryptography").hazmat.primitives.serialization.PrivateFormat.PKCS8,
        encryption_algorithm=__import__("cryptography").hazmat.primitives.serialization.NoEncryption(),
    )
    forged3 = pyjwt.encode({"sub": "admin", "role": "admin", "iss": JWT_ISS,
                            "aud": JWT_AUD, "iat": int(time.time()),
                            "exp": int(time.time()) + 900}, evil_pem, algorithm="RS256")
    r = await rt.client.get("/api/v1/devices", headers={"Authorization": f"Bearer {forged3}"})
    rt.record(
        "S3 공격자 RSA키로 서명한 위조 JWT",
        "Manager 공개키 기반 서명 검증",
        "서명 불일치 → 401",
        f"상태코드={r.status_code} detail={r.json().get('detail','')[:50]}",
        r.status_code == 401,
        await rt.audit_has(event="http", detail_contains="/devices 401"),
    )

    # --- S4. 정상 device 토큰의 payload를 admin으로 변조 --------------------
    h, p_b64, s = tokenA.split(".")
    payload = json.loads(base64.urlsafe_b64decode(p_b64 + "=="))
    payload["role"] = "admin"
    tampered_p = base64.urlsafe_b64encode(
        json.dumps(payload).encode()).rstrip(b"=").decode()
    tampered = f"{h}.{tampered_p}.{s}"
    r = await rt.client.get("/api/v1/audit", headers={"Authorization": f"Bearer {tampered}"})
    rt.record(
        "S4 JWT payload 변조 (device→admin 권한상승)",
        "서명 무결성",
        "payload 변조 시 서명 깨짐 → 401",
        f"상태코드={r.status_code}",
        r.status_code == 401,
        await rt.audit_has(event="http", detail_contains="/audit 401"),
    )

    # --- S5. 만료된 JWT ----------------------------------------------------
    expired = pyjwt.encode(
        {"sub": "dev-alpha", "role": "device", "iss": JWT_ISS, "aud": JWT_AUD,
         "iat": int(time.time()) - 2000, "exp": int(time.time()) - 1000},
        rt.app.state.jwt_private_pem, algorithm="RS256")
    r = await rt.client.post("/api/v1/telemetry",
                             headers={"Authorization": f"Bearer {expired}"},
                             json={"device_id": "dev-alpha", "jws": "x"})
    rt.record(
        "S5 만료 JWT 재사용",
        "토큰 수명(exp) 강제",
        "만료 토큰 → 401",
        f"상태코드={r.status_code} detail={r.json().get('detail','')[:50]}",
        r.status_code == 401,
        await rt.audit_has(event="http", detail_contains="/telemetry 401"),
    )

    # --- S6. RBAC 권한상승: operator가 admin 전용 호출 ---------------------
    hits = []
    for method, path in [("GET", "/api/v1/audit"),
                         ("POST", "/api/v1/devices/dev-alpha/revoke"),
                         ("POST", "/api/v1/devices/dev-alpha/approve")]:
        r = await rt.client.request(method, path,
                                    headers={"Authorization": f"Bearer {op_token}"})
        hits.append(r.status_code)
    rt.record(
        "S6 RBAC 수직 권한상승 (operator→admin)",
        "RBAC 매트릭스 (audit/approve/revoke=admin 전용)",
        "operator 역할 → 403",
        f"상태코드={hits}",
        all(c == 403 for c in hits),
        await rt.audit_has(event="http", detail_contains="/audit 403"),
    )

    # --- S7. RBAC 권한상승: device가 관리 API 호출 -------------------------
    hits = []
    for method, path in [("GET", "/api/v1/devices"), ("GET", "/api/v1/audit")]:
        r = await rt.client.request(method, path,
                                    headers={"Authorization": f"Bearer {tokenA}"})
        hits.append(r.status_code)
    rt.record(
        "S7 RBAC 권한상승 (device→operator/admin)",
        "RBAC 매트릭스 (devices/audit)",
        "device 역할 → 403",
        f"상태코드={hits}",
        all(c == 403 for c in hits),
        await rt.audit_has(event="http", detail_contains="/devices 403"),
    )

    # --- S8. 타 디바이스 사칭: A 토큰으로 B의 telemetry 전송 ----------------
    jws_for_b = sign_payload({"device_id": idB, "temperature": 99}, keyB)
    r = await rt.client.post("/api/v1/telemetry",
                             headers={"Authorization": f"Bearer {tokenA}"},
                             json={"device_id": idB, "jws": jws_for_b})
    rt.record(
        "S8 타 디바이스 사칭 텔레메트리 (횡적 권한상승)",
        "토큰 sub ↔ device_id 바인딩",
        "A의 토큰으로 B 데이터 전송 → 403",
        f"상태코드={r.status_code} detail={r.json().get('detail','')[:55]}",
        r.status_code == 403,
        await rt.audit_has(event="telemetry_reject", device_id=idB),
    )

    # --- S9. JWS 위조: 남의 키로 서명한 텔레메트리 -------------------------
    jws_evil = sign_payload({"device_id": idA, "temperature": 1}, evil_pem)
    r = await rt.client.post("/api/v1/telemetry",
                             headers={"Authorization": f"Bearer {tokenA}"},
                             json={"device_id": idA, "jws": jws_evil})
    rt.record(
        "S9 JWS 서명 위조 (데이터 무결성)",
        "디바이스 인증서 공개키 기반 JWS 검증",
        "미등록 키 서명 → 401 (JWS 검증 실패)",
        f"상태코드={r.status_code} detail={r.json().get('detail','')[:50]}",
        r.status_code == 401,
        await rt.audit_has(event="telemetry_reject", detail_contains="JWS"),
    )

    # --- S10. CSR SAN 스푸핑: dev-legit 등록에 admin SAN CSR ---------------
    spoof_csr, _ = pki.create_csr("dev-admin-spoof")  # SAN=dev-admin-spoof
    r = await rt.client.post("/api/v1/devices/register", json={
        "device_id": "dev-legit", "site": "x", "group": "y",
        "csr_pem": spoof_csr.decode(), "bootstrap_token": BOOTSTRAP_TOKEN})
    rt.record(
        "S10 CSR SAN 신원 스푸핑",
        "CSR SAN ↔ device_id 교차검증",
        "SAN≠device_id → 400 거부",
        f"상태코드={r.status_code} detail={r.json().get('detail','')[:55]}",
        r.status_code == 400,
        await rt.audit_has(event="register", detail_contains="does not"),
    )

    # --- S11. 손상된 CSR 주입 (파서 크래시/500 유발 시도) ------------------
    r = await rt.client.post("/api/v1/devices/register", json={
        "device_id": "dev-junk", "site": "x", "group": "y",
        "csr_pem": "-----BEGIN CERTIFICATE REQUEST-----\nNOT_A_REAL_CSR\n"
                   "-----END CERTIFICATE REQUEST-----",
        "bootstrap_token": BOOTSTRAP_TOKEN})
    rt.record(
        "S11 손상된 CSR 주입 (DoS/500 유발)",
        "CSR 파서 예외처리",
        "500이 아니라 400 + 감사기록",
        f"상태코드={r.status_code} detail={r.json().get('detail','')[:40]}",
        r.status_code == 400,
        await rt.audit_has(event="register", detail_contains="invalid CSR"),
    )

    # --- S12. bootstrap 토큰 오류/무차별 대입 -----------------------------
    csr_bf, _ = pki.create_csr("dev-bf")
    r = await rt.client.post("/api/v1/devices/register", json={
        "device_id": "dev-bf", "site": "x", "group": "y",
        "csr_pem": csr_bf.decode(), "bootstrap_token": "wrong-token-guess"})
    rt.record(
        "S12 부트스트랩 토큰 무단 등록",
        "등록 게이트 (bootstrap token)",
        "잘못된 토큰 → 401",
        f"상태코드={r.status_code} detail={r.json().get('detail','')[:40]}",
        r.status_code == 401,
        await rt.audit_has(event="register", detail_contains="invalid bootstrap"),
    )

    # --- S13. 외부 CA 위조 인증서로 인증 시도 ------------------------------
    ext_ca_cert, ext_ca_key = pki.create_ca("EVIL CA")
    ext_csr, _ = pki.create_csr("dev-alpha")   # 정상 SAN 이지만 외부 CA 서명
    ext_leaf = pki.sign_csr(ext_ca_cert, ext_ca_key, ext_csr)
    r = await rt.client.post("/api/v1/auth/token", json={"cert_pem": ext_leaf.decode()})
    rt.record(
        "S13 외부 CA 위조 인증서 인증",
        "인증서 체인 검증 (신뢰 앵커)",
        "타 CA 발급 인증서 → 401 chain 실패",
        f"상태코드={r.status_code} detail={r.json().get('detail','')[:45]}",
        r.status_code == 401,
        await rt.audit_has(event="auth_fail", detail_contains="chain"),
    )

    # --- S14. 폐기(revoke)된 디바이스 인증서 재사용 -----------------------
    # dev-bravo를 admin이 폐기 → 여전히 암호학적으로 유효한 certB 로 재인증 시도
    rr = await rt.client.post(f"/api/v1/devices/{idB}/revoke",
                              headers={"Authorization": f"Bearer {admin_token}"})
    r = await rt.client.post("/api/v1/auth/token", json={"cert_pem": certB})
    blocked = r.status_code == 401 and rr.status_code == 200
    rt.record(
        "S14 폐기된 인증서 재사용",
        "인증서 폐기(CRL/serial) 반영",
        "revoke 후 재인증 → 401",
        f"revoke={rr.status_code}, 재인증={r.status_code} "
        f"detail={r.json().get('detail','')[:35]}",
        blocked,
        await rt.audit_has(event="auth_fail", device_id=idB, detail_contains="revoked"),
    )

    # --- S15. 텔레메트리 재전송(replay) — jti/iat 재전송 방어 검증 ---------
    jws_ok = sign_payload({"device_id": idA, "temperature": 21, "seq": 1,
                           "jti": __import__("uuid").uuid4().hex,
                           "iat": int(time.time())}, keyA)
    r1 = await rt.client.post("/api/v1/telemetry",
                              headers={"Authorization": f"Bearer {tokenA}"},
                              json={"device_id": idA, "jws": jws_ok})
    r2 = await rt.client.post("/api/v1/telemetry",   # 동일 JWS(동일 jti) 재전송
                              headers={"Authorization": f"Bearer {tokenA}"},
                              json={"device_id": idA, "jws": jws_ok})
    replay_blocked = r1.status_code == 200 and r2.status_code == 409
    rt.record(
        "S15 텔레메트리 재전송(replay) 공격",
        "메시지 재사용 방지 (nonce/jti)",
        "동일 JWS 2회 → 2번째 409 거부",
        f"1차={r1.status_code}, 2차(재전송)={r2.status_code}",
        replay_blocked,
        await rt.audit_has(event="telemetry_reject", detail_contains="replay"),
        note="JWS에 jti/iat 추가 + Manager 측 (device_id,jti) 중복거부로 방어",
    )

    return rt


def print_report(rt: RedTeam) -> None:
    print("\n" + "=" * 78)
    print(" 시나리오 기반 공격 테스트 결과 요약")
    print("=" * 78)
    header = f"{'#':>2} {'시나리오':<38} {'차단':<6} {'감사':<6}"
    print(header)
    print("-" * 78)
    blocked_n = 0
    for r in rt.results:
        b = "O" if r.blocked else "X"
        a = "O" if r.audited else "-"
        if r.blocked:
            blocked_n += 1
        name = r.name if len(r.name) <= 37 else r.name[:36] + "…"
        print(f"{r.idx:>2} {name:<38} {b:<6} {a:<6}")
    print("-" * 78)
    print(f" 차단 {blocked_n}/{len(rt.results)} 시나리오 · "
          f"감사기록 {sum(1 for r in rt.results if r.audited)}/{len(rt.results)}")
    # 한계/미차단 항목
    gaps = [r for r in rt.results if not r.blocked]
    if gaps:
        print("\n [주의] 차단되지 않은 시나리오:")
        for r in gaps:
            print(f"   - {r.name}: {r.note or r.observed}")
    print("=" * 78)


if __name__ == "__main__":
    rt = asyncio.run(run())
    print_report(rt)
    asyncio.run(rt.aclose())
