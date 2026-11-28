"""One-time vault approvals, four-lock blind replication, stats and zero-leakage tests (0031)."""

from __future__ import annotations

import hashlib
import io
import json
import math
import random
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sqp import ledger
from sqp.data import build, vision
from sqp.data.pit import (
    APPROVALS,
    AccessRefused,
    IntegrityError,
    PointInTime,
    approval_used_key,
    dataset_window_ready,
)
from sqp.research import blind, run, trials
from sqp.research.studies import BlindMetrics
from sqp.stats import (
    circular_block_bootstrap_mean_test,
    evaluate_walk_forward_forecasts,
    holm_adjust,
    newey_west_hac,
    ols_fit,
    partial_lead_lag,
    purged_walk_forward_splits,
)
from sqp.storage import LocalStore

ROOT = Path(__file__).resolve().parents[1]
UM = "data/futures/um/monthly/klines/"
DATASET = "um-klines-1h"
INHERITED_SPEC = {"source": "myquant", "path": "research/btc-eth-001/trials.jsonl"}
MAX_KT = "2026-11-18T00:00:00Z"

HYPOTHESIS_TEMPLATE = """id = "{hid}"
title = "盲复现测试"
family = "{family}"
universe = "U1"
status = "registered"
created = 2026-11-27
mechanism = "迁移验收测试"
predictions = ["与第二代结论一致"]
falsification = "超出 1.0 标准误或结论反转"

[registration]
registered_at = 2026-11-27
segments = {segments}
min_effect_sharpe = 2.9
test_plan = "盲复现"
literature_exposure = "前代已公开"

[thresholds]
t_stat = 3.0
deflated_sharpe = 0.95
pbo = 0.10
cost_multiplier = 2.0
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _merge(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "merge", "--allow-empty")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")


def _ledger_text(*records: dict) -> str:
    text = ""
    for record in records:
        text += ledger.canonical(ledger.build(text, record)).decode() + "\n"
    return text


def _synthetic_ref_bytes(
    *,
    beta_eth: float = 0.01,
    se_eth: float = 0.02,
    obs_eth: int = 1000,
    beta_sol: float = -0.005,
    se_sol: float = 0.03,
    obs_sol: int = 1000,
    beta_h: float = 0.002,
    se_h: float = 0.01,
    obs_h: int = 2000,
    supported: bool = False,
    boot_p: float = 0.45,
) -> bytes:
    rec = {
        "trial_id": "T0001",
        "hypothesis_id": "H0001",
        "status": "completed",
        "result": {
            "metrics": {
                "tests": {
                    "daily_btc_eth": {
                        "coefficient": beta_eth,
                        "standard_error": se_eth,
                        "observations": obs_eth,
                        "p_value": 0.6,
                    },
                    "daily_btc_sol": {
                        "coefficient": beta_sol,
                        "standard_error": se_sol,
                        "observations": obs_sol,
                        "p_value": 0.85,
                    },
                    "hourly_btc_eth": {
                        "coefficient": beta_h,
                        "standard_error": se_h,
                        "observations": obs_h,
                        "p_value": 0.84,
                    },
                },
                "holm": [
                    {
                        "test": "daily_btc_eth",
                        "raw_p_value": 0.6,
                        "adjusted_p_value": 1.0,
                        "reject": False,
                    },
                    {
                        "test": "daily_btc_sol",
                        "raw_p_value": 0.85,
                        "adjusted_p_value": 1.0,
                        "reject": False,
                    },
                    {
                        "test": "hourly_btc_eth",
                        "raw_p_value": 0.84,
                        "adjusted_p_value": 1.0,
                        "reject": False,
                    },
                ],
                "cost_gate": {"net_edge_bootstrap_p_value": boot_p, "net_daily_edge": -0.0005},
                "verdict": {
                    "supported": supported,
                    "criteria": {
                        "holm_adjusted_primary_p_below_0.05": False,
                        "primary_partial_r_squared_at_least_0.005": False,
                        "holdout_oos_incremental_r_squared_above_0": False,
                        "net_daily_edge_after_costs_positive": False,
                    },
                },
            }
        },
    }
    return (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8")


def _sample_quality() -> dict[str, int | str]:
    return {
        "daily_rows": 1001,
        "daily_dropped_incomplete_days": 0,
        "daily_dropped_close_rows": 0,
        "daily_dropped_return_rows": 1,
        "daily_dropped_lag_pairs": 0,
        "hourly_rows": 2001,
        "hourly_dropped_close_rows": 0,
        "hourly_dropped_return_rows": 1,
        "hourly_dropped_lag_pairs": 0,
        "duplicate_bars": 0,
        "invalid_close_bars": 0,
        "daily_first": "2023-01-02T16:00:00+00:00",
        "daily_last": "2026-11-16T16:00:00+00:00",
        "hourly_first": "2026-08-20T01:00:00+00:00",
        "hourly_last": "2026-11-17T23:00:00+00:00",
    }


def _sample_raw_metrics(
    *,
    beta_eth: float = 0.018,
    obs_eth: int = 1000,
    beta_sol: float = -0.005,
    obs_sol: int = 1000,
    beta_h: float = 0.002,
    obs_h: int = 2000,
    supported: bool = False,
    holm_eth: bool = False,
    boot_reject: bool = False,
    quality: dict | None = None,
) -> BlindMetrics:
    return BlindMetrics(
        values={
            "supported": supported,
            "cost_gate_bootstrap_reject": boot_reject,
            "criteria": {
                "holm_adjusted_primary_p_below_0.05": holm_eth,
                "primary_partial_r_squared_at_least_0.005": False,
                "holdout_oos_incremental_r_squared_above_0": False,
                "net_daily_edge_after_costs_positive": False,
            },
            "holm_reject": {
                "daily_btc_eth": holm_eth,
                "daily_btc_sol": False,
                "hourly_btc_eth": False,
            },
            "tests": {
                "daily_btc_eth": {"coefficient": beta_eth, "observations": obs_eth},
                "daily_btc_sol": {"coefficient": beta_sol, "observations": obs_sol},
                "hourly_btc_eth": {"coefficient": beta_h, "observations": obs_h},
            },
        },
        quality=_sample_quality() if quality is None else quality,
    )


def _make_manifest(ref_bytes: bytes) -> str:
    commit = "feb6a07b7a03bbfe7a6c2fccf2b28c1c97b537a7"
    doc = {
        "generated_by": "test",
        "sources": [
            {
                "name": "myquant",
                "commit": commit,
                "files": [
                    {
                        "path": INHERITED_SPEC["path"],
                        "bytes": len(ref_bytes),
                        "lines": 1,
                        "sha256": hashlib.sha256(ref_bytes).hexdigest(),
                    }
                ],
            }
        ],
    }
    return json.dumps(doc, sort_keys=True)


def _zipped_bars(symbol: str, start: datetime, n: int) -> bytes:
    start_ms = int(start.timestamp() * 1000)
    hour = 3_600_000
    rows = "".join(
        f"{start_ms + i * hour},100,101,99,100.5,10,"
        f"{start_ms + (i + 1) * hour - 1},1000,5,5,500,0\n"
        for i in range(n)
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{symbol}.csv", rows)
    return buf.getvalue()


def _build_lake(
    tmp_path: Path, months: dict[str, datetime], symbols: tuple[str, ...]
) -> LocalStore:
    store = LocalStore(tmp_path / "lake")
    source = vision.Source(
        site="https://site.test/",
        listing="https://list.test/bucket",
        symbol_sets={},
        datasets=[{"id": DATASET, "kind": "klines", "prefix": UM + "{symbol}/1h/"}],
    )
    for month, start in months.items():
        for sym in symbols:
            key = f"{vision.RAW_PREFIX}{UM}{sym}/1h/{sym}-1h-{month}.zip"
            store.put(key, _zipped_bars(sym, start, 2))
    build.build(source, lambda: store, room=10**9)
    return store


BLIND_STUDY_CODE = """
import sys
from datetime import UTC, datetime

from sqp.research.studies import SECTIONS, BlindMetrics, Outcome

CALLS = []


def run(pit, params):
    CALLS.append(1)
    behavior = params.get("behavior", "ok")
    if behavior == "print_and_ok":
        print("LEAK_BETA=0.98765")
        sys.stderr.write("LEAK_SE=0.01234\\n")
    if behavior == "crash":
        print("LEAK_BEFORE_CRASH=3.14159")
        raise ValueError("LEAK_BETA=9.99")
    if behavior == "return_outcome":
        return Outcome(metrics={"x": 1}, sections={s: "t" for s in SECTIONS})
    if behavior == "past_limit":
        pit.table("um-klines-1h", end=datetime(2026, 11, 18, 1, tzinfo=UTC))
    # Touch exploration, confirmation, vault and forward up to max_knowable_time.
    pit.table(
        "um-klines-1h",
        start=datetime(2024, 1, 31, 22, tzinfo=UTC),
        end=datetime(2026, 11, 18, 0, tzinfo=UTC),
    )
    quality = {
        "daily_rows": 1001,
        "daily_dropped_incomplete_days": 0,
        "daily_dropped_close_rows": 0,
        "daily_dropped_return_rows": 1,
        "daily_dropped_lag_pairs": 0,
        "hourly_rows": 2001,
        "hourly_dropped_close_rows": 0,
        "hourly_dropped_return_rows": 1,
        "hourly_dropped_lag_pairs": 0,
        "duplicate_bars": 0,
        "invalid_close_bars": 0,
        "daily_first": "2023-01-02T16:00:00+00:00",
        "daily_last": "2026-11-16T16:00:00+00:00",
        "hourly_first": "2026-08-20T01:00:00+00:00",
        "hourly_last": "2026-11-17T23:00:00+00:00",
    }
    if behavior == "bad_quality":
        quality["secret_sharpe"] = 3.14
    return BlindMetrics(
        values={
            "supported": False,
            "cost_gate_bootstrap_reject": False,
            "criteria": {
                "holm_adjusted_primary_p_below_0.05": False,
                "primary_partial_r_squared_at_least_0.005": False,
                "holdout_oos_incremental_r_squared_above_0": False,
                "net_daily_edge_after_costs_positive": False,
            },
            "holm_reject": {
                "daily_btc_eth": False,
                "daily_btc_sol": False,
                "hourly_btc_eth": False,
            },
            "tests": {
                "daily_btc_eth": {
                    "coefficient": params.get("beta_eth", 0.014),
                    "observations": 1000,
                },
                "daily_btc_sol": {"coefficient": -0.005, "observations": 1000},
                "hourly_btc_eth": {"coefficient": 0.002, "observations": 2000},
            },
        },
        quality=quality,
    )
"""


@pytest.fixture
def blind_studies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    pkg = tmp_path / "blindpkg" / "bstudies"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "btc_eth_001.py").write_text(BLIND_STUDY_CODE)
    monkeypatch.syspath_prepend(str(tmp_path / "blindpkg"))
    yield "bstudies"
    for name in [m for m in sys.modules if m.startswith("bstudies")]:
        del sys.modules[name]


@pytest.fixture
def blind_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A git repo + local store set up for end-to-end blind replication tests."""
    ref_bytes = _synthetic_ref_bytes()
    root = tmp_path / "repo"
    for sub in (
        "config",
        "registry/universes",
        "registry/ledger",
        "registry/hypotheses",
        "registry/inherited",
        "src/sqp/research/studies",
    ):
        (root / sub).mkdir(parents=True)
    shutil.copy(ROOT / "registry/universes/U1.toml", root / "registry/universes/U1.toml")
    shutil.copy(ROOT / "config/inheritance.toml", root / "config/inheritance.toml")
    shutil.copy(ROOT / trials.INHERITED, root / trials.INHERITED)
    (root / blind.MANIFEST_PATH).write_text(_make_manifest(ref_bytes))
    (root / "registry/hypotheses/H-0010.toml").write_text(
        HYPOTHESIS_TEMPLATE.format(
            hid="H-0010",
            family="crypto.cross-asset-transmission",
            segments='["vault"]',
        )
    )
    (root / "registry/hypotheses/H-0011.toml").write_text(
        HYPOTHESIS_TEMPLATE.format(
            hid="H-0011",
            family="crypto.fresh-family",
            segments='["vault"]',
        )
    )
    (root / "src/sqp/research/studies/btc_eth_001.py").write_text("x = 1\n")
    for name in ("approvals.jsonl", "refusals.jsonl", "trials.jsonl"):
        (root / "registry/ledger" / name).write_text("")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    _merge(root)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")

    store = _build_lake(
        tmp_path,
        months={
            "2024-01": datetime(2024, 1, 31, 22, tzinfo=UTC),
            "2024-02": datetime(2024, 2, 1, 0, tzinfo=UTC),
            "2025-11": datetime(2025, 11, 1, 0, tzinfo=UTC),
            "2026-11": datetime(2026, 11, 17, 22, tzinfo=UTC),
        },
        symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
    )
    ref = blind.manifest_entry(lambda p: (root / p).read_text(), "myquant", INHERITED_SPEC["path"])
    assert ref is not None
    store.put(blind.raw_cache_key(ref), ref_bytes)
    return root, store, ref, ref_bytes


def _append_blind_pair(
    root: Path,
    *,
    trial_tid: str = "T-0001",
    hypothesis: str = "H-0010",
    max_kt: str = MAX_KT,
    params: dict | None = None,
) -> int:
    ap = ledger.append(
        root / APPROVALS,
        {
            "type": "vault",
            "universe": "U1",
            "hypothesis": hypothesis,
            "trial": trial_tid,
            "mode": "blind_replication",
            "study": "btc_eth_001",
            "max_knowable_time": max_kt,
            "inherited": INHERITED_SPEC,
        },
    )
    ledger.append(
        root / trials.TRIALS,
        {
            "type": "trial",
            "hypothesis": hypothesis,
            "study": "btc_eth_001",
            "segment": "vault",
            "mode": "blind_replication",
            "approval": ap["seq"],
            "max_knowable_time": max_kt,
            "inherited": INHERITED_SPEC,
            "tests": 3,
            "params": params or {},
            "note": "盲复现测试",
        },
    )
    _merge(root)
    return int(ap["seq"])


# --- 1. Static checks: one-time vault approvals, burned families & four locks ---


def test_static_vault_approval_uniqueness_and_binding(sandbox):
    sandbox.write("registry/universes/U1.toml", (ROOT / "registry/universes/U1.toml").read_text())
    sandbox.write("config/inheritance.toml", (ROOT / "config/inheritance.toml").read_text())
    sandbox.write(blind.MANIFEST_PATH, _make_manifest(_synthetic_ref_bytes()))
    sandbox.write(
        "registry/hypotheses/H-0010.toml",
        HYPOTHESIS_TEMPLATE.format(
            hid="H-0010", family="crypto.cross-asset-transmission", segments='["vault"]'
        ),
    )
    sandbox.write(
        "registry/hypotheses/H-0011.toml",
        HYPOTHESIS_TEMPLATE.format(hid="H-0011", family="crypto.fresh", segments='["vault"]'),
    )
    sandbox.write("src/sqp/research/studies/btc_eth_001.py", "x = 1\n")
    sandbox.write(APPROVALS, "")
    sandbox.write(trials.TRIALS, "")
    base = sandbox.commit("base")

    # Duplicate vault approval for the same trial -> vault-approval violation.
    dup = _ledger_text(
        {"type": "vault", "universe": "U1", "hypothesis": "H-0011", "trial": "T-0001"},
        {"type": "vault", "universe": "U1", "hypothesis": "H-0011", "trial": "T-0001"},
    )
    sandbox.write(APPROVALS, dup)
    assert any(v.rule == "vault-approval" and "重复签发" in v.message for v in sandbox.check(base))

    # Two trials sharing one approval seq -> trial-card violation.
    sandbox.write(
        APPROVALS,
        _ledger_text(
            {"type": "vault", "universe": "U1", "hypothesis": "H-0011", "trial": "T-0001"}
        ),
    )
    t = {
        "type": "trial",
        "hypothesis": "H-0011",
        "study": "btc_eth_001",
        "segment": "vault",
        "approval": 1,
        "params": {},
        "note": "共享批准",
    }
    sandbox.write(trials.TRIALS, _ledger_text(t, t))
    assert any(
        v.rule == "trial-card" and "已被 T-0001 引用" in v.message for v in sandbox.check(base)
    )


def test_static_burned_family_guards_and_four_locks(sandbox):
    sandbox.write("registry/universes/U1.toml", (ROOT / "registry/universes/U1.toml").read_text())
    sandbox.write("config/inheritance.toml", (ROOT / "config/inheritance.toml").read_text())
    sandbox.write(blind.MANIFEST_PATH, _make_manifest(_synthetic_ref_bytes()))
    sandbox.write(
        "registry/hypotheses/H-0010.toml",
        HYPOTHESIS_TEMPLATE.format(
            hid="H-0010",
            family="crypto.cross-asset-transmission",
            segments='["confirmation", "vault"]',
        ),
    )
    sandbox.write(
        "registry/hypotheses/H-0011.toml",
        HYPOTHESIS_TEMPLATE.format(hid="H-0011", family="crypto.fresh", segments='["vault"]'),
    )
    sandbox.write(
        "registry/hypotheses/H-0012.toml",
        HYPOTHESIS_TEMPLATE.format(
            hid="H-0012", family="crypto.funding", segments='["confirmation", "vault"]'
        ),
    )
    sandbox.write("src/sqp/research/studies/btc_eth_001.py", "x = 1\n")
    sandbox.write(APPROVALS, "")
    sandbox.write(trials.TRIALS, "")
    base = sandbox.commit("base")

    # 1. Burned family cannot submit standard confirmation trial.
    std_burned = {
        "type": "trial",
        "hypothesis": "H-0010",
        "study": "btc_eth_001",
        "segment": "confirmation",
        "params": {},
        "note": "偷用确认集",
    }
    sandbox.write(trials.TRIALS, _ledger_text(std_burned))
    assert any("families_burned" in v.message for v in sandbox.check(base))

    # 2. vault_burned (crypto.funding) cannot submit standard vault trial; confirmation is ok.
    sandbox.write(
        APPROVALS,
        _ledger_text(
            {"type": "vault", "universe": "U1", "hypothesis": "H-0012", "trial": "T-0001"}
        ),
    )
    sandbox.write(
        trials.TRIALS,
        _ledger_text(
            {
                "type": "trial",
                "hypothesis": "H-0012",
                "study": "btc_eth_001",
                "segment": "vault",
                "approval": 1,
                "params": {},
                "note": "偷用金库",
            }
        ),
    )
    assert any("vault_burned" in v.message for v in sandbox.check(base))
    sandbox.write(APPROVALS, "")
    sandbox.write(
        trials.TRIALS,
        _ledger_text(
            {
                "type": "trial",
                "hypothesis": "H-0012",
                "study": "btc_eth_001",
                "segment": "confirmation",
                "params": {},
                "note": "资金费率确认集仍干净",
            }
        ),
    )
    assert not any(v.rule == "trial-card" for v in sandbox.check(base))

    # 3. Unburned family cannot use blind_replication.
    sandbox.write(
        APPROVALS,
        _ledger_text(
            {
                "type": "vault",
                "universe": "U1",
                "hypothesis": "H-0011",
                "trial": "T-0001",
                "mode": "blind_replication",
                "study": "btc_eth_001",
                "max_knowable_time": MAX_KT,
                "inherited": INHERITED_SPEC,
            }
        ),
    )
    sandbox.write(
        trials.TRIALS,
        _ledger_text(
            {
                "type": "trial",
                "hypothesis": "H-0011",
                "study": "btc_eth_001",
                "segment": "vault",
                "mode": "blind_replication",
                "approval": 1,
                "max_knowable_time": MAX_KT,
                "inherited": INHERITED_SPEC,
                "params": {},
                "note": "未烧毁族越权",
            }
        ),
    )
    assert any("不在 U1 的 families_burned" in v.message for v in sandbox.check(base))

    # 4. Past burned_through (2026-11-20 > 2026-11-19) refused.
    past = "2026-11-20T00:00:00Z"
    sandbox.write(
        APPROVALS,
        _ledger_text(
            {
                "type": "vault",
                "universe": "U1",
                "hypothesis": "H-0010",
                "trial": "T-0001",
                "mode": "blind_replication",
                "study": "btc_eth_001",
                "max_knowable_time": past,
                "inherited": INHERITED_SPEC,
            }
        ),
    )
    sandbox.write(
        trials.TRIALS,
        _ledger_text(
            {
                "type": "trial",
                "hypothesis": "H-0010",
                "study": "btc_eth_001",
                "segment": "vault",
                "mode": "blind_replication",
                "approval": 1,
                "max_knowable_time": past,
                "inherited": INHERITED_SPEC,
                "params": {},
                "note": "超截止日",
            }
        ),
    )
    assert any("超过了 burned_through" in v.message for v in sandbox.check(base))


# --- 2. Runtime: readiness, consumption, PIT clamp, zero-leakage, re-run block ---


def test_blind_replication_end_to_end_zero_leakage_and_rerun_blocked(
    blind_env, blind_studies, capsys
):
    root, store, ref, _ = blind_env
    seq = _append_blind_pair(root, params={"behavior": "print_and_ok", "beta_eth": 0.014})

    done = run.run_trial(root, store, "T-0001", package=blind_studies)
    out = capsys.readouterr()
    assert done.alarms == []
    assert "判定：一致" in done.lines
    assert "0.98765" not in out.out + out.err + "\n".join(done.lines)
    assert "0.01234" not in out.out + out.err + "\n".join(done.lines)
    assert store.exists(approval_used_key(seq))
    assert not store.exists("derived/trials/T-0001/console.log")  # console discarded!

    res_bytes = store.get("derived/trials/T-0001/result.json")
    rep_bytes = store.get("derived/trials/T-0001/report.md")
    res = json.loads(res_bytes)
    blind.assert_no_forbidden_keys(res)
    assert res["metrics"]["overall_verdict"] == "一致"
    assert res["metrics"]["tests"]["daily_btc_eth"]["delta_se"] == 0.2
    assert res["metrics"]["reference"]["sha256"] == ref.sha256
    assert "0.98765" not in rep_bytes.decode() and "0.01234" not in rep_bytes.decode()
    assert "0.014" not in res_bytes.decode() and "0.014" not in rep_bytes.decode()

    # Re-running the same trial card is immediately blocked without invoking the study.
    mod = sys.modules[f"{blind_studies}.btc_eth_001"]
    calls_before = len(mod.CALLS)
    with pytest.raises(run.TrialRefused, match="已经开始过"):
        run.run_trial(root, store, "T-0001", package=blind_studies)
    assert len(mod.CALLS) == calls_before


def test_data_not_ready_refuses_without_consuming_approval(blind_env, blind_studies):
    root, store, _, _ = blind_env
    seq = _append_blind_pair(
        root,
        params={
            "required_dataset": DATASET,
            "required_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            "required_start_month": "2026-10",  # 2026-10 is absent in blind_env lake
            "required_end_month": "2026-11",
        },
    )
    with pytest.raises(run.TrialRefused, match="未消耗批准"):
        run.run_trial(root, store, "T-0001", package=blind_studies)
    assert not store.exists(run.started_key("T-0001"))
    assert not store.exists(approval_used_key(seq))


def test_blind_crash_redacts_error_discards_console_and_burns_approval(blind_env, blind_studies):
    root, store, _, _ = blind_env
    seq = _append_blind_pair(root, params={"behavior": "crash"})
    with pytest.raises(ValueError):
        run.run_trial(root, store, "T-0001", package=blind_studies)
    failed = store.get("ledger/trials/T-0001.failed.json").decode()
    assert "ValueError" in failed and "9.99" not in failed
    assert not store.exists("derived/trials/T-0001/console.log")
    assert store.exists(approval_used_key(seq))  # consumed: cannot reuse!


def test_blind_pit_enforces_max_knowable_time_and_blocks_direct_multi_segment(
    blind_env, blind_studies
):
    root, store, _, _ = blind_env
    # Direct multi-segment PointInTime without blind_replication receipt is refused immediately.
    with pytest.raises(AccessRefused, match="盲复现"):
        PointInTime(
            root,
            store,
            universe="U1",
            purpose="H-0010",
            allowed=frozenset({"confirmation", "vault", "forward"}),
        )
    # Study querying past max_knowable_time is refused by PointInTime._authorize().
    _append_blind_pair(root, params={"behavior": "past_limit"})
    with pytest.raises(AccessRefused, match="超过允许的上限"):
        run.run_trial(root, store, "T-0001", package=blind_studies)


def test_blind_study_cannot_return_outcome_sneak_quality_or_loosen_bar(blind_env, blind_studies):
    root, store, _, _ = blind_env
    _append_blind_pair(root, params={"behavior": "return_outcome"})
    with pytest.raises(TypeError, match="BlindMetrics"):
        run.run_trial(root, store, "T-0001", package=blind_studies)

    # Sneaking an un-allowlisted key into BlindMetrics.quality fails.
    _append_blind_pair(root, trial_tid="T-0002", params={"behavior": "bad_quality"})
    with pytest.raises(ValueError, match="白名单"):
        run.run_trial(root, store, "T-0002", package=blind_studies)

    # Trying to loosen max_delta_se > 1.0 via params fails.
    _append_blind_pair(root, trial_tid="T-0003", params={"max_delta_se": 1.5})
    with pytest.raises(ValueError, match="更严"):
        run.run_trial(root, store, "T-0003", package=blind_studies)


def test_tampered_reference_fails_before_burning_approval(blind_env, blind_studies):
    root, store, ref, ref_bytes = blind_env
    # Overwrite cache with tampered bytes in LocalStore.
    tampered_path = store.root / blind.raw_cache_key(ref)
    tampered_path.write_bytes(ref_bytes.replace(b"0.01", b"0.99", 1))
    seq = _append_blind_pair(root)
    with pytest.raises(IntegrityError, match="SHA-256"):
        run.run_trial(root, store, "T-0001", package=blind_studies)
    assert not store.exists(run.started_key("T-0001"))
    assert not store.exists(approval_used_key(seq))


def test_research_boundary_blocks_blind_module_and_allows_blind_metrics(sandbox):
    def viols(src: str) -> list[str]:
        sandbox.write("src/sqp/research/studies/s.py", src)
        return [v.message for v in sandbox.check() if v.rule == "research-boundary"]

    assert viols("from sqp.research import blind\n")
    assert viols("from sqp.research.blind import compare\n")
    assert viols("from sqp.data.pit import VaultReceipt, consume_vault_approval\n")
    good = (
        "from sqp.research.studies import BlindMetrics\n"
        "from sqp.stats import (\n"
        "    ols_fit, newey_west_hac, partial_lead_lag, holm_adjust,\n"
        "    purged_walk_forward_splits, evaluate_walk_forward_forecasts,\n"
        "    circular_block_bootstrap_mean_test,\n"
        ")\n"
    )
    assert viols(good) == []


def test_blind_metrics_repr_is_redacted():
    m = BlindMetrics(values={"beta": 0.12345}, quality={"rows": 99})
    assert repr(m) == "<BlindMetrics redacted>"
    assert str(m) == "<BlindMetrics redacted>"
    assert "0.12345" not in f"{m!r} {m}"


# --- 3. Pure-Python stats precision & blind comparator thresholds -------------


def test_pure_python_ols_hac_and_partial_lead_lag_accuracy():
    rng = random.Random(20261127)
    n = 300
    x1 = [rng.gauss(0, 1) for _ in range(n)]
    x2 = [rng.gauss(0, 1) for _ in range(n)]
    e = [rng.gauss(0, 0.25) for _ in range(n)]
    y = [0.5 + 1.2 * x1[i] - 0.7 * x2[i] + e[i] for i in range(n)]

    fit = ols_fit(y, {"x1": x1, "x2": x2})
    assert fit.coefficients[0] == pytest.approx(0.5, abs=0.05)
    assert fit.coefficients[1] == pytest.approx(1.2, abs=0.05)
    assert fit.coefficients[2] == pytest.approx(-0.7, abs=0.05)
    assert fit.r_squared > 0.95

    hac0 = newey_west_hac(y, {"x1": x1, "x2": x2}, max_lag=0, small_sample_correction=False)
    hac0_ssc = newey_west_hac(y, {"x1": x1, "x2": x2}, max_lag=0, small_sample_correction=True)
    scale = math.sqrt(n / (n - 3))
    assert hac0_ssc.coefficients[1].standard_error == pytest.approx(
        hac0.coefficients[1].standard_error * scale, rel=1e-12
    )
    # Normal p-value exactness check.
    c1 = hac0.coefficients[1]
    assert c1.t_statistic is not None and c1.p_value is not None
    assert c1.p_value == pytest.approx(math.erfc(abs(c1.t_statistic) / math.sqrt(2.0)), rel=1e-12)

    # Collinear design matrix rejected.
    with pytest.raises(ValueError):
        ols_fit(y, {"x1": x1, "dup": x1})

    # Partial lead-lag: strong planted lag-1 effect recovered cleanly.
    src = [rng.gauss(0, 1) for _ in range(n)]
    fac = [rng.gauss(0, 1) for _ in range(n)]
    tgt = [0.0] * n
    for t in range(1, n):
        tgt[t] = 0.6 * src[t - 1] + 0.3 * fac[t - 1] + 0.1 * tgt[t - 1] + rng.gauss(0, 0.5)
    pll = partial_lead_lag(src, tgt, lag_steps=1, common_factors={"fac": fac}, max_hac_lag=5)
    assert pll.source_effect.estimate == pytest.approx(0.6, abs=0.1)
    assert pll.source_effect.p_value is not None and pll.source_effect.p_value < 1e-6
    assert pll.partial_r_squared is not None and pll.partial_r_squared > 0.3


def test_holm_purged_walk_forward_and_bootstrap_determinism():
    items = holm_adjust([0.04, 0.01, 0.03], alpha=0.05)
    assert [it.adjusted_p_value for it in items] == pytest.approx([0.06, 0.03, 0.06])
    assert [it.reject for it in items] == [False, True, False]

    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    decisions = [t0 + timedelta(days=i) for i in range(100)]
    label_ends = [d + timedelta(days=2) for d in decisions]
    folds = purged_walk_forward_splits(
        decisions, label_ends, initial_train_size=50, test_size=25, embargo=timedelta(days=1)
    )
    assert len(folds) == 2
    # Rows 48 and 49 have label_end >= day 50 -> purged; row 47 has decision < day 49 -> kept.
    assert folds[0].purged_indices == (48, 49)
    assert folds[0].train_indices[-1] == 47

    actual = [1.0] * 100
    base_pred = [0.0] * 100
    aug_pred = [0.5] * 100
    ev = evaluate_walk_forward_forecasts(actual, base_pred, aug_pred, folds=folds)
    assert ev.observations == 50
    assert ev.incremental_oos_r_squared == pytest.approx(0.75)

    # Circular block bootstrap is deterministic and separates shifted mean from zero mean.
    shifted = [0.05 + (0.01 if i % 2 == 0 else -0.01) for i in range(60)]
    b1 = circular_block_bootstrap_mean_test(shifted, block_length=5, resamples=500, seed=42)
    b2 = circular_block_bootstrap_mean_test(shifted, block_length=5, resamples=500, seed=42)
    assert b1 == b2
    assert b1.p_value < 0.01


def test_blind_compare_thresholds_and_byte_determinism():
    ref_bytes = _synthetic_ref_bytes(beta_eth=0.01, se_eth=0.02, obs_eth=1000)
    ref = blind.InheritedRef(
        source="myquant",
        url="https://github.com/zhihong1357/myquant",
        commit="feb6a07b7a03bbfe7a6c2fccf2b28c1c97b537a7",
        path=INHERITED_SPEC["path"],
        bytes=len(ref_bytes),
        sha256=hashlib.sha256(ref_bytes).hexdigest(),
    )

    # Within 1.0 SE -> 一致, deterministic across calls.
    o1 = blind.compare_btc_eth_001(_sample_raw_metrics(beta_eth=0.018), ref_bytes, ref)
    o2 = blind.compare_btc_eth_001(_sample_raw_metrics(beta_eth=0.018), ref_bytes, ref)
    assert o1.verdict == "一致" and o1 == o2
    assert o1.metrics["tests"]["daily_btc_eth"]["delta_se"] == 0.4

    # 1.01 SE -> 不一致 (strictly enforces lineage.md 1.0 SE bar).
    o_se = blind.compare_btc_eth_001(_sample_raw_metrics(beta_eth=0.0302), ref_bytes, ref)
    assert o_se.verdict == "不一致"
    assert "daily_btc_eth:FAIL_DELTA_SE_THRESHOLD" in o_se.metrics["failure_reasons"]

    # Coverage diff > 5% -> 不一致.
    o_cov = blind.compare_btc_eth_001(_sample_raw_metrics(obs_eth=940), ref_bytes, ref)
    assert o_cov.verdict == "不一致"
    assert "daily_btc_eth:FAIL_SAMPLE_COVERAGE" in o_cov.metrics["failure_reasons"]

    # Verdict / Holm / bootstrap flip -> 不一致.
    o_flip = blind.compare_btc_eth_001(
        _sample_raw_metrics(supported=True, holm_eth=True, boot_reject=True), ref_bytes, ref
    )
    assert o_flip.verdict == "不一致"
    assert "overall:FAIL_VERDICT_MISMATCH" in o_flip.metrics["failure_reasons"]
    assert "daily_btc_eth:FAIL_HOLM_MISMATCH" in o_flip.metrics["failure_reasons"]
    assert "cost_gate:FAIL_BOOTSTRAP_MISMATCH" in o_flip.metrics["failure_reasons"]


def test_dataset_window_ready_checks_metadata_only(tmp_path):
    store = _build_lake(
        tmp_path,
        months={"2026-10": datetime(2026, 10, 1, tzinfo=UTC)},
        symbols=("BTCUSDT", "ETHUSDT"),
    )
    assert dataset_window_ready(store, DATASET, ("BTCUSDT", "ETHUSDT"), "2026-10", "2026-10") == []
    missing = dataset_window_ready(
        store, DATASET, ("BTCUSDT", "ETHUSDT", "SOLUSDT"), "2026-10", "2026-11"
    )
    assert missing == ["2026-10:SOLUSDT", "2026-11:BTCUSDT", "2026-11:ETHUSDT", "2026-11:SOLUSDT"]
