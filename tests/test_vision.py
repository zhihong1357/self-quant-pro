"""Mirroring Binance Vision: listing, symbol rules, integrity checks, idempotence, the cap."""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from pathlib import Path

import pytest

from sqp import cli
from sqp.data import vision
from sqp.storage import LocalStore

LISTING = "https://list.test/bucket"
SITE = "https://site.test/"
UM = "data/futures/um/monthly/klines/"
SPOT = "data/spot/monthly/klines/"

SOURCE = vision.Source(
    site=SITE,
    listing=LISTING,
    symbol_sets={
        "um-usdt-perp": {"listing": UM, "pattern": "^[0-9A-Z]+USDT(SETTLED)*$"},
        "spot-of-perp": {"listing": SPOT, "pattern": "^[0-9A-Z]+USDT$", "of": "um-usdt-perp"},
    },
    datasets=[
        {"id": "um", "prefix": UM + "{symbol}/1h/", "symbols": "um-usdt-perp"},
        {"id": "spot", "prefix": SPOT + "{symbol}/1h/", "symbols": "spot-of-perp"},
    ],
)

FILES = {
    UM + "BTCUSDT/1h/BTCUSDT-1h-2024-01.zip": b"btc-1",
    UM + "BTCUSDT/1h/BTCUSDT-1h-2024-02.zip": b"btc-2",
    UM + "1000PEPEUSDT/1h/1000PEPEUSDT-1h-2024-01.zip": b"pepe",
    UM + "OLDUSDTSETTLED/1h/OLDUSDTSETTLED-1h-2021-01.zip": b"old",
    UM + "ETHUSDC/1h/ETHUSDC-1h-2024-01.zip": b"usdc margined",
    UM + "BTCUSDT_240329/1h/BTCUSDT_240329-1h-2024-01.zip": b"delivery",
    SPOT + "BTCUSDT/1h/BTCUSDT-1h-2024-01.zip": b"btc spot",
    SPOT + "PEPEUSDT/1h/PEPEUSDT-1h-2024-01.zip": b"pepe spot",
    SPOT + "OLDUSDT/1h/OLDUSDT-1h-2021-01.zip": b"old spot",
    SPOT + "DOGEUSDT/1h/DOGEUSDT-1h-2024-01.zip": b"no perpetual",
}
WANTED = 7  # BTC x2, PEPE, OLD perpetuals; BTC, PEPE, OLD spot


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class FakeSite:
    """Serves the S3 listing (two entries per page) and the files."""

    def __init__(self, files: dict[str, bytes], multipart: frozenset[str] = frozenset()) -> None:
        self.files = dict(files)
        self.listed = dict(files)  # what the listing reports; may differ from what is served
        self.multipart = multipart
        self.calls: list[str] = []

    def etag(self, key: str) -> str:
        return md5(self.listed[key]) + ("-2" if key in self.multipart else "")

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url.startswith(LISTING):
            return self._listing(urllib.parse.parse_qs(urllib.parse.urlparse(url).query))
        key = url.removeprefix(SITE)
        if key.endswith(".CHECKSUM"):
            name = key.removesuffix(".CHECKSUM")
            return f"{hashlib.sha256(self.files[name]).hexdigest()}  {name}\n".encode()
        return self.files[key]

    def _listing(self, query: dict[str, list[str]]) -> bytes:
        prefix, marker = query["prefix"][0], query.get("marker", [""])[0]
        keys = list(self.listed) + [k + ".CHECKSUM" for k in self.listed]
        entries: dict[str, str] = {}
        for key in keys:
            if key.startswith(prefix):
                rest = key[len(prefix) :]
                name = prefix + rest.split("/")[0] + "/" if "/" in rest else key
                entries[name] = "folder" if "/" in rest else "object"
        ordered = [e for e in sorted(entries) if e > marker]
        page, truncated = ordered[:2], len(ordered) > 2
        parts = ['<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">']
        for name in page:
            if entries[name] == "folder":
                parts.append(f"<CommonPrefixes><Prefix>{name}</Prefix></CommonPrefixes>")
            else:
                zip_key = name.removesuffix(".CHECKSUM")
                etag = self.etag(zip_key) if name == zip_key else md5(b"checksum")
                size = len(self.listed[zip_key]) if name == zip_key else 98
                parts.append(
                    f"<Contents><Key>{name}</Key><ETag>&quot;{etag}&quot;</ETag>"
                    f"<Size>{size}</Size><LastModified>2024-02-05T00:00:00.000Z</LastModified>"
                    "</Contents>"
                )
        parts.append(f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>")
        if truncated:
            parts.append(f"<NextMarker>{page[-1]}</NextMarker>")
        parts.append("</ListBucketResult>")
        return "".join(parts).encode()


RUNS = iter(range(1, 1000))


def run(site: FakeSite, store: LocalStore, room: int = 10**9) -> tuple[vision.Plan, object]:
    symbols = vision.resolve_symbols(site, SOURCE, None)
    remotes = vision.list_upstream(site, SOURCE, symbols, workers=4)
    sizes = store.sizes()
    stored = {k: v for k, v in sizes.items() if k.startswith(vision.RAW_PREFIX)}
    plan = vision.make_plan(remotes, stored, vision.recorded_etags(store), room)
    outcome = vision.mirror(
        plan,
        fetch=site,
        site=SITE,
        store_factory=lambda: store,
        run_id=f"run{next(RUNS):03d}",
        workers=4,
    )
    return plan, outcome


def test_listing_follows_pagination() -> None:
    objects, folders = vision.list_prefix(FakeSite(FILES), LISTING, UM + "BTCUSDT/1h/")
    assert len(objects) == 4 and folders == []  # two zips and their checksums, over two pages


def test_symbol_rules_keep_usdt_perpetuals_and_their_spot_pairs() -> None:
    symbols = vision.resolve_symbols(FakeSite(FILES), SOURCE, None)
    assert symbols["um-usdt-perp"] == ["1000PEPEUSDT", "BTCUSDT", "OLDUSDTSETTLED"]
    assert symbols["spot-of-perp"] == ["BTCUSDT", "OLDUSDT", "PEPEUSDT"]
    only = vision.resolve_symbols(FakeSite(FILES), SOURCE, {"BTCUSDT"})
    assert only == {"um-usdt-perp": ["BTCUSDT"], "spot-of-perp": ["BTCUSDT"]}


@pytest.mark.parametrize(
    ("perp", "spot"),
    [("1000PEPEUSDT", "PEPEUSDT"), ("1MBABYDOGEUSDT", "BABYDOGEUSDT"), ("1INCHUSDT", "1INCHUSDT")],
)
def test_spot_candidates(perp: str, spot: str) -> None:
    assert spot in vision.spot_candidates(perp)


def test_mirror_stores_each_file_once(tmp_path: Path) -> None:
    site, store = FakeSite(FILES), LocalStore(tmp_path)
    plan, outcome = run(site, store)
    assert len(plan.fetch) == WANTED and outcome.stored == WANTED and not outcome.failed
    assert store.get(vision.RAW_PREFIX + UM + "BTCUSDT/1h/BTCUSDT-1h-2024-01.zip") == b"btc-1"
    manifest = store.keys(vision.MANIFEST_PREFIX)
    assert len(manifest) == 1 and manifest[0].endswith("-0001.jsonl")
    assert len(store.get(manifest[0]).decode().splitlines()) == WANTED
    downloads = [c for c in site.calls if c.startswith(SITE)]
    assert not any(c.endswith(".CHECKSUM") for c in downloads)  # MD5 ETags sufficed

    again, _ = run(site, store)
    assert again.fetch == [] and again.present == WANTED


def test_corrupt_download_is_not_stored(tmp_path: Path) -> None:
    site, store = FakeSite(FILES), LocalStore(tmp_path)
    bad = UM + "BTCUSDT/1h/BTCUSDT-1h-2024-02.zip"
    site.files[bad] = b"btc-X"  # same size, different content than the listing promises
    _, outcome = run(site, store)
    assert len(outcome.failed) == 1 and "ETag" in outcome.failed[0]
    assert not store.exists(vision.RAW_PREFIX + bad)


def test_multipart_etag_falls_back_to_published_checksum(tmp_path: Path) -> None:
    key = UM + "BTCUSDT/1h/BTCUSDT-1h-2024-01.zip"
    site, store = FakeSite(FILES, multipart=frozenset({key})), LocalStore(tmp_path)
    _, outcome = run(site, store)
    assert outcome.stored == WANTED
    assert site.calls.count(SITE + key + ".CHECKSUM") == 1


def test_upstream_replacement_is_reported_not_overwritten(tmp_path: Path) -> None:
    site, store = FakeSite(FILES), LocalStore(tmp_path)
    run(site, store)
    key = UM + "BTCUSDT/1h/BTCUSDT-1h-2024-01.zip"
    site.files[key] = site.listed[key] = b"btc-1 revised"
    plan, _ = run(site, store)
    assert [r.key for r, _ in plan.changed] == [key] and plan.fetch == []
    assert store.get(vision.RAW_PREFIX + key) == b"btc-1"
    events = [
        json.loads(line)
        for k in store.keys(vision.MANIFEST_PREFIX)
        for line in store.get(k).decode().splitlines()
        if "event" in line
    ]
    assert [e["event"] for e in events] == ["upstream_changed"]


def test_cap_leaves_the_rest_for_later(tmp_path: Path) -> None:
    site, store = FakeSite(FILES), LocalStore(tmp_path)
    plan, outcome = run(site, store, room=12)
    assert plan.capped > 0 and outcome.stored == len(plan.fetch) < WANTED
    assert sum(r.size for r in plan.fetch) <= 12


def test_cli_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    site = FakeSite(FILES)
    monkeypatch.setattr(vision, "http_get", site)
    monkeypatch.setattr(vision, "load_source", lambda _root: SOURCE)
    lake = str(tmp_path / "lake")
    assert cli.main(["data", "sync", "--local", lake, "--dry-run"]) == 0
    assert f"本次要下载：{WANTED} 个" in capsys.readouterr().out
    assert LocalStore(Path(lake)).keys() == []
    assert cli.main(["data", "sync", "--local", lake, "--workers", "2"]) == 0
    assert f"已下载并校验：{WANTED} 个" in capsys.readouterr().out
