from __future__ import annotations

import pytest

from sqp.stats import expected_max_sharpe, min_detectable_sharpe, years_needed


def test_matches_the_inherited_charter_table():
    # finance-research charter, section 3: 5 years and 100 trials give about 1.13
    assert expected_max_sharpe(100, 5) == pytest.approx(1.13, abs=0.005)


def test_confirmation_set_of_u1_needs_a_sharpe_near_2_9():
    assert min_detectable_sharpe(639 / 365) == pytest.approx(2.90, abs=0.01)


def test_years_needed_is_the_inverse():
    assert min_detectable_sharpe(years_needed(1.0)) == pytest.approx(1.0)


def test_a_single_trial_has_no_selection_bias():
    assert expected_max_sharpe(1, 5) == 0.0
