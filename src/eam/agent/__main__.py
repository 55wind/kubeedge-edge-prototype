"""``python -m eam.agent`` 진입점 — 단일 Device를 상시 기동해 enroll 후 주기적으로
센서 텔레메트리를 전송한다.

K8s 배포(``k8s/agent-edge1.yaml``, ``k8s/agent-edge2.yaml``)의 컨테이너
``command``로 사용되는 실제 실행 가능한 엔트리다. 모든 옵션은 CLI 인자 또는
동일 목적의 환경변수로 지정할 수 있다(CLI 인자가 우선).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from typing import Optional, Sequence

from eam.agent.agent import EdgeAgent

logger = logging.getLogger("eam.agent.run")

DEFAULT_INTERVAL_SECONDS = 30.0
DEFAULT_SENSOR_TYPE = "temperature"


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="EdgeAgent 상시 실행기 - enroll 후 주기적으로 센서 텔레메트리를 전송한다."
    )
    parser.add_argument(
        "--device-id", default=os.environ.get("DEVICE_ID"), help="디바이스 식별자 (env: DEVICE_ID)"
    )
    parser.add_argument(
        "--site", default=os.environ.get("DEVICE_SITE", "unknown-site"), help="설치 사이트 (env: DEVICE_SITE)"
    )
    parser.add_argument(
        "--group", default=os.environ.get("DEVICE_GROUP", "sensors"), help="디바이스 그룹 (env: DEVICE_GROUP)"
    )
    parser.add_argument(
        "--manager-url",
        default=os.environ.get("MANAGER_BASE_URL"),
        help="Manager 베이스 URL, 예: http://__CLOUD_IP__:30443 (env: MANAGER_BASE_URL)",
    )
    parser.add_argument(
        "--bootstrap-token",
        default=os.environ.get("BOOTSTRAP_TOKEN"),
        help="Manager 등록용 부트스트랩 토큰 (env: BOOTSTRAP_TOKEN)",
    )
    parser.add_argument(
        "--certs-dir",
        default=os.environ.get("CERTS_DIR", "certs"),
        help="인증서/개인키/버퍼 저장 디렉터리 (env: CERTS_DIR)",
    )
    parser.add_argument(
        "--sensor-type",
        default=os.environ.get("AGENT_SENSOR_TYPE", DEFAULT_SENSOR_TYPE),
        help="시뮬레이션할 센서 종류: temperature|humidity (env: AGENT_SENSOR_TYPE)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("AGENT_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)),
        help="텔레메트리 전송 주기(초) (env: AGENT_INTERVAL_SECONDS)",
    )
    args = parser.parse_args(argv)

    missing = [
        name
        for name, value in (
            ("--device-id/DEVICE_ID", args.device_id),
            ("--manager-url/MANAGER_BASE_URL", args.manager_url),
            ("--bootstrap-token/BOOTSTRAP_TOKEN", args.bootstrap_token),
        )
        if not value
    ]
    if missing:
        parser.error(f"필수 값이 누락되었습니다: {', '.join(missing)}")
    return args


async def _run(args: argparse.Namespace) -> None:
    agent = EdgeAgent(
        device_id=args.device_id,
        site=args.site,
        group=args.group,
        manager_url=args.manager_url,
        certs_dir=args.certs_dir,
    )

    stop = asyncio.Event()

    def _handle_signal(signum: int, _frame: object) -> None:
        logger.info("signal %s 수신 - 현재 주기 완료 후 종료합니다", signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):  # pragma: no cover - 플랫폼별 시그널 제약
            pass

    try:
        status = await agent.enroll(args.bootstrap_token)
        logger.info("enroll 완료: device_id=%s status=%s", args.device_id, status)

        while not stop.is_set():
            flushed = await agent.flush_buffer()
            if flushed:
                logger.info("버퍼 재전송 %d건 성공", flushed)

            reading = agent.read_sensor(args.sensor_type)
            ok = await agent.send_telemetry(reading)
            logger.info("텔레메트리 전송 %s: %s", "성공" if ok else "버퍼 적재(일시 실패)", reading)

            try:
                await asyncio.wait_for(stop.wait(), timeout=args.interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await agent.aclose()


def main(argv: Optional[Sequence[str]] = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
