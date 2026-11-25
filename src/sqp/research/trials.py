"""Trial cards (decision 0025): what may run, registered on main before it runs.

Each trial is one line of registry/ledger/trials.jsonl, appended in a pull request
and merged by the owner before GitHub Actions may run it. The trial's id comes from
the ledger's sequence number: seq 7 is T-0007. A card holds no results; those stay
in the private bucket (decision 0011).

    {"type": "trial", "hypothesis": "H-0001", "study": "thin_slice",
     "segment": "exploration", "params": {...}, "note": "why this trial"}

Optional keys: "approval" (the seq of the owner's vault approval; required for the
vault segment) and "repeat_of" (an earlier exploration or synthetic trial run again
to show its fingerprints are reproducible). A repeat counts like any other trial:
over-counting is allowed, under-counting is not.

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

from sqp import ledger

TRIALS = "registry/ledger/trials.jsonl"
APPROVALS = "registry/ledger/approvals.jsonl"
INHERITED = "registry/inherited/trials.json"
STUDIES_DIR = "src/sqp/research/studies/"
SEGMENTS = ("exploration", "confirmation", "vault", "forward", "synthetic")
GUARDED = frozenset({"confirmation", "vault", "forward"})
READABLE_STATUSES = frozenset({"registered", "confirmed"})  # as in sqp.data.pit
HYPOTHESIS = re.compile(r"^H-\d{4}$")
STUDY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TRIAL_ID = re.compile(r"^T-\d{4,}$")
KEYS = frozenset({"type", "hypothesis", "study", "segment", "params", "note"})
OPTIONAL = frozenset({"approval", "repeat_of"})
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


def _card(read: Callable[[str], str | None], hypothesis: str) -> dict | None:
    text = read(f"registry/hypotheses/{hypothesis}.toml")
    if text is None:
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def _approved(approvals: str, seq: object, universe: str, hypothesis: str) -> bool:
    if ledger.verify(approvals):
        return False  # a broken chain approves nothing, as in sqp.data.pit
    for line in approvals.splitlines():
        record = json.loads(line)
        if record.get("seq") == seq:
            return (
                record.get("type") == "vault"
                and record.get("universe") == universe
                and record.get("hypothesis") == hypothesis
            )
    return False


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
        if segment == "confirmation" and "repeat_of" not in record:
            if hypothesis in confirmed:
                bad(f"{hypothesis} 已经有确认检验 {confirmed[hypothesis]}：每个假说只做一次")
            confirmed.setdefault(hypothesis, tid)
        if "approval" in record and segment != "vault":
            bad("只有金库段的试验才写 approval")
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
            found.extend(f"{tid}：{m}" for m in _runnable(record, read, approvals))
    return found


def _runnable(record: dict, read: Callable[[str], str | None], approvals: str) -> list[str]:
    """Checks against other files: what a card needs to be run now."""
    found = []
    hypothesis, study, segment = record["hypothesis"], record["study"], record["segment"]
    if STUDY.match(str(study)) and read(f"{STUDIES_DIR}{study}.py") is None:
        found.append(f"研究代码 {STUDIES_DIR}{study}.py 不存在")
    card = _card(read, hypothesis)
    if card is None:
        return [*found, f"假说卡 registry/hypotheses/{hypothesis}.toml 不存在或无法解析"]
    universe = card.get("universe")
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
    if segment == "vault" and not _approved(
        approvals, record.get("approval"), str(universe), hypothesis
    ):
        found.append("读金库段要写 approval：所有者批准记录的 seq（同一研究宇宙、同一假说）")
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
        own += card.get("universe") == universe
    universe_card = tomllib.loads(read(f"registry/universes/{universe}.toml") or "")
    inherited = json.loads(read(INHERITED) or "{}")
    by_domain = inherited.get("summary", {}).get("counted_by_domain", {})
    return own + int(by_domain.get(universe_card.get("domain"), 0))
