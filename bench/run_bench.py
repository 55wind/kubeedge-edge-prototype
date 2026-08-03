"""bench/run_bench.py: N-스윕 성능 벤치마크 CLI.

각 N에 대해 Manager FastAPI 앱을 인프로세스(``httpx.ASGITransport``, 실제 소켓
없음)로 새로 띄우고 ``eam.simulator.fleet.run_fleet``을 재사용해 가상 디바이스
N대의 enroll/auth/telemetry 지연시간 분포와 인증 처리량을 측정한다.

기존 ``eam.simulator.fleet``의 인프로세스 부트스트랩 로직
(``_build_inprocess_transport``)과 동일한 패턴을 각 N마다 반복해 상태를
격리한다(N마다 새 tmp 디렉터리 + 새 Manager 앱 인스턴스 - 이전 N의 디바이스
등록/DB 상태가 다음 N 측정에 영향을 주지 않도록).

처리량(auth ops/sec) 산식: ``auth_ops_per_sec = auth phase 성공 횟수 /
전체 플릿 실행 wall_time_s``. fleet은 단계별 wall-clock을 별도로 기록하지
않으므로(enroll->auth->telemetry가 디바이스별로 순차, 디바이스 간에는 동시
실행) 이 지표는 "인증 단계만의" 순수 처리량이 아니라 "인증을 포함한 전체
수명주기 처리량의 근사치"다. 이 근사는 docs/perf/PERFORMANCE_REPORT.md의
"한계·전제"에 명시한다.

실행: ``python bench/run_bench.py [--sizes 10,25,50,100,200] [--concurrency 10]``
결과: ``bench/results/bench_YYYYMMDD_HHMMSS.json``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from eam.manager.app import create_app  # noqa: E402
from eam.simulator.fleet import run_fleet  # noqa: E402

DEFAULT_SIZES = [10, 25, 50, 100, 200]
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _build_inprocess_app(tmp_dir: Path):
    """N번째 실행을 위한 완전히 격리된 Manager 앱 인스턴스를 인프로세스로 생성.

    ``eam.simulator.fleet._build_inprocess_transport``와 동일한 패턴
    (AUTO_APPROVE=true로 즉시 승인, ASGITransport로 실네트워크 우회)을 따르되,
    N마다 새 tmp 디렉터리를 사용해 DB/인증서 상태를 완전히 분리한다.
    """
    os.environ["AUTO_APPROVE"] = "true"
    app = create_app(
        certs_dir=tmp_dir / "manager_certs",
        store_db_path=tmp_dir / "eam.db",
        audit_db_path=tmp_dir / "eam_audit.db",
    )
    transport = httpx.ASGITransport(app=app)
    bootstrap_token = str(app.state.bootstrap_token)
    return app, transport, bootstrap_token


def run_one_size(
    n: int, concurrency: int, telemetry_per_device: int
) -> Dict[str, Any]:
    """N대에 대해 격리된 Manager 앱을 새로 띄우고 fleet을 실행, 결과 dict 반환."""
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"eam-bench-n{n}-"))
    _app, transport, bootstrap_token = _build_inprocess_app(tmp_dir)

    t0 = time.perf_counter()
    result = asyncio.run(
        run_fleet(
            n,
            "http://in-process.eam.local",
            concurrency,
            telemetry_per_device,
            transport=transport,
            bootstrap_token=bootstrap_token,
            certs_dir_root=tmp_dir / "device_certs",
        )
    )
    elapsed = time.perf_counter() - t0

    auth_stats = result.phase_stats["auth"]
    auth_ops_per_sec = auth_stats.count / result.wall_time_s if result.wall_time_s > 0 else 0.0

    entry = result.to_dict()
    entry["auth_ops_per_sec"] = auth_ops_per_sec
    entry["measurement_wall_time_s"] = elapsed
    # 에러 메시지는 로그 폭주를 막기 위해 앞 5개만 보존.
    entry["errors"] = entry["errors"][:5]
    return entry


def run_sweep(
    sizes: Sequence[int], concurrency: int, telemetry_per_device: int
) -> Dict[str, Any]:
    runs: List[Dict[str, Any]] = []
    sweep_start = time.perf_counter()
    for n in sizes:
        print(f"[run_bench] N={n} concurrency={concurrency} 실행 중...", file=sys.stderr)
        entry = run_one_size(n, concurrency, telemetry_per_device)
        print(
            f"[run_bench] N={n}: auth p50={entry['phase_stats']['auth']['p50']:.1f}ms "
            f"p95={entry['phase_stats']['auth']['p95']:.1f}ms "
            f"throughput={entry['auth_ops_per_sec']:.2f}/s "
            f"success={entry['success_count']}/{n}",
            file=sys.stderr,
        )
        runs.append(entry)
    sweep_elapsed = time.perf_counter() - sweep_start

    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "sizes": list(sizes),
        "concurrency": concurrency,
        "telemetry_per_device": telemetry_per_device,
        "sweep_wall_time_s": sweep_elapsed,
        "machine": {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "runs": runs,
    }


def _parse_sizes(raw: str) -> List[int]:
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def main(argv: Optional[Sequence[str]] = None) -> Path:
    parser = argparse.ArgumentParser(
        description="N-스윕 성능 벤치마크 (in-process ASGI, 실네트워크/Docker 없음)"
    )
    parser.add_argument(
        "--sizes",
        type=_parse_sizes,
        default=DEFAULT_SIZES,
        help="쉼표로 구분된 디바이스 수 목록 (기본 10,25,50,100,200)",
    )
    parser.add_argument("--concurrency", type=int, default=10, help="동시 실행 수 (기본 10)")
    parser.add_argument(
        "--telemetry-per-device",
        type=int,
        default=1,
        help="디바이스당 텔레메트리 전송 횟수 (기본 1 - 인증 처리량 측정에 집중)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=RESULTS_DIR,
        help="결과 JSON을 저장할 디렉터리 (기본 bench/results)",
    )
    args = parser.parse_args(argv)

    summary = run_sweep(args.sizes, args.concurrency, args.telemetry_per_device)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = args.output_dir / f"bench_{ts}.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[run_bench] 결과 저장: {out_path}", file=sys.stderr)
    return out_path


if __name__ == "__main__":
    main()
