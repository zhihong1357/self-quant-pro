"""The first generation's recordings are archived as git bundles (decision 0024)."""

from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sqp import cli
from sqp.data import gen1
from sqp.storage import LocalStore

NOW = datetime.now(UTC).replace(microsecond=0)
HOUR_AGO = NOW - timedelta(hours=1)
BRANCHES = (
    gen1.Branch("someone/recorder", "main", ("data/gex",), 3),
    gen1.Branch("someone/recorder", "lab-data", (), 2),
)


def git(repo: Path, *args: str, when: datetime = HOUR_AGO) -> str:
    stamp = when.isoformat()
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    identity = ("-c", "user.name=test", "-c", "user.email=test@example.com")
    proc = subprocess.run(
        ["git", "-C", str(repo), *identity, *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.stdout.strip()


def record(repo: Path, path: str, text: str, when: datetime = HOUR_AGO) -> str:
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a", encoding="utf-8") as f:
        f.write(text)
    git(repo, "add", "-A", when=when)
    git(repo, "commit", "-qm", f"snapshot {when:%Y-%m-%d %H:%M}", when=when)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def clones(tmp_path: Path) -> Path:
    repo = tmp_path / "clones" / "recorder"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    record(repo, "README.md", "recorder\n")
    record(repo, "data/gex/BTC_options_2026-11-24.txt", "strike,iv\n")
    git(repo, "checkout", "-q", "--orphan", "lab-data")
    git(repo, "rm", "-rqf", ".")
    record(repo, "basis.txt", "time,basis\n")
    git(repo, "checkout", "-q", "main")
    return tmp_path / "clones"


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "store")


def run(clones: Path, store: LocalStore, run_id: str, **kw: object) -> gen1.Report:
    kw = {"room": 10**9, "now": NOW, **kw}
    return gen1.import_branches(BRANCHES, clones, store, run_id=run_id, **kw)  # type: ignore[arg-type]


def restore(store: LocalStore, keys: list[str], where: Path) -> Path:
    """Rebuild the branches from the stored bundles, the way decision 0024 says to."""
    target = where / "restore.git"
    subprocess.run(["git", "init", "-q", "--bare", str(target)], check=True)
    for i, key in enumerate(keys):
        bundle = where / f"{i}.bundle"
        bundle.write_bytes(store.get(key))
        git(target, "fetch", "-q", str(bundle), "+refs/*:refs/*")
    return target


def test_the_registered_branches_are_the_three_in_the_lineage() -> None:
    branches = gen1.load_branches(Path(__file__).resolve().parents[1])
    names = {b.name for b in branches}
    assert names == {"fakeout-backtest/main", "orderflow/gex-data", "orderflow/lab-data"}
    assert all(b.stale_after_days > 0 for b in branches)


def test_the_first_import_keeps_whole_branches(clones: Path, store: LocalStore) -> None:
    report = run(clones, store, "r1")
    assert report.alarms == []
    assert report.manifest == f"{gen1.MANIFEST_PREFIX}r1.jsonl"
    state = gen1.latest_state(store)
    assert set(state) == {"recorder/main", "recorder/lab-data"}
    main = state["recorder/main"]
    assert main["base"] is None and main["commits"] == 2 and not main["rewritten"]
    assert main["tip"] == git(clones / "recorder", "rev-parse", "main")
    assert main["key"] == f"{gen1.RAW_PREFIX}recorder/main/{main['tip']}-from-root.bundle"
    assert main["sha256"] == hashlib.sha256(store.get(main["key"])).hexdigest()


def test_later_imports_add_only_new_commits_and_restore_whole(
    clones: Path, store: LocalStore, tmp_path: Path
) -> None:
    run(clones, store, "r1")
    first = gen1.latest_state(store)
    tip = record(clones / "recorder", "data/gex/BTC_options_2026-11-25.txt", "strike,iv\n")
    run(clones, store, "r2")
    second = gen1.latest_state(store)
    assert second["recorder/main"]["base"] == first["recorder/main"]["tip"]
    assert second["recorder/main"]["commits"] == 1
    assert second["recorder/lab-data"] == first["recorder/lab-data"]  # carried forward
    keys = [first["recorder/main"]["key"], second["recorder/main"]["key"]]
    restored = restore(store, keys, tmp_path)
    assert git(restored, "rev-parse", "refs/heads/main") == tip


def test_nothing_new_writes_no_manifest(clones: Path, store: LocalStore) -> None:
    run(clones, store, "r1")
    report = run(clones, store, "r2")
    assert report.manifest is None
    assert store.keys(gen1.MANIFEST_PREFIX) == [f"{gen1.MANIFEST_PREFIX}r1.jsonl"]
    assert "recorder/main：没有新提交" in report.lines


def test_rewritten_history_is_kept_whole_and_raises_an_alarm(
    clones: Path, store: LocalStore
) -> None:
    run(clones, store, "r1")
    old = gen1.latest_state(store)["recorder/main"]
    git(clones / "recorder", "commit", "-q", "--amend", "-m", "rewritten")
    report = run(clones, store, "r2")
    main = gen1.latest_state(store)["recorder/main"]
    assert main["rewritten"] and main["base"] is None
    assert any("改写" in alarm for alarm in report.alarms)
    assert store.exists(old["key"])  # the old history is still there


def test_stale_data_raises_an_alarm(clones: Path, store: LocalStore) -> None:
    report = run(clones, store, "r1", now=NOW + timedelta(days=5))
    assert len(report.alarms) == 2
    assert all("天没有新数据" in alarm for alarm in report.alarms)


def test_staleness_looks_at_the_data_not_the_branch_tip(clones: Path, store: LocalStore) -> None:
    later = NOW + timedelta(days=3)
    repo = clones / "recorder"
    record(repo, "README.md", "code changed\n", when=later)  # a fresh commit without data
    git(repo, "checkout", "-q", "lab-data")
    record(repo, "basis.txt", "1,0.1\n", when=later)
    git(repo, "checkout", "-q", "main")
    report = run(clones, store, "r1", now=later + timedelta(hours=1))
    assert len(report.alarms) == 1 and report.alarms[0].startswith("recorder/main")


def test_an_upload_left_by_a_failed_run_is_reused(
    clones: Path, tmp_path: Path, store: LocalStore
) -> None:
    class ManifestFails(LocalStore):
        def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
            if key.startswith(gen1.MANIFEST_PREFIX):
                raise OSError("disk full")
            super().put(key, data, content_type)

    with pytest.raises(OSError):
        run(clones, ManifestFails(store.root), "r1")  # type: ignore[arg-type]
    report = run(clones, store, "r2")
    assert report.alarms == [] and report.manifest
    state = gen1.latest_state(store)
    assert len(store.keys(gen1.RAW_PREFIX)) == 2
    for record_ in state.values():
        assert record_["sha256"] == hashlib.sha256(store.get(record_["key"])).hexdigest()


def test_the_storage_limit_is_respected(clones: Path, store: LocalStore) -> None:
    report = run(clones, store, "r1", room=10)
    assert store.keys(gen1.RAW_PREFIX) == [] and report.manifest is None
    assert sum("超出存储上限" in alarm for alarm in report.alarms) == 2


def test_remote_branches_of_a_normal_clone_are_used(
    clones: Path, store: LocalStore, tmp_path: Path
) -> None:
    clone = tmp_path / "checkout" / "recorder"
    subprocess.run(["git", "clone", "-q", str(clones / "recorder"), str(clone)], check=True)
    run(clone.parent, store, "r1")
    main = gen1.latest_state(store)["recorder/main"]
    restored = restore(store, [main["key"]], tmp_path)
    assert git(restored, "rev-parse", "refs/remotes/origin/main") == main["tip"]


def test_shallow_and_missing_clones_are_refused(
    clones: Path, store: LocalStore, tmp_path: Path
) -> None:
    shallow = tmp_path / "shallow" / "recorder"
    url = (clones / "recorder").as_uri()
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", "--no-single-branch", url, str(shallow)], check=True
    )
    with pytest.raises(gen1.Gen1Error, match="浅克隆"):
        run(shallow.parent, store, "r1")
    with pytest.raises(gen1.Gen1Error, match="找不到克隆"):
        run(tmp_path / "nowhere", store, "r1")


def test_the_command_fails_on_alarms(
    clones: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "registry/sources").mkdir(parents=True)
    (repo_root / gen1.SOURCE_PATH).write_text(
        '[[branches]]\nrepo = "someone/recorder"\nbranch = "main"\npaths = ["data/gex"]\n'
        "stale_after_days = 3\n",
        encoding="utf-8",
    )
    args = ["data", "gen1", "--repo", str(repo_root)]
    assert cli.main([*args, "--local", str(tmp_path / "s1"), "--clones", str(clones)]) == 0
    stale = tmp_path / "stale" / "recorder"
    stale.mkdir(parents=True)
    git(stale, "init", "-q", "-b", "main")
    record(stale, "data/gex/a.txt", "x\n", when=NOW - timedelta(days=9))
    assert cli.main([*args, "--local", str(tmp_path / "s2"), "--clones", str(stale.parent)]) == 1
    assert "报警：recorder/main：已经 9 天没有新数据" in capsys.readouterr().out
