"""EdgeGateway: 하위 사설망 디바이스를 대신해 Manager에 배치 업링크하는 게이트웨이.

게이트웨이 자신은 :class:`eam.agent.agent.EdgeAgent`로 구현된 하나의 Device로서
정상적으로 enroll/get_token을 수행한다. ``attach(device_id)``로 등록하는
하위 디바이스는 사설망(private-IP) 뒤에 있어 Manager에 직접 도달할 수 없다는
전제이므로 Manager에 별도로 enroll하지 않는다 — 대신 게이트웨이가 그 값을
모아 하나의 JWS 서명 텔레메트리 메시지(``{gateway_id, batch: [...]}``)로
업링크한다. 결과적으로 Manager에는 이 텔레메트리가 "보내는 주체"인
게이트웨이의 device_id 아래 저장된다(Manager 스키마/API는 수정하지 않음).
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Union

import httpx

from eam.agent.agent import EdgeAgent, simulate_sensor_reading

PathLike = Union[str, "os.PathLike[str]"]

DEFAULT_SENSOR_TYPES = ("temperature", "humidity")


class SubDevice:
    """Manager에 직접 등록되지 않는, 게이트웨이 뒤 사설망의 가상 하위 디바이스."""

    def __init__(self, device_id: str):
        self.device_id = device_id

    def read_sensor(self, sensor_type: str) -> Dict[str, Any]:
        """센서 값을 랜덤 시뮬레이션으로 생성한다 (EdgeAgent.read_sensor와 동일 로직)."""
        return simulate_sensor_reading(sensor_type)


class EdgeGateway:
    """자신은 Device로 enroll하고, 하위 사설망 디바이스는 로컬로만 관리하는 게이트웨이."""

    def __init__(
        self,
        gateway_id: str,
        site: str,
        group: str,
        manager_url: str,
        certs_dir: PathLike,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.gateway_id = gateway_id
        self.agent = EdgeAgent(
            device_id=gateway_id,
            site=site,
            group=group,
            manager_url=manager_url,
            certs_dir=certs_dir,
            transport=transport,
        )
        self.sub_devices: Dict[str, SubDevice] = {}

    # -- 게이트웨이 자신의 Manager 등록 -----------------------------------

    async def enroll(self, bootstrap_token: str) -> str:
        return await self.agent.enroll(bootstrap_token)

    async def get_token(self) -> str:
        return await self.agent.get_token()

    # -- 하위 사설망 디바이스 관리(Manager에 등록하지 않음) ------------------

    def attach(self, device_id: str) -> SubDevice:
        """하위 가상 디바이스를 게이트웨이에 로컬로 붙인다 (Manager에는 등록되지 않음)."""
        sub = SubDevice(device_id)
        self.sub_devices[device_id] = sub
        return sub

    def detach(self, device_id: str) -> None:
        self.sub_devices.pop(device_id, None)

    # -- 배치 업링크 ------------------------------------------------------

    def collect_batch(
        self, sensor_types: Iterable[str] = DEFAULT_SENSOR_TYPES
    ) -> List[Dict[str, Any]]:
        """붙어 있는 모든 하위 디바이스로부터 한 번씩 센서 값을 읽어 배치를 만든다."""
        batch: List[Dict[str, Any]] = []
        for device_id, sub in self.sub_devices.items():
            for sensor_type in sensor_types:
                reading = sub.read_sensor(sensor_type)
                batch.append({"device_id": device_id, **reading})
        return batch

    async def send_batch_telemetry(
        self, sensor_types: Iterable[str] = DEFAULT_SENSOR_TYPES
    ) -> bool:
        """하위 디바이스 배치를 단일 JWS 서명 텔레메트리로 게이트웨이 신원으로 전송한다."""
        batch = self.collect_batch(sensor_types)
        payload = {"gateway_id": self.gateway_id, "batch": batch}
        return await self.agent.send_telemetry(payload)

    async def flush_buffer(self) -> int:
        return await self.agent.flush_buffer()

    async def aclose(self) -> None:
        await self.agent.aclose()
