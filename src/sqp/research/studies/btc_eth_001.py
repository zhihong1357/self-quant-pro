"""H-0010 / G2 migration acceptance A: blind Binance replication of Gen 2 btc-eth-001.

Replicates the preregistered methodology of `myquant@e992b10` (`research/btc-eth-001/run_trial.py`
and `H0001/v1.json`) on Binance USDT-M 1-hour perpetual klines (`um-klines-1h`) through the
four-lock blind replication channel (decisions 0031 and 0032):

1. Daily 16:00 UTC bars aggregated from 24 complete hourly bars (`2023-01-01T16:00Z` to
   `2026-11-18T00:00Z`; the partial final day `2026-11-17T16:00..23:00Z` is dropped).
2. Hourly panel over `2026-08-20T00:00Z` to `2026-11-18T00:00Z`.
3. Per-instrument adjacent log returns computed *before* inner-joining BTCUSDT, ETHUSDT and
   SOLUSDT (no forward filling; returns spanning a missing bar are invalidated).
4. Three-test partial lead-lag family with Bartlett-kernel Newey-West HAC and Holm-Bonferroni
   FWER adjustment:
   - `daily_btc_eth`: lag-1 BTC -> ETH controlling for ETH lag-1 and SOL lag-1 (max_lag=5);
   - `daily_btc_sol`: lag-1 BTC -> SOL controlling for SOL lag-1 and ETH lag-1 (max_lag=5);
   - `hourly_btc_eth`: lag-1 BTC -> ETH controlling for ETH lag-1 and SOL lag-1 (max_lag=12).
5. Purged walk-forward OOS evaluation (`initial_train=365`, `test_size=90`, `embargo=1d`,
   `minimum_train=30`, `label_end = decision_time + 2 days`), scored on the preregistered
   holdout window (`decision_time >= 2026-01-02T16:00Z`) with training folds unchanged.
6. Taker-cost gate on holdout OOS augmented forecasts (sign rule, 0.05% entry/final exit,
   0.10% direction flip) with two-sided circular block bootstrap (`block_length=10`,
   `resamples=2000`, `seed=42`).

All methodology constants are hardcoded here (not read from `params`), and raw third-party
estimates are handed solely to the framework comparator in memory via `BlindMetrics`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import polars as pl

from sqp.data.pit import PointInTime
from sqp.research.studies import BlindMetrics
from sqp.stats import (
    OLSResult,
    WalkForwardFold,
    circular_block_bootstrap_mean_test,
    evaluate_walk_forward_forecasts,
    holm_adjust,
    newey_west_hac,
    ols_fit,
    partial_lead_lag,
    purged_walk_forward_splits,
)

DATASET = "um-klines-1h"
BTC = "BTCUSDT"
ETH = "ETHUSDT"
SOL = "SOLUSDT"
SYMBOLS = (BTC, ETH, SOL)

DAILY_START = datetime(2023, 1, 1, 16, 0, tzinfo=UTC)
HOURLY_START = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
CUTOFF = datetime(2026, 11, 18, 0, 0, tzinfo=UTC)
HOLDOUT_START = datetime(2026, 1, 2, 16, 0, tzinfo=UTC)

DAILY_BAR_SECONDS = 86_400
HOURLY_BAR_SECONDS = 3_600

DAILY_MAX_HAC_LAG = 5
HOURLY_MAX_HAC_LAG = 12
MIN_OBSERVATIONS = 30

WF_INITIAL_TRAIN = 365
WF_TEST_SIZE = 90
WF_EMBARGO_DAYS = 1
WF_MIN_TRAIN = 30

TAKER_FEE_PER_SIDE = 0.0005
ROUND_TRIP_COST = 0.001
BOOTSTRAP_BLOCK_LENGTH = 10
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 42

MIN_EFFECT_SIZE = 0.005
ALPHA = 0.05


@dataclass(frozen=True)
class _ReturnPanel:
    times: tuple[datetime, ...]
    btc: tuple[float, ...]
    eth: tuple[float, ...]
    sol: tuple[float, ...]
    dropped_close_rows: int
    dropped_return_rows: int
    dropped_lag_pairs: int
    adjacent_indices: tuple[int, ...]


@dataclass(frozen=True)
class _TestOutcome:
    coefficient: float
    observations: int
    p_value: float
    partial_r_squared: float | None


def _to_utc_datetime(raw: datetime) -> datetime:
    utc = raw.astimezone(UTC)
    return datetime(
        utc.year,
        utc.month,
        utc.day,
        utc.hour,
        utc.minute,
        utc.second,
        tzinfo=UTC,
    )


def _extract_symbol_series(
    frame: pl.DataFrame, time_col: str
) -> dict[str, list[tuple[datetime, float]]]:
    series: dict[str, list[tuple[datetime, float]]] = {sym: [] for sym in SYMBOLS}
    ordered = frame.sort(["symbol", time_col])
    for sym, ts, close in ordered.select(["symbol", time_col, "close"]).iter_rows():
        series[str(sym)].append((_to_utc_datetime(ts), float(close)))
    return series


def _build_synchronized_panel(
    close_series: dict[str, list[tuple[datetime, float]]],
    *,
    bar_seconds: int,
) -> _ReturnPanel:
    close_maps: dict[str, dict[datetime, float]] = {}
    ret_maps: dict[str, dict[datetime, float]] = {}
    all_times_set: set[datetime] = set()

    for sym in SYMBOLS:
        pairs = close_series[sym]
        if not pairs:
            raise ValueError(f"缺少品种 {sym} 的有效 K 线数据")
        c_map: dict[datetime, float] = {}
        r_map: dict[datetime, float] = {}
        prev_t: datetime | None = None
        prev_c: float | None = None
        for t, c in pairs:
            c_map[t] = c
            all_times_set.add(t)
            if (
                prev_t is not None
                and prev_c is not None
                and (t - prev_t).total_seconds() == bar_seconds
            ):
                ret = math.log(c / prev_c)
                if math.isfinite(ret):
                    r_map[t] = ret
            prev_t = t
            prev_c = c
        close_maps[sym] = c_map
        ret_maps[sym] = r_map

    all_times = sorted(all_times_set)
    dropped_close_rows = sum(
        1 for t in all_times if any(t not in close_maps[sym] for sym in SYMBOLS)
    )
    complete_times = tuple(t for t in all_times if all(t in ret_maps[sym] for sym in SYMBOLS))
    if len(complete_times) < MIN_OBSERVATIONS + 1:
        raise ValueError("同步收益面板有效行数不足")
    dropped_return_rows = len(all_times) - len(complete_times)

    btc = tuple(ret_maps[BTC][t] for t in complete_times)
    eth = tuple(ret_maps[ETH][t] for t in complete_times)
    sol = tuple(ret_maps[SOL][t] for t in complete_times)

    adj_indices = tuple(
        i
        for i in range(len(complete_times) - 1)
        if (complete_times[i + 1] - complete_times[i]).total_seconds() == bar_seconds
    )
    if len(adj_indices) < MIN_OBSERVATIONS:
        raise ValueError("相邻滞后一期样本量不足")
    dropped_lag_pairs = (len(complete_times) - 1) - len(adj_indices)

    return _ReturnPanel(
        times=complete_times,
        btc=btc,
        eth=eth,
        sol=sol,
        dropped_close_rows=int(dropped_close_rows),
        dropped_return_rows=int(dropped_return_rows),
        dropped_lag_pairs=int(dropped_lag_pairs),
        adjacent_indices=adj_indices,
    )


def _aggregate_daily_1600(hourly_clean: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    shift = pl.duration(hours=16)
    with_day = hourly_clean.with_columns(
        ((pl.col("open_time") - shift).dt.truncate("1d") + shift).alias("day_open")
    )
    grouped = with_day.group_by(["symbol", "day_open"]).agg(
        pl.len().alias("bar_count"),
        pl.col("open_time").max().alias("last_open"),
        pl.col("close").sort_by("open_time").last().alias("close"),
    )
    is_complete = (pl.col("bar_count") == 24) & (
        pl.col("last_open") == pl.col("day_open") + pl.duration(hours=23)
    )
    incomplete_days = grouped.filter(~is_complete).select("day_open").n_unique()
    daily = grouped.filter(is_complete).select(["symbol", "day_open", "close"])
    return daily, int(incomplete_days)


def _partial_test(
    source: tuple[float, ...],
    target: tuple[float, ...],
    factor_name: str,
    factor: tuple[float, ...],
    *,
    panel: _ReturnPanel,
    max_hac_lag: int,
) -> _TestOutcome:
    if panel.dropped_lag_pairs == 0:
        res = partial_lead_lag(
            source,
            target,
            lag_steps=1,
            common_factors={factor_name: factor},
            max_hac_lag=max_hac_lag,
            minimum_observations=MIN_OBSERVATIONS,
        )
        eff = res.source_effect
        if eff.p_value is None:
            raise ValueError("HAC p 值不可用")
        return _TestOutcome(
            coefficient=float(eff.estimate),
            observations=int(res.observations),
            p_value=float(eff.p_value),
            partial_r_squared=(
                None if res.partial_r_squared is None else float(res.partial_r_squared)
            ),
        )

    idx = panel.adjacent_indices
    response = [target[i + 1] for i in idx]
    base_predictors = {
        "target_lag": [target[i] for i in idx],
        factor_name: [factor[i] for i in idx],
    }
    augmented_predictors = {
        "target_lag": base_predictors["target_lag"],
        factor_name: base_predictors[factor_name],
        "source": [source[i] for i in idx],
    }
    base_fit = ols_fit(response, base_predictors)
    aug_fit = ols_fit(response, augmented_predictors)
    hac = newey_west_hac(response, augmented_predictors, max_lag=max_hac_lag)
    eff = next(c for c in hac.coefficients if c.name == "source")
    if eff.p_value is None:
        raise ValueError("HAC p 值不可用")
    base_ssr = base_fit.sum_squared_residuals
    partial_r2: float | None = None
    if base_ssr > 0.0:
        partial_r2 = max(0.0, min(1.0, (base_ssr - aug_fit.sum_squared_residuals) / base_ssr))
    return _TestOutcome(
        coefficient=float(eff.estimate),
        observations=len(response),
        p_value=float(eff.p_value),
        partial_r_squared=None if partial_r2 is None else float(partial_r2),
    )


def _ols_predict_rows(
    fit: OLSResult,
    predictors: dict[str, list[float]],
    indices: tuple[int, ...],
) -> list[float]:
    intercept = 0.0 if fit.intercept is None else float(fit.intercept)
    preds: list[float] = []
    for row_idx in indices:
        total = intercept
        for offset, name in enumerate(fit.predictor_names):
            total += fit.coefficients[offset + 1] * predictors[name][row_idx]
        preds.append(total)
    return preds


def _run_walk_forward_holdout(
    daily: _ReturnPanel,
) -> tuple[float | None, list[float], list[float]]:
    idx = daily.adjacent_indices
    target = [daily.eth[i + 1] for i in idx]
    predictors = {
        "target_lag": [daily.eth[i] for i in idx],
        "sol": [daily.sol[i] for i in idx],
        "btc": [daily.btc[i] for i in idx],
    }
    decision_times = [daily.times[i] for i in idx]
    label_ends = [t + timedelta(days=2) for t in decision_times]
    folds = purged_walk_forward_splits(
        decision_times,
        label_ends,
        initial_train_size=WF_INITIAL_TRAIN,
        test_size=WF_TEST_SIZE,
        embargo=timedelta(days=WF_EMBARGO_DAYS),
        minimum_train_size=WF_MIN_TRAIN,
    )

    n = len(target)
    base_forecasts = [math.nan] * n
    aug_forecasts = [math.nan] * n
    for fold in folds:
        tr = fold.train_indices
        te = fold.test_indices
        y_train = [target[k] for k in tr]
        base_fit = ols_fit(
            y_train,
            {
                "target_lag": [predictors["target_lag"][k] for k in tr],
                "sol": [predictors["sol"][k] for k in tr],
            },
        )
        aug_fit = ols_fit(
            y_train,
            {
                "target_lag": [predictors["target_lag"][k] for k in tr],
                "sol": [predictors["sol"][k] for k in tr],
                "btc": [predictors["btc"][k] for k in tr],
            },
        )
        b_preds = _ols_predict_rows(base_fit, predictors, te)
        a_preds = _ols_predict_rows(aug_fit, predictors, te)
        for pos, row_idx in enumerate(te):
            base_forecasts[row_idx] = b_preds[pos]
            aug_forecasts[row_idx] = a_preds[pos]

    holdout_folds: list[WalkForwardFold] = []
    for fold in folds:
        restricted = tuple(k for k in fold.test_indices if decision_times[k] >= HOLDOUT_START)
        if not restricted:
            continue
        holdout_folds.append(
            WalkForwardFold(
                fold_number=fold.fold_number,
                train_indices=fold.train_indices,
                test_indices=restricted,
                purged_indices=fold.purged_indices,
                embargoed_indices=fold.embargoed_indices,
                test_start_time=decision_times[restricted[0]],
                test_last_time=decision_times[restricted[-1]],
            )
        )
    if not holdout_folds:
        raise ValueError("留出期窗口内没有可用的 walk-forward 测试样本")
    holdout_eval = evaluate_walk_forward_forecasts(
        target,
        base_forecasts,
        aug_forecasts,
        folds=holdout_folds,
    )
    holdout_indices = [k for fold in holdout_folds for k in fold.test_indices]
    holdout_forecasts = [float(aug_forecasts[k]) for k in holdout_indices]
    holdout_actuals = [float(target[k]) for k in holdout_indices]
    return holdout_eval.incremental_oos_r_squared, holdout_forecasts, holdout_actuals


def _run_cost_gate(forecasts: list[float], actuals: list[float]) -> tuple[float, bool]:
    if not forecasts or len(forecasts) != len(actuals):
        raise ValueError("成本闸输入为空或长度不一致")
    positions = [1.0 if f > 0.0 else (-1.0 if f < 0.0 else 0.0) for f in forecasts]
    gross = [pos * act for pos, act in zip(positions, actuals, strict=True)]
    costs = [0.0] * len(positions)
    previous = 0.0
    for i, pos in enumerate(positions):
        if pos == previous:
            continue
        if previous == 0.0 or pos == 0.0:
            costs[i] += TAKER_FEE_PER_SIDE
        else:
            costs[i] += ROUND_TRIP_COST
        previous = pos
    if positions[-1] != 0.0:
        costs[-1] += TAKER_FEE_PER_SIDE
    net = [g - c for g, c in zip(gross, costs, strict=True)]
    net_daily_edge = sum(net) / len(net)
    boot = circular_block_bootstrap_mean_test(
        net,
        null_mean=0.0,
        block_length=BOOTSTRAP_BLOCK_LENGTH,
        resamples=BOOTSTRAP_RESAMPLES,
        seed=BOOTSTRAP_SEED,
    )
    return float(net_daily_edge), bool(boot.p_value < ALPHA)


def run(pit: PointInTime, params: dict) -> BlindMetrics:
    """Run the blind BTC/ETH/SOL transmission replication and return raw BlindMetrics."""
    raw = pit.table(
        DATASET,
        start=DAILY_START,
        end=CUTOFF,
        columns=["symbol", "open_time", "close", "knowable_time"],
    )
    scoped = raw.filter(
        pl.col("symbol").is_in(list(SYMBOLS))
        & (pl.col("open_time") >= DAILY_START)
        & (pl.col("open_time") < CUTOFF)
    )
    valid_close = (
        pl.col("close").is_not_null() & pl.col("close").is_finite() & (pl.col("close") > 0.0)
    )
    invalid_close_bars = int(scoped.filter(~valid_close).height)
    valid_rows = scoped.filter(valid_close).sort(["symbol", "open_time", "knowable_time"])
    deduped = valid_rows.unique(subset=["symbol", "open_time"], keep="last", maintain_order=True)
    duplicate_bars = int(valid_rows.height - deduped.height)

    daily_df, daily_dropped_incomplete_days = _aggregate_daily_1600(deduped)
    daily_panel = _build_synchronized_panel(
        _extract_symbol_series(daily_df, "day_open"),
        bar_seconds=DAILY_BAR_SECONDS,
    )

    hourly_df = deduped.filter(pl.col("open_time") >= HOURLY_START)
    hourly_panel = _build_synchronized_panel(
        _extract_symbol_series(hourly_df, "open_time"),
        bar_seconds=HOURLY_BAR_SECONDS,
    )

    primary = _partial_test(
        daily_panel.btc,
        daily_panel.eth,
        "sol",
        daily_panel.sol,
        panel=daily_panel,
        max_hac_lag=DAILY_MAX_HAC_LAG,
    )
    secondary = _partial_test(
        daily_panel.btc,
        daily_panel.sol,
        "eth",
        daily_panel.eth,
        panel=daily_panel,
        max_hac_lag=DAILY_MAX_HAC_LAG,
    )
    robustness = _partial_test(
        hourly_panel.btc,
        hourly_panel.eth,
        "sol",
        hourly_panel.sol,
        panel=hourly_panel,
        max_hac_lag=HOURLY_MAX_HAC_LAG,
    )

    holm = holm_adjust([primary.p_value, secondary.p_value, robustness.p_value], alpha=ALPHA)
    holdout_oos_r2, holdout_forecasts, holdout_actuals = _run_walk_forward_holdout(daily_panel)
    net_daily_edge, boot_reject = _run_cost_gate(holdout_forecasts, holdout_actuals)

    criteria = {
        "holm_adjusted_primary_p_below_0.05": bool(holm[0].adjusted_p_value < ALPHA),
        "primary_partial_r_squared_at_least_0.005": bool(
            primary.partial_r_squared is not None and primary.partial_r_squared >= MIN_EFFECT_SIZE
        ),
        "holdout_oos_incremental_r_squared_above_0": bool(
            holdout_oos_r2 is not None and holdout_oos_r2 > 0.0
        ),
        "net_daily_edge_after_costs_positive": bool(net_daily_edge > 0.0),
    }
    supported = bool(all(criteria.values()))

    values = {
        "supported": supported,
        "cost_gate_bootstrap_reject": bool(boot_reject),
        "criteria": criteria,
        "holm_reject": {
            "daily_btc_eth": bool(holm[0].reject),
            "daily_btc_sol": bool(holm[1].reject),
            "hourly_btc_eth": bool(holm[2].reject),
        },
        "tests": {
            "daily_btc_eth": {
                "coefficient": float(primary.coefficient),
                "observations": int(primary.observations),
            },
            "daily_btc_sol": {
                "coefficient": float(secondary.coefficient),
                "observations": int(secondary.observations),
            },
            "hourly_btc_eth": {
                "coefficient": float(robustness.coefficient),
                "observations": int(robustness.observations),
            },
        },
    }
    quality = {
        "daily_rows": len(daily_panel.times),
        "daily_dropped_incomplete_days": int(daily_dropped_incomplete_days),
        "daily_dropped_close_rows": int(daily_panel.dropped_close_rows),
        "daily_dropped_return_rows": int(daily_panel.dropped_return_rows),
        "daily_dropped_lag_pairs": int(daily_panel.dropped_lag_pairs),
        "hourly_rows": len(hourly_panel.times),
        "hourly_dropped_close_rows": int(hourly_panel.dropped_close_rows),
        "hourly_dropped_return_rows": int(hourly_panel.dropped_return_rows),
        "hourly_dropped_lag_pairs": int(hourly_panel.dropped_lag_pairs),
        "duplicate_bars": int(duplicate_bars),
        "invalid_close_bars": int(invalid_close_bars),
        "daily_first": str(daily_panel.times[0].isoformat(timespec="seconds")),
        "daily_last": str(daily_panel.times[-1].isoformat(timespec="seconds")),
        "hourly_first": str(hourly_panel.times[0].isoformat(timespec="seconds")),
        "hourly_last": str(hourly_panel.times[-1].isoformat(timespec="seconds")),
    }
    return BlindMetrics(values=values, quality=quality)
