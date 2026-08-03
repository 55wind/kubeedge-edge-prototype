"""bench/run_bench.py: N-스윕 성능 벤치마크 CLI.

각 N에 대해 Manager FastAPI 앱을 인프로세스(``httpx.ASGITransport``, 실제 소켓
없음)로 새로 띄우고 두 가지를 측정한다:

1. **전체 수명주기(enroll->auth->telemetry)** - ``eam.simulator.fleet.run_fleet``
   재사용. enroll 단계는 디바이스가 스스로 키를 생성하는 현실적인 CPU 비용을
   포함하므로(``EdgeAgent.enroll``이 ``asyncio.to_thread``로 오프로드 - 이제
   이벤트 루프를 막지 않는다) 지연시간 분포(p50/p95/p99)를 있는 그대로 보고한다.
2. **인증(auth) 전용 버스트** - 이미 발급된 인증서를 가진 디바이스들이 동시에
   ``POST /auth/token``만 호출하는 별도 측정(``_run_auth_only_storm``). 클라이언트
   측 키 생성 비용이 전혀 섞이지 않은, 서버(Manager 단일 워커)의 순수 인증 처리
   용량을 측정하기 위함이다 - 1,000기 외삽 모델(``bench/model.py``)의 서비스율
   μ는 **이 수치**(``auth_only_ops_per_sec``)로 적합한다.

과거 버전(이 수정 전)은 μ를 "auth phase 성공 횟수 / 전체 플릿 실행
wall_time_s"로 근사했는데, 그 시절 enroll()이 이벤트 루프 위에서 동기적으로
RSA-2048 키 생성을 수행해(Semaphore(10)가 있어도 실질적 동시성이 없었음)
측정된 처리량 상한이 "Manager의 인증 처리 용량"이 아니라 "벤치마크 하네스
자신의 (사실상 직렬화된) 키 생성 속도"를 반영하는 문제가 있었다. 이번 수정으로
(a) enroll의 키 생성을 스레드로 내보내고 (b) μ 적합 대상을 인증 전용 버스트로
분리해 이 문제를 해소했다 - 자세한 내용은 docs/perf/PERFORMANCE_REPORT.md §5.

Manager 앱은 N마다 새 tmp 디렉터리로 완전히 새로 띄운다(이전 N의 디바이스
등록/DB 상태가 다음 N 측정에 영향을 주지 않도록).

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
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from eam.agent.agent import EdgeAgent  # noqa: E402
from eam.manager.app import create_app  # noqa: E402
from eam.simulator.fleet import _phase_stats, run_fleet  # noqa: E402

DEFAULT_SIZES = [10, 25, 50, 100, 200]
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# AUTO_APPROVE는 이 프로세스 전체(모든 N에 대해)에서 동일하게 필요한 환경설정이라
# 루프 밖에서 한 번만 설정한다(과거에는 N마다 반복 설정 - 부작용은 없었지만
# "매 반복 설정해야만 하는 것"처럼 오독될 여지가 있어 정리).
os.environ["AUTO_APPROVE"] = "true"


def _build_inprocess_app(tmp_dir: Path):
    """N번째 실행을 위한 완전히 격리된 Manager 앱 인스턴스를 인프로세스로 생성.

    ``eam.simulator.fleet._build_inprocess_transport``와 동일한 패턴
    (ASGITransport로 실네트워크 우회)을 따르되, N마다 새 tmp 디렉터리를 사용해
    DB/인증서 상태를 완전히 분리한다.
    """
    app = create_app(
        certs_dir=tmp_dir / "manager_certs",
        store_db_path=tmp_dir / "eam.db",
        audit_db_path=tmp_dir / "eam_audit.db",
    )
    transport = httpx.ASGITransport(app=app)
    bootstrap_token = str(app.state.bootstrap_token)
    return app, transport, bootstrap_token


async def _run_auth_only_storm(
    transport: httpx.AsyncBaseTransport,
    bootstrap_token: str,
    n: int,
    concurrency: int,
    certs_root: Path,
) -> Tuple[float, List[float]]:
    """N대를 먼저 enroll(설정 단계, 미측정)한 뒤, ``POST /auth/token``만 동시에
    쏘는 버스트를 실행해 그 wall-clock과 개별 지연시간(ms)을 반환한다.

    이 결과는 클라이언트 키 생성 비용이 전혀 섞이지 않은 서버 인증 처리 능력을
    반영한다 - 이미 인증서가 발급된 디바이스가 토큰만 재요청하는 "인증 스톰"
    상황과 정확히 대응된다(도착 즉시 토큰 캐시가 비어 있으므로 매 호출이 실제로
    ``/auth/token``을 때린다).
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def _make_and_enroll(i: int) -> EdgeAgent:
        device_id = f"authburst-{i:05d}"
        agent = EdgeAgent(
            device_id=device_id,
            site="bench-auth-site",
            group="bench-auth-group",
            manager_url="http://in-process.eam.local",
            certs_dir=certs_root / device_id,
            transport=transport,
        )
        async with semaphore:
            await agent.enroll(bootstrap_token)
        return agent

    agents = await asyncio.gather(*[_make_and_enroll(i) for i in range(n)])

    latencies_ms: List[float] = [0.0] * n

    async def _auth_one(i: int, agent: EdgeAgent) -> None:
        async with semaphore:
            t0 = time.perf_counter()
            await agent.get_token()
            latencies_ms[i] = (time.perf_counter() - t0) * 1000.0

    burst_start = time.perf_counter()
    await asyncio.gather(*[_auth_one(i, a) for i, a in enumerate(agents)])
    wall_s = time.perf_counter() - burst_start

    await asyncio.gather(*[a.aclose() for a in agents])
    return wall_s, latencies_ms


def run_one_size(
    n: int, concurrency: int, telemetry_per_device: int
) -> Dict[str, Any]:
    """N대에 대해 격리된 Manager 앱을 새로 띄우고 전체 수명주기 + 인증 전용
    버스트를 실행, 결과 dict를 반환한다."""
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

    # 참고용(§5에 명시): 전체 수명주기(enroll+auth+telemetry) wall-clock을
    # 분모로 쓴 근사치 - 클라이언트 키 생성 비용이 섞여 있어 외삽 모델의 μ
    # 적합에는 쓰지 않는다.
    auth_stats = result.phase_stats["auth"]
    full_lifecycle_ops_per_sec = (
        auth_stats.count / result.wall_time_s if result.wall_time_s > 0 else 0.0
    )

    auth_burst_wall_s, auth_burst_latencies_ms = asyncio.run(
        _run_auth_only_storm(
            transport, bootstrap_token, n, concurrency, tmp_dir / "auth_burst_certs"
        )
    )
    auth_only_ops_per_sec = n / auth_burst_wall_s if auth_burst_wall_s > 0 else 0.0

    entry = result.to_dict()
    entry["full_lifecycle_ops_per_sec"] = full_lifecycle_ops_per_sec
    entry["auth_only_ops_per_sec"] = auth_only_ops_per_sec
    entry["auth_only_wall_time_s"] = auth_burst_wall_s
    entry["auth_only_latency_ms"] = _phase_stats(auth_burst_latencies_ms).to_dict()
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
            f"[run_bench] N={n}: auth(전체 수명주기) p50={entry['phase_stats']['auth']['p50']:.1f}ms "
            f"p95={entry['phase_stats']['auth']['p95']:.1f}ms | "
            f"auth-only 버스트 p95={entry['auth_only_latency_ms']['p95']:.1f}ms "
            f"throughput={entry['auth_only_ops_per_sec']:.2f}/s "
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
