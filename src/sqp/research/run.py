"""Run one registered trial (decision 0025).

Order, so that nothing is read before it is on record:

1. the card must be merged on main, and the whole trials ledger must check out;
2. only GitHub Actions running main's code, started by a schedule or by hand;
3. every trial writes `ledger/trials/<T>.started.json`, then a confirmation trial
   also writes `ledger/confirmations/<universe>/<H>.json`. Both are append-only, so
   a second run of the same card (a GitHub re-run included), or a second
   confirmation of a hypothesis, is refused before it reads anything; a run that
   crashes after the first marker still counts and is never picked up again;
4. the study runs with a point-in-time reader limited to the card's segment:
   exploration cards get no `purpose` and no guarded segment, a confirmation card
   only confirmation, and so on (decision 0025);
5. the metrics and the standard report go to `derived/trials/<T>/`, and
   `ledger/trials/<T>.completed.json` records their fingerprints.

Only object names and fingerprints are printed: logs are public (decision 0011).
Whatever the study prints is captured into `derived/trials/<T>/console.log`.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib
import io
import json
import os
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqp import ledger
from sqp.data.pit import PointInTime, _git, _on_main, _running_main_in_actions
from sqp.research import trials
from sqp.research.studies import SECTIONS
from sqp.storage import ObjectExists, Store

PACKAGE = "sqp.research.studies"
LEDGER_PREFIX = "ledger/trials/"
CONFIRMATIONS_PREFIX = "ledger/confirmations/"
DERIVED_PREFIX = "derived/trials/"
JSON = "application/json; charset=utf-8"


class TrialRefused(Exception):
    pass


def started_key(tid: str) -> str:
    return f"{LEDGER_PREFIX}{tid}.started.json"


def completed_key(tid: str) -> str:
    return f"{LEDGER_PREFIX}{tid}.completed.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _run_info() -> dict:
    env = os.environ
    return {k.lower(): env.get(k) for k in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA")}


@dataclass
class Finished:
    trial: str
    lines: list[str] = field(default_factory=list)
    alarms: list[str] = field(default_factory=list)


def _main_trials(repo_root: Path, strict: set[str]) -> tuple[str, list[trials.Trial]]:
    """The trials ledger on main. Ledger-wide rules are checked for every card; the
    checks against other files only for the cards in `strict`."""
    text = _on_main(repo_root, trials.TRIALS) or ""
    if broken := ledger.verify(text):
        raise TrialRefused("main 上的试验账本损坏：" + "；".join(broken))
    if found := trials.problems(text, lambda p: _on_main(repo_root, p), strict=strict):
        raise TrialRefused("main 上的试验卡有问题：" + "；".join(found))
    return text, trials.parse(text)


def pending(repo_root: Path, store: Store) -> list[str]:
    """Cards merged on main that have not started yet, oldest first."""
    _, cards = _main_trials(repo_root, set())
    return [t.id for t in cards if not store.exists(started_key(t.id))]


def _put_marker(store: Store, key: str, doc: dict, refusal: str) -> None:
    try:
        store.put(key, ledger.canonical(doc), content_type=JSON)
    except ObjectExists:
        raise TrialRefused(refusal) from None


def _report(trial: trials.Trial, card: dict, sections: dict, facts: dict) -> bytes:
    lines = [
        f"# {trial.id} · {trial.hypothesis} · {card.get('title', '')}",
        "",
        f"- 研究代码：`{trial.study}`；数据段：{trial.segment}；代码提交：`{facts['code_commit']}`",
        f"- 试验卡：{trial.record['note']}",
        "",
    ]
    for number, name in enumerate(SECTIONS, start=1):
        lines += [f"## {number}. {name}", "", sections[name].strip(), ""]
        if name == "数据":
            lines += [
                f"- 读过的分块：{facts['parts']} 个；输入指纹：`{facts['inputs_sha256']}`",
                "",
            ]
        if name == "结果" and facts["counted"] is not None:
            lines += [f"- 本研究宇宙累计试验次数（含继承，含本次）：{facts['counted']}", ""]
    return "\n".join(lines).encode()


def _outcome(result: object) -> tuple[dict, dict]:
    metrics = getattr(result, "metrics", None)
    sections = getattr(result, "sections", None)
    if not isinstance(metrics, dict) or not isinstance(sections, dict):
        raise TypeError("研究代码必须返回 Outcome(metrics=dict, sections=dict)")
    missing = [s for s in SECTIONS if not str(sections.get(s, "")).strip()]
    if missing:
        raise ValueError(f"标准报告缺少章节：{'、'.join(missing)}（不适用就写明“不适用”和原因）")
    return metrics, sections


def run_trial(
    repo_root: Path,
    store: Store,
    tid: str,
    *,
    package: str = PACKAGE,
    cache_dir: Path | None = None,
) -> Finished:
    if not trials.TRIAL_ID.match(tid):
        raise TrialRefused(f"试验编号不对：{tid!r}")
    text, cards = _main_trials(repo_root, {tid})
    trial = next((t for t in cards if t.id == tid), None)
    if trial is None:
        raise TrialRefused(f"main 上没有试验卡 {tid}：先开 PR 登记，所有者合并之后才能跑")
    if not _running_main_in_actions(repo_root):
        raise TrialRefused("试验只能在 GitHub Actions 里、由定时或手动触发、运行 main 的代码")
    card = tomllib.loads(_on_main(repo_root, f"registry/hypotheses/{trial.hypothesis}.toml") or "")
    universe = card["universe"]
    if trial.repeat_of and not store.exists(completed_key(trial.repeat_of)):
        raise TrialRefused(f"要重跑的 {trial.repeat_of} 还没有跑完，没有可以比对的指纹")
    commit = (_git(repo_root, "rev-parse", "HEAD") or "").strip()
    stamp = {"trial": tid, "card_hash": trial.record["hash"], "code_commit": commit}

    _put_marker(
        store,
        started_key(tid),
        {**stamp, "at": _now(), **_run_info()},
        f"{tid} 已经开始过（每张试验卡只跑一次；要再试，登记一张新卡）",
    )
    if trial.segment == "confirmation":
        _put_marker(
            store,
            f"{CONFIRMATIONS_PREFIX}{universe}/{trial.hypothesis}.json",
            {**stamp, "at": _now(), **_run_info()},
            f"{trial.hypothesis} 已经做过确认检验：每个假说只做一次",
        )

    params = copy.deepcopy(trial.params)  # the study may change its copy, never the card's
    pit = None
    if trial.segment != "synthetic":
        guarded = trial.segment in trials.GUARDED
        pit = PointInTime(
            repo_root,
            store,
            universe=universe,
            purpose=trial.hypothesis if guarded else None,
            cache_dir=cache_dir,
            allowed=frozenset({trial.segment} if guarded else ()),
        )
    console = io.StringIO()
    try:
        study = importlib.import_module(f"{package}.{trial.study}")
        with contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
            metrics, sections = _outcome(study.run(pit, params))
        inputs = dict(pit.inputs) if pit is not None else {}
        # allow_nan=False: NaN and Infinity are not JSON; a study must say what it means
        result = json.dumps(
            {"metrics": metrics}, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()  # no trial id: comparable across repeats
        counted = (
            None if universe == "synthetic" else trials.counted(text, _read(repo_root), universe)
        )
        facts = {
            "code_commit": commit,
            "parts": len(inputs),
            "inputs_sha256": _sha(ledger.canonical(inputs)),
            "counted": counted,
        }
        report = _report(trial, card, sections, facts)
    except Exception as exc:
        # The exception's message may carry numbers, so only its type is stored and
        # shown; the run still counts as a trial (decision 0025).
        failed = {**stamp, "at": _now(), "error": type(exc).__name__, **_run_info()}
        store.put(f"{LEDGER_PREFIX}{tid}.failed.json", ledger.canonical(failed), content_type=JSON)
        _keep_console(store, tid, console)
        raise
    _keep_console(store, tid, console)
    store.put(f"{DERIVED_PREFIX}{tid}/result.json", result, content_type=JSON)
    store.put(
        f"{DERIVED_PREFIX}{tid}/report.md", report, content_type="text/markdown; charset=utf-8"
    )
    completed = {
        **stamp,
        "params_sha256": _sha(ledger.canonical(trial.params)),
        "inputs_sha256": facts["inputs_sha256"],
        "result_sha256": _sha(result),
        "report_sha256": _sha(report),
        "at": _now(),
        **_run_info(),
    }
    store.put(completed_key(tid), ledger.canonical(completed), content_type=JSON)

    done = Finished(tid)
    done.lines += [
        f"试验 {tid}（{trial.hypothesis}，{trial.segment}）跑完。",
        f"结果：{DERIVED_PREFIX}{tid}/result.json",
        f"报告：{DERIVED_PREFIX}{tid}/report.md",
        f"指纹：输入 {completed['inputs_sha256']}；结果 {completed['result_sha256']}；"
        f"报告 {completed['report_sha256']}",
    ]
    if trial.repeat_of:
        original = json.loads(store.get(completed_key(trial.repeat_of)))
        same = all(original[k] == completed[k] for k in ("inputs_sha256", "result_sha256"))
        was = original["code_commit"]
        code = (
            "代码提交相同" if was == commit else f"代码提交不同（原 {was[:12]}，现 {commit[:12]}）"
        )
        if same:
            done.lines.append(f"和 {trial.repeat_of} 的输入、结果指纹一致（{code}）：重算一致。")
        else:
            done.alarms.append(f"和 {trial.repeat_of} 的指纹不一致（{code}）：结果不可重现。")
    return done


def _keep_console(store: Store, tid: str, console: io.StringIO) -> None:
    """What the study printed goes to the private bucket, never to the public log."""
    if text := console.getvalue():
        store.put(
            f"{DERIVED_PREFIX}{tid}/console.log",
            text.encode(),
            content_type="text/plain; charset=utf-8",
        )


def _read(repo_root: Path):
    return lambda path: _on_main(repo_root, path)
