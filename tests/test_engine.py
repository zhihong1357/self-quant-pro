"""The cross-sectional engine, on tables small enough to check by hand."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from sqp.research import engine

H = timedelta(hours=1)


def at(hour: int) -> datetime:
    return datetime(2022, 1, 1, tzinfo=UTC) + hour * H


def klines(bars: dict[str, list[tuple[int, float]]]) -> pl.DataFrame:
    """symbol -> [(hour the bar becomes knowable, close)]."""
    rows = [(s, at(h), c) for s, items in bars.items() for h, c in items]
    return pl.DataFrame(rows, schema=["symbol", "knowable_time", "close"], orient="row")


def test_rebalance_times_include_both_ends():
    assert engine.rebalance_times(at(0), at(16), 8 * H) == [at(0), at(8), at(16)]
    with pytest.raises(ValueError):
        engine.rebalance_times(at(0), at(1), timedelta(0))


def test_prices_are_the_latest_knowable_bar_and_staleness_decides_tradability():
    k = klines({"A": [(0, 10.0), (1, 11.0), (2, 12.0)], "B": [(0, 5.0)]})
    p = engine.prices_at(k, [at(1), at(2), at(3)], max_staleness=H)
    rows = {(r["symbol"], r["time"]): (r["price"], r["fresh"]) for r in p.to_dicts()}
    assert rows[("A", at(1))] == (11.0, True)
    assert rows[("A", at(3))] == (12.0, True)  # one hour old is still fresh
    assert rows[("B", at(1))] == (5.0, True)
    assert rows[("B", at(2))] == (5.0, False)  # stopped trading: stale, price kept


def test_a_symbol_has_no_price_before_its_first_bar():
    p = engine.prices_at(klines({"A": [(0, 1.0)], "N": [(2, 1.0)]}), [at(1), at(2)], H)
    assert set(map(tuple, p.select("symbol", "time").rows())) == {
        ("A", at(1)),
        ("A", at(2)),
        ("N", at(2)),
    }


def test_a_delisted_contract_exits_at_its_last_price_instead_of_vanishing():
    k = klines({"B": [(0, 100.0), (1, 50.0)]})  # falls by half, then stops trading
    prices = engine.prices_at(k, [at(0), at(1), at(2)], max_staleness=timedelta(0))
    r = engine.period_returns(prices).to_dicts()
    assert [(x["time"], x["ret"]) for x in r] == [(at(0), -0.5), (at(1), 0.0)]


def ten_signals(time: datetime, knowable: datetime | None = None) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [f"S{i}" for i in range(10)],
            "time": [time] * 10,
            "value": [float(i) for i in range(10)],
            "knowable_time": [knowable or time] * 10,
        }
    )


def fresh(symbols: list[str], time: datetime) -> pl.DataFrame:
    return pl.DataFrame({"symbol": symbols, "time": [time] * len(symbols), "fresh": True})


def test_quintiles_are_dollar_neutral_with_gross_exposure_one():
    w = engine.quantile_weights(
        ten_signals(at(0)),
        fresh([f"S{i}" for i in range(10)], at(0)),
        groups=5,
        long="low",
        min_names=10,
    )
    weights = dict(zip(w["symbol"], w["weight"], strict=True))
    assert weights == {"S0": 0.25, "S1": 0.25, "S8": -0.25, "S9": -0.25}
    high = engine.quantile_weights(
        ten_signals(at(0)),
        fresh([f"S{i}" for i in range(10)], at(0)),
        groups=5,
        long="high",
        min_names=10,
    )
    assert dict(zip(high["symbol"], high["weight"], strict=True))["S9"] == 0.25


def test_too_few_names_give_no_portfolio_and_stale_names_are_skipped():
    names = [f"S{i}" for i in range(10)]
    signal = ten_signals(at(0))
    assert engine.quantile_weights(
        signal, fresh(names, at(0)), groups=5, long="low", min_names=11
    ).is_empty()
    tradable = fresh(names, at(0)).with_columns(fresh=pl.col("symbol") != "S0")
    w = engine.quantile_weights(signal, tradable, groups=3, long="low", min_names=9)
    assert "S0" not in w["symbol"].to_list()


def test_a_signal_not_yet_knowable_is_an_error():
    with pytest.raises(ValueError, match="前视"):
        engine.quantile_weights(
            ten_signals(at(0), knowable=at(1)),
            fresh([f"S{i}" for i in range(10)], at(0)),
            groups=5,
            long="low",
            min_names=5,
        )


def test_returns_carry_turnover_and_costs_by_hand():
    times = [at(0), at(1)]
    weights = pl.DataFrame(
        {
            "symbol": ["X", "Y", "X", "Y"],
            "time": [times[0]] * 2 + [times[1]] * 2,
            "weight": [0.5, -0.5] * 2,
        }
    )
    returns = pl.DataFrame(
        {
            "symbol": ["X", "Y", "X", "Y"],
            "time": [times[0]] * 2 + [times[1]] * 2,
            "next_time": [at(1)] * 2 + [at(2)] * 2,
            "ret": [0.10, -0.10, 0.0, 0.0],
        }
    )
    carry = pl.DataFrame({"symbol": ["X", "Y"], "time": [times[0]] * 2, "carry": [-0.01, -0.02]})
    out = engine.portfolio_returns(weights, returns, carry=carry, cost_rate=0.001).to_dicts()
    first, second = out
    assert first["gross"] == pytest.approx(0.10)  # long X +10 %, short Y -10 %
    assert first["carry"] == pytest.approx(0.5 * -0.01 + -0.5 * -0.02)  # short receives
    assert first["turnover"] == pytest.approx(1.0)  # from cash into gross exposure 1
    assert first["net"] == pytest.approx(0.10 + 0.005 - 0.001)
    # Before the second rebalance X drifted to 0.5 * 1.1 / 1.1, Y to -0.5 * 0.9 / 1.1.
    assert second["turnover"] == pytest.approx(abs(-0.5 + 0.5 * 0.9 / 1.1))
    assert second["cost"] == pytest.approx(0.001 * abs(-0.5 + 0.5 * 0.9 / 1.1))
    assert (first["longs"], first["shorts"]) == (1, 1)


def test_closing_at_a_time_with_too_few_names_is_charged_to_the_period_before():
    """Portfolios at hours 0 and 2; hour 1 had too few names, so the first portfolio is
    closed there and the second opened from cash. The last time (3) is not a trade."""
    times = [at(0), at(1), at(2), at(3)]
    weights = pl.DataFrame(
        {
            "symbol": ["X", "Y", "X", "Y"],
            "time": [times[0]] * 2 + [times[2]] * 2,
            "weight": [0.5, -0.5] * 2,
        }
    )
    returns = pl.DataFrame(
        {
            "symbol": ["X", "Y"] * 3,
            "time": [times[0]] * 2 + [times[1]] * 2 + [times[2]] * 2,
            "next_time": [times[1]] * 2 + [times[2]] * 2 + [times[3]] * 2,
            "ret": [0.10, -0.10, 0.0, 0.0, 0.0, 0.0],
        }
    )
    first, second = engine.portfolio_returns(weights, returns, cost_rate=0.001).to_dicts()
    closing = 0.5 * 1.1 / 1.1 + 0.5 * 0.9 / 1.1  # the drifted weights at hour 1
    assert first["time"] == times[0]
    assert first["turnover"] == pytest.approx(1.0 + closing)
    assert first["cost"] == pytest.approx(0.001 * (1.0 + closing))
    assert second["turnover"] == pytest.approx(1.0)  # from cash again; nothing at hour 3
    assert second["net"] == pytest.approx(-0.001)


def test_positions_earn_the_next_period_not_the_one_that_made_the_signal():
    """The look-ahead canary: a signal built from the return that just happened must
    earn the return that follows it, here zero."""
    k = klines(
        {"UP": [(0, 100.0), (1, 110.0), (2, 110.0)], "DOWN": [(0, 100.0), (1, 90.0), (2, 90.0)]}
    )
    times = [at(0), at(1), at(2)]
    prices = engine.prices_at(k, times, max_staleness=H)
    returns = engine.period_returns(prices)
    past = returns.filter(pl.col("time") == at(0)).select(
        "symbol",
        pl.lit(at(1)).cast(engine.TIME).alias("time"),
        pl.col("ret").alias("value"),
        pl.lit(at(1)).cast(engine.TIME).alias("knowable_time"),
    )
    w = engine.quantile_weights(past, prices, groups=2, long="high", min_names=2)
    out = engine.portfolio_returns(w, returns)
    assert out["gross"].to_list() == [0.0]


def test_a_holding_without_a_return_is_an_error():
    weights = pl.DataFrame({"symbol": ["X"], "time": [at(0)], "weight": [0.5]})
    returns = pl.DataFrame({"symbol": ["Y"], "time": [at(0)], "next_time": [at(1)], "ret": [0.0]})
    with pytest.raises(ValueError, match="没有这一期的收益"):
        engine.portfolio_returns(weights, returns)


def test_summary_by_hand():
    values = [0.02, -0.01, 0.03, -0.02, 0.01, 0.00, 0.02, -0.01]
    times = [datetime(2021, 12, 28, tzinfo=UTC) + timedelta(days=i) for i in range(8)]
    s = engine.summarize(pl.DataFrame({"time": times, "net": values}), periods_per_year=365)
    mean = sum(values) / 8
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / 7)
    assert s["periods"] == 8
    assert s["sharpe_annual"] == pytest.approx(mean / sd * math.sqrt(365), rel=1e-9)
    assert s["mean_annual"] == pytest.approx(mean * 365, rel=1e-9)
    assert s["max_drawdown"] == pytest.approx(-0.02, rel=1e-9)  # the -2 % day after a peak
    assert set(s["sharpe_by_year"]) == {"2021", "2022"}
    assert s["positive_quarters"] == 4  # (2,-1) (3,-2) (1,0) (2,-1) all sum to +1


def test_results_are_rounded_to_ten_digits():
    assert engine.rounded(1 / 3) == float("0.3333333333")
    assert engine.rounded(float("nan")) is None
