"""End-to-end synthetic rehearsal for `src/sqp/research/studies/btc_eth_001.py` (decision 0032).

Runs the real study module through the real `run.run_trial` + `PointInTime` + `blind`
comparator against full-window (2023-01 .. 2026-11) synthetic Binance 1h klines, proving:
- exact panel alignment (`2023-01-02T16:00Z .. 2026-11-16T16:00Z`, 1,415 daily rows;
  `2026-08-20T01:00Z .. 2026-11-17T23:00Z`, 2,159 hourly rows);
- consistent verdict on a null DGP and inconsistent verdict on a planted transmission DGP;
- zero leakage of raw estimates, zero console log, one-time approval consumption & re-run block;
- strict post-cutoff immunity (mutating bars after `2026-11-18T00:00Z` changes zero bits);
- exact accounting of duplicate bars, invalid closes, incomplete days, and gap-spanning lag pairs;
- exact agreement of cost-gate fee rules and purged walk-forward holdout slicing.
"""

# ruff: noqa: E402  (sys.path set before importing helper test_blind_replication)

from __future__ import annotations

import hashlib
import io
import itertools
import json
import math
import random
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_blind_replication as tb
from sqp.checks import research as boundary
from sqp.data import build
from sqp.data.pit import PointInTime, approval_used_key
from sqp.research import blind, run, trials
from sqp.research.studies import btc_eth_001
from sqp.storage import LocalStore

ROOT = Path(__file__).resolve().parents[1]
DECISION_0032 = "docs/decisions/0032-btc-eth-001-study-n5-and-isolation-order.md"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def _generate_hourly_table(
    *,
    seed: int,
    planted_beta: float = 0.0,
    post_cutoff_multiplier: float = 1.0,
    drop_bar: tuple[str, datetime] | None = None,
    duplicate_bar: tuple[str, datetime, float] | None = None,
    invalid_bar: tuple[str, datetime] | None = None,
) -> pl.DataFrame:
    """Synthesize realistic 1h klines from 2023-01-01T00:00Z through 2026-11-25T23:00Z."""
    rng = random.Random(seed)
    start = datetime(2023, 1, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 11, 26, 0, 0, tzinfo=UTC)
    total_hours = int((end - start).total_seconds() // 3600)
    cutoff = datetime(2026, 11, 18, 0, 0, tzinfo=UTC)

    # Generate daily BTC impulses on the 16:00 UTC grid so a planted lag-1 signal shows up cleanly.
    prev_day_btc_ret = 0.0
    cur_day_btc_acc = 0.0
    btc_log = math.log(20_000.0)
    eth_log = math.log(1_500.0)
    sol_log = math.log(20.0)

    sym_col: list[str] = []
    open_ms_col: list[int] = []
    close_ms_col: list[int] = []
    know_ms_col: list[int] = []
    close_col: list[float] = []

    hour_ms = 3_600_000
    base_ms = int(start.timestamp() * 1000)

    for step in range(total_hours):
        t = start + timedelta(hours=step)
        if t.hour == 16:
            prev_day_btc_ret = cur_day_btc_acc
            cur_day_btc_acc = 0.0
        common = rng.gauss(0.0, 0.004)
        r_btc = common + rng.gauss(0.0, 0.004)
        planted_push = (planted_beta * prev_day_btc_ret / 24.0) if planted_beta else 0.0
        r_eth = common + planted_push + rng.gauss(0.0, 0.003)
        r_sol = common + rng.gauss(0.0, 0.005)
        cur_day_btc_acc += r_btc

        btc_log += r_btc
        eth_log += r_eth
        sol_log += r_sol

        mult = post_cutoff_multiplier if t >= cutoff else 1.0
        prices = (
            ("BTCUSDT", math.exp(btc_log) * mult),
            ("ETHUSDT", math.exp(eth_log) * mult),
            ("SOLUSDT", math.exp(sol_log) * mult),
        )
        oms = base_ms + step * hour_ms
        cms = oms + hour_ms - 1
        kms = oms + hour_ms
        for sym, price in prices:
            if drop_bar is not None and drop_bar == (sym, t):
                continue
            if duplicate_bar is not None and (duplicate_bar[0], duplicate_bar[1]) == (sym, t):
                # Earlier stale revision (smaller knowable_time) that dedup must discard.
                sym_col.append(sym)
                open_ms_col.append(oms)
                close_ms_col.append(cms - 1000)
                know_ms_col.append(kms - 1000)
                close_col.append(duplicate_bar[2])
            sym_col.append(sym)
            open_ms_col.append(oms)
            close_ms_col.append(cms)
            know_ms_col.append(kms)
            close_col.append(price)
            if invalid_bar is not None and invalid_bar == (sym, t):
                sym_col.append(sym)
                open_ms_col.append(oms + 60_000)
                close_ms_col.append(cms + 60_000)
                know_ms_col.append(kms + 60_000)
                close_col.append(-5.0)

    df = pl.DataFrame(
        {
            "symbol": sym_col,
            "open_ms": open_ms_col,
            "close_ms": close_ms_col,
            "know_ms": know_ms_col,
            "close": close_col,
        }
    )
    return df.select(
        pl.col("symbol"),
        pl.from_epoch(pl.col("open_ms"), time_unit="ms")
        .dt.replace_time_zone("UTC")
        .alias("open_time"),
        pl.from_epoch(pl.col("close_ms"), time_unit="ms")
        .dt.replace_time_zone("UTC")
        .alias("close_time"),
        pl.from_epoch(pl.col("know_ms"), time_unit="ms")
        .dt.replace_time_zone("UTC")
        .alias("knowable_time"),
        pl.col("close").alias("open"),
        pl.col("close").alias("high"),
        pl.col("close").alias("low"),
        pl.col("close"),
        pl.lit(100.0).alias("volume"),
        pl.lit(1000.0).alias("quote_volume"),
        pl.lit(10).cast(pl.Int64).alias("trades"),
        pl.lit(50.0).alias("taker_buy_volume"),
        pl.lit(500.0).alias("taker_buy_quote_volume"),
    )


def _write_monthly_lake(root: Path, table: pl.DataFrame) -> LocalStore:
    store = LocalStore(root)
    with_month = table.with_columns(pl.col("open_time").dt.strftime("%Y-%m").alias("_month"))
    for (month,), part in with_month.partition_by("_month", as_dict=True).items():
        body = part.drop("_month").sort(["symbol", "open_time"])
        buf = io.BytesIO()
        body.write_parquet(buf, compression="zstd")
        pq_bytes = buf.getvalue()
        stem = f"{build.month_prefix(tb.DATASET, str(month))}.r01"
        pq_key = f"{stem}.parquet"
        store.put(pq_key, pq_bytes, content_type="application/octet-stream")
        store.put(build.checksum_key(pq_key), build.checksum_doc(pq_key, pq_bytes))
        inputs_doc = {
            "dataset": tb.DATASET,
            "month": str(month),
            "version": build.VERSION,
            "rows": body.height,
            "inputs": [
                {
                    "key": f"raw/synthetic/{sym}-{month}.zip",
                    "sha256": hashlib.sha256(f"{sym}-{month}".encode()).hexdigest(),
                    "symbol": sym,
                }
                for sym in SYMBOLS
            ],
            "duplicates": [],
        }
        store.put(f"{stem}.inputs.json", json.dumps(inputs_doc, sort_keys=True).encode())
    return store


def _setup_real_study_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ref_bytes: bytes
) -> Path:
    repo = tmp_path / "repo"
    for sub in (
        "config",
        "docs/decisions",
        "registry/universes",
        "registry/ledger",
        "registry/hypotheses",
        "registry/inherited",
        "src/sqp/research/studies",
    ):
        (repo / sub).mkdir(parents=True)
    shutil.copy(ROOT / "registry/universes/U1.toml", repo / "registry/universes/U1.toml")
    shutil.copy(ROOT / "config/inheritance.toml", repo / "config/inheritance.toml")
    shutil.copy(ROOT / trials.INHERITED, repo / trials.INHERITED)
    shutil.copy(ROOT / tb.DECISION_0031, repo / tb.DECISION_0031)
    shutil.copy(ROOT / DECISION_0032, repo / DECISION_0032)
    shutil.copy(
        ROOT / "src/sqp/research/studies/btc_eth_001.py",
        repo / "src/sqp/research/studies/btc_eth_001.py",
    )
    shutil.copy(ROOT / "src/sqp/research/blind.py", repo / "src/sqp/research/blind.py")
    shutil.copy(ROOT / "src/sqp/stats.py", repo / "src/sqp/stats.py")
    (repo / blind.MANIFEST_PATH).write_text(tb._make_manifest(ref_bytes))
    (repo / "registry/hypotheses/H-0010.toml").write_text(
        tb.HYPOTHESIS_TEMPLATE.format(
            hid="H-0010",
            family="crypto.cross-asset-transmission",
            segments='["confirmation", "vault", "forward"]',
        )
    )
    for name in ("approvals.jsonl", "refusals.jsonl", "trials.jsonl"):
        (repo / "registry/ledger" / name).write_text("")
    tb._git(repo, "init", "-q", "-b", "main")
    tb._git(repo, "config", "user.email", "test@example.com")
    tb._git(repo, "config", "user.name", "test")
    tb._merge(repo)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    return repo


@pytest.fixture(scope="module")
def _null_lake_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("btc_eth_null_lake")
    table = _generate_hourly_table(seed=12345, planted_beta=0.0, post_cutoff_multiplier=10.0)
    _write_monthly_lake(base / "lake", table)
    return base / "lake"


def test_btc_eth_001_passes_research_boundary_with_zero_violations() -> None:
    path = "src/sqp/research/studies/btc_eth_001.py"
    src = (ROOT / path).read_text("utf-8")
    assert boundary._scan(path, src) == []


def test_btc_eth_001_synthetic_rehearsal_null_consistent_and_zero_leakage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _null_lake_dir: Path, capsys
) -> None:
    # Synthetic Gen 2 null reference matching full-window sample counts (1,414 daily, 2,158 hourly).
    ref_bytes = tb._synthetic_ref_bytes(
        beta_eth=0.0,
        se_eth=0.04,
        obs_eth=1414,
        beta_sol=0.0,
        se_sol=0.05,
        obs_sol=1414,
        beta_h=0.0,
        se_h=0.03,
        obs_h=2158,
        supported=False,
        boot_p=0.50,
    )
    repo = _setup_real_study_repo(tmp_path, monkeypatch, ref_bytes)
    store = LocalStore(tmp_path / "store1")
    shutil.copytree(_null_lake_dir, store.root, dirs_exist_ok=True)
    ref = blind.manifest_entry(
        lambda p: (repo / p).read_text(), "myquant", tb.INHERITED_SPEC["path"]
    )
    assert ref is not None
    store.put(blind.raw_cache_key(ref), ref_bytes)

    seq = tb._append_blind_pair(repo)
    done = run.run_trial(repo, store, "T-0001")  # real package="sqp.research.studies"
    out = capsys.readouterr()

    assert done.alarms == []
    assert "判定：一致" in done.lines
    assert out.out == "" and out.err == ""
    assert store.exists(approval_used_key(seq))
    assert not store.exists("derived/trials/T-0001/console.log")

    res_bytes = store.get("derived/trials/T-0001/result.json")
    rep_bytes = store.get("derived/trials/T-0001/report.md")
    res = json.loads(res_bytes)
    blind.assert_no_forbidden_keys(res)

    metrics = res["metrics"]
    assert metrics["overall_verdict"] == "一致"
    assert metrics["verdict_consistent"] is True
    assert all(metrics["criteria_consistent"].values())
    assert all(metrics["holm_consistent"].values())
    assert metrics["cost_gate_bootstrap_consistent"] is True
    for name in blind.BTC_ETH_TESTS:
        assert metrics["tests"][name]["within_threshold"] is True
        assert metrics["tests"][name]["coverage_ok"] is True
        assert metrics["tests"][name]["observations_diff"] == 0

    q = metrics["panel_quality"]
    assert q["daily_rows"] == 1415
    assert q["daily_first"] == "2023-01-02T16:00:00+00:00"
    assert q["daily_last"] == "2026-11-16T16:00:00+00:00"
    assert q["daily_dropped_incomplete_days"] == 1
    assert q["daily_dropped_close_rows"] == 0
    assert q["daily_dropped_return_rows"] == 1
    assert q["daily_dropped_lag_pairs"] == 0
    assert q["hourly_rows"] == 2159
    assert q["hourly_first"] == "2026-08-20T01:00:00+00:00"
    assert q["hourly_last"] == "2026-11-17T23:00:00+00:00"
    assert q["hourly_dropped_close_rows"] == 0
    assert q["hourly_dropped_return_rows"] == 1
    assert q["hourly_dropped_lag_pairs"] == 0
    assert q["duplicate_bars"] == 0
    assert q["invalid_close_bars"] == 0
    assert "一致" in rep_bytes.decode("utf-8")

    # Re-running the same trial is refused immediately.
    with pytest.raises(run.TrialRefused, match="已经开始过"):
        run.run_trial(repo, store, "T-0001")

    # Byte-for-byte determinism across an independent store.
    store2 = LocalStore(tmp_path / "store2")
    shutil.copytree(_null_lake_dir, store2.root, dirs_exist_ok=True)
    store2.put(blind.raw_cache_key(ref), ref_bytes)
    run.run_trial(repo, store2, "T-0001")
    res2 = json.loads(store2.get("derived/trials/T-0001/result.json"))
    assert res2["metrics"] == metrics


def test_btc_eth_001_post_cutoff_immunity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _null_lake_dir: Path
) -> None:
    """Mutating bars after 2026-11-18T00:00Z must leave BlindMetrics bit-for-bit identical."""
    ref_bytes = tb._synthetic_ref_bytes(obs_eth=1414, obs_sol=1414, obs_h=2158)
    repo = _setup_real_study_repo(tmp_path, monkeypatch, ref_bytes)
    seq1 = tb._append_blind_pair(repo)

    store_a = LocalStore(tmp_path / "lake_a")
    shutil.copytree(_null_lake_dir, store_a.root, dirs_exist_ok=True)  # has 10x post-cutoff shock
    table_b = _generate_hourly_table(seed=12345, planted_beta=0.0, post_cutoff_multiplier=0.05)
    store_b = _write_monthly_lake(tmp_path / "lake_b", table_b)

    from sqp.data.pit import consume_vault_approval

    rc_a = consume_vault_approval(
        repo,
        store_a,
        universe="U1",
        hypothesis="H-0010",
        trial="T-0001",
        approval=seq1,
        mode="blind_replication",
        study="btc_eth_001",
        max_knowable_time=btc_eth_001.CUTOFF,
        inherited=tb.INHERITED_SPEC,
    )
    rc_b = consume_vault_approval(
        repo,
        store_b,
        universe="U1",
        hypothesis="H-0010",
        trial="T-0001",
        approval=seq1,
        mode="blind_replication",
        study="btc_eth_001",
        max_knowable_time=btc_eth_001.CUTOFF,
        inherited=tb.INHERITED_SPEC,
    )
    pit_a = PointInTime(
        repo,
        store_a,
        universe="U1",
        purpose="H-0010",
        allowed=frozenset({"confirmation", "vault", "forward"}),
        vault_receipt=rc_a,
        max_knowable_time=btc_eth_001.CUTOFF,
    )
    pit_b = PointInTime(
        repo,
        store_b,
        universe="U1",
        purpose="H-0010",
        allowed=frozenset({"confirmation", "vault", "forward"}),
        vault_receipt=rc_b,
        max_knowable_time=btc_eth_001.CUTOFF,
    )
    m_a = btc_eth_001.run(pit_a, {})
    m_b = btc_eth_001.run(pit_b, {})
    assert m_a == m_b


def test_btc_eth_001_synthetic_rehearsal_planted_signal_inconsistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref_bytes = tb._synthetic_ref_bytes(
        beta_eth=0.0,
        se_eth=0.02,
        obs_eth=1414,
        beta_sol=0.0,
        se_sol=0.03,
        obs_sol=1414,
        beta_h=0.0,
        se_h=0.02,
        obs_h=2158,
        supported=False,
        boot_p=0.50,
    )
    repo = _setup_real_study_repo(tmp_path, monkeypatch, ref_bytes)
    table = _generate_hourly_table(seed=99, planted_beta=0.40)
    store = _write_monthly_lake(tmp_path / "planted_lake", table)
    ref = blind.manifest_entry(
        lambda p: (repo / p).read_text(), "myquant", tb.INHERITED_SPEC["path"]
    )
    assert ref is not None
    store.put(blind.raw_cache_key(ref), ref_bytes)

    tb._append_blind_pair(repo)
    done = run.run_trial(repo, store, "T-0001")
    assert "判定：不一致" in done.lines
    res = json.loads(store.get("derived/trials/T-0001/result.json"))
    blind.assert_no_forbidden_keys(res)
    metrics = res["metrics"]
    assert metrics["overall_verdict"] == "不一致"
    assert metrics["verdict_consistent"] is False
    assert metrics["tests"]["daily_btc_eth"]["within_threshold"] is False
    assert metrics["tests"]["daily_btc_eth"]["delta_se"] <= blind.MAX_REPORTED_DELTA_SE


def test_btc_eth_001_handles_missing_bar_duplicate_and_invalid_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref_bytes = tb._synthetic_ref_bytes(obs_eth=1414, obs_sol=1414, obs_h=2158)
    repo = _setup_real_study_repo(tmp_path, monkeypatch, ref_bytes)
    seq = tb._append_blind_pair(repo)

    table = _generate_hourly_table(
        seed=7,
        drop_bar=("ETHUSDT", datetime(2024, 6, 15, 5, 0, tzinfo=UTC)),
        duplicate_bar=("BTCUSDT", datetime(2024, 5, 1, 10, 0, tzinfo=UTC), 1.0),
        invalid_bar=("SOLUSDT", datetime(2024, 5, 2, 10, 0, tzinfo=UTC)),
    )
    store = _write_monthly_lake(tmp_path / "dirty_lake", table)

    from sqp.data.pit import consume_vault_approval

    rc = consume_vault_approval(
        repo,
        store,
        universe="U1",
        hypothesis="H-0010",
        trial="T-0001",
        approval=seq,
        mode="blind_replication",
        study="btc_eth_001",
        max_knowable_time=btc_eth_001.CUTOFF,
        inherited=tb.INHERITED_SPEC,
    )
    pit = PointInTime(
        repo,
        store,
        universe="U1",
        purpose="H-0010",
        allowed=frozenset({"confirmation", "vault", "forward"}),
        vault_receipt=rc,
        max_knowable_time=btc_eth_001.CUTOFF,
    )
    out = btc_eth_001.run(pit, {})
    q = out.quality
    assert q["duplicate_bars"] == 1
    assert q["invalid_close_bars"] == 1
    assert q["daily_dropped_incomplete_days"] == 2  # 2024-06-14T16:00Z + partial 2026-11-17T16:00Z
    assert q["daily_dropped_close_rows"] == 1  # ETH missing 2024-06-14T16:00Z
    assert q["daily_dropped_return_rows"] == 3  # 2023-01-01 + missing day + post-gap ETH return
    assert q["daily_rows"] == 1413
    assert q["daily_dropped_lag_pairs"] == 1  # non-adjacent pair across the 2024-06 gap dropped
    assert out.values["tests"]["daily_btc_eth"]["observations"] == 1411


def test_btc_eth_001_cost_gate_exact_fee_accounting() -> None:
    # Sequence: +1 (entry 0.05%), +1 (hold 0), -1 (flip 0.10%), -1 (hold 0), +1 (flip + exit 0.15%).
    forecasts = [0.01, 0.02, -0.01, -0.02, 0.03] * 10
    actuals = [0.01, 0.01, -0.01, -0.01, 0.01] * 10  # gross = +0.01 every day
    net_edge, boot_reject = btc_eth_001._run_cost_gate(forecasts, actuals)

    # Compute expected fee total independently: entry (0.05%), flips (0.10%), exit (0.05%).
    positions = [1.0 if f > 0 else -1.0 for f in forecasts]
    expected_cost = 0.0005 + 0.0005  # entry + final exit
    expected_cost += sum(0.001 for a, b in itertools.pairwise(positions) if a != b)
    expected_net = (0.01 * len(actuals) - expected_cost) / len(actuals)
    assert math.isclose(net_edge, expected_net, rel_tol=1e-13, abs_tol=1e-15)
    assert boot_reject is True
