"""EdgeAgent: Device 측에서 Manager와 통신하는 클라이언트.

수명주기: ``enroll()`` (CSR 생성 -> 등록 -> 인증서 저장) -> ``get_token()``
(cert -> bearer JWT, 만료 임박 시 자동 갱신) -> ``send_telemetry()``
(디바이스 개인키로 JWS 서명 -> Manager에 POST, 실패 시 로컬 버퍼 적재).

``transport``에 ``httpx.ASGITransport(app=...)``를 주입하면 실제 소켓 없이
Manager FastAPI 앱을 인프로세스로 직접 두드릴 수 있다 (테스트/벤치마크 재사용 목적).
"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union

import httpx
import jwt as pyjwt

from eam.common import pki
from eam.common.jws import sign_payload

PathLike = Union[str, "os.PathLike[str]"]

# 만료 이 시간(초) 이내로 남으면 캐시된 토큰을 버리고 새로 발급받는다.
TOKEN_REFRESH_MARGIN_SECONDS = 60

REGISTER_PATH = "/api/v1/devices/register"
TOKEN_PATH = "/api/v1/auth/token"
TELEMETRY_PATH = "/api/v1/telemetry"


class AgentError(Exception):
    """EdgeAgent 사용 순서 오류(예: enroll 이전에 get_token 호출)."""


def simulate_sensor_reading(sensor_type: str) -> Dict[str, Any]:
    """센서 값을 랜덤 시뮬레이션으로 생성한다 (temperature/humidity).

    :class:`EdgeAgent` (자기 자신의 센서)와 ``eam.gateway`` 의
    ``SubDevice`` (게이트웨이 뒤 사설망 하위 디바이스, Manager에 직접
    등록되지 않음)가 이 함수를 공유해 동일한 시뮬레이션 로직을 쓴다.
    """
    if sensor_type == "temperature":
        value = round(random.uniform(15.0, 35.0), 2)
        unit = "celsius"
    elif sensor_type == "humidity":
        value = round(random.uniform(20.0, 90.0), 2)
        unit = "percent"
    else:
        raise ValueError(f"unknown sensor_type: {sensor_type!r}")

    return {
        "sensor_type": sensor_type,
        "value": value,
        "unit": unit,
        "ts": time.time(),
    }


class EdgeAgent:
    """Manager API를 두드리는 Device 측 클라이언트.

    ``manager_url``은 httpx의 ``base_url``로 사용된다. 실 네트워크를 타는 경우
    실제 Manager 주소를, 테스트/시뮬레이터에서는 ``transport``가 라우팅을
    전담하므로 임의의 플레이스홀더 URL이어도 무방하다.
    """

    def __init__(
        self,
        device_id: str,
        site: str,
        group: str,
        manager_url: str,
        certs_dir: PathLike,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.device_id = device_id
        self.site = site
        self.group = group
        self.manager_url = manager_url
        self.certs_dir = Path(certs_dir)
        self.certs_dir.mkdir(parents=True, exist_ok=True)

        self.client = httpx.AsyncClient(transport=transport, base_url=manager_url)

        self._key_pem: Optional[bytes] = None
        self._cert_pem: Optional[str] = None
        self._token: Optional[str] = None
        self._token_exp: Optional[float] = None

        buffer_dir_env = os.environ.get("AGENT_BUFFER_DIR")
        if buffer_dir_env:
            self.buffer_path = Path(buffer_dir_env) / f"{device_id}.jsonl"
            self.buffer_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            self.buffer_path = self.certs_dir / "buffer.jsonl"

    # -- enrollment -----------------------------------------------------

    async def enroll(self, bootstrap_token: str) -> str:
        """CSR을 생성해 Manager에 등록하고, 발급된 인증서+개인키를 certs_dir에 저장한다.

        Returns:
            등록 상태(``"approved"`` 또는 ``"pending"``).

        Raises:
            httpx.HTTPStatusError: Manager가 등록을 거부한 경우(잘못된
                bootstrap_token 등).
        """
        csr_pem, key_pem = pki.create_csr(self.device_id)
        resp = await self.client.post(
            REGISTER_PATH,
            json={
                "device_id": self.device_id,
                "site": self.site,
                "group": self.group,
                "csr_pem": csr_pem.decode(),
                "bootstrap_token": bootstrap_token,
            },
        )
        resp.raise_for_status()
        body = resp.json()

        self._key_pem = key_pem
        pki.save_pem(self.certs_dir / f"{self.device_id}.key.pem", key_pem)

        if body.get("cert_pem"):
            self._cert_pem = body["cert_pem"]
            pki.save_pem(
                self.certs_dir / f"{self.device_id}.cert.pem", self._cert_pem.encode()
            )

        return str(body["status"])

    # -- authentication ---------------------------------------------------

    async def get_token(self) -> str:
        """캐시된 bearer JWT를 반환하고, 만료 60초 이내면 자동 재발급한다."""
        now = time.time()
        if (
            self._token is not None
            and self._token_exp is not None
            and now < self._token_exp - TOKEN_REFRESH_MARGIN_SECONDS
        ):
            return self._token

        if not self._cert_pem:
            raise AgentError(
                "get_token() called before a successful enroll() (no certificate on file)"
            )

        resp = await self.client.post(TOKEN_PATH, json={"cert_pem": self._cert_pem})
        resp.raise_for_status()
        body = resp.json()
        token = str(body["access_token"])

        # 클라이언트 측 만료 시각 파악 목적으로만 디코드(서명 검증은 생략해도 무방 -
        # Manager가 방금 발급한 토큰의 exp 클레임을 읽을 뿐 신뢰 판단에 쓰지 않음).
        claims = pyjwt.decode(token, options={"verify_signature": False})
        exp = claims.get("exp")
        self._token_exp = float(exp) if exp is not None else now + float(
            body.get("expires_in", 900)
        )
        self._token = token
        return token

    # -- telemetry --------------------------------------------------------

    async def _post_telemetry_once(self, payload: Dict[str, Any]) -> bool:
        """payload를 JWS 서명 후 1회 전송 시도. 성공(2xx) 여부만 반환한다.

        토큰 갱신(``get_token``)이 필요한 상황에서 그 요청 자체가 전송 실패로
        끝나는 경우(예: 네트워크 단절 중 최초 텔레메트리 전송 시도)도 이
        메서드 관점에서는 동일한 "이번 전송 실패"로 취급해 버퍼링 대상이
        되게 한다.
        """
        try:
            token = await self.get_token()
        except httpx.HTTPError:
            return False

        jws_token = sign_payload(payload, self._key_pem)
        try:
            resp = await self.client.post(
                TELEMETRY_PATH,
                json={"device_id": self.device_id, "jws": jws_token},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError:
            return False
        return resp.status_code < 400

    def _append_buffer(self, payload: Dict[str, Any]) -> None:
        self.buffer_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.buffer_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    async def send_telemetry(self, payload: Dict[str, Any]) -> bool:
        """payload를 서명·전송한다. 전송 실패(transport/HTTP 오류) 시 로컬 버퍼에 적재한다."""
        ok = await self._post_telemetry_once(payload)
        if not ok:
            self._append_buffer(payload)
        return ok

    async def flush_buffer(self) -> int:
        """버퍼에 적재된 payload를 순서대로 재전송하고, 여전히 실패한 것만 남긴다.

        Returns:
            성공적으로 재전송한 payload 개수.
        """
        if not self.buffer_path.exists():
            return 0

        lines = [
            line for line in self.buffer_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]

        remaining: list[str] = []
        sent = 0
        for line in lines:
            payload = json.loads(line)
            ok = await self._post_telemetry_once(payload)
            if ok:
                sent += 1
            else:
                remaining.append(line)

        if remaining:
            self.buffer_path.write_text("\n".join(remaining) + "\n", encoding="utf-8")
        else:
            self.buffer_path.unlink(missing_ok=True)
        return sent

    # -- sensors ------------------------------------------------------------

    def read_sensor(self, sensor_type: str) -> Dict[str, Any]:
        """센서 값을 랜덤 시뮬레이션으로 생성한다 (temperature/humidity)."""
        return simulate_sensor_reading(sensor_type)

    # -- lifecycle ------------------------------------------------------------

    async def aclose(self) -> None:
        """내부 httpx.AsyncClient를 닫는다."""
        await self.client.aclose()
