"""H-0006, the positive control, on a synthetic market where the truth is known."""

from __future__ import annotations

import json
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from sqp.research import trials
from sqp.research.studies import SECTIONS, planted_signal

ROOT = Path(__file__).resolve().parents[1]
H = timedelta(hours=1)


def synthetic_klines(first: datetime, days: int, symbols: int = 24, seed: int = 1) -> pl.DataFrame:
    """Independent random walks (about 5 % a day); quote volume grows with the index."""
    rng = random.Random(seed)
    rows = []
    for i in range(symbols):
        price = 100.0
        for h in range(days * 24):
            price *= math.exp(rng.gauss(0.0, 0.01))
            rows.append((f"S{i:02d}", first + h * H, price, 1e6 * (i + 1) * (1 + rng.random())))
    return pl.DataFrame(
        rows, schema=["symbol", "open_time", "close", "quote_volume"], orient="row"
    ).with_columns(
        pl.col("open_time").cast(pl.Datetime("us", "UTC")),
        knowable_time=(pl.col("open_time") + H).cast(pl.Datetime("us", "UTC")),
    )


def registered() -> dict:
    text = (ROOT / trials.TRIALS).read_text("utf-8")
    card = next(t for t in trials.parse(text) if t.study == "planted_signal")
    return dict(card.params)


def small(**changes) -> dict:
    """The registered parameters on a shorter synthetic window in 2020."""
    return {**registered(), "window_start": "2020-04-10", "window_end": "2021-01-31", **changes}


@pytest.fixture(scope="module")
def market() -> pl.DataFrame:
    return synthetic_klines(datetime(2020, 1, 1, tzinfo=UTC), days=400)


def test_the_control_passes_on_a_market_with_no_signal_of_its_own(market):
    outcome = planted_signal.evaluate(market, small())
    checks = outcome.metrics["checks"]
    assert outcome.verdict == "通过", checks
    arms = outcome.metrics["arms"]
    assert 0.04 <= arms["strong"]["estimated_strength"] <= 0.12
    assert abs(arms["canary"]["estimated_strength"]) < 0.04
    assert outcome.metrics["names"] == 20
    assert list(outcome.sections) == list(SECTIONS)


def test_a_look_ahead_bug_in_the_engine_fails_the_control(market, monkeypatch):
    """Label each return one day late, as an engine that trades on the return that
    made the signal would: the canary must be found and the verdict must fail."""
    honest = planted_signal.period_returns

    def peeking(prices: pl.DataFrame) -> pl.DataFrame:
        r = honest(prices)
        day = timedelta(days=1)
        late = r.with_columns(pl.col("time") + day, pl.col("next_time") + day)
        first = r.filter(pl.col("time") == r["time"].min()).with_columns(ret=pl.lit(0.0))
        return pl.concat([first, late])

    monkeypatch.setattr(planted_signal, "period_returns", peeking)
    outcome = planted_signal.evaluate(market, small())
    assert outcome.verdict == "不通过"
    assert outcome.metrics["checks"]["canary_not_found"] is False


def test_a_signal_too_weak_to_find_fails_the_control(market):
    outcome = planted_signal.evaluate(
        market, small(strengths={"strong": 0.0, "half": 0.0, "zero": 0.0})
    )
    assert outcome.verdict == "不通过"
    assert outcome.metrics["checks"]["strong_found"] is False


def test_the_result_is_deterministic(market):
    one = planted_signal.evaluate(market, small())
    two = planted_signal.evaluate(market, small())
    assert json.dumps(one.metrics, sort_keys=True) == json.dumps(two.metrics, sort_keys=True)


def test_the_registered_parameters_work_on_their_own_window():
    """The card's window (2022-05-03 to 2024-01-31), on synthetic data of that span."""
    params = registered()
    first = planted_signal.day(params["window_start"]) - timedelta(days=95)
    market = synthetic_klines(first, days=95 + 640)
    outcome = planted_signal.evaluate(market, params)
    assert outcome.verdict == "通过", outcome.metrics["checks"]
    assert outcome.metrics["days"] == 639


def test_names_are_picked_before_the_window_with_enough_history(market):
    start = datetime(2020, 4, 10, tzinfo=UTC)
    names = planted_signal.liquid_names(market, start, days=90, names=3, min_days=80)
    assert names == ["S23", "S22", "S21"]  # volume grows with the index
    late = market.filter(
        ~((pl.col("symbol") == "S23") & (pl.col("open_time") < start - timedelta(days=30)))
    )
    assert "S23" not in planted_signal.liquid_names(late, start, days=90, names=3, min_days=80)


def test_the_planted_signal_does_not_depend_on_the_data():
    times = [datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=i) for i in range(3)]
    a = planted_signal.planted(["B", "A"], times, seed=5)
    b = planted_signal.planted(["A", "B"], times, seed=5)
    assert a.equals(b)


class RecordingPit:
    def __init__(self, klines: pl.DataFrame) -> None:
        self.klines, self.calls = klines, []

    def table(self, dataset, *, end, start=None, columns=None):
        self.calls.append({"dataset": dataset, "start": start, "end": end, "columns": columns})
        return self.klines


def test_it_reads_only_exploration_data_through_the_interface():
    params = registered()
    first = planted_signal.day(params["window_start"]) - timedelta(days=95)
    pit = RecordingPit(synthetic_klines(first, days=95 + 640))
    planted_signal.run(pit, params)  # type: ignore[arg-type]
    (call,) = pit.calls
    assert call["dataset"] == "um-klines-1h"
    assert call["end"] <= datetime(2024, 1, 31, 23, 59, 59, tzinfo=UTC)  # exploration ends here
    assert set(call["columns"]) == {"symbol", "open_time", "close", "quote_volume"}
