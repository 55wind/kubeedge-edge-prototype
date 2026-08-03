"""bench/report.py: 최신 벤치마크 JSON -> 한국어 성능 리포트(MD) + PNG 차트 2개.

``bench/results/bench_*.json`` 중 가장 최신 파일을 읽어:
  - N별 enroll/auth/telemetry 지연시간(p50/p95/p99/mean/max) 표 (전체 수명주기,
    참고용 - enroll에는 디바이스 키 생성 비용이 포함됨)
  - N별 인증 전용(auth-only) 버스트 처리량/지연시간 표 - 클라이언트 키 생성
    비용이 섞이지 않은 서버(Manager 단일 워커) 순수 인증 처리 능력
  - ``bench.model.build_extrapolation``으로 1,000기 외삽 결과 (μ는 auth-only
    버스트 처리량으로 적합)
  - PNG 2개: N별 auth-only p95 latency, N별 auth-only 처리량
을 ``docs/perf/PERFORMANCE_REPORT.md`` + ``docs/perf/*.png``로 렌더링한다.

matplotlib은 non-interactive Agg 백엔드를 사용하고(헤드리스 환경 대응),
한글 폰트 문제를 피하기 위해 PNG 축 라벨은 영어로 작성한다(한국어 설명은
MD 본문에만 포함).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from bench.model import build_extrapolation  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"
DOCS_PERF_DIR = Path(__file__).resolve().parent.parent / "docs" / "perf"

TARGET_DEVICES = 1000
WINDOW_S = 60.0  # 1,000기가 정전 복구 등으로 60초 내 동시 재인증한다고 가정
TARGET_P95_S = 1.0  # SLA 목표: 대기시간 p95 < 1초


def find_latest_result(results_dir: Path = RESULTS_DIR) -> Path:
    candidates = sorted(results_dir.glob("bench_*.json"))
    if not candidates:
        raise FileNotFoundError(
            f"{results_dir}에 bench_*.json이 없습니다. 먼저 run_bench.py를 실행하세요."
        )
    return candidates[-1]


def load_result(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _throughput_points(data: Dict[str, Any]) -> List[tuple]:
    """μ 적합용 (N, 처리량) 포인트 - auth-only 버스트 처리량을 사용한다.

    ``auth_only_ops_per_sec``는 클라이언트 키 생성 비용이 섞이지 않은, 이미
    발급된 인증서로 ``POST /auth/token``만 동시에 호출한 결과이므로 서버
    (Manager 단일 워커)의 순수 인증 처리 용량을 반영한다.
    """
    return [(run["n"], run["auth_only_ops_per_sec"]) for run in data["runs"]]


def render_charts(data: Dict[str, Any], out_dir: Path) -> tuple:
    out_dir.mkdir(parents=True, exist_ok=True)
    runs = data["runs"]
    ns = [r["n"] for r in runs]
    p95s = [r["auth_only_latency_ms"]["p95"] for r in runs]
    throughputs = [r["auth_only_ops_per_sec"] for r in runs]

    p95_path = out_dir / "p95_latency_vs_n.png"
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(ns, p95s, marker="o", color="#2563eb")
    ax.set_xlabel("Number of devices (N)")
    ax.set_ylabel("Auth-only p95 latency (ms)")
    ax.set_title("Auth-only (POST /auth/token) p95 latency vs N")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(p95_path, dpi=150)
    plt.close(fig)

    throughput_path = out_dir / "throughput_vs_n.png"
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(ns, throughputs, marker="o", color="#16a34a")
    ax.set_xlabel("Number of devices (N)")
    ax.set_ylabel("Auth-only throughput (ops/sec)")
    ax.set_title("Auth-only (POST /auth/token) throughput vs N")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(throughput_path, dpi=150)
    plt.close(fig)

    return p95_path, throughput_path


def _fmt_wait(seconds: float) -> str:
    if seconds == float("inf"):
        return "발산(무한대)"
    return f"{seconds * 1000:.1f} ms"


def render_markdown(
    data: Dict[str, Any], extrapolation: Dict[str, Any], p95_png: Path, throughput_png: Path
) -> str:
    lines: List[str] = []
    lines.append("# 성능 벤치마크 리포트")
    lines.append("")
    lines.append(f"- 실행 시각: {data['timestamp']}")
    lines.append(
        f"- 측정 대상 N: {', '.join(str(n) for n in data['sizes'])} "
        f"(클라이언트 동시성 concurrency={data['concurrency']}, "
        f"디바이스당 텔레메트리 {data['telemetry_per_device']}회)"
    )
    lines.append(
        f"- 측정 환경: Python {data['machine']['python_version']}, "
        f"CPU 코어 {data['machine']['cpu_count']}개, {data['machine']['platform']}"
    )
    lines.append(
        f"- 전체 스윕 소요 시간: {data['sweep_wall_time_s']:.1f}s "
        "(in-process ASGI - 실네트워크/Docker 미사용)"
    )
    lines.append("")

    lines.append("## 1. N별 단계별 지연시간 (ms) - 전체 수명주기 (참고용)")
    lines.append("")
    lines.append(
        "enroll 단계에는 디바이스가 스스로 RSA-2048 키를 생성하는 현실적인 CPU "
        "비용이 포함돼 있다(`EdgeAgent.enroll()`이 `asyncio.to_thread`로 오프로드해 "
        "이벤트 루프는 막지 않지만, 그 자체의 소요 시간은 여전히 존재). 이 표는 "
        "있는 그대로의 수명주기 체감 지연시간 참고용이며, **1,000기 외삽 모델의 "
        "μ 적합에는 사용하지 않는다**(§2·§4·§5 참고)."
    )
    lines.append("")
    lines.append(
        "| N | 단계 | count | p50 | p95 | p99 | mean | max |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for run in data["runs"]:
        n = run["n"]
        for phase in ("enroll", "auth", "telemetry"):
            s = run["phase_stats"][phase]
            lines.append(
                f"| {n} | {phase} | {s['count']} | {s['p50']:.1f} | {s['p95']:.1f} | "
                f"{s['p99']:.1f} | {s['mean']:.1f} | {s['max']:.1f} |"
            )
    lines.append("")

    lines.append("## 2. N별 인증 전용(auth-only) 버스트 - 외삽 모델의 기반 지표")
    lines.append("")
    lines.append(
        "**측정 방법**: N대를 먼저 enroll(인증서 발급, 측정 대상 아님)한 뒤, 이미 "
        "발급된 인증서로 `POST /auth/token`만 동시에 호출하는 별도 버스트를 "
        "실행한다(`bench/run_bench.py:_run_auth_only_storm`). 클라이언트 키 생성 "
        "비용이 전혀 섞이지 않으므로, 관측된 처리량은 Manager 단일 워커(단일 "
        "프로세스/단일 이벤트 루프)의 순수 인증 처리 용량(인증서 체인 검증 + "
        "RS256 JWT 서명, ASGI 오버헤드 포함)을 반영한다."
    )
    lines.append("")
    lines.append(
        "처리량 산식: `auth_only_ops_per_sec = N / auth-only 버스트 wall_time_s` "
        "(버스트 wall_time은 N개 `/auth/token` 호출이 모두 끝날 때까지의 시간 - "
        "키 생성이 섞인 §1의 전체 수명주기 wall_time과는 다른 측정치)."
    )
    lines.append("")
    lines.append(
        "| N | auth-only wall_time_s | auth-only p50 | auth-only p95 | auth-only ops/sec | "
        "(참고) 전체 수명주기 ops/sec |"
    )
    lines.append("|---|---|---|---|---|---|")
    for run in data["runs"]:
        a = run["auth_only_latency_ms"]
        lines.append(
            f"| {run['n']} | {run['auth_only_wall_time_s']:.3f} | {a['p50']:.1f} | "
            f"{a['p95']:.1f} | {run['auth_only_ops_per_sec']:.2f} | "
            f"{run['full_lifecycle_ops_per_sec']:.2f} |"
        )
    lines.append("")
    lines.append(
        "참고 열(전체 수명주기 ops/sec)은 §5-①에서 설명하는 과거 버전의 근사치를 "
        "비교용으로 남긴 것으로, 외삽에는 쓰이지 않는다."
    )
    lines.append("")

    lines.append("## 3. 차트 (auth-only 버스트 기준)")
    lines.append("")
    lines.append(f"![auth-only p95 latency vs N]({p95_png.name})")
    lines.append("")
    lines.append(f"![auth-only throughput vs N]({throughput_png.name})")
    lines.append("")

    lines.append("## 4. 1,000기 외삽 모델 결과")
    lines.append("")
    mu = extrapolation["mu_per_worker_auth_per_s"]
    lines.append(
        f"- 워커(프로세스) 1개당 실측 서비스율(μ) 추정치: **{mu:.2f} auth/s** "
        f"(§2 auth-only 버스트 처리량 중 최댓값을 워커 "
        f"{extrapolation['assumed_benchmark_workers']}개 기준으로 나눔 - 클라이언트 키 "
        "생성 비용이 섞이지 않은, in-process 단일 워커 Manager의 순수 인증 처리 "
        "용량 + ASGI 오버헤드를 측정한 값. §5-① 참고)"
    )
    lines.append(
        f"- 시나리오: {extrapolation['target_devices']}대 디바이스가 "
        f"{extrapolation['window_s']:.0f}초 내 동시 재인증(포아송 도착 근사, M/M/c 대기행렬)"
    )
    lines.append(f"- 목표 SLA: 대기시간(Wq) p95 < {extrapolation['target_p95_s']:.1f}초")
    lines.append("")

    single = extrapolation["single_worker_projection"]
    lines.append("### 4.1 단일 워커(c=1) 기준 예측")
    lines.append("")
    lines.append(f"- 유틸라이제이션(ρ) = {single['rho']:.2f} (안정 여부: {single['stable']})")
    lines.append(f"- 평균 대기시간(Wq): {_fmt_wait(single['mean_wait_s'])}")
    lines.append(f"- p95 대기시간: {_fmt_wait(single['p95_wait_s'])}")
    lines.append(f"- p99 대기시간: {_fmt_wait(single['p99_wait_s'])}")
    lines.append(f"- 최대 대기시간(근사, N개 중 최댓값): {_fmt_wait(single['max_wait_estimate_s'])}")
    lines.append(f"- 지속 가능 처리량: {single['sustainable_auth_per_sec']:.2f} auth/s")
    lines.append("")

    recommended_c = extrapolation["recommended_replicas"]
    chosen = extrapolation["recommended_projection"]
    lines.append("### 4.2 권장 레플리카 수 및 예측")
    lines.append("")
    if recommended_c is not None:
        lines.append(f"- 목표 SLA(p95<{extrapolation['target_p95_s']:.1f}s) 충족 최소 레플리카 수: **{recommended_c}대**")
    else:
        lines.append("- 탐색 범위 내에서 목표 SLA를 충족하는 레플리카 수를 찾지 못함")
    lines.append(f"- 해당 구성에서 평균 대기시간: {_fmt_wait(chosen['mean_wait_s'])}")
    lines.append(f"- 해당 구성에서 p95 대기시간: {_fmt_wait(chosen['p95_wait_s'])}")
    lines.append(f"- 해당 구성에서 최대 대기시간(근사): {_fmt_wait(chosen['max_wait_estimate_s'])}")
    lines.append(f"- 해당 구성에서 지속 가능 처리량: {chosen['sustainable_auth_per_sec']:.2f} auth/s")
    lines.append("")
    lines.append(f"**병목 판단**: {extrapolation['bottleneck_verdict']}")
    lines.append("")

    poly = extrapolation["poly_sanity_check"]
    lines.append("### 4.3 다항 최소제곱 적합 (보조 검산)")
    lines.append("")
    lines.append(
        "M/M/c 외삽과 별개로, 실측 (N, auth-only 처리량) 포인트에 2차 다항식을 "
        "최소제곱으로 적합해 추세가 M/M/c의 포화 가정과 어긋나지 않는지 교차 "
        "확인합니다."
    )
    lines.append("")
    lines.append(f"- 적합 계수(최고차항부터): {[round(c, 6) for c in poly['coeffs']]}")
    lines.append(
        f"- 측정 최대 N에서 다항식 예측 처리량: {poly['predicted_throughput_at_max_measured_n']:.2f} auth/s"
    )
    lines.append(
        f"- N=1000 외삽 시 다항식 예측 처리량: {poly['predicted_throughput_at_1000']:.2f} auth/s "
        "(참고용 - 다항 외삽은 N이 측정 범위를 크게 벗어나면 신뢰도가 낮아짐)"
    )
    lines.append("")

    lines.append("## 5. 한계 및 전제")
    lines.append("")
    lines.append(
        "① **(수정 이력) 과거 μ 추정치는 서버가 아니라 하네스 자신의 병목을 "
        "측정한 것이었음**: 이 리포트의 이전 버전은 μ를 \"auth 단계 성공 횟수 / "
        "플릿 전체 실행 wall_time_s\"(전체 수명주기 wall-clock을 분모로 사용)로 "
        "추정했다. 그런데 당시 `EdgeAgent.enroll()`은 CSR/RSA-2048 키 생성을 "
        "이벤트 루프 위에서 **동기적으로** 수행했고, 이는 CPU-bound 작업이라 "
        "`asyncio.Semaphore(concurrency)`로 동시성을 열어줘도 실질적으로는 한 "
        "코루틴의 키 생성이 끝나야 다음 코루틴이 진행되는 사실상 직렬 실행이었다. "
        "따라서 그 μ 상한은 \"Manager의 인증 처리 용량\"이 아니라 \"벤치마크 "
        "하네스 자신의 (사실상 직렬화된) 클라이언트 키 생성 속도\"를 측정한 "
        "것이었고, 여기서 도출된 \"레플리카 4대\" 같은 서버 확장 권고는 "
        "클라이언트 측 아티팩트에 대한 처방이었다는 점에서 부정확했다. 이번 "
        "수정으로 (a) `EdgeAgent.enroll()`의 키 생성을 `asyncio.to_thread`로 "
        "오프로드해 이벤트 루프를 막지 않게 하고(현실적으로도 타당함 - 실제 "
        "디바이스들은 각자 독립적으로/병렬로 키를 생성하지, 하나의 루프 뒤에서 "
        "직렬화되지 않는다), (b) μ 적합 대상을 §2의 인증 전용(auth-only) 버스트 "
        "처리량으로 분리했다. **현재 μ(§4)는 이미 인증서가 발급된 디바이스들이 "
        "동시에 `POST /auth/token`만 호출할 때의 in-process 단일 워커(단일 "
        "프로세스) Manager의 순수 인증 처리 용량 - 인증서 체인 검증 + RS256 JWT "
        "서명 연산 + ASGI 오버헤드를 포함하되 클라이언트 키 생성/실네트워크/uvicorn "
        "다중 워커 오버헤드는 제외 - 을 측정한다.**"
    )
    lines.append(
        "② **레플리카 확장 권고의 전제**: §4.2의 \"N대 레플리카로 확장\" 논리는 "
        "①에서 측정한 서버 측(§2 auth-only) 병목에 대한 처방이며, 클라이언트(각 "
        "디바이스)의 키 생성은 서버와 독립적으로 병렬 수행된다고 가정한다(실제로 "
        "디바이스마다 별도의 CPU/코어를 갖는 임베디드 환경에서는 합리적인 가정). "
        "만약 클라이언트 키 생성이 여전히 병목이라면(예: 저사양 임베디드 디바이스) "
        "레플리카 확장만으로는 체감 지연시간이 개선되지 않는다 - 이 경우 §1의 "
        "전체 수명주기 enroll 지연시간을 별도로 참고해야 한다."
    )
    lines.append(
        "③ **벤치마크 환경**: 실제 uvicorn 다중 워커/네트워크가 아닌 단일 프로세스 "
        "in-process ASGI(`httpx.ASGITransport`)로 측정 - 실네트워크 지연/컨테이너 "
        "오버헤드는 반영되지 않음."
    )
    lines.append(
        "④ **M/M/c 가정**: 도착은 포아송 과정, 서비스시간은 지수분포라고 근사. "
        "실제 정전 복구 등 인증 스톰은 도착이 더 뭉칠 수 있어(버스트) 실제 대기시간이 "
        "모델 예측보다 클 수 있음."
    )
    lines.append(
        "⑤ **선형 확장 가정**: c개 레플리카로 확장 시 총 서비스율이 c*μ로 선형 증가한다고 "
        "가정 - 실제로는 공유 DB(SQLite)/락 경합으로 선형보다 낮을 수 있음(SQLite는 "
        "다중 프로세스 동시 쓰기에 제약이 있어 운영 환경에서는 레플리카별 DB 분리 또는 "
        "다른 저장소로의 전환이 별도로 검토돼야 함)."
    )
    lines.append(
        "⑥ **최대 대기시간 근사**: 이론적 상한이 아니라 N개 표본 중 최댓값을 "
        "`(1 - 1/N)` 분위수로 근사한 값."
    )
    lines.append(
        "⑦ **numpy 사용 고지**: 이 프로젝트의 허용 패키지 목록에는 numpy가 명시돼 있지 "
        "않지만, matplotlib의 필수 의존성으로 이미 설치돼 있어 다항 최소제곱 적합에서만 "
        "제한적으로 사용함(`bench/model.py` 참고)."
    )
    lines.append("")

    return "\n".join(lines)


def main() -> Path:
    latest = find_latest_result()
    data = load_result(latest)
    points = _throughput_points(data)

    extrapolation = build_extrapolation(
        points,
        target_devices=TARGET_DEVICES,
        window_s=WINDOW_S,
        target_p95_s=TARGET_P95_S,
    )

    p95_png, throughput_png = render_charts(data, DOCS_PERF_DIR)
    markdown = render_markdown(data, extrapolation, p95_png, throughput_png)

    DOCS_PERF_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DOCS_PERF_DIR / "PERFORMANCE_REPORT.md"
    out_path.write_text(markdown, encoding="utf-8")

    print(f"[report] 소스 JSON: {latest}")
    print(f"[report] 리포트 작성: {out_path}")
    print(f"[report] 차트: {p95_png}, {throughput_png}")
    return out_path


if __name__ == "__main__":
    main()
