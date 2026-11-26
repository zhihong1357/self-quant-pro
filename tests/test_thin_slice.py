"""H-0001 rebuilt from the second generation (migration acceptance A, lineage section 4)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from sqp.research import trials
from sqp.research.studies import SECTIONS, thin_slice

ROOT = Path(__file__).resolve().parents[1]
# finance-research registry/trials.jsonl, T-000001 (synthetic data, so it may be public)
GEN2 = {
    "pairs": 141,
    "pairs_differing": 141,
    "corr_pit": 0.3630558307549197,
    "corr_pit_low": 0.21036955231055732,
    "corr_pit_high": 0.49845210410508156,
    "t_pit": 4.593812979948832,
    "corr_revised": 0.4770539621144875,
    "t_revised": 6.399533073393137,
    "theory_pit": 0.3492151478847891,
    "theory_revised": 0.4472135954999579,
}


def cards() -> list[trials.Trial]:
    text = (ROOT / trials.TRIALS).read_text("utf-8")
    return [t for t in trials.parse(text) if t.study == "thin_slice"]


def params() -> dict:
    return dict(cards()[0].params)


def test_the_registered_card_reproduces_the_second_generation():
    outcome = thin_slice.run(None, params())
    m = outcome.metrics
    for key, value in GEN2.items():
        assert abs(m[key] - value) < 1e-9, key
    assert m["theory_inside_interval"] is True
    assert m["gen2_pairs"] == m["pairs"] and m["gen2_abs_diff"] < 1e-9
    assert list(outcome.sections) == list(SECTIONS)
    assert "：一致。" in outcome.sections["结论"]
    assert outcome.verdict == "一致"


def test_the_repeat_card_is_the_same_trial():
    first, repeat = cards()
    assert repeat.repeat_of == first.id and repeat.params == first.params


def test_metrics_and_report_text_are_deterministic():
    one, two = thin_slice.run(None, params()), thin_slice.run(None, params())
    for a, b in ((one.metrics, two.metrics), (one.sections, two.sections)):
        assert json.dumps(a, sort_keys=True, allow_nan=False) == json.dumps(
            b, sort_keys=True, allow_nan=False
        )


def test_a_version_is_knowable_on_its_day_and_the_latest_one_wins():
    month = date(2005, 1, 1)
    indicator = [(month, date(2005, 2, 16), 1.0), (month, date(2005, 3, 18), 2.0)]
    assert thin_slice.as_known(indicator, date(2005, 2, 15)) == {}
    assert thin_slice.as_known(indicator, date(2005, 2, 16)) == {month: 1.0}  # same day
    assert thin_slice.as_known(indicator, date(2005, 3, 18)) == {month: 2.0}
    assert thin_slice.as_known(indicator, date(2005, 3, 17)) == {month: 1.0}


def test_the_clock_limit_decides_the_last_decision():
    _, returns = thin_slice.generate(params())
    limit = date.fromisoformat(params()["clock_limit"])  # 2011-12-31
    public = [month for month, knowable, _ in returns if knowable <= limit]
    assert max(public) == date(2011, 11, 1)  # December's return is public only in January


def test_a_gap_from_the_second_generation_is_reported_as_inconsistent():
    outcome = thin_slice.run(None, {**params(), "gen2_corr_pit": 0.36})
    assert "不一致，需要排查" in outcome.sections["结论"]
    assert outcome.verdict == "不一致"


def test_without_a_second_generation_value_it_still_runs():
    p = {k: v for k, v in params().items() if not k.startswith("gen2_")}
    assert "gen2_abs_diff" not in thin_slice.run(None, p).metrics


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"clock_limit": "1999-12-31"}, "至少要 4 对"),
        ({"confidence": 1.0}, "confidence"),
    ],
)
def test_degenerate_worlds_say_why_they_fail(change, message):
    with pytest.raises(ValueError, match=message):
        thin_slice.run(None, {**params(), **change})


def test_it_refuses_a_data_interface():
    with pytest.raises(ValueError, match="pit 必须是 None"):
        thin_slice.run(object(), params())  # type: ignore[arg-type]
