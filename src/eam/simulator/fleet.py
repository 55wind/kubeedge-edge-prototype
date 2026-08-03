"""run_fleet: 가상 디바이스 N대를 동시성 제한 하에 실행하고 단계별 지연시간을 집계.

CLI: ``python -m eam.simulator.fleet --n 50`` — ``--manager-url``을 생략하면
Manager FastAPI 앱을 인프로세스로 띄우고 ``httpx.ASGITransport``로 직접
두드린다(실서버/네트워크 불필요, Task 4 벤치마크가 동일 함수를 재사용한다).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

import httpx

from eam.simulator.vdevice import VirtualDevice, VirtualDeviceResult

PathLike = Union[str, "os.PathLike[str]"]

DEFAULT_BOOTSTRAP_TOKEN = "fleet-sim-bootstrap-token"


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    """선형 보간 백분위수 (0<=pct<=100). ``sorted_values``는 이미 정렬돼 있어야 한다."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_values[int(k)]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


@dataclass
class PhaseStats:
    """한 단계(enroll/auth/telemetry)의 지연시간(ms) 통계."""

    count: int
    p50: float
    p95: float
    p99: float
    mean: float
    max: float

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)


def _phase_stats(values: List[float]) -> PhaseStats:
    if not values:
        return PhaseStats(count=0, p50=0.0, p95=0.0, p99=0.0, mean=0.0, max=0.0)
    ordered = sorted(values)
    return PhaseStats(
        count=len(values),
        p50=_percentile(ordered, 50),
        p95=_percentile(ordered, 95),
        p99=_percentile(ordered, 99),
        mean=statistics.fmean(values),
        max=max(values),
    )


@dataclass
class FleetResult:
    """플릿 실행 결과: 성공/실패 수, 단계별 지연시간 통계, 총 소요시간."""

    n: int
    concurrency: int
    success_count: int
    fail_count: int
    wall_time_s: float
    phase_stats: Dict[str, PhaseStats]
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n": self.n,
            "concurrency": self.concurrency,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "wall_time_s": self.wall_time_s,
            "phase_stats": {k: v.to_dict() for k, v in self.phase_stats.items()},
            "errors": self.errors,
        }


async def run_fleet(
    n: int,
    manager_url: str,
    concurrency: int = 10,
    telemetry_per_device: int = 3,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    bootstrap_token: str = DEFAULT_BOOTSTRAP_TOKEN,
    certs_dir_root: Optional[PathLike] = None,
    device_id_prefix: str = "sim-dev",
) -> FleetResult:
    """가상 디바이스 ``n``대의 수명주기(enroll->auth->telemetry)를 동시 실행한다.

    ``asyncio.Semaphore(concurrency)``로 동시 실행 수를 제한한다. ``transport``에
    ``httpx.ASGITransport(app=...)``를 넘기면 실 네트워크 없이 인프로세스로
    Manager 앱을 직접 두드린다.
    """
    root = Path(certs_dir_root) if certs_dir_root is not None else Path(
        tempfile.mkdtemp(prefix="eam-fleet-certs-")
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def _run_one(index: int) -> VirtualDeviceResult:
        device_id = f"{device_id_prefix}-{index:05d}"
        async with semaphore:
            vdevice = VirtualDevice(
                device_id=device_id,
                site="sim-site",
                group="sim-group",
                manager_url=manager_url,
                certs_dir=root / device_id,
                transport=transport,
            )
            return await vdevice.run_lifecycle(
                bootstrap_token, telemetry_count=telemetry_per_device
            )

    start = time.perf_counter()
    results = await asyncio.gather(*[_run_one(i) for i in range(n)])
    wall_time_s = time.perf_counter() - start

    enroll_values = [r.enroll_ms for r in results if r.enroll_ms is not None]
    auth_values = [r.auth_ms for r in results if r.auth_ms is not None]
    telemetry_values = [ms for r in results for ms in r.telemetry_ms]

    phase_stats = {
        "enroll": _phase_stats(enroll_values),
        "auth": _phase_stats(auth_values),
        "telemetry": _phase_stats(telemetry_values),
    }

    success_count = sum(1 for r in results if r.success)
    errors = [r.error for r in results if r.error]

    return FleetResult(
        n=n,
        concurrency=concurrency,
        success_count=success_count,
        fail_count=n - success_count,
        wall_time_s=wall_time_s,
        phase_stats=phase_stats,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_inprocess_transport() -> "tuple[httpx.AsyncBaseTransport, str, str, Path]":
    """--manager-url이 생략된 경우 Manager 앱을 인프로세스로 띄워 ASGITransport를 만든다."""
    from eam.manager.app import create_app

    tmp_dir = Path(tempfile.mkdtemp(prefix="eam-fleet-app-"))
    # AUTO_APPROVE가 켜져 있어야 플릿의 각 가상 디바이스가 즉시 인증서를 받아
    # get_token()으로 넘어갈 수 있다 (수동 승인 대기 시뮬레이션은 이 CLI의 범위 밖).
    os.environ["AUTO_APPROVE"] = "true"
    app = create_app(
        certs_dir=tmp_dir / "manager_certs",
        store_db_path=tmp_dir / "eam.db",
        audit_db_path=tmp_dir / "eam_audit.db",
    )
    transport = httpx.ASGITransport(app=app)
    return transport, "http://in-process.eam.local", str(app.state.bootstrap_token), tmp_dir


def _cli(argv: Optional[Sequence[str]] = None) -> FleetResult:
    parser = argparse.ArgumentParser(
        description="가상 디바이스 플릿(fleet) 시뮬레이터 - enroll/auth/telemetry 지연시간 집계"
    )
    parser.add_argument("--n", type=int, default=50, help="가상 디바이스 수 (기본 50)")
    parser.add_argument("--concurrency", type=int, default=10, help="동시 실행 수 (기본 10)")
    parser.add_argument(
        "--telemetry-per-device", type=int, default=3, help="디바이스당 텔레메트리 전송 횟수"
    )
    parser.add_argument(
        "--manager-url",
        default=None,
        help="생략 시 Manager 앱을 인프로세스로 띄워 ASGITransport로 직접 호출",
    )
    parser.add_argument(
        "--bootstrap-token",
        default=None,
        help="--manager-url 지정 시 필수 (실서버의 BOOTSTRAP_TOKEN)",
    )
    args = parser.parse_args(argv)

    if args.manager_url:
        if not args.bootstrap_token:
            raise SystemExit("--manager-url 사용 시 --bootstrap-token이 필요합니다.")
        transport = None
        manager_url = args.manager_url
        bootstrap_token = args.bootstrap_token
        certs_root: Optional[Path] = None
    else:
        transport, manager_url, bootstrap_token, tmp_dir = _build_inprocess_transport()
        certs_root = tmp_dir / "device_certs"

    result = asyncio.run(
        run_fleet(
            args.n,
            manager_url,
            args.concurrency,
            args.telemetry_per_device,
            transport=transport,
            bootstrap_token=bootstrap_token,
            certs_dir_root=certs_root,
        )
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    _cli()
