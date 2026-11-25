"""Trial cards and the runner (decision 0025): registered on main first, run once."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import UNIVERSE
from sqp import ledger
from sqp.data import build, vision
from sqp.data.pit import AccessRefused
from sqp.research import run, trials
from sqp.storage import LocalStore

ROOT = Path(__file__).resolve().parents[1]
STUDY_FILE = "src/sqp/research/studies/probe.py"

# --- the trial-card check ------------------------------------------------------

CARD = """id = "{id}"
title = "测试"
family = "crypto.test"
universe = "{universe}"
status = "draft"
created = 2026-11-25
mechanism = "测试"
predictions = ["测试"]
falsification = "测试"
"""

REGISTERED = """
[registration]
registered_at = 2026-11-25
segments = ["confirmation"]
min_effect_sharpe = 3.0
test_plan = "测试"
literature_exposure = "测试"

[thresholds]
t_stat = 3.0
deflated_sharpe = 0.95
pbo = 0.10
cost_multiplier = 2.0
"""


def card_text(hid: str, universe: str = "U9", registered: bool = False) -> str:
    text = CARD.format(id=hid, universe=universe)
    if registered:
        text = text.replace('status = "draft"', 'status = "registered"') + REGISTERED
    return text


def trial(**overrides) -> dict:
    record = {
        "type": "trial",
        "hypothesis": "H-0001",
        "study": "probe",
        "segment": "exploration",
        "params": {"mode": "ok"},
        "note": "测试",
    }
    record.update(overrides)
    return {k: v for k, v in record.items() if v is not None}


def ledger_text(*records: dict) -> str:
    text = ""
    for record in records:
        text += ledger.canonical(ledger.build(text, record)).decode() + "\n"
    return text


@pytest.fixture
def cards(sandbox):
    sandbox.write("registry/universes/U9.toml", UNIVERSE)
    sandbox.write("registry/hypotheses/H-0001.toml", card_text("H-0001"))
    sandbox.write("registry/hypotheses/H-0002.toml", card_text("H-0002", registered=True))
    sandbox.write("registry/hypotheses/H-0003.toml", card_text("H-0003", universe="synthetic"))
    sandbox.write(STUDY_FILE, "x = 1\n")
    sandbox.write("registry/ledger/approvals.jsonl", "")
    return sandbox


def card_problems(sandbox, *records: dict) -> list[str]:
    sandbox.write(trials.TRIALS, ledger_text(*records))
    return [v.message for v in sandbox.check() if v.rule == "trial-card"]


def test_well_formed_cards_pass(cards):
    assert cards.rules() == set()
    good = [
        trial(),
        trial(hypothesis="H-0002", segment="confirmation"),
        trial(hypothesis="H-0003", segment="synthetic"),
        trial(repeat_of="T-0001"),
    ]
    assert card_problems(cards, *good) == []


@pytest.mark.parametrize(
    ("record", "needle"),
    [
        (trial(study="nothere"), "不存在"),
        (trial(study="Bad-Name"), "小写字母"),
        (trial(hypothesis="H-0009"), "假说卡"),
        (trial(note=" "), "note"),
        (trial(params=[1]), "params"),
        (trial(segment="later"), "segment"),
        (trial(extra=1), "不认识"),
        (trial(segment="confirmation"), "草稿"),
        (trial(hypothesis="H-0002", segment="vault"), "注册时没有计划"),
        (trial(segment="synthetic"), "合成数据"),
        (trial(hypothesis="H-0003"), "合成数据"),
        (trial(approval=1), "只有金库段"),
        (trial(repeat_of="T-0005"), "不在它前面"),
    ],
)
def test_cards_that_cannot_run_as_written_are_refused(cards, record, needle):
    found = card_problems(cards, record)
    assert any(needle in message for message in found), found


def test_one_confirmation_per_hypothesis(cards):
    twice = trial(hypothesis="H-0002", segment="confirmation")
    found = card_problems(cards, twice, twice)
    assert found == ["T-0002：H-0002 已经有确认检验 T-0001：每个假说只做一次"]


def test_a_repeat_must_be_identical_and_never_on_guarded_segments(cards):
    first = trial(hypothesis="H-0002", segment="confirmation")
    again = trial(hypothesis="H-0002", segment="confirmation", repeat_of="T-0001")
    assert any("不能重跑" in m for m in card_problems(cards, first, again))
    other = trial(params={"mode": "other"}, repeat_of="T-0001")
    assert any("params" in m for m in card_problems(cards, trial(), other))


def test_the_vault_needs_the_owners_approval_by_seq(cards):
    vault = card_text("H-0002", registered=True).replace('["confirmation"]', '["vault"]')
    cards.write("registry/hypotheses/H-0002.toml", vault)
    record = trial(hypothesis="H-0002", segment="vault", approval=1)
    assert any("approval" in m for m in card_problems(cards, record))
    approval = {"type": "vault", "universe": "U9", "hypothesis": "H-0002"}
    cards.write("registry/ledger/approvals.jsonl", ledger_text(approval))
    assert card_problems(cards, record) == []


def test_trials_count_once_registered_plus_inherited():
    def read(path: str) -> str | None:
        return {
            "registry/hypotheses/H-0001.toml": card_text("H-0001", universe="U1"),
            "registry/hypotheses/H-0003.toml": card_text("H-0003", universe="synthetic"),
            "registry/universes/U1.toml": 'domain = "crypto"\n',
            trials.INHERITED: json.dumps({"summary": {"counted_by_domain": {"crypto": 9}}}),
        }.get(path)

    text = ledger_text(
        trial(),
        trial(repeat_of="T-0001"),
        trial(hypothesis="H-0003", segment="synthetic"),
        trial(params={"mode": "crash"}),
    )
    assert trials.counted(text, read, "U1") == 9 + 2


# --- the runner ----------------------------------------------------------------

PROBE = """
from sqp.research.studies import SECTIONS, Outcome
from datetime import datetime, timezone


def run(pit, params):
    mode = params["mode"]
    if mode == "crash":
        raise ValueError("a message with a number 1.2345")
    if mode == "peek":
        pit.table("um-klines-1h", end=datetime(2024, 3, 1, tzinfo=timezone.utc))
    rows = 0
    if pit is not None:
        end = datetime(2024, 1, 31, 23, 30, tzinfo=timezone.utc)
        rows = pit.table("um-klines-1h", end=end).height
    sections = {s: "测试" for s in SECTIONS}
    if mode == "short":
        sections.pop("稳健性")
    return Outcome(metrics={"rows": rows}, sections=sections)
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _merge(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "merge", "--allow-empty")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")


@pytest.fixture
def studies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    package = tmp_path / "pkg" / "probestudies"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "probe.py").write_text(PROBE)
    monkeypatch.syspath_prepend(str(tmp_path / "pkg"))
    yield "probestudies"
    for name in [m for m in sys.modules if m.startswith("probestudies")]:
        del sys.modules[name]


@pytest.fixture
def lake(tmp_path: Path) -> LocalStore:
    store = LocalStore(tmp_path / "lake")
    um = "data/futures/um/monthly/klines/"
    source = vision.Source(
        site="https://site.test/",
        listing="https://list.test/bucket",
        symbol_sets={},
        datasets=[{"id": "um-klines-1h", "kind": "klines", "prefix": um + "{symbol}/1h/"}],
    )
    for month, start in {
        "2024-01": datetime(2024, 1, 31, 22),
        "2024-02": datetime(2024, 2, 1),
    }.items():
        first = int(start.replace(tzinfo=UTC).timestamp() * 1000)
        hour = 3_600_000
        rows = "".join(
            f"{first + i * hour},1,1,1,1,1,{first + (i + 1) * hour - 1},1,1,1,1,0\n"
            for i in range(2)
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("BTCUSDT.csv", rows)
        store.put(f"{vision.RAW_PREFIX}{um}BTCUSDT/1h/BTCUSDT-1h-{month}.zip", buffer.getvalue())
    build.build(source, lambda: store, room=10**9)
    return store


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    for sub in (
        "registry/universes",
        "registry/ledger",
        "registry/hypotheses",
        "registry/inherited",
    ):
        (root / sub).mkdir(parents=True)
    shutil.copy(ROOT / "registry/universes/U1.toml", root / "registry/universes/U1.toml")
    shutil.copy(ROOT / trials.INHERITED, root / trials.INHERITED)
    (root / "registry/hypotheses/H-0001.toml").write_text(card_text("H-0001", universe="U1"))
    (root / "registry/hypotheses/H-0002.toml").write_text(
        card_text("H-0002", universe="U1", registered=True)
    )
    (root / "registry/hypotheses/H-0003.toml").write_text(card_text("H-0003", universe="synthetic"))
    (root / "src/sqp/research/studies").mkdir(parents=True)
    (root / STUDY_FILE).write_text("x = 1\n")
    for name in ("approvals.jsonl", "refusals.jsonl", "trials.jsonl"):
        (root / "registry/ledger" / name).write_text("")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    _merge(root)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    return root


def register(root: Path, *records: dict) -> None:
    for record in records:
        ledger.append(root / trials.TRIALS, record)
    _merge(root)


def test_a_registered_trial_runs_once_and_stores_only_fingerprints_in_the_ledger(
    repo, lake, studies
):
    register(repo, trial())
    assert run.pending(repo, lake) == ["T-0001"]
    done = run.run_trial(repo, lake, "T-0001", package=studies)
    assert done.alarms == []
    assert run.pending(repo, lake) == []
    report = lake.get("derived/trials/T-0001/report.md").decode()
    assert "## 9. 适用范围与停止条件" in report
    assert "累计试验次数（含继承，含本次）：10" in report  # 9 inherited crypto trials
    completed = json.loads(lake.get(run.completed_key("T-0001")))
    assert completed["result_sha256"] in "\n".join(done.lines)
    assert "rows" not in "\n".join(done.lines)  # no numbers in the log
    with pytest.raises(run.TrialRefused, match="已经开始过"):
        run.run_trial(repo, lake, "T-0001", package=studies)


def test_only_cards_merged_on_main_run(repo, lake, studies):
    ledger.append(repo / trials.TRIALS, trial())  # appended but not merged
    with pytest.raises(run.TrialRefused, match="先开 PR 登记"):
        run.run_trial(repo, lake, "T-0001", package=studies)


def test_only_in_actions_on_main(repo, lake, studies, monkeypatch):
    register(repo, trial())
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    with pytest.raises(run.TrialRefused, match="GitHub Actions"):
        run.run_trial(repo, lake, "T-0001", package=studies)
    assert not lake.exists(run.started_key("T-0001"))  # refused before the marker


def test_an_exploration_trial_cannot_read_past_exploration_even_if_registered(repo, lake, studies):
    register(repo, trial(hypothesis="H-0002", params={"mode": "peek"}))
    with pytest.raises(AccessRefused):
        run.run_trial(repo, lake, "T-0001", package=studies)
    assert lake.exists(run.started_key("T-0001"))  # it started, so it counts


def test_a_crash_after_starting_counts_and_keeps_numbers_out(repo, lake, studies):
    register(repo, trial(params={"mode": "crash"}))
    with pytest.raises(ValueError):
        run.run_trial(repo, lake, "T-0001", package=studies)
    failed = lake.get("ledger/trials/T-0001.failed.json").decode()
    assert "ValueError" in failed and "1.2345" not in failed
    assert run.pending(repo, lake) == []  # a new card is needed to try again


def test_an_incomplete_report_is_an_error(repo, lake, studies):
    register(repo, trial(params={"mode": "short"}))
    with pytest.raises(ValueError, match="稳健性"):
        run.run_trial(repo, lake, "T-0001", package=studies)


def test_a_confirmation_marker_is_written_before_any_read(repo, lake, studies):
    register(repo, trial(hypothesis="H-0002", segment="confirmation"))
    marker = "ledger/confirmations/U1/H-0002.json"
    lake.put(marker, b"{}")  # as if an earlier run had confirmed it
    with pytest.raises(run.TrialRefused, match="只做一次"):
        run.run_trial(repo, lake, "T-0001", package=studies)
    assert not lake.exists(run.started_key("T-0001"))


def test_a_confirmation_trial_reads_with_the_registered_purpose(repo, lake, studies):
    register(repo, trial(hypothesis="H-0002", segment="confirmation", params={"mode": "peek"}))
    run.run_trial(repo, lake, "T-0001", package=studies)
    assert lake.exists("ledger/confirmations/U1/H-0002.json")


def test_a_repeat_with_the_same_fingerprints_is_reported_reproducible(repo, lake, studies):
    register(repo, trial())
    run.run_trial(repo, lake, "T-0001", package=studies)
    register(repo, trial(repeat_of="T-0001"))
    done = run.run_trial(repo, lake, "T-0002", package=studies)
    assert done.alarms == [] and any("重算一致" in line for line in done.lines)


def test_a_synthetic_trial_gets_no_data_interface(repo, lake, studies):
    register(repo, trial(hypothesis="H-0003", segment="synthetic"))
    run.run_trial(repo, lake, "T-0001", package=studies)
    assert "累计试验次数" not in lake.get("derived/trials/T-0001/report.md").decode()


def test_a_broken_card_on_main_stops_every_run(repo, lake, studies):
    register(repo, trial(study="nothere"))
    with pytest.raises(run.TrialRefused, match="试验卡有问题"):
        run.pending(repo, lake)
