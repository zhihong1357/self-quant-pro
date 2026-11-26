from __future__ import annotations

import pytest

from sqp.stats import (
    deflated_sharpe,
    detection_power,
    expected_max_sharpe,
    min_detectable_sharpe,
    sharpe_standard_error,
    years_needed,
)


def test_matches_the_inherited_charter_table():
    # finance-research charter, section 3: 5 years and 100 trials give about 1.13
    assert expected_max_sharpe(100, 5) == pytest.approx(1.13, abs=0.005)


def test_confirmation_set_of_u1_needs_a_sharpe_near_2_9():
    assert min_detectable_sharpe(639 / 365) == pytest.approx(2.90, abs=0.01)


def test_years_needed_is_the_inverse():
    assert min_detectable_sharpe(years_needed(1.0)) == pytest.approx(1.0)


def test_a_single_trial_has_no_selection_bias():
    assert expected_max_sharpe(1, 5) == 0.0


def test_detection_power_inverts_the_hurdle():
    years = 639 / 365
    assert detection_power(min_detectable_sharpe(years), years) == pytest.approx(0.8)
    assert detection_power(0.0, years) == pytest.approx(1 - 0.99865, abs=1e-4)  # P(Z > 3)


def test_sharpe_standard_error_reduces_to_lo_for_normal_returns():
    import math

    sr, n = 0.1, 500
    assert sharpe_standard_error(sr, n) == pytest.approx(math.sqrt((1 + sr * sr / 2) / (n - 1)))
    assert sharpe_standard_error(sr, n, skew=-1, kurtosis=9) > sharpe_standard_error(sr, n)


def test_deflated_sharpe_gets_stricter_with_more_trials():
    one, many = deflated_sharpe(0.1, 639, 1), deflated_sharpe(0.1, 639, 1000)
    assert one > 0.99 and many < one
    assert deflated_sharpe(0.0, 639, 1) == pytest.approx(0.5)


@pytest.mark.parametrize("bad", [{"n": 1}, {"trials": 0}])
def test_deflated_sharpe_rejects_nonsense(bad):
    args = {"sharpe": 0.1, "n": 100, "trials": 5} | bad
    with pytest.raises(ValueError):
        deflated_sharpe(**args)


@pytest.mark.parametrize(("sharpe", "n", "skew"), [(0.1, 1, 0.0), (1.0, 100, 3.0)])
def test_sharpe_standard_error_rejects_nonsense(sharpe, n, skew):
    """Too few returns; or a skew so large for this Sharpe ratio that the variance would
    be negative (1 - 3 x 1 + (3 - 1) / 4 < 0)."""
    with pytest.raises(ValueError):
        sharpe_standard_error(sharpe, n, skew=skew)
