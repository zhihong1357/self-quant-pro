"""Turn mirrored archives into monthly research tables (decision 0019).

Layout, per dataset and month:

    datasets/binance-vision/<dataset>/v1/<YYYY-MM>.rNN.parquet
    datasets/binance-vision/<dataset>/v1/<YYYY-MM>.rNN.sha256.json
    datasets/binance-vision/<dataset>/v1/<YYYY-MM>.rNN.inputs.json

A part is written parquet first, then its checksum file (the SHA-256 of the
parquet bytes, which readers check, local caches included), then its inputs
file, which lists the raw archives it holds; a part without an inputs file is
ignored by readers and by the next build. Parts built before checksum files
existed get one from `backfill_checksums`: objects are never overwritten, so
the bytes hashed then are the bytes written at build time.

Parts are append-only: archives mirrored after a month was built go into the
next part. Archives with identical content (the same contract under an old and a
SETTLED folder name) are kept once.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import polars as pl

from sqp.data.parse import parse
from sqp.data.vision import RAW_PREFIX, Source
from sqp.storage import Store

DATASET_PREFIX = "datasets/binance-vision/"
VERSION = "v1"
_PART = re.compile(r"/(?P<month>\d{4}-\d{2})\.r(?P<part>\d{2,})\.(?P<ext>parquet|inputs\.json)$")
_TIME = {"klines": "open_time", "funding": "calc_time"}
CHECKSUM_SUFFIX = ".sha256.json"


def checksum_doc(key: str, data: bytes) -> bytes:
    """The checksum file of a parquet part: its key, size and SHA-256."""
    doc = {"key": key, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return json.dumps(doc, sort_keys=True).encode()


def checksum_key(parquet_key: str) -> str:
    return parquet_key.removesuffix(".parquet") + CHECKSUM_SUFFIX


def month_prefix(dataset: str, month: str) -> str:
    return f"{DATASET_PREFIX}{dataset}/{VERSION}/{month}"


def raw_files(source: Source, raw_keys: list[str]) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """(dataset, month) -> [(symbol, raw key)] for every mirrored archive."""
    found: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for ds in source.datasets:
        head, _, tail = ds["prefix"].partition("{symbol}")
        pattern = re.compile(
            re.escape(RAW_PREFIX + head)
            + r"(?P<symbol>[^/]+)"
            + re.escape(tail)
            + r"[^/]+-(?P<month>\d{4}-\d{2})\.zip$"
        )
        for key in raw_keys:
            match = pattern.match(key)
            if match:
                found[(ds["id"], match["month"])].append((match["symbol"], key))
    return found


def parts(store: Store, dataset: str) -> dict[str, dict]:
    """month -> {"next": next part number, "inputs": [docs of committed parts]}."""
    months: dict[str, dict] = defaultdict(lambda: {"next": 1, "inputs": []})
    for key in store.keys(f"{DATASET_PREFIX}{dataset}/{VERSION}/"):
        match = _PART.search(key)
        if not match:
            continue
        month = months[match["month"]]
        month["next"] = max(month["next"], int(match["part"]) + 1)
        if match["ext"] == "inputs.json":
            month["inputs"].append(json.loads(store.get(key)))
    return months


@dataclass
class BuildOutcome:
    parts: int = 0
    rows: int = 0
    duplicates: int = 0
    bytes: int = 0
    capped: list[str] = field(default_factory=list)
    checksums: int = 0  # checksum files added for parts built before they existed


def build(
    source: Source,
    store_factory: Callable[[], Store],
    *,
    room: int,
    workers: int = 16,
) -> dict[str, BuildOutcome]:
    """Build the missing parts of every dataset; stop writing once `room` is used up."""
    local = threading.local()

    def store() -> Store:
        if not hasattr(local, "store"):
            local.store = store_factory()
        return local.store

    raw_keys = [k for k in store().sizes(RAW_PREFIX)]
    files = raw_files(source, raw_keys)
    outcomes: dict[str, BuildOutcome] = {}
    with ThreadPoolExecutor(workers) as pool:
        for ds in source.datasets:
            outcome = outcomes[ds["id"]] = BuildOutcome()
            built = parts(store(), ds["id"])
            months = sorted(m for (d, m) in files if d == ds["id"])
            for month in months:
                state = built[month]
                covered = {i["key"] for doc in state["inputs"] for i in doc["inputs"]}
                covered |= {d["key"] for doc in state["inputs"] for d in doc["duplicates"]}
                seen = {i["sha256"]: i["key"] for doc in state["inputs"] for i in doc["inputs"]}
                new = sorted(e for e in files[(ds["id"], month)] if e[1] not in covered)
                if not new:
                    continue
                blobs = list(pool.map(lambda entry: store().get(entry[1]), new))
                inputs, duplicates, frames = [], [], []
                for (symbol, key), data in zip(new, blobs, strict=True):
                    sha256 = hashlib.sha256(data).hexdigest()
                    if sha256 in seen:
                        duplicates.append({"key": key, "same_as": seen[sha256]})
                        continue
                    seen[sha256] = key
                    inputs.append({"key": key, "sha256": sha256, "symbol": symbol})
                    frames.append(parse(ds["kind"], data, symbol))
                outcome.duplicates += len(duplicates)
                table = pl.concat(frames) if frames else None
                buffer = io.BytesIO()
                if table is not None:
                    table.sort(["symbol", _TIME[ds["kind"]]]).write_parquet(
                        buffer, compression="zstd"
                    )
                doc = {
                    "dataset": ds["id"],
                    "month": month,
                    "version": VERSION,
                    "rows": 0 if table is None else table.height,
                    "inputs": inputs,
                    "duplicates": duplicates,
                }
                body = json.dumps(doc, sort_keys=True, indent=1).encode()
                if len(buffer.getvalue()) + len(body) > room:
                    outcome.capped.append(month)
                    continue
                stem = f"{month_prefix(ds['id'], month)}.r{state['next']:02d}"
                written = len(buffer.getvalue()) + len(body)
                if table is not None:
                    parquet = buffer.getvalue()
                    store().put(stem + ".parquet", parquet)
                    checksum = checksum_doc(stem + ".parquet", parquet)
                    store().put(checksum_key(stem + ".parquet"), checksum)
                    written += len(checksum)
                store().put(stem + ".inputs.json", body)
                room -= written
                outcome.parts += 1
                outcome.rows += doc["rows"]
                outcome.bytes += written
            outcome.checksums = backfill_checksums(store, ds["id"], pool)
    return outcomes


def backfill_checksums(store: Callable[[], Store], dataset: str, pool: ThreadPoolExecutor) -> int:
    """Write the missing checksum file of every committed parquet part; return how many."""
    keys = set(store().keys(f"{DATASET_PREFIX}{dataset}/{VERSION}/"))
    missing = sorted(
        k
        for k in keys
        if k.endswith(".parquet")
        and k.removesuffix(".parquet") + ".inputs.json" in keys
        and checksum_key(k) not in keys
    )

    def add(key: str) -> None:
        store().put(checksum_key(key), checksum_doc(key, store().get(key)))

    list(pool.map(add, missing))
    return len(missing)
