"""Descriptive profile of a universe's exploration segment (decision 0021).

It describes the data and never performance: no returns, and no statistic that
relates a signal to later returns, so a run is not a trial (protocol section 6).
It reads only through the point-in-time interface, with `end` just before the
confirmation segment.

The report is deterministic for the same code and inputs: no timestamps in the
body, sorted keys, rounded numbers. Its object name carries the code commit and
a fingerprint of the inputs (the SHA-256 of every table read), so a second run
either finds the same report and confirms its fingerprint, or exposes
non-determinism.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from sqp.data.pit import PointInTime
from sqp.storage import ObjectExists, Store

REPORT_PREFIX = "derived/profiles/"
QUANTILES = (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)
EXTREME = 0.001  # 0.1% in one settlement
DEFAULT_RATE = 0.0001  # Binance's interest-only rate per 8-hour settlement
MIN_CROSS_SECTION = 20  # quintiles need at least this many contracts at one settlement


def _r(x: float | None) -> float | None:
    return None if x is None else float(f"{x:.6g}")


def exploration_end(pit: PointInTime) -> datetime:
    confirmation = next(s for s in pit.segments if s.name == "confirmation")
    return confirmation.start - timedelta(microseconds=1)


def code_commit(repo_root: Path) -> str:
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    head = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    dirty = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain"], capture_output=True, text=True
    )
    commit = head.stdout.strip() or "unknown"
    return commit + ("-dirty" if dirty.stdout.strip() else "")


def _distribution(df: pl.DataFrame) -> dict:
    if df.is_empty():
        return {"settlements": 0}
    rate, annual = df["funding_rate"], df["annualized"]
    return {
        "settlements": df.height,
        "contracts": df["symbol"].n_unique(),
        "rate_mean": _r(rate.mean()),
        "rate_std": _r(rate.std()),
        "rate_quantiles": {str(q): _r(rate.quantile(q, interpolation="linear")) for q in QUANTILES},
        "annualized_mean": _r(annual.mean()),
        "annualized_median": _r(annual.median()),
        "annualized_median_off_default": _r(annual.filter(rate != DEFAULT_RATE).median()),
        "share_positive": _r((rate > 0).mean()),
        "share_default_rate": _r((rate == DEFAULT_RATE).mean()),
        "share_extreme": _r((rate.abs() >= EXTREME).mean()),
    }


def _persistence(df: pl.DataFrame) -> dict:
    """Does a high rate stay high? Autocorrelation, and cross-sectional quintile stays."""
    df = df.sort(["symbol", "calc_time"])
    auto = {}
    for lag in (1, 3, 9):
        pairs = df.select(
            "funding_rate", pl.col("funding_rate").shift(lag).over("symbol").alias("before")
        ).drop_nulls()
        auto[str(lag)] = _r(pairs.select(pl.corr("funding_rate", "before")).item())
    hour = pl.col("calc_time").dt.truncate("1h")
    size = pl.len().over(hour)
    rank = pl.col("funding_rate").rank("average").over(hour)
    ranked = (
        df.with_columns(size=size, quintile=((rank - 1) * 5 / size).floor().cast(pl.Int64) + 1)
        .filter(pl.col("size") >= MIN_CROSS_SECTION)
        .sort(["symbol", "calc_time"])
        .with_columns(following=pl.col("quintile").shift(-1).over("symbol"))
        .drop_nulls("following")
    )
    stays = (
        ranked.group_by("quintile")
        .agg((pl.col("following") == pl.col("quintile")).mean().alias("stay"))
        .sort("quintile")
    )
    return {
        "autocorrelation_by_lag": auto,
        "quintile_stay_probability": {
            str(q): _r(s) for q, s in zip(stays["quintile"], stays["stay"], strict=True)
        },
        "quintile_pairs": ranked.height,
    }


def _liquidity_terciles(funding: pl.DataFrame, klines: pl.DataFrame) -> pl.DataFrame:
    """Each funding row's liquidity tercile (1 = least liquid) for its month, from the
    mean daily quote volume over the 30 days before the month starts."""
    daily = (
        klines.with_columns(day=pl.col("open_time").dt.truncate("1d"))
        .group_by("symbol", "day")
        .agg(pl.col("quote_volume").sum())
    )
    groups = []
    for month in sorted(funding["month"].unique().to_list()):
        start = datetime(int(month[:4]), int(month[5:]), 1, tzinfo=UTC)
        window = daily.filter(
            (pl.col("day") >= start - timedelta(days=30)) & (pl.col("day") < start)
        )
        active = funding.filter(pl.col("month") == month)["symbol"].unique()
        liquidity = (
            window.filter(pl.col("symbol").is_in(active.implode()))
            .group_by("symbol")
            .agg(pl.col("quote_volume").mean().alias("liquidity"))
        )
        if liquidity.height < 3:
            continue
        rank = pl.col("liquidity").rank("ordinal")
        groups.append(
            liquidity.sort("symbol").with_columns(
                month=pl.lit(month),
                tercile=((rank - 1) * 3 / pl.len()).floor().cast(pl.Int64) + 1,
            )
        )
    if not groups:
        return funding.with_columns(tercile=pl.lit(None, dtype=pl.Int64))
    terciles = pl.concat(groups).select("symbol", "month", "tercile")
    return funding.join(terciles, on=["symbol", "month"], how="left")


def _by_tercile(df: pl.DataFrame) -> dict:
    return {str(t): _distribution(df.filter(pl.col("tercile") == t)) for t in (1, 2, 3)} | {
        "unranked_settlements": df.filter(pl.col("tercile").is_null()).height
    }


def build_profile(pit: PointInTime) -> dict:
    end = exploration_end(pit)
    funding = pit.table(
        "um-funding",
        end=end,
        columns=["symbol", "calc_time", "funding_interval_hours", "funding_rate"],
    )
    hours = pl.col("funding_interval_hours").fill_null(8)
    funding = funding.with_columns(
        annualized=pl.col("funding_rate") * (24 / hours) * 365,
        year=pl.col("calc_time").dt.year(),
        month=pl.col("calc_time").dt.strftime("%Y-%m"),
    )
    klines = pit.table("um-klines-1h", end=end, columns=["symbol", "open_time", "quote_volume"])
    funding = _liquidity_terciles(funding, klines)
    per_month = funding.group_by("month").agg(pl.col("symbol").n_unique().alias("n")).sort("month")
    bear = funding.filter(pl.col("year") == 2022)
    return {
        "universe": pit.universe,
        "segment": "exploration",
        "end": end.isoformat(),
        "definitions": {
            "annualized": "rate x (24 / interval hours) x 365; interval defaults to 8 hours",
            "extreme": f"|rate| >= {EXTREME} in one settlement",
            "default_rate": f"rate == {DEFAULT_RATE}, the interest-only rate",
            "liquidity": "mean daily quote volume over the 30 days before the month",
            "quintile_stay": "probability the next settlement stays in the same "
            f"cross-sectional quintile (settlements with >= {MIN_CROSS_SECTION} contracts)",
        },
        "coverage": {
            "first_month": per_month["month"].min(),
            "last_month": per_month["month"].max(),
            "contracts_per_month": {
                "min": per_month["n"].min(),
                "median": _r(per_month["n"].median()),
                "max": per_month["n"].max(),
            },
        },
        "funding": {
            "all": _distribution(funding),
            "by_year": {
                str(y): _distribution(funding.filter(pl.col("year") == y))
                for y in sorted(funding["year"].unique().to_list())
            },
            "by_liquidity_tercile": _by_tercile(funding),
        },
        "persistence": _persistence(funding),
        "bear_2022": {"all": _distribution(bear), "by_liquidity_tercile": _by_tercile(bear)},
    }


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.2f}%"


def render_markdown(report: dict) -> str:
    """A short Chinese summary for the owner; the JSON holds everything."""
    columns = ["范围", "结算次数", "合约数", "年化均值", "不含默认费率的年化中位数", "为正的比例"]
    columns += ["恰为默认费率", "极端（≥0.1%）"]
    head = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]

    def row(name: str, d: dict) -> str:
        if not d.get("settlements"):
            return f"| {name} | 0 | | | | | | |"
        return (
            f"| {name} | {d['settlements']:,} | {d['contracts']} | {_pct(d['annualized_mean'])} | "
            f"{_pct(d['annualized_median_off_default'])} | {_pct(d['share_positive'])} | "
            f"{_pct(d['share_default_rate'])} | {_pct(d['share_extreme'])} |"
        )

    funding = report["funding"]
    lines = [
        f"# {report['universe']} 探索集画像（截至 {report['end'][:10]}）",
        "",
        "只描述数据，不算任何收益。完整数字见同名的 .json 文件。",
        "",
        "## 资金费率的分布",
        "",
        *head,
        row("全部", funding["all"]),
        *(row(year, d) for year, d in funding["by_year"].items()),
        "",
        "## 按流动性三等分（1 = 最不活跃）",
        "",
        *head,
        *(row(f"第 {t} 组", funding["by_liquidity_tercile"][t]) for t in ("1", "2", "3")),
        "",
        "## 2022 年熊市",
        "",
        *head,
        row("2022 全部", report["bear_2022"]["all"]),
        *(
            row(f"2022 第 {t} 组", report["bear_2022"]["by_liquidity_tercile"][t])
            for t in ("1", "2", "3")
        ),
        "",
        "## 持续性：这一期高，下一期还高吗",
        "",
        "| 间隔（结算次数） | 1 | 3 | 9 |",
        "|---|---|---|---|",
        "| 自相关 | "
        + " | ".join(
            str(report["persistence"]["autocorrelation_by_lag"][k]) for k in ("1", "3", "9")
        )
        + " |",
        "",
        "| 五分位 | 1（最低） | 2 | 3 | 4 | 5（最高） |",
        "|---|---|---|---|---|---|",
        "| 下一期留在同一组的概率 | "
        + " | ".join(
            _pct(report["persistence"]["quintile_stay_probability"].get(str(q)))
            for q in range(1, 6)
        )
        + " |",
        "",
        f"代码：`{report['code_commit']}`；输入：{len(report['inputs'])} 个分块，"
        f"指纹 `{report['inputs_fingerprint'][:16]}`。",
        "",
    ]
    return "\n".join(lines)


def write_profile(pit: PointInTime, store: Store, repo_root: Path) -> tuple[str, str, bool]:
    """Build, then store the JSON and the summary. Returns (key, fingerprint, already there)."""
    report = build_profile(pit)
    inputs = [{"key": key, "sha256": pit.inputs[key]} for key in sorted(pit.inputs)]
    report["inputs"] = inputs
    lines = "".join(f"{i['key']} {i['sha256']}\n" for i in inputs)
    report["inputs_fingerprint"] = hashlib.sha256(lines.encode()).hexdigest()
    report["code_commit"] = code_commit(repo_root)
    body = json.dumps(report, sort_keys=True, ensure_ascii=False, indent=1).encode()
    fingerprint = hashlib.sha256(body).hexdigest()
    commit = report["code_commit"]
    code = commit[:12] + ("-dirty" if commit.endswith("-dirty") else "")
    stem = (
        f"{REPORT_PREFIX}{pit.universe}-exploration/"
        f"code-{code}-inputs-{report['inputs_fingerprint'][:12]}"
    )
    try:
        store.put(stem + ".json", body, content_type="application/json; charset=utf-8")
    except ObjectExists:
        stored = hashlib.sha256(store.get(stem + ".json")).hexdigest()
        if stored != fingerprint:
            raise RuntimeError(
                f"同样的代码和输入，算出的画像不一样：{stored[:16]} ≠ {fingerprint[:16]}"
            ) from None
        return stem + ".json", fingerprint, True
    summary = render_markdown(report).encode()
    store.put(stem + ".md", summary, content_type="text/markdown; charset=utf-8")
    return stem + ".json", fingerprint, False
