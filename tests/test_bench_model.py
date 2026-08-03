"""Unit tests for bench.model - pure-function M/M/c extrapolation + poly fit.

Uses synthetic data only (no bench execution, no I/O) so this stays fast and
independent of the fleet/manager stack. Focus: monotonicity properties and
boundary (utilization >= 1) handling per task-4 brief.
"""

from __future__ import annotations

import math

import pytest

from bench.model import (
    ProjectionResult,
    bottleneck_verdict,
    build_extrapolation,
    erlang_b,
    erlang_c,
    expected_max_wait,
    fit_service_rate,
    mmc_wait,
    polyfit_throughput,
    polyval,
    project_auth_storm,
    recommend_replica_count,
    wait_percentile,
)


# ---------------------------------------------------------------------------
# erlang_b / erlang_c
# ---------------------------------------------------------------------------


def test_erlang_b_is_probability_in_unit_interval():
    for c in (1, 2, 5, 20):
        for a in (0.0, 0.5, 1.0, 3.0, 10.0):
            b = erlang_b(c, a)
            assert 0.0 <= b <= 1.0


def test_erlang_b_zero_load_is_zero_blocking():
    assert erlang_b(5, 0.0) == 0.0


def test_erlang_c_increases_with_offered_load_for_fixed_servers():
    c = 4
    probs = [erlang_c(c, a) for a in (0.5, 1.0, 2.0, 3.0, 3.9)]
    for earlier, later in zip(probs, probs[1:]):
        assert later >= earlier


def test_erlang_c_saturates_to_one_at_or_above_capacity():
    assert erlang_c(3, 3.0) == 1.0
    assert erlang_c(3, 10.0) == 1.0


def test_erlang_c_rejects_nonpositive_c():
    with pytest.raises(ValueError):
        erlang_c(0, 1.0)


# ---------------------------------------------------------------------------
# mmc_wait monotonicity + boundary
# ---------------------------------------------------------------------------


def test_mmc_wait_higher_mu_reduces_wait_holding_lambda_and_c_fixed():
    lam = 10.0
    c = 2
    wq_slow = mmc_wait(lam, mu=6.0, c=c).wq_s
    wq_fast = mmc_wait(lam, mu=12.0, c=c).wq_s
    assert wq_fast < wq_slow


def test_mmc_wait_more_servers_reduces_wait_holding_lambda_and_mu_fixed():
    lam = 10.0
    mu = 6.0
    wq_c2 = mmc_wait(lam, mu, c=2).wq_s
    wq_c4 = mmc_wait(lam, mu, c=4).wq_s
    assert wq_c4 < wq_c2


def test_mmc_wait_unstable_when_utilization_at_or_above_one():
    result = mmc_wait(lam=100.0, mu=1.0, c=1)
    assert result.stable is False
    assert result.rho >= 1.0
    assert math.isinf(result.wq_s)
    assert math.isinf(result.w_s)


def test_mmc_wait_exactly_at_boundary_is_unstable_not_crashing():
    # rho == 1.0 exactly (lam == c*mu) must not raise / divide-by-zero crash.
    result = mmc_wait(lam=10.0, mu=5.0, c=2)
    assert result.stable is False
    assert result.rho == pytest.approx(1.0)


def test_mmc_wait_zero_arrivals_has_zero_queueing():
    result = mmc_wait(lam=0.0, mu=5.0, c=1)
    assert result.stable is True
    assert result.wq_s == 0.0
    assert result.w_s == pytest.approx(1.0 / 5.0)


def test_mmc_wait_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        mmc_wait(lam=1.0, mu=0.0, c=1)
    with pytest.raises(ValueError):
        mmc_wait(lam=1.0, mu=1.0, c=0)
    with pytest.raises(ValueError):
        mmc_wait(lam=-1.0, mu=1.0, c=1)


# ---------------------------------------------------------------------------
# wait_percentile / expected_max_wait
# ---------------------------------------------------------------------------


def test_wait_percentile_is_nondecreasing_in_q():
    mmc = mmc_wait(lam=8.0, mu=6.0, c=2)
    q_values = (0.5, 0.8, 0.9, 0.95, 0.99)
    percentiles = [wait_percentile(mmc, q) for q in q_values]
    for earlier, later in zip(percentiles, percentiles[1:]):
        assert later >= earlier


def test_wait_percentile_zero_when_below_no_wait_mass():
    # Very light load -> erlang_c (Pc) tiny -> low percentiles are exactly 0.
    mmc = mmc_wait(lam=0.1, mu=10.0, c=4)
    assert wait_percentile(mmc, 0.5) == 0.0


def test_wait_percentile_unstable_is_infinite():
    mmc = mmc_wait(lam=100.0, mu=1.0, c=1)
    assert math.isinf(wait_percentile(mmc, 0.95))


def test_wait_percentile_rejects_out_of_range_q():
    mmc = mmc_wait(lam=1.0, mu=5.0, c=1)
    with pytest.raises(ValueError):
        wait_percentile(mmc, 0.0)
    with pytest.raises(ValueError):
        wait_percentile(mmc, 1.0)


def test_expected_max_wait_grows_with_more_customers():
    mmc = mmc_wait(lam=8.0, mu=6.0, c=2)
    small = expected_max_wait(mmc, 10)
    large = expected_max_wait(mmc, 1000)
    assert large >= small


def test_expected_max_wait_zero_customers_is_zero():
    mmc = mmc_wait(lam=8.0, mu=6.0, c=2)
    assert expected_max_wait(mmc, 0) == 0.0


def test_expected_max_wait_unstable_is_infinite():
    mmc = mmc_wait(lam=100.0, mu=1.0, c=1)
    assert math.isinf(expected_max_wait(mmc, 1000))


# ---------------------------------------------------------------------------
# fit_service_rate / polyfit
# ---------------------------------------------------------------------------


def test_fit_service_rate_picks_max_observed_throughput_divided_by_workers():
    points = [(10, 50.0), (25, 80.0), (50, 95.0), (100, 90.0)]
    assert fit_service_rate(points, assumed_workers=1) == 95.0
    assert fit_service_rate(points, assumed_workers=2) == pytest.approx(47.5)


def test_fit_service_rate_rejects_empty_points():
    with pytest.raises(ValueError):
        fit_service_rate([])


def test_fit_service_rate_rejects_nonpositive_workers():
    with pytest.raises(ValueError):
        fit_service_rate([(10, 50.0)], assumed_workers=0)


def test_polyfit_throughput_returns_expected_coeff_count():
    points = [(10, 20.0), (25, 40.0), (50, 55.0), (100, 60.0), (200, 61.0)]
    coeffs = polyfit_throughput(points, degree=2)
    assert len(coeffs) == 3  # degree 2 -> 3 coefficients


def test_polyfit_throughput_exact_fit_recovers_linear_data():
    # y = 2x + 1 exactly -> degree-1 fit should recover it near-exactly.
    points = [(0, 1.0), (1, 3.0), (2, 5.0), (3, 7.0)]
    coeffs = polyfit_throughput(points, degree=1)
    assert polyval(coeffs, 10) == pytest.approx(21.0, abs=1e-6)


def test_polyfit_throughput_requires_at_least_two_points():
    with pytest.raises(ValueError):
        polyfit_throughput([(10, 20.0)])


# ---------------------------------------------------------------------------
# project_auth_storm / recommend_replica_count / bottleneck_verdict
# ---------------------------------------------------------------------------


def test_project_auth_storm_more_replicas_lowers_p95_wait():
    p1 = project_auth_storm(target_devices=1000, window_s=60.0, mu_per_worker=5.0, c_workers=4)
    p2 = project_auth_storm(target_devices=1000, window_s=60.0, mu_per_worker=5.0, c_workers=8)
    assert isinstance(p1, ProjectionResult)
    assert p2.p95_wait_s <= p1.p95_wait_s
    assert p2.sustainable_auth_per_sec > p1.sustainable_auth_per_sec


def test_project_auth_storm_unstable_case_has_infinite_waits_not_crash():
    proj = project_auth_storm(target_devices=1000, window_s=1.0, mu_per_worker=1.0, c_workers=1)
    assert proj.stable is False
    assert math.isinf(proj.p95_wait_s)
    assert math.isinf(proj.max_wait_estimate_s)


def test_recommend_replica_count_finds_minimal_c_satisfying_target():
    # With mu=50/s, lam=1000/60=16.7/s: even c=1 should already be comfortably
    # stable and fast. Use a stricter target_p95 with a lower mu to force
    # replica growth above 1.
    c = recommend_replica_count(
        target_devices=1000, window_s=60.0, mu_per_worker=5.0, target_p95_s=1.0, max_c=64
    )
    assert c is not None
    assert c >= 1
    # Confirm a smaller c would not satisfy target (minimality spot check).
    if c > 1:
        smaller = project_auth_storm(1000, 60.0, 5.0, c - 1)
        assert not (smaller.stable and smaller.p95_wait_s < 1.0)


def test_recommend_replica_count_returns_none_when_unreachable():
    # Absurdly low mu and a tiny max_c search ceiling -> cannot meet target.
    c = recommend_replica_count(
        target_devices=1000, window_s=1.0, mu_per_worker=0.01, target_p95_s=0.001, max_c=2
    )
    assert c is None


def test_bottleneck_verdict_text_varies_by_outcome():
    assert "단일 인스턴스" in bottleneck_verdict(1, 64)
    assert "레플리카" in bottleneck_verdict(8, 64)
    assert "재검토" in bottleneck_verdict(None, 64)


# ---------------------------------------------------------------------------
# build_extrapolation (integration of the pure pieces)
# ---------------------------------------------------------------------------


def test_build_extrapolation_smoke_and_shape():
    points = [(10, 40.0), (25, 70.0), (50, 90.0), (100, 95.0), (200, 96.0)]
    result = build_extrapolation(
        points, target_devices=1000, window_s=60.0, target_p95_s=1.0
    )
    assert result["mu_per_worker_auth_per_s"] == 96.0
    assert "single_worker_projection" in result
    assert "recommended_projection" in result
    assert "poly_sanity_check" in result
    assert isinstance(result["bottleneck_verdict"], str) and result["bottleneck_verdict"]


def test_build_extrapolation_recommended_replicas_is_int_or_none():
    points = [(10, 5.0), (25, 6.0), (50, 6.5)]
    result = build_extrapolation(
        points, target_devices=1000, window_s=10.0, target_p95_s=0.01, max_replica_search=4
    )
    assert result["recommended_replicas"] is None or isinstance(
        result["recommended_replicas"], int
    )
