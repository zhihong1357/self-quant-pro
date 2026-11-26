"""The Binance recorder (decision 0027): windows, create-only objects, retries, refusals."""

from __future__ import annotations

import gzip
import hashlib
import itertools
import json
from datetime import UTC, datetime

import pytest

from sqp.data import binance_api as api
from sqp.storage import LocalStore

H = api.HOUR_MS
DAY = api.DAY_MS
STEPS = {"30m": H // 2, "1h": H}
NOW = int(datetime(2026, 12, 1, 0, 17, tzinfo=UTC).timestamp() * 1000)


def ok(payload: object, headers: dict | None = None) -> api.Reply:
    return api.Reply(200, json.dumps(payload).encode(), headers or {})


class FakeBinance:
    """Two USDT perpetuals worth recording, one USDC perpetual and one quarterly that are not.
    Settlements every 8 hours; `crowd` adds contracts settling at one instant (offset ms)."""

    def __init__(
        self,
        now_ms: int,
        *,
        fail: dict[str, list] | None = None,
        refuse: str | None = None,
        funding_needs_symbol: bool = False,
        crowd: list[tuple[int, int]] = (),
        headers: dict | None = None,
    ) -> None:
        self.now_ms = now_ms
        self.fail = fail or {}
        self.refuse = refuse
        self.funding_needs_symbol = funding_needs_symbol
        self.crowd = crowd  # (how many contracts, millisecond offset after the hour)
        self.headers = headers or {}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, path: str, params: dict) -> api.Reply:
        assert path in api.PATHS
        self.calls.append((path, dict(params)))
        queue = self.fail.get(path)
        if queue:
            status = queue.pop(0)
            if status == "network":
                raise ConnectionResetError("reset")
            return api.Reply(status, b'{"code":-1}', {"retry-after": "1"})
        if path == "/fapi/v1/time":
            return ok({"serverTime": self.now_ms})
        if path == "/fapi/v1/exchangeInfo":
            rows = [
                ("AAAUSDT", "PERPETUAL", "USDT", "TRADING"),
                ("BBBUSDT", "PERPETUAL", "USDT", "SETTLING"),
                ("CCCUSDC", "PERPETUAL", "USDC", "TRADING"),
                ("AAAUSDT_261225", "CURRENT_QUARTER", "USDT", "TRADING"),
            ]
            keys = ("symbol", "contractType", "quoteAsset", "status")
            return ok({"symbols": [dict(zip(keys, r, strict=True)) for r in rows]})
        if path == "/fapi/v1/premiumIndex":
            return ok([{"symbol": "AAAUSDT", "lastFundingRate": "0.0001"}])
        start, end, limit = params["startTime"], params["endTime"], params["limit"]
        if path == "/fapi/v1/klines":
            if params["symbol"] == self.refuse:
                return api.Reply(400, b'{"code":-1121,"msg":"Invalid symbol."}', {})
            step = STEPS[params["interval"]]
            first = -(-start // step) * step
            bars = [[t, "1", "1", "1", "1", "9", t + step - 1] for t in range(first, end + 1, step)]
            return ok(bars[:limit], self.headers)
        if path == "/futures/data/openInterestHist":
            step = STEPS[params["period"]]
            oldest = self.now_ms - 30 * DAY
            first = -(-max(start, oldest) // step) * step
            rows = [
                {"symbol": params["symbol"], "sumOpenInterest": "5", "timestamp": t}
                for t in range(first, end + 1, step)
            ]
            return ok(rows[:limit])
        assert path == "/fapi/v1/fundingRate"
        if "symbol" not in params and self.funding_needs_symbol:
            return api.Reply(400, b'{"code":-1102,"msg":"symbol required"}', {})
        return ok(self._settlements(params.get("symbol"), start, end)[:limit])

    def _settlements(self, symbol: str | None, start: int, end: int) -> list[dict]:
        rows = []
        first = -(-start // (8 * H)) * 8 * H
        for t in range(first, end + 1, 8 * H):
            rows += [(t, s) for s in ("AAAUSDT", "BBBUSDT")]
        for h in range(-(-start // H) * H - H, end + 1, H):
            for n, offset in self.crowd:
                if start <= h + offset <= end:
                    rows += [(h + offset, f"X{i:04d}USDT") for i in range(n)]
        rows = sorted(r for r in rows if symbol in (None, r[1]))
        return [{"symbol": s, "fundingTime": t, "fundingRate": "0.0001"} for t, s in rows]


class Clock:
    def __init__(self) -> None:
        self.t, self.slept = 0.0, []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def client_for(fake: FakeBinance) -> tuple[api.Client, Clock]:
    clock = Clock()
    return api.Client(fake, sleep=clock.sleep, clock=clock), clock


def run(tmp_path, fake: FakeBinance, now: int = NOW, limit: int = 10**12) -> api.Report:
    client, _ = client_for(fake)
    return api.record(client, LocalStore(tmp_path), now_ms=now, limit_bytes=limit)


def lines(store: LocalStore, key: str) -> list[dict]:
    text = gzip.decompress(store.get(key)).decode()
    return [json.loads(line) for line in text.splitlines()]


def history_keys(store: LocalStore, dataset: str) -> list[str]:
    return [k for k in store.keys(f"{api.RAW_PREFIX}{dataset}/") if k.endswith(".jsonl.gz")]


def test_the_first_run_takes_two_snapshots_and_29_days_of_history(tmp_path):
    fake = FakeBinance(NOW)
    report = run(tmp_path, fake)
    store = LocalStore(tmp_path)
    assert report.written == [
        "exchange-info",
        "premium-index",
        "funding",
        "klines-30m",
        "open-interest-30m",
    ]
    assert report.failed == [] and report.refused == 0 and report.symbols == 2
    assert report.bucket_bytes == sum(store.sizes().values())  # data and manifests
    # 00:17 UTC: the last whole hour that ended an hour ago ends at 23:00 the day before.
    for dataset in ("funding", "klines-30m", "open-interest-30m"):
        assert [k.rsplit("/", 1)[1] for k in history_keys(store, dataset)] == [
            "20261101T23Z_20261130T23Z.jsonl.gz"
        ]
    asked = {p["symbol"] for path, p in fake.calls if path == "/fapi/v1/klines"}
    assert asked == {"AAAUSDT", "BBBUSDT"}  # USDT perpetuals only, whatever their status


def test_every_object_has_a_manifest_with_its_sha256(tmp_path):
    run(tmp_path, FakeBinance(NOW))
    store = LocalStore(tmp_path)
    manifests = store.keys(api.MANIFEST_PREFIX)
    assert len(manifests) == 5
    for key in manifests:
        m = json.loads(store.get(key))
        assert hashlib.sha256(store.get(m["key"])).hexdigest() == m["sha256"]
        assert m["source"] == api.BASE
    klines = next(
        m for m in map(json.loads, map(store.get, manifests)) if m["dataset"] == "klines-30m"
    )
    assert {api.manifest_key(json.loads(store.get(k))["key"]) for k in manifests} == set(manifests)
    assert klines["window"]["end"] - klines["window"]["start"] + 1 == 29 * DAY


def test_the_next_run_continues_exactly_where_the_last_stopped(tmp_path):
    run(tmp_path, FakeBinance(NOW))
    later = NOW + DAY
    report = run(tmp_path, FakeBinance(later), now=later)
    assert report.failed == []
    store = LocalStore(tmp_path)
    for dataset in ("funding", "klines-30m", "open-interest-30m"):
        names = [k.rsplit("/", 1)[1] for k in history_keys(store, dataset)]
        assert names == [
            "20261101T23Z_20261130T23Z.jsonl.gz",
            "20261130T23Z_20261201T23Z.jsonl.gz",
        ]
    bars = [
        bar[0]
        for key in history_keys(store, "klines-30m")
        for line in lines(store, key)
        if line["params"]["symbol"] == "AAAUSDT"
        for bar in json.loads(line["body"])
    ]
    assert bars == sorted(set(bars)) and len(bars) == 30 * 48  # no bar twice, none missing
    assert {b % (H // 2) for b in bars} == {0}  # every bar on the half hour


def test_within_the_same_hour_there_is_nothing_new_to_record(tmp_path):
    run(tmp_path, FakeBinance(NOW))
    report = run(tmp_path, FakeBinance(NOW + 60_000), now=NOW + 60_000)
    assert report.nothing_new == ["funding", "klines-30m", "open-interest-30m"]
    assert report.written == ["exchange-info", "premium-index"]  # a snapshot per run


def test_a_dataset_failing_for_now_is_left_whole_for_the_next_run(tmp_path):
    report = run(tmp_path, FakeBinance(NOW, fail={"/fapi/v1/klines": [503] * 4}))
    assert "klines-30m" not in report.written
    assert [f.split("：")[0] for f in report.failed] == ["klines-30m"]
    assert "open-interest-30m" in report.written  # the others are not held back
    store = LocalStore(tmp_path)
    assert history_keys(store, "klines-30m") == []
    report = run(tmp_path, FakeBinance(NOW + DAY), now=NOW + DAY)
    assert [k.rsplit("/", 1)[1] for k in history_keys(store, "klines-30m")] == [
        "20261102T23Z_20261201T23Z.jsonl.gz"  # as far back as it may still reach
    ]


def test_network_errors_and_5xx_are_retried(tmp_path):
    fake = FakeBinance(NOW, fail={"/fapi/v1/premiumIndex": ["network", 502]})
    report = run(tmp_path, fake)
    assert "premium-index" in report.written and report.failed == []


def test_a_refused_symbol_is_kept_as_an_answer_and_does_not_block(tmp_path):
    report = run(tmp_path, FakeBinance(NOW, refuse="BBBUSDT"))
    assert "klines-30m" in report.written and report.refused == 1
    store = LocalStore(tmp_path)
    (key,) = history_keys(store, "klines-30m")
    refused = [line for line in lines(store, key) if line["status"] != 200]
    assert [(r["status"], r["params"]["symbol"]) for r in refused] == [(400, "BBBUSDT")]


def test_a_ban_stops_the_run_at_once(tmp_path):
    fake = FakeBinance(NOW, fail={"/fapi/v1/klines": [418]})
    with pytest.raises(api.Banned):
        run(tmp_path, fake)
    store = LocalStore(tmp_path)
    assert history_keys(store, "klines-30m") == history_keys(store, "open-interest-30m") == []
    assert not any(path == "/futures/data/openInterestHist" for path, _ in fake.calls)


def test_429_waits_as_told_then_retries(tmp_path):
    fake = FakeBinance(NOW, fail={"/fapi/v1/exchangeInfo": [429]})
    client, clock = client_for(fake)
    report = api.record(client, LocalStore(tmp_path), now_ms=NOW, limit_bytes=10**12)
    assert "exchange-info" in report.written
    assert 1 in clock.slept  # the Retry-After of the fake


def test_too_many_429_leave_the_snapshot_unrecorded(tmp_path):
    fake = FakeBinance(NOW, fail={"/fapi/v1/exchangeInfo": [429] * 4})
    with pytest.raises(api.NotFinal):
        run(tmp_path, fake)


def test_a_refused_region_writes_nothing(tmp_path):
    with pytest.raises(api.Unreachable, match="451"):
        run(tmp_path, FakeBinance(NOW, fail={"/fapi/v1/time": [451]}))
    assert LocalStore(tmp_path).keys() == []


def test_funding_falls_back_to_one_contract_at_a_time(tmp_path):
    fake = FakeBinance(NOW, funding_needs_symbol=True)
    report = run(tmp_path, fake)
    assert "funding" in report.written
    per_symbol = [p for path, p in fake.calls if path == "/fapi/v1/fundingRate" and "symbol" in p]
    assert {p["symbol"] for p in per_symbol} == {"AAAUSDT", "BBBUSDT"}


def test_a_crowded_hour_is_halved_until_nothing_is_cut_off(tmp_path):
    fake = FakeBinance(NOW, crowd=[(700, 0), (700, 1)])  # 1,400 settlements in one hour
    report = run(tmp_path, fake)
    assert "funding" in report.written
    store = LocalStore(tmp_path)
    (key,) = history_keys(store, "funding")
    seen = [
        (row["symbol"], row["fundingTime"])
        for line in lines(store, key)
        for row in json.loads(line["body"])
    ]
    assert len(seen) == len(set(seen))  # nothing twice
    hours = 29 * 24
    assert len(seen) == hours * 1400 + 29 * 3 * 2  # nothing cut off


def test_more_than_a_page_in_one_millisecond_is_not_guessed_at(tmp_path):
    report = run(tmp_path, FakeBinance(NOW, crowd=[(1500, 0)]))
    assert "funding" not in report.written
    assert report.failed and report.failed[0].startswith("funding")
    assert "klines-30m" in report.written


def test_the_bucket_limit_is_respected(tmp_path):
    report = run(tmp_path, FakeBinance(NOW), limit=1)
    assert report.written == []
    assert all("上限" in f for f in report.failed)
    assert LocalStore(tmp_path).keys() == []


def test_requests_are_paced_per_group(tmp_path):
    fake = FakeBinance(NOW)
    client, clock = client_for(fake)
    times: dict[str, list[float]] = {}
    inner = client.get

    def timed(path, params):
        group = {
            "/fapi/v1/fundingRate": "funding",
            "/futures/data/openInterestHist": "open-interest",
        }
        times.setdefault(group.get(path, "general"), []).append(clock())
        return inner(path, params)

    client.get = timed
    api.record(client, LocalStore(tmp_path), now_ms=NOW, limit_bytes=10**12)
    for group, stamps in times.items():
        gaps = [b - a for a, b in itertools.pairwise(stamps)]
        assert min(gaps) >= api.GAPS[group] - 1e-9, group


def test_heavy_used_weight_pauses_for_a_minute(tmp_path):
    fake = FakeBinance(NOW, headers={api.WEIGHT_HEADER: "2000"})
    client, clock = client_for(fake)
    api.record(client, LocalStore(tmp_path), now_ms=NOW, limit_bytes=10**12)
    assert 60 in clock.slept


def test_only_public_market_data_paths_can_be_called():
    with pytest.raises(ValueError, match="不在允许的接口里"):
        api.http_get("/fapi/v1/account", {})
    client, _ = client_for(FakeBinance(NOW))
    with pytest.raises(ValueError):
        client.call("general", "/fapi/v2/balance", {})
    assert all(p.startswith(("/fapi/v1/", "/futures/data/")) for p in api.PATHS)


def test_window_helpers():
    assert api.last_hour_end(NOW) == int(datetime(2026, 12, 1, tzinfo=UTC).timestamp() * 1000) - 1
    keys = [
        f"{api.RAW_PREFIX}klines-30m/20261101T23Z_20261130T23Z.jsonl.gz",
        f"{api.RAW_PREFIX}klines-30m/20261130T23Z_20261201T23Z.jsonl.gz",
        f"{api.RAW_PREFIX}exchange-info/20261201T001700Z.jsonl.gz",
    ]
    end = int(datetime(2026, 12, 1, 23, tzinfo=UTC).timestamp() * 1000) - 1
    assert api.covered_until(keys) == {"klines-30m": end}
    assert api.klines_page(98) == 99
    assert api.klines_page(99) == 499  # a page of 99 would page once more for nothing
    asked = {h.fixed.get("interval") or h.fixed.get("period") for h in api.HISTORIES}
    assert asked == {"30m"}  # decision 0029


def test_the_manifest_names_the_code_that_ran(tmp_path):
    client, _ = client_for(FakeBinance(NOW))
    store = LocalStore(tmp_path)
    api.record(client, store, now_ms=NOW, limit_bytes=10**12, code_commit="a" * 40)
    commits = {json.loads(store.get(k))["code_commit"] for k in store.keys(api.MANIFEST_PREFIX)}
    assert commits == {"a" * 40}


def test_the_cloud_run_bootstrap_needs_only_the_standard_library():
    """The job runs it on the stock python image, before anything is installed."""
    import ast
    import sys
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "scripts/recorder/bootstrap.py"
    tree = ast.parse(source.read_text("utf-8"))
    names = {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    names |= {
        n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    }
    assert names - {"__future__"} <= set(sys.stdlib_module_names)


def _bootstrap():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts/recorder/bootstrap.py"
    spec = importlib.util.spec_from_file_location("bootstrap", path)
    boot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boot)
    return boot


def _archive(comment: bytes) -> bytes:
    import io
    import zipfile

    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as z:
        z.writestr("self-quant-pro-main/pyproject.toml", "")
        z.comment = comment
    return bundle.getvalue()


SHA = "0123456789abcdef" * 2 + "01234567"


@pytest.mark.parametrize("probe", [False, True])
def test_the_bootstrap_runs_the_commit_its_archive_names(tmp_path, monkeypatch, probe):
    """GitHub's zip of main carries the commit as its comment (checked on the live
    archive, 2026-11-26): no anonymous API call, no race between commit and download."""
    boot = _bootstrap()
    fetched, ran = [], []

    def get(url, timeout=120, attempts=3):
        fetched.append(url)
        return _archive(SHA.encode())

    monkeypatch.setattr(boot, "_get", get)
    monkeypatch.setattr(boot.subprocess, "run", lambda cmd, check: ran.append(cmd))
    monkeypatch.setattr(boot.subprocess, "call", lambda cmd, env: ran.append((cmd, env)) or 0)
    monkeypatch.setattr(boot.tempfile, "mkdtemp", lambda prefix: str(tmp_path))
    monkeypatch.setenv("SQP_PROBE", "1" if probe else "0")
    assert boot.main() == 0
    assert fetched == ["https://codeload.github.com/zhihong1357/self-quant-pro/zip/refs/heads/main"]
    assert ran[0][-1] == f"uv=={boot.UV}"
    command, env = ran[1]
    assert command[:4] == ["uv", "run", "--locked", "--no-dev"]
    assert command[command.index("--directory") + 1] == str(tmp_path / "self-quant-pro-main")
    assert ("--probe" in command) is probe
    assert env["SQP_CODE_COMMIT"] == SHA


def test_without_a_comment_the_bootstrap_asks_the_api_and_never_fails_on_it(monkeypatch):
    import io
    import zipfile

    boot = _bootstrap()
    bundle = zipfile.ZipFile(io.BytesIO(_archive(b"")))
    monkeypatch.setattr(
        boot, "_get", lambda url, timeout=120, attempts=3: json.dumps({"sha": SHA}).encode()
    )
    assert boot.commit_of(bundle) == SHA

    def limited(url, timeout=120, attempts=3):
        raise OSError("403 rate limit exceeded")

    monkeypatch.setattr(boot, "_get", limited)
    assert boot.commit_of(bundle) == "unknown"  # the run goes on; the manifest says so


def test_bootstrap_downloads_are_retried_with_spaced_waits(monkeypatch):
    boot = _bootstrap()
    calls, waits = [], []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"ok"

    def urlopen(request, timeout):
        calls.append(request.full_url)
        if len(calls) < 3:
            raise ConnectionResetError("reset")
        return Resp()

    monkeypatch.setattr(boot.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(boot.time, "sleep", waits.append)
    assert boot._get("https://codeload.github.com/x") == b"ok"
    assert waits == [15, 30]


class FlakyStore(LocalStore):
    """A local store whose puts fail, for the chosen keys, a set number of times."""

    def __init__(self, root, fails_on, times=10**9) -> None:
        super().__init__(root)
        self.fails_on, self.left = fails_on, times

    def put(self, key, data, content_type=None):
        if self.fails_on(key) and self.left > 0:
            self.left -= 1
            raise ConnectionError("503 from the bucket")
        return super().put(key, data, content_type=content_type)


def test_a_failing_object_write_is_reported_and_does_not_stop_the_rest(tmp_path):
    client, _ = client_for(FakeBinance(NOW))
    store = FlakyStore(tmp_path, lambda key: "/klines-30m/" in key and key.startswith("raw/"))
    report = api.record(client, store, now_ms=NOW, limit_bytes=10**12)
    assert "klines-30m" not in report.written and "open-interest-30m" in report.written
    assert any(f.startswith("klines-30m：写入失败") for f in report.failed)
    report = run(tmp_path, FakeBinance(NOW + DAY), now=NOW + DAY)  # the next run records it
    assert "klines-30m" in report.written


def test_a_manifest_that_could_not_be_written_is_written_by_the_next_run(tmp_path):
    client, _ = client_for(FakeBinance(NOW))
    store = FlakyStore(tmp_path, lambda key: key.startswith(api.MANIFEST_PREFIX + "funding/"))
    writer_sleeps = []
    original = api.Writer.__init__

    def quick(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.sleep = writer_sleeps.append

    api.Writer.__init__ = quick
    try:
        report = api.record(client, store, now_ms=NOW, limit_bytes=10**12)
    finally:
        api.Writer.__init__ = original
    assert "funding" in report.written  # the data is in; only its manifest is missing
    assert any(f.startswith("funding：清单没写成") for f in report.failed)
    assert writer_sleeps == [1, 2]  # three tries
    (raw,) = history_keys(LocalStore(tmp_path), "funding")
    assert not LocalStore(tmp_path).exists(api.manifest_key(raw))
    assert report.bucket_bytes == sum(LocalStore(tmp_path).sizes().values())  # unwritten: uncounted

    report = run(tmp_path, FakeBinance(NOW + 60_000), now=NOW + 60_000)
    assert report.healed == ["funding"] and report.failed == []
    healed = json.loads(LocalStore(tmp_path).get(api.manifest_key(raw)))
    assert healed["sha256"] == hashlib.sha256(LocalStore(tmp_path).get(raw)).hexdigest()
    assert healed["lines"] == len(lines(LocalStore(tmp_path), raw))
    assert healed["window"]["end"] - healed["window"]["start"] + 1 == 29 * DAY
    assert report.bucket_bytes == sum(LocalStore(tmp_path).sizes().values())


def test_the_source_registry_names_every_recorded_dataset():
    import tomllib
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "registry/sources/binance-api.toml"
    source = tomllib.loads(path.read_text("utf-8"))
    recorded = {"exchange-info", "premium-index", "funding"} | {h.name for h in api.HISTORIES}
    assert {d["id"] for d in source["datasets"]} == recorded
