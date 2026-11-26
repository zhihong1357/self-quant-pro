"""Candidates A and B for the first confirmation test, estimated on exploration (G2).

The research map (section 1) and decision 0026 fix what is estimated and how the
result is judged, before any of it is run. One module serves both, chosen by
params["candidate"], because studies may not import each other.

A, H-0002: cross-sectional funding on small and mid perpetuals. Every day, one minute
after the 00:00 UTC settlement, sort the eligible contracts by the average funding rate
(scaled to 8 hours) settled over the day that just ended, go long the lowest fifth and
short the highest fifth, hold for a day, through three settlements. Daily, on a day's
average, rather than every 8 hours on the latest rate: the premium the hypothesis is
about persists for days, while a single rate is noisy, and re-sorting on noise every 8
hours turns the book over hundreds of times a year, which at taker costs no carry can
pay for (a rehearsal on synthetic data showed about 700 times). Eligible: a fresh price,
median daily quote volume over the previous 30 days of at least `min_volume`, and not
in the most liquid third of those (the large contracts, where arbitrage capital is
plentiful and the published carry has decayed). The return has two parts, reported
apart: the price change, and the funding paid or received at the settlements the
positions are held through.

H-0009 is A rebalanced weekly on the week's average rate (`rebalance_hours` = 168):
T-0004 found the daily ranks churning, and turnover costs ate most of the premium.

B, H-0003: liquidity and short-term reversal. Every day at 00:00 UTC, split the
eligible contracts into thirds by the same volume; within the least liquid third, go
long the fifth that fell most over the previous day and short the fifth that rose most,
hold one day. The previous day's return is measured between the volume-weighted
average prices of the last hour of each day, so the bid-ask bounce of a single last
trade cannot create a reversal that is not there. The most liquid third is run the
same way as the comparison the mechanism predicts to be weaker; it is reported only.

Costs (decision 0026): every trade pays the taker fee plus half the bid-ask spread,
estimated per contract from the previous 30 days of hourly highs, lows and closes
(Abdi and Ranaldo 2017). Maker costs (fee only) are reported as a reference. Only a
contract whose own spread can be estimated is tradable, so every entry pays its own.

There is one run on real data, and a crash counts as a trial: a bar without positive,
finite prices or a funding record without a finite rate is dropped, a settlement
recorded twice is averaged, a missing or zero settlement interval counts as 8 hours,
and a comparison that is only reported gives no summary when it has too few periods.
For the portfolio that is judged, too few periods are an error: a run that could not be
estimated must not look like one that failed the test. How much dirty data was met is
reported under data_issues and in the report.

The power test (decision 0026): effect = min(half the full-sample annualised net
Sharpe, the Sharpe over the last 365 days), both corrected for autocorrelation (the
engine's `_hac` values: Newey-West over at least a week, never corrected upward), and 0
unless both the full sample and 2022 are positive. The candidate passes ("通过") if,
at that effect, the probability of t >= 3 on the confirmation segment (639 days) is at
least 80 %. Only the verdict reaches the public log; every number stays in the
private bucket.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from sqp.data.pit import PointInTime
from sqp.research.engine import (
    period_returns,
    portfolio_returns,
    prices_at,
    quantile_weights,
    rebalance_times,
    rounded,
    summarize,
)
from sqp.research.studies import SECTIONS, Outcome
from sqp.stats import detection_power, min_detectable_sharpe

KLINES, FUNDING = "um-klines-1h", "um-funding"
DAY, HOUR = timedelta(days=1), timedelta(hours=1)
TIME = pl.Datetime("us", "UTC")
COLUMNS = ["symbol", "open_time", "close", "high", "low", "volume", "quote_volume"]


def day(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# --- shared inputs -----------------------------------------------------------


def every_day(table: pl.DataFrame) -> pl.DataFrame:
    """`table` (symbol, day, ...) on every calendar day from its first to its last, the
    missing days null: a rolling window of n rows is then n days."""
    if table.is_empty():
        return table
    days = pl.datetime_range(
        table["day"].min(), table["day"].max(), "1d", time_unit="us", time_zone="UTC", eager=True
    )
    grid = table.select("symbol").unique().join(pl.DataFrame({"day": days}), how="cross")
    return grid.join(table, on=["symbol", "day"], how="left").sort("symbol", "day")


def valid_bars(klines: pl.DataFrame) -> pl.DataFrame:
    """Bars with positive, finite prices and a finite, non-negative volume. A zero or
    missing low would make the spread estimate infinite and the run crash."""
    prices = [pl.col(c).is_finite() & (pl.col(c) > 0) for c in ("close", "high", "low")]
    volume = pl.col("quote_volume").is_finite() & (pl.col("quote_volume") >= 0)
    return klines.filter(pl.all_horizontal(*prices) & volume)


def settlements(funding: pl.DataFrame) -> pl.DataFrame:
    """symbol, calc_time, knowable_time, funding_rate, per_8h: one row per settlement,
    with a finite rate. A settlement recorded twice is averaged, not counted twice; a
    missing or non-positive interval counts as 8 hours."""
    hours = (
        pl.when(pl.col("funding_interval_hours") > 0)
        .then(pl.col("funding_interval_hours"))
        .otherwise(8)
    )
    return (
        funding.filter(pl.col("funding_rate").is_finite())
        .with_columns(
            pl.col("calc_time", "knowable_time").cast(TIME),
            per_8h=pl.col("funding_rate") * 8 / hours,
        )
        .group_by("symbol", "calc_time")
        .agg(
            pl.col("knowable_time").min(),
            pl.col("funding_rate").mean(),
            pl.col("per_8h").mean(),
        )
        .sort("calc_time", "symbol")
    )


def daily_volume(klines: pl.DataFrame) -> pl.DataFrame:
    """symbol, day, volume: quote volume per UTC day (by the bar's open time; the day's
    last bar closes, and becomes knowable, at the next day's 00:00)."""
    return klines.group_by(
        "symbol", pl.col("open_time").cast(TIME).dt.truncate("1d").alias("day")
    ).agg(pl.col("quote_volume").sum().alias("volume"))


def liquidity(volume: pl.DataFrame, days: int, min_days: int) -> pl.DataFrame:
    """symbol, day, liquidity: the median daily volume over the `days` days before `day`
    (so it is knowable at day's 00:00), with at least `min_days` of them traded."""
    rolled = every_day(volume).with_columns(
        liquidity=pl.col("volume").rolling_median(days, min_samples=min_days).over("symbol"),
    )
    # The window ending yesterday applies today.
    return rolled.select("symbol", (pl.col("day") + DAY).alias("day"), "liquidity").drop_nulls(
        "liquidity"
    )


def half_spread(klines: pl.DataFrame, days: int, min_days: int) -> pl.DataFrame:
    """symbol, day, half_spread: half the bid-ask spread estimated from consecutive hourly
    bars over the `days` days before `day` (Abdi and Ranaldo 2017: s^2 = 4 E[(c_t - m_t)
    (c_t - m_t+1)], c the log close, m the log mid-range); negatives count as 0.

    Each term needs the next bar, so it is dated by the next bar's day: a day's terms
    are all knowable when that day ends. After a contract stops trading its last
    estimate carries on, so that its exit still pays a spread."""
    terms = (
        klines.select(
            "symbol",
            pl.col("open_time").cast(TIME),
            pl.col("close").log().alias("c"),
            ((pl.col("high").log() + pl.col("low").log()) / 2).alias("m"),
        )
        .sort("symbol", "open_time")
        .with_columns(
            m_next=pl.col("m").shift(-1).over("symbol"),
            next_open=pl.col("open_time").shift(-1).over("symbol"),
        )
        .filter(pl.col("next_open") - pl.col("open_time") == HOUR)  # consecutive bars only
        .with_columns(term=4 * (pl.col("c") - pl.col("m")) * (pl.col("c") - pl.col("m_next")))
        .filter(pl.col("term").is_finite())
    )
    daily = terms.group_by("symbol", pl.col("next_open").dt.truncate("1d").alias("day")).agg(
        pl.col("term").mean()
    )
    rolled = (
        every_day(daily)
        .with_columns(s2=pl.col("term").rolling_mean(days, min_samples=min_days).over("symbol"))
        .with_columns(pl.col("s2").forward_fill().over("symbol"))
    )
    # clip keeps a missing estimate missing (it does not turn it into 0).
    return rolled.select(
        "symbol",
        (pl.col("day") + DAY).alias("day"),
        (pl.col("s2").clip(lower_bound=0.0).sqrt() / 2).alias("half_spread"),
    ).drop_nulls("half_spread")


def by_day(table: pl.DataFrame, rows: pl.DataFrame) -> pl.DataFrame:
    """Attach a per-day table (symbol, day, ...) to rows (symbol, time) by the time's UTC
    day; rows without a value for that day get nulls."""
    return (
        rows.with_columns(day=pl.col("time").dt.truncate("1d"))
        .join(table, on=["symbol", "day"], how="left")
        .drop("day")
    )


def cost_table(
    spread: pl.DataFrame, prices: pl.DataFrame, fee: float, with_spread: bool
) -> pl.DataFrame:
    """symbol, time, cost_rate, fallback: the one-way cost of trading each contract at each
    time. Only contracts with their own spread estimate are tradable, and a contract
    that stops trading keeps its last one, so this should never be needed; as a safety
    net, a contract without one pays the widest estimate known at that time (never one
    from later). How often that happens is reported."""
    rows = by_day(spread, prices.select("symbol", "time")).with_columns(
        fallback=pl.col("half_spread").is_null()
    )
    widest = (
        rows.group_by("time")
        .agg(widest=pl.col("half_spread").max())
        .sort("time")
        .with_columns(pl.col("widest").forward_fill())  # only what was known by then
    )
    rows = rows.join(widest, on="time", how="left").with_columns(
        pl.col("half_spread").fill_null(pl.col("widest"))
    )
    spread_part = pl.col("half_spread") if with_spread else pl.lit(0.0)
    return rows.select("symbol", "time", (fee + spread_part).alias("cost_rate"), "fallback")


def fallback_share(weights: pl.DataFrame, costs: pl.DataFrame) -> float | None:
    held = weights.join(costs, on=["symbol", "time"], how="left")
    return rounded(held["fallback"].fill_null(True).mean()) if held.height else None


def eligible_thirds(
    prices: pl.DataFrame, liq: pl.DataFrame, spread: pl.DataFrame, min_volume: float
) -> pl.DataFrame:
    """symbol, time, third: contracts with a fresh price, enough volume and their own
    spread estimate, split into thirds by liquidity within each time (1 = least
    liquid), ties broken by symbol."""
    rows = by_day(spread, by_day(liq, prices.filter("fresh").select("symbol", "time")))
    return (
        rows.filter((pl.col("liquidity") >= min_volume) & pl.col("half_spread").is_not_null())
        .sort("time", "liquidity", "symbol")
        .with_columns(n=pl.len().over("time"), rank=pl.int_range(pl.len()).over("time"))
        .select("symbol", "time", (pl.col("rank") * 3 // pl.col("n") + 1).alias("third"))
    )


def summary(periods: pl.DataFrame, ppy: int, column: str = "net") -> dict | None:
    """The engine's summary, or None when there are too few periods for one."""
    return summarize(periods, column=column, periods_per_year=ppy) if periods.height >= 4 else None


def run_portfolio(
    weights: pl.DataFrame,
    returns: pl.DataFrame,
    carry: pl.DataFrame | None,
    taker: pl.DataFrame,
    maker: pl.DataFrame,
    ppy: int,
) -> tuple[dict, pl.DataFrame]:
    """Taker (judged), maker (reference) and the parts of the taker result; and the
    taker series itself."""
    net = portfolio_returns(weights, returns, carry=carry, cost_rate=taker)
    ref = portfolio_returns(weights, returns, carry=carry, cost_rate=maker)
    names = net["longs"].mean() + net["shorts"].mean() if net.height else None
    out = {
        "taker": summary(net, ppy),
        "maker_reference": summary(ref, ppy),
        "gross_price_only": summary(net, ppy, column="gross"),
        "names_per_period": rounded(float(names)) if names is not None else None,
        "cost_fallback_share": fallback_share(weights, taker),
    }
    if carry is not None:
        out["carry_only"] = summary(net, ppy, column="carry")
    return out, net


# --- candidate A: funding ------------------------------------------------------


def funding_signal(funding: pl.DataFrame, times: list[datetime], window: timedelta) -> pl.DataFrame:
    """symbol, time, value, knowable_time: the average rate, scaled to 8 hours, of the
    settlements that became knowable in (time - window, time]. A contract with none in
    that window has no signal."""
    rates = (
        settlements(funding)
        .select("symbol", "knowable_time", pl.col("per_8h").alias("value"))
        .filter(pl.col("value").is_finite())
        .sort("knowable_time")
    )
    starts = pl.DataFrame({"time": pl.Series(times).cast(TIME)}).sort("time")
    # Each settlement goes to the first rebalance at or after it became knowable.
    assigned = rates.join_asof(
        starts, left_on="knowable_time", right_on="time", strategy="forward"
    ).filter(pl.col("time") - pl.col("knowable_time") < window)
    return (
        assigned.sort("symbol", "time", "knowable_time")
        .group_by("symbol", "time")
        .agg(pl.col("value").mean(), pl.col("knowable_time").max())
        .sort("time", "symbol")
    )


def funding_carry(funding: pl.DataFrame, times: list[datetime]) -> pl.DataFrame:
    """symbol, time, carry: what a long position held over (time, next time] earns from
    funding, minus every rate settled in that window (longs pay positive rates).
    Settlements are on the hour; the recorded time can be a little later, so it is
    rounded down to its hour first."""
    starts = pl.DataFrame({"time": pl.Series(times).cast(TIME)}).sort("time")
    settled = (
        settlements(funding)
        .select("symbol", pl.col("calc_time").dt.truncate("1h").alias("settled"), "funding_rate")
        .sort("settled")
    )
    # Each settlement belongs to the last rebalance strictly before it.
    owner = settled.join_asof(
        starts.with_columns(start=pl.col("time")),
        left_on="settled",
        right_on="start",
        strategy="backward",
        allow_exact_matches=False,
    )
    return (
        owner.drop_nulls("time")
        .group_by("symbol", "time")
        .agg((-pl.col("funding_rate").sum()).alias("carry"))
    )


def data_issues(klines: pl.DataFrame, bars: pl.DataFrame, funding: pl.DataFrame | None) -> dict:
    out = {"bars_dropped": klines.height - bars.height}
    if funding is not None:
        finite = funding.filter(pl.col("funding_rate").is_finite())
        out["funding_rows_dropped"] = funding.height - finite.height
        out["settlements_recorded_twice"] = finite.height - settlements(funding).height
        out["intervals_assumed_8h"] = funding.filter(
            pl.col("funding_interval_hours").fill_null(0) <= 0
        ).height
    return out


def candidate_a(klines: pl.DataFrame, funding: pl.DataFrame, p: dict) -> dict:
    bars = valid_bars(klines)
    offset = timedelta(minutes=int(p["offset_minutes"]))
    every = timedelta(hours=int(p["rebalance_hours"]))
    times = rebalance_times(day(p["start"]) + offset, day(p["end"]) + offset, every)
    prices = prices_at(bars, times, max_staleness=HOUR)
    returns = period_returns(prices)
    liq = liquidity(daily_volume(bars), int(p["liquidity_days"]), int(p["liquidity_min_days"]))
    spread = half_spread(bars, int(p["spread_days"]), int(p["spread_min_days"]))
    thirds = eligible_thirds(prices, liq, spread, float(p["min_volume"]))
    small = thirds.filter((pl.col("third") <= 2) & (pl.col("time") < times[-1]))
    tradable = prices.join(small, on=["symbol", "time"], how="semi")
    signal = funding_signal(funding, times, window=every)
    weights = quantile_weights(
        signal, tradable, groups=int(p["groups"]), long="low", min_names=int(p["min_names"])
    )
    taker = cost_table(spread, prices, float(p["taker_fee"]), with_spread=True)
    maker = cost_table(spread, prices, float(p["maker_fee"]), with_spread=False)
    carry = funding_carry(funding, times)
    ppy = round(timedelta(days=365) / every)
    out, _ = run_portfolio(weights, returns, carry, taker, maker, ppy=ppy)
    return out | {"data_issues": data_issues(klines, bars, funding)}


# --- candidate B: reversal ---------------------------------------------------------


def vwap_bars(klines: pl.DataFrame) -> pl.DataFrame:
    """The bars with the hour's volume-weighted average price in place of the close. The
    close is a single trade at the bid or the ask; the average over the hour is not."""
    return klines.filter(pl.col("volume") > 0).with_columns(
        close=pl.col("quote_volume") / pl.col("volume")
    )


def candidate_b(klines: pl.DataFrame, p: dict) -> dict:
    bars = valid_bars(klines)
    times = rebalance_times(day(p["start"]), day(p["end"]), DAY)
    prices = prices_at(bars, times, max_staleness=HOUR)
    returns = period_returns(prices)
    moved = period_returns(prices_at(vwap_bars(bars), times, max_staleness=HOUR))
    past = moved.select(
        "symbol",
        pl.col("next_time").alias("time"),
        pl.col("ret").alias("value"),
        pl.col("next_time").alias("knowable_time"),
    )
    liq = liquidity(daily_volume(bars), int(p["liquidity_days"]), int(p["liquidity_min_days"]))
    spread = half_spread(bars, int(p["spread_days"]), int(p["spread_min_days"]))
    thirds = eligible_thirds(prices, liq, spread, float(p["min_volume"]))
    taker = cost_table(spread, prices, float(p["taker_fee"]), with_spread=True)
    maker = cost_table(spread, prices, float(p["maker_fee"]), with_spread=False)
    out, gross = {}, {}
    for name, third in (("least_liquid", 1), ("most_liquid", 3)):
        members = thirds.filter((pl.col("third") == third) & (pl.col("time") < times[-1]))
        tradable = prices.join(members, on=["symbol", "time"], how="semi")
        weights = quantile_weights(
            past, tradable, groups=int(p["groups"]), long="low", min_names=int(p["min_names"])
        )
        out[name], series = run_portfolio(weights, returns, None, taker, maker, ppy=365)
        gross[name] = series.select("time", "gross")
    # The mechanism's corollary, before costs: reversal stronger where liquidity is worse.
    gap = (
        gross["least_liquid"]
        .join(gross["most_liquid"], on="time", suffix="_most")
        .select("time", (pl.col("gross") - pl.col("gross_most")).alias("net"))
    )
    out["least_minus_most_gross"] = summary(gap, 365)  # reported only: may be None
    # What the power test judges, and its maker reference: the least liquid third.
    out["taker"] = out["least_liquid"]["taker"]
    out["maker_reference"] = out["least_liquid"]["maker_reference"]
    return out | {"data_issues": data_issues(klines, bars, None)}


# --- the power test (decision 0026) ---------------------------------------------


def power_test(result: dict | None, p: dict) -> dict:
    # Returns that follow each other overstate a Sharpe ratio annualised from short
    # periods (Lo 2002): the corrected values are the ones judged. No summary (too few
    # periods) is no evidence: the effect is 0.
    result = result or {"sharpe_annual_hac": None, "sharpe_last_365_days_hac": None}
    full = result["sharpe_annual_hac"]
    recent = result["sharpe_last_365_days_hac"]
    bear = result.get("sharpe_by_year", {}).get(str(p["bear_year"]))
    usable = full is not None and full > 0 and bear is not None and bear > 0 and recent is not None
    effect = min(full / 2, recent) if usable else 0.0
    years = int(p["confirmation_days"]) / 365
    power = detection_power(max(effect, 0.0), years, float(p["t"]))
    return {
        "full_sharpe": full,
        "recent_sharpe": recent,
        "bear_year_sharpe": bear,
        "effect_for_power": rounded(effect),
        "hurdle": rounded(min_detectable_sharpe(years, float(p["t"]), float(p["power"]))),
        "power": rounded(power),
        "prior": float(p["prior"]),
        "prior_times_power": rounded(float(p["prior"]) * power),
        "passes": power >= float(p["power"]),
    }


def evaluate(klines: pl.DataFrame, funding: pl.DataFrame | None, p: dict) -> Outcome:
    if p["candidate"] == "A":
        if funding is None:
            raise ValueError("候选 A 需要资金费率")
        result = candidate_a(klines, funding, p)
    elif p["candidate"] == "B":
        result = candidate_b(klines, p)
    else:
        raise ValueError('candidate 只能是 "A" 或 "B"')
    if result["taker"] is None:
        # Loud, not a quiet "不通过": nothing was estimated (Gemini's third review).
        raise ValueError("被检验的组合不到 4 期，无法估计：多半是数据或参数出了问题")
    test = power_test(result["taker"], p)
    metrics = {"candidate": p["candidate"], "estimate": result, "power_test": test}
    verdict = "通过" if test["passes"] else "不通过"
    return Outcome(metrics=metrics, sections=sections(metrics, p), verdict=verdict)


def run(pit: PointInTime, params: dict) -> Outcome:
    first, end = day(params["read_from"]), day(params["read_until"])
    klines = pit.table(KLINES, start=first, end=end, columns=COLUMNS)
    funding = None
    if params["candidate"] == "A":
        funding = pit.table(
            FUNDING,
            start=first,
            end=end,
            columns=["symbol", "calc_time", "funding_interval_hours", "funding_rate"],
        )
    return evaluate(klines, funding, params)


# --- the report -------------------------------------------------------------------


def f3(x: float | None) -> str:
    return "无" if x is None else f"{x:.3f}"


NONE = {
    k: None
    for k in (
        "mean_annual",
        "vol_annual",
        "sharpe_annual",
        "t_stat",
        "t_stat_hac",
        "max_drawdown",
        "turnover_annual",
        "cost_annual",
        "sharpe_last_365_days",
        "autocorrelation_factor",
        "positive_quarters",
    )
} | {"sharpe_by_year": {}}


def by_year(s: dict) -> str:
    return "，".join(f"{y} 年 {f3(v)}" for y, v in s["sharpe_by_year"].items()) or "无"


def row(label: str, s: dict) -> str:
    return (
        f"| {label} | {f3(s['mean_annual'])} | {f3(s['vol_annual'])} | "
        f"{f3(s['sharpe_annual'])} | {f3(s['t_stat'])} | {f3(s['max_drawdown'])} | "
        f"{f3(s['turnover_annual'])} | {f3(s['cost_annual'])} |"
    )


def dirty(issues: dict) -> str:
    text = f"遇到的脏数据：丢掉 K 线 {issues['bars_dropped']} 根"
    if "funding_rows_dropped" in issues:
        text += (
            f"、资金费记录 {issues['funding_rows_dropped']} 条；"
            f"同一次结算记了两次 {issues['settlements_recorded_twice']} 次；"
            f"结算间隔按 8 小时处理 {issues['intervals_assumed_8h']} 条"
        )
    return text + "。"


def sections(m: dict, p: dict) -> dict:
    c, est, test = m["candidate"], m["estimate"], m["power_test"]
    taker = est["taker"] or NONE
    verdict = "通过" if test["passes"] else "不通过"
    if c == "A":
        name = "中小合约的横截面资金费率"
        who = "流动性最好的三分之一除外"
        how = (
            f"每 {p['rebalance_hours']} 小时（00:00 结算后 1 分钟）"
            "按这段时间里结算的资金费率的平均值（折算成 8 小时）"
        )
        fallback = est["cost_fallback_share"]
        parts = (
            "拆开看（吃单）：只算价格变动的年化夏普 "
            f"{f3((est['gross_price_only'] or NONE)['sharpe_annual'])}，"
            f"只算资金费的 {f3((est['carry_only'] or NONE)['sharpe_annual'])}。"
        )
        rows = [("吃单", taker), ("挂单（参考）", est["maker_reference"] or NONE)]
        names = est["names_per_period"]
    else:
        name = "流动性越差、反转越强"
        who = "只取流动性最差的三分之一；流动性最好的三分之一同样做一遍作对照"
        how = "每天 00:00（UTC）按前一天的涨跌（用每天最后一小时的成交均价计算）"
        least, most = est["least_liquid"], est["most_liquid"]
        fallback = least["cost_fallback_share"]
        parts = (
            "对照：流动性最好的三分之一，吃单后年化夏普 "
            f"{f3((most['taker'] or NONE)['sharpe_annual'])}；成本前，最差减最好的年化夏普 "
            f"{f3((est['least_minus_most_gross'] or NONE)['sharpe_annual'])}（机制预测为正）。"
        )
        rows = [
            ("吃单", taker),
            ("挂单（参考）", est["maker_reference"] or NONE),
            ("对照：流动性最好，吃单", most["taker"] or NONE),
        ]
        names = least["names_per_period"]
    text = {
        "结论": (
            f"候选 {c}：{name}。按吃单成本，探索集全段年化夏普 {f3(taker['sharpe_annual'])}，"
            f"修正自相关后 {f3(test['full_sharpe'])}（t 值 {f3(taker['t_stat_hac'])}）；"
            f"最近 365 天修正后 {f3(test['recent_sharpe'])}；"
            f"{p['bear_year']} 年 {f3(test['bear_year_sharpe'])}。"
            f"用于功效的效应 {f3(test['effect_for_power'])}，门槛 {f3(test['hurdle'])}，"
            f"确认集上的把握 {f3(test['power'])}：功效检验{verdict}。{parts}"
        ),
        # The runner's heading names the card: more than one card runs candidate A.
        "假说与机制": "见报告标题里的假说卡（registry/hypotheses/ 下的同名文件）。",
        "数据": (
            "Binance U 本位永续，经时点接口只读探索集：1 小时 K 线"
            + ("和资金费率" if c == "A" else "")
            + f"，组合从 {p['start']} 到 {p['end']}。合约要有新鲜价格，"
            f"前 {p['liquidity_days']} 天日成交额中位数不低于 {p['min_volume']:,} 美元，"
            f"{who}。已下架的合约按最后的价格出场，留在样本里。" + dirty(est["data_issues"])
        ),
        "方法": (
            f"{how}分 {p['groups']} 组，做多低的一端、做空高的一端，美元中性，"
            f"合约数不少于 {p['min_names']} 的时刻才建仓，走 sqp.research.engine。"
            f"成本：吃单 {p['taker_fee']} 加半个买卖价差（Abdi 与 Ranaldo 2017，"
            f"用前 {p['spread_days']} 天的小时 K 线估计）；挂单 {p['maker_fee']} 只作参考。"
            f"没有自己的价差估计、按当时最宽价差收费的持仓占 {f3(fallback)}。"
            "功效检验按决策 0026，用修正了自相关的夏普（Newey–West，回看至少一周，"
            f"只往下修；这次的放大倍数 {f3(taker['autocorrelation_factor'])}）。"
            "t 值还修正了偏度和峰度。"
            "没有模拟杠杆和强平（总敞口 1 倍）。"
        ),
        "结果": (
            "| 口径 | 年化收益 | 年化波动 | 年化夏普 | t 值 | 最大回撤 | 年换手 | 年成本 |\n"
            "|---|---|---|---|---|---|---|---|\n"
            + "\n".join(row(label, s) for label, s in rows)
            + f"\n\n逐年夏普（吃单）：{by_year(taker)}。"
            f"4 段里为正的段：{taker['positive_quarters']}。"
            f"平均每期持有 {f3(names)} 个合约。"
        ),
        "边际贡献": "不适用：还没有整体模型（G4），这里只估计单个信号。",
        "成本与容量": (
            "按散户吃单费率和估计的价差；没有市场冲击，资金规模按很小算。容量曲线在 G4 做。"
        ),
        "稳健性": (
            "这里只做一次估计，不做参数扰动：每多试一组参数，都算一次试验。"
            "分段、2022 年熊市和最近 365 天已在功效检验里用上。"
        ),
        "适用范围与停止条件": (
            "只是探索集上的估计，最多支撑 E1；它只决定要不要花确认集。"
            "功效检验不通过，就不花确认集，改等前向数据（决策 0026）。"
        ),
    }
    return {k: text[k] for k in SECTIONS}
