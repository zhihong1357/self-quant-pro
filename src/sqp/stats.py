"""The statistics every hypothesis faces: the power hurdle before it may spend
confirmation data, and the Sharpe ratio's uncertainty and deflation after a test.

The hurdles use the normal approximation for the annualised Sharpe ratio estimator
(standard error about 1/sqrt(years)). Fat tails and autocorrelation make the real
hurdle higher, so read them as lower bounds.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import NormalDist

EULER_MASCHERONI = 0.5772156649015329
_Z = NormalDist()
_SQRT2 = math.sqrt(2.0)


def min_detectable_sharpe(years: float, t: float = 3.0, power: float = 0.8) -> float:
    """Smallest annualised Sharpe that reaches t-statistic `t` with probability `power`."""
    if years <= 0:
        raise ValueError("years must be positive")
    return (t + _Z.inv_cdf(power)) / math.sqrt(years)


def years_needed(sharpe: float, t: float = 3.0, power: float = 0.8) -> float:
    """Sample length (years) at which an annualised `sharpe` reaches `t` with `power`."""
    if sharpe <= 0:
        raise ValueError("sharpe must be positive")
    return ((t + _Z.inv_cdf(power)) / sharpe) ** 2


def expected_max_sharpe(trials: int, years: float) -> float:
    """Expected best annualised Sharpe among `trials` independent strategies with no edge.

    Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio".
    """
    if trials <= 1:
        return 0.0
    if years <= 0:
        raise ValueError("years must be positive")
    z1 = _Z.inv_cdf(1 - 1 / trials)
    z2 = _Z.inv_cdf(1 - 1 / (trials * math.e))
    return ((1 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2) / math.sqrt(years)


def detection_power(sharpe: float, years: float, t: float = 3.0) -> float:
    """Probability that an annualised `sharpe` reaches t-statistic `t` in `years` (one-sided,
    normal approximation): the inverse of min_detectable_sharpe."""
    if years <= 0:
        raise ValueError("years must be positive")
    return 1 - _Z.cdf(t - sharpe * math.sqrt(years))


def sharpe_standard_error(sharpe: float, n: int, skew: float = 0.0, kurtosis: float = 3.0) -> float:
    """Standard error of a per-period Sharpe ratio estimated from `n` returns, allowing
    for skew and fat tails (Mertens 2002; `kurtosis` is the raw kurtosis, 3 for normal).
    Serial correlation is not allowed for: use it only on returns that are not
    autocorrelated, or read it as a lower bound."""
    if n < 2:
        raise ValueError("n must be at least 2")
    variance = (1 - skew * sharpe + (kurtosis - 1) / 4 * sharpe * sharpe) / (n - 1)
    if variance <= 0:
        raise ValueError("skew and kurtosis are inconsistent with this Sharpe ratio")
    return math.sqrt(variance)


def deflated_sharpe(
    sharpe: float, n: int, trials: int, skew: float = 0.0, kurtosis: float = 3.0
) -> float:
    """Probability that the true per-period Sharpe exceeds what the best of `trials`
    edgeless strategies would show by luck (Bailey & Lopez de Prado 2014).

    The variance of the trials' Sharpe estimates is taken as that of one estimator
    under the null, 1 / (n - 1): the trials are assumed independent and of the same
    length. Correlated trials make the true bar lower, so this errs on the strict side.
    """
    if trials < 1:
        raise ValueError("trials must be at least 1")
    if n < 2:
        raise ValueError("n must be at least 2")
    bar = 0.0
    if trials > 1:
        z1 = _Z.inv_cdf(1 - 1 / trials)
        z2 = _Z.inv_cdf(1 - 1 / (trials * math.e))
        bar = ((1 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2) / math.sqrt(n - 1)
    return _Z.cdf((sharpe - bar) / sharpe_standard_error(sharpe, n, skew, kurtosis))


# --- Regression, HAC, Holm, purged walk-forward and block bootstrap (decision 0031) ---


@dataclass(frozen=True)
class OLSResult:
    observations: int
    predictor_names: tuple[str, ...]
    coefficient_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    intercept: float | None
    residuals: tuple[float, ...]
    sum_squared_residuals: float
    r_squared: float
    degrees_of_freedom: int
    xtx_inv: tuple[tuple[float, ...], ...]


@dataclass(frozen=True)
class HACCoefficient:
    name: str
    estimate: float
    standard_error: float
    t_statistic: float | None
    p_value: float | None


@dataclass(frozen=True)
class HACResult:
    ols: OLSResult
    observations: int
    max_lag: int
    small_sample_correction: bool
    coefficients: tuple[HACCoefficient, ...]


@dataclass(frozen=True)
class PartialLeadLagResult:
    lag_steps: int
    observations: int
    base_model: OLSResult
    augmented_model: OLSResult
    hac: HACResult
    source_effect: HACCoefficient
    incremental_r_squared: float
    partial_r_squared: float | None


@dataclass(frozen=True)
class HolmItem:
    original_index: int
    raw_p_value: float
    adjusted_p_value: float
    reject: bool


@dataclass(frozen=True)
class WalkForwardFold:
    fold_number: int
    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]
    purged_indices: tuple[int, ...]
    embargoed_indices: tuple[int, ...]
    test_start_time: datetime
    test_last_time: datetime


@dataclass(frozen=True)
class WalkForwardEvaluation:
    observations: int
    baseline_mse: float
    augmented_mse: float
    incremental_oos_r_squared: float | None


@dataclass(frozen=True)
class BootstrapMeanTestResult:
    sample_mean: float
    null_mean: float
    p_value: float
    observations: int
    block_length: int
    resamples: int
    seed: int


def _finite_floats(values: Iterable[float], name: str) -> list[float]:
    out = [float(v) for v in values]
    if not out:
        raise ValueError(f"{name} 不能为空")
    if not all(math.isfinite(v) for v in out):
        raise ValueError(f"{name} 包含非有限数值")
    return out


def _design_matrix(
    target: Iterable[float],
    predictors: Mapping[str, Iterable[float]],
    *,
    include_intercept: bool,
) -> tuple[list[float], list[list[float]], tuple[str, ...], tuple[str, ...]]:
    y = _finite_floats(target, "target")
    n = len(y)
    names: list[str] = []
    cols: list[list[float]] = []
    for name, raw in predictors.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("自变量名字不能为空")
        if name == "intercept" or name in names:
            raise ValueError(f"自变量名字冲突：{name}")
        col = _finite_floats(raw, f"predictor '{name}'")
        if len(col) != n:
            raise ValueError(f"自变量 '{name}' 长度与目标不一致")
        names.append(name)
        cols.append(col)
    if not cols and not include_intercept:
        raise ValueError("无截距项时至少需要一个自变量")
    if include_intercept:
        design = [[1.0, *(col[i] for col in cols)] for i in range(n)]
        coef_names = ("intercept", *names)
    else:
        design = [[col[i] for col in cols] for i in range(n)]
        coef_names = tuple(names)
    if n <= len(coef_names):
        raise ValueError("样本量必须大于待估参数个数")
    return y, design, tuple(names), coef_names


def _qr_solve(design: list[list[float]], y: list[float]) -> tuple[list[float], list[list[float]]]:
    """Householder QR for an n x k full-rank design matrix; returns (beta, (X'X)^-1)."""
    n = len(y)
    k = len(design[0])
    cols = [[design[i][j] for i in range(n)] for j in range(k)]
    qty = list(y)

    for j in range(k):
        col_j = cols[j]
        norm_sq = sum(col_j[i] * col_j[i] for i in range(j, n))
        norm = math.sqrt(norm_sq)
        if norm < 1e-12:
            raise ValueError("设计矩阵秩不足或共线")
        sign = 1.0 if col_j[j] >= 0.0 else -1.0
        u0 = col_j[j] + sign * norm
        v = [1.0, *(col_j[i] / u0 for i in range(j + 1, n))]
        tau = 2.0 / sum(x * x for x in v)

        col_j[j] = -sign * norm
        for i in range(j + 1, n):
            col_j[i] = 0.0

        for other in range(j + 1, k):
            c = cols[other]
            dot = sum(v[i - j] * c[i] for i in range(j, n))
            scale = tau * dot
            for i in range(j, n):
                c[i] -= scale * v[i - j]

        dot_y = sum(v[i - j] * qty[i] for i in range(j, n))
        scale_y = tau * dot_y
        for i in range(j, n):
            qty[i] -= scale_y * v[i - j]

    for j in range(k):
        if abs(cols[j][j]) < 1e-12:
            raise ValueError("设计矩阵在 QR 分解后奇异")

    beta = [0.0] * k
    for i in range(k - 1, -1, -1):
        rhs = qty[i] - sum(cols[j][i] * beta[j] for j in range(i + 1, k))
        beta[i] = rhs / cols[i][i]

    u_inv = [[0.0] * k for _ in range(k)]
    for col in range(k):
        for i in range(col, -1, -1):
            rhs = (1.0 if i == col else 0.0) - sum(
                cols[j][i] * u_inv[j][col] for j in range(i + 1, col + 1)
            )
            u_inv[i][col] = rhs / cols[i][i]

    xtx_inv = [
        [sum(u_inv[a][c] * u_inv[b][c] for c in range(max(a, b), k)) for b in range(k)]
        for a in range(k)
    ]
    return beta, xtx_inv


def ols_fit(
    target: Iterable[float],
    predictors: Mapping[str, Iterable[float]],
    *,
    include_intercept: bool = True,
) -> OLSResult:
    """Full-rank OLS using Householder QR (pure Python, deterministic)."""
    y, design, predictor_names, coefficient_names = _design_matrix(
        target, predictors, include_intercept=include_intercept
    )
    n, k = len(y), len(coefficient_names)
    beta, xtx_inv = _qr_solve(design, y)
    residuals = [
        yi - sum(row[j] * beta[j] for j in range(k)) for row, yi in zip(design, y, strict=True)
    ]
    ssr = sum(r * r for r in residuals)
    if include_intercept:
        mean_y = sum(y) / n
        sst = sum((yi - mean_y) ** 2 for yi in y)
    else:
        sst = sum(yi * yi for yi in y)
    eps = 2.220446049250313e-16
    r_squared = (1.0 if ssr <= eps else 0.0) if sst <= eps else (1.0 - ssr / sst)
    if not (math.isfinite(ssr) and math.isfinite(r_squared)):
        raise ArithmeticError("OLS 产生了非有限结果")
    return OLSResult(
        observations=n,
        predictor_names=predictor_names,
        coefficient_names=coefficient_names,
        coefficients=tuple(beta),
        intercept=beta[0] if include_intercept else None,
        residuals=tuple(residuals),
        sum_squared_residuals=max(0.0, ssr),
        r_squared=r_squared,
        degrees_of_freedom=n - k,
        xtx_inv=tuple(tuple(row) for row in xtx_inv),
    )


def newey_west_hac(
    target: Iterable[float],
    predictors: Mapping[str, Iterable[float]],
    *,
    max_lag: int,
    include_intercept: bool = True,
    small_sample_correction: bool = False,
) -> HACResult:
    """OLS with Bartlett-kernel Newey-West HAC standard errors and asymptotic normal p-values.

    Matches the Gen 2 myquant HAC kernel (`small_sample_correction=False` by default,
    two-sided normal reference `erfc(|t| / sqrt(2))`).
    """
    if isinstance(max_lag, bool) or not isinstance(max_lag, int) or max_lag < 0:
        raise ValueError("max_lag 必须是非负整数")
    y, design, _, coefficient_names = _design_matrix(
        target, predictors, include_intercept=include_intercept
    )
    n, k = len(y), len(coefficient_names)
    if max_lag >= n:
        raise ValueError("max_lag 必须小于样本量")
    fitted = ols_fit(y, predictors, include_intercept=include_intercept)
    scores = [
        [row[j] * res for j in range(k)] for row, res in zip(design, fitted.residuals, strict=True)
    ]

    meat = [[0.0] * k for _ in range(k)]
    for st in scores:
        for a in range(k):
            sa = st[a]
            for b in range(k):
                meat[a][b] += sa * st[b]
    for lag in range(1, max_lag + 1):
        weight = 1.0 - lag / (max_lag + 1.0)
        for i in range(lag, n):
            now, prev = scores[i], scores[i - lag]
            for a in range(k):
                na, pa = now[a], prev[a]
                for b in range(k):
                    meat[a][b] += weight * (na * prev[b] + pa * now[b])

    xtx_inv = fitted.xtx_inv
    left = [[sum(xtx_inv[a][c] * meat[c][b] for c in range(k)) for b in range(k)] for a in range(k)]
    cov = [[sum(left[a][c] * xtx_inv[c][b] for c in range(k)) for b in range(k)] for a in range(k)]
    if small_sample_correction:
        scale = n / fitted.degrees_of_freedom
        cov = [[v * scale for v in row] for row in cov]
    cov = [[0.5 * (cov[a][b] + cov[b][a]) for b in range(k)] for a in range(k)]

    diag_scale = max(1.0, max(abs(cov[i][i]) for i in range(k)))
    coeffs: list[HACCoefficient] = []
    for idx, name in enumerate(coefficient_names):
        var = cov[idx][idx]
        if not math.isfinite(var) or var < -1e-12 * diag_scale:
            raise ArithmeticError("HAC 方差出现异常负值或非有限值")
        se = math.sqrt(max(0.0, var))
        est = fitted.coefficients[idx]
        if se == 0.0:
            t_stat, p_val = None, None
        else:
            t_stat = est / se
            p_val = math.erfc(abs(t_stat) / _SQRT2)
        coeffs.append(HACCoefficient(name, est, se, t_stat, p_val))
    return HACResult(
        ols=fitted,
        observations=n,
        max_lag=max_lag,
        small_sample_correction=small_sample_correction,
        coefficients=tuple(coeffs),
    )


def partial_lead_lag(
    source: Iterable[float],
    target: Iterable[float],
    *,
    lag_steps: int = 1,
    common_factors: Mapping[str, Iterable[float]] | None = None,
    max_hac_lag: int,
    minimum_observations: int = 30,
) -> PartialLeadLagResult:
    """Test whether `source[t]` adds information about `target[t + lag_steps]` given
    `target[t]` and any `common_factors` observed at `t` (strictly point-in-time lagged)."""
    if isinstance(lag_steps, bool) or not isinstance(lag_steps, int) or lag_steps < 1:
        raise ValueError("lag_steps 必须是正整数")
    src = _finite_floats(source, "source")
    tgt = _finite_floats(target, "target")
    if len(src) != len(tgt):
        raise ValueError("source 与 target 长度必须一致")
    if lag_steps >= len(src) or len(src) - lag_steps < minimum_observations:
        raise ValueError("对齐后的样本量不足")

    base_predictors: dict[str, list[float]] = {"target_lag": tgt[:-lag_steps]}
    for name, raw in (common_factors or {}).items():
        if name in {"intercept", "target_lag", "source"}:
            raise ValueError(f"公共因子名字被保留：{name}")
        vec = _finite_floats(raw, f"common factor '{name}'")
        if len(vec) != len(tgt):
            raise ValueError(f"公共因子 '{name}' 长度与 target 不一致")
        base_predictors[name] = vec[:-lag_steps]

    response = tgt[lag_steps:]
    augmented_predictors = {**base_predictors, "source": src[:-lag_steps]}
    base_fit = ols_fit(response, base_predictors)
    augmented_fit = ols_fit(response, augmented_predictors)
    hac = newey_west_hac(response, augmented_predictors, max_lag=max_hac_lag)
    source_effect = next(c for c in hac.coefficients if c.name == "source")

    base_ssr = base_fit.sum_squared_residuals
    partial_r2: float | None = None
    if base_ssr > 0.0:
        partial_r2 = max(0.0, min(1.0, (base_ssr - augmented_fit.sum_squared_residuals) / base_ssr))
    return PartialLeadLagResult(
        lag_steps=lag_steps,
        observations=len(response),
        base_model=base_fit,
        augmented_model=augmented_fit,
        hac=hac,
        source_effect=source_effect,
        incremental_r_squared=augmented_fit.r_squared - base_fit.r_squared,
        partial_r_squared=partial_r2,
    )


def holm_adjust(p_values: Sequence[float], *, alpha: float = 0.05) -> tuple[HolmItem, ...]:
    """Step-down Holm-Bonferroni FWER adjustment preserving original order."""
    vals = _finite_floats(p_values, "p_values")
    if any(v < 0.0 or v > 1.0 for v in vals):
        raise ValueError("p_values 必须在 [0, 1] 之间")
    if not (0.0 < alpha < 1.0):
        raise ValueError("alpha 必须在 (0, 1) 之间")
    m = len(vals)
    order = sorted(range(m), key=lambda i: vals[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, idx in enumerate(order):
        running = max(running, (m - rank) * vals[idx])
        adjusted[idx] = min(1.0, running)
    return tuple(
        HolmItem(
            original_index=i,
            raw_p_value=vals[i],
            adjusted_p_value=adjusted[i],
            reject=adjusted[i] <= alpha,
        )
        for i in range(m)
    )


def purged_walk_forward_splits(
    decision_times: Sequence[datetime],
    label_end_times: Sequence[datetime],
    *,
    initial_train_size: int,
    test_size: int,
    step_size: int | None = None,
    embargo: timedelta = timedelta(0),
    minimum_train_size: int = 30,
) -> tuple[WalkForwardFold, ...]:
    """Disjoint chronological walk-forward folds with label purging and pre-test embargo."""
    n = len(decision_times)
    if n < 2 or len(label_end_times) != n:
        raise ValueError("decision_times 与 label_end_times 长度不匹配或过短")
    for i in range(n):
        if decision_times[i].tzinfo is None or label_end_times[i].tzinfo is None:
            raise ValueError("时间必须带时区")
        if label_end_times[i] <= decision_times[i]:
            raise ValueError("label_end_time 必须晚于 decision_time")
        if i > 0 and decision_times[i] <= decision_times[i - 1]:
            raise ValueError("decision_times 必须严格递增")
    advance = test_size if step_size is None else step_size
    if initial_train_size < minimum_train_size or test_size < 1 or advance < test_size:
        raise ValueError("walk-forward 窗口参数不合法")
    if embargo < timedelta(0):
        raise ValueError("embargo 不能为负")

    folds: list[WalkForwardFold] = []
    fold_number = 1
    for test_start in range(initial_train_size, n, advance):
        test_stop = min(test_start + test_size, n)
        t_start = decision_times[test_start]
        purged = tuple(i for i in range(test_start) if label_end_times[i] >= t_start)
        purged_set = set(purged)
        after_purge = [i for i in range(test_start) if i not in purged_set]
        cutoff = t_start - embargo
        embargoed = tuple(i for i in after_purge if decision_times[i] >= cutoff)
        embargoed_set = set(embargoed)
        train = tuple(i for i in after_purge if i not in embargoed_set)
        if len(train) >= minimum_train_size:
            folds.append(
                WalkForwardFold(
                    fold_number=fold_number,
                    train_indices=train,
                    test_indices=tuple(range(test_start, test_stop)),
                    purged_indices=purged,
                    embargoed_indices=embargoed,
                    test_start_time=decision_times[test_start],
                    test_last_time=decision_times[test_stop - 1],
                )
            )
        fold_number += 1
    if not folds:
        raise ValueError("没有可用的 walk-forward 测试折")
    return tuple(folds)


def evaluate_walk_forward_forecasts(
    actual: Sequence[float],
    baseline_forecasts: Sequence[float],
    augmented_forecasts: Sequence[float],
    *,
    folds: Sequence[WalkForwardFold],
) -> WalkForwardEvaluation:
    """Score paired OOS forecasts over supplied chronological non-overlapping folds."""
    n = len(actual)
    if len(baseline_forecasts) != n or len(augmented_forecasts) != n or not folds:
        raise ValueError("预测向量长度不匹配或折数为空")
    base_sse = 0.0
    aug_sse = 0.0
    count = 0
    prev_end = -1
    for fold in folds:
        if fold.test_indices[0] <= prev_end:
            raise ValueError("测试折必须按时间顺序且互不重叠")
        prev_end = fold.test_indices[-1]
        for idx in fold.test_indices:
            y, b, a = (
                float(actual[idx]),
                float(baseline_forecasts[idx]),
                float(augmented_forecasts[idx]),
            )
            if not (math.isfinite(y) and math.isfinite(b) and math.isfinite(a)):
                raise ValueError("测试折里存在非有限预测或目标值")
            base_sse += (y - b) ** 2
            aug_sse += (y - a) ** 2
            count += 1
    inc_r2 = None if base_sse == 0.0 else 1.0 - aug_sse / base_sse
    return WalkForwardEvaluation(
        observations=count,
        baseline_mse=base_sse / count,
        augmented_mse=aug_sse / count,
        incremental_oos_r_squared=inc_r2,
    )


def circular_block_bootstrap_mean_test(
    values: Iterable[float],
    *,
    null_mean: float = 0.0,
    block_length: int,
    resamples: int = 2000,
    seed: int = 42,
) -> BootstrapMeanTestResult:
    """Two-sided circular block-bootstrap test of H0: mean(values) = null_mean.

    Uses an isolated `random.Random(seed)` so repeated runs are byte-for-byte identical.
    """
    sample = _finite_floats(values, "values")
    n = len(sample)
    if n < 2:
        raise ValueError("自助法至少需要 2 个观测")
    max_block = max(1, n // 2)
    if block_length < 1 or block_length > max_block:
        raise ValueError(f"block_length 必须在 [1, {max_block}] 内")
    if resamples < 100 or seed < 0 or not math.isfinite(null_mean):
        raise ValueError("自助法参数不合法")

    obs_mean = sum(sample) / n
    centered = [v - obs_mean + null_mean for v in sample]
    obs_dist = abs(obs_mean - null_mean)
    blocks = (n + block_length - 1) // block_length
    rng = random.Random(seed)
    exceedances = 0
    for _ in range(resamples):
        total = 0.0
        taken = 0
        for _ in range(blocks):
            start = rng.randrange(n)
            limit = min(block_length, n - taken)
            for offset in range(limit):
                total += centered[(start + offset) % n]
            taken += limit
        if abs(total / n - null_mean) >= obs_dist:
            exceedances += 1
    p_value = (exceedances + 1) / (resamples + 1)
    return BootstrapMeanTestResult(
        sample_mean=obs_mean,
        null_mean=null_mean,
        p_value=p_value,
        observations=n,
        block_length=block_length,
        resamples=resamples,
        seed=seed,
    )
