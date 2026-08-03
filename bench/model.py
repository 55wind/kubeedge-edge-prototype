"""1,000기 외삽 모델: 순수 함수 모음 (파일 I/O 없음 - 단위테스트 용이성 목적).

측정된 소규모(N<=수백) 벤치마크 결과를 입력받아 1,000대 디바이스가 동시에
(콜드스타트/정전 복구 등으로) 재인증을 시도하는 "인증 스톰(auth storm)"
시나리오를 M/M/c 대기행렬 근사(Erlang C)로 외삽한다.

핵심 가정(문서화 - docs/perf/PERFORMANCE_REPORT.md의 "한계·전제" 절 참고):
  1. 인증 요청 도착은 포아송 과정(Poisson arrival)을 따른다고 근사한다
     (실제로는 디바이스 재부팅 타이밍이 뭉칠 수 있어 버스트가 더 심할 수 있음).
  2. 서비스시간은 지수분포(memoryless)라고 근사한다 (M/M/c의 표준 가정).
  3. 벤치마크 프로세스 1개는 "워커 1개"에 해당하며, 실측 최대 처리량을
     그대로 워커 1개의 서비스율 mu로 사용한다(``assumed_benchmark_workers``로
     조정 가능). 운영 환경에서 c개의 동일 워커/레플리카로 수평 확장한다고
     가정해 총 서비스율을 ``c * mu``로 모델링한다(선형 확장 가정 - 실제로는
     공유 DB/락 경합으로 선형보다 낮을 수 있음).
  4. "최대 대기시간"은 이론적 상한이 아니라, N개 표본 중 최댓값의 근사치로
     ``(1 - 1/N)`` 분위수를 사용한다(극값통계의 대략적 근사).

numpy 사용 고지: numpy는 이 프로젝트의 명시적 허용 패키지 목록에는 없지만
matplotlib의 필수 의존성으로 이미 설치되어 있어(다항 최소제곱 적합에 한해)
사용한다. 이 사실은 리포트에도 명시한다.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float]

EULER_MASCHERONI = 0.5772156649015329


# ---------------------------------------------------------------------------
# Erlang B / C (recursive form - factorial 오버플로 회피)
# ---------------------------------------------------------------------------


def erlang_b(c: int, a: float) -> float:
    """Erlang B(차단 확률) - 재귀식으로 계산해 큰 c/a에서도 오버플로가 없다.

    ``B(0, a) = 1``; ``B(n, a) = (a*B(n-1, a)) / (n + a*B(n-1, a))``.
    """
    if c < 0:
        raise ValueError("c must be >= 0")
    if a < 0:
        raise ValueError("a (offered load) must be >= 0")
    b = 1.0
    for n in range(1, c + 1):
        b = (a * b) / (n + a * b)
    return b


def erlang_c(c: int, a: float) -> float:
    """Erlang C(대기 확률). ``a >= c``(포화/불안정)이면 1.0을 반환한다.

    ``C(c,a) = c*B(c,a) / (c - a*(1 - B(c,a)))``.
    """
    if c <= 0:
        raise ValueError("c must be >= 1")
    if a >= c:
        return 1.0
    b = erlang_b(c, a)
    denom = c - a * (1.0 - b)
    if denom <= 0:
        return 1.0
    return (c * b) / denom


# ---------------------------------------------------------------------------
# M/M/c 대기시간
# ---------------------------------------------------------------------------


@dataclass
class MMCWaitResult:
    """M/M/c 대기행렬의 도착률(lam)/서비스율(mu)/서버수(c)에 따른 대기 통계."""

    lam_per_s: float
    mu_per_worker: float
    c_workers: int
    rho: float
    stable: bool
    erlang_c: float
    wq_s: float  # 평균 대기시간(큐에서 기다리는 시간)
    w_s: float  # 평균 총 체류시간(대기 + 서비스)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def mmc_wait(lam: float, mu: float, c: int) -> MMCWaitResult:
    """M/M/c 큐의 평균 대기시간(Erlang C 공식)을 계산한다.

    ``rho = lam / (c*mu) >= 1``이면 불안정(발산)이므로 ``stable=False``와 함께
    ``wq_s=w_s=inf``를 반환한다(예외를 던지지 않음 - 호출측이 명확히 판단 가능).
    """
    if mu <= 0:
        raise ValueError("mu must be > 0")
    if c <= 0:
        raise ValueError("c must be >= 1")
    if lam < 0:
        raise ValueError("lam must be >= 0")

    a = lam / mu  # 오퍼드 로드 (Erlangs)
    rho = a / c

    if rho >= 1.0:
        return MMCWaitResult(
            lam_per_s=lam,
            mu_per_worker=mu,
            c_workers=c,
            rho=rho,
            stable=False,
            erlang_c=1.0,
            wq_s=math.inf,
            w_s=math.inf,
        )

    pc = erlang_c(c, a)
    wq = pc / (c * mu - lam)
    w = wq + 1.0 / mu
    return MMCWaitResult(
        lam_per_s=lam,
        mu_per_worker=mu,
        c_workers=c,
        rho=rho,
        stable=True,
        erlang_c=pc,
        wq_s=wq,
        w_s=w,
    )


def wait_percentile(mmc: MMCWaitResult, q: float) -> float:
    """대기시간의 q-분위수(0<q<1)를 반환한다.

    대기시간 분포는 "확률 (1-Pc)로 대기 0" + "확률 Pc로 지수분포(rate=k)"의
    혼합분포다 (k = c*mu - lam). 따라서:
      F(t) = 1 - Pc * exp(-k*t)  (t>=0)
    ``F(t)=q``를 풀면 ``t = -ln((1-q)/Pc) / k`` (단, ``(1-q) <= Pc``일 때만
    유효; 그렇지 않으면 분위수가 "대기 0" 구간에 속하므로 0을 반환한다).
    """
    if not 0.0 < q < 1.0:
        raise ValueError("q must be in (0, 1)")
    if not mmc.stable:
        return math.inf
    pc = mmc.erlang_c
    if pc <= 0.0:
        return 0.0
    threshold = 1.0 - q
    if threshold >= pc:
        return 0.0
    k = mmc.c_workers * mmc.mu_per_worker - mmc.lam_per_s
    if k <= 0:
        return math.inf
    return -math.log(threshold / pc) / k


def expected_max_wait(mmc: MMCWaitResult, n_customers: int) -> float:
    """N명의 도착 고객 중 대기시간 최댓값의 근사치.

    극값통계의 대략적 근사로 ``(1 - 1/N)`` 분위수를 사용한다(N이 클수록
    최댓값 분위수가 1에 가까워짐 - 표본이 많을수록 극단값이 커진다는 직관과
    부합). 정확한 순서통계량 기댓값이 아니라 실무적 근사치임을 리포트에 명시.

    ``n_customers == 1``이면 ``q = 1 - 1/1 = 0.0``이 되어
    ``wait_percentile``의 ``0<q<1`` 전제를 벗어난다(고객이 1명뿐이면 "최댓값"은
    그 한 명의 대기시간 그 자체이므로 분위수 근사가 애초에 불필요) - 이 경우
    평균 대기시간(``mmc.wq_s``)을 그대로 반환한다.
    """
    if n_customers <= 0:
        return 0.0
    if not mmc.stable:
        return math.inf
    if n_customers == 1:
        return mmc.wq_s
    q = 1.0 - 1.0 / n_customers
    return wait_percentile(mmc, q)


# ---------------------------------------------------------------------------
# 처리량 실측 -> 서비스율 적합
# ---------------------------------------------------------------------------


def fit_service_rate(points: Sequence[Point], assumed_workers: int = 1) -> float:
    """실측 (N, auth_ops_per_sec) 포인트에서 워커 1개당 서비스율 mu를 추정.

    벤치마크는 단일 인프로세스가 곧 "워커 assumed_workers개"에 해당한다고
    가정하고, 관측된 처리량 중 최댓값(포화 처리량 추정치)을
    ``assumed_workers``로 나눠 워커 1개당 서비스율을 얻는다.
    """
    if not points:
        raise ValueError("points must not be empty")
    if assumed_workers <= 0:
        raise ValueError("assumed_workers must be >= 1")
    max_throughput = max(tp for _, tp in points)
    if max_throughput <= 0:
        raise ValueError("observed throughput must be > 0")
    return max_throughput / assumed_workers


def polyfit_throughput(points: Sequence[Point], degree: int = 2) -> List[float]:
    """N->처리량 다항 최소제곱 적합 (2차 보조 확인용).

    numpy는 matplotlib의 의존성으로 이미 설치돼 있어 사용한다(모듈 docstring
    참고). 반환값은 numpy.polyfit과 동일한 순서(최고차항부터)의 float 리스트.
    """
    import numpy as np  # matplotlib 의존성으로 이미 설치됨 - 리포트에 고지

    if len(points) < 2:
        raise ValueError("need at least 2 points to fit a polynomial")
    degree = min(degree, len(points) - 1)
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    coeffs = np.polyfit(xs, ys, degree)
    return [float(v) for v in coeffs]


def polyval(coeffs: Sequence[float], x: float) -> float:
    """다항식 계수(``polyfit_throughput`` 출력)와 x로 y를 계산."""
    import numpy as np

    return float(np.polyval(list(coeffs), x))


# ---------------------------------------------------------------------------
# 1,000기 외삽 프로젝션
# ---------------------------------------------------------------------------


@dataclass
class ProjectionResult:
    """N대 디바이스가 window_s초 동안 동시 인증을 시도할 때의 예측 결과."""

    target_devices: int
    window_s: float
    mu_per_worker: float
    c_workers: int
    lam_per_s: float
    rho: float
    stable: bool
    erlang_c: float
    mean_wait_s: float
    p95_wait_s: float
    p99_wait_s: float
    max_wait_estimate_s: float
    mean_total_latency_s: float
    sustainable_auth_per_sec: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def project_auth_storm(
    target_devices: int, window_s: float, mu_per_worker: float, c_workers: int
) -> ProjectionResult:
    """target_devices대가 window_s초에 걸쳐 균등 도착(포아송 근사)한다고 볼 때
    M/M/c(``c_workers``개 워커, 워커당 서비스율 ``mu_per_worker``)의 대기 예측.
    """
    if window_s <= 0:
        raise ValueError("window_s must be > 0")
    lam = target_devices / window_s
    mmc = mmc_wait(lam, mu_per_worker, c_workers)
    sustainable = c_workers * mu_per_worker

    if not mmc.stable:
        return ProjectionResult(
            target_devices=target_devices,
            window_s=window_s,
            mu_per_worker=mu_per_worker,
            c_workers=c_workers,
            lam_per_s=lam,
            rho=mmc.rho,
            stable=False,
            erlang_c=mmc.erlang_c,
            mean_wait_s=math.inf,
            p95_wait_s=math.inf,
            p99_wait_s=math.inf,
            max_wait_estimate_s=math.inf,
            mean_total_latency_s=math.inf,
            sustainable_auth_per_sec=sustainable,
        )

    p95 = wait_percentile(mmc, 0.95)
    p99 = wait_percentile(mmc, 0.99)
    max_est = expected_max_wait(mmc, target_devices)
    return ProjectionResult(
        target_devices=target_devices,
        window_s=window_s,
        mu_per_worker=mu_per_worker,
        c_workers=c_workers,
        lam_per_s=lam,
        rho=mmc.rho,
        stable=True,
        erlang_c=mmc.erlang_c,
        mean_wait_s=mmc.wq_s,
        p95_wait_s=p95,
        p99_wait_s=p99,
        max_wait_estimate_s=max_est,
        mean_total_latency_s=mmc.w_s,
        sustainable_auth_per_sec=sustainable,
    )


def recommend_replica_count(
    target_devices: int,
    window_s: float,
    mu_per_worker: float,
    target_p95_s: float,
    max_c: int = 64,
) -> Optional[int]:
    """목표 p95 대기시간(``target_p95_s``)을 만족하는 최소 워커/레플리카 수.

    ``max_c``까지 탐색해도 만족하는 c가 없으면 ``None``을 반환한다(호출측이
    "확장으로 해결 불가"로 판단할 수 있게).
    """
    if max_c <= 0:
        raise ValueError("max_c must be >= 1")
    for c in range(1, max_c + 1):
        proj = project_auth_storm(target_devices, window_s, mu_per_worker, c)
        if proj.stable and proj.p95_wait_s < target_p95_s:
            return c
    return None


def bottleneck_verdict(recommended_replicas: Optional[int], max_c: int) -> str:
    """추천 레플리카 수를 바탕으로 병목/확장성에 대한 한국어 판단 문장 생성."""
    if recommended_replicas is None:
        return (
            f"레플리카를 {max_c}대까지 늘려도 목표 SLA를 만족하지 못함 - "
            "인증 처리 로직(인증서 서명/키 연산) 자체의 최적화 또는 아키텍처 "
            "재검토가 필요."
        )
    if recommended_replicas == 1:
        return "단일 인스턴스로도 1,000기 동시 인증 스톰의 목표 SLA를 충족 가능."
    return (
        f"{recommended_replicas}대의 Manager 레플리카로 수평 확장하면 목표 SLA를 "
        "충족 가능 - 병목은 워커(프로세스)당 인증 처리 용량(서명/검증 연산)."
    )


def build_extrapolation(
    throughput_points: Sequence[Point],
    *,
    target_devices: int = 1000,
    window_s: float = 60.0,
    target_p95_s: float = 1.0,
    assumed_benchmark_workers: int = 1,
    max_replica_search: int = 64,
) -> Dict[str, Any]:
    """벤치마크 실측 (N, auth_ops_per_sec) 포인트 -> 1,000기 외삽 결과 dict.

    파일 I/O 없이 입력값만으로 계산하는 순수 함수 (report.py가 JSON 로딩 후
    호출).
    """
    mu = fit_service_rate(throughput_points, assumed_benchmark_workers)

    poly_coeffs = polyfit_throughput(throughput_points, degree=2)
    max_n = max(n for n, _ in throughput_points)
    poly_sanity = {
        "coeffs": poly_coeffs,
        "predicted_throughput_at_max_measured_n": polyval(poly_coeffs, max_n),
        "predicted_throughput_at_1000": polyval(poly_coeffs, target_devices),
    }

    baseline = project_auth_storm(target_devices, window_s, mu, 1)
    recommended = recommend_replica_count(
        target_devices, window_s, mu, target_p95_s, max_replica_search
    )
    chosen_c = recommended if recommended is not None else max_replica_search
    chosen_projection = project_auth_storm(target_devices, window_s, mu, chosen_c)
    verdict = bottleneck_verdict(recommended, max_replica_search)

    return {
        "mu_per_worker_auth_per_s": mu,
        "assumed_benchmark_workers": assumed_benchmark_workers,
        "poly_sanity_check": poly_sanity,
        "single_worker_projection": baseline.to_dict(),
        "recommended_replicas": recommended,
        "recommended_projection": chosen_projection.to_dict(),
        "bottleneck_verdict": verdict,
        "target_devices": target_devices,
        "window_s": window_s,
        "target_p95_s": target_p95_s,
    }
