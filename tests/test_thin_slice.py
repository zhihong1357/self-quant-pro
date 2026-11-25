"""H-0001 rebuilt from the second generation (migration acceptance A, lineage section 4)."""

from __future__ import annotations

import json
from pathlib import Path

from sqp.research import trials
from sqp.research.studies import SECTIONS, thin_slice

ROOT = Path(__file__).resolve().parents[1]
# finance-research registry/trials.jsonl, T-000001 (synthetic data, so it may be public)
GEN2 = {
    "pairs": 141,
    "corr_pit": 0.3630558307549197,
    "corr_pit_low": 0.21036955231055732,
    "corr_pit_high": 0.49845210410508156,
    "corr_revised": 0.4770539621144875,
    "theory_pit": 0.3492151478847891,
}


def registered_params() -> dict:
    text = (ROOT / trials.TRIALS).read_text("utf-8")
    card = next(t for t in trials.parse(text) if t.study == "thin_slice")
    return card.params


def test_the_registered_card_reproduces_the_second_generation():
    outcome = thin_slice.run(None, registered_params())
    m = outcome.metrics
    assert m["pairs"] == GEN2["pairs"] and m["pairs_differing"] == GEN2["pairs"]
    for key, value in GEN2.items():
        assert abs(m[key] - value) < 1e-9, key
    assert m["theory_inside_interval"] is True
    assert m["gen2_gap_in_standard_errors"] <= 1
    assert list(outcome.sections) == list(SECTIONS)


def test_the_result_is_deterministic():
    params = registered_params()
    first, second = thin_slice.run(None, params), thin_slice.run(None, params)
    assert json.dumps(first.metrics, sort_keys=True) == json.dumps(second.metrics, sort_keys=True)


def test_only_the_first_release_is_knowable_at_the_decision():
    params = registered_params()
    indicator, _ = thin_slice.generate(params)
    month = thin_slice.month_start(thin_slice.date(2005, 1, 1), 0)
    versions = [(k, v) for m, k, v in indicator if m == month]
    decision = thin_slice.month_start(month, int(params["lag_months"]))
    released, revised = sorted(versions)
    assert released[0] <= decision < revised[0]
    assert thin_slice.as_known(indicator, decision)[month] == released[1]
