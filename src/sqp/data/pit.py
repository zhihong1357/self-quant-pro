"""Point-in-time reads (decisions 0020 and 0022): research code sees only what was knowable.

The only way to read research tables is `PointInTime.table(dataset, end=...)`,
which never returns a row whose `knowable_time` is after `end`. So look-ahead
cannot be written through this interface: to see later data, a caller must ask
for a later `end`, and every window is checked against the universe's segments
before anything is read:

- exploration (and anything older): always allowed;
- confirmation and forward: only for a registered hypothesis (`purpose`) of the
  same universe, and only in GitHub Actions running main's code, started by a
  schedule or by hand;
- vault: in addition, the owner's approval in registry/ledger/approvals.jsonl.

Segments, registrations and approvals are read from `refs/remotes/origin/main`,
never from the working tree: merging is the owner's signature and anyone can
edit a working tree. A refused window raises `AccessRefused` and is recorded in
registry/ledger/refusals.jsonl and under ledger/ in the store, where it can be
neither changed nor deleted; if either record cannot be written, the refusal
says so.

Trust model. This class stops honest mistakes and makes deliberate bypasses
visible in review; it cannot stop code in the same process that sets out to
cheat. The hard boundary is the bucket's credentials: only workflows on main
(merged, owner-reviewed code) and the owner can read it, not agents.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import MappingProxyType

import polars as pl

from sqp import ledger
from sqp.data.build import DATASET_PREFIX, VERSION
from sqp.storage import Store

MAIN = "refs/remotes/origin/main"
APPROVALS = "registry/ledger/approvals.jsonl"
REFUSALS = "registry/ledger/refusals.jsonl"
REFUSALS_PREFIX = "ledger/refusals/"
READABLE_STATUSES = frozenset({"registered", "confirmed"})
SEGMENT_NAMES = ("exploration", "confirmation", "vault", "forward")
GUARDED_EVENTS = frozenset({"schedule", "workflow_dispatch"})  # as in the bucket's identity rule
_PART = re.compile(r"/(?P<month>\d{4}-\d{2})\.r\d{2,}\.parquet$")


class AccessRefused(Exception):
    pass


class IntegrityError(Exception):
    pass


@dataclass(frozen=True)
class Segment:
    name: str
    start: datetime  # inclusive
    end: datetime | None  # exclusive; None = open-ended


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time(), tzinfo=UTC)


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.stdout.decode("utf-8")


def _on_main(repo_root: Path, relpath: str) -> str | None:
    """A file as merged on main; None if absent or git is unavailable."""
    return _git(repo_root, "show", f"{MAIN}:{relpath}")


def load_segments(repo_root: Path, universe: str) -> tuple[Segment, ...]:
    """The universe's segments as merged on main. Without them nothing can be read."""
    if not re.fullmatch(r"U\d+", universe):
        raise ValueError(f"研究宇宙的名字不对：{universe!r}")
    text = _on_main(repo_root, f"registry/universes/{universe}.toml")
    if text is None:
        raise AccessRefused(f"main 上没有研究宇宙 {universe}，或者 git 不可用：什么都不读")
    table = tomllib.loads(text)["segments"]
    segments = []
    for name in SEGMENT_NAMES:
        end = table[name].get("end")
        stop = _midnight(end + timedelta(days=1)) if end else None
        segment = Segment(name, _midnight(table[name]["start"]), stop)
        if stop is not None and segment.start >= stop:
            raise ValueError(f"研究宇宙 {universe} 的 {name} 段是空的")
        segments.append(segment)
    for before, after in itertools.pairwise(segments):
        if before.end != after.start:
            raise ValueError(f"研究宇宙 {universe} 的 {before.name} 和 {after.name} 没有首尾相接")
    return tuple(segments)


def _registered(repo_root: Path, universe: str, hypothesis: str) -> bool:
    if not re.fullmatch(r"H-\d{4}", hypothesis):
        return False
    text = _on_main(repo_root, f"registry/hypotheses/{hypothesis}.toml")
    if text is None:
        return False
    try:
        card = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    return card.get("status") in READABLE_STATUSES and card.get("universe") == universe


def _vault_approved(repo_root: Path, universe: str, hypothesis: str) -> bool:
    text = _on_main(repo_root, APPROVALS)
    if not text or ledger.verify(text):
        return False  # nothing merged, or a broken chain: approves nothing
    for line in text.splitlines():
        record = json.loads(line)
        if (
            record.get("type") == "vault"
            and record.get("universe") == universe
            and record.get("hypothesis") == hypothesis
        ):
            return True
    return False


def _running_main_in_actions(repo_root: Path) -> bool:
    """GitHub Actions, started by a schedule or by hand on main, running main's commit.

    The event check mirrors the bucket's identity rule, which rejects pull-request
    events; the commit check catches a job that checked out something else.
    """
    env = os.environ
    if env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REF") != "refs/heads/main":
        return False
    if env.get("GITHUB_EVENT_NAME") not in GUARDED_EVENTS:
        return False
    head = _git(repo_root, "rev-parse", "HEAD")
    main = _git(repo_root, "rev-parse", MAIN)
    return head is not None and head == main


def _utc(t: datetime, name: str) -> datetime:
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError(f"{name} 必须带时区")
    return t.astimezone(UTC)


def _month(t: datetime) -> str:
    return f"{t.year:04d}-{t.month:02d}"


def _previous_month(t: datetime) -> str:
    first = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return _month(first - timedelta(days=1))


class PointInTime:
    def __init__(
        self,
        repo_root: Path,
        store: Store,
        *,
        universe: str = "U1",
        purpose: str | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self._repo_root = repo_root
        self._store = store
        self._cache_dir = cache_dir
        self._universe = universe
        self._purpose = purpose
        self._segments = load_segments(repo_root, universe)
        self._inputs: dict[str, str] = {}  # object key -> SHA-256 of the bytes read

    @property
    def universe(self) -> str:
        return self._universe

    @property
    def purpose(self) -> str | None:
        return self._purpose

    @property
    def segments(self) -> tuple[Segment, ...]:
        return self._segments

    @property
    def inputs(self) -> MappingProxyType[str, str]:
        """Every table read and the SHA-256 of its bytes: fingerprints exactly what was used."""
        return MappingProxyType(self._inputs)

    def touched(self, start: datetime | None, end: datetime) -> list[str]:
        """Names of the segments a window of knowable times overlaps (times in UTC)."""
        names = []
        for seg in self._segments:
            after_start = seg.end is None or start is None or start < seg.end
            if after_start and end >= seg.start:
                names.append(seg.name)
        return names

    def _refuse(self, dataset: str, start: datetime | None, end: datetime, reason: str) -> None:
        """Record the refusal in both places, then raise. A record that could not be
        written is named in the error, so a lost audit trail never goes unnoticed."""
        env = os.environ
        record = {
            "universe": self._universe,
            "dataset": dataset,
            "start": start.isoformat() if start else None,
            "end": end.isoformat(),
            "purpose": self._purpose,
            "reason": reason,
            "at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "github_run_id": env.get("GITHUB_RUN_ID"),
            "github_sha": env.get("GITHUB_SHA"),
        }
        problems = []
        try:
            ledger.append(self._repo_root / REFUSALS, record)
        except Exception as exc:  # a local failure must not stop the store copy
            problems.append(f"本地账本：{type(exc).__name__}: {exc}")
        stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
        body = json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
        try:
            self._store.put(
                f"{REFUSALS_PREFIX}{stamp}.json",
                body,
                content_type="application/json; charset=utf-8",
            )
        except Exception as exc:  # reported below, never swallowed
            problems.append(f"存储桶：{type(exc).__name__}: {exc}")
        if problems:
            message = "拒绝记录没能写入：" + "；".join(problems)
            print(message, file=sys.stderr)
            raise AccessRefused(f"{reason}（{message}）")
        raise AccessRefused(reason)

    def _authorize(
        self, dataset: str, start: datetime | None, end: datetime
    ) -> tuple[datetime | None, datetime]:
        """Normalise the window to UTC and refuse (recording it) unless it may be read."""
        end = _utc(end, "end")
        start = None if start is None else _utc(start, "start")
        if start is not None and start > end:
            raise ValueError("start 不能晚于 end")
        guarded = [s for s in self.touched(start, end) if s != "exploration"]
        if not guarded:
            return start, end
        what = "、".join(guarded)
        if not _running_main_in_actions(self._repo_root):
            reason = f"读{what}段只能在 GitHub Actions 里、由定时或手动触发、运行 main 的代码"
            self._refuse(dataset, start, end, reason)
        purpose = self._purpose
        if purpose is None or not _registered(self._repo_root, self._universe, purpose):
            reason = f"读{what}段需要本研究宇宙已合并注册的假说（purpose={purpose}）"
            self._refuse(dataset, start, end, reason)
        if "vault" in guarded and not _vault_approved(self._repo_root, self._universe, purpose):
            self._refuse(dataset, start, end, f"读金库段需要所有者已合并的批准记录（{APPROVALS}）")
        return start, end

    def _get(self, key: str) -> bytes:
        if self._cache_dir is None:
            data = self._store.get(key)
        else:
            cached = self._cache_dir / key
            if cached.is_file():  # objects never change, so a cached copy stays valid
                data = cached.read_bytes()
            else:
                data = self._store.get(key)
                cached.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=cached.parent, prefix=".tmp-")
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                    os.replace(tmp, cached)  # atomic: no half-written file under the real name
                except BaseException:
                    Path(tmp).unlink(missing_ok=True)
                    raise
        digest = hashlib.sha256(data).hexdigest()
        if self._inputs.setdefault(key, digest) != digest:
            raise IntegrityError(f"同一个分块两次读到的内容不一样：{key}")
        return data

    def table(
        self,
        dataset: str,
        *,
        end: datetime,
        start: datetime | None = None,
        columns: list[str] | None = None,
    ) -> pl.DataFrame:
        """Rows whose knowable_time lies in [start, end]. Never anything after `end`."""
        start, end = self._authorize(dataset, start, end)
        prefix = f"{DATASET_PREFIX}{dataset}/{VERSION}/"
        keys = self._store.keys(prefix)
        committed = {k.removesuffix(".inputs.json") for k in keys if k.endswith(".inputs.json")}
        # Tables are split by the month of the event. A row becomes knowable at most a
        # little after its month ends (a kline at its close), so reading one month
        # before `start` is enough. A dataset published with a longer lag would need
        # a wider look-back.
        first = _previous_month(start) if start else "0000-00"
        last = _month(end)
        wanted_columns = (
            None if columns is None else list(dict.fromkeys([*columns, "knowable_time"]))
        )
        frames = []
        for key in keys:
            match = _PART.search(key)
            wanted = match is not None and first <= match["month"] <= last
            if wanted and key.removesuffix(".parquet") in committed:
                frames.append(pl.read_parquet(io.BytesIO(self._get(key)), columns=wanted_columns))
        if not frames:
            return pl.DataFrame()
        rows = pl.concat(frames).filter(pl.col("knowable_time") <= end)
        if start is not None:
            rows = rows.filter(pl.col("knowable_time") >= start)
        return rows
