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


class PermanentTelemetryError(Exception):
    """Manager가 4xx로 영구 거부한 경우(폐기된 디바이스, 위조/불일치 JWS 등).

    네트워크 오류나 Manager 측 5xx와 달리, 동일한 요청을 재시도해도 결과가
    바뀌지 않는 것이 확정적이므로 :meth:`EdgeAgent.send_telemetry` /
    :meth:`EdgeAgent.flush_buffer` 는 이 경우를 버퍼링(무한 재시도) 대상에서
    제외하고 이 예외로 즉시 알린다.
    """

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


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

        self._load_existing_credentials()

    def _load_existing_credentials(self) -> None:
        """certs_dir에 이전 enroll()이 남긴 인증서+개인키가 있으면 로드한다.

        이게 없으면 프로세스가 재시작될 때마다 매번 다시 enroll()해야 하고,
        그러면 Manager는 이미 등록된 device_id에 대해 무조건 409를 반환하므로
        재시작 후에는 아무것도 동작하지 않게 된다 - "재시작 후에도 로컬 버퍼가
        남아 있다가 재전송된다"는 전제 자체가 성립하려면 자격증명도 함께
        디스크에서 복구되어야 한다.
        """
        key_path = self.certs_dir / f"{self.device_id}.key.pem"
        cert_path = self.certs_dir / f"{self.device_id}.cert.pem"
        if key_path.exists():
            self._key_pem = pki.load_pem(key_path)
        if cert_path.exists():
            self._cert_pem = pki.load_pem(cert_path).decode()

    # -- enrollment -----------------------------------------------------

    async def enroll(self, bootstrap_token: str) -> str:
        """CSR을 생성해 Manager에 등록하고, 발급된 인증서+개인키를 certs_dir에 저장한다.

        이미 certs_dir에서 인증서가 로드되어 있으면(이전에 이 디바이스가
        "approved"까지 성공했고 프로세스만 재시작된 경우) Manager에 다시
        등록을 시도하지 않고 즉시 ``"approved"``를 반환한다 - Manager의
        register 엔드포인트는 이미 등록된 device_id에 대해 무조건 409를
        반환하므로, 재시도해봐야 실패만 반복될 뿐이다. (인증서 없이 개인키만
        복구된 "pending" 상태 - 아직 관리자 승인 대기 중 재시작된 경우 - 는
        이 단축 경로 대상이 아니며, device 역할로는 자신의 승인 상태를 조회할
        Manager API가 없어 이번 구현 범위 밖으로 남겨둔다.)

        Returns:
            등록 상태(``"approved"`` 또는 ``"pending"``).

        Raises:
            httpx.HTTPStatusError: Manager가 등록을 거부한 경우(잘못된
                bootstrap_token 등).
        """
        if self._cert_pem is not None:
            return "approved"

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
        """payload를 JWS 서명 후 1회 전송 시도.

        실패를 두 종류로 명확히 구분한다(재시도해도 되는지가 완전히 다르므로):

        * **일시적(transient)** - 네트워크 오류/타임아웃(``httpx.TransportError``)
          또는 Manager 측 5xx. 재시도하면 성공할 수 있으므로 ``False``를
          반환해 호출자가 버퍼링하게 한다.
        * **영구적(permanent)** - ``/auth/token`` 또는 ``/telemetry``가 4xx로
          거부(폐기된 디바이스, 위조/불일치 JWS 등). 재시도해도 동일하게
          실패하는 것이 확정적이므로 :class:`PermanentTelemetryError`를 그대로
          던진다 - 버퍼링하면 무한 재시도 루프가 된다.

        Returns:
            성공(2xx)이면 True, 일시적 실패면 False.

        Raises:
            PermanentTelemetryError: Manager가 4xx로 영구 거부한 경우.
        """
        try:
            token = await self.get_token()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if 400 <= status < 500:
                raise PermanentTelemetryError(
                    f"auth/token permanently rejected (status={status}): {exc}",
                    status_code=status,
                ) from exc
            return False  # Manager 측 5xx -> 일시적, 버퍼링 대상.
        except httpx.TransportError:
            return False  # 네트워크 계층 오류(연결 불가, 타임아웃 등) -> 일시적.

        jws_token = sign_payload(payload, self._key_pem)
        try:
            resp = await self.client.post(
                TELEMETRY_PATH,
                json={"device_id": self.device_id, "jws": jws_token},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TransportError:
            return False

        if resp.status_code < 400:
            return True
        if resp.status_code < 500:
            raise PermanentTelemetryError(
                f"telemetry permanently rejected (status={resp.status_code}): {resp.text}",
                status_code=resp.status_code,
            )
        return False  # Manager 측 5xx -> 일시적, 버퍼링 대상.

    def _append_buffer(self, payload: Dict[str, Any]) -> None:
        self.buffer_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.buffer_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    async def send_telemetry(self, payload: Dict[str, Any]) -> bool:
        """payload를 서명·전송한다.

        * 성공(2xx): ``True``.
        * 일시적 실패(네트워크 오류, Manager 5xx): 로컬 버퍼(JSONL)에 적재하고
          ``False``를 반환(나중에 :meth:`flush_buffer`로 재시도 가능).
        * 영구 실패(4xx - 폐기된 디바이스, 위조/불일치 JWS 등): 버퍼링하지
          않고 :class:`PermanentTelemetryError`를 그대로 전파한다 - 재시도해도
          실패가 확정적인 요청을 무한정 쌓아두지 않기 위함이다.
        """
        ok = await self._post_telemetry_once(payload)
        if not ok:
            self._append_buffer(payload)
        return ok

    async def flush_buffer(self) -> int:
        """버퍼에 적재된 payload를 순서대로 재전송한다.

        * 성공: 버퍼에서 제거.
        * 일시적 실패(네트워크 오류, Manager 5xx): 버퍼에 그대로 남겨 다음
          ``flush_buffer()`` 호출에서 다시 시도한다.
        * 영구 실패(4xx, :class:`PermanentTelemetryError`): 재시도해도 결과가
          바뀌지 않는 것이 확정적이므로 **드롭**한다(버퍼에 남기지 않음) -
          그렇지 않으면 회복 불가능한 항목이 매 flush마다 무한 재시도되어
          버퍼가 영원히 비워지지 않는다. 드롭된 항목은 별도의 dead-letter
          저장 없이 그냥 버려진다(자체 판단 사항, 보고서에 명시).

        Returns:
            성공적으로 재전송한 payload 개수(영구 실패로 드롭된 항목은
            포함하지 않음).
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
            try:
                ok = await self._post_telemetry_once(payload)
            except PermanentTelemetryError:
                continue  # 영구 실패: 드롭하고 다음 항목으로 진행.
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
