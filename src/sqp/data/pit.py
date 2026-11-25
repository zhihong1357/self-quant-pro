"""Point-in-time reads (decisions 0020 and 0022): research code sees only what was knowable.

The only way to read research tables is `PointInTime.table(dataset, end=...)`,
which never returns a row whose `knowable_time` is after `end`. So look-ahead
cannot be written through this interface: to see later data, a caller must ask
for a later `end`, and every window is checked against the universe's segments
before anything is read:

- exploration (and anything older): always allowed;
- confirmation and forward: only for a registered hypothesis (`purpose`) of the
  same universe, and only in GitHub Actions on main;
- vault: in addition, the owner's approval in registry/ledger/approvals.jsonl.

Segments, registrations and approvals are read from `refs/remotes/origin/main`,
never from the working tree: merging is the owner's signature and anyone can
edit a working tree. A refused window raises `AccessRefused` and is recorded in
registry/ledger/refusals.jsonl and, when the store accepts it, under ledger/ in
the store, where it can be neither changed nor deleted.

Trust model. This class stops honest mistakes and makes deliberate bypasses
visible in review; it cannot stop code in the same process that sets out to
cheat. The hard boundary is the bucket's credentials: only workflows on main
(merged, owner-reviewed code) and the owner can read it, not agents.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import itertools
import json
import os
import re
import secrets
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

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
_PART = re.compile(r"/(?P<month>\d{4}-\d{2})\.r\d{2,}\.parquet$")


class AccessRefused(Exception):
    pass


@dataclass(frozen=True)
class Segment:
    name: str
    start: datetime  # inclusive
    end: datetime | None  # exclusive; None = open-ended


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time(), tzinfo=UTC)


def _on_main(repo_root: Path, relpath: str) -> str | None:
    """A file as merged on main; None if absent or git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{MAIN}:{relpath}"],
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.stdout.decode("utf-8")


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
        segments.append(Segment(name, _midnight(table[name]["start"]), stop))
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


def _in_actions_on_main() -> bool:
    return (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
    )


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
        self.universe = universe
        self.purpose = purpose
        self._segments = load_segments(repo_root, universe)
        # Object key -> SHA-256 of the bytes read: fingerprints exactly what was used.
        self.inputs: dict[str, str] = {}

    @property
    def segments(self) -> tuple[Segment, ...]:
        return self._segments

    def touched(self, start: datetime | None, end: datetime) -> list[str]:
        """Names of the segments a window of knowable times overlaps."""
        names = []
        for seg in self._segments:
            after_start = seg.end is None or start is None or start < seg.end
            if after_start and end >= seg.start:
                names.append(seg.name)
        return names

    def _refuse(self, dataset: str, start: datetime | None, end: datetime, reason: str) -> None:
        record = {
            "universe": self.universe,
            "dataset": dataset,
            "start": start.isoformat() if start else None,
            "end": end.isoformat(),
            "purpose": self.purpose,
            "reason": reason,
        }
        ledger.append(self._repo_root / REFUSALS, record)
        stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
        body = json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
        # In GitHub Actions the working tree is thrown away; the store is not. If the
        # store refuses, the local ledger already has the record, and we raise next.
        with contextlib.suppress(Exception):
            self._store.put(f"{REFUSALS_PREFIX}{stamp}.json", body)
        raise AccessRefused(reason)

    def authorize(self, dataset: str, start: datetime | None, end: datetime) -> None:
        """Raise AccessRefused (and record it) unless the window may be read."""
        guarded = [s for s in self.touched(start, end) if s != "exploration"]
        if not guarded:
            return
        what = "、".join(guarded)
        if not _in_actions_on_main():
            self._refuse(dataset, start, end, f"读{what}段只能在 GitHub Actions 的 main 上运行")
        purpose = self.purpose
        if purpose is None or not _registered(self._repo_root, self.universe, purpose):
            reason = f"读{what}段需要本研究宇宙已合并注册的假说（purpose={purpose}）"
            self._refuse(dataset, start, end, reason)
        if "vault" in guarded and not _vault_approved(self._repo_root, self.universe, purpose):
            self._refuse(dataset, start, end, f"读金库段需要所有者已合并的批准记录（{APPROVALS}）")

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
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, cached)  # atomic: no half-written file under the real name
        self.inputs[key] = hashlib.sha256(data).hexdigest()
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
        end = _utc(end, "end")
        start = None if start is None else _utc(start, "start")
        if start is not None and start > end:
            raise ValueError("start 不能晚于 end")
        self.authorize(dataset, start, end)
        prefix = f"{DATASET_PREFIX}{dataset}/{VERSION}/"
        keys = self._store.keys(prefix)
        committed = {k.removesuffix(".inputs.json") for k in keys if k.endswith(".inputs.json")}
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
