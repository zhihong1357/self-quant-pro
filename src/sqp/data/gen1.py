"""Archive the first generation's recordings (decision 0024).

The first generation still records, every day, data that cannot be fetched again
(docs/lineage.md section 2). This module copies the git history of those branches
into the store as git bundles: the first bundle of a branch holds its whole
history, later ones only the commits added since the last import. Nothing is
parsed here; research tables come later, when a hypothesis needs them.

The clones are made by the workflow with a read-only token; this module only reads
them. To restore a branch, fetch its bundles in manifest order into a bare repo:

    git init --bare restore.git
    git -C restore.git fetch <bundle> '+refs/*:refs/*'
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqp import storage

SOURCE_PATH = "registry/sources/gen1.toml"
RAW_PREFIX = "raw/gen1/"
MANIFEST_PREFIX = "manifests/gen1/"


class Gen1Error(Exception):
    """A clone is missing or unusable."""


@dataclass(frozen=True)
class Branch:
    repo: str  # owner/name on GitHub
    branch: str
    paths: tuple[str, ...]  # the part that cannot be fetched again; empty means all of it
    stale_after_days: int

    @property
    def clone_name(self) -> str:
        return self.repo.rsplit("/", 1)[-1]

    @property
    def name(self) -> str:
        return f"{self.clone_name}/{self.branch}"


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    alarms: list[str] = field(default_factory=list)
    manifest: str | None = None


def load_branches(repo_root: Path) -> tuple[Branch, ...]:
    with (repo_root / SOURCE_PATH).open("rb") as f:
        source = tomllib.load(f)
    return tuple(
        Branch(b["repo"], b["branch"], tuple(b.get("paths", ())), int(b["stale_after_days"]))
        for b in source["branches"]
    )


def _git(clone: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise Gen1Error(f"git {args[0]} 失败（{clone}）：{proc.stderr.strip()}")
    return proc.stdout.strip()


def _ok(clone: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(clone), *args], capture_output=True).returncode == 0


def _ref(clone: Path, branch: str) -> str:
    """The branch's full name: a remote branch after actions/checkout, a local one otherwise."""
    if not clone.is_dir():
        raise Gen1Error(f"找不到克隆：{clone}")
    if _git(clone, "rev-parse", "--is-shallow-repository") != "false":
        raise Gen1Error(f"{clone} 是浅克隆，缺历史；工作流里要设 fetch-depth: 0")
    for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
        if _ok(clone, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
            return ref
    raise Gen1Error(f"{clone} 里找不到分支 {branch}")


def latest_state(store: storage.Store) -> dict[str, dict]:
    """Where each branch stood after the last import; the newest manifest lists all of them."""
    keys = store.keys(MANIFEST_PREFIX)
    if not keys:
        return {}
    lines = store.get(keys[-1]).decode("utf-8").splitlines()
    return {r["name"]: r for r in (json.loads(line) for line in lines if line)}


def _bundle(clone: Path, revs: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "branch.bundle"
        _git(clone, "bundle", "create", str(out), revs)
        return out.read_bytes()


def _verify(clone: Path, data: bytes) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "branch.bundle"
        out.write_bytes(data)
        _git(clone, "bundle", "verify", str(out))


def _put(store: storage.Store, key: str, data: bytes) -> bytes:
    try:
        store.put(key, data, content_type="application/octet-stream")
        return data
    except storage.ObjectExists:
        # An earlier run stored the same range and stopped before its manifest.
        return store.get(key)


def _freshness(clone: Path, ref: str, branch: Branch, now: datetime) -> tuple[str, str | None]:
    """A line saying when data last arrived, and an alarm if that was too long ago."""
    last = _git(clone, "log", "-1", "--format=%cI", ref, "--", *branch.paths)
    if not last:
        return (
            f"{branch.name}：找不到数据的提交",
            f"{branch.name}：{list(branch.paths)} 没有任何提交",
        )
    when = datetime.fromisoformat(last).astimezone(UTC)
    line = f"{branch.name}：最近一次数据 {when:%Y-%m-%d %H:%M} UTC"
    age = now - when
    if age <= timedelta(days=branch.stale_after_days):
        return line, None
    alarm = (
        f"{branch.name}：已经 {age.days} 天没有新数据（最近一次 {when:%Y-%m-%d %H:%M} UTC），"
        f"去看 {branch.repo} 的录制任务"
    )
    return line, alarm


def import_branches(
    branches: Iterable[Branch],
    clones: Path,
    store: storage.Store,
    *,
    run_id: str,
    room: int,
    now: datetime,
) -> Report:
    """Store a bundle of what each branch gained since the last import, then a manifest
    of where every branch stands. Alarms: stale data, rewritten history, no room."""
    report = Report()
    state = latest_state(store)
    changed = False
    for branch in branches:
        clone = clones / branch.clone_name
        ref = _ref(clone, branch.branch)
        tip = _git(clone, "rev-parse", ref)
        prev = state.get(branch.name)
        base = prev["tip"] if prev else None
        if base == tip:
            report.lines.append(f"{branch.name}：没有新提交")
        else:
            rewritten = base is not None and not _ok(
                clone, "merge-base", "--is-ancestor", base, tip
            )
            if rewritten:
                report.alarms.append(
                    f"{branch.name}：上次导入到 {base[:12]}，它已经不在现在的历史里，"
                    "说明历史被改写过。这次另存一份完整的历史，旧的照样保留"
                )
                base = None
            revs = f"{base}..{ref}" if base else ref
            data = _bundle(clone, revs)
            if len(data) > room:
                report.alarms.append(
                    f"{branch.name}：要存 {len(data):,} 字节，超出存储上限，这次没存"
                )
            else:
                key = f"{RAW_PREFIX}{branch.name}/{tip}-from-{base or 'root'}.bundle"
                stored = _put(store, key, data)
                _verify(clone, stored)
                room -= len(stored)
                commits = int(_git(clone, "rev-list", "--count", revs))
                state[branch.name] = {
                    "name": branch.name,
                    "repo": branch.repo,
                    "branch": branch.branch,
                    "tip": tip,
                    "tip_time": _git(clone, "show", "-s", "--format=%cI", tip),
                    "base": base,
                    "commits": commits,
                    "rewritten": rewritten,
                    "key": key,
                    "bytes": len(stored),
                    "sha256": hashlib.sha256(stored).hexdigest(),
                    "run": run_id,
                }
                changed = True
                report.lines.append(
                    f"{branch.name}：新提交 {commits} 个，存为 {key}（{len(stored):,} 字节）"
                )
        line, alarm = _freshness(clone, ref, branch, now)
        report.lines.append(line)
        if alarm:
            report.alarms.append(alarm)
    if changed:
        key = f"{MANIFEST_PREFIX}{run_id}.jsonl"
        body = "".join(
            json.dumps(state[name], ensure_ascii=False, sort_keys=True) + "\n"
            for name in sorted(state)
        )
        store.put(key, body.encode("utf-8"), content_type="application/x-ndjson; charset=utf-8")
        report.manifest = key
    return report
