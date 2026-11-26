"""Cross-sectional portfolios: signal -> weights -> period returns -> summary (G2).

Pure functions on polars tables: nothing here reads or writes data. Studies get
their tables from the point-in-time interface and pass them in, so this module is
on the research-boundary allow-list (studies may import the functions defined here).

The time rule every function keeps:

- a portfolio is formed at a rebalance time t, from information knowable at t
  (`knowable_time <= t`), and held over (t, t_next];
- the entry price at t is the close of the latest bar knowable at t, and a contract
  is tradable at t only if that bar is at most `max_staleness` old;
- the exit price at t_next is the close of the latest bar knowable at t_next, however
  old: a contract that stopped trading (delisted) exits at its last price, so dead
  contracts lose what they lost instead of dropping out of the sample.

Form portfolios only at rebalance times that have a next one: the last time has no
period left to earn, and a holding without a return is an error, not a zero.

Weights are dollar-neutral with gross exposure 1: +1/2 spread over the long group,
-1/2 over the short group. Costs are charged on turnover at each rebalance, as a
one-way rate per unit of notional traded, including the close-out at a time with too
few names for a new portfolio; only the very last close-out, where the sample ends,
is not charged.

Inputs are checked rather than trusted: a key that appears twice, a signal without a
knowable time and a portfolio that cannot be dollar-neutral are errors; missing, NaN
and infinite values are no signal and no price. Nothing is silently counted twice.

Results are rounded to 10 significant digits, and anything smaller than 1e-12 is 0:
polars may add floats in any order, and the last bits (a sum that should be 0 coming
out as 1e-17) must not change a result's fingerprint.
"""

from __future__ import annotations

import itertools
import math
from datetime import datetime, timedelta

import polars as pl

from sqp.stats import sharpe_standard_error

TIME = pl.Datetime("us", "UTC")
DIGITS = 10
TINY = 1e-12


def rounded(x: float | None) -> float | None:
    """`x` to 10 significant digits, so a result does not depend on summation order; a
    value smaller than 1e-12 is 0 (a sum that should be 0 can come out as 1e-17)."""
    if x is None or math.isnan(x) or math.isinf(x):
        return None
    if abs(x) < TINY:
        return 0.0
    return float(f"{x:.{DIGITS}g}")


def _unique(table: pl.DataFrame, name: str, keys: tuple[str, ...] = ("symbol", "time")) -> None:
    """A key that appears twice would be counted twice by every join after it."""
    if table.height and table.select(keys).is_duplicated().any():
        raise ValueError(f"{name}里有重复的（{'，'.join(keys)}）")


def rebalance_times(start: datetime, end: datetime, every: timedelta) -> list[datetime]:
    """start, start + every, ... up to and including end."""
    if every <= timedelta(0):
        raise ValueError("every 必须是正的时间间隔")
    times, t = [], start
    while t <= end:
        times.append(t)
        t += every
    return times


def _times(times: list[datetime]) -> pl.DataFrame:
    if not times or any(a >= b for a, b in itertools.pairwise(times)):
        raise ValueError("调仓时刻必须非空并且严格递增")
    return pl.DataFrame({"time": pl.Series(times).cast(TIME)})


def prices_at(
    klines: pl.DataFrame, times: list[datetime], max_staleness: timedelta
) -> pl.DataFrame:
    """symbol, time, price, fresh: the close of the latest bar knowable at each time.

    `klines` needs symbol, close and knowable_time. A symbol has rows from its first
    bar on; `fresh` says whether that bar is at most `max_staleness` old at `time`. A
    bar without a positive, finite close is ignored. If two bars of a symbol become
    knowable at the same time, the larger close is used, the same way on every run.
    """
    bars = (
        klines.select("symbol", "close", pl.col("knowable_time").cast(TIME))
        .drop_nulls()
        .filter(pl.col("close").is_finite() & (pl.col("close") > 0))
        .sort("knowable_time", "symbol", "close")  # every column: duplicates resolve alike
    )
    grid = _times(times).join(bars.select("symbol").unique(), how="cross").sort("time")
    # Both sides are sorted by time as a whole, hence within every symbol too; polars
    # cannot verify that when `by` is given, so it is said here instead.
    joined = grid.join_asof(
        bars,
        left_on="time",
        right_on="knowable_time",
        by="symbol",
        strategy="backward",
        check_sortedness=False,
    )
    return (
        joined.drop_nulls("close")
        .with_columns(fresh=(pl.col("time") - pl.col("knowable_time")) <= max_staleness)
        .select("symbol", "time", pl.col("close").alias("price"), "fresh")
        .sort("symbol", "time")
    )


def period_returns(prices: pl.DataFrame) -> pl.DataFrame:
    """symbol, time, ret: from the entry price at `time` (only where fresh) to the exit
    price at the next rebalance time. The last time has no next and gives no row."""
    times = prices.select("time").unique().sort("time")
    following = times.with_columns(next_time=pl.col("time").shift(-1)).drop_nulls()
    entry = prices.filter("fresh").select("symbol", "time", pl.col("price").alias("entry"))
    exit_ = prices.select(
        "symbol", pl.col("time").alias("next_time"), pl.col("price").alias("exit")
    )
    return (
        entry.join(following, on="time")
        .join(exit_, on=["symbol", "next_time"], how="left")
        .with_columns(ret=pl.col("exit") / pl.col("entry") - 1)
        .filter(pl.col("ret").is_finite())
        .select("symbol", "time", "next_time", "ret")
        .sort("symbol", "time")
    )


def quantile_weights(
    signal: pl.DataFrame,
    tradable: pl.DataFrame,
    *,
    groups: int,
    long: str,
    min_names: int,
) -> pl.DataFrame:
    """symbol, time, weight: at each time, sort the tradable symbols by `value`; long the
    lowest group (`long="low"`) or the highest (`long="high"`), short the other end.

    `signal` needs symbol, time, value and knowable_time; a value not yet knowable at
    its time, or without a knowable time, is an error, not something to filter away. A
    missing, NaN or infinite value is no signal. `tradable` is the output of prices_at
    (only fresh rows count). Times with fewer than `min_names` symbols get no portfolio;
    `min_names` must be at least `groups`, or a group could be empty and the portfolio
    one-sided. Ties are broken by symbol name, so the result is deterministic.
    """
    if long not in ("low", "high"):
        raise ValueError('long 只能是 "low" 或 "high"')
    if groups < 2:
        raise ValueError("groups 至少是 2")
    if min_names < groups:
        raise ValueError("min_names 不能小于 groups：否则有的组是空的，组合不再美元中性")
    _unique(signal, "信号")
    _unique(tradable, "可交易表")
    unknown = signal.filter(pl.col("knowable_time").is_null()).height
    if unknown:
        raise ValueError(f"信号缺少可知时间：{unknown} 行（无法排除前视）")
    ahead = signal.filter(pl.col("knowable_time").cast(TIME) > pl.col("time").cast(TIME))
    if ahead.height:
        raise ValueError(f"信号在调仓时刻之后才可知：{ahead.height} 行（前视）")
    fresh = tradable.filter("fresh").select("symbol", "time")
    ranked = (
        signal.select("symbol", pl.col("time").cast(TIME), "value")
        .filter(pl.col("value").is_finite())  # drops missing values, NaN and infinities
        .join(fresh, on=["symbol", "time"])
        .sort("time", "value", "symbol")
        .with_columns(
            n=pl.len().over("time"),
            rank=pl.int_range(pl.len()).over("time"),
        )
        .filter(pl.col("n") >= min_names)
        .with_columns(group=(pl.col("rank") * groups // pl.col("n")) + 1)
    )
    low, high = 1, groups
    long_group, short_group = (low, high) if long == "low" else (high, low)
    legs = ranked.filter(pl.col("group").is_in([long_group, short_group])).with_columns(
        side=pl.when(pl.col("group") == long_group).then(1).otherwise(-1)
    )
    return (
        legs.with_columns(size=pl.len().over("time", "side"))
        .with_columns(weight=pl.col("side") * 0.5 / pl.col("size"))
        .select("symbol", "time", "weight")
        .sort("time", "symbol")
    )


def portfolio_returns(
    weights: pl.DataFrame,
    returns: pl.DataFrame,
    *,
    carry: pl.DataFrame | None = None,
    cost_rate: float | pl.DataFrame = 0.0,
) -> pl.DataFrame:
    """time, gross, carry, cost, net, turnover, longs, shorts: one row per rebalance time
    with a portfolio, for the period that starts then.

    gross: sum of weight x price return. carry: sum of weight x `carry` (the return a
    long position earns over the period on top of the price, e.g. minus the funding it
    pays; symbol, time, carry). cost: turnover x one-way cost rate, either a number or
    a table (symbol, time, cost_rate). Turnover is measured against the previous
    weights drifted by their returns; funding is cash, so it changes the account's
    value but not the positions.

    Trades happen at every rebalance time before the last one. Where too few names (or
    none, as in an exchange-wide outage) left no new portfolio, the old one is only
    closed, and that cost belongs to the period that ends there. The last time ends the
    sample: positions are marked there, not closed.
    """
    _unique(weights, "权重表")
    _unique(returns, "收益表")
    if carry is not None:
        _unique(carry, "资金费表")
    if isinstance(cost_rate, pl.DataFrame):
        _unique(cost_rate, "成本表")
    held = weights.join(returns, on=["symbol", "time"], how="left")
    missing = held.filter(pl.col("ret").is_null())
    if missing.height:
        raise ValueError(
            f"有 {missing.height} 个持仓没有这一期的收益：先用 prices_at 过滤可交易的合约"
        )
    if carry is not None:
        held = held.join(carry.select("symbol", "time", "carry"), on=["symbol", "time"], how="left")
    else:
        held = held.with_columns(carry=pl.lit(0.0))
    held = held.with_columns(pl.col("carry").fill_null(0.0))
    per_time = (
        held.sort("time", "symbol")  # a fixed order inside every sum
        .group_by("time")
        .agg(
            gross=(pl.col("weight") * pl.col("ret")).sum(),
            carry=(pl.col("weight") * pl.col("carry")).sum(),
            longs=(pl.col("weight") > 0).sum(),
            shorts=(pl.col("weight") < 0).sum(),
            next_time=pl.col("next_time").first(),
        )
        .sort("time")
    )
    # Weights just before each rebalance: last period's weights drifted by their returns.
    wiped = per_time.filter(1 + pl.col("gross") + pl.col("carry") <= 0).height
    if wiped:
        raise ValueError(f"有 {wiped} 期把账户亏光了（1 + 收益 ≤ 0），之后的权重无从算起")
    drifted = (
        held.join(per_time.select("time", "gross", pl.col("carry").alias("cash")), on="time")
        # Positions drift with prices; funding is cash: it moves the account's value, by
        # which every weight is divided, and leaves the positions alone.
        .with_columns(
            before=pl.col("weight") * (1 + pl.col("ret")) / (1 + pl.col("gross") + pl.col("cash"))
        )
        .select("symbol", pl.col("next_time").alias("time"), "before")
    )
    trades = (
        weights.select("symbol", "time", "weight")
        .join(drifted, on=["symbol", "time"], how="full", coalesce=True)
        .with_columns(pl.col("weight").fill_null(0.0), pl.col("before").fill_null(0.0))
        # Every time before the last: an outage (no fresh price anywhere) is not the end.
        .filter(pl.col("time") < returns["next_time"].max())
        .with_columns(traded=(pl.col("weight") - pl.col("before")).abs())
        .sort("time", "symbol")
    )
    if isinstance(cost_rate, pl.DataFrame):
        trades = trades.join(
            cost_rate.select("symbol", "time", "cost_rate"), on=["symbol", "time"], how="left"
        )
        if trades.filter(pl.col("cost_rate").is_null() & (pl.col("traded") > 0)).height:
            raise ValueError("成本表缺少某些成交的费率")
    else:
        trades = trades.with_columns(cost_rate=pl.lit(float(cost_rate)))
    # Each trade belongs to the portfolio formed at its time, or, where none was formed,
    # to the period that ends there.
    opened = per_time.select("time", period=pl.col("time"))
    closed = per_time.select(pl.col("next_time").alias("time"), period=pl.col("time")).join(
        opened, on="time", how="anti"
    )
    costs = (
        trades.join(pl.concat([opened, closed]), on="time")
        .sort("period", "symbol", "time")
        .group_by("period")
        .agg(
            turnover=pl.col("traded").sum(),
            cost=(pl.col("traded") * pl.col("cost_rate").fill_null(0.0)).sum(),
        )
        .rename({"period": "time"})
    )
    return (
        per_time.join(costs, on="time", how="left")
        .with_columns(pl.col("turnover").fill_null(0.0), pl.col("cost").fill_null(0.0))
        .with_columns(net=pl.col("gross") + pl.col("carry") - pl.col("cost"))
        .select("time", "gross", "carry", "cost", "net", "turnover", "longs", "shorts")
        .sort("time")
    )


def _moments(values: list[float]) -> tuple[float, float, float, float]:
    n = len(values)
    mean = sum(values) / n
    centred = [v - mean for v in values]
    var = sum(c * c for c in centred) / (n - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return mean, 0.0, 0.0, 3.0
    m2 = sum(c * c for c in centred) / n
    skew = sum(c**3 for c in centred) / n / m2**1.5
    kurt = sum(c**4 for c in centred) / n / m2**2
    return mean, sd, skew, kurt


def long_run_ratio(values: list[float], lags: int) -> float:
    """How much autocorrelation inflates the variance of a mean: the Newey-West long-run
    variance (Bartlett weights over `lags` lags) over the plain variance. Never below 1:
    negative autocorrelation is not allowed to make a result look better. Divide a
    Sharpe ratio or t-statistic by its square root (Lo 2002)."""
    n = len(values)
    mean = sum(values) / n
    centred = [v - mean for v in values]
    plain = sum(c * c for c in centred) / n
    if plain == 0:
        return 1.0
    lags = max(0, min(lags, n - 1))
    total = plain
    for k in range(1, lags + 1):
        cov = sum(centred[i] * centred[i - k] for i in range(k, n)) / n
        total += 2 * (1 - k / (lags + 1)) * cov
    return max(1.0, total / plain)


def summarize(periods: pl.DataFrame, *, column: str = "net", periods_per_year: float) -> dict:
    """Annualised mean, volatility and Sharpe; t-statistic with a fat-tail standard
    error; the Sharpe ratios and t-statistic again with an allowance for autocorrelation
    over at least a week (`_hac`: divided by the square root of long_run_ratio; the
    last-365-days value reuses the full sample's factor, as a year is too short to
    estimate it well); max drawdown; Sharpe by
    calendar year and over the last 365 days; how many of 4 equal sub-periods had a
    positive mean; turnover and cost per year."""
    rows = periods.sort("time")
    values = rows[column].to_list()
    n = len(values)
    if n < 4:
        raise ValueError(f"只有 {n} 期，至少要 4 期")
    mean, sd, skew, kurt = _moments(values)
    sr = mean / sd if sd else 0.0
    t_stat = sr / sharpe_standard_error(sr, n, skew, kurt) if sd else 0.0
    wealth, peak, drawdown = 1.0, 1.0, 0.0
    for v in values:
        wealth *= 1 + v
        peak = max(peak, wealth)
        drawdown = min(drawdown, wealth / peak - 1)
    times = rows["time"].to_list()
    by_year: dict[str, float | None] = {}
    for year in sorted({t.year for t in times}):
        chunk = [v for t, v in zip(times, values, strict=True) if t.year == year]
        by_year[str(year)] = _annual_sharpe(chunk, periods_per_year)
    recent = [v for t, v in zip(times, values, strict=True) if t > times[-1] - timedelta(days=365)]
    quarter = n // 4
    pieces = [values[i * quarter : (i + 1) * quarter if i < 3 else n] for i in range(4)]
    # The usual rule, floor(4 (n/100)^(2/9)) lags, but never less than a week: funding
    # and liquidity regimes last days, and too short a window under-corrects.
    lags = max(int(4 * (n / 100) ** (2 / 9)), round(7 * periods_per_year / 365))
    inflation = math.sqrt(long_run_ratio(values, lags))
    recent_sharpe = _annual_sharpe(recent, periods_per_year)
    return {
        "periods": n,
        "mean_annual": rounded(mean * periods_per_year),
        "vol_annual": rounded(sd * math.sqrt(periods_per_year)),
        "sharpe_annual": rounded(sr * math.sqrt(periods_per_year)),
        "t_stat": rounded(t_stat),
        "skew": rounded(skew),
        "kurtosis": rounded(kurt),
        "max_drawdown": rounded(drawdown),
        "sharpe_by_year": {k: rounded(v) for k, v in by_year.items()},
        "sharpe_last_365_days": rounded(recent_sharpe),
        "autocorrelation_factor": rounded(inflation**2),
        "sharpe_annual_hac": rounded(sr * math.sqrt(periods_per_year) / inflation),
        "t_stat_hac": rounded(t_stat / inflation),
        "sharpe_last_365_days_hac": (
            None if recent_sharpe is None else rounded(recent_sharpe / inflation)
        ),
        "positive_quarters": sum(sum(p) > 0 for p in pieces),
        "turnover_annual": rounded(_mean(rows, "turnover") * periods_per_year),
        "cost_annual": rounded(_mean(rows, "cost") * periods_per_year),
    }


def _mean(rows: pl.DataFrame, column: str) -> float:
    return float(rows[column].mean()) if column in rows.columns and rows.height else 0.0


def _annual_sharpe(values: list[float], periods_per_year: float) -> float | None:
    if len(values) < 2:
        return None
    mean, sd, _, _ = _moments(values)
    return mean / sd * math.sqrt(periods_per_year) if sd else None
