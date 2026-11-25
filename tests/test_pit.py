"""Point-in-time reads: no row after `end`; segment guards; refusals are recorded.

These are the G1 acceptance tests for look-ahead and the vault guard, plus the
gaps an independent review found (decision 0022).
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import zipfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sqp import ledger
from sqp.data import build, vision
from sqp.data.pit import APPROVALS, REFUSALS, REFUSALS_PREFIX, AccessRefused, PointInTime
from sqp.storage import LocalStore

ROOT = Path(__file__).resolve().parents[1]
H = 3_600_000
UM = "data/futures/um/monthly/klines/"
SOURCE = vision.Source(
    site="https://site.test/",
    listing="https://list.test/bucket",
    symbol_sets={},
    datasets=[{"id": "um-klines-1h", "kind": "klines", "prefix": UM + "{symbol}/1h/"}],
)
DATASET = "um-klines-1h"


def ms(*args: int) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp() * 1000)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def zipped_bars(start_ms: int, n: int) -> bytes:
    rows = "".join(
        f"{start_ms + i * H},1,1,1,1,1,{start_ms + (i + 1) * H - 1},1,1,1,1,0\n" for i in range(n)
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("BTCUSDT.csv", rows)
    return buffer.getvalue()


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    """Two bars on each side of every segment edge."""
    lake = LocalStore(tmp_path / "lake")
    for month, start in {
        "2024-01": ms(2024, 1, 31, 22),
        "2024-02": ms(2024, 2, 1, 0),
        "2025-10": ms(2025, 10, 31, 22),
        "2025-11": ms(2025, 11, 1, 0),
        "2026-10": ms(2026, 10, 31, 22),
        "2026-11": ms(2026, 11, 1, 0),
    }.items():
        key = f"{vision.RAW_PREFIX}{UM}BTCUSDT/1h/BTCUSDT-1h-{month}.zip"
        lake.put(key, zipped_bars(start, 2))
    build.build(SOURCE, lambda: lake, room=10**9)
    return lake


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def merge(root: Path) -> None:
    """Commit everything and call it origin/main, as if the owner had merged it."""
    git(root, "add", "-A")
    git(root, "commit", "-qm", "merge", "--allow-empty")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "registry" / "universes").mkdir(parents=True)
    (root / "registry" / "ledger").mkdir(parents=True)
    shutil.copy(ROOT / "registry/universes/U1.toml", root / "registry/universes/U1.toml")
    (root / APPROVALS).write_text("")
    (root / REFUSALS).write_text("")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "test")
    merge(root)
    return root


@pytest.fixture
def actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend to run in GitHub Actions on main."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")


def register(root: Path, hypothesis: str, status: str = "registered", universe: str = "U1") -> None:
    (root / "registry" / "hypotheses").mkdir(exist_ok=True)
    card = f'status = "{status}"\nuniverse = "{universe}"\n'
    (root / "registry" / "hypotheses" / f"{hypothesis}.toml").write_text(card)


def approve(root: Path, hypothesis: str, kind: str = "vault") -> None:
    ledger.append(root / APPROVALS, {"type": kind, "universe": "U1", "hypothesis": hypothesis})


def refusals(root: Path) -> list[str]:
    text = (root / REFUSALS).read_text()
    assert ledger.verify(text) == []
    return [line for line in text.splitlines() if line]


# --- look-ahead ---------------------------------------------------------------


def test_no_row_knowable_after_end(repo: Path, store: LocalStore) -> None:
    rows = PointInTime(repo, store).table(DATASET, end=utc(2024, 1, 31, 23, 30))
    assert rows.height == 1  # the 22:00 bar; the 23:00 bar only closes at midnight
    assert (rows["knowable_time"] <= utc(2024, 1, 31, 23, 30)).all()


def test_the_last_microsecond_of_exploration_is_still_exploration(
    repo: Path, store: LocalStore
) -> None:
    end = utc(2024, 2, 1) - timedelta(microseconds=1)
    assert PointInTime(repo, store).table(DATASET, end=end).height == 1
    assert refusals(repo) == []


def test_a_row_belongs_to_the_segment_in_which_it_became_knowable(
    repo: Path, store: LocalStore
) -> None:
    # The 23:00 bar of 31 January becomes knowable at 1 February 00:00: confirmation.
    with pytest.raises(AccessRefused):
        PointInTime(repo, store).table(DATASET, end=utc(2024, 2, 1))
    assert len(refusals(repo)) == 1


def test_times_must_carry_a_time_zone_and_run_forwards(repo: Path, store: LocalStore) -> None:
    pit = PointInTime(repo, store)
    with pytest.raises(ValueError):
        pit.table(DATASET, end=datetime(2024, 1, 1))
    with pytest.raises(ValueError):
        pit.table(DATASET, end=utc(2024, 1, 1), start=datetime(2023, 1, 1))
    with pytest.raises(ValueError):
        pit.table(DATASET, end=utc(2023, 1, 1), start=utc(2024, 1, 1))


def test_other_time_zones_are_converted_to_utc(
    repo: Path, store: LocalStore, actions: None
) -> None:
    register(repo, "H-0001")
    merge(repo)
    # 31 January 20:00 at UTC-8 is 1 February 04:00 UTC: the February table must be read.
    end = datetime(2024, 1, 31, 20, tzinfo=timezone(timedelta(hours=-8)))
    rows = PointInTime(repo, store, purpose="H-0001").table(DATASET, end=end)
    assert rows["knowable_time"].max() == utc(2024, 2, 1, 2)


# --- where the rules come from ------------------------------------------------


def test_segments_come_from_main_not_the_working_tree(repo: Path, store: LocalStore) -> None:
    universe = repo / "registry/universes/U1.toml"
    universe.write_text(universe.read_text().replace("2024-01-31", "2099-12-31"))  # not merged
    with pytest.raises(AccessRefused):
        PointInTime(repo, store).table(DATASET, end=utc(2025, 11, 1, 2))


def test_without_the_universe_on_main_nothing_is_read(repo: Path, store: LocalStore) -> None:
    with pytest.raises(AccessRefused):
        PointInTime(repo, store, universe="U9")
    with pytest.raises(ValueError):
        PointInTime(repo, store, universe="../U1")
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(AccessRefused):
        PointInTime(repo, store)


# --- confirmation and forward -------------------------------------------------


def test_guarded_reads_run_only_in_github_actions_on_main(repo: Path, store: LocalStore) -> None:
    register(repo, "H-0001")
    merge(repo)
    with pytest.raises(AccessRefused, match="GitHub Actions"):
        PointInTime(repo, store, purpose="H-0001").table(DATASET, end=utc(2024, 2, 1, 2))


def test_confirmation_needs_a_merged_registration(
    repo: Path, store: LocalStore, actions: None
) -> None:
    pit = PointInTime(repo, store, purpose="H-0001")
    register(repo, "H-0001")  # in the working tree only
    with pytest.raises(AccessRefused, match="已合并注册"):
        pit.table(DATASET, end=utc(2024, 2, 1, 2))
    merge(repo)
    rows = pit.table(DATASET, end=utc(2024, 2, 1, 2), start=utc(2024, 1, 31, 23))
    assert rows.height == 4  # knowable at 23:00, 00:00, 01:00 and 02:00; both ends inclusive


@pytest.mark.parametrize(
    ("status", "universe"),
    [("draft", "U1"), ("rejected", "U1"), ("retired", "U1"), ("registered", "U2")],
)
def test_only_live_cards_of_this_universe_unlock(
    repo: Path, store: LocalStore, actions: None, status: str, universe: str
) -> None:
    register(repo, "H-0001", status=status, universe=universe)
    merge(repo)
    with pytest.raises(AccessRefused):
        PointInTime(repo, store, purpose="H-0001").table(DATASET, end=utc(2024, 2, 1, 2))


def test_forward_alone_needs_a_registration_but_no_vault_approval(
    repo: Path, store: LocalStore, actions: None
) -> None:
    register(repo, "H-0001")
    merge(repo)
    pit = PointInTime(repo, store, purpose="H-0001")
    rows = pit.table(DATASET, start=utc(2026, 11, 1, 1), end=utc(2026, 11, 1, 2))
    assert rows.height == 2
    with pytest.raises(AccessRefused, match="批准"):  # this window crosses the vault
        pit.table(DATASET, start=utc(2025, 10, 31, 23), end=utc(2026, 11, 1, 2))


# --- the vault ----------------------------------------------------------------


def test_vault_needs_a_merged_owner_approval(repo: Path, store: LocalStore, actions: None) -> None:
    register(repo, "H-0001")
    merge(repo)
    pit = PointInTime(repo, store, purpose="H-0001")
    with pytest.raises(AccessRefused, match="批准"):
        pit.table(DATASET, end=utc(2025, 11, 1, 2))
    approve(repo, "H-0001")
    with pytest.raises(AccessRefused):  # appended locally, not merged yet
        pit.table(DATASET, end=utc(2025, 11, 1, 2))
    merge(repo)
    rows = pit.table(DATASET, end=utc(2025, 11, 1, 2), start=utc(2025, 10, 31, 23))
    assert rows.height == 4
    assert len(refusals(repo)) == 2


@pytest.mark.parametrize("case", ["other hypothesis", "other type", "broken chain"])
def test_approvals_that_do_not_count(
    repo: Path, store: LocalStore, actions: None, case: str
) -> None:
    register(repo, "H-0001")
    register(repo, "H-0002")
    if case == "other hypothesis":
        approve(repo, "H-0002")
    elif case == "other type":
        approve(repo, "H-0001", kind="confirmation")
    else:
        approve(repo, "H-0001")
        path = repo / APPROVALS
        path.write_text(path.read_text().replace('"U1"', '"U2"'))  # edit a sealed record
    merge(repo)
    with pytest.raises(AccessRefused):
        PointInTime(repo, store, purpose="H-0001").table(DATASET, end=utc(2025, 11, 2))


def test_refusals_are_also_kept_in_the_store(repo: Path, store: LocalStore) -> None:
    with pytest.raises(AccessRefused):
        PointInTime(repo, store).table(DATASET, end=utc(2025, 11, 2))
    (key,) = store.keys(REFUSALS_PREFIX)
    assert json.loads(store.get(key))["end"].startswith("2025-11-02")


# --- reading the tables -------------------------------------------------------


def test_uncommitted_parts_are_not_read(repo: Path, store: LocalStore) -> None:
    for key in store.keys(build.DATASET_PREFIX):
        if key.endswith(".inputs.json") and "2024-01" in key:
            (store.root / key).unlink()  # simulate a part whose build never committed
    assert PointInTime(repo, store).table(DATASET, end=utc(2024, 1, 31, 23)).height == 0


def test_part_numbers_past_99_are_read(repo: Path, store: LocalStore) -> None:
    stem = build.month_prefix(DATASET, "2024-01")
    for ext in (".parquet", ".inputs.json"):
        store.put(f"{stem}.r100{ext}", store.get(f"{stem}.r01{ext}"))
    rows = PointInTime(repo, store).table(DATASET, end=utc(2024, 1, 31, 23, 30))
    assert rows.height == 2  # the same bar from r01 and r100


def test_cached_reads_fetch_each_object_once_and_fingerprint_the_bytes(
    repo: Path, store: LocalStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    original = store.get
    monkeypatch.setattr(store, "get", lambda key: calls.append(key) or original(key))
    cache = tmp_path / "cache"
    pit = PointInTime(repo, store, cache_dir=cache)
    for _ in range(2):
        pit.table(DATASET, end=utc(2024, 1, 31, 23, 30))
    assert len(calls) == len(set(calls)) == 1
    clean = dict(pit.inputs)
    (key,) = clean
    (cache / key).write_bytes(original(f"{build.month_prefix(DATASET, '2024-02')}.r01.parquet"))
    tampered = PointInTime(repo, store, cache_dir=cache)
    tampered.table(DATASET, end=utc(2024, 1, 31, 23, 30))
    assert tampered.inputs[key] != clean[key]  # a swapped cache file changes the fingerprint
