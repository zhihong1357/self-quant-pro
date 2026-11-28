"""Framework-side blind comparison for burned-family replications (decision 0031).

This module lives outside `src/sqp/research/studies/` and is NOT on `research-boundary`'s
allow-list, so study code can neither import it nor read inherited reference files.
The trial runner:
1. resolves the pinned upstream reference from `origin/main:registry/inherited/manifest.json`;
2. loads its bytes from private bucket cache (`raw/gen2/...`) or pinned GitHub raw URL and
   checks exact byte length and SHA-256 against the frozen manifest;
3. feeds the study's in-memory `BlindMetrics` into the comparator here;
4. discards all raw third-party estimates from guarded segments and emits only allowlisted
   blind comparison outputs (`一致` / `不一致`, unsigned `delta_se`, counts, fingerprints).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass

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

BTC_ETH_TESTS = ("daily_btc_eth", "daily_btc_sol", "hourly_btc_eth")
BTC_ETH_CRITERIA = (
    "holm_adjusted_primary_p_below_0.05",
    "primary_partial_r_squared_at_least_0.005",
    "holdout_oos_incremental_r_squared_above_0",
    "net_daily_edge_after_costs_positive",
)
QUALITY_INT_KEYS = (
    "daily_rows",
    "daily_dropped_incomplete_days",
    "daily_dropped_close_rows",
    "daily_dropped_return_rows",
    "daily_dropped_lag_pairs",
    "hourly_rows",
    "hourly_dropped_close_rows",
    "hourly_dropped_return_rows",
    "hourly_dropped_lag_pairs",
    "duplicate_bars",
    "invalid_close_bars",
)
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


def raw_cache_key(ref: InheritedRef) -> str:
    return f"{RAW_GEN2_PREFIX}{ref.source}/{ref.commit}/{ref.path}"


def _valid_relpath(path: object) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def manifest_entry(
    read: Callable[[str], str | None], source: object, path: object
) -> InheritedRef | None:
    """Resolve a pinned upstream inherited file from `manifest.json` and `inheritance.toml`."""
    if not isinstance(source, str) or not source or not _valid_relpath(path):
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
                if (
                    isinstance(size, int)
                    and not isinstance(size, bool)
                    and size > 0
                    and isinstance(digest, str)
                    and _HEX64.match(digest)
                ):
                    return InheritedRef(source, url, commit, str(path), size, digest)
    return None


def raw_github_url(ref: InheritedRef) -> str:
    match = _GITHUB_REPO.match(ref.url)
    if match is None:
        raise IntegrityError(f"继承来源地址不是合法的 GitHub 仓库：{ref.url!r}")
    return f"https://raw.githubusercontent.com/{match.group(1)}/{ref.commit}/{ref.path}"


def load_reference_bytes(
    ref: InheritedRef,
    store: Store,
    *,
    fetch: Callable[[str], bytes] | None = None,
) -> bytes:
    """Load pinned Gen 2 reference bytes from store cache or GitHub and verify SHA-256."""
    cache_key = raw_cache_key(ref)
    if store.exists(cache_key):
        data = store.get(cache_key)
    else:
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
            if not isinstance(key, str):
                raise ValueError("盲输出字典的键必须是字符串")
            if key.lower() in FORBIDDEN_KEYS:
                raise ValueError(f"盲输出包含违禁字段：{prefix}{key}")
            assert_no_forbidden_keys(value, f"{prefix}{key}.")
    elif isinstance(obj, list | tuple):
        for idx, item in enumerate(obj):
            assert_no_forbidden_keys(item, f"{prefix}[{idx}].")


def _strict_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"盲输出计数 {name} 必须是非负整数")
    return value


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"盲输出布尔值 {name} 必须是 bool")
    return value


def _strict_finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"原始统计量 {name} 必须是有限数字")
    return float(value)


def _clean_quality(raw_quality: object) -> dict[str, int | str]:
    if not isinstance(raw_quality, dict):
        raise ValueError("BlindMetrics.quality 必须是字典")
    allowed = set(QUALITY_INT_KEYS) | set(QUALITY_TIME_KEYS)
    extra = sorted(set(raw_quality) - allowed)
    if extra:
        raise ValueError(f"BlindMetrics.quality 含有未在白名单内的字段：{', '.join(extra)}")
    cleaned: dict[str, int | str] = {}
    for key in QUALITY_INT_KEYS:
        if key not in raw_quality:
            raise ValueError(f"BlindMetrics.quality 缺少计数字段：{key}")
        cleaned[key] = _strict_int(raw_quality[key], key)
    for key in QUALITY_TIME_KEYS:
        val = raw_quality.get(key)
        if not isinstance(val, str) or not _ISO_UTC.match(val):
            raise ValueError(f"BlindMetrics.quality.{key} 必须是 ISO-8601 UTC 时间")
        cleaned[key] = val
    return cleaned


def compare_btc_eth_001(
    raw: BlindMetrics,
    ref_bytes: bytes,
    ref: InheritedRef,
    *,
    max_delta_se: float = 1.0,
    max_sample_diff_ratio: float = 0.05,
) -> Outcome:
    """Blindly compare Gen 3 Binance btc-eth-001 estimates against pinned Gen 2 OKX results.

    Drops all raw Binance coefficients, SEs, t-statistics, p-values, R-squareds and returns.
    """
    if not isinstance(raw, BlindMetrics):
        raise TypeError("盲复现研究必须返回 BlindMetrics")
    if max_delta_se <= 0 or not math.isfinite(max_delta_se):
        raise ValueError("max_delta_se 必须是正数")
    if not (0.0 < max_sample_diff_ratio <= 1.0):
        raise ValueError("max_sample_diff_ratio 必须在 (0, 1] 内")

    lines = [line for line in ref_bytes.decode("utf-8").splitlines() if line.strip()]
    if len(lines) != 1:
        raise IntegrityError(f"预期 btc-eth-001 对照账本有 1 条记录，实际有 {len(lines)} 条")
    ref_metrics = json.loads(lines[0])["result"]["metrics"]
    ref_tests = ref_metrics["tests"]
    ref_holm = {item["test"]: bool(item["reject"]) for item in ref_metrics["holm"]}
    ref_verdict = ref_metrics["verdict"]
    ref_criteria = {k: bool(ref_verdict["criteria"][k]) for k in BTC_ETH_CRITERIA}
    ref_supported = bool(ref_verdict["supported"])
    ref_boot_reject = float(ref_metrics["cost_gate"]["net_edge_bootstrap_p_value"]) < 0.05

    vals = raw.values
    if not isinstance(vals, dict):
        raise ValueError("BlindMetrics.values 必须是字典")
    cur_tests = vals.get("tests")
    cur_holm = vals.get("holm_reject")
    cur_criteria = vals.get("criteria")
    if not (
        isinstance(cur_tests, dict)
        and isinstance(cur_holm, dict)
        and isinstance(cur_criteria, dict)
    ):
        raise ValueError("BlindMetrics.values 缺少 tests / holm_reject / criteria")

    cur_supported = _strict_bool(vals.get("supported"), "supported")
    cur_boot_reject = _strict_bool(
        vals.get("cost_gate_bootstrap_reject"), "cost_gate_bootstrap_reject"
    )
    quality = _clean_quality(raw.quality)

    verdict_consistent = cur_supported == ref_supported
    criteria_consistent = {
        k: _strict_bool(cur_criteria.get(k), k) == ref_criteria[k] for k in BTC_ETH_CRITERIA
    }
    holm_consistent = {
        name: _strict_bool(cur_holm.get(name), name) == ref_holm[name] for name in BTC_ETH_TESTS
    }
    boot_consistent = cur_boot_reject == ref_boot_reject

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
        if not isinstance(entry, dict):
            raise ValueError(f"BlindMetrics.values.tests 缺少 {name}")
        cur_coef = _strict_finite(entry.get("coefficient"), f"{name}.coefficient")
        cur_obs = _strict_int(entry.get("observations"), f"{name}.observations")
        ref_coef = float(ref_tests[name]["coefficient"])
        ref_se = float(ref_tests[name]["standard_error"])
        ref_obs = int(ref_tests[name]["observations"])
        if ref_se <= 0.0 or ref_obs <= 0:
            raise IntegrityError(f"对照基线 {name} 的标准误或样本量非法")

        exact_delta = abs(cur_coef - ref_coef) / ref_se
        within_bar = exact_delta <= max_delta_se
        cov_ratio = abs(cur_obs - ref_obs) / ref_obs
        cov_ok = cov_ratio <= max_sample_diff_ratio
        if not holm_consistent[name]:
            failures.append(f"{name}:FAIL_HOLM_MISMATCH")
        if not within_bar:
            failures.append(f"{name}:FAIL_DELTA_SE_THRESHOLD")
        if not cov_ok:
            failures.append(f"{name}:FAIL_SAMPLE_COVERAGE")
        test_summary[name] = {
            "holm_consistent": holm_consistent[name],
            "within_threshold": within_bar,
            "delta_se": round(exact_delta, 2),
            "observations_binance": cur_obs,
            "observations_diff": cur_obs - ref_obs,
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
        "| 检验项 | Holm 一致 | 无符号 ΔSE | ≤ 门槛 | Binance 样本 | 样本差 | 覆盖达标 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name in BTC_ETH_TESTS:
        row = test_summary[name]
        table_lines.append(
            f"| `{name}` | {'是' if row['holm_consistent'] else '否'} | "
            f"{row['delta_se']:.2f} | {'是' if row['within_threshold'] else '否'} | "
            f"{row['observations_binance']} | {row['observations_diff']:+d} | "
            f"{'是' if row['coverage_ok'] else '否'} |"
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
            "本报告严格遵守盲复现规程（决策 0031）：不输出 Binance 确认集、"
            "金库、前向时段的任何原始收益率、回归系数、标准误、t 值或 p 值。"
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
            "按 `myquant@e992b10` 的实际实现逐项对齐：16:00 UTC 开盘锚定日线"
            "（24 根完整 1h K 线聚合、不前向填充）；逐品种计算严格相邻对数收益后"
            "内连接；Householder QR 最小二乘 + 固定滞后 Bartlett 核 Newey–West HAC"
            "（日频 5、小时频 12，无小样本自由度放大，双侧正态 p 值）；"
            "Holm 3 组多重校正；带清洗和 1 天禁运期的向前滚动样本外评估；"
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
    assert tuple(sections) == SECTIONS
    return Outcome(metrics=metrics, sections=sections, verdict=verdict)


COMPARATORS: dict[str, Callable[..., Outcome]] = {
    "btc_eth_001": compare_btc_eth_001,
}


DEFAULT_MAX_DELTA_SE = 1.0
DEFAULT_MAX_SAMPLE_DIFF_RATIO = 0.05


def compare(
    study: str,
    raw: BlindMetrics,
    ref_bytes: bytes,
    ref: InheritedRef,
    params: dict,
) -> Outcome:
    comparator = COMPARATORS.get(study)
    if comparator is None:
        raise ValueError(f"没有为研究 {study!r} 登记盲复现比对器")
    max_delta_se = _strict_finite(params.get("max_delta_se", DEFAULT_MAX_DELTA_SE), "max_delta_se")
    max_sample_diff = _strict_finite(
        params.get("max_sample_diff_ratio", DEFAULT_MAX_SAMPLE_DIFF_RATIO),
        "max_sample_diff_ratio",
    )
    if max_delta_se > DEFAULT_MAX_DELTA_SE:
        raise ValueError(f"max_delta_se 只能比默认更严（不能超过 {DEFAULT_MAX_DELTA_SE}）")
    if max_sample_diff > DEFAULT_MAX_SAMPLE_DIFF_RATIO:
        raise ValueError(
            f"max_sample_diff_ratio 只能比默认更严（不能超过 {DEFAULT_MAX_SAMPLE_DIFF_RATIO}）"
        )
    return comparator(
        raw,
        ref_bytes,
        ref,
        max_delta_se=max_delta_se,
        max_sample_diff_ratio=max_sample_diff,
    )
