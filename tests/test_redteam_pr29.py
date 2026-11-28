"""Red-team regression tests for PR #29 (decision 0031), originally authored by Model B at d07b14e.

Every test originally passed while its corresponding weakness existed. All 13 cases have
been kept (none deleted) and converted into permanent regression tests asserting that both
static (`research-boundary` / `trials.problems`) and runtime (`PointInTime` / `blind` /
`run` / `cli`) gates refuse every attack. F-numbers match the review document.
"""

# ruff: noqa: E402, F811  (sys.path set first; the PR's fixtures are used as parameters)

from __future__ import annotations

import json
import sys
import urllib.error
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TESTS = HERE if (HERE / "test_blind_replication.py").exists() else HERE.parent / "tests"
sys.path.insert(0, str(TESTS))

import test_blind_replication as tb
from sqp import cli, ledger
from sqp.checks import research as boundary
from sqp.data import pit as pit_mod
from sqp.data.pit import AccessRefused, PointInTime, approval_used_key
from sqp.research import blind, run, trials
from sqp.research.studies import BlindMetrics
from test_blind_replication import (
    _blind_lake_template,  # noqa: F401
    blind_env,  # noqa: F401
    blind_studies,  # noqa: F401
)

STUDY_PATH = "src/sqp/research/studies/btc_eth_001.py"


def _write_study(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pkg: str, code: str) -> str:
    d = tmp_path / f"{pkg}_root" / pkg
    d.mkdir(parents=True)
    (d / "__init__.py").write_text("")
    (d / "btc_eth_001.py").write_text(code)
    monkeypatch.syspath_prepend(str(tmp_path / f"{pkg}_root"))
    return pkg


def _main_text(root: Path, rel: str) -> str:
    return pit_mod._on_main(root, rel) or ""


def _static_problems(root: Path, tid: str) -> list[str]:
    text = _main_text(root, trials.TRIALS)
    return trials.problems(text, lambda p: pit_mod._on_main(root, p), strict={tid})


QUALITY_OK = (
    '{"daily_rows": 1001, "daily_dropped_incomplete_days": 0, "daily_dropped_close_rows": 0,'
    ' "daily_dropped_return_rows": 1, "daily_dropped_lag_pairs": 0, "hourly_rows": 2001,'
    ' "hourly_dropped_close_rows": 0, "hourly_dropped_return_rows": 1,'
    ' "hourly_dropped_lag_pairs": 0, "duplicate_bars": 0, "invalid_close_bars": 0,'
    ' "daily_first": "2023-01-02T16:00:00+00:00", "daily_last": "2026-11-16T16:00:00+00:00",'
    ' "hourly_first": "2026-08-20T01:00:00+00:00", "hourly_last": "2026-11-17T23:00:00+00:00"}'
)

VALUES_OK = """{
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
        "daily_btc_eth": {"coefficient": BETA_ETH, "observations": OBS_ETH},
        "daily_btc_sol": {"coefficient": -0.005, "observations": 1000},
        "hourly_btc_eth": {"coefficient": 0.002, "observations": 2000},
    },
}"""


# ---------------------------------------------------------------------------
# F1  value side channel: quality ints / timestamps / observations / delta_se
# ---------------------------------------------------------------------------
LEAKY_STUDY = (
    """from sqp.research.studies import BlindMetrics


def run(pit, params):
    secret = 0.0123456789  # stands for any vault-period number the study computed
    quality = """
    + QUALITY_OK
    + """
    quality["duplicate_bars"] = int(secret * 10**12)
    quality["daily_first"] = "9999-99-99T99:99:99Z"
    BETA_ETH = 0.01 + 123456789.0 * 0.02
    OBS_ETH = 10**30
    values = """
    + VALUES_OK
    + """
    return BlindMetrics(values=values, quality=quality)
"""
)


def test_f1_quality_and_counts_are_an_unbounded_value_channel(blind_env, tmp_path, monkeypatch):
    root, store, ref, ref_bytes = blind_env
    pkg = _write_study(tmp_path, monkeypatch, "leakpkg", LEAKY_STUDY)
    tb._append_blind_pair(root)
    with pytest.raises(run.BlindTrialFailed) as exc_info:
        run.run_trial(root, store, "T-0001", package=pkg)
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert not store.exists("derived/trials/T-0001/result.json")
    assert not store.exists("derived/trials/T-0001/report.md")

    # Verify every individual channel is bounded or rejected.
    max_kt = datetime(2026, 11, 18, 0, tzinfo=UTC)
    bad_dup = {**tb._sample_quality(), "duplicate_bars": 12345678900}
    with pytest.raises(ValueError, match="上限"):
        blind.compare_btc_eth_001(
            tb._sample_raw_metrics(quality=bad_dup), ref_bytes, ref, max_knowable_time=max_kt
        )
    bad_date = {**tb._sample_quality(), "daily_first": "9999-99-99T99:99:99Z"}
    with pytest.raises(ValueError, match="合法的日历时间"):
        blind.compare_btc_eth_001(
            tb._sample_raw_metrics(quality=bad_date), ref_bytes, ref, max_knowable_time=max_kt
        )
    past_window_date = {**tb._sample_quality(), "hourly_last": "2026-12-01T00:00:00Z"}
    with pytest.raises(ValueError, match="允许窗口"):
        blind.compare_btc_eth_001(
            tb._sample_raw_metrics(quality=past_window_date),
            ref_bytes,
            ref,
            max_knowable_time=max_kt,
        )
    with pytest.raises(ValueError, match="上限"):
        blind.compare_btc_eth_001(
            tb._sample_raw_metrics(obs_eth=10**30), ref_bytes, ref, max_knowable_time=max_kt
        )
    capped = blind.compare_btc_eth_001(
        tb._sample_raw_metrics(beta_eth=0.01 + 123456789.0 * 0.02),
        ref_bytes,
        ref,
        max_knowable_time=max_kt,
    )
    assert capped.verdict == "不一致"
    assert capped.metrics["tests"]["daily_btc_eth"]["delta_se"] == blind.MAX_REPORTED_DELTA_SE
    assert capped.metrics["tests"]["daily_btc_eth"]["delta_se_capped"] is True
    assert "123456789" not in json.dumps(capped.metrics)
    assert "123456789" not in "\n".join(capped.sections.values())


# ---------------------------------------------------------------------------
# F2  threshold typo in card params rejected statically and before burning approval
# ---------------------------------------------------------------------------
def test_f2_bad_threshold_param_burns_one_time_approval(blind_env, blind_studies):
    root, store, _ref, _ = blind_env
    seq = tb._append_blind_pair(root, params={**tb.VALID_BLIND_PARAMS, "max_delta_se": 1.5})
    assert _static_problems(root, "T-0001")  # static gate rejects it!
    with pytest.raises(run.TrialRefused):
        run.run_trial(root, store, "T-0001", package=blind_studies)
    assert not store.exists(approval_used_key(seq))  # one-time approval preserved
    assert not store.exists(run.started_key("T-0001"))  # card not burned

    # Unknown / misspelled param key is also rejected statically and pre-flight.
    seq2 = tb._append_blind_pair(
        root,
        trial_tid="T-0002",
        params={**tb.VALID_BLIND_PARAMS, "max_delta_s": 0.5},
        redo_decision=tb.DECISION_0031,
    )
    assert any("未登记" in msg for msg in _static_problems(root, "T-0002"))
    with pytest.raises(run.TrialRefused):
        run.run_trial(root, store, "T-0002", package=blind_studies)
    assert not store.exists(approval_used_key(seq2))
    assert not store.exists(run.started_key("T-0002"))


# ---------------------------------------------------------------------------
# F3  data-readiness preflight is mandatory: omitting required_* is refused
# ---------------------------------------------------------------------------
def test_f3_readiness_preflight_silently_skipped(blind_env, blind_studies):
    root, store, _ref, _ = blind_env
    seq = tb._append_blind_pair(root, params={})
    probs = _static_problems(root, "T-0001")
    assert any("required_dataset" in m for m in probs)
    with pytest.raises(run.TrialRefused):
        run.run_trial(root, store, "T-0001", package=blind_studies)
    assert not store.exists(run.started_key("T-0001"))
    assert not store.exists(approval_used_key(seq))


# ---------------------------------------------------------------------------
# F4  failed.json uses error_name and never stores study-made exception names
# ---------------------------------------------------------------------------
TYPE_NAME_STUDY = """def run(pit, params):
    secret = 0.0123
    kind = type("LEAK_" + str(secret).replace(".", "_"), (Exception,), {})
    raise kind()
"""


def test_f4_failed_json_keeps_dynamic_type_name(blind_env, tmp_path, monkeypatch):
    root, store, _ref, _ = blind_env
    # Static gate now blocks dynamic `type(...)` too.
    assert boundary._scan(STUDY_PATH, TYPE_NAME_STUDY)
    pkg = _write_study(tmp_path, monkeypatch, "typepkg", TYPE_NAME_STUDY)
    tb._append_blind_pair(root)
    with pytest.raises(run.BlindTrialFailed) as info:
        run.run_trial(root, store, "T-0001", package=pkg)
    failed = json.loads(store.get("ledger/trials/T-0001.failed.json"))
    assert "LEAK_0_0123" not in failed["error"]
    assert "名字不打印" in failed["error"]
    assert failed["error"] == cli._error_name(info.value.__cause__)


# ---------------------------------------------------------------------------
# F5  str.format blocked by research-boundary AND caller frame has no ref_bytes
# ---------------------------------------------------------------------------
FAKE_STUDY = (
    """from sqp.research.studies import BlindMetrics


def grab(text, test, field):
    at = text.find(chr(34) + "tests" + chr(34))
    at = text.find(chr(34) + test + chr(34), at)
    at = text.find(chr(34) + field + chr(34) + ": ", at) + len(field) + 4
    stop = min(i for i in (text.find(",", at), text.find("}", at)) if i >= 0)
    return text[at:stop]


def run(pit, params):
    nonce = "{0._vault_receipt.nonce}".format(pit)
    try:
        raise ValueError("x")
    except ValueError as err:
        caught = err
    ref = "{0.__traceback__.tb_frame.f_back.f_locals[ref_bytes]}".format(caught)
    BETA_ETH = float(grab(ref, "daily_btc_eth", "coefficient"))
    OBS_ETH = int(grab(ref, "daily_btc_eth", "observations"))
    values = """
    + VALUES_OK
    + """
    for name in ("daily_btc_sol", "hourly_btc_eth"):
        values["tests"][name]["coefficient"] = float(grab(ref, name, "coefficient"))
        values["tests"][name]["observations"] = int(grab(ref, name, "observations"))
    quality = """
    + QUALITY_OK
    + """
    quality["duplicate_bars"] = len(nonce)
    return BlindMetrics(values=values, quality=quality)
"""
)


def test_f5_format_string_bypasses_boundary_and_fakes_replication(blind_env, tmp_path, monkeypatch):
    root, store, _ref, _ = blind_env
    # Static gate blocks `.format` and `.format_map`.
    assert boundary._scan(STUDY_PATH, FAKE_STUDY)
    assert boundary._scan(STUDY_PATH, 'def run(pit, p):\n    return "{a}".format_map({"a": 1})\n')
    # Defense in depth: even if static check were bypassed, study runs in an isolated frame
    # without `ref_bytes` in `f_back.f_locals`, so it cannot fake "一致".
    pkg = _write_study(tmp_path, monkeypatch, "fakepkg", FAKE_STUDY)
    tb._append_blind_pair(root)
    with pytest.raises(run.BlindTrialFailed):
        run.run_trial(root, store, "T-0001", package=pkg)
    assert not store.exists("derived/trials/T-0001/result.json")


# ---------------------------------------------------------------------------
# F6  burned families: standard forward trials blocked on/before burned_through
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("family", ["crypto.cross-asset-transmission", "crypto.funding"])
def test_f6_burned_family_forward_reads_burned_days(blind_env, family):
    root, store, _ref, _ = blind_env
    (root / "registry/hypotheses/H-0012.toml").write_text(
        tb.HYPOTHESIS_TEMPLATE.format(hid="H-0012", family=family, segments='["forward"]')
    )
    ledger.append(
        root / trials.TRIALS,
        {
            "type": "trial",
            "hypothesis": "H-0012",
            "study": "btc_eth_001",
            "segment": "forward",
            "params": {},
            "note": "普通前向检验",
        },
    )
    tb._merge(root)
    reader = PointInTime(
        root, store, universe="U1", purpose="H-0012", allowed=frozenset({"forward"})
    )
    # Forward dates on/before burned_through (2026-11-19) are refused and logged.
    with pytest.raises(AccessRefused, match="暴露"):
        reader.table(
            "um-klines-1h",
            start=datetime(2026, 11, 1, tzinfo=UTC),
            end=datetime(2026, 11, 19, 12, tzinfo=UTC),
        )
    with pytest.raises(AccessRefused):
        reader.table(
            "um-klines-1h",
            start=datetime(2026, 11, 19, 23, 59, tzinfo=UTC),
            end=datetime(2026, 11, 21, tzinfo=UTC),
        )
    # Strictly after burned_through (>= 2026-11-20T00:00:00Z) succeeds.
    clean = reader.table(
        "um-klines-1h",
        start=datetime(2026, 11, 20, 0, tzinfo=UTC),
        end=datetime(2026, 11, 21, 0, tzinfo=UTC),
    )
    assert clean.height > 0


# ---------------------------------------------------------------------------
# F7  CLI reports "前置检查失败，本次未开始、未消耗批准" when preflight fails
# ---------------------------------------------------------------------------
def test_f7_cli_misreports_preflight_failure(blind_env, blind_studies, monkeypatch, capsys):
    root, store, ref, _ = blind_env
    cache = store.root / "raw/gen2" / ref.source / ref.commit / ref.path
    cache.unlink()  # cache miss -> GitHub fetch

    def offline(url: str, **_k) -> bytes:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("sqp.data.vision.http_get", offline)
    seq = tb._append_blind_pair(root)
    assert cli._run_one(root, store, "T-0001") == 1
    out = capsys.readouterr().out
    assert "未开始、未消耗批准" in out
    assert "URLError" in out
    assert "这次已开始，计入试验次数" not in out
    assert not store.exists(run.started_key("T-0001"))
    assert not store.exists(approval_used_key(seq))


# ---------------------------------------------------------------------------
# F8  PointInTime(allowed=None) enforces the single-guarded-segment lock too
# ---------------------------------------------------------------------------
def test_f8_allowed_none_skips_multi_segment_lock(blind_env):
    root, store, _ref, _ = blind_env
    (root / "registry/hypotheses/H-0013.toml").write_text(
        tb.HYPOTHESIS_TEMPLATE.format(
            hid="H-0013", family="crypto.fresh-family", segments='["confirmation", "forward"]'
        )
    )
    tb._merge(root)
    reader = PointInTime(root, store, universe="U1", purpose="H-0013")  # allowed=None
    a = reader.table(
        "um-klines-1h", start=datetime(2024, 2, 1, tzinfo=UTC), end=datetime(2024, 2, 2, tzinfo=UTC)
    )
    assert a.height > 0
    with pytest.raises(AccessRefused, match="跨多个受保护段"):
        reader.table(
            "um-klines-1h",
            start=datetime(2026, 11, 17, tzinfo=UTC),
            end=datetime(2026, 11, 18, tzinfo=UTC),
        )
    with pytest.raises(AccessRefused, match="跨多个受保护段"):
        PointInTime(
            root,
            store,
            universe="U1",
            purpose="H-0013",
            allowed=frozenset({"confirmation", "forward"}),
        )


# ---------------------------------------------------------------------------
# F9  blind public log prints one fixed line (no error class or study stack frames)
# ---------------------------------------------------------------------------
CHOICE_STUDY = """def run(pit, params):
    secret_bits = 5
    kinds = (KeyError, ValueError, TypeError, IndexError, LookupError, OSError, EOFError, NameError)
    raise kinds[secret_bits]()
"""


def test_f9_public_log_carries_study_chosen_error_type(blind_env, tmp_path, monkeypatch, capsys):
    root, store, _ref, _ = blind_env
    pkg = _write_study(tmp_path, monkeypatch, "choicepkg", CHOICE_STUDY)
    monkeypatch.setitem(run.run_trial.__kwdefaults__, "package", pkg)
    tb._append_blind_pair(root)
    assert cli._run_one(root, store, "T-0001") == 1
    out = capsys.readouterr().out
    assert "盲复现出错" in out
    assert "OSError" not in out
    assert "btc_eth_001.py" not in out
    assert len(out.strip().splitlines()) == 1
    failed = json.loads(store.get("ledger/trials/T-0001.failed.json"))
    assert failed["error"] == "OSError"


# ---------------------------------------------------------------------------
# F10  datetime subclasses & custom tzinfo refused both statically and at runtime
# ---------------------------------------------------------------------------
MOMENT = """
class Moment(datetime):
    def __gt__(self, other):
        return False

    def __ge__(self, other):
        return False
"""


def test_f10a_exploration_card_reads_vault_without_approval(blind_env, monkeypatch):
    root, store, _ref, _ = blind_env
    code = (
        "from datetime import UTC, datetime\n"
        + MOMENT
        + (
            "\n\ndef run(pit, params):\n"
            "    return pit.table('um-klines-1h', start=Moment(2025, 10, 31, tzinfo=UTC),\n"
            "                     end=Moment(2025, 11, 2, tzinfo=UTC))\n"
        )
    )
    # Static gate rejects dunder method definitions in studies.
    assert boundary._scan(STUDY_PATH, code)
    for var in ("GITHUB_ACTIONS", "GITHUB_REF", "GITHUB_EVENT_NAME"):
        monkeypatch.delenv(var)
    namespace: dict = {}
    exec(compile(code, "study.py", "exec"), namespace)
    reader = PointInTime(root, store, universe="U1", allowed=frozenset())
    # Runtime gate rejects datetime subclasses (UTC and non-UTC offsets).
    with pytest.raises(TypeError, match="datetime"):
        namespace["run"](reader, {})
    Moment = namespace["Moment"]
    with pytest.raises(TypeError, match="datetime"):
        reader.table(
            "um-klines-1h",
            end=Moment(2025, 11, 2, tzinfo=timezone(timedelta(hours=-8))),
        )

    # Custom tzinfo subclass is also refused by _utc().
    class SneakyTZ(tzinfo):
        def utcoffset(self, dt):
            return timedelta(0)

        def dst(self, dt):
            return timedelta(0)

        def tzname(self, dt):
            return "UTC"

    with pytest.raises(TypeError, match="tzinfo"):
        reader.table("um-klines-1h", end=datetime(2024, 1, 31, 23, tzinfo=SneakyTZ()))


BEYOND_STUDY = (
    "from datetime import UTC, datetime\n\nfrom sqp.research.studies import BlindMetrics\n"
    + MOMENT
    + """

def run(pit, params):
    rows = pit.table("um-klines-1h", start=datetime(2026, 11, 1, tzinfo=UTC),
                     end=Moment(2026, 12, 31, tzinfo=UTC))
    latest = rows["knowable_time"].max().replace(microsecond=0)
    BETA_ETH = 0.014
    OBS_ETH = 1000
    values = """
    + VALUES_OK
    + """
    quality = """
    + QUALITY_OK
    + """
    quality["hourly_last"] = latest.isoformat()
    return BlindMetrics(values=values, quality=quality)
"""
)


def test_f10b_blind_study_reads_past_max_knowable_time(blind_env, tmp_path, monkeypatch):
    from sqp.data import build, vision

    root, store, _ref, _ = blind_env
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):  # fresh forward data after burned_through
        key = f"{vision.RAW_PREFIX}{tb.UM}{sym}/1h/{sym}-1h-2026-12.zip"
        store.put(key, tb._zipped_bars(sym, datetime(2026, 12, 1, tzinfo=UTC), 2))
    source = vision.Source(
        site="https://site.test/",
        listing="https://list.test/bucket",
        symbol_sets={},
        datasets=[{"id": tb.DATASET, "kind": "klines", "prefix": tb.UM + "{symbol}/1h/"}],
    )
    build.build(source, lambda: store, room=10**9)
    assert boundary._scan(STUDY_PATH, BEYOND_STUDY)
    pkg = _write_study(tmp_path, monkeypatch, "beyondpkg", BEYOND_STUDY)
    tb._append_blind_pair(root)  # max_knowable_time = 2026-11-18T00:00:00Z
    with pytest.raises(run.BlindTrialFailed) as exc_info:
        run.run_trial(root, store, "T-0001", package=pkg)
    assert isinstance(exc_info.value.__cause__, TypeError)
    assert not store.exists("derived/trials/T-0001/result.json")


# ---------------------------------------------------------------------------
# F1b str/int subclasses refused statically (dunder def) and at runtime (blind)
# ---------------------------------------------------------------------------
INJECT_STUDY = (
    """from sqp.research.studies import BlindMetrics


class Stamp(str):
    def __format__(self, spec):
        return "<<beta_binance=0.987654321 se=0.0123 sharpe_vault=2.5>>"


def run(pit, params):
    BETA_ETH = 0.014
    OBS_ETH = 1000
    values = """
    + VALUES_OK
    + """
    quality = """
    + QUALITY_OK
    + """
    quality["daily_first"] = Stamp("2023-01-02T16:00:00+00:00")
    return BlindMetrics(values=values, quality=quality)
"""
)


def test_f1b_str_subclass_injects_free_text_into_report(blind_env, tmp_path, monkeypatch):
    root, store, ref, ref_bytes = blind_env
    assert boundary._scan(STUDY_PATH, INJECT_STUDY)
    pkg = _write_study(tmp_path, monkeypatch, "injectpkg", INJECT_STUDY)
    tb._append_blind_pair(root)
    with pytest.raises(run.BlindTrialFailed) as exc_info:
        run.run_trial(root, store, "T-0001", package=pkg)
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert not store.exists("derived/trials/T-0001/report.md")

    # int subclasses are also refused by blind._strict_int().
    class SneakyInt(int):
        def __format__(self, spec):
            return "LEAKED"

    bad_int_q = {**tb._sample_quality(), "daily_rows": SneakyInt(1001)}
    with pytest.raises(ValueError, match="内置整数"):
        blind.compare_btc_eth_001(
            BlindMetrics(values=tb._sample_raw_metrics().values, quality=bad_int_q),
            ref_bytes,
            ref,
        )
