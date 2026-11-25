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
DAY = timedelta(days=1)
T = pl.Datetime("us", "UTC")


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
    for a, b in ((one.metrics, two.metrics), (one.sections, two.sections)):
        assert json.dumps(a, sort_keys=True, allow_nan=False) == json.dumps(
            b, sort_keys=True, allow_nan=False
        )


def held_days(market: pl.DataFrame, params: dict) -> tuple[list[str], pl.DataFrame]:
    """The names, and the days on which each sits in the long or short fifth by s."""
    start = planted_signal.day(params["window_start"])
    names = planted_signal.liquid_names(market, start, 90, 20, 80)
    times = planted_signal.rebalance_times(start, planted_signal.day(params["window_end"]), DAY)
    s = planted_signal.planted(names, times, int(params["seed"]))
    ranked = s.with_columns(rank=pl.col("s").rank("ordinal").over("time"))
    return names, ranked.filter((pl.col("rank") <= 4) | (pl.col("rank") > 16))


def test_a_delisting_and_a_missing_bar_inside_the_window_do_not_break_the_control(market):
    """Real data has both (LUNA was delisted in May 2022, days into the window). A held
    contract that stops trading, or misses its 00:00 bar, exits at its last price with
    nothing planted: the planted part must not count it."""
    params = small()
    names, held = held_days(market, params)
    after = planted_signal.day("2020-06-01")
    d1 = held.filter((pl.col("symbol") == names[0]) & (pl.col("time") > after))["time"][0]
    d2 = held.filter((pl.col("symbol") == names[1]) & (pl.col("time") > d1))["time"][0]
    broken = market.filter(
        ~((pl.col("symbol") == names[0]) & (pl.col("knowable_time") > d1))  # delisted
        & ~((pl.col("symbol") == names[1]) & (pl.col("knowable_time") == d2 + DAY))
    )
    outcome = planted_signal.evaluate(broken, params)
    assert outcome.metrics["checks"]["engine_consistent"] is True
    assert outcome.verdict == "通过", outcome.metrics["checks"]
    json.dumps(outcome.metrics, allow_nan=False)


def test_planting_by_multiplying_keeps_prices_positive_on_a_crash_day():
    """r = -0.99 with sd 0.25 and s = -1: adding would give growth 1 - 0.99 - 0.02 < 0."""
    times = [datetime(2022, 5, d, tzinfo=UTC) for d in (10, 11, 12)]
    closes = pl.DataFrame(
        {"symbol": ["L"] * 3, "time": times, "close": [100.0, 1.0, 1.0], "ret": [-0.99, 0.0, None]}
    ).with_columns(pl.col("time").cast(T))
    s = pl.DataFrame({"symbol": ["L"] * 3, "time": times, "s": [-1.0, 0.0, 0.0]}).with_columns(
        pl.col("time").cast(T)
    )
    sd = pl.DataFrame({"symbol": ["L"], "sd": [0.25]})
    path = planted_signal.plant(closes, s, sd, k=0.08, lag=0)["close"].to_list()
    assert min(path) > 0
    assert path[1] == pytest.approx(100 * 0.01 * (1 - 0.08 * 0.25))


def test_weights_that_are_not_dollar_neutral_with_gross_one_fail_the_control(market, monkeypatch):
    """Doubling every weight keeps every t-value and the linearity check intact; only the
    book check sees it (Gemini's review found the gap)."""
    honest = planted_signal.quantile_weights

    def doubled(*args, **kwargs):
        return honest(*args, **kwargs).with_columns(pl.col("weight") * 2)

    monkeypatch.setattr(planted_signal, "quantile_weights", doubled)
    outcome = planted_signal.evaluate(market, small())
    assert outcome.metrics["checks"]["dollar_neutral_gross_one"] is False
    assert outcome.verdict == "不通过"


def test_each_check_can_fail_the_control_on_its_own():
    good = {
        "strong": {"t_stat": 8.0, "estimated_strength": 0.08, "positive_quarters": 4},
        "zero": {"t_stat": 0.5},
        "canary": {"t_stat": -0.5},
    }
    params = registered()
    assert planted_signal.judge(good, params, True, True)[0] == "通过"
    assert planted_signal.judge(good, params, False, True)[0] == "不通过"
    assert planted_signal.judge(good, params, True, False)[0] == "不通过"
    for arm, key, value in [
        ("strong", "estimated_strength", 0.2),
        ("strong", "positive_quarters", 2),
        ("zero", "t_stat", -3.5),  # found, in the wrong direction: still a bug
        ("canary", "t_stat", -8.0),
    ]:
        bad = {**good, arm: {**good[arm], key: value}}
        assert planted_signal.judge(bad, params, True, True)[0] == "不通过", (arm, key)


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
