"""Mirror Binance Vision monthly archives into append-only storage (decision 0018).

Only bytes move here. Nothing is parsed, so no segment's content is read; the
vault guard belongs to the point-in-time interface that parses the files. What
to mirror comes from registry/sources/binance-vision.toml.

Integrity: Binance's listing gives each file's S3 ETag, which for single-part
uploads is the MD5 of the content; we check it, and fall back to the published
.CHECKSUM (SHA-256) when the ETag is not a plain MD5. A file already mirrored
whose upstream ETag later changes is reported, never overwritten.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqp.storage import ObjectExists, Store

SOURCE_PATH = "registry/sources/binance-vision.toml"
RAW_PREFIX = "raw/binance-vision/"
MANIFEST_PREFIX = "manifests/binance-vision/"
MANIFEST_CHUNK = 2000
_NS = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
_MD5 = re.compile(r"^[0-9a-f]{32}$")
_SETTLED = re.compile(r"(SETTLED)+$")
_MULTIPLIER = re.compile(r"^(1000000|100000|10000|1000|1M)(?=[A-Z])")

Fetch = Callable[[str], bytes]


def http_get(url: str, *, attempts: int = 4, timeout: float = 60) -> bytes:
    """GET with retries on network errors, 429 and 5xx."""
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            retry = exc.code == 429 or exc.code >= 500
            if not retry or attempt == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts - 1:
                raise
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class Remote:
    key: str  # path under the site root, e.g. data/futures/um/monthly/...zip
    etag: str
    size: int
    last_modified: str


@dataclass(frozen=True)
class Source:
    site: str
    listing: str
    symbol_sets: dict[str, dict]
    datasets: list[dict]


def load_source(repo_root: Path) -> Source:
    with (repo_root / SOURCE_PATH).open("rb") as f:
        data = tomllib.load(f)
    return Source(data["site"], data["listing"], data["symbols"], data["datasets"])


def list_prefix(fetch: Fetch, listing: str, prefix: str) -> tuple[list[Remote], list[str]]:
    """Objects and sub-folders directly under `prefix`, following pagination."""
    objects: list[Remote] = []
    folders: list[str] = []
    marker = ""
    while True:
        query = {"delimiter": "/", "prefix": prefix}
        if marker:
            query["marker"] = marker
        root = ET.fromstring(fetch(f"{listing}?{urllib.parse.urlencode(query)}"))
        for c in root.findall("s:Contents", _NS):
            objects.append(
                Remote(
                    key=c.findtext("s:Key", "", _NS),
                    etag=c.findtext("s:ETag", "", _NS).strip('"'),
                    size=int(c.findtext("s:Size", "0", _NS)),
                    last_modified=c.findtext("s:LastModified", "", _NS),
                )
            )
        folders.extend(
            p.findtext("s:Prefix", "", _NS) for p in root.findall("s:CommonPrefixes", _NS)
        )
        if root.findtext("s:IsTruncated", "false", _NS) != "true":
            return objects, folders
        marker = root.findtext("s:NextMarker", "", _NS) or max([o.key for o in objects] + folders)


def folder_names(folders: Iterable[str]) -> list[str]:
    return [f.rstrip("/").rsplit("/", 1)[-1] for f in folders]


def spot_candidates(perp: str) -> set[str]:
    """Spot pair names that may belong to a perpetual's coin."""
    plain = _SETTLED.sub("", perp)
    return {plain, _MULTIPLIER.sub("", plain)}


def resolve_symbols(fetch: Fetch, source: Source, only: set[str] | None) -> dict[str, list[str]]:
    """Symbol sets from the listing. `only` keeps those perpetuals (and their spot pairs)."""
    listed: dict[str, list[str]] = {}
    for name, spec in source.symbol_sets.items():
        _, folders = list_prefix(fetch, source.listing, spec["listing"])
        pattern = re.compile(spec["pattern"])
        listed[name] = [n for n in folder_names(folders) if pattern.match(n)]
    resolved: dict[str, list[str]] = {}
    for name, spec in source.symbol_sets.items():  # base sets first
        if "of" not in spec:
            resolved[name] = [s for s in listed[name] if only is None or s in only]
    for name, spec in source.symbol_sets.items():  # then sets derived from them
        if "of" in spec:
            wanted = set().union(*(spot_candidates(p) for p in resolved[spec["of"]]))
            resolved[name] = [s for s in listed[name] if s in wanted]
    return resolved


def list_upstream(
    fetch: Fetch, source: Source, symbols: dict[str, list[str]], workers: int
) -> list[Remote]:
    """Every archive zip of every dataset for the resolved symbols."""
    prefixes = [
        ds["prefix"].format(symbol=symbol)
        for ds in source.datasets
        for symbol in symbols[ds["symbols"]]
    ]
    remotes: list[Remote] = []
    with ThreadPoolExecutor(workers) as pool:
        for objects, _ in pool.map(lambda p: list_prefix(fetch, source.listing, p), prefixes):
            remotes.extend(o for o in objects if o.key.endswith(".zip"))
    return sorted(remotes, key=lambda r: r.key)


def recorded_etags(store: Store) -> dict[str, str]:
    """Upstream ETag at the time each mirrored file was fetched, from the manifests."""
    etags: dict[str, str] = {}
    for key in store.keys(MANIFEST_PREFIX):
        for line in store.get(key).decode().splitlines():
            record = json.loads(line)
            if "sha256" in record:
                etags[record["key"]] = record["upstream_etag"]
    return etags


@dataclass
class Plan:
    upstream: int = 0
    upstream_bytes: int = 0
    present: int = 0
    fetch: list[Remote] = field(default_factory=list)
    changed: list[tuple[Remote, str]] = field(default_factory=list)
    capped: int = 0


def make_plan(
    remotes: list[Remote], stored: dict[str, int], etags: dict[str, str], room: int
) -> Plan:
    """What to download, given what the bucket holds and how much room is left."""
    plan = Plan(upstream=len(remotes), upstream_bytes=sum(r.size for r in remotes))
    for remote in remotes:
        key = RAW_PREFIX + remote.key
        if key in stored:
            plan.present += 1
            recorded = etags.get(key)
            if recorded is not None and recorded != remote.etag:
                plan.changed.append((remote, recorded))
        elif remote.size <= room:
            plan.fetch.append(remote)
            room -= remote.size
        else:
            plan.capped += 1
    return plan


class IntegrityError(Exception):
    pass


def verify(fetch: Fetch, site: str, remote: Remote, data: bytes) -> str:
    """Check size and content against the listing; return the SHA-256."""
    if len(data) != remote.size:
        raise IntegrityError(f"大小不符：{remote.key}")
    sha256 = hashlib.sha256(data).hexdigest()
    if _MD5.match(remote.etag):
        if hashlib.md5(data).hexdigest() != remote.etag:  # the S3 ETag of a single-part upload
            raise IntegrityError(f"MD5 与 ETag 不符：{remote.key}")
    else:
        published = fetch(site + remote.key + ".CHECKSUM").decode().split()[0]
        if published != sha256:
            raise IntegrityError(f"SHA-256 与 CHECKSUM 不符：{remote.key}")
    return sha256


@dataclass
class Outcome:
    stored: int = 0
    already: int = 0
    failed: list[str] = field(default_factory=list)
    stored_bytes: int = 0


def mirror(
    plan: Plan,
    *,
    fetch: Fetch,
    site: str,
    store_factory: Callable[[], Store],
    run_id: str,
    workers: int,
    progress: Callable[[int], None] = lambda done: None,
) -> Outcome:
    """Download, verify and store the planned files; write manifests in chunks."""
    local = threading.local()

    def store() -> Store:
        if not hasattr(local, "store"):
            local.store = store_factory()
        return local.store

    def work(remote: Remote) -> dict:
        data = fetch(site + remote.key)
        sha256 = verify(fetch, site, remote, data)
        store().put(RAW_PREFIX + remote.key, data)
        return {
            "key": RAW_PREFIX + remote.key,
            "source_url": site + remote.key,
            "bytes": remote.size,
            "sha256": sha256,
            "upstream_etag": remote.etag,
            "upstream_last_modified": remote.last_modified,
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }

    outcome = Outcome()
    pending: list[dict] = []
    chunk = 0

    def flush() -> None:
        nonlocal chunk, pending
        if pending:
            chunk += 1
            body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in pending)
            store().put(f"{MANIFEST_PREFIX}{run_id}-{chunk:04d}.jsonl", body.encode())
            pending = []

    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(work, remote): remote for remote in plan.fetch}
        for done, future in enumerate(as_completed(futures), 1):
            remote = futures[future]
            try:
                record = future.result()
            except ObjectExists:
                outcome.already += 1
            except Exception as exc:  # one bad file must not stop the rest
                outcome.failed.append(f"{remote.key}: {type(exc).__name__}: {exc}"[:300])
            else:
                outcome.stored += 1
                outcome.stored_bytes += record["bytes"]
                pending.append(record)
                if len(pending) >= MANIFEST_CHUNK:
                    flush()
            progress(done)
    if plan.changed:
        seen = datetime.now(UTC).isoformat(timespec="seconds")
        pending.extend(
            {
                "key": RAW_PREFIX + remote.key,
                "event": "upstream_changed",
                "recorded_etag": recorded,
                "upstream_etag": remote.etag,
                "upstream_last_modified": remote.last_modified,
                "seen_at": seen,
            }
            for remote, recorded in plan.changed
        )
    flush()
    return outcome
