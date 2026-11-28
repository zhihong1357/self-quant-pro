"""Round-two red-team regression tests for PR #29, originally authored by Model B at 056b354.

Every test originally passed while its corresponding weakness existed. All 4 cases have
been converted into permanent regression tests asserting R1/R2/R4 are refused and R3
safely accepts Polars' exact `ZoneInfo("UTC")` while still refusing subclasses and
non-UTC `ZoneInfo`. R-numbers match the round-two review.
"""

# ruff: noqa: E402, F811  (sys.path set first; the PR's fixtures are used as parameters)

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pytest

HERE = Path(__file__).resolve().parent
TESTS = HERE if (HERE / "test_blind_replication.py").exists() else HERE.parent / "tests"
sys.path.insert(0, str(TESTS))

import test_blind_replication as tb
from sqp.checks import research as boundary
from sqp.data.pit import PointInTime
from sqp.research import run
from test_blind_replication import _blind_lake_template, blind_env  # noqa: F401

STUDY_PATH = "src/sqp/research/studies/btc_eth_001.py"

QUALITY_OK = (
    '{"daily_rows": 1001, "daily_dropped_incomplete_days": 0, "daily_dropped_close_rows": 0,'
    ' "daily_dropped_return_rows": 1, "daily_dropped_lag_pairs": 0, "hourly_rows": 2001,'
    ' "hourly_dropped_close_rows": 0, "hourly_dropped_return_rows": 1,'
    ' "hourly_dropped_lag_pairs": 0, "duplicate_bars": 0, "invalid_close_bars": 0,'
    ' "daily_first": "2023-01-02T16:00:00+00:00", "daily_last": "2026-11-16T16:00:00+00:00",'
    ' "hourly_first": "2026-08-20T01:00:00+00:00", "hourly_last": "2026-11-17T23:00:00+00:00"}'
)


def _write_study(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pkg: str, code: str) -> str:
    d = tmp_path / f"{pkg}_root" / pkg
    d.mkdir(parents=True)
    (d / "__init__.py").write_text("")
    (d / "btc_eth_001.py").write_text(code)
    monkeypatch.syspath_prepend(str(tmp_path / f"{pkg}_root"))
    return pkg


# ---------------------------------------------------------------------------
# R1  `match` statements are refused outright by research-boundary
# ---------------------------------------------------------------------------
ESCAPE_STUDY = (
    """from sqp.research.studies import BlindMetrics

EVIDENCE = {}


def grab(text, test, field):
    at = text.find(chr(34) + "tests" + chr(34))
    at = text.find(chr(34) + test + chr(34), at)
    at = text.find(chr(34) + field + chr(34) + ": ", at) + len(field) + 4
    stop = min(i for i in (text.find(",", at), text.find("}", at)) if i >= 0)
    return text[at:stop]


def run(pit, params):
    match pit:
        case object(_store=store):
            pass
    match store:
        case object(__class__=object(__name__=kind)):
            EVIDENCE["store_kind"] = kind
    fresh = [k for k in store.keys("datasets/") if "/2026-12." in k and k.endswith(".parquet")]
    EVIDENCE["fresh_part_bytes"] = len(store.get(fresh[0]))  # no PointInTime involved
    match pit.table:
        case object(__func__=object(__globals__=pit_globals)):
            EVIDENCE["pid"] = pit_globals["os"].getpid()
            EVIDENCE["eval"] = pit_globals["__builtins__"]["eval"]("6 * 7")
    try:
        raise ValueError("x")
    except ValueError as err:
        caught = err
    match caught:
        case object(
            __traceback__=object(tb_frame=object(f_back=object(f_back=object(f_locals=caller))))
        ):
            ref = caller["ref_bytes"].decode()
    EVIDENCE["ref_bytes_len"] = len(ref)
    tests = {}
    for name in ("daily_btc_eth", "daily_btc_sol", "hourly_btc_eth"):
        tests[name] = {
            "coefficient": float(grab(ref, name, "coefficient")),
            "observations": int(grab(ref, name, "observations")),
        }
    values = {
        "supported": False,
        "cost_gate_bootstrap_reject": False,
        "criteria": {
            "holm_adjusted_primary_p_below_0.05": False,
            "primary_partial_r_squared_at_least_0.005": False,
            "holdout_oos_incremental_r_squared_above_0": False,
            "net_daily_edge_after_costs_positive": False,
        },
        "holm_reject": {"daily_btc_eth": False, "daily_btc_sol": False, "hourly_btc_eth": False},
        "tests": tests,
    }
    quality = """
    + QUALITY_OK
    + """
    return BlindMetrics(values=values, quality=quality)
"""
)


def test_r1_match_statement_escapes_research_boundary():
    viols = boundary._scan(STUDY_PATH, ESCAPE_STUDY)
    assert len(viols) >= 4
    assert all("match" in v.message for v in viols)
    assert boundary._scan(
        STUDY_PATH, "def run(pit, p):\n    match p:\n        case _:\n            pass\n"
    )


# ---------------------------------------------------------------------------
# R2  dataclasses.make_dataclass and metaclasses are refused statically & at runtime
# ---------------------------------------------------------------------------
MAKE_DATACLASS_STUDY = """import dataclasses
from datetime import UTC, datetime

Moment = dataclasses.make_dataclass(
    "Moment",
    [],
    bases=(datetime,),
    namespace={"__gt__": lambda self, other: False, "__ge__": lambda self, other: False},
    init=False,
    repr=False,
    eq=False,
)
Leak = dataclasses.make_dataclass(
    "LEAK_0_0123", [], bases=(Exception,), module="google.api_core.exceptions"
)


def run(pit, params):
    return pit.table("um-klines-1h", end=Moment(2026, 12, 31, tzinfo=UTC))
"""


def test_r2_make_dataclass_slips_past_type_and_dunder_bans(blind_env):
    root, store, _ref, _ = blind_env
    # Static layer now refuses make_dataclass (and metaclasses such as typing.ABCMeta).
    assert boundary._scan(STUDY_PATH, MAKE_DATACLASS_STUDY)
    assert boundary._scan(STUDY_PATH, "from dataclasses import make_dataclass\n")
    assert boundary._scan(STUDY_PATH, "from typing import ABCMeta\n")
    # Runtime layer also continues to refuse the constructed subclasses.
    namespace: dict = {}
    exec(compile(MAKE_DATACLASS_STUDY, "study.py", "exec"), namespace)
    reader = PointInTime(root, store, universe="U1", allowed=frozenset())
    with pytest.raises(TypeError):
        namespace["run"](reader, {})
    assert run.error_name(namespace["Leak"]()) != "LEAK_0_0123"


# ---------------------------------------------------------------------------
# R3  usability: exact ZoneInfo('UTC') from polars is accepted; subclasses/non-UTC refused
# ---------------------------------------------------------------------------
def test_r3_polars_datetimes_are_rejected_by_pit(blind_env):
    root, store, _ref, _ = blind_env
    reader = PointInTime(root, store, universe="U1", allowed=frozenset())
    end = pl.Series([datetime(2024, 1, 31, 23, tzinfo=UTC)]).max()
    assert type(end.tzinfo) is ZoneInfo and end.tzinfo.key == "UTC"
    rows = reader.table("um-klines-1h", end=end)
    assert rows.height > 0

    # Non-UTC ZoneInfo and ZoneInfo subclasses are still refused.
    with pytest.raises(TypeError, match="tzinfo"):
        reader.table(
            "um-klines-1h", end=datetime(2024, 1, 31, 23, tzinfo=ZoneInfo("America/New_York"))
        )

    class SneakyZoneInfo(ZoneInfo):
        pass

    with pytest.raises(TypeError, match="tzinfo"):
        reader.table("um-klines-1h", end=datetime(2024, 1, 31, 23, tzinfo=SneakyZoneInfo("UTC")))


# ---------------------------------------------------------------------------
# R4  invalid dataset names rejected before _authorize; blind refusals redacted
# ---------------------------------------------------------------------------
REFUSAL_STUDY = (
    """from datetime import UTC, datetime

from sqp.data.pit import AccessRefused
from sqp.research.studies import BlindMetrics


def run(pit, params):
    try:
        pit.table("LEAK beta=0.987654321", end=datetime(2026, 11, 19, tzinfo=UTC))
    except AccessRefused:
        pass
    BETA_ETH = 0.014
    values = {
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
    }
    quality = """
    + QUALITY_OK
    + """
    return BlindMetrics(values=values, quality=quality)
"""
)

BLIND_VALID_DATASET_REFUSAL_STUDY = """from datetime import UTC, datetime

from sqp.data.pit import AccessRefused
from sqp.research.studies import BlindMetrics


def run(pit, params):
    pit.table(
        "um-klines-1h",
        start=datetime(2026, 11, 1, 12, 34, 56, 789012, tzinfo=UTC),
        end=datetime(2026, 11, 19, 15, 45, 59, 654321, tzinfo=UTC),
    )
    return BlindMetrics(values={}, quality={})
"""


def test_r4_refusal_ledger_keeps_free_text(blind_env, tmp_path, monkeypatch):
    root, store, _ref, _ = blind_env
    pkg = _write_study(tmp_path, monkeypatch, "refusalpkg", REFUSAL_STUDY)
    tb._append_blind_pair(root)
    # Invalid dataset name raises ValueError before _authorize -> no refusal record written!
    with pytest.raises(run.BlindTrialFailed) as exc_info:
        run.run_trial(root, store, "T-0001", package=pkg)
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert store.keys("ledger/refusals/") == []

    # Even when a blind study triggers AccessRefused with a valid dataset name and
    # sub-hour timestamps, the refusal record hashes dataset and truncates times to the hour.
    pkg2 = _write_study(tmp_path, monkeypatch, "refusalpkg2", BLIND_VALID_DATASET_REFUSAL_STUDY)
    tb._append_blind_pair(root, trial_tid="T-0002", redo_decision=tb.DECISION_0031)
    with pytest.raises(run.BlindTrialFailed):
        run.run_trial(root, store, "T-0002", package=pkg2)
    refusal_keys = store.keys("ledger/refusals/")
    assert len(refusal_keys) == 1
    raw_text = store.get(refusal_keys[0]).decode()
    rec = json.loads(raw_text)
    expected_hash = "sha256:" + hashlib.sha256(b"um-klines-1h").hexdigest()[:12]
    assert rec["dataset"] == expected_hash
    assert "um-klines-1h" not in raw_text
    assert rec["start"] == "2026-11-01T12:00:00+00:00"
    assert rec["end"] == "2026-11-19T15:00:00+00:00"
    assert "789012" not in raw_text and "654321" not in raw_text
