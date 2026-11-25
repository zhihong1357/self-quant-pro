"""Candidates A and B: each piece against a known answer, then whole runs on synthetic
markets with an edge, without one, and with nothing but a bid-ask bounce."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from sqp.research.studies import SECTIONS
from sqp.research.studies import first_candidates as fc

H = timedelta(hours=1)
T = pl.Datetime("us", "UTC")

# The parameters the trial cards will carry (registered in a later pull request, after
# the positive control passes; decision 0026). Frozen here so that they are reviewed now.
COMMON = {
    "read_from": "2019-12-01",
    "read_until": "2024-01-31T23:59:59",
    "start": "2020-01-01",
    "liquidity_days": 30,
    "liquidity_min_days": 20,
    "min_volume": 1_000_000,
    "groups": 5,
    "min_names": 20,
    "spread_days": 30,
    "spread_min_days": 20,
    "taker_fee": 0.0005,
    "maker_fee": 0.0002,
    "bear_year": 2022,
    "confirmation_days": 639,
    "t": 3.0,
    "power": 0.8,
}
PLANNED_A = {
    **COMMON,
    "candidate": "A",
    "end": "2024-01-31",
    "offset_minutes": 1,
    "rebalance_hours": 24,
    "prior": 0.35,
}
PLANNED_B = {**COMMON, "candidate": "B", "end": "2024-01-31", "prior": 0.30}


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def frame(rows: list[tuple]) -> pl.DataFrame:
    """Hourly klines from (symbol, open time, close, high, low, volume, quote volume)."""
    return pl.DataFrame(
        rows,
        schema=["symbol", "open_time", "close", "high", "low", "volume", "quote_volume"],
        orient="row",
    ).with_columns(pl.col("open_time").cast(T), knowable_time=(pl.col("open_time") + H).cast(T))


# --- pieces ------------------------------------------------------------------


def test_funding_paid_during_a_holding_belongs_to_it_and_not_before():
    times = [utc(2021, 1, 1, 0, 1), utc(2021, 1, 1, 8, 1), utc(2021, 1, 1, 16, 1)]
    funding = pl.DataFrame(
        {
            "symbol": ["X"] * 4,
            "calc_time": [
                utc(2021, 1, 1, 0, 0, 59),  # settled at 00:00, before anyone here held
                utc(2021, 1, 1, 8) + timedelta(milliseconds=5),
                utc(2021, 1, 1, 16, 1, 30),  # settled at 16:00, recorded late
                utc(2021, 1, 2),
            ],
            "funding_rate": [0.5, 0.001, 0.002, 0.003],
        }
    )
    carry = fc.funding_carry(funding, times).sort("time").to_dicts()
    # 08:00 is paid by whoever held from 00:01; 16:00 by whoever held from 08:01, even
    # though its record came after 16:01; the next day's 00:00 by whoever held from 16:01.
    assert carry == [
        {"symbol": "X", "time": times[0], "carry": -0.001},
        {"symbol": "X", "time": times[1], "carry": -0.002},
        {"symbol": "X", "time": times[2], "carry": -0.003},
    ]


def test_the_funding_signal_is_the_past_day_average_scaled_to_eight_hours():
    times = [utc(2021, 1, 2, 0, 1), utc(2021, 1, 3, 0, 1), utc(2021, 1, 5, 0, 1)]
    settled = [
        utc(2021, 1, 1, 0, 0, 30),  # knowable just before the day began: not in it
        utc(2021, 1, 1, 4),
        utc(2021, 1, 1, 8),
        utc(2021, 1, 2, 0, 0, 1),
        utc(2021, 1, 2, 0, 2),  # after the rebalance at 00:01: counts the next day
    ]
    funding = pl.DataFrame(
        {
            "symbol": ["F"] * 5,
            "calc_time": settled,
            "knowable_time": settled,
            "funding_interval_hours": [8, 4, 4, None, 8],
            "funding_rate": [0.9, 0.001, 0.002, 0.003, 0.006],
        }
    ).with_columns(pl.col("calc_time", "knowable_time").cast(T))
    signal = fc.funding_signal(funding, times, window=timedelta(days=1)).sort("time").to_dicts()
    # Jan 2: 0.001 and 0.002 per 4 hours (0.002, 0.004 per 8) and 0.003 per 8: average 0.003.
    assert [r["time"] for r in signal] == times[:2]  # Jan 5: nothing settled in its day
    assert signal[0]["value"] == pytest.approx(0.003)
    assert signal[1]["value"] == pytest.approx(0.006)
    assert all(r["knowable_time"] <= r["time"] for r in signal)


def test_the_spread_estimate_recovers_a_known_bid_ask_bounce():
    """Closes bounce between bid and ask around a slow mid: the estimator should find a
    spread near the true 0.4 %. Nothing before 20 days of data; a contract that stops
    trading keeps its last estimate, so that its exit still pays."""
    rng = random.Random(4)
    true_spread, first, rows = 0.004, utc(2021, 1, 1), []
    for symbol, days in (("S", 40), ("D", 25)):
        mid = 100.0
        for h in range(days * 24):
            mid *= math.exp(rng.gauss(0, 0.002))
            side = 1 if rng.random() < 0.5 else -1
            close = mid * (1 + side * true_spread / 2)
            high, low = mid * (1 + true_spread / 2 + 0.001), mid * (1 - true_spread / 2 - 0.001)
            rows.append((symbol, first + h * H, close, high, low, 1.0, close))
    est = fc.half_spread(frame(rows), days=30, min_days=20)
    on = {(r["symbol"], r["day"]): r["half_spread"] for r in est.to_dicts()}
    assert on[("S", utc(2021, 2, 9))] * 2 == pytest.approx(true_spread, rel=0.35)
    assert est.filter(pl.col("day") < utc(2021, 1, 21)).is_empty()  # not enough days yet
    assert on[("D", utc(2021, 2, 9))] == on[("D", utc(2021, 2, 5))]  # carried on


def test_a_spread_term_is_dated_by_the_day_its_second_bar_opens():
    """The 23:00 bar's term needs the next day's 00:00 bar, which is only knowable at
    01:00: it belongs to the next day, not to the day whose estimate applies at 00:00."""
    bars = [
        ("X", utc(2021, 1, 1, 22), 100.0, 100.0, 100.0, 1.0, 100.0),
        ("X", utc(2021, 1, 1, 23), 101.0, 101.0, 99.0, 1.0, 101.0),  # closes at the high
        ("X", utc(2021, 1, 2, 0), 99.0, 103.0, 97.0, 1.0, 99.0),
    ]
    est = fc.half_spread(frame(bars), days=1, min_days=1).sort("day").to_dicts()
    # Jan 1 has one term (22:00 with 23:00): c = m = log 100, so 0. Applies on Jan 2.
    # The 23:00 bar's term is dated Jan 2 and applies only on Jan 3.
    assert [r["day"] for r in est] == [utc(2021, 1, 2), utc(2021, 1, 3)]
    assert est[0]["half_spread"] == 0.0
    c, m = math.log(101), (math.log(101) + math.log(99)) / 2
    m_next = (math.log(103) + math.log(97)) / 2
    assert est[1]["half_spread"] == pytest.approx(math.sqrt(4 * (c - m) * (c - m_next)) / 2)


def test_liquidity_uses_only_finished_days_and_counts_calendar_days():
    volume = pl.DataFrame(
        {
            "symbol": ["V"] * 4,
            "day": [utc(2021, 1, d) for d in (1, 2, 4, 5)],  # no trading on the 3rd
            "volume": [1.0, 1.0, 1.0, 99.0],
        }
    )
    liq = {r["day"]: r["liquidity"] for r in fc.liquidity(volume, days=2, min_days=2).to_dicts()}
    assert liq[utc(2021, 1, 3)] == 1.0  # from the 1st and 2nd
    assert utc(2021, 1, 4) not in liq  # the 2nd and an empty 3rd: one day is too few
    assert liq[utc(2021, 1, 6)] == 50.0  # the spike on the 5th is known only on the 6th


def test_thirds_take_fresh_contracts_with_enough_volume():
    t = utc(2021, 1, 1)
    names = list("ABCDEFGH")
    prices = pl.DataFrame(
        {"symbol": names, "time": [t] * 8, "price": [1.0] * 8, "fresh": [True] * 6 + [False, True]}
    ).with_columns(pl.col("time").cast(T))
    liq = pl.DataFrame(
        {"symbol": names, "day": [t] * 8, "liquidity": [6.0, 5, 4, 3, 2, 1, 9, 0.5]}
    ).with_columns(pl.col("day").cast(T))
    thirds = fc.eligible_thirds(prices, liq, min_volume=1.0)
    got = dict(zip(thirds["symbol"], thirds["third"], strict=True))
    # G has no fresh price, H too little volume: neither is counted.
    assert got == {"F": 1, "E": 1, "D": 2, "C": 2, "B": 3, "A": 3}


def test_a_contract_without_a_spread_estimate_pays_the_widest_one():
    t = utc(2021, 1, 1)
    prices = pl.DataFrame({"symbol": ["P", "Q", "R"], "time": [t] * 3}).with_columns(
        pl.col("time").cast(T)
    )
    spread = pl.DataFrame(
        {"symbol": ["P", "Q"], "day": [t] * 2, "half_spread": [0.001, 0.003]}
    ).with_columns(pl.col("day").cast(T))
    costs = fc.cost_table(spread, prices, fee=0.0005, with_spread=True).sort("symbol").to_dicts()
    assert [r["cost_rate"] for r in costs] == pytest.approx([0.0015, 0.0035, 0.0035])
    assert [r["fallback"] for r in costs] == [False, False, True]
    maker = fc.cost_table(spread, prices, fee=0.0002, with_spread=False)
    assert maker["cost_rate"].to_list() == pytest.approx([0.0002] * 3)


@pytest.mark.parametrize(
    ("full", "recent", "bear", "effect", "passes"),
    [
        (8.0, 5.0, 2.0, 4.0, True),  # half of 8 is 4, below the recent 5
        (8.0, 3.0, 2.0, 3.0, True),  # the recent year is weaker: it decides
        (8.0, 2.0, 2.0, 2.0, False),  # below the hurdle of about 2.90
        (8.0, 5.0, -0.5, 0.0, False),  # 2022 went the other way
        (-1.0, 5.0, 2.0, 0.0, False),  # loses money over the whole sample
        (8.0, None, 2.0, 0.0, False),  # no recent year to check
    ],
)
def test_the_power_test_follows_decision_0026(full, recent, bear, effect, passes):
    summary = {"sharpe_annual_hac": full, "sharpe_last_365_days_hac": recent}
    summary["sharpe_by_year"] = {"2022": bear}
    test = fc.power_test(summary, PLANNED_A)
    assert test["effect_for_power"] == pytest.approx(effect)
    assert test["passes"] is passes
    assert (test["power"] >= 0.8) is passes
    assert test["hurdle"] == pytest.approx(2.90, abs=0.01)


def test_the_power_test_judges_the_sharpe_corrected_for_autocorrelation():
    """Raw values that would pass, corrected values that do not: the corrected ones decide."""
    summary = {
        "sharpe_annual": 8.0,
        "sharpe_last_365_days": 6.0,
        "sharpe_annual_hac": 4.0,
        "sharpe_last_365_days_hac": 2.0,
        "sharpe_by_year": {"2022": 1.0},
    }
    test = fc.power_test(summary, PLANNED_A)
    assert (test["effect_for_power"], test["passes"]) == (2.0, False)


# --- whole runs on synthetic markets --------------------------------------------

FIRST, LAST = utc(2021, 6, 1), utc(2024, 1, 1)


def market(edge: float, symbols: int = 40, seed: int = 9) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Hourly klines and 8-hourly funding from mid 2021 to the end of 2023. Funding rates
    persist (each settlement keeps 90 % of the last one's deviation); over the 8 hours
    after a settlement the contract moves by -edge x rate plus noise, so with edge > 0
    low-funding contracts outperform."""
    rng = random.Random(seed)
    k_rows, f_rows = [], []
    for i in range(symbols):
        name, price, drift, x = f"S{i:02d}USDT", 100.0, 0.0, 0.0
        for h in range((LAST - FIRST) // H):
            t = FIRST + h * H
            if t.hour % 8 == 0:
                x = 0.9 * x + rng.gauss(0.0, 0.0003)
                rate = 0.0001 + x
                f_rows.append((name, t, 8, rate))
                drift = -edge * rate / 8
            price *= math.exp(drift + rng.gauss(0, 0.004))
            quote = 2e6 * (i + 1)
            k_rows.append((name, t, price, price * 1.001, price * 0.999, quote / price, quote))
    funding = pl.DataFrame(
        f_rows,
        schema=["symbol", "calc_time", "funding_interval_hours", "funding_rate"],
        orient="row",
    ).with_columns(pl.col("calc_time").cast(T), knowable_time=pl.col("calc_time").cast(T))
    return frame(k_rows), funding


def bounce_market(symbols: int = 40, seed: int = 5, half: float = 0.005) -> pl.DataFrame:
    """No reversal at all: mids follow random walks, and each hour's last trade lands on
    the bid or the ask at random. Trades on both sides average out to the mid."""
    rng = random.Random(seed)
    rows = []
    for i in range(symbols):
        name, mid = f"B{i:02d}USDT", 100.0
        for h in range((LAST - FIRST) // H):
            mid *= math.exp(rng.gauss(0, 0.004))
            close = mid * (1 + (half if rng.random() < 0.5 else -half))
            quote = 2e6 * (i + 1)
            high, low = mid * (1 + half + 0.001), mid * (1 - half - 0.001)
            rows.append((name, FIRST + h * H, close, high, low, quote / mid, quote))
    return frame(rows)


def synthetic(params: dict) -> dict:
    """The planned parameters on the synthetic window. 40 contracts split into thirds
    leave about 13 per third, so B's minimum of 20 names is lowered to 10 here only."""
    names = 10 if params["candidate"] == "B" else params["min_names"]
    window = {"start": "2021-08-01", "end": "2023-12-30"}
    return {**params, **window, "min_names": names}


@pytest.fixture(scope="module")
def strong() -> tuple[pl.DataFrame, pl.DataFrame]:
    return market(edge=40.0)


@pytest.fixture(scope="module")
def flat() -> tuple[pl.DataFrame, pl.DataFrame]:
    """Prices ignore funding. B's null; not A's: with persistent rates and nothing in
    prices to offset them, shorting high funding collects real carry."""
    return market(edge=0.0)


@pytest.fixture(scope="module")
def fair() -> tuple[pl.DataFrame, pl.DataFrame]:
    """A's null: prices rise by exactly the funding longs pay (edge -1), so the premium
    is fully priced and carry plus price earns nothing before costs."""
    return market(edge=-1.0)


def test_a_finds_a_strong_funding_edge_and_passes(strong):
    outcome = fc.evaluate(*strong, synthetic(PLANNED_A))
    assert outcome.verdict == "通过", outcome.metrics["power_test"]
    assert list(outcome.sections) == list(SECTIONS)
    est = outcome.metrics["estimate"]
    assert est["taker"]["cost_annual"] > 0  # costs are charged
    assert est["maker_reference"]["cost_annual"] < est["taker"]["cost_annual"]
    assert est["cost_fallback_share"] == 0.0


def test_a_without_an_edge_does_not_pass(fair):
    outcome = fc.evaluate(*fair, synthetic(PLANNED_A))
    assert outcome.verdict == "不通过"
    est = outcome.metrics["estimate"]
    assert est["carry_only"]["sharpe_annual"] > 3  # the funding is there...
    assert est["gross_price_only"]["sharpe_annual"] < -3  # ...and prices take it back
    assert outcome.metrics["power_test"]["effect_for_power"] == 0.0


def test_b_runs_and_a_random_walk_does_not_pass(flat):
    outcome = fc.evaluate(flat[0], None, synthetic(PLANNED_B))
    assert outcome.verdict == "不通过"
    assert list(outcome.sections) == list(SECTIONS)
    est = outcome.metrics["estimate"]
    assert set(est) == {
        "least_liquid",
        "most_liquid",
        "least_minus_most_gross",
        "taker",
        "maker_reference",
    }
    assert est["taker"]["cost_annual"] > 0


def test_b_is_not_fooled_by_the_bid_ask_bounce(monkeypatch):
    """With the last trade's price, the bounce alone looks like a strong reversal; with
    the hour's average price, there is nothing to find."""
    klines, params = bounce_market(), synthetic(PLANNED_B)
    honest = fc.evaluate(klines, None, params).metrics["estimate"]["least_liquid"]
    monkeypatch.setattr(fc, "vwap_bars", lambda k: k)  # the naive signal
    naive = fc.evaluate(klines, None, params).metrics["estimate"]["least_liquid"]
    assert naive["gross_price_only"]["t_stat"] > 3
    assert abs(honest["gross_price_only"]["t_stat"]) < 3


def test_an_unknown_candidate_is_an_error(flat):
    with pytest.raises(ValueError, match="candidate"):
        fc.evaluate(flat[0], flat[1], {**PLANNED_A, "candidate": "C"})
    with pytest.raises(ValueError, match="资金费率"):
        fc.evaluate(flat[0], None, PLANNED_A)


class RecordingPit:
    def __init__(self, tables: dict[str, pl.DataFrame]) -> None:
        self.tables, self.calls = tables, []

    def table(self, dataset, *, end, start=None, columns=None):
        self.calls.append({"dataset": dataset, "start": start, "end": end, "columns": columns})
        return self.tables[dataset]


@pytest.mark.parametrize(
    ("planned", "datasets"),
    [(PLANNED_A, {"um-klines-1h", "um-funding"}), (PLANNED_B, {"um-klines-1h"})],
)
def test_the_planned_cards_read_only_exploration(flat, planned, datasets):
    pit = RecordingPit({"um-klines-1h": flat[0], "um-funding": flat[1]})
    params = synthetic(planned)
    assert (params["read_from"], params["read_until"]) == (
        planned["read_from"],
        planned["read_until"],
    )
    fc.run(pit, params)  # type: ignore[arg-type]
    assert {c["dataset"] for c in pit.calls} == datasets
    for call in pit.calls:
        assert utc(2019, 12, 1) <= call["start"]  # exploration starts here
        assert call["end"] < utc(2024, 2, 1)  # and confirmation here
    assert "volume" in pit.calls[0]["columns"]


def test_the_planned_windows():
    """The last time is only an exit, and its price must still be in exploration: 00:01
    on 31 January for A (the next would need 1 February), 00:00 that day for B."""
    a = fc.rebalance_times(
        fc.day(PLANNED_A["start"]) + timedelta(minutes=1),
        fc.day(PLANNED_A["end"]) + timedelta(minutes=1),
        timedelta(hours=PLANNED_A["rebalance_hours"]),
    )
    assert (a[0], a[-1]) == (utc(2020, 1, 1, 0, 1), utc(2024, 1, 31, 0, 1))
    assert a[-1] + H < fc.day(PLANNED_A["read_until"])  # its price bar is in the read
    b = fc.rebalance_times(fc.day(PLANNED_B["start"]), fc.day(PLANNED_B["end"]), 24 * H)
    assert (b[0], b[-1]) == (utc(2020, 1, 1), utc(2024, 1, 31))
    first_liquidity = fc.day(PLANNED_A["start"]) - fc.day(PLANNED_A["read_from"])
    assert first_liquidity >= timedelta(days=PLANNED_A["liquidity_min_days"])


def test_the_registered_cards_carry_the_reviewed_parameters():
    """T-0004 and T-0005 must run exactly what was reviewed: PLANNED_A and PLANNED_B."""
    from pathlib import Path

    from sqp.research import trials

    text = (Path(__file__).resolve().parents[1] / trials.TRIALS).read_text("utf-8")
    cards = {t.hypothesis: t for t in trials.parse(text) if t.study == "first_candidates"}
    assert set(cards) == {"H-0002", "H-0003"}
    assert dict(cards["H-0002"].params) == PLANNED_A
    assert dict(cards["H-0003"].params) == PLANNED_B
    assert all(t.segment == "exploration" and not t.repeat_of for t in cards.values())
