"""Parse Binance Vision archive files into typed tables (decision 0019).

Every row carries `knowable_time`: the earliest moment a trader could have known
it. A kline is knowable once it closes; a funding rate when it is settled. The
point-in-time layer filters on this column and nothing else.

Format quirks handled here: some files have a header line and some do not; spot
timestamps switched from milliseconds to microseconds on 2025-01-01.
"""

from __future__ import annotations

import io
import zipfile

import polars as pl

KLINE_FIELDS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
]
FUNDING_FIELDS = ["calc_time", "funding_interval_hours", "last_funding_rate"]
_MICROSECONDS = 10**14  # epoch values above this are microseconds, not milliseconds

KINDS = {"klines", "funding"}


def _csv(zip_bytes: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = z.namelist()
        if len(names) != 1:
            raise ValueError(f"归档里应当只有一个文件，实际有 {len(names)} 个")
        return z.read(names[0])


def _has_header(csv: bytes) -> bool:
    first = csv.lstrip().split(b"\n", 1)[0].split(b",", 1)[0].strip()
    return not first.lstrip(b"-").replace(b".", b"").isdigit()


def _read(csv: bytes, fields: list[str], dtypes: dict[str, type[pl.DataType]]) -> pl.DataFrame:
    return pl.read_csv(
        csv,
        has_header=False,
        skip_rows=1 if _has_header(csv) else 0,
        new_columns=fields,
        schema_overrides=dtypes,
    )


def _to_ms(col: str) -> pl.Expr:
    raw = pl.col(col)
    return pl.when(raw > _MICROSECONDS).then(raw // 1000).otherwise(raw)


def _utc(expr: pl.Expr) -> pl.Expr:
    return pl.from_epoch(expr, time_unit="ms").dt.replace_time_zone("UTC")


def parse_klines(zip_bytes: bytes, symbol: str) -> pl.DataFrame:
    """Klines, mark-price klines and spot klines share one layout."""
    floats = ["open", "high", "low", "close", "volume", "quote_volume"]
    floats += ["taker_buy_volume", "taker_buy_quote_volume"]
    dtypes = {name: pl.Float64 for name in floats}
    dtypes |= {"open_time": pl.Int64, "close_time": pl.Int64, "trades": pl.Int64}
    dtypes["ignore"] = pl.String
    df = _read(_csv(zip_bytes), KLINE_FIELDS, dtypes)
    open_ms, close_ms = _to_ms("open_time"), _to_ms("close_time")
    return df.select(
        pl.lit(symbol).alias("symbol"),
        _utc(open_ms).alias("open_time"),
        _utc(close_ms).alias("close_time"),
        _utc(close_ms + 1).alias("knowable_time"),
        *floats[:6],
        "trades",
        *floats[6:],
    )


def parse_funding(zip_bytes: bytes, symbol: str) -> pl.DataFrame:
    dtypes = {
        "calc_time": pl.Int64,
        "funding_interval_hours": pl.Int64,
        "last_funding_rate": pl.Float64,
    }
    df = _read(_csv(zip_bytes), FUNDING_FIELDS, dtypes)
    calc = _utc(_to_ms("calc_time"))
    return df.select(
        pl.lit(symbol).alias("symbol"),
        calc.alias("calc_time"),
        calc.alias("knowable_time"),
        "funding_interval_hours",
        pl.col("last_funding_rate").alias("funding_rate"),
    )


def parse(kind: str, zip_bytes: bytes, symbol: str) -> pl.DataFrame:
    if kind == "klines":
        return parse_klines(zip_bytes, symbol)
    if kind == "funding":
        return parse_funding(zip_bytes, symbol)
    raise ValueError(f"不认识的数据类型：{kind}")
