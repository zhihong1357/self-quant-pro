"""H-0006, the positive control (decision 0026): can the engine find a signal we planted?

Real daily returns of the most liquid U1 perpetuals at the end of the exploration
segment are the noise. Into them a signal of known strength is planted:

    1 + r'[i, t] = (1 + r[i, t]) * (1 + k * sd[i] * s[i, t])

where r[i, t] is the return of contract i from day t to day t + 1, sd[i] its sample
standard deviation over the window, and s[i, t] standard normal, independent across
contracts and days and of the returns, drawn from a fixed seed. s[i, t] is "known" at
day t, so a portfolio formed at t that buys high s and sells low s earns k times a
known amount, sum of w * sd * s * (1 + r), on top of noise. Planting by multiplying
keeps every price positive even on a -99 % day (LUNA, May 2022), where adding could
have pushed it below zero, and keeps the result exactly linear in k. The portfolio
goes through the same engine that candidates A and B use (sqp.research.engine), which
is the point: the control tests the code the real estimates will rely on.

Four arms share the same s:

- strong (k = 0.08): must be found: t >= 3, estimated k within [0.04, 0.12], and a
  positive mean in at least 3 of 4 equal sub-periods;
- half (k = 0.04): reported only;
- zero (k = 0): must not be found in either direction (|t| < 3);
- canary (k = 0.08, planted into the return of the day that just ended, r[i, t - 1]):
  a correct engine trades at t and earns r[i, t], so it must not find it (|t| < 3). An
  engine that used the return that made the signal would show a large profit.

The signal is planted into the daily prices the engine reads, never into the engine's
own output: planted into the engine's return table, it would move with any mistake the
engine makes in pairing weights with days, and the canary could not see it (the
first version of this study did exactly that; its own test caught it).

On top of that, the engine is checked for consistency: every day's portfolio must be
dollar-neutral with gross exposure 1, and every arm's portfolio return must equal the
zero arm's plus k times the planted part computed here from the same weights. The
planted part counts only the days where a signal was planted, those with a next day's
close: a contract held into the day it stops trading (delisted), or across a missing
00:00 bar, exits at its last price with nothing planted, and counting it anyway would
call a correct engine inconsistent (a rehearsal and Gemini's review both caught that).

Costs, funding and hourly prices are left out: they would mix assumptions into a test
of the statistics. The engine's unit tests and the candidates' tests cover them. The
verdict ("通过" or "不通过") is the only thing that reaches the log.
"""

from __future__ import annotations

import random
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

DATASET = "um-klines-1h"
DAY = timedelta(days=1)


def day(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def liquid_names(
    klines: pl.DataFrame, start: datetime, days: int, names: int, min_days: int
) -> list[str]:
    """The `names` contracts with the highest median daily quote volume over the `days`
    days before `start`, counting only contracts with at least `min_days` such days."""
    before = klines.filter(
        (pl.col("open_time") >= start - days * DAY) & (pl.col("open_time") < start)
    )
    daily = before.group_by("symbol", pl.col("open_time").dt.truncate("1d").alias("day")).agg(
        pl.col("quote_volume").sum()
    )
    ranked = (
        daily.group_by("symbol")
        .agg(pl.col("quote_volume").median().alias("median"), pl.len().alias("days"))
        .filter(pl.col("days") >= min_days)
        .sort(["median", "symbol"], descending=[True, False])
    )
    return ranked["symbol"].head(names).to_list()


def planted(symbols: list[str], times: list[datetime], seed: int) -> pl.DataFrame:
    """symbol, time, s: standard normal draws in a fixed order, so the same seed always
    gives the same signal whatever data is missing."""
    rng = random.Random(seed)
    rows = [(symbol, t, rng.gauss(0.0, 1.0)) for t in times for symbol in sorted(symbols)]
    return pl.DataFrame(rows, schema=["symbol", "time", "s"], orient="row").with_columns(
        pl.col("time").cast(pl.Datetime("us", "UTC"))
    )


def daily_closes(bars: pl.DataFrame, times: list[datetime]) -> pl.DataFrame:
    """symbol, time, close, ret: the close knowable exactly at each day's 00:00 and the
    return to the next day's. Computed here, not by the engine, so that the signal is
    planted into the data the engine reads and not into the engine's own output."""
    days = pl.DataFrame({"time": pl.Series(times).cast(pl.Datetime("us", "UTC"))})
    closes = (
        bars.select("symbol", pl.col("knowable_time").cast(pl.Datetime("us", "UTC")), "close")
        .join(days, left_on="knowable_time", right_on="time")
        .rename({"knowable_time": "time"})
        .sort("symbol", "time")
    )
    following = pl.col("close").shift(-1).over("symbol")
    next_day = pl.col("time").shift(-1).over("symbol") == pl.col("time") + DAY
    return closes.with_columns(ret=pl.when(next_day).then(following / pl.col("close") - 1))


def plant(
    closes: pl.DataFrame, s: pl.DataFrame, sd: pl.DataFrame, k: float, lag: int
) -> pl.DataFrame:
    """A daily price path whose growth from day t to t + 1 is (1 + r) (1 + k sd s[t + lag]):
    lag 0 plants the signal known at t into the return that follows it; lag 1 plants it
    into the return that ends when it becomes known (the canary). A gap in the data
    starts the path again from the real price."""
    shifted = s.with_columns(pl.col("time") - lag * DAY)
    growth = (
        closes.join(sd, on="symbol")
        .join(shifted, on=["symbol", "time"], how="left")
        .with_columns(pl.col("s").fill_null(0.0))
        .with_columns(g=(1 + pl.col("ret")) * (1 + k * pl.col("sd") * pl.col("s")))
        .sort("symbol", "time")
        .with_columns(
            run=pl.col("ret").is_null().shift(1, fill_value=False).cum_sum().over("symbol")
        )
    )
    # Within a run of consecutive days: price = first price x product of earlier growth.
    path = growth.with_columns(
        before=pl.col("g").shift(1).over("symbol", "run").fill_null(1.0),
    ).with_columns(
        close=pl.col("close").first().over("symbol", "run")
        * pl.col("before").cum_prod().over("symbol", "run")
    )
    return path.select("symbol", pl.col("time").alias("knowable_time"), "close")


def evaluate(klines: pl.DataFrame, params: dict) -> Outcome:
    """Everything after reading the data; tests call this with synthetic klines."""
    start, end = day(params["window_start"]), day(params["window_end"])
    names = liquid_names(
        klines, start, int(params["liquidity_days"]), int(params["names"]), int(params["min_days"])
    )
    if len(names) < int(params["min_names"]):
        raise ValueError(f"只找到 {len(names)} 个流动性够的合约，少于 {params['min_names']}")
    times = rebalance_times(start, end, DAY)
    closes = daily_closes(klines.filter(pl.col("symbol").is_in(names)), times)
    # A contract with fewer than two returns in the window gets no signal (sd 0).
    sd = closes.group_by("symbol").agg(pl.col("ret").std().fill_null(0.0).alias("sd"))
    s = planted(names, times, int(params["seed"]))
    arms = {name: (float(k), 0) for name, k in params["strengths"].items()}
    if set(arms) != {"strong", "half", "zero"}:
        raise ValueError("strengths 必须正好有 strong、half、zero 三组")
    arms["canary"] = (float(params["canary_strength"]), 1)
    # The portfolio is the same in every arm: formed from s at each day that has a next.
    real = closes.select("symbol", pl.col("time").alias("knowable_time"), "close")
    tradable = prices_at(real, times, max_staleness=timedelta(hours=1))
    signal = s.filter(pl.col("time") < times[-1]).select(
        "symbol", "time", pl.col("s").alias("value"), pl.col("time").alias("knowable_time")
    )
    weights = quantile_weights(
        signal,
        tradable,
        groups=int(params["groups"]),
        long="high",
        min_names=int(params["min_names"]),
    )
    # What each portfolio earns per unit of k when the engine pairs weights and returns
    # correctly: the sum of w * sd * s * (1 + r) over the day it is held, counting only
    # the contracts into whose next day a signal was planted.
    planted_days = closes.filter(pl.col("ret").is_not_null()).select("symbol", "time", "ret")
    part = (
        weights.join(planted_days, on=["symbol", "time"])
        .join(s, on=["symbol", "time"])
        .join(sd, on="symbol")
        .sort("time", "symbol")
        .group_by("time")
        .agg((pl.col("weight") * pl.col("sd") * pl.col("s") * (1 + pl.col("ret"))).sum())
        .rename({"weight": "part"})
    )
    # Every day's portfolio: dollar-neutral, gross exposure 1.
    books = weights.group_by("time").agg(
        net=pl.col("weight").sum(), gross=pl.col("weight").abs().sum()
    )
    neutral = books.height > 0 and bool(
        ((books["net"].abs() < 1e-12) & ((books["gross"] - 1).abs() < 1e-12)).all()
    )
    results, nets = {}, {}
    for name, (k, lag) in arms.items():
        prices = prices_at(plant(closes, s, sd, k, lag), times, max_staleness=timedelta(hours=1))
        periods = portfolio_returns(weights, period_returns(prices))
        joined = periods.join(part, on="time", how="left").with_columns(
            pl.col("part").fill_null(0.0)
        )
        mean_part = float(joined["part"].mean())
        estimate = float(joined["net"].mean()) / mean_part if mean_part else None
        nets[name] = periods.select("time", pl.col("net").alias(name))
        results[name] = {
            "strength": k,
            "planted_into": "下一天" if lag == 0 else "已经发生的那一天",
            "estimated_strength": rounded(estimate),
            **summarize(periods, periods_per_year=365),
        }
    # Linearity: a planted arm must be the zero arm plus k times the planted part
    # computed above from the same weights. An engine that pairs weights with the wrong
    # day breaks it, even where the t-values happen to look fine.
    consistent = True
    for name in ("strong", "half"):
        gap = (
            nets[name]
            .join(nets["zero"], on="time")
            .join(part, on="time", how="left")
            .with_columns(pl.col("part").fill_null(0.0))
            .select((pl.col(name) - pl.col("zero") - arms[name][0] * pl.col("part")).abs().max())
            .item()
        )
        consistent = consistent and gap is not None and gap < 1e-9
    verdict, checks = judge(results, params, consistent, neutral)
    metrics = {
        "names": len(names),
        "days": len(times),
        "arms": results,
        "checks": checks,
        "engine_consistent": consistent,
        "dollar_neutral_gross_one": neutral,
    }
    return Outcome(metrics=metrics, sections=sections(metrics, params, names), verdict=verdict)


def judge(results: dict, params: dict, consistent: bool, neutral: bool) -> tuple[str, dict]:
    strong, zero, canary = results["strong"], results["zero"], results["canary"]
    bar = float(params["pass_t"])
    low, high = float(params["strength_low"]), float(params["strength_high"])
    estimate = strong["estimated_strength"]
    checks = {
        "strong_found": strong["t_stat"] is not None and strong["t_stat"] >= bar,
        "strong_estimate_in_range": estimate is not None and low <= estimate <= high,
        "strong_quarters": strong["positive_quarters"] >= int(params["quarters_positive"]),
        "zero_not_found": zero["t_stat"] is None or abs(zero["t_stat"]) < bar,
        "canary_not_found": canary["t_stat"] is None or abs(canary["t_stat"]) < bar,
        "engine_consistent": consistent,
        "dollar_neutral_gross_one": neutral,
    }
    return ("通过" if all(checks.values()) else "不通过"), checks


def run(pit: PointInTime, params: dict) -> Outcome:
    start, end = day(params["window_start"]), day(params["window_end"])
    first = start - (int(params["liquidity_days"]) + 1) * DAY
    # The last close needed is the one knowable at `end` (00:00); nothing later is read,
    # so the window stays inside exploration.
    klines = pit.table(
        DATASET,
        start=first,
        end=end,
        columns=["symbol", "open_time", "close", "quote_volume"],
    )
    return evaluate(klines, params)


def f3(x: float | None) -> str:
    return "无" if x is None else f"{x:.3f}"


def sections(m: dict, params: dict, names: list[str]) -> dict:
    arms, checks = m["arms"], m["checks"]
    strong = arms["strong"]
    ok = "通过" if all(checks.values()) else "不通过"
    failed = [name for name, passed in checks.items() if not passed]
    rows = "\n".join(
        f"| {name} | {a['strength']} | {f3(a['estimated_strength'])} | {f3(a['t_stat'])} | "
        f"{f3(a['sharpe_annual'])} | {a['positive_quarters']} / 4 |"
        for name, a in arms.items()
    )
    text = {
        "结论": (
            f"阳性对照{ok}。强组（强度 {strong['strength']}）的 t 值为 {f3(strong['t_stat'])}，"
            f"估出的强度 {f3(strong['estimated_strength'])}；"
            f"零组 t 值 {f3(arms['zero']['t_stat'])}；"
            f"前视金丝雀 t 值 {f3(arms['canary']['t_stat'])}。"
            + (f"没通过的检查：{'、'.join(failed)}。" if failed else "全部检查通过。")
        ),
        "假说与机制": (
            "见 registry/hypotheses/H-0006.toml：引擎必须检出植入的已知信号，"
            "检不出零信号和前视金丝雀。"
        ),
        "数据": (
            "Binance U 本位永续 1 小时 K 线，经时点接口只读探索集。"
            f"窗口 {params['window_start']} 至 {params['window_end']}，"
            f"{m['days']} 个调仓日，每天 00:00（UTC）调仓。"
            f"合约是窗口开始前 {params['liquidity_days']} 天"
            f"日成交额中位数最高的 {m['names']} 个："
            f"{'、'.join(names)}。"
        ),
        "方法": (
            f"植入：1 + r' = (1 + r)(1 + k·sd·s)，s 由种子 {params['seed']} 生成"
            "（乘法植入，暴跌日价格也不会变成负数）。"
            f"每天按 s 分 {params['groups']} 组，做多最高组、做空最低组（美元中性），"
            "走 sqp.research.engine；不扣成本。"
            "估出的强度 = 组合平均收益 ÷ 组合在植入部分上的平均暴露；"
            "持有到下架或遇到缺失的 00:00 K 线的那一天没有植入，不计入植入部分。"
            "t 值按偏度和峰度修正标准误，没有做自相关修正（s 在时间上独立）。"
        ),
        "结果": (
            "| 组 | 植入强度 | 估出的强度 | t 值 | 年化夏普 | 为正的段 |\n"
            "|---|---|---|---|---|---|\n"
            f"{rows}\n\n"
            "引擎一致性（每组 = 零组 + k × 植入部分）："
            f"{'是' if m['engine_consistent'] else '否'}；"
            "每天美元中性、总敞口 1 倍："
            f"{'是' if m['dollar_neutral_gross_one'] else '否'}。"
        ),
        "边际贡献": "不适用：元研究，研究对象是流程本身。",
        "成本与容量": "不适用：植入的信号以波动率为单位，没有成本模型。",
        "稳健性": "半组（强度 0.04）只报告，不参与判定。",
        "适用范围与停止条件": (
            "只说明引擎在这类日度、横截面、美元中性的组合上能检出已知信号，"
            "并且不会把前一期的收益当成可以赚的收益；不说明任何真实信号存在。"
            "不覆盖资金费、成本扣减和小时级 K 线的对齐：这些由引擎的单元测试"
            "（tests/test_engine.py）和候选 A、B 的测试覆盖。"
            "判定为不通过时，先修引擎，候选 A、B 不登记。"
        ),
    }
    return {name: text[name] for name in SECTIONS}
