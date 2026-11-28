"""Round-three red-team regression tests for PR #29, originally authored by Model B at 6b5e76d.

Every test originally passed while its corresponding weakness existed. All 4 cases have
been converted into permanent regression tests asserting N1/N2a/N2b are refused by
`research-boundary` and L4 refuses missing/mismatched `code_sha256` both at gate time
and before `started.json` / `used.json` at runtime. N- and L-numbers match the round-three review.
"""

# ruff: noqa: E402, F811  (sys.path set first; the PR's fixtures are used as parameters)

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TESTS = HERE if (HERE / "test_blind_replication.py").exists() else HERE.parent / "tests"
sys.path.insert(0, str(TESTS))

import test_blind_replication as tb
from sqp.checks import research as boundary
from sqp.data.pit import APPROVALS, approval_used_key
from sqp.research import blind, run, trials
from test_blind_replication import _blind_lake_template, blind_env, blind_studies  # noqa: F401

STUDY_PATH = "src/sqp/research/studies/btc_eth_001.py"

QUALITY_OK = (
    '{"daily_rows": 1001, "daily_dropped_incomplete_days": 0, "daily_dropped_close_rows": 0,'
    ' "daily_dropped_return_rows": 1, "daily_dropped_lag_pairs": 0, "hourly_rows": 2001,'
    ' "hourly_dropped_close_rows": 0, "hourly_dropped_return_rows": 1,'
    ' "hourly_dropped_lag_pairs": 0, "duplicate_bars": 0, "invalid_close_bars": 0,'
    ' "daily_first": "2023-01-02T16:00:00+00:00", "daily_last": "2026-11-16T16:00:00+00:00",'
    ' "hourly_first": "2026-08-20T01:00:00+00:00", "hourly_last": "2026-11-17T23:00:00+00:00"}'
)

VALUES = """{
    "supported": False,
    "cost_gate_bootstrap_reject": False,
    "criteria": {
        "holm_adjusted_primary_p_below_0.05": False,
        "primary_partial_r_squared_at_least_0.005": False,
        "holdout_oos_incremental_r_squared_above_0": False,
        "net_daily_edge_after_costs_positive": False,
    },
    "holm_reject": {"daily_btc_eth": False, "daily_btc_sol": False, "hourly_btc_eth": False},
    "tests": {
        "daily_btc_eth": {"coefficient": BETA_ETH, "observations": 1000},
        "daily_btc_sol": {"coefficient": -0.005, "observations": 1000},
        "hourly_btc_eth": {"coefficient": 0.002, "observations": 2000},
    },
}"""


# ---------------------------------------------------------------------------
# N1  enum & functools removed from ALLOWED_MODULES; global_enum in OTHER_NAMES
# ---------------------------------------------------------------------------
GLOBAL_ENUM_STUDY = (
    """import enum
import functools

from sqp.research.studies import BlindMetrics


def run(pit, params):
    secret = "0.987654321"
    knobs = enum.Enum(
        "Knobs", [("MAX_REPORTED_DELTA_SE", 1e300)], type=float, module="sqp.research.blind"
    )
    enum.global_enum(knobs)
    hook = enum.Enum(
        "Hook", [("print", (print, "LEAK=" + secret))], type=functools.partial, module="builtins"
    )
    enum.global_enum(hook)
    BETA_ETH = 0.01 + 123456789.0 * 0.02
    values = """
    + VALUES
    + """
    quality = """
    + QUALITY_OK
    + """
    return BlindMetrics(values=values, quality=quality)
"""
)


def test_n1_global_enum_rewrites_framework_and_builtins():
    viols = boundary._scan(STUDY_PATH, GLOBAL_ENUM_STUDY)
    msgs = [v.message for v in viols]
    assert any("import enum" in m for m in msgs)
    assert any("import functools" in m for m in msgs)
    assert any("global_enum" in m for m in msgs)


# ---------------------------------------------------------------------------
# N2  attribute assignment and deletion refused across all study code
# ---------------------------------------------------------------------------
TOUCHED_STUDY = """from datetime import UTC, datetime

from sqp.data.pit import PointInTime


def run(pit, params):
    PointInTime.touched = lambda self, start, end: []
    return pit.table(
        "um-klines-1h",
        start=datetime(2025, 10, 31, tzinfo=UTC),
        end=datetime(2025, 11, 2, tzinfo=UTC),
    )
"""

FILTER_STUDY = (
    """from datetime import UTC, datetime

import polars as pl

from sqp.research.studies import BlindMetrics

EVIDENCE = {}


def run(pit, params):
    original = pl.DataFrame.filter
    pl.DataFrame.filter = lambda self, *args, **kwargs: self
    rows = pit.table("um-klines-1h", end=datetime(2026, 11, 18, tzinfo=UTC))
    pl.DataFrame.filter = original
    EVIDENCE["latest"] = rows["knowable_time"].max()
    BETA_ETH = 0.014
    values = """
    + VALUES
    + """
    quality = """
    + QUALITY_OK
    + """
    return BlindMetrics(values=values, quality=quality)
"""
)


def test_n2a_class_patch_lets_exploration_card_read_vault():
    viols = boundary._scan(STUDY_PATH, TOUCHED_STUDY)
    assert len(viols) == 1
    assert "赋值或删除属性" in viols[0].message


def test_n2b_polars_patch_reads_past_cutoff():
    viols = boundary._scan(STUDY_PATH, FILTER_STUDY)
    assert len(viols) == 2
    assert all("赋值或删除属性" in v.message for v in viols)


# ---------------------------------------------------------------------------
# L4  one-time blind approval locks reviewed code SHA-256 at gate & runtime
# ---------------------------------------------------------------------------
def test_l4_approval_code_sha256_enforced_at_gate(sandbox):
    # Static gate checks: missing, malformed, extra key, or mismatched code_sha256 refused.
    sandbox.write(
        "registry/universes/U1.toml", (tb.ROOT / "registry/universes/U1.toml").read_text()
    )
    sandbox.write("config/inheritance.toml", (tb.ROOT / "config/inheritance.toml").read_text())
    sandbox.write(blind.MANIFEST_PATH, tb._make_manifest(tb._synthetic_ref_bytes()))
    sandbox.write(tb.DECISION_0031, (tb.ROOT / tb.DECISION_0031).read_text())
    sandbox.write(
        "registry/hypotheses/H-0010.toml",
        tb.HYPOTHESIS_TEMPLATE.format(
            hid="H-0010",
            family="crypto.cross-asset-transmission",
            segments='["confirmation", "vault", "forward"]',
        ),
    )
    sandbox.write(STUDY_PATH, "x = 1\n")
    sandbox.write("src/sqp/research/blind.py", (tb.ROOT / "src/sqp/research/blind.py").read_text())
    sandbox.write("src/sqp/stats.py", (tb.ROOT / "src/sqp/stats.py").read_text())
    sandbox.write(APPROVALS, "")
    sandbox.write(trials.TRIALS, "")
    base = sandbox.commit("base")

    ap_base = {
        "type": "vault",
        "universe": "U1",
        "hypothesis": "H-0010",
        "trial": "T-0001",
        "mode": "blind_replication",
        "study": "btc_eth_001",
        "max_knowable_time": tb.MAX_KT,
        "inherited": tb.INHERITED_SPEC,
    }
    card = {
        "type": "trial",
        "hypothesis": "H-0010",
        "study": "btc_eth_001",
        "segment": "vault",
        "mode": "blind_replication",
        "approval": 1,
        "max_knowable_time": tb.MAX_KT,
        "inherited": tb.INHERITED_SPEC,
        "tests": 3,
        "params": dict(tb.VALID_BLIND_PARAMS),
        "note": "L4 门禁测试",
    }
    sandbox.write(trials.TRIALS, tb._ledger_text(card))

    # Missing code_sha256 -> refused by vault-approval and trial-card.
    sandbox.write(APPROVALS, tb._ledger_text(ap_base))
    viols = sandbox.check(base)
    assert any(v.rule == "vault-approval" and "code_sha256" in v.message for v in viols)
    assert any(v.rule == "trial-card" and "code_sha256" in v.message for v in viols)

    # Wrong hash against PR bytes -> refused at gate.
    good_hashes = tb._code_sha256(sandbox.root)
    bad_hashes = {**good_hashes, STUDY_PATH: "0" * 64}
    sandbox.write(APPROVALS, tb._ledger_text({**ap_base, "code_sha256": bad_hashes}))
    viols = sandbox.check(base)
    assert any(v.rule == "trial-card" and "批准锁定的代码已改动" in v.message for v in viols)

    # Extra unapproved file key -> refused.
    extra_hashes = {**good_hashes, "src/sqp/cli.py": "a" * 64}
    sandbox.write(APPROVALS, tb._ledger_text({**ap_base, "code_sha256": extra_hashes}))
    viols = sandbox.check(base)
    assert any(v.rule == "vault-approval" and "不能包含" in v.message for v in viols)

    # Missing stats.py -> refused now that all 3 files (study, blind.py, stats.py) are mandatory.
    missing_stats = {k: v for k, v in good_hashes.items() if k != "src/sqp/stats.py"}
    sandbox.write(APPROVALS, tb._ledger_text({**ap_base, "code_sha256": missing_stats}))
    viols = sandbox.check(base)
    assert any(v.rule == "vault-approval" and "src/sqp/stats.py" in v.message for v in viols)

    # Exact 3-file hashes -> passes gate cleanly.
    sandbox.write(APPROVALS, tb._ledger_text({**ap_base, "code_sha256": good_hashes}))
    assert not any(v.rule in ("vault-approval", "trial-card") for v in sandbox.check(base))


def test_l4_approval_not_bound_to_reviewed_code(blind_env, blind_studies):
    # Runtime: modifying the study file after approval is signed refuses before started/used.
    root, store, _ref, _ = blind_env
    seq = tb._append_blind_pair(root)
    study_file = root / STUDY_PATH
    study_file.write_text("x = 2  # changed after the approval was signed\n")
    tb._merge(root)
    with pytest.raises(
        run.TrialRefused,
        match=r"^前置检查失败，本次未开始、未消耗批准：批准锁定的代码已改动$",
    ):
        run.run_trial(root, store, "T-0001", package=blind_studies)
    assert not store.exists(run.started_key("T-0001"))
    assert not store.exists(approval_used_key(seq))

    # Restoring study_file and instead modifying blind.py also refuses before started/used.
    study_file.write_text("x = 1\n")
    blind_file = root / "src/sqp/research/blind.py"
    orig_blind = blind_file.read_bytes()
    blind_file.write_bytes(orig_blind + b"\n# tampered\n")
    tb._merge(root)
    with pytest.raises(
        run.TrialRefused,
        match=r"^前置检查失败，本次未开始、未消耗批准：批准锁定的代码已改动$",
    ):
        run.run_trial(root, store, "T-0001", package=blind_studies)
    assert not store.exists(run.started_key("T-0001"))
    assert not store.exists(approval_used_key(seq))

    # Restoring blind.py and instead modifying stats.py also refuses before started/used.
    blind_file.write_bytes(orig_blind)
    stats_file = root / "src/sqp/stats.py"
    stats_file.write_bytes(stats_file.read_bytes() + b"\n# tampered\n")
    tb._merge(root)
    with pytest.raises(
        run.TrialRefused,
        match=r"^前置检查失败，本次未开始、未消耗批准：批准锁定的代码已改动$",
    ):
        run.run_trial(root, store, "T-0001", package=blind_studies)
    assert not store.exists(run.started_key("T-0001"))
    assert not store.exists(approval_used_key(seq))
