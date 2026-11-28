"""Trial cards (decision 0025): what may run, registered on main before it runs.

Each trial is one line of registry/ledger/trials.jsonl, appended in a pull request
and merged by the owner before GitHub Actions may run it. The trial's id comes from
the ledger's sequence number: seq 7 is T-0007. A card holds no results; those stay
in the private bucket (decision 0011).

    {"type": "trial", "hypothesis": "H-0001", "study": "thin_slice",
     "segment": "exploration", "params": {...}, "note": "why this trial"}

Optional keys: "approval" (the seq of the owner's vault approval; required for the
vault segment), "repeat_of" (an earlier exploration or synthetic trial run again
to show its fingerprints are reproducible) and "tests" (how many tests one run holds,
such as the arms of a control; the card counts that many times, at least once). A
repeat counts like any other trial: over-counting is allowed, under-counting is not.

Rules about the ledger as a whole (one confirmation per hypothesis, what a repeat
may point to) apply to every card. Rules that look at other files (the study code
exists, the hypothesis is registered, the vault approval) apply to new cards and to
the card being run, so a later status change or a renamed study never blocks the
cards that come after it.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable, Container
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqp import ledger
from sqp.research import blind

TRIALS = "registry/ledger/trials.jsonl"
APPROVALS = "registry/ledger/approvals.jsonl"
INHERITED = "registry/inherited/trials.json"
STUDIES_DIR = "src/sqp/research/studies/"
SEGMENTS = ("exploration", "confirmation", "vault", "forward", "synthetic")
GUARDED = frozenset({"confirmation", "vault", "forward"})
MODES = ("standard", "blind_replication")
READABLE_STATUSES = frozenset({"registered", "confirmed"})  # as in sqp.data.pit
HYPOTHESIS = re.compile(r"^H-\d{4}$")
STUDY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TRIAL_ID = re.compile(r"^T-\d{4,}$")
KEYS = frozenset({"type", "hypothesis", "study", "segment", "params", "note"})
OPTIONAL = frozenset({"approval", "repeat_of", "tests", "mode", "max_knowable_time", "inherited"})
LEDGER_KEYS = frozenset({"seq", "prev", "at", "hash"})


def trial_id(seq: int) -> str:
    return f"T-{seq:04d}"


@dataclass(frozen=True)
class Trial:
    id: str
    record: dict  # the whole ledger line, hash included

    @property
    def hypothesis(self) -> str:
        return self.record["hypothesis"]

    @property
    def study(self) -> str:
        return self.record["study"]

    @property
    def segment(self) -> str:
        return self.record["segment"]

    @property
    def params(self) -> dict:
        return self.record["params"]

    @property
    def repeat_of(self) -> str | None:
        return self.record.get("repeat_of")

    @property
    def mode(self) -> str:
        return self.record.get("mode", "standard")

    @property
    def approval(self) -> int | None:
        return self.record.get("approval")

    @property
    def max_knowable_time(self) -> str | None:
        return self.record.get("max_knowable_time")

    @property
    def inherited(self) -> dict | None:
        return self.record.get("inherited")


def parse(text: str) -> list[Trial]:
    """Every trial card in a ledger's text (the chain is checked elsewhere)."""
    trials = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # reported by the ledger-chain rule
        if isinstance(record, dict) and record.get("type") == "trial" and "seq" in record:
            trials.append(Trial(trial_id(record["seq"]), record))
    return trials


def _parse_iso_utc(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(UTC)


def _valid_inherited_dict(raw: object) -> bool:
    return (
        isinstance(raw, dict)
        and set(raw) == {"source", "path"}
        and isinstance(raw.get("source"), str)
        and bool(raw["source"].strip())
        and isinstance(raw.get("path"), str)
        and bool(raw["path"].strip())
    )


def _card(read: Callable[[str], str | None], hypothesis: str) -> dict | None:
    text = read(f"registry/hypotheses/{hypothesis}.toml")
    if text is None:
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def _universe(read: Callable[[str], str | None], universe: str) -> dict | None:
    text = read(f"registry/universes/{universe}.toml")
    if text is None:
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def approval_problems(approvals: str) -> list[str]:
    """Ledger-wide rules for vault records in registry/ledger/approvals.jsonl (decision 0031)."""
    if not approvals.strip():
        return []
    if ledger.verify(approvals):
        return []  # chain issues are reported by ledger-chain
    found: list[str] = []
    by_trial: dict[str, int] = {}
    for line in approvals.splitlines():
        record = json.loads(line)
        if not isinstance(record, dict) or record.get("type") != "vault":
            continue
        seq = record.get("seq", 0)
        prefix = f"批准 #{seq}"
        if not isinstance(record.get("universe"), str) or not record["universe"]:
            found.append(f"{prefix}：缺少 universe")
        if not (
            isinstance(record.get("hypothesis"), str) and HYPOTHESIS.match(record["hypothesis"])
        ):
            found.append(f"{prefix}：hypothesis 必须形如 H-0001")
        trial = record.get("trial")
        if not (isinstance(trial, str) and TRIAL_ID.match(trial)):
            found.append(f"{prefix}：金库批准必须绑定单张试验卡 trial（形如 T-0007）")
        elif trial in by_trial:
            found.append(f"{prefix}：试验卡 {trial} 已有金库批准 #{by_trial[trial]}，不能重复签发")
        else:
            by_trial[trial] = seq
        mode = record.get("mode", "standard")
        if mode not in MODES:
            found.append(f"{prefix}：mode 只能是 {'/'.join(MODES)} 之一")
        elif mode == "blind_replication":
            if not (isinstance(record.get("study"), str) and STUDY.match(record["study"])):
                found.append(f"{prefix}：盲复现批准必须写明合法的 study")
            if _parse_iso_utc(record.get("max_knowable_time")) is None:
                found.append(f"{prefix}：盲复现批准必须写明带 UTC 时区的 max_knowable_time")
            if not _valid_inherited_dict(record.get("inherited")):
                found.append(f"{prefix}：盲复现批准必须写明 inherited（含 source 和 path）")
    return found


def _find_approval(
    approvals: str, seq: object, universe: str, hypothesis: str, tid: str
) -> dict | None:
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        return None
    if not approvals.strip() or ledger.verify(approvals):
        return None  # a broken chain approves nothing, as in sqp.data.pit
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


def problems(
    text: str, read: Callable[[str], str | None], strict: Container[str] | None = None
) -> list[str]:
    """What is wrong with the trial cards in a ledger, in Chinese; empty means fine.

    `read(path)` returns a file of the same repository state, or None if absent.
    `strict` names the cards whose study code, hypothesis and approval are checked
    against those files; None means every card.
    """
    found: list[str] = []
    seen: dict[str, Trial] = {}
    confirmed: dict[str, str] = {}
    used_approvals: dict[int, str] = {}
    approvals = read(APPROVALS) or ""
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return found  # reported by the ledger-chain rule
        if not isinstance(record, dict) or record.get("type") != "trial":
            continue
        tid = trial_id(record.get("seq", 0))

        def bad(message: str, tid: str = tid) -> None:
            found.append(f"{tid}：{message}")

        body = set(record) - LEDGER_KEYS
        if missing := sorted(KEYS - body):
            bad(f"缺少字段 {', '.join(missing)}")
            continue
        if extra := sorted(body - KEYS - OPTIONAL):
            bad(f"多了不认识的字段 {', '.join(extra)}")
        hypothesis, study, segment = record["hypothesis"], record["study"], record["segment"]
        if not (isinstance(hypothesis, str) and HYPOTHESIS.match(hypothesis)):
            bad("hypothesis 必须形如 H-0001")
            continue
        if not (isinstance(study, str) and STUDY.match(study)):
            bad("study 必须是小写字母、数字和下划线，字母开头")
        if segment not in SEGMENTS:
            bad(f"segment 只能是 {'/'.join(SEGMENTS)} 之一")
            continue
        if not isinstance(record["params"], dict):
            bad("params 必须是 JSON 对象")
        if not (isinstance(record["note"], str) and record["note"].strip()):
            bad("note 不能为空：一句话写明为什么做这次试验")
        mode = record.get("mode", "standard")
        if mode not in MODES:
            bad(f"mode 只能是 {'/'.join(MODES)} 之一")
            continue
        if segment == "confirmation" and "repeat_of" not in record:
            if hypothesis in confirmed:
                bad(f"{hypothesis} 已经有确认检验 {confirmed[hypothesis]}：每个假说只做一次")
            confirmed.setdefault(hypothesis, tid)
        tests = record.get("tests", 1)
        if not (isinstance(tests, int) and not isinstance(tests, bool) and tests >= 1):
            bad("tests 必须是不小于 1 的整数")
        if "approval" in record:
            ap = record["approval"]
            if segment != "vault":
                bad("只有金库段的试验才写 approval")
            elif isinstance(ap, bool) or not isinstance(ap, int) or ap < 1:
                bad("approval 必须是正整数（批准记录的 seq）")
            elif ap in used_approvals:
                bad(f"金库批准 #{ap} 已被 {used_approvals[ap]} 引用：每份批准只对一张试验卡有效")
            else:
                used_approvals[ap] = tid
        if mode == "blind_replication":
            if segment != "vault":
                bad("盲复现试验卡的 segment 必须为 vault")
            if "approval" not in record:
                bad("盲复现试验卡必须写 approval")
            if "repeat_of" in record:
                bad("盲复现试验卡不能设置 repeat_of")
            if _parse_iso_utc(record.get("max_knowable_time")) is None:
                bad("盲复现试验卡必须写带 UTC 时区的 max_knowable_time")
            if not _valid_inherited_dict(record.get("inherited")):
                bad("盲复现试验卡必须写 inherited（含 source 和 path）")
        else:
            if "max_knowable_time" in record:
                bad("只有盲复现模式（mode=blind_replication）才写 max_knowable_time")
            if "inherited" in record:
                bad("只有盲复现模式（mode=blind_replication）才写 inherited")
        if "repeat_of" in record:
            original = seen.get(record["repeat_of"])
            if original is None:
                bad(f"repeat_of 指向的试验 {record['repeat_of']} 不在它前面")
            elif segment in GUARDED:
                bad("确认集、金库、前向只能用一次，不能重跑")
            elif any(original.record[k] != record[k] for k in ("hypothesis", "study", "segment")):
                bad("重跑必须和原试验的假说、研究代码、数据段完全相同")
            elif original.params != record["params"]:
                bad("重跑必须和原试验的 params 完全相同")
        seen[tid] = Trial(tid, record)
        if strict is None or tid in strict:
            found.extend(f"{tid}：{m}" for m in _runnable(tid, record, read, approvals))
    return found


def _runnable(
    tid: str, record: dict, read: Callable[[str], str | None], approvals: str
) -> list[str]:
    """Checks against other files: what a card needs to be run now."""
    found = []
    hypothesis, study, segment = record["hypothesis"], record["study"], record["segment"]
    mode = record.get("mode", "standard")
    if STUDY.match(str(study)) and read(f"{STUDIES_DIR}{study}.py") is None:
        found.append(f"研究代码 {STUDIES_DIR}{study}.py 不存在")
    card = _card(read, hypothesis)
    if card is None:
        return [*found, f"假说卡 registry/hypotheses/{hypothesis}.toml 不存在或无法解析"]
    universe = card.get("universe")
    family = card.get("family")
    if (universe == "synthetic") != (segment == "synthetic"):
        found.append("合成数据的假说只能用 synthetic 段，真实数据的假说不能用 synthetic 段")
    if segment in GUARDED:
        planned = (card.get("registration") or {}).get("segments") or []
        if card.get("status") not in READABLE_STATUSES:
            found.append(
                f"读 {segment} 段需要状态为 registered 或 confirmed 的假说，"
                f"{hypothesis} 现在是 {card.get('status')!r}"
            )
        elif segment not in planned:
            found.append(f"{hypothesis} 注册时没有计划用 {segment} 段")

    ucard = _universe(read, str(universe)) if universe and universe != "synthetic" else None
    exposure = (ucard or {}).get("exposure") or {}
    families_burned = exposure.get("families_burned") or []
    vault_burned = exposure.get("vault_burned") or []

    if mode == "standard":
        if family in families_burned and segment in ("confirmation", "vault"):
            found.append(
                f"假说族 {family} 已在 {universe} 的 families_burned 中，"
                f"普通试验不能再读 {segment} 段（复现请用 mode=blind_replication）"
            )
        elif family in vault_burned and segment == "vault":
            found.append(f"假说族 {family} 已在 {universe} 的 vault_burned 中，不能再读金库段")

    if segment == "vault":
        ap_rec = _find_approval(approvals, record.get("approval"), str(universe), hypothesis, tid)
        if ap_rec is None:
            found.append(
                "读金库段要写 approval：所有者已合并批准记录的 seq"
                f"（须与 {universe}、{hypothesis}、{tid} 一一绑定）"
            )
        elif ap_rec.get("mode", "standard") != mode:
            found.append(
                f"批准记录的 mode 为 {ap_rec.get('mode', 'standard')!r}，与试验卡 {mode!r} 不一致"
            )
        elif mode == "blind_replication":
            ap_limit = _parse_iso_utc(ap_rec.get("max_knowable_time"))
            card_limit = _parse_iso_utc(record.get("max_knowable_time"))
            if (
                ap_rec.get("study") != study
                or ap_limit != card_limit
                or ap_rec.get("inherited") != record.get("inherited")
            ):
                found.append(
                    "盲复现批准记录的 study / max_knowable_time / inherited 与试验卡不一致"
                )

    if mode == "blind_replication":
        if study not in blind.COMPARATORS:
            found.append(f"盲复现研究 {study!r} 没有在 sqp.research.blind 中注册比对器")
        if family not in families_burned:
            found.append(
                f"假说族 {family!r} 不在 {universe} 的 families_burned 中，不能走盲复现通道"
            )
        through = exposure.get("burned_through")
        card_limit = _parse_iso_utc(record.get("max_knowable_time"))
        if not isinstance(through, date):
            found.append(f"{universe} 缺少合法的 exposure.burned_through，不能走盲复现通道")
        elif card_limit is not None:
            cutoff = datetime.combine(through + timedelta(days=1), time(), tzinfo=UTC)
            if card_limit >= cutoff:
                found.append(
                    f"盲复现 max_knowable_time ({record.get('max_knowable_time')}) "
                    f"超过了 burned_through ({through})"
                )
        inherited = record.get("inherited")
        if _valid_inherited_dict(inherited):
            assert isinstance(inherited, dict)
            if blind.manifest_entry(read, inherited["source"], inherited["path"]) is None:
                found.append(
                    f"盲复现引用的继承基线 {inherited['source']}/{inherited['path']} "
                    "不在 registry/inherited/manifest.json 中"
                )
    return found


def counted(text: str, read: Callable[[str], str | None], universe: str) -> int:
    """Trials that count against a universe: this generation's (synthetic runs
    excluded; a card counts once it is registered, run or not; repeats count too)
    plus the inherited ones of the universe's domain."""
    own = 0
    for trial in parse(text):
        if trial.segment == "synthetic":
            continue
        card = _card(read, trial.hypothesis) or {}
        if card.get("universe") == universe:
            tests = trial.record.get("tests", 1)
            own += tests if isinstance(tests, int) and tests >= 1 else 1
    universe_card = tomllib.loads(read(f"registry/universes/{universe}.toml") or "")
    inherited = json.loads(read(INHERITED) or "{}")
    by_domain = inherited.get("summary", {}).get("counted_by_domain", {})
    return own + int(by_domain.get(universe_card.get("domain"), 0))
