"""The exploration profile: descriptive only, exploration only, and reproducible."""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sqp import cli
from sqp.data import build, profile, vision
from sqp.data.pit import PointInTime
from sqp.storage import LocalStore

ROOT = Path(__file__).resolve().parents[1]
H = 3_600_000
FUNDING = "data/futures/um/monthly/fundingRate/"
KLINES = "data/futures/um/monthly/klines/"
SOURCE = vision.Source(
    site="https://site.test/",
    listing="https://list.test/bucket",
    symbol_sets={},
    datasets=[
        {"id": "um-funding", "kind": "funding", "prefix": FUNDING + "{symbol}/"},
        {"id": "um-klines-1h", "kind": "klines", "prefix": KLINES + "{symbol}/1h/"},
    ],
)
SYMBOLS = [f"C{i:02d}USDT" for i in range(25)]
MONTHS = {"2021-12": (2021, 12), "2022-01": (2022, 1), "2024-03": (2024, 3)}  # last: confirmation


def zipped(text: str, member: str) -> bytes:
    """Like Binance's archives, the member name carries the symbol, so two contracts
    with the same numbers still make different archives."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr(member, text)
    return buffer.getvalue()


def month_start(year: int, month: int) -> int:
    return int(datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000)


@pytest.fixture
def lake(tmp_path: Path) -> LocalStore:
    store = LocalStore(tmp_path / "lake")
    for i, symbol in enumerate(SYMBOLS):
        for month, (year, mon) in MONTHS.items():
            t0 = month_start(year, mon)
            rates = "".join(
                f"{t0 + k * 8 * H},8,{0.0001 * (i % 5) + 0.00001 * (k % 3)}\n" for k in range(90)
            )
            key = f"{vision.RAW_PREFIX}{FUNDING}{symbol}/{symbol}-fundingRate-{month}.zip"
            header = "calc_time,funding_interval_hours,last_funding_rate\n"
            store.put(key, zipped(header + rates, f"{symbol}-fundingRate-{month}.csv"))
            bars = "".join(
                f"{t0 + k * H},1,1,1,1,1,{t0 + (k + 1) * H - 1},{(i + 1) * 1000},1,1,1,0\n"
                for k in range(24 * 28)
            )
            key = f"{vision.RAW_PREFIX}{KLINES}{symbol}/1h/{symbol}-1h-{month}.zip"
            store.put(key, zipped(bars, f"{symbol}-1h-{month}.csv"))
    build.build(SOURCE, lambda: store, room=10**10)
    return store


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "registry" / "universes").mkdir(parents=True)
    shutil.copy(ROOT / "registry/universes/U1.toml", root / "registry/universes/U1.toml")
    monkeypatch.setenv("GITHUB_SHA", "0123456789abcdef0123")
    return root


def test_profile_reads_only_exploration_and_is_reproducible(repo: Path, lake: LocalStore) -> None:
    pit = PointInTime(repo, lake)
    key, fingerprint, existed = profile.write_profile(pit, lake, repo)
    assert not existed
    assert not any("2024-03" in k for k in pit.read_keys)  # confirmation month untouched
    report = json.loads(lake.get(key))
    assert report["funding"]["all"]["settlements"] == len(SYMBOLS) * 2 * 90
    assert set(report["funding"]["by_year"]) == {"2021", "2022"}
    assert report["coverage"]["first_month"] == "2021-12"
    assert lake.exists(key.removesuffix(".json") + ".md")

    again = PointInTime(repo, lake)
    assert profile.write_profile(again, lake, repo) == (key, fingerprint, True)


def test_profile_describes_but_never_measures_returns(repo: Path, lake: LocalStore) -> None:
    key, _, _ = profile.write_profile(PointInTime(repo, lake), lake, repo)
    text = lake.get(key).decode()
    assert "return" not in text.lower() and "sharpe" not in text.lower()


def test_liquidity_terciles_use_only_the_month_before(repo: Path, lake: LocalStore) -> None:
    key, _, _ = profile.write_profile(PointInTime(repo, lake), lake, repo)
    terciles = json.loads(lake.get(key))["funding"]["by_liquidity_tercile"]
    # December has no earlier klines, so only January's settlements get a tercile.
    assert terciles["unranked_settlements"] == len(SYMBOLS) * 90
    assert sum(terciles[t]["settlements"] for t in ("1", "2", "3")) == len(SYMBOLS) * 90


def test_cli_profile_prints_no_numbers_from_the_data(
    repo: Path, lake: LocalStore, capsys: pytest.CaptureFixture
) -> None:
    args = ["data", "profile", "--repo", str(repo), "--local", str(lake.root)]
    assert cli.main(args) == 0
    first = capsys.readouterr().out
    assert "画像：derived/profiles/U1-exploration/" in first and "settlements" not in first
    assert cli.main(args) == 0
    assert "重算一致" in capsys.readouterr().out


def test_a_dirty_working_tree_is_named_as_such(
    repo: Path, lake: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_SHA")
    monkeypatch.setattr(profile, "code_commit", lambda _root: "0123456789abcdef-dirty")
    key, _, _ = profile.write_profile(PointInTime(repo, lake), lake, repo)
    assert "/code-0123456789ab-dirty-inputs-" in key
