"""Two hurdles every hypothesis faces before it may spend confirmation data.

Both use the normal approximation for the annualised Sharpe ratio estimator (standard
error about 1/sqrt(years)). Fat tails and autocorrelation make the real hurdle higher,
so read the results as lower bounds.
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
