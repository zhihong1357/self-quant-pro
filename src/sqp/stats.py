"""The statistics every hypothesis faces: the power hurdle before it may spend
confirmation data, and the Sharpe ratio's uncertainty and deflation after a test.

The hurdles use the normal approximation for the annualised Sharpe ratio estimator
(standard error about 1/sqrt(years)). Fat tails and autocorrelation make the real
hurdle higher, so read them as lower bounds.
"""

from __future__ import annotations

import math
from statistics import NormalDist

EULER_MASCHERONI = 0.5772156649015329
_Z = NormalDist()


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
