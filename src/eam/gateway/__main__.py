"""``python -m eam.gateway`` 진입점 — 게이트웨이를 상시 기동해 사설망 하위 디바이스의
텔레메트리를 집선(aggregate)해 Manager로 배치 업링크한다.

K8s 배포(``k8s/gateway.yaml``)의 컨테이너 ``command``로 사용되며, "사설IP 집선"
시나리오를 시연한다: Manager에 직접 도달할 수 없는 사설망(예: 192.168.x.x 대역)
하위 디바이스들이 게이트웨이 하나의 Device 신원(mTLS 인증서)으로만 업링크된다.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from typing import Optional, Sequence

from eam.gateway.gateway import EdgeGateway

logger = logging.getLogger("eam.gateway.run")

DEFAULT_INTERVAL_SECONDS = 30.0
DEFAULT_SUB_DEVICES = "priv-sensor-01,priv-sensor-02"


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="EdgeGateway 상시 실행기 - 사설망 하위 디바이스 텔레메트리를 집선해 배치 업링크한다."
    )
    parser.add_argument(
        "--gateway-id", default=os.environ.get("GATEWAY_ID"), help="게이트웨이 자신의 디바이스 식별자 (env: GATEWAY_ID)"
    )
    parser.add_argument(
        "--site", default=os.environ.get("DEVICE_SITE", "unknown-site"), help="설치 사이트 (env: DEVICE_SITE)"
    )
    parser.add_argument(
        "--group", default=os.environ.get("DEVICE_GROUP", "gateways"), help="디바이스 그룹 (env: DEVICE_GROUP)"
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
        "--sub-devices",
        default=os.environ.get("GATEWAY_SUB_DEVICES", DEFAULT_SUB_DEVICES),
        help="사설망 하위 디바이스 식별자 콤마 목록 (env: GATEWAY_SUB_DEVICES)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("AGENT_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)),
        help="배치 업링크 주기(초) (env: AGENT_INTERVAL_SECONDS)",
    )
    args = parser.parse_args(argv)

    missing = [
        name
        for name, value in (
            ("--gateway-id/GATEWAY_ID", args.gateway_id),
            ("--manager-url/MANAGER_BASE_URL", args.manager_url),
            ("--bootstrap-token/BOOTSTRAP_TOKEN", args.bootstrap_token),
        )
        if not value
    ]
    if missing:
        parser.error(f"필수 값이 누락되었습니다: {', '.join(missing)}")
    return args


async def _run(args: argparse.Namespace) -> None:
    gateway = EdgeGateway(
        gateway_id=args.gateway_id,
        site=args.site,
        group=args.group,
        manager_url=args.manager_url,
        certs_dir=args.certs_dir,
    )

    sub_device_ids = [d.strip() for d in args.sub_devices.split(",") if d.strip()]
    for device_id in sub_device_ids:
        gateway.attach(device_id)
    logger.info("사설망 하위 디바이스 %d대 부착: %s", len(sub_device_ids), sub_device_ids)

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
        status = await gateway.enroll(args.bootstrap_token)
        logger.info("게이트웨이 enroll 완료: gateway_id=%s status=%s", args.gateway_id, status)

        while not stop.is_set():
            flushed = await gateway.flush_buffer()
            if flushed:
                logger.info("버퍼 재전송 %d건 성공", flushed)

            ok = await gateway.send_batch_telemetry()
            logger.info(
                "배치 텔레메트리 전송 %s: 하위 디바이스 %d대분",
                "성공" if ok else "버퍼 적재(일시 실패)",
                len(sub_device_ids),
            )

            try:
                await asyncio.wait_for(stop.wait(), timeout=args.interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await gateway.aclose()


def main(argv: Optional[Sequence[str]] = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
