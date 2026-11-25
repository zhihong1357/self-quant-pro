"""Point-in-time reads (decision 0020): research code sees only what was knowable.

The only way to read research tables is `PointInTime.table(dataset, end=...)`,
which never returns a row whose `knowable_time` is after `end`. So look-ahead
cannot be written through this interface: to see later data, a caller must ask
for a later `end`, and every window is checked against the universe's segments
before anything is read:

- exploration (and anything older): always allowed;
- confirmation and forward: only for a registered hypothesis (`purpose`);
- vault: only for a registered hypothesis the owner approved in
  registry/ledger/approvals.jsonl.

Registrations and approvals count only once merged: they are read from
`origin/main`, never from the working tree, because merging is the owner's
signature and anyone can edit a working tree.

A refused window is appended to registry/ledger/refusals.jsonl and raises
`AccessRefused`. A row belongs to the segment in which it became knowable.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import polars as pl

from sqp import ledger
from sqp.data.build import DATASET_PREFIX, VERSION
from sqp.storage import Store

APPROVALS = "registry/ledger/approvals.jsonl"
REFUSALS = "registry/ledger/refusals.jsonl"
_PART = re.compile(r"/(?P<month>\d{4}-\d{2})\.r\d{2}\.parquet$")


class AccessRefused(Exception):
    pass


@dataclass(frozen=True)
class Segment:
    name: str
    start: datetime  # inclusive
    end: datetime | None  # exclusive; None = open-ended


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time(), tzinfo=UTC)


def load_segments(repo_root: Path, universe: str) -> list[Segment]:
    path = repo_root / "registry" / "universes" / f"{universe}.toml"
    with path.open("rb") as f:
        segments = tomllib.load(f)["segments"]
    out = []
    for name in ("exploration", "confirmation", "vault", "forward"):
        seg = segments[name]
        end = seg.get("end")
        out.append(
            Segment(
                name, _midnight(seg["start"]), _midnight(end + timedelta(days=1)) if end else None
            )
        )
    return out


def _on_main(repo_root: Path, relpath: str) -> str | None:
    """A file as merged on origin/main; None if absent or git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"origin/main:{relpath}"],
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.stdout.decode("utf-8")


def _registered(repo_root: Path, hypothesis: str) -> bool:
    if not re.fullmatch(r"H-\d{4}", hypothesis):
        return False
    text = _on_main(repo_root, f"registry/hypotheses/{hypothesis}.toml")
    return text is not None and tomllib.loads(text).get("status", "draft") != "draft"


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
        self.repo_root = repo_root
        self.store = store
        self.universe = universe
        self.purpose = purpose
        self.cache_dir = cache_dir
        # Every object read. Objects never change, so this list fingerprints the inputs.
        self.read_keys: set[str] = set()
        self.segments = load_segments(repo_root, universe)

    def touched(self, start: datetime | None, end: datetime) -> list[str]:
        """Names of the segments a window of knowable times overlaps."""
        names = []
        for seg in self.segments:
            after_start = seg.end is None or start is None or start < seg.end
            if after_start and end >= seg.start:
                names.append(seg.name)
        return names

    def _refuse(self, dataset: str, start: datetime | None, end: datetime, reason: str) -> None:
        ledger.append(
            self.repo_root / REFUSALS,
            {
                "universe": self.universe,
                "dataset": dataset,
                "start": start.isoformat() if start else None,
                "end": end.isoformat(),
                "purpose": self.purpose,
                "reason": reason,
            },
        )
        raise AccessRefused(reason)

    def authorize(self, dataset: str, start: datetime | None, end: datetime) -> None:
        if end.tzinfo is None or (start is not None and start.tzinfo is None):
            raise ValueError("时间必须带时区（UTC）")
        touched = self.touched(start, end)
        guarded = [s for s in touched if s != "exploration"]
        if not guarded:
            return
        purpose = self.purpose
        if purpose is None or not _registered(self.repo_root, purpose):
            self._refuse(
                dataset,
                start,
                end,
                f"读{'、'.join(guarded)}段需要已合并注册的假说（purpose={purpose}）",
            )
        if "vault" in guarded and not _vault_approved(self.repo_root, self.universe, purpose):
            self._refuse(dataset, start, end, f"读金库段需要所有者已合并的批准记录（{APPROVALS}）")

    def _get(self, key: str) -> bytes:
        if self.cache_dir is None:
            return self.store.get(key)
        cached = self.cache_dir / key
        if not cached.is_file():  # objects never change, so a cached copy stays valid
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(self.store.get(key))
        return cached.read_bytes()

    def table(
        self,
        dataset: str,
        *,
        end: datetime,
        start: datetime | None = None,
        columns: list[str] | None = None,
    ) -> pl.DataFrame:
        """Rows whose knowable_time lies in [start, end]. Never anything after `end`."""
        self.authorize(dataset, start, end)
        prefix = f"{DATASET_PREFIX}{dataset}/{VERSION}/"
        keys = self.store.keys(prefix)
        committed = {k.removesuffix(".inputs.json") for k in keys if k.endswith(".inputs.json")}
        first = _previous_month(start) if start else "0000-00"
        last = _month(end)
        frames = []
        for key in keys:
            match = _PART.search(key)
            wanted = match is not None and first <= match["month"] <= last
            if wanted and key.removesuffix(".parquet") in committed:
                wanted_columns = (
                    None if columns is None else list(dict.fromkeys([*columns, "knowable_time"]))
                )
                frames.append(pl.read_parquet(io.BytesIO(self._get(key)), columns=wanted_columns))
                self.read_keys.add(key)
        if not frames:
            return pl.DataFrame()
        rows = pl.concat(frames).filter(pl.col("knowable_time") <= end)
        if start is not None:
            rows = rows.filter(pl.col("knowable_time") >= start)
        return rows
