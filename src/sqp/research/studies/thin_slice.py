"""H-0001, the thin slice, rebuilt from the second generation (migration acceptance A).

The synthetic world is the one finance-research used (its sources/synthetic.py),
generated the same way from the same seed, so the numbers can be compared:

* the true indicator x[m] for month m is standard normal;
* its first release, `first_release_days` after month m ends, is x[m] plus noise;
  its revision, `revision_days` after month m ends, is x[m] itself;
* the return of month m + lag is beta * x[m] plus noise, public when that month ends.

A bet on month m + lag is placed when that month starts. At that moment only the
first release is public; using the revision is looking ahead. The study pairs each
return with the indicator as it was knowable at the decision (the same rule as
`sqp.data.pit`: a version is visible once its knowable time is at or before the
moment), and, as a control, with the indicator as known at the end of the run.

The world is synthetic, so the run reads no market data (`pit` is None) and does
not count as a trial (protocol section 6). Values are rounded to six decimals as
in the second generation, so the bytes, and hence the result, do not depend on the
platform's last bit.
"""

from __future__ import annotations

import math
import random
import statistics
from datetime import date, timedelta

from sqp.research.studies import SECTIONS, Outcome

DECIMALS = 6


def month_start(first: date, months: int) -> date:
    index = first.year * 12 + first.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def generate(params: dict) -> tuple[list[tuple], list[tuple]]:
    """(indicator versions, returns) as (month, knowable day, value), second-gen order."""
    rng = random.Random(int(params["seed"]))
    first = date.fromisoformat(f"{params['first_month']}-01")
    beta, lag = float(params["beta"]), int(params["lag_months"])
    release_after = timedelta(days=int(params["first_release_days"]))
    revision_after = timedelta(days=int(params["revision_days"]))
    indicator, returns = [], []
    for m in range(int(params["months"])):
        x = rng.gauss(0.0, 1.0)
        released = x + rng.gauss(0.0, float(params["release_noise"]))
        y = beta * x + rng.gauss(0.0, float(params["return_noise"]))
        month, month_end = month_start(first, m), month_start(first, m + 1)
        indicator.append((month, month_end + release_after, round(released, DECIMALS)))
        indicator.append((month, month_end + revision_after, round(x, DECIMALS)))
        returns.append(
            (month_start(first, m + lag), month_start(first, m + lag + 1), round(y, DECIMALS))
        )
    return indicator, returns


def as_known(indicator: list[tuple], moment: date) -> dict[date, float]:
    """The latest version of each month's indicator knowable at `moment`."""
    known: dict[date, tuple[date, float]] = {}
    for month, knowable, value in indicator:
        if knowable <= moment and (month not in known or knowable >= known[month][0]):
            known[month] = (knowable, value)
    return {month: value for month, (_, value) in known.items()}


def theory(params: dict) -> tuple[float, float]:
    """Population correlations with the return: (first release, true value)."""
    beta = float(params["beta"])
    sd_y = math.sqrt(beta * beta + float(params["return_noise"]) ** 2)
    sd_first = math.sqrt(1.0 + float(params["release_noise"]) ** 2)
    return beta / (sd_y * sd_first), beta / sd_y


def fisher(r: float, n: int, confidence: float) -> tuple[float, float]:
    z = statistics.NormalDist().inv_cdf(0.5 + confidence / 2)
    centre, half = math.atanh(r), z / math.sqrt(n - 3)
    return math.tanh(centre - half), math.tanh(centre + half)


def t_value(r: float, n: int) -> float:
    return r * math.sqrt((n - 2) / (1 - r * r))


def run(pit: None, params: dict) -> Outcome:
    lag, confidence = int(params["lag_months"]), float(params["confidence"])
    limit = date.fromisoformat(params["clock_limit"])
    indicator, returns = generate(params)
    public = {month: y for month, knowable, y in returns if knowable <= limit}
    revised = as_known(indicator, limit)  # what a naive backtest would use
    x_pit, x_revised, y = [], [], []
    for decision in sorted(public):
        month = month_start(decision, -lag)
        then = as_known(indicator, decision)
        if month in then and month in revised:
            x_pit.append(then[month])
            x_revised.append(revised[month])
            y.append(public[decision])
    n = len(y)
    r_pit = statistics.correlation(x_pit, y)
    r_revised = statistics.correlation(x_revised, y)
    low, high = fisher(r_pit, n, confidence)
    th_pit, th_revised = theory(params)
    previous = float(params["gen2_corr_pit"])
    standard_error = (1 - r_pit * r_pit) / math.sqrt(n - 1)
    metrics = {
        "pairs": n,
        "pairs_differing": sum(a != b for a, b in zip(x_pit, x_revised, strict=True)),
        "corr_pit": round(r_pit, 12),
        "corr_pit_low": round(low, 12),
        "corr_pit_high": round(high, 12),
        "t_pit": round(t_value(r_pit, n), 12),
        "corr_revised": round(r_revised, 12),
        "t_revised": round(t_value(r_revised, n), 12),
        "theory_pit": round(th_pit, 12),
        "theory_revised": round(th_revised, 12),
        "theory_inside_interval": low <= th_pit <= high,
        "gen2_corr_pit": previous,
        "gen2_gap_in_standard_errors": round(abs(r_pit - previous) / standard_error, 6),
    }
    return Outcome(metrics=metrics, sections=sections(metrics, params, min(public), max(public)))


def f3(v: float) -> str:
    return f"{v:.3f}"


def sections(m: dict, params: dict, first: date, last: date) -> dict:
    n, pct, lag = m["pairs"], round(float(params["confidence"]) * 100), params["lag_months"]
    inside = "落在区间内" if m["theory_inside_interval"] else "不在区间内，需要排查"
    same = "一致" if m["gen2_gap_in_standard_errors"] <= 1 else "不一致，需要排查"
    gap = m["corr_revised"] - m["corr_pit"]
    text = {
        "结论": (
            f"在真相已知的合成世界里，按决策时刻能看到的首发值计算，指标与 {lag} 个月后收益的"
            f"相关系数为 {f3(m['corr_pit'])}（{pct}% 置信区间 {f3(m['corr_pit_low'])} 至 "
            f"{f3(m['corr_pit_high'])}，{n} 对），理论值 {f3(m['theory_pit'])} {inside}。"
            f"改用事后的修订值，相关系数为 {f3(m['corr_revised'])}，高出 {f3(gap)}：这就是"
            "修订带来的前视偏差。和第二代 finance-research 的结果（"
            f"{f3(m['gen2_corr_pit'])}）相差 {m['gen2_gap_in_standard_errors']:.3f} 个标准误，"
            f"{same}。"
        ),
        "假说与机制": "见 registry/hypotheses/H-0001.toml：时点读取把修订挡在回测之外。",
        "数据": (
            f"合成数据，由代码按固定种子 {params['seed']} 生成，和第二代的生成方法相同。"
            f"指标的首发值在月份结束后 {params['first_release_days']} 天可知（含误差），修订值在 "
            f"{params['revision_days']} 天后可知（等于真实值）；收益在该月结束时可知。"
            f"决策月份 {first:%Y-%m} 至 {last:%Y-%m}，运行的时钟上限 {params['clock_limit']}。"
            "已知缺陷：数据是合成的，不含真实市场的任何特征。"
        ),
        "方法": (
            f"每个月初做一次决策，取当时可知的 {lag} 个月前那个月的指标（只能是首发值），"
            "与该月收益配对；对照组用时钟上限时可知的版本（修订值）。皮尔逊相关，"
            "t 值为 r·√((n−2)/(1−r²))，置信区间用费希尔变换。"
            "和第二代的偏离：时点规则在研究代码里实现（合成数据不经过存储桶），"
            "规则与 sqp.data.pit 相同：可知时刻不晚于决策时刻的最新版本。"
        ),
        "结果": (
            "| 量 | 时点数据（首发值） | 修订后数据（对照） |\n|---|---|---|\n"
            f"| 相关系数 | {f3(m['corr_pit'])} | {f3(m['corr_revised'])} |\n"
            f"| 理论值 | {f3(m['theory_pit'])} | {f3(m['theory_revised'])} |\n"
            f"| t 值 | {m['t_pit']:.2f} | {m['t_revised']:.2f} |\n"
            f"| 配对数 | {n} | {n} |\n\n"
            f"时点值和修订值逐对比较：{m['pairs_differing']} / {n} 对不同。"
            "合成数据上的运行不计入试验次数，所以不做多重检验折损。"
        ),
        "边际贡献": "不适用：元研究，研究对象是流程本身，不产生进入模型的信号。",
        "成本与容量": "不适用：没有交易。",
        "稳健性": "本次不做。换种子、换误差、换公布延迟属于 G3 的元研究。",
        "适用范围与停止条件": (
            f"只适用于这个合成世界：一个月度指标、一次修订、{lag} 个月的领先期。"
            "结论关于流程本身，不关于任何真实市场。"
        ),
    }
    return {name: text[name] for name in SECTIONS}
