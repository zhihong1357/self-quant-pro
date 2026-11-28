"""Point-in-time reads (decisions 0020 and 0022): research code sees only what was knowable.

The only way to read research tables is `PointInTime.table(dataset, end=...)`,
which never returns a row whose `knowable_time` is after `end`. So look-ahead
cannot be written through this interface: to see later data, a caller must ask
for a later `end`, and every window is checked against the universe's segments
before anything is read:

- exploration (and anything older): always allowed;
- confirmation and forward: only for a registered hypothesis (`purpose`) of the
  same universe, and only in GitHub Actions running main's code, started by a
  schedule or by hand;
- vault: in addition, the owner's approval in registry/ledger/approvals.jsonl.

Segments, registrations and approvals are read from `refs/remotes/origin/main`,
never from the working tree: merging is the owner's signature and anyone can
edit a working tree. A refused window raises `AccessRefused` and is recorded in
registry/ledger/refusals.jsonl and under ledger/ in the store, where it can be
neither changed nor deleted; if either record cannot be written, the refusal
says so.

Every part is checked against the SHA-256 written next to it at build time
(`<part>.sha256.json`, read from the store, never from the cache), whether its
bytes come from the store or from a local cache. A part without one is refused.

Trust model. This class stops honest mistakes and makes deliberate bypasses
visible in review; it cannot stop code in the same process that sets out to
cheat. The hard boundary is the bucket's credentials: only workflows on main
(merged, owner-reviewed code) and the owner can read it, not agents.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from zoneinfo import ZoneInfo

import polars as pl

from sqp import ledger
from sqp.data.build import DATASET_PREFIX, VERSION, checksum_key
from sqp.storage import ObjectExists, Store

MAIN = "refs/remotes/origin/main"
APPROVALS = "registry/ledger/approvals.jsonl"
APPROVALS_USED_PREFIX = "ledger/approvals/"
REFUSALS = "registry/ledger/refusals.jsonl"
REFUSALS_PREFIX = "ledger/refusals/"
READABLE_STATUSES = frozenset({"registered", "confirmed"})
SEGMENT_NAMES = ("exploration", "confirmation", "vault", "forward")
GUARDED_EVENTS = frozenset({"schedule", "workflow_dispatch"})  # as in the bucket's identity rule
_PART = re.compile(r"/(?P<month>\d{4}-\d{2})\.r\d{2,}\.parquet$")
_INPUTS_PART = re.compile(r"/(?P<month>\d{4}-\d{2})\.r(?P<part>\d{2,})\.inputs\.json$")
_MONTH_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])$")
_TRIAL_ID = re.compile(r"^T-\d{4,}$")
_DATASET_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
BLIND_CODE_FILE = "src/sqp/research/blind.py"
STATS_CODE_FILE = "src/sqp/stats.py"


class AccessRefused(Exception):
    pass


class IntegrityError(Exception):
    pass


@dataclass(frozen=True)
class Segment:
    name: str
    start: datetime  # inclusive
    end: datetime | None  # exclusive; None = open-ended


@dataclass(frozen=True)
class VaultReceipt:
    """One-time runtime receipt issued only after the store records approval consumption."""

    approval: int
    trial: str
    universe: str
    hypothesis: str
    nonce: str
    mode: str = "standard"
    max_knowable_time: datetime | None = None


def approval_used_key(seq: int) -> str:
    return f"{APPROVALS_USED_PREFIX}{seq}.used.json"


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time(), tzinfo=UTC)


def _git_bytes(repo_root: Path, *args: str) -> bytes | None:
    try:
        out = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.stdout


def _git(repo_root: Path, *args: str) -> str | None:
    raw = _git_bytes(repo_root, *args)
    return None if raw is None else raw.decode("utf-8")


def _on_main(repo_root: Path, relpath: str) -> str | None:
    """A file as merged on main; None if absent or git is unavailable."""
    return _git(repo_root, "show", f"{MAIN}:{relpath}")


def _on_main_bytes(repo_root: Path, relpath: str) -> bytes | None:
    """Raw bytes of a file as merged on main; None if absent or git is unavailable."""
    return _git_bytes(repo_root, "show", f"{MAIN}:{relpath}")


def approval_code_sha256_problems(record: dict, study: str | None = None) -> list[str]:
    """Validate the `code_sha256` mapping on a blind_replication vault approval (L4)."""
    raw = record.get("code_sha256")
    if type(raw) is not dict:
        return ["盲复现批准必须写明 code_sha256（锁定研究代码与 blind.py 的 SHA-256）"]
    found: list[str] = []
    if study:
        study_path = f"src/sqp/research/studies/{study}.py"
        required = {study_path, BLIND_CODE_FILE}
        allowed = required | {STATS_CODE_FILE}
        keys = set(raw)
        if missing := sorted(required - keys):
            found.append(f"盲复现批准的 code_sha256 缺少必填文件：{', '.join(missing)}")
        if extra := sorted(keys - allowed):
            found.append(
                "盲复现批准的 code_sha256 只能锁定研究代码与 blind.py（可选 stats.py），"
                f"不能包含：{', '.join(extra)}"
            )
    for path, digest in raw.items():
        if type(path) is not str or type(digest) is not str or not _SHA256_HEX.match(digest):
            found.append(f"盲复现批准的 code_sha256 中 {path!r} 的值不是 64 位小写十六进制 SHA-256")
    return found


def verify_approval_code_hashes(
    record: dict,
    read_bytes: Callable[[str], bytes | None],
    *,
    study: str,
) -> list[str]:
    """Compare raw bytes of every file locked in `code_sha256` against its SHA-256 (L4)."""
    fmt = approval_code_sha256_problems(record, study)
    if fmt:
        return fmt
    found: list[str] = []
    for path, expected in sorted(record["code_sha256"].items()):
        data = read_bytes(path)
        if data is None:
            found.append(f"批准锁定的代码文件不存在：{path}")
        elif hashlib.sha256(data).hexdigest() != expected:
            found.append(f"批准锁定的代码已改动：{path}")
    return found


def _universe_table(repo_root: Path, universe: str) -> dict:
    if not re.fullmatch(r"U\d+", universe):
        raise ValueError(f"研究宇宙的名字不对：{universe!r}")
    text = _on_main(repo_root, f"registry/universes/{universe}.toml")
    if text is None:
        raise AccessRefused(f"main 上没有研究宇宙 {universe}，或者 git 不可用：什么都不读")
    return tomllib.loads(text)


def load_segments(repo_root: Path, universe: str) -> tuple[Segment, ...]:
    """The universe's segments as merged on main. Without them nothing can be read."""
    table = _universe_table(repo_root, universe)["segments"]
    segments = []
    for name in SEGMENT_NAMES:
        end = table[name].get("end")
        stop = _midnight(end + timedelta(days=1)) if end else None
        segment = Segment(name, _midnight(table[name]["start"]), stop)
        if stop is not None and segment.start >= stop:
            raise ValueError(f"研究宇宙 {universe} 的 {name} 段是空的")
        segments.append(segment)
    for before, after in itertools.pairwise(segments):
        if before.end != after.start:
            raise ValueError(f"研究宇宙 {universe} 的 {before.name} 和 {after.name} 没有首尾相接")
    return tuple(segments)


def _hypothesis_card(repo_root: Path, hypothesis: str) -> dict | None:
    if not re.fullmatch(r"H-\d{4}", hypothesis):
        return None
    text = _on_main(repo_root, f"registry/hypotheses/{hypothesis}.toml")
    if text is None:
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def _registered(repo_root: Path, universe: str, hypothesis: str) -> bool:
    card = _hypothesis_card(repo_root, hypothesis)
    if card is None:
        return False
    return card.get("status") in READABLE_STATUSES and card.get("universe") == universe


def parse_iso_utc(raw: object) -> datetime | None:
    if type(raw) is not str or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(UTC)


_parse_iso_utc = parse_iso_utc


def find_vault_approval(
    approvals: str,
    seq: object,
    universe: str,
    hypothesis: str,
    tid: str,
) -> dict | None:
    """Find a trial-bound vault approval in an already-loaded approvals ledger text."""
    if (
        type(seq) is not int
        or seq < 1
        or not isinstance(tid, str)
        or not _TRIAL_ID.match(tid)
        or not approvals.strip()
        or ledger.verify(approvals)
    ):
        return None  # nothing merged, bad seq/trial, or a broken chain: approves nothing
    for line in approvals.splitlines():
        record = json.loads(line)
        if record.get("seq") == seq:
            if (
                record.get("type") == "vault"
                and record.get("universe") == universe
                and record.get("hypothesis") == hypothesis
                and record.get("trial") == tid
            ):
                return record
            return None
    return None


def _vault_approval_record(
    repo_root: Path,
    universe: str,
    hypothesis: str,
    *,
    trial: str,
    approval: int,
) -> dict | None:
    text = _on_main(repo_root, APPROVALS)
    if not text:
        return None
    return find_vault_approval(text, approval, universe, hypothesis, trial)


def burned_cutoff(exposure: dict) -> datetime | None:
    """UTC midnight immediately after `exposure.burned_through` (first clean forward instant)."""
    through = exposure.get("burned_through")
    if type(through) is not date:
        return None
    return _midnight(through + timedelta(days=1))


def burned_family_allowed(
    repo_root: Path,
    universe: str,
    hypothesis: str,
    max_knowable_time: datetime,
) -> tuple[bool, str]:
    """Check on origin/main that `hypothesis`'s family is burned and cutoff is within bounds."""
    card = _hypothesis_card(repo_root, hypothesis)
    if card is None:
        return False, f"main 上没有假说卡 {hypothesis}"
    family = card.get("family")
    exposure = _universe_table(repo_root, universe).get("exposure", {})
    burned = exposure.get("families_burned") or []
    if family not in burned:
        return False, f"假说族 {family!r} 不在 {universe} 的 families_burned 中，不能走盲复现通道"
    cutoff = burned_cutoff(exposure)
    if cutoff is None:
        return False, f"{universe} 的 exposure.burned_through 缺失或不是日期"
    if _utc(max_knowable_time, "max_knowable_time") >= cutoff:
        through = exposure.get("burned_through")
        return (
            False,
            f"盲复现时间上限 {max_knowable_time.isoformat()} 超过了 burned_through ({through})",
        )
    return True, ""


def check_vault_approval(
    repo_root: Path,
    store: Store,
    *,
    universe: str,
    hypothesis: str,
    trial: str,
    approval: int,
    mode: str = "standard",
    study: str | None = None,
    max_knowable_time: datetime | None = None,
    inherited: dict | None = None,
) -> tuple[dict, datetime | None]:
    """Read-only verification of a trial-bound vault approval before writing any ledger markers."""
    if not _running_main_in_actions(repo_root):
        raise AccessRefused("读金库段只能在 GitHub Actions 里、由定时或手动触发、运行 main 的代码")
    if not _registered(repo_root, universe, hypothesis):
        raise AccessRefused(f"读金库段需要本研究宇宙已合并注册的假说（{hypothesis}）")
    record = _vault_approval_record(repo_root, universe, hypothesis, trial=trial, approval=approval)
    if record is None:
        raise AccessRefused(
            f"金库批准 #{approval} 不存在，或与 {universe}/{hypothesis}/{trial} 不匹配"
        )
    rec_mode = record.get("mode", "standard")
    if rec_mode != mode:
        raise AccessRefused(f"金库批准 #{approval} 的模式是 {rec_mode!r}，不是 {mode!r}")

    limit_utc: datetime | None = None
    if mode == "blind_replication":
        if max_knowable_time is None or not study or not inherited:
            raise AccessRefused(
                "盲复现消费金库批准时必须提供 study、max_knowable_time 和 inherited"
            )
        limit_utc = _utc(max_knowable_time, "max_knowable_time")
        rec_limit = parse_iso_utc(record.get("max_knowable_time"))
        if (
            record.get("study") != study
            or rec_limit != limit_utc
            or record.get("inherited") != inherited
        ):
            raise AccessRefused(f"金库批准 #{approval} 的盲复现范围与试验卡不一致")
        ok, reason = burned_family_allowed(repo_root, universe, hypothesis, limit_utc)
        if not ok:
            raise AccessRefused(reason)
        if fmt_issues := approval_code_sha256_problems(record, study):
            raise AccessRefused("；".join(fmt_issues))
        if verify_approval_code_hashes(record, lambda p: _on_main_bytes(repo_root, p), study=study):
            raise AccessRefused("批准锁定的代码已改动")
    elif mode != "standard":
        raise AccessRefused(f"不支持的金库批准模式：{mode!r}")

    if store.exists(approval_used_key(approval)):
        raise AccessRefused(
            f"金库批准 #{approval} 已经使用过：每份批准只对一张试验卡有效，用完即作废"
        )
    return record, limit_utc


def consume_vault_approval(
    repo_root: Path,
    store: Store,
    *,
    universe: str,
    hypothesis: str,
    trial: str,
    approval: int,
    mode: str = "standard",
    study: str | None = None,
    max_knowable_time: datetime | None = None,
    inherited: dict | None = None,
    stamp: dict | None = None,
) -> VaultReceipt:
    """Verify a trial-bound vault approval on origin/main and atomically consume it once."""
    _, limit_utc = check_vault_approval(
        repo_root,
        store,
        universe=universe,
        hypothesis=hypothesis,
        trial=trial,
        approval=approval,
        mode=mode,
        study=study,
        max_knowable_time=max_knowable_time,
        inherited=inherited,
    )

    nonce = secrets.token_hex(16)
    doc: dict[str, object] = {
        **(stamp or {}),
        "approval": approval,
        "trial": trial,
        "universe": universe,
        "hypothesis": hypothesis,
        "mode": mode,
        "nonce": nonce,
        "at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    if limit_utc is not None:
        doc["max_knowable_time"] = limit_utc.isoformat().replace("+00:00", "Z")
    marker_key = approval_used_key(approval)
    try:
        store.put(
            marker_key,
            ledger.canonical(doc),
            content_type="application/json; charset=utf-8",
        )
    except ObjectExists:
        raise AccessRefused(
            f"金库批准 #{approval} 已经使用过：每份批准只对一张试验卡有效，用完即作废"
        ) from None
    except Exception as exc:
        raise AccessRefused(
            f"写入金库批准 #{approval} 消费标记时存储异常（{type(exc).__name__}），"
            f"批准可能已消耗，请先核对存储桶中的 {marker_key}"
        ) from exc

    return VaultReceipt(
        approval=approval,
        trial=trial,
        universe=universe,
        hypothesis=hypothesis,
        nonce=nonce,
        mode=mode,
        max_knowable_time=limit_utc,
    )


def _months_between(start_month: str, end_month: str) -> list[str]:
    m1, m2 = _MONTH_RE.match(start_month), _MONTH_RE.match(end_month)
    if m1 is None or m2 is None or start_month > end_month:
        raise ValueError(f"月份范围不合法：{start_month!r} ~ {end_month!r}")
    year, month = int(m1["year"]), int(m1["month"])
    end_y, end_m = int(m2["year"]), int(m2["month"])
    months = []
    while (year, month) <= (end_y, end_m):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return months


def dataset_window_ready(
    store: Store,
    dataset: str,
    symbols: list[str] | tuple[str, ...],
    start_month: str,
    end_month: str,
) -> list[str]:
    """Check committed .inputs.json metadata only (never parquet rows) for required months/symbols.

    Assumes monthly-archive datasets (such as `um-klines-1h`) where one committed input
    per symbol-month implies the whole month is present; not sufficient on its own for
    daily-archive datasets. Returns a list of missing `YYYY-MM:SYMBOL` entries; empty means ready.
    """
    prefix = f"{DATASET_PREFIX}{dataset}/{VERSION}/"
    keys = store.keys(prefix)
    present = set(keys)
    by_month: dict[str, set[str]] = {}
    for key in keys:
        match = _INPUTS_PART.search(key)
        if not match:
            continue
        stem = key.removesuffix(".inputs.json")
        parquet_key = f"{stem}.parquet"
        if parquet_key not in present or checksum_key(parquet_key) not in present:
            continue
        try:
            doc = json.loads(store.get(key))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        items = doc.get("inputs", [])
        if not isinstance(items, list):
            continue
        covered = by_month.setdefault(match["month"], set())
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("symbol"), str):
                covered.add(item["symbol"])
    missing = []
    for month in _months_between(start_month, end_month):
        covered = by_month.get(month, set())
        for sym in symbols:
            if sym not in covered:
                missing.append(f"{month}:{sym}")
    return missing


def _running_main_in_actions(repo_root: Path) -> bool:
    """GitHub Actions, started by a schedule or by hand on main, running main's commit.

    The event check mirrors the bucket's identity rule, which rejects pull-request
    events; the commit check catches a job that checked out something else.
    """
    env = os.environ
    if env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REF") != "refs/heads/main":
        return False
    if env.get("GITHUB_EVENT_NAME") not in GUARDED_EVENTS:
        return False
    head = _git(repo_root, "rev-parse", "HEAD")
    main = _git(repo_root, "rev-parse", MAIN)
    return head is not None and head == main


def _utc(t: datetime, name: str) -> datetime:
    if type(t) is not datetime:
        raise TypeError(f"{name} 必须是内置 datetime，不能是子类")
    tz = t.tzinfo
    if tz is None:
        raise ValueError(f"{name} 必须带时区")
    is_std_tz = type(tz) is timezone or (type(tz) is ZoneInfo and tz.key == "UTC")
    if not is_std_tz:
        raise TypeError(
            f"{name} 的 tzinfo 必须是内置 timezone 或 ZoneInfo('UTC')，不能是自定义子类"
        )
    if t.utcoffset() is None:
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
        allowed: frozenset[str] | None = None,
        vault_receipt: VaultReceipt | None = None,
        max_knowable_time: datetime | None = None,
    ) -> None:
        self._repo_root = repo_root
        self._store = store
        self._cache_dir = cache_dir
        self._universe = universe
        self._purpose = purpose
        # Guarded segments this reader may touch; None = any the purpose allows. The
        # trial runner sets it from the trial card, so a card registered for one
        # segment cannot read another (decision 0025).
        self._allowed = None if allowed is None else frozenset(allowed)
        self._vault_receipt = vault_receipt
        self._max_knowable_time = (
            None if max_knowable_time is None else _utc(max_knowable_time, "max_knowable_time")
        )
        multi_guarded = self._allowed is not None and len(self._allowed - {"exploration"}) > 1
        if multi_guarded and (
            vault_receipt is None
            or vault_receipt.mode != "blind_replication"
            or self._max_knowable_time is None
            or self._max_knowable_time != vault_receipt.max_knowable_time
        ):
            raise AccessRefused("跨多个受保护段读取只对带一次性凭证和截止时间的盲复现开放")
        self._segments = load_segments(repo_root, universe)
        self._inputs: dict[str, str] = {}  # object key -> SHA-256 of the bytes read
        self._guarded_seen: set[str] = set()

    @property
    def universe(self) -> str:
        return self._universe

    @property
    def purpose(self) -> str | None:
        return self._purpose

    @property
    def segments(self) -> tuple[Segment, ...]:
        return self._segments

    @property
    def inputs(self) -> MappingProxyType[str, str]:
        """Every table read and the SHA-256 of its bytes: fingerprints exactly what was used."""
        return MappingProxyType(self._inputs)

    def touched(self, start: datetime | None, end: datetime) -> list[str]:
        """Names of the segments a window of knowable times overlaps (times in UTC)."""
        end = _utc(end, "end")
        start = None if start is None else _utc(start, "start")
        names = []
        for seg in self._segments:
            after_start = seg.end is None or start is None or start < seg.end
            if after_start and end >= seg.start:
                names.append(seg.name)
        return names

    def _is_blind_mode(self) -> bool:
        return self._vault_receipt is not None and self._vault_receipt.mode == "blind_replication"

    def _refuse(self, dataset: str, start: datetime | None, end: datetime, reason: str) -> None:
        """Record the refusal in both places, then raise. A record that could not be
        written is named in the error, so a lost audit trail never goes unnoticed."""
        env = os.environ
        if self._is_blind_mode():
            ds_field = f"sha256:{hashlib.sha256(dataset.encode('utf-8')).hexdigest()[:12]}"
            start_field = (
                start.replace(minute=0, second=0, microsecond=0).isoformat() if start else None
            )
            end_field = end.replace(minute=0, second=0, microsecond=0).isoformat()
        else:
            ds_field = dataset
            start_field = start.isoformat() if start else None
            end_field = end.isoformat()
        record = {
            "universe": self._universe,
            "dataset": ds_field,
            "start": start_field,
            "end": end_field,
            "purpose": self._purpose,
            "reason": reason,
            "at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "github_run_id": env.get("GITHUB_RUN_ID"),
            "github_sha": env.get("GITHUB_SHA"),
        }
        problems = []
        try:
            ledger.append(self._repo_root / REFUSALS, record)
        except Exception as exc:  # a local failure must not stop the store copy
            problems.append(f"本地账本：{type(exc).__name__}: {exc}")
        stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
        body = json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
        try:
            self._store.put(
                f"{REFUSALS_PREFIX}{stamp}.json",
                body,
                content_type="application/json; charset=utf-8",
            )
        except Exception as exc:  # reported below, never swallowed
            problems.append(f"存储桶：{type(exc).__name__}: {exc}")
        if problems:
            message = "拒绝记录没能写入：" + "；".join(problems)
            print(message, file=sys.stderr)
            raise AccessRefused(f"{reason}（{message}）")
        raise AccessRefused(reason)

    def _verify_vault_receipt(self, purpose: str) -> str | None:
        receipt = self._vault_receipt
        if receipt is None:
            return f"读金库段需要所有者已合并的一次性批准记录及消费凭证（{APPROVALS}）"
        if receipt.universe != self._universe or receipt.hypothesis != purpose:
            return "金库消费凭证与当前研究宇宙或假说不匹配"
        record = _vault_approval_record(
            self._repo_root,
            self._universe,
            purpose,
            trial=receipt.trial,
            approval=receipt.approval,
        )
        if record is None or record.get("mode", "standard") != receipt.mode:
            return f"main 上找不到与凭证匹配的一次性金库批准 #{receipt.approval}"
        marker_key = approval_used_key(receipt.approval)
        if not self._store.exists(marker_key):
            return f"存储桶缺少金库批准 #{receipt.approval} 的已消费标记"
        try:
            marker = json.loads(self._store.get(marker_key))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return f"金库批准 #{receipt.approval} 的已消费标记损坏"
        if (
            marker.get("approval") != receipt.approval
            or marker.get("trial") != receipt.trial
            or marker.get("universe") != self._universe
            or marker.get("hypothesis") != purpose
            or marker.get("mode") != receipt.mode
            or marker.get("nonce") != receipt.nonce
        ):
            return f"金库批准 #{receipt.approval} 的已消费标记与本次运行凭证不一致"
        if receipt.mode == "blind_replication":
            rec_limit = parse_iso_utc(record.get("max_knowable_time"))
            if (
                self._max_knowable_time is None
                or self._max_knowable_time != receipt.max_knowable_time
                or rec_limit != self._max_knowable_time
            ):
                return "盲复现缺少有效的时间截断上限，或与批准记录不一致"
            ok, reason = burned_family_allowed(
                self._repo_root, self._universe, purpose, self._max_knowable_time
            )
            if not ok:
                return reason
        return None

    def _check_burned_exposure(
        self,
        purpose: str,
        guarded: list[str],
        dataset: str,
        start: datetime | None,
        end: datetime,
    ) -> None:
        """Enforce universe [exposure] limits for standard (non-blind) reads."""
        card = _hypothesis_card(self._repo_root, purpose)
        family = card.get("family") if card else None
        if not family:
            return
        exposure = _universe_table(self._repo_root, self._universe).get("exposure") or {}
        families_burned = exposure.get("families_burned") or []
        vault_burned = exposure.get("vault_burned") or []
        if family in families_burned and ("confirmation" in guarded or "vault" in guarded):
            self._refuse(
                dataset,
                start,
                end,
                f"假说族 {family} 已在 {self._universe} 的 families_burned 中，"
                "普通读取不能读确认集或金库段",
            )
        if family in vault_burned and "vault" in guarded:
            self._refuse(
                dataset,
                start,
                end,
                f"假说族 {family} 已在 {self._universe} 的 vault_burned 中，普通读取不能读金库段",
            )
        if "forward" in guarded and (family in families_burned or family in vault_burned):
            cutoff = burned_cutoff(exposure)
            if cutoff is None:
                self._refuse(
                    dataset,
                    start,
                    end,
                    f"{self._universe} 缺少合法的 exposure.burned_through，"
                    f"无法核对已烧毁族 {family} 的前向起点",
                )
            if start is None or start < cutoff:
                through = exposure.get("burned_through")
                self._refuse(
                    dataset,
                    start,
                    end,
                    f"假说族 {family} 已被前代暴露至 {through}，"
                    f"普通前向读取的 start 必须不早于 {cutoff.isoformat()}",
                )

    def _authorize(
        self, dataset: str, start: datetime | None, end: datetime
    ) -> tuple[datetime | None, datetime]:
        """Normalise the window to UTC and refuse (recording it) unless it may be read."""
        end = _utc(end, "end")
        start = None if start is None else _utc(start, "start")
        if start is not None and start > end:
            raise ValueError("start 不能晚于 end")
        is_blind_mode = self._is_blind_mode()
        if self._max_knowable_time is not None and end > self._max_knowable_time:
            limit_iso = self._max_knowable_time.isoformat()
            reason = (
                f"请求结束时间超过允许的上限 {limit_iso}"
                if is_blind_mode
                else f"请求结束时间 {end.isoformat()} 超过允许的上限 {limit_iso}"
            )
            self._refuse(dataset, start, end, reason)
        guarded = [s for s in self.touched(start, end) if s != "exploration"]
        if not guarded:
            return start, end
        what = "、".join(guarded)
        if self._allowed is not None and not set(guarded) <= self._allowed:
            granted = "、".join(["exploration", *sorted(self._allowed)])
            self._refuse(dataset, start, end, f"这次读取只登记了 {granted} 段，不能读{what}段")
        if not _running_main_in_actions(self._repo_root):
            reason = f"读{what}段只能在 GitHub Actions 里、由定时或手动触发、运行 main 的代码"
            self._refuse(dataset, start, end, reason)
        purpose = self._purpose
        if purpose is None or not _registered(self._repo_root, self._universe, purpose):
            reason = f"读{what}段需要本研究宇宙已合并注册的假说（purpose={purpose}）"
            self._refuse(dataset, start, end, reason)
        if not is_blind_mode:
            self._check_burned_exposure(purpose, guarded, dataset, start, end)
        need_vault_check = "vault" in guarded or is_blind_mode
        if need_vault_check and (reason := self._verify_vault_receipt(purpose)):
            self._refuse(dataset, start, end, reason)
        combined_guarded = self._guarded_seen | set(guarded)
        if len(combined_guarded) > 1 and not is_blind_mode:
            self._refuse(
                dataset,
                start,
                end,
                "跨多个受保护段读取只对带一次性凭证和截止时间的盲复现开放",
            )
        self._guarded_seen.update(guarded)
        return start, end

    def _expected_sha256(self, key: str, keys: set[str]) -> str:
        """The SHA-256 recorded for a part when it was built, read from the store, never
        from the cache: a cache can be edited, the store cannot."""
        name = checksum_key(key)
        if name not in keys:
            raise IntegrityError(f"分块没有校验值：{key}。先运行一次 sqp data build 补写")
        try:
            doc = json.loads(self._store.get(name))
        except (UnicodeDecodeError, json.JSONDecodeError):
            doc = None
        if (
            not isinstance(doc, dict)
            or doc.get("key") != key
            or not isinstance(doc.get("sha256"), str)
        ):
            raise IntegrityError(f"校验值文件和分块对不上：{name}")
        return doc["sha256"]

    def _get(self, key: str, expected: str) -> bytes:
        cached = None if self._cache_dir is None else self._cache_dir / key
        from_cache = cached is not None and cached.is_file()
        data = cached.read_bytes() if from_cache else self._store.get(key)
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected:
            where = (
                f"本机缓存的分块和校验值对不上：{cached}。删掉它再重跑"
                if from_cache
                else (f"存储里的分块和校验值对不上：{key}")
            )
            raise IntegrityError(where)
        if cached is not None and not from_cache:
            cached.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=cached.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, cached)  # atomic: no half-written file under the real name
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        if self._inputs.setdefault(key, digest) != digest:
            raise IntegrityError(f"同一个分块两次读到的内容不一样：{key}")
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
        if type(dataset) is not str or not _DATASET_ID.match(dataset):
            raise ValueError("dataset 名称不合法（仅允许小写字母、数字和连字符，1～64 字符）")
        start, end = self._authorize(dataset, start, end)
        prefix = f"{DATASET_PREFIX}{dataset}/{VERSION}/"
        keys = self._store.keys(prefix)
        present = set(keys)
        committed = {k.removesuffix(".inputs.json") for k in keys if k.endswith(".inputs.json")}
        # Tables are split by the month of the event. A row becomes knowable at most a
        # little after its month ends (a kline at its close), so reading one month
        # before `start` is enough. A dataset published with a longer lag would need
        # a wider look-back.
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
                data = self._get(key, self._expected_sha256(key, present))
                frames.append(pl.read_parquet(io.BytesIO(data), columns=wanted_columns))
        if not frames:
            return pl.DataFrame()
        rows = pl.concat(frames).filter(pl.col("knowable_time") <= end)
        if start is not None:
            rows = rows.filter(pl.col("knowable_time") >= start)
        return rows
