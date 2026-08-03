"""VirtualDevice: EdgeAgent를 래핑해 enroll->인증->텔레메트리 수명주기를 실행.

각 단계(enroll/auth/telemetry)의 소요시간을 밀리초 단위로 기록해서
:mod:`eam.simulator.fleet`가 N대를 동시 실행한 뒤 p50/p95/p99 등 통계를
집계할 수 있게 한다.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import List, Optional, Union

import httpx

from eam.agent.agent import EdgeAgent

PathLike = Union[str, "os.PathLike[str]"]


@dataclass
class VirtualDeviceResult:
    """가상 디바이스 1대의 수명주기 실행 결과."""

    device_id: str
    success: bool
    enroll_ms: Optional[float] = None
    auth_ms: Optional[float] = None
    telemetry_ms: List[float] = field(default_factory=list)
    error: Optional[str] = None


class VirtualDevice:
    """EdgeAgent를 래핑해 enroll -> get_token -> send_telemetry(k회) 수명주기를 실행."""

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
        self.agent = EdgeAgent(
            device_id=device_id,
            site=site,
            group=group,
            manager_url=manager_url,
            certs_dir=certs_dir,
            transport=transport,
        )

    async def run_lifecycle(
        self, bootstrap_token: str, telemetry_count: int = 1
    ) -> VirtualDeviceResult:
        """enroll -> get_token -> send_telemetry(telemetry_count회)를 실행하고 결과를 반환.

        중간에 실패하면 그때까지 측정된 지연시간과 함께 ``success=False``,
        ``error``에 사유를 담아 반환한다(예외를 상위로 전파하지 않음 -
        fleet가 N대를 모을 때 개별 실패를 정상적으로 집계할 수 있도록).
        """
        result = VirtualDeviceResult(device_id=self.device_id, success=False)
        try:
            t0 = time.perf_counter()
            await self.agent.enroll(bootstrap_token)
            result.enroll_ms = (time.perf_counter() - t0) * 1000.0

            t0 = time.perf_counter()
            await self.agent.get_token()
            result.auth_ms = (time.perf_counter() - t0) * 1000.0

            sensor_types = ("temperature", "humidity")
            for i in range(telemetry_count):
                payload = self.agent.read_sensor(sensor_types[i % len(sensor_types)])
                t0 = time.perf_counter()
                ok = await self.agent.send_telemetry(payload)
                result.telemetry_ms.append((time.perf_counter() - t0) * 1000.0)
                if not ok:
                    # send_telemetry() already appended this payload to the
                    # local buffer for later flush_buffer() retry; from this
                    # lifecycle run's point of view it still counts as a
                    # failed attempt (Manager was not actually reached).
                    raise RuntimeError(
                        f"telemetry send #{i} failed transiently (buffered for later retry, "
                        "not yet delivered to Manager)"
                    )

            result.success = True
        except Exception as exc:  # noqa: BLE001 - 개별 디바이스 실패를 결과에 담아 반환
            result.error = f"{exc.__class__.__name__}: {exc}"
        finally:
            await self.agent.aclose()
        return result
