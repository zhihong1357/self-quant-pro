"""Record Binance USD-M futures public market data into append-only storage (decision 0027).

Runs once a day as a Cloud Run job in asia-northeast1 (Tokyo), set up by
scripts/gcp/setup-recorder.sh. GitHub's runners are in the United States, which Binance
lists as prohibited and whose addresses its main API refuses. Only the public
market-data paths in `PATHS` are called, with no account and no key: nothing here can
trade or move anything (rule 1).

What is kept: each response as it came, gzip'd JSON lines, one object per dataset per
run under raw/binance-api/um/<dataset>/, and a manifest per object (same name under
manifests/binance-api/um/) naming the window it covers and its SHA-256. Objects are
create-only. A write that fails is reported, never fatal: the other datasets go on; an
object whose manifest did not get written is given one by the next run.

A dataset is written only when every one of its requests got a final answer: a 200, or
a 4xx that no retry can change (an unknown symbol, say). Anything that might succeed
later (network errors, 5xx, repeated 429) leaves the dataset unwritten, and the next run
asks for the same window again. A 418 (this address is banned for a while) stops the run.

Windows: each history dataset continues from the end of the last window written for it,
read from the object names, up to the end of the last whole hour that ended at least
`SETTLE_MS` ago; the first run reaches back `LOOKBACK_MS`, inside what the API keeps
(open interest: the latest month).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqp.storage import ObjectExists, Store

BASE = "https://fapi.binance.com"
RAW_PREFIX = "raw/binance-api/um/"
MANIFEST_PREFIX = "manifests/binance-api/um/"
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
LOOKBACK_MS = 29 * DAY_MS
# Windows end an hour before the last whole hour: some statistics are published a few
# minutes late, and a window never reopens.
SETTLE_MS = HOUR_MS
USER_AGENT = "self-quant-pro-recorder (github.com/zhihong1357/self-quant-pro)"

# Every path this module may call: public market data only.
PATHS = frozenset(
    {
        "/fapi/v1/time",
        "/fapi/v1/exchangeInfo",
        "/fapi/v1/premiumIndex",
        "/fapi/v1/fundingRate",
        "/fapi/v1/klines",
        "/futures/data/openInterestHist",
    }
)

# Seconds between two requests of a group, inside Binance's published limits:
# 2,400 weight a minute in general; funding history 500 requests per 5 minutes;
# open-interest statistics 1,000 requests per 5 minutes.
GAPS = {"general": 0.25, "funding": 0.7, "open-interest": 0.35}
WEIGHT_HEADER = "x-mbx-used-weight-1m"
WEIGHT_PAUSE_AT = 1800  # of 2,400: pause for a minute rather than get close
FUNDING_PAGE = 1000
_WINDOW_KEY = re.compile(r"/(\d{8}T\d{2}Z)_(\d{8}T\d{2}Z)\.jsonl\.gz$")


class Banned(Exception):
    """418: Binance has banned this address for a while. The run stops."""


class NotFinal(Exception):
    """A request still failing after the retries: its dataset is not written."""


class Unreachable(Exception):
    """The API did not answer the first request with a 200 (451: this region is refused)."""


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    headers: dict[str, str]


Get = Callable[[str, dict], Reply]


def http_get(path: str, params: dict, timeout: float = 30) -> Reply:
    """One GET to the public API. Network errors raise OSError; HTTP errors are replies."""
    if path not in PATHS:
        raise ValueError(f"不在允许的接口里：{path}")
    query = "?" + urllib.parse.urlencode(params) if params else ""
    request = urllib.request.Request(BASE + path + query, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return Reply(resp.status, resp.read(), headers)
    except urllib.error.HTTPError as exc:
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        return Reply(exc.code, exc.read(), headers)


@dataclass
class Client:
    """Paces the requests of each group, retries what may still succeed, never 418."""

    get: Get
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    attempts: int = 4
    requests: int = 0
    _last: dict[str, float] = field(default_factory=dict)

    def call(self, group: str, path: str, params: dict) -> Reply:
        if path not in PATHS:
            raise ValueError(f"不在允许的接口里：{path}")
        for attempt in range(self.attempts):
            wait = self._last.get(group, -math.inf) + GAPS[group] - self.clock()
            if wait > 0:
                self.sleep(wait)
            self._last[group] = self.clock()
            self.requests += 1
            try:
                reply = self.get(path, params)
            except OSError:
                reply = None
            if reply is not None:
                if reply.status == 418:
                    raise Banned(path)
                if reply.status == 429:
                    self.sleep(_retry_after(reply))
                    continue
                if reply.status < 500:
                    self._ease_off(reply)
                    return reply
            if attempt < self.attempts - 1:
                self.sleep(2**attempt)
        raise NotFinal(f"{path} {sorted(params.items())}")

    def _ease_off(self, reply: Reply) -> None:
        try:
            used = int(reply.headers.get(WEIGHT_HEADER, "0"))
        except ValueError:
            return
        if used >= WEIGHT_PAUSE_AT:
            self.sleep(60)


def _retry_after(reply: Reply) -> float:
    try:
        seconds = float(reply.headers.get("retry-after", "60"))
    except ValueError:
        seconds = 60
    return min(max(seconds, 1), 300)


@dataclass(frozen=True)
class Window:
    start: int  # milliseconds, inclusive, on an hour
    end: int  # milliseconds, inclusive: the last millisecond of an hour

    @property
    def hours(self) -> int:
        return (self.end + 1 - self.start) // HOUR_MS


def last_hour_end(now_ms: int) -> int:
    """The last millisecond of the last whole hour: everything up to it is final."""
    return now_ms - now_ms % HOUR_MS - 1


def stamp(ms: int, fmt: str = "%Y%m%dT%HZ") -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime(fmt)


def _from_stamp(text: str) -> int:
    moment = datetime.strptime(text, "%Y%m%dT%HZ").replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)


def covered_until(keys: list[str]) -> dict[str, int]:
    """For each history dataset, the last millisecond its stored windows reach."""
    ends: dict[str, int] = {}
    for key in keys:
        match = _WINDOW_KEY.search(key)
        if not key.startswith(RAW_PREFIX) or not match:
            continue
        dataset = key[len(RAW_PREFIX) :].split("/", 1)[0]
        end = _from_stamp(match.group(2)) - 1
        ends[dataset] = max(ends.get(dataset, -1), end)
    return ends


def perpetuals(body: bytes) -> list[str]:
    """USDT-margined perpetual contracts in an exchangeInfo reply, whatever their status:
    a contract being delisted is still worth recording until it disappears."""
    symbols = sorted(
        s["symbol"]
        for s in json.loads(body)["symbols"]
        if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
    )
    if not symbols:
        raise ValueError("合约列表里没有 USDT 永续合约")
    return symbols


def _line(path: str, params: dict, reply: Reply, at: str) -> dict:
    line = {"path": path, "params": params, "status": reply.status, "fetched_at": at}
    try:
        line["body"] = reply.body.decode("utf-8")
    except UnicodeDecodeError:
        line["body_hex"] = reply.body.hex()
    return line


class Lines:
    """Response lines, gzip'd as they arrive. A first run holds a month of every contract;
    kept as text it ran the 1 GiB job out of memory (2026-11-26)."""

    def __init__(self) -> None:
        self._buffer: io.BytesIO | None = io.BytesIO()
        self._gzip = gzip.GzipFile(fileobj=self._buffer, mode="wb", mtime=0)
        self._count = 0

    @classmethod
    def of(cls, lines: list[dict]) -> Lines:
        out = cls()
        for line in lines:
            out.append(line)
        return out

    def append(self, line: dict) -> None:
        text = json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n"
        self._gzip.write(text.encode("utf-8"))
        self._count += 1

    def __len__(self) -> int:
        return self._count

    def finish(self) -> bytes:
        """The gzip'd bytes, exactly once: a second call would otherwise store nothing."""
        if self._buffer is None:
            raise RuntimeError("Lines.finish() called twice")
        self._gzip.close()
        data = self._buffer.getvalue()
        self._buffer.close()
        self._buffer = None
        return data


def _now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def fetch_per_symbol(
    client: Client,
    group: str,
    path: str,
    fixed: dict,
    pieces: list[tuple[Window, int]],
    symbols: list[str],
    time_of: Callable[[object], int],
    now: Callable[[], str] = _now_text,
) -> tuple[Lines, int]:
    """Every symbol's rows, piece after piece (a window and its page size), paging forward
    inside a piece. A final 4xx answer is kept and ends that symbol."""
    lines, refused = Lines(), 0
    for symbol in symbols:
        for piece, page in pieces:
            cursor, answered = piece.start, True
            while cursor <= piece.end:
                params = {**fixed, "symbol": symbol, "startTime": cursor}
                params |= {"endTime": piece.end, "limit": page}
                reply = client.call(group, path, params)
                lines.append(_line(path, params, reply, now()))
                if reply.status != 200:
                    answered = False
                    break
                rows = json.loads(reply.body)
                if len(rows) < page:
                    break
                nxt = time_of(rows[-1]) + 1
                if nxt <= cursor:
                    raise NotFinal(f"{path} {symbol}：翻页没有前进")
                cursor = nxt
            if not answered:
                refused += 1
                break
    return lines, refused


def fetch_funding(
    client: Client,
    window: Window,
    symbols: list[str],
    now: Callable[[], str] = _now_text,
) -> tuple[Lines, int]:
    """Funding settlements of every contract, one hour at a time; an hour that fills a
    whole page is halved until it does not. If the API wants a symbol, ask per symbol."""
    path, lines = "/fapi/v1/fundingRate", Lines()
    todo = [
        (start, min(start + HOUR_MS - 1, window.end))
        for start in range(window.start, window.end + 1, HOUR_MS)
    ]
    todo.reverse()
    while todo:
        start, end = todo.pop()
        params = {"startTime": start, "endTime": end, "limit": FUNDING_PAGE}
        reply = client.call("funding", path, params)
        if reply.status == 400 and not lines:
            pieces = [(window, FUNDING_PAGE)]
            return fetch_per_symbol(
                client, "funding", path, {}, pieces, symbols, _funding_time, now
            )
        if reply.status != 200:
            raise NotFinal(f"{path} {params}：{reply.status}")
        if len(json.loads(reply.body)) >= FUNDING_PAGE:
            if start == end:
                raise NotFinal(f"{path}：同一毫秒的结算超过 {FUNDING_PAGE} 条")
            middle = (start + end) // 2
            todo += [(middle + 1, end), (start, middle)]
            continue
        lines.append(_line(path, params, reply, now()))
    return lines, 0


def _funding_time(row: object) -> int:
    return int(row["fundingTime"])  # type: ignore[index]


def probe(client: Client) -> int:
    """The status code of one cheap request: 200 means this place can record."""
    return client.call("general", "/fapi/v1/time", {}).status


@dataclass(frozen=True)
class History:
    name: str
    group: str
    path: str
    fixed: dict
    time_of: Callable[[object], int]
    per_hour: int  # rows per hour: 2 for 30-minute bars
    page: int | None = None  # None: sized by Binance's klines weight steps


# 30-minute rows (decision 0029): two make an hour, and open interest is kept only a month.
HISTORIES = (
    History(
        "klines-30m",
        "general",
        "/fapi/v1/klines",
        {"interval": "30m"},
        lambda r: int(r[0]),
        per_hour=2,
    ),
    History(
        "open-interest-30m",
        "open-interest",
        "/futures/data/openInterestHist",
        {"period": "30m"},
        lambda r: int(r["timestamp"]),
        per_hour=2,
        page=500,
    ),
)


KLINES_PAGE = 499  # the largest limit that still costs 2


def klines_page(rows: int) -> int:
    """Binance weighs a klines request by its limit: under 100 costs 1, under 500 costs 2.
    Exactly 99 rows would fill a page of 99 and then ask once more for nothing."""
    return 99 if rows < 99 else KLINES_PAGE


def pieces(window: Window, h: History) -> list[tuple[Window, int]]:
    """The window cut into pieces no answer can fill, each with its page size. Then the
    rows do not depend on which end of a longer range the API would keep."""
    step = ((h.page or KLINES_PAGE) - 1) // h.per_hour * HOUR_MS
    out = []
    for start in range(window.start, window.end + 1, step):
        piece = Window(start, min(start + step - 1, window.end))
        out.append((piece, h.page or klines_page(piece.hours * h.per_hour)))
    return out


@dataclass
class Report:
    written: list[str] = field(default_factory=list)
    nothing_new: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    healed: list[str] = field(default_factory=list)
    refused: int = 0
    symbols: int = 0
    stored_bytes: int = 0
    bucket_bytes: int = 0  # the whole store after this run, for the log


@dataclass
class Writer:
    """Create-only objects with a manifest each, inside the bucket's size limit."""

    store: Store
    run: str
    limit_bytes: int
    total_bytes: int
    code_commit: str = ""
    sleep: Callable[[float], None] = time.sleep

    def write(self, dataset: str, key: str, lines: Lines, extra: dict, report: Report) -> None:
        data = lines.finish()
        if self.total_bytes + len(data) > self.limit_bytes:
            report.failed.append(f"{dataset}：存储桶会超过上限，没写")
            return
        try:
            self.store.put(key, data, content_type="application/gzip")
        except ObjectExists:
            report.nothing_new.append(dataset)
            return
        except Exception as exc:  # the next run asks for the same window again
            report.failed.append(f"{dataset}：写入失败（{type(exc).__name__}），下次重录")
            return
        manifest = {
            "key": key,
            "dataset": dataset,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "lines": len(lines),
            "run": self.run,
            "code_commit": self.code_commit,
            "source": BASE,
            "recorded_at": _now_text(),
            **extra,
        }
        body = json.dumps(manifest, sort_keys=True).encode()
        self.total_bytes += len(data)
        report.written.append(dataset)
        report.stored_bytes += len(data)
        if self.put_manifest(manifest_key(key), body):
            self.total_bytes += len(body)
        else:
            report.failed.append(f"{dataset}：清单没写成，下次运行会补上")

    def put_manifest(self, key: str, body: bytes, attempts: int = 3) -> bool:
        for attempt in range(attempts):
            try:
                self.store.put(key, body)
                return True
            except ObjectExists:
                return True
            except Exception:
                if attempt < attempts - 1:
                    self.sleep(2**attempt)
        return False


def manifest_key(raw_key: str) -> str:
    """The manifest of a raw object: the same name under the manifest prefix."""
    if not raw_key.startswith(RAW_PREFIX) or not raw_key.endswith(".jsonl.gz"):
        raise ValueError(f"不是录制的原始对象：{raw_key}")
    return MANIFEST_PREFIX + raw_key[len(RAW_PREFIX) : -len(".jsonl.gz")] + ".json"


def heal(store: Store, raw_keys: list[str], writer: Writer, report: Report) -> None:
    """Write the manifest an earlier run could not: hashed from the stored object."""
    have = set(store.keys(MANIFEST_PREFIX))
    for key in raw_keys:
        if not key.endswith(".jsonl.gz") or manifest_key(key) in have:
            continue
        dataset = key[len(RAW_PREFIX) :].split("/", 1)[0]
        try:
            data = store.get(key)
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as text:  # never all of it at once
                lines = sum(chunk.count(b"\n") for chunk in iter(lambda: text.read(1 << 20), b""))
        except Exception as exc:
            report.failed.append(f"{dataset}：补写清单时读不到原始对象（{type(exc).__name__}）")
            continue
        manifest = {
            "key": key,
            "dataset": dataset,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "lines": lines,
            "source": BASE,
            "healed_at": _now_text(),
            "healed_by_run": writer.run,
        }
        match = _WINDOW_KEY.search(key)
        if match:  # history objects: the same window field a written manifest has
            start, end = (_from_stamp(g) for g in match.groups())
            manifest["window"] = {"start": start, "end": end - 1}
        body = json.dumps(manifest, sort_keys=True).encode()
        if writer.put_manifest(manifest_key(key), body):
            writer.total_bytes += len(body)
            report.healed.append(dataset)
        else:
            report.failed.append(f"{dataset}：清单还是没写成，下次运行再补")


def record(
    client: Client, store: Store, *, now_ms: int, limit_bytes: int, code_commit: str = ""
) -> Report:
    """One run: two snapshots, then every history dataset from where it stopped.
    `code_commit` (the commit of main that is running) goes into every manifest."""
    report = Report()
    status = probe(client)
    if status != 200:
        raise Unreachable(str(status))
    run = stamp(now_ms, "%Y%m%dT%H%M%SZ")
    keys = store.keys(RAW_PREFIX)
    writer = Writer(store, run, limit_bytes, sum(store.sizes().values()), code_commit)
    heal(store, keys, writer, report)

    snapshots = {}
    for dataset, path in (
        ("exchange-info", "/fapi/v1/exchangeInfo"),
        ("premium-index", "/fapi/v1/premiumIndex"),
    ):
        reply = client.call("general", path, {})
        if reply.status != 200:
            raise NotFinal(f"{path}：{reply.status}")
        snapshots[dataset] = reply
        line = _line(path, {}, reply, _now_text())
        writer.write(dataset, f"{RAW_PREFIX}{dataset}/{run}.jsonl.gz", Lines.of([line]), {}, report)
    symbols = perpetuals(snapshots["exchange-info"].body)
    report.symbols = len(symbols)

    ends = covered_until(keys)
    last = last_hour_end(now_ms - SETTLE_MS)
    floor = last + 1 - LOOKBACK_MS
    plans: list[tuple[str, Callable[[Window], tuple[Lines, int]]]] = [
        ("funding", lambda w: fetch_funding(client, w, symbols)),
    ]
    for h in HISTORIES:

        def fetch(w: Window, h: History = h):
            return fetch_per_symbol(
                client, h.group, h.path, h.fixed, pieces(w, h), symbols, h.time_of
            )

        plans.append((h.name, fetch))
    for dataset, fetch in plans:
        start = max(ends.get(dataset, floor - 1) + 1, floor)
        if start > last:
            report.nothing_new.append(dataset)
            continue
        window = Window(start, last)
        try:
            lines, refused = fetch(window)
        except NotFinal as exc:
            report.failed.append(f"{dataset}：{exc}"[:300])
            continue
        key = f"{RAW_PREFIX}{dataset}/{stamp(window.start)}_{stamp(window.end + 1)}.jsonl.gz"
        extra = {"window": {"start": window.start, "end": window.end}, "symbols": len(symbols)}
        writer.write(dataset, key, lines, extra | {"refused": refused}, report)
        report.refused += refused
    report.bucket_bytes = writer.total_bytes
    return report
