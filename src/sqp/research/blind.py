"""Framework-side blind comparison for burned-family replications (decision 0031).

This module lives outside `src/sqp/research/studies/` and is NOT on `research-boundary`'s
allow-list, so study code can neither import it nor read inherited reference files.
The trial runner:
1. validates blind parameters (`validate_params`) before writing any ledger marker;
2. resolves the pinned upstream reference from `origin/main:registry/inherited/manifest.json`;
3. loads its bytes from private bucket cache (`raw/gen2/...`) or pinned GitHub raw URL,
   checking exact byte length and SHA-256 (falling back to GitHub if the cache is damaged),
   and validates the reference JSON schema before consuming the one-time vault approval;
4. feeds the study's in-memory `BlindMetrics` into the comparator here;
5. enforces exact builtin types, real UTC timestamps within the authorized window, bounded
   panel-quality counts, clamped sample differences and capped `delta_se`, discards all raw
   third-party estimates from guarded segments, and emits only allowlisted blind outputs.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import tomllib
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqp.data import vision
from sqp.data.pit import IntegrityError
from sqp.research.studies import SECTIONS, BlindMetrics, Outcome
from sqp.storage import Store

MANIFEST_PATH = "registry/inherited/manifest.json"
INHERITANCE_CONFIG = "config/inheritance.toml"
RAW_GEN2_PREFIX = "raw/gen2/"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_GITHUB_REPO = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\+00:00|Z)$")
_MONTH_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])$")

DEFAULT_MAX_DELTA_SE = 1.0
DEFAULT_MAX_SAMPLE_DIFF_RATIO = 0.05
MAX_REPORTED_DELTA_SE = 9.99

BTC_ETH_TESTS = ("daily_btc_eth", "daily_btc_sol", "hourly_btc_eth")
BTC_ETH_CRITERIA = (
    "holm_adjusted_primary_p_below_0.05",
    "primary_partial_r_squared_at_least_0.005",
    "holdout_oos_incremental_r_squared_above_0",
    "net_daily_edge_after_costs_positive",
)
BTC_ETH_DAILY_WINDOW_START = datetime(2023, 1, 1, 0, 0, tzinfo=UTC)
BTC_ETH_HOURLY_WINDOW_START = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
BTC_ETH_DEFAULT_MAX_TIME = datetime(2026, 11, 19, 23, 59, 59, tzinfo=UTC)

QUALITY_DAILY_INT_KEYS = (
    "daily_rows",
    "daily_dropped_incomplete_days",
    "daily_dropped_close_rows",
    "daily_dropped_return_rows",
    "daily_dropped_lag_pairs",
)
QUALITY_HOURLY_INT_KEYS = (
    "hourly_rows",
    "hourly_dropped_close_rows",
    "hourly_dropped_return_rows",
    "hourly_dropped_lag_pairs",
)
QUALITY_RAW_INT_KEYS = ("duplicate_bars", "invalid_close_bars")
QUALITY_INT_KEYS = (*QUALITY_DAILY_INT_KEYS, *QUALITY_HOURLY_INT_KEYS, *QUALITY_RAW_INT_KEYS)
QUALITY_TIME_KEYS = ("daily_first", "daily_last", "hourly_first", "hourly_last")

FORBIDDEN_KEYS = frozenset(
    {
        "actuals",
        "adjusted_p_value",
        "augmented_mse",
        "augmented_r_squared",
        "base_r_squared",
        "baseline_mse",
        "beta",
        "coef",
        "coefficient",
        "correlation",
        "equity",
        "forecasts",
        "gross_daily_edge",
        "incremental_oos_r_squared",
        "incremental_r_squared",
        "mean_return",
        "median_trailing_vol",
        "mse",
        "net_daily_edge",
        "net_edge_bootstrap_p_value",
        "net_edge_ci_lower",
        "net_edge_ci_upper",
        "ols_r_squared",
        "ols_slope",
        "p_adj",
        "p_value",
        "partial_r_squared",
        "pnl",
        "r2",
        "r_squared",
        "raw_p_value",
        "return",
        "returns",
        "se",
        "sharpe",
        "standard_error",
        "std_err",
        "t_stat",
        "t_statistic",
        "t_value",
        "total_cost",
    }
)


@dataclass(frozen=True)
class InheritedRef:
    source: str
    url: str
    commit: str
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class ComparatorSpec:
    comparator: Callable[..., Outcome]
    ref_validator: Callable[[bytes], object]
    required_dataset: str
    required_symbols: tuple[str, ...]
    min_start_month: str
    min_end_month: str
    min_tests: int
    allowed_params: frozenset[str]
    max_delta_se: float = DEFAULT_MAX_DELTA_SE
    max_sample_diff_ratio: float = DEFAULT_MAX_SAMPLE_DIFF_RATIO


def raw_cache_key(ref: InheritedRef) -> str:
    return f"{RAW_GEN2_PREFIX}{ref.source}/{ref.commit}/{ref.path}"


def _valid_relpath(path: object) -> bool:
    if type(path) is not str or not path or path.startswith("/") or "\\" in path:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def manifest_entry(
    read: Callable[[str], str | None], source: object, path: object
) -> InheritedRef | None:
    """Resolve a pinned upstream inherited file from `manifest.json` and `inheritance.toml`."""
    if type(source) is not str or not source or not _valid_relpath(path):
        return None
    manifest_raw = read(MANIFEST_PATH)
    config_raw = read(INHERITANCE_CONFIG)
    if manifest_raw is None or config_raw is None:
        return None
    try:
        manifest = json.loads(manifest_raw)
        cfg = tomllib.loads(config_raw)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError):
        return None
    urls = {s.get("name"): s.get("url") for s in cfg.get("source", []) if isinstance(s, dict)}
    url = urls.get(source)
    if not isinstance(url, str) or not _GITHUB_REPO.match(url):
        return None
    for src in manifest.get("sources", []):
        if src.get("name") != source:
            continue
        commit = src.get("commit")
        if not isinstance(commit, str) or not _HEX40.match(commit):
            return None
        for item in src.get("files", []):
            if item.get("path") == path:
                size, digest = item.get("bytes"), item.get("sha256")
                if type(size) is int and size > 0 and type(digest) is str and _HEX64.match(digest):
                    return InheritedRef(source, url, commit, str(path), size, digest)
    return None


def raw_github_url(ref: InheritedRef) -> str:
    match = _GITHUB_REPO.match(ref.url)
    if match is None:
        raise IntegrityError(f"继承来源地址不是合法的 GitHub 仓库：{ref.url!r}")
    encoded = urllib.parse.quote(ref.path, safe="/")
    return f"https://raw.githubusercontent.com/{match.group(1)}/{ref.commit}/{encoded}"


def _matches_manifest(ref: InheritedRef, data: bytes) -> bool:
    return len(data) == ref.bytes and hashlib.sha256(data).hexdigest() == ref.sha256


def load_reference_bytes(
    ref: InheritedRef,
    store: Store,
    *,
    fetch: Callable[[str], bytes] | None = None,
) -> bytes:
    """Load pinned Gen 2 reference bytes from store cache or GitHub and verify SHA-256.

    If an existing append-only bucket cache object fails manifest verification, falls back
    to fetching from the pinned GitHub commit URL so a damaged cache entry never permanently
    bricks blind replications.
    """
    cache_key = raw_cache_key(ref)
    if store.exists(cache_key):
        cached = store.get(cache_key)
        if _matches_manifest(ref, cached):
            return cached
        print(
            f"警告：存储桶缓存 {cache_key} 与 manifest 校验不符，改从固定上游提交回退拉取。",
            file=sys.stderr,
        )
    getter = vision.http_get if fetch is None else fetch
    data = getter(raw_github_url(ref))
    if len(data) != ref.bytes:
        raise IntegrityError(
            f"继承对照文件字节数与 manifest 不一致：{ref.source}/{ref.path} "
            f"（预期 {ref.bytes}，实际 {len(data)}）"
        )
    digest = hashlib.sha256(data).hexdigest()
    if digest != ref.sha256:
        raise IntegrityError(f"继承对照文件 SHA-256 与 manifest 不一致：{ref.source}/{ref.path}")
    return data


def assert_no_forbidden_keys(obj: object, prefix: str = "") -> None:
    """Recursively verify that a blind output structure contains no raw-metric keys."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if type(key) is not str:
                raise ValueError("盲输出字典的键必须是内置字符串")
            if key.lower() in FORBIDDEN_KEYS:
                raise ValueError(f"盲输出包含违禁字段：{prefix}{key}")
            assert_no_forbidden_keys(value, f"{prefix}{key}.")
    elif isinstance(obj, list | tuple):
        for idx, item in enumerate(obj):
            assert_no_forbidden_keys(item, f"{prefix}[{idx}].")


def _strict_int(value: object, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"盲输出计数 {name} 必须是不少于 {minimum} 的内置整数")
    if maximum is not None and value > maximum:
        raise ValueError(f"盲输出计数 {name} 超出允许上限（上限 {maximum}）")
    return int(value)


def _strict_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"盲输出布尔值 {name} 必须是内置 bool")
    return bool(value)


def _strict_finite(value: object, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"原始统计量 {name} 必须是有限的内置数字")
    return float(value)


def _strict_utc_timestamp(
    value: object, name: str, *, lower: datetime, upper: datetime
) -> tuple[datetime, str]:
    if type(value) is not str or not _ISO_UTC.match(value):
        raise ValueError(f"BlindMetrics.quality.{name} 必须是内置 ISO-8601 UTC 时间字符串")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"BlindMetrics.quality.{name} 不是合法的日历时间") from None
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ValueError(f"BlindMetrics.quality.{name} 必须是 UTC 时区")
    dt = dt.astimezone(UTC)
    if dt < lower or dt > upper:
        raise ValueError(
            f"BlindMetrics.quality.{name} 超出允许窗口 [{lower.isoformat()}, {upper.isoformat()}]"
        )
    return dt, dt.isoformat(timespec="seconds")


def _window_limits(upper: datetime) -> tuple[int, int, int]:
    max_days = max(1, (upper - BTC_ETH_DAILY_WINDOW_START).days + 2)
    max_hourly = max(1, int((upper - BTC_ETH_HOURLY_WINDOW_START).total_seconds() // 3600) + 2)
    max_raw_bars = max_days * 24 * 3
    return max_days, max_hourly, max_raw_bars


def _clean_quality(raw_quality: object, *, upper: datetime) -> dict[str, int | str]:
    if type(raw_quality) is not dict:
        raise ValueError("BlindMetrics.quality 必须是内置字典")
    allowed = set(QUALITY_INT_KEYS) | set(QUALITY_TIME_KEYS)
    for k in raw_quality:
        if type(k) is not str:
            raise ValueError("BlindMetrics.quality 的键必须是内置字符串")
    extra = sorted(set(raw_quality) - allowed)
    if extra:
        raise ValueError(f"BlindMetrics.quality 含有未在白名单内的字段：{', '.join(extra)}")
    max_days, max_hourly, max_raw_bars = _window_limits(upper)
    cleaned: dict[str, int | str] = {}
    for key in QUALITY_DAILY_INT_KEYS:
        if key not in raw_quality:
            raise ValueError(f"BlindMetrics.quality 缺少计数字段：{key}")
        cleaned[key] = _strict_int(raw_quality[key], key, minimum=0, maximum=max_days)
    for key in QUALITY_HOURLY_INT_KEYS:
        if key not in raw_quality:
            raise ValueError(f"BlindMetrics.quality 缺少计数字段：{key}")
        cleaned[key] = _strict_int(raw_quality[key], key, minimum=0, maximum=max_hourly)
    for key in QUALITY_RAW_INT_KEYS:
        if key not in raw_quality:
            raise ValueError(f"BlindMetrics.quality 缺少计数字段：{key}")
        cleaned[key] = _strict_int(raw_quality[key], key, minimum=0, maximum=max_raw_bars)

    parsed_times: dict[str, datetime] = {}
    bounds = {
        "daily_first": (BTC_ETH_DAILY_WINDOW_START, upper),
        "daily_last": (BTC_ETH_DAILY_WINDOW_START, upper),
        "hourly_first": (BTC_ETH_HOURLY_WINDOW_START, upper),
        "hourly_last": (BTC_ETH_HOURLY_WINDOW_START, upper),
    }
    for key in QUALITY_TIME_KEYS:
        lo, hi = bounds[key]
        dt, canonical_iso = _strict_utc_timestamp(raw_quality.get(key), key, lower=lo, upper=hi)
        parsed_times[key] = dt
        cleaned[key] = canonical_iso
    if parsed_times["daily_first"] > parsed_times["daily_last"]:
        raise ValueError("BlindMetrics.quality.daily_first 不能晚于 daily_last")
    if parsed_times["hourly_first"] > parsed_times["hourly_last"]:
        raise ValueError("BlindMetrics.quality.hourly_first 不能晚于 hourly_last")
    return cleaned


@dataclass(frozen=True)
class _ParsedBtcEthRef:
    tests: dict[str, tuple[float, float, int]]
    holm: dict[str, bool]
    criteria: dict[str, bool]
    supported: bool
    boot_reject: bool


def _parse_btc_eth_001_ref(ref_bytes: bytes) -> _ParsedBtcEthRef:
    try:
        lines = [line for line in ref_bytes.decode("utf-8").splitlines() if line.strip()]
    except UnicodeDecodeError as exc:
        raise IntegrityError("btc-eth-001 对照账本不是合法的 UTF-8") from exc
    if len(lines) != 1:
        raise IntegrityError(f"预期 btc-eth-001 对照账本有 1 条记录，实际有 {len(lines)} 条")
    try:
        record = json.loads(lines[0])
        ref_metrics = record["result"]["metrics"]
        ref_tests_raw = ref_metrics["tests"]
        ref_holm_raw = ref_metrics["holm"]
        ref_verdict = ref_metrics["verdict"]
        ref_criteria_raw = ref_verdict["criteria"]
        boot_p = float(ref_metrics["cost_gate"]["net_edge_bootstrap_p_value"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise IntegrityError("btc-eth-001 对照账本结构缺失或损坏") from exc
    if not math.isfinite(boot_p):
        raise IntegrityError("对照基线自助法 p 值不是有限数字")

    tests: dict[str, tuple[float, float, int]] = {}
    for name in BTC_ETH_TESTS:
        try:
            entry = ref_tests_raw[name]
            coef = float(entry["coefficient"])
            se = float(entry["standard_error"])
            obs = int(entry["observations"])
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrityError(f"对照基线缺少或损坏检验项 {name}") from exc
        if not (math.isfinite(coef) and math.isfinite(se)) or se <= 0.0 or obs <= 0:
            raise IntegrityError(f"对照基线 {name} 的标准误或样本量非法")
        tests[name] = (coef, se, obs)

    try:
        holm = {item["test"]: bool(item["reject"]) for item in ref_holm_raw}
        criteria = {k: bool(ref_criteria_raw[k]) for k in BTC_ETH_CRITERIA}
        supported = bool(ref_verdict["supported"])
    except (KeyError, TypeError) as exc:
        raise IntegrityError("对照基线缺少 holm / criteria / supported 字段") from exc
    if any(name not in holm for name in BTC_ETH_TESTS):
        raise IntegrityError("对照基线 holm 列表缺少预注册检验项")
    return _ParsedBtcEthRef(
        tests=tests,
        holm=holm,
        criteria=criteria,
        supported=supported,
        boot_reject=boot_p < 0.05,
    )


def compare_btc_eth_001(
    raw: BlindMetrics,
    ref_bytes: bytes,
    ref: InheritedRef,
    *,
    max_delta_se: float = DEFAULT_MAX_DELTA_SE,
    max_sample_diff_ratio: float = DEFAULT_MAX_SAMPLE_DIFF_RATIO,
    max_knowable_time: datetime | None = None,
) -> Outcome:
    """Blindly compare Gen 3 Binance btc-eth-001 estimates against pinned Gen 2 OKX results.

    Drops all raw Binance coefficients, SEs, t-statistics, p-values, R-squareds and returns,
    bounds quality counts and timestamps to the authorized window, clamps sample differences,
    and caps reported `delta_se` at `MAX_REPORTED_DELTA_SE`.
    """
    if type(raw) is not BlindMetrics:
        raise TypeError("盲复现研究必须返回内置 BlindMetrics")
    if max_delta_se <= 0 or not math.isfinite(max_delta_se):
        raise ValueError("max_delta_se 必须是正数")
    if not (0.0 < max_sample_diff_ratio <= 1.0):
        raise ValueError("max_sample_diff_ratio 必须在 (0, 1] 内")

    upper = BTC_ETH_DEFAULT_MAX_TIME if max_knowable_time is None else max_knowable_time
    if upper < BTC_ETH_HOURLY_WINDOW_START:
        raise ValueError("max_knowable_time 早于小时线窗口起点")
    max_days, max_hourly, _ = _window_limits(upper)

    parsed_ref = _parse_btc_eth_001_ref(ref_bytes)

    vals = raw.values
    if type(vals) is not dict:
        raise ValueError("BlindMetrics.values 必须是内置字典")
    cur_tests = vals.get("tests")
    cur_holm = vals.get("holm_reject")
    cur_criteria = vals.get("criteria")
    if not (type(cur_tests) is dict and type(cur_holm) is dict and type(cur_criteria) is dict):
        raise ValueError("BlindMetrics.values 缺少内置字典 tests / holm_reject / criteria")

    cur_supported = _strict_bool(vals.get("supported"), "supported")
    cur_boot_reject = _strict_bool(
        vals.get("cost_gate_bootstrap_reject"), "cost_gate_bootstrap_reject"
    )
    quality = _clean_quality(raw.quality, upper=upper)

    verdict_consistent = cur_supported == parsed_ref.supported
    criteria_consistent = {
        k: _strict_bool(cur_criteria.get(k), k) == parsed_ref.criteria[k] for k in BTC_ETH_CRITERIA
    }
    holm_consistent = {
        name: _strict_bool(cur_holm.get(name), name) == parsed_ref.holm[name]
        for name in BTC_ETH_TESTS
    }
    boot_consistent = cur_boot_reject == parsed_ref.boot_reject

    failures: list[str] = []
    if not verdict_consistent:
        failures.append("overall:FAIL_VERDICT_MISMATCH")
    for k, ok in criteria_consistent.items():
        if not ok:
            failures.append(f"criterion:{k}:FAIL_CRITERION_MISMATCH")
    if not boot_consistent:
        failures.append("cost_gate:FAIL_BOOTSTRAP_MISMATCH")

    test_summary: dict[str, dict[str, object]] = {}
    for name in BTC_ETH_TESTS:
        entry = cur_tests.get(name)
        if type(entry) is not dict:
            raise ValueError(f"BlindMetrics.values.tests 缺少内置字典 {name}")
        cur_coef = _strict_finite(entry.get("coefficient"), f"{name}.coefficient")
        obs_cap = max_hourly if name.startswith("hourly") else max_days
        cur_obs = _strict_int(
            entry.get("observations"), f"{name}.observations", minimum=1, maximum=obs_cap
        )
        ref_coef, ref_se, ref_obs = parsed_ref.tests[name]

        exact_delta = abs(cur_coef - ref_coef) / ref_se
        within_bar = exact_delta <= max_delta_se
        reported_delta = min(round(exact_delta, 2), MAX_REPORTED_DELTA_SE)
        delta_capped = exact_delta > MAX_REPORTED_DELTA_SE

        raw_diff = cur_obs - ref_obs
        cov_ratio = abs(raw_diff) / ref_obs
        cov_ok = cov_ratio <= max_sample_diff_ratio
        max_diff = max(1, math.ceil(ref_obs * max_sample_diff_ratio))
        clamped_diff = max(-max_diff, min(max_diff, raw_diff))
        diff_capped = abs(raw_diff) > max_diff

        if not holm_consistent[name]:
            failures.append(f"{name}:FAIL_HOLM_MISMATCH")
        if not within_bar:
            failures.append(f"{name}:FAIL_DELTA_SE_THRESHOLD")
        if not cov_ok:
            failures.append(f"{name}:FAIL_SAMPLE_COVERAGE")
        test_summary[name] = {
            "holm_consistent": holm_consistent[name],
            "within_threshold": within_bar,
            "delta_se": reported_delta,
            "delta_se_capped": delta_capped,
            "observations_diff": clamped_diff,
            "observations_diff_capped": diff_capped,
            "coverage_ok": cov_ok,
        }

    failures.sort()
    verdict = "一致" if not failures else "不一致"
    metrics = {
        "overall_verdict": verdict,
        "verdict_consistent": verdict_consistent,
        "criteria_consistent": criteria_consistent,
        "holm_consistent": holm_consistent,
        "cost_gate_bootstrap_consistent": boot_consistent,
        "max_delta_se_threshold": round(max_delta_se, 2),
        "max_sample_diff_ratio_threshold": round(max_sample_diff_ratio, 4),
        "tests": test_summary,
        "panel_quality": quality,
        "failure_reasons": failures,
        "reference": {
            "source": ref.source,
            "commit": ref.commit,
            "path": ref.path,
            "sha256": ref.sha256,
        },
    }
    assert_no_forbidden_keys(metrics)

    table_lines = [
        "| 检验项 | Holm 一致 | 无符号 ΔSE（封顶 9.99） | ≤ 门槛 | 样本差（截断） | 覆盖达标 |",
        "|---|---|---|---|---|---|",
    ]
    for name in BTC_ETH_TESTS:
        row = test_summary[name]
        delta_val = float(row["delta_se"])  # type: ignore[arg-type]
        delta_txt = (
            f"≥{MAX_REPORTED_DELTA_SE:.2f}" if row["delta_se_capped"] else f"{delta_val:.2f}"
        )
        diff_val = int(row["observations_diff"])  # type: ignore[arg-type]
        diff_txt = (
            f"{diff_val:+d}（超限截断）" if row["observations_diff_capped"] else f"{diff_val:+d}"
        )
        table_lines.append(
            f"| `{name}` | {'是' if row['holm_consistent'] else '否'} | "
            f"{delta_txt} | {'是' if row['within_threshold'] else '否'} | "
            f"{diff_txt} | {'是' if row['coverage_ok'] else '否'} |"
        )
    criteria_lines = [
        f"- `{k}`：{'一致' if criteria_consistent[k] else '不一致'}" for k in BTC_ETH_CRITERIA
    ]
    fail_text = "无" if not failures else "、".join(f"`{f}`" for f in failures)
    q = quality
    sections = {
        "结论": (
            f"总体判定：**{verdict}**。\n\n"
            f"- 总体结论是否与第二代一致：{'是' if verdict_consistent else '否'}\n"
            f"- 成本闸自助法（5% 显著性）是否一致：{'是' if boot_consistent else '否'}\n"
            f"- 未通过项：{fail_text}\n\n"
            "本报告遵守盲复现规程（决策 0031）：不输出 Binance 确认集、"
            "金库、前向时段的任何原始收益率、回归系数、标准误、t 值或 p 值；"
            "计数与时间均经内置类型与窗口上限约束，ΔSE 封顶至 9.99，样本差截断至允许范围。"
        ),
        "假说与机制": (
            "迁移验收 A 的另一半（代际传承第 4 节）：在第三代框架内用 Binance "
            "U 本位永续合约 1 小时 K 线盲复现第二代 `myquant` 的 `btc-eth-001`"
            "（BTC/ETH/SOL 跨资产日频与小时频传导，原结论为否定）。"
            "`crypto.cross-asset-transmission` 属于前代已用掉的假说族。"
        ),
        "数据": (
            "- 数据集：`um-klines-1h`（BTCUSDT、ETHUSDT、SOLUSDT）\n"
            f"- 日线收益范围：{q['daily_first']} 至 {q['daily_last']}"
            f"（有效 {q['daily_rows']} 行；丢弃不完整日 "
            f"{q['daily_dropped_incomplete_days']}、缺收盘行 "
            f"{q['daily_dropped_close_rows']}、缺收益行 "
            f"{q['daily_dropped_return_rows']}、跨缺口滞后对 "
            f"{q['daily_dropped_lag_pairs']}）\n"
            f"- 小时线收益范围：{q['hourly_first']} 至 {q['hourly_last']}"
            f"（有效 {q['hourly_rows']} 行；缺收盘行 "
            f"{q['hourly_dropped_close_rows']}、缺收益行 "
            f"{q['hourly_dropped_return_rows']}、跨缺口滞后对 "
            f"{q['hourly_dropped_lag_pairs']}）\n"
            f"- 脏数据剔除：重复 K 线 {q['duplicate_bars']} 条、"
            f"非正或缺失收盘价 {q['invalid_close_bars']} 条\n"
            f"- 第二代冻结对照：`{ref.source}@{ref.commit[:12]}:{ref.path}`"
            f"（SHA-256 `{ref.sha256}`）"
        ),
        "方法": (
            "按 `myquant` 含 `run_trial.py` 的最早公开提交 `e992b10` 逐项对齐："
            "16:00 UTC 开盘锚定日线（24 根完整 1h K 线聚合、不前向填充）；"
            "逐品种计算严格相邻对数收益后内连接；Householder QR 最小二乘 + "
            "固定滞后 Bartlett 核 Newey–West HAC（日频 5、小时频 12，无小样本自由度放大，"
            "双侧正态 p 值）；Holm 3 组多重校正；带清洗和 1 天禁运期的向前滚动样本外评估；"
            "0.10% 往返吃单成本闸与循环分块自助法均值检验。"
            f"一致门槛：全部结论一致、3 组无符号 ΔSE ≤ {max_delta_se:.2f}"
            "（分母为冻结第二代标准误）、"
            f"样本量差 ≤ {max_sample_diff_ratio:.0%}。"
        ),
        "结果": "\n".join([*table_lines, "", "四条预注册准则一致性：", *criteria_lines]),
        "边际贡献": "不适用：本试验为前代已否定假说族的框架迁移盲复现，不涉及组合边际贡献。",
        "成本与容量": (
            "不适用：沿用第二代预注册的 0.05% 单边 / 0.10% 翻仓吃单成本闸，仅核对定性结论一致性。"
        ),
        "稳健性": (
            "小时频 `hourly_btc_eth` 已纳入三组 Holm 检验族与 ΔSE 核对；"
            "跨交易所（OKX 对比 Binance）本身即为稳健性检验。"
        ),
        "适用范围与停止条件": (
            "仅用于 G2 迁移验收 A 与一次性金库批准通道的端到端检验；"
            "本族已烧毁且原结论为否定，不进入后续实盘或确认集流程。"
        ),
    }
    if tuple(sections) != SECTIONS:
        raise RuntimeError("盲复现报告章节与 SECTIONS 定义不一致")
    return Outcome(metrics=metrics, sections=sections, verdict=verdict)


COMPARATOR_SPECS: dict[str, ComparatorSpec] = {
    "btc_eth_001": ComparatorSpec(
        comparator=compare_btc_eth_001,
        ref_validator=_parse_btc_eth_001_ref,
        required_dataset="um-klines-1h",
        required_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
        min_start_month="2023-01",
        min_end_month="2026-11",
        min_tests=3,
        allowed_params=frozenset(
            {
                "required_dataset",
                "required_symbols",
                "required_start_month",
                "required_end_month",
                "max_delta_se",
                "max_sample_diff_ratio",
            }
        ),
    ),
}

COMPARATORS: dict[str, Callable[..., Outcome]] = {
    name: spec.comparator for name, spec in COMPARATOR_SPECS.items()
}


def validate_params(
    study: str,
    params: object,
    *,
    max_knowable_time: datetime | None = None,
) -> list[str]:
    """Validate blind-replication params statically (gate) and pre-flight (before approval)."""
    spec = COMPARATOR_SPECS.get(study)
    if spec is None:
        return [f"没有为研究 {study!r} 登记盲复现比对器"]
    if type(params) is not dict:
        return ["盲复现试验卡的 params 必须是 JSON 对象"]

    found: list[str] = []
    extra = sorted(k for k in params if not isinstance(k, str) or k not in spec.allowed_params)
    if extra:
        found.append(f"盲复现 params 含有未登记的字段：{', '.join(str(k) for k in extra)}")

    dataset = params.get("required_dataset")
    if dataset != spec.required_dataset:
        found.append(f"盲复现必须填写 required_dataset = {spec.required_dataset!r}")

    symbols = params.get("required_symbols")
    if (
        not isinstance(symbols, list)
        or not symbols
        or not all(type(s) is str and s for s in symbols)
        or not set(spec.required_symbols) <= set(symbols)
    ):
        need = "、".join(spec.required_symbols)
        found.append(f"盲复现必须填写 required_symbols（至少包含 {need}）")

    start_month = params.get("required_start_month")
    end_month = params.get("required_end_month")
    if (
        type(start_month) is not str
        or type(end_month) is not str
        or _MONTH_RE.match(start_month) is None
        or _MONTH_RE.match(end_month) is None
        or start_month > end_month
    ):
        found.append(
            "盲复现必须填写合法的 required_start_month 和 required_end_month（YYYY-MM，且起 ≤ 止）"
        )
    else:
        if start_month > spec.min_start_month or end_month < spec.min_end_month:
            found.append(
                f"盲复现前置就绪检查月份须至少覆盖 {spec.min_start_month} 至 {spec.min_end_month}"
            )
        if max_knowable_time is not None:
            kt_month = f"{max_knowable_time.year:04d}-{max_knowable_time.month:02d}"
            if kt_month < end_month:
                found.append(
                    f"盲复现 max_knowable_time 所在月份 ({kt_month}) "
                    f"早于 required_end_month ({end_month})"
                )

    if "max_delta_se" in params:
        v = params["max_delta_se"]
        if (
            type(v) not in (int, float)
            or not math.isfinite(v)
            or not (0.0 < float(v) <= spec.max_delta_se)
        ):
            found.append(f"max_delta_se 必须在 (0, {spec.max_delta_se}] 内（只能比默认更严）")

    if "max_sample_diff_ratio" in params:
        v = params["max_sample_diff_ratio"]
        if (
            type(v) not in (int, float)
            or not math.isfinite(v)
            or not (0.0 < float(v) <= spec.max_sample_diff_ratio)
        ):
            found.append(
                f"max_sample_diff_ratio 必须在 (0, {spec.max_sample_diff_ratio}] 内"
                "（只能比默认更严）"
            )
    return found


def validate_reference_bytes(study: str, ref_bytes: bytes) -> None:
    """Validate the downloaded/cached Gen 2 reference payload before consuming approval."""
    spec = COMPARATOR_SPECS.get(study)
    if spec is None:
        raise ValueError(f"没有为研究 {study!r} 登记盲复现比对器")
    spec.ref_validator(ref_bytes)


def compare(
    study: str,
    raw: BlindMetrics,
    ref_bytes: bytes,
    ref: InheritedRef,
    params: dict,
    *,
    max_knowable_time: datetime | None = None,
) -> Outcome:
    spec = COMPARATOR_SPECS.get(study)
    if spec is None:
        raise ValueError(f"没有为研究 {study!r} 登记盲复现比对器")
    if problems := validate_params(study, params, max_knowable_time=max_knowable_time):
        raise ValueError("；".join(problems))
    max_delta_se = _strict_finite(params.get("max_delta_se", spec.max_delta_se), "max_delta_se")
    max_sample_diff = _strict_finite(
        params.get("max_sample_diff_ratio", spec.max_sample_diff_ratio),
        "max_sample_diff_ratio",
    )
    return spec.comparator(
        raw,
        ref_bytes,
        ref,
        max_delta_se=max_delta_se,
        max_sample_diff_ratio=max_sample_diff,
        max_knowable_time=max_knowable_time,
    )
