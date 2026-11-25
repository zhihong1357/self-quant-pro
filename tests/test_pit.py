"""Point-in-time reads: no row after `end`; segment guards; refusals are logged.

These are the G1 acceptance tests for look-ahead and the vault guard.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sqp import ledger
from sqp.data import build, vision
from sqp.data.pit import APPROVALS, REFUSALS, AccessRefused, PointInTime
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
        z.writestr("f.csv", rows)
    return buffer.getvalue()


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    """Two bars on each side of the exploration/confirmation and confirmation/vault edges."""
    lake = LocalStore(tmp_path / "lake")
    for month, start in {
        "2024-01": ms(2024, 1, 31, 22),
        "2024-02": ms(2024, 2, 1, 0),
        "2025-10": ms(2025, 10, 31, 22),
        "2025-11": ms(2025, 11, 1, 0),
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
    git(root, "commit", "-qm", "merge")
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


def register(root: Path, hypothesis: str) -> None:
    (root / "registry" / "hypotheses").mkdir(exist_ok=True)
    (root / "registry" / "hypotheses" / f"{hypothesis}.toml").write_text('status = "registered"\n')


def refusals(root: Path) -> list[str]:
    text = (root / REFUSALS).read_text()
    assert ledger.verify(text) == []
    return [line for line in text.splitlines() if line]


def test_no_row_knowable_after_end(repo: Path, store: LocalStore) -> None:
    pit = PointInTime(repo, store)
    rows = pit.table("um-klines-1h", end=utc(2024, 1, 31, 23, 30))
    assert rows.height == 1  # the 22:00 bar; the 23:00 bar only closes at midnight
    assert (rows["knowable_time"] <= utc(2024, 1, 31, 23, 30)).all()


def test_a_row_belongs_to_the_segment_in_which_it_became_knowable(
    repo: Path, store: LocalStore
) -> None:
    pit = PointInTime(repo, store)
    # The 23:00 bar of 31 January becomes knowable at 1 February 00:00: confirmation.
    with pytest.raises(AccessRefused):
        pit.table("um-klines-1h", end=utc(2024, 2, 1))
    assert len(refusals(repo)) == 1


def test_confirmation_needs_a_merged_registration(repo: Path, store: LocalStore) -> None:
    pit = PointInTime(repo, store, purpose="H-0001")
    register(repo, "H-0001")  # in the working tree only
    with pytest.raises(AccessRefused, match="已合并注册"):
        pit.table("um-klines-1h", end=utc(2024, 2, 1, 2))
    merge(repo)
    rows = pit.table("um-klines-1h", end=utc(2024, 2, 1, 2), start=utc(2024, 1, 31, 23))
    assert rows.height == 4  # knowable at 23:00, 00:00, 01:00 and 02:00; both ends inclusive


def test_vault_needs_a_merged_owner_approval(repo: Path, store: LocalStore) -> None:
    register(repo, "H-0001")
    merge(repo)
    pit = PointInTime(repo, store, purpose="H-0001")
    with pytest.raises(AccessRefused, match="批准"):
        pit.table("um-klines-1h", end=utc(2025, 11, 1, 2))
    approval = {"type": "vault", "universe": "U1", "hypothesis": "H-0001"}
    ledger.append(repo / APPROVALS, approval)
    with pytest.raises(AccessRefused):  # appended locally, not merged yet
        pit.table("um-klines-1h", end=utc(2025, 11, 1, 2))
    merge(repo)
    rows = pit.table("um-klines-1h", end=utc(2025, 11, 1, 2), start=utc(2025, 10, 31, 23))
    assert rows.height == 4
    assert len(refusals(repo)) == 2


def test_an_approval_for_another_hypothesis_does_not_count(repo: Path, store: LocalStore) -> None:
    register(repo, "H-0001")
    register(repo, "H-0002")
    ledger.append(repo / APPROVALS, {"type": "vault", "universe": "U1", "hypothesis": "H-0002"})
    merge(repo)
    with pytest.raises(AccessRefused):
        PointInTime(repo, store, purpose="H-0001").table("um-klines-1h", end=utc(2025, 11, 2))


def test_times_must_carry_a_time_zone(repo: Path, store: LocalStore) -> None:
    with pytest.raises(ValueError):
        PointInTime(repo, store).table("um-klines-1h", end=datetime(2024, 1, 1))


def test_uncommitted_parts_are_not_read(repo: Path, store: LocalStore) -> None:
    for key in store.keys(build.DATASET_PREFIX):
        if key.endswith(".inputs.json") and "2024-01" in key:
            (store.root / key).unlink()  # simulate a part whose build never committed
    assert PointInTime(repo, store).table("um-klines-1h", end=utc(2024, 1, 31, 23)).height == 0


def test_cached_reads_fetch_each_object_once(
    repo: Path, store: LocalStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    original = store.get
    monkeypatch.setattr(store, "get", lambda key: calls.append(key) or original(key))
    pit = PointInTime(repo, store, cache_dir=tmp_path / "cache")
    for _ in range(2):
        pit.table("um-klines-1h", end=utc(2024, 1, 31, 23, 30))
    assert len(calls) == len(set(calls)) == 1
