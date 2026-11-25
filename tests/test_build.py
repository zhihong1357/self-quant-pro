"""Parsing archives and building monthly tables: formats, knowable time, parts, dedup, cap."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from sqp import cli
from sqp.data import build, parse, vision
from sqp.storage import LocalStore

H = 3_600_000  # one hour in milliseconds
T0 = 1_577_836_800_000  # 2020-01-01T00:00:00Z
HEADER = ",".join(parse.KLINE_FIELDS)


def zipped(text: str, name: str = "f.csv") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr(name, text)
    return buffer.getvalue()


def kline_rows(start: int, n: int, unit: int = 1, price: float = 100.0) -> str:
    rows = []
    for i in range(n):
        open_t = (start + i * H) * unit
        close_t = (start + (i + 1) * H - 1) * unit
        rows.append(f"{open_t},{price},{price + 1},{price - 1},{price},10,{close_t},1000,5,4,400,0")
    return "\n".join(rows) + "\n"


def test_klines_with_and_without_header_parse_the_same() -> None:
    plain = parse.parse("klines", zipped(kline_rows(T0, 3)), "BTCUSDT")
    headed = parse.parse("klines", zipped(HEADER + "\n" + kline_rows(T0, 3)), "BTCUSDT")
    assert plain.equals(headed) and plain.height == 3
    first = plain.row(0, named=True)
    assert first["open_time"] == datetime(2020, 1, 1, tzinfo=UTC)
    assert first["knowable_time"] == datetime(2020, 1, 1, 1, tzinfo=UTC)  # when the bar closes


def test_spot_microsecond_timestamps_are_normalised() -> None:
    t2025 = 1_735_689_600_000  # 2025-01-01T00:00:00Z
    micro = parse.parse("klines", zipped(kline_rows(t2025, 2, unit=1000)), "BTCUSDT")
    assert micro["open_time"][0] == datetime(2025, 1, 1, tzinfo=UTC)


def test_funding_is_knowable_at_settlement() -> None:
    header = ",".join(parse.FUNDING_FIELDS)
    text = f"{header}\n{T0},8,0.0001\n{T0 + 8 * H},8,-0.0002\n"
    df = parse.parse("funding", zipped(text), "BTCUSDT")
    assert df["funding_rate"].to_list() == [0.0001, -0.0002]
    assert (df["knowable_time"] == df["calc_time"]).all()


def test_archive_must_hold_one_file() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("a.csv", "1")
        z.writestr("b.csv", "2")
    with pytest.raises(ValueError):
        parse.parse("klines", buffer.getvalue(), "X")


UM = "data/futures/um/monthly/klines/"
SOURCE = vision.Source(
    site="https://site.test/",
    listing="https://list.test/bucket",
    symbol_sets={},
    datasets=[{"id": "um-klines-1h", "kind": "klines", "prefix": UM + "{symbol}/1h/"}],
)


def raw_key(symbol: str, month: str) -> str:
    return f"{vision.RAW_PREFIX}{UM}{symbol}/1h/{symbol}-1h-{month}.zip"


def committed(store: LocalStore, month: str) -> list[dict]:
    keys = store.keys(build.month_prefix("um-klines-1h", month))
    return [json.loads(store.get(k)) for k in keys if k.endswith(".inputs.json")]


def test_build_writes_parts_and_adds_late_archives_as_new_parts(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    store.put(raw_key("BTCUSDT", "2020-01"), zipped(kline_rows(T0, 3)))
    store.put(raw_key("ETHUSDT", "2020-01"), zipped(kline_rows(T0, 3, price=10)))
    out = build.build(SOURCE, lambda: store, room=10**9)["um-klines-1h"]
    assert (out.parts, out.rows) == (1, 6)
    table = pl.read_parquet(
        io.BytesIO(store.get(build.month_prefix("um-klines-1h", "2020-01") + ".r01.parquet"))
    )
    assert table["symbol"].unique().sort().to_list() == ["BTCUSDT", "ETHUSDT"]

    assert build.build(SOURCE, lambda: store, room=10**9)["um-klines-1h"].parts == 0
    store.put(raw_key("SOLUSDT", "2020-01"), zipped(kline_rows(T0, 3, price=1)))
    again = build.build(SOURCE, lambda: store, room=10**9)["um-klines-1h"]
    assert (again.parts, again.rows) == (1, 3)
    docs = committed(store, "2020-01")
    assert [len(d["inputs"]) for d in docs] == [2, 1]


def test_identical_archives_under_two_names_are_kept_once(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    same = zipped(kline_rows(T0, 3))
    store.put(raw_key("OLDUSDT", "2020-01"), same)
    store.put(raw_key("OLDUSDTSETTLED", "2020-01"), same)
    out = build.build(SOURCE, lambda: store, room=10**9)["um-klines-1h"]
    assert (out.rows, out.duplicates) == (3, 1)
    (doc,) = committed(store, "2020-01")
    assert doc["duplicates"][0]["same_as"] == raw_key("OLDUSDT", "2020-01")


def test_an_uncommitted_part_is_ignored_and_not_overwritten(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    store.put(raw_key("BTCUSDT", "2020-01"), zipped(kline_rows(T0, 3)))
    store.put(build.month_prefix("um-klines-1h", "2020-01") + ".r01.parquet", b"crashed half way")
    assert build.build(SOURCE, lambda: store, room=10**9)["um-klines-1h"].parts == 1
    assert [d["rows"] for d in committed(store, "2020-01")] == [3]
    assert store.exists(build.month_prefix("um-klines-1h", "2020-01") + ".r02.inputs.json")


def test_cap_skips_months_that_do_not_fit(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    store.put(raw_key("BTCUSDT", "2020-01"), zipped(kline_rows(T0, 3)))
    out = build.build(SOURCE, lambda: store, room=100)["um-klines-1h"]
    assert out.parts == 0 and out.capped == ["2020-01"]


def test_cli_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    store = LocalStore(tmp_path)
    store.put(raw_key("BTCUSDT", "2020-01"), zipped(kline_rows(T0, 2)))
    monkeypatch.setattr(vision, "load_source", lambda _root: SOURCE)
    assert cli.main(["data", "build", "--local", str(tmp_path)]) == 0
    assert "um-klines-1h：新建 1 个分块，2 行" in capsys.readouterr().out
