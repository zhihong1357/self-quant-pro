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

import builtins
import contextlib
import copy
import hashlib
import importlib
import io
import json
import os
import sys
import tomllib
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqp import ledger
from sqp.data.pit import (
    AccessRefused,
    IntegrityError,
    PointInTime,
    VaultReceipt,
    _git,
    _on_main,
    _running_main_in_actions,
    check_vault_approval,
    consume_vault_approval,
    dataset_window_ready,
    parse_iso_utc,
)
from sqp.research import blind, trials
from sqp.research.studies import SECTIONS, VERDICTS, BlindMetrics
from sqp.storage import ObjectExists, Store

PACKAGE = "sqp.research.studies"
LEDGER_PREFIX = "ledger/trials/"
CONFIRMATIONS_PREFIX = "ledger/confirmations/"
DERIVED_PREFIX = "derived/trials/"
JSON = "application/json; charset=utf-8"


class TrialRefused(Exception):
    pass


class BlindTrialFailed(Exception):
    """Post-start failure of a blind replication trial; public log prints a fixed line only."""


def _is_verified_module_type(kind: type, prefixes: tuple[str, ...]) -> bool:
    mod_name = getattr(kind, "__module__", "")
    if not isinstance(mod_name, str) or not mod_name.startswith(prefixes):
        return False
    mod = sys.modules.get(mod_name)
    return mod is not None and getattr(mod, kind.__name__, None) is kind


def error_name(exc: BaseException) -> str:
    """The error's type if it is a built-in or known framework/network error.

    A type made by study code is never named in public logs or `failed.json`:
    its name could be built from data (`type("S3_14", ...)`).
    """
    kind = type(exc)
    if getattr(builtins, kind.__name__, None) is kind or kind in (
        AccessRefused,
        IntegrityError,
        TrialRefused,
        BlindTrialFailed,
        urllib.error.URLError,
        urllib.error.HTTPError,
    ):
        return kind.__name__
    if _is_verified_module_type(kind, ("google.api_core.", "google.cloud.")):
        return kind.__name__
    return "研究代码自己定义的错误类型（名字不打印）"


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
    approvals = _on_main(repo_root, trials.APPROVALS) or ""
    if broken_ap := ledger.verify(approvals):
        raise TrialRefused("main 上的批准账本损坏：" + "；".join(broken_ap))
    if bad_ap := trials.approval_problems(approvals):
        raise TrialRefused("main 上的金库批准记录有问题：" + "；".join(bad_ap))
    text = _on_main(repo_root, trials.TRIALS) or ""
    if broken := ledger.verify(text):
        raise TrialRefused("main 上的试验账本损坏：" + "；".join(broken))
    if found := trials.problems(text, lambda p: _on_main(repo_root, p), strict=strict):
        raise TrialRefused("main 上的试验卡有问题：" + "；".join(found))
    return text, trials.parse(text)


def pending(repo_root: Path, store: Store) -> list[str]:
    """Cards merged on main that have not started yet and can run now, oldest first."""
    _, cards = _main_trials(repo_root, set())
    waiting = [t for t in cards if not store.exists(started_key(t.id))]
    # A repeat waits until its original has finished; if the original failed, the
    # repeat never becomes runnable, and it must not fail every scheduled run.
    return [t.id for t in waiting if not t.repeat_of or store.exists(completed_key(t.repeat_of))]


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


def _outcome(result: object) -> tuple[dict, dict, str | None]:
    if isinstance(result, BlindMetrics):
        raise TypeError("普通试验不能返回 BlindMetrics，必须返回 Outcome")
    metrics = getattr(result, "metrics", None)
    sections = getattr(result, "sections", None)
    verdict = getattr(result, "verdict", None)
    if not isinstance(metrics, dict) or not isinstance(sections, dict):
        raise TypeError("研究代码必须返回 Outcome(metrics=dict, sections=dict)")
    missing = [s for s in SECTIONS if not str(sections.get(s, "")).strip()]
    if missing:
        raise ValueError(f"标准报告缺少章节：{'、'.join(missing)}（不适用就写明“不适用”和原因）")
    if verdict is not None:
        # Exactly a str, then replaced by our own copy: a str subclass could pass the
        # membership test with its own __eq__ and print anything (logs are public).
        if type(verdict) is not str or verdict not in VERDICTS:
            raise ValueError(f"判定只能是 {'、'.join(VERDICTS)} 之一")  # never echo it
        verdict = VERDICTS[VERDICTS.index(verdict)]
    return metrics, sections, verdict


def _preflight_readiness(store: Store, params: dict, *, required: bool = False) -> None:
    """Verify required dataset months/symbols exist in .inputs.json before consuming approval."""
    dataset = params.get("required_dataset")
    symbols = params.get("required_symbols")
    start_month = params.get("required_start_month")
    end_month = params.get("required_end_month")
    if not any(v is not None for v in (dataset, symbols, start_month, end_month)):
        if required:
            raise TrialRefused(
                "盲复现试验卡缺少必填的 required_dataset / required_symbols / 月份前置检查参数"
            )
        return
    if not (
        isinstance(dataset, str)
        and isinstance(symbols, list)
        and symbols
        and all(isinstance(s, str) and s for s in symbols)
        and isinstance(start_month, str)
        and isinstance(end_month, str)
    ):
        raise TrialRefused(
            "试验卡的 required_dataset / required_symbols / 月份前置检查参数格式不对"
        )
    missing = dataset_window_ready(store, dataset, symbols, start_month, end_month)
    if missing:
        preview = "、".join(missing[:6])
        more = f" 等共 {len(missing)} 项" if len(missing) > 6 else ""
        raise TrialRefused(f"数据还没到齐，本次未开始、未消耗批准：缺少 {preview}{more}")


@contextlib.contextmanager
def _silence_os_fds():
    """Redirect OS fd 1 & 2 to /dev/null during blind study execution (catches Rust/C output)."""
    saved: list[tuple[int, int]] = []
    devnull: int | None = None
    try:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            for fd in (1, 2):
                saved.append((fd, os.dup(fd)))
                os.dup2(devnull, fd)
        except OSError:
            pass
        yield
    finally:
        for fd, copy_fd in saved:
            with contextlib.suppress(OSError):
                os.dup2(copy_fd, fd)
            with contextlib.suppress(OSError):
                os.close(copy_fd)
        if devnull is not None:
            with contextlib.suppress(OSError):
                os.close(devnull)


def _invoke_study(
    package: str,
    study_name: str,
    pit: PointInTime | None,
    params: dict,
    console: io.StringIO,
    *,
    silence_fds: bool,
) -> object:
    """Run study code in its own stack frame without reference bytes or receipts in locals."""
    study = importlib.import_module(f"{package}.{study_name}")
    with contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
        if silence_fds:
            with _silence_os_fds():
                return study.run(pit, params)
        return study.run(pit, params)


def run_trial(
    repo_root: Path,
    store: Store,
    tid: str,
    *,
    package: str = PACKAGE,
    cache_dir: Path | None = None,
    fetch: Callable[[str], bytes] | None = None,
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
    if store.exists(started_key(tid)):
        raise TrialRefused(f"{tid} 已经开始过（每张试验卡只跑一次；要再试，登记一张新卡）")
    commit = (_git(repo_root, "rev-parse", "HEAD") or "").strip()
    stamp = {"trial": tid, "card_hash": trial.record["hash"], "code_commit": commit}

    is_blind = trial.mode == "blind_replication"
    ref: blind.InheritedRef | None = None
    ref_bytes: bytes | None = None
    limit_dt: datetime | None = None
    # Pre-flight: every check before started.json leaves the card & one-time approval intact.
    try:
        if is_blind:
            limit_dt = parse_iso_utc(trial.max_knowable_time)
            if limit_dt is None:
                raise TrialRefused("盲复现试验卡缺少合法的 max_knowable_time")
            if bad_params := blind.validate_params(
                trial.study, trial.params, max_knowable_time=limit_dt
            ):
                raise TrialRefused("前置检查失败，本次未开始、未消耗批准：" + "；".join(bad_params))
            _preflight_readiness(store, trial.params, required=True)
            inherited = trial.inherited or {}
            ref = blind.manifest_entry(
                _read(repo_root), inherited.get("source"), inherited.get("path")
            )
            if ref is None:
                raise TrialRefused("盲复现引用的继承基线不在 main 的 manifest.json 中")
            ref_bytes = blind.load_reference_bytes(ref, store, fetch=fetch)
            blind.validate_reference_bytes(trial.study, ref_bytes)
        if trial.segment == "vault":
            if trial.approval is None:
                raise TrialRefused("读金库段必须提供 approval（批准记录的 seq）")
            try:
                check_vault_approval(
                    repo_root,
                    store,
                    universe=universe,
                    hypothesis=trial.hypothesis,
                    trial=tid,
                    approval=trial.approval,
                    mode=trial.mode,
                    study=trial.study,
                    max_knowable_time=limit_dt,
                    inherited=trial.inherited,
                )
            except AccessRefused as exc:
                raise TrialRefused(str(exc)) from None
    except TrialRefused:
        raise
    except Exception as exc:
        raise TrialRefused(f"前置检查失败，本次未开始、未消耗批准：{error_name(exc)}") from None

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
    vault_receipt: VaultReceipt | None = None
    if trial.segment == "vault":
        if trial.approval is None:
            raise TrialRefused("读金库段必须提供 approval（批准记录的 seq）")
        try:
            vault_receipt = consume_vault_approval(
                repo_root,
                store,
                universe=universe,
                hypothesis=trial.hypothesis,
                trial=tid,
                approval=trial.approval,
                mode=trial.mode,
                study=trial.study,
                max_knowable_time=limit_dt,
                inherited=trial.inherited,
                stamp={**stamp, **_run_info()},
            )
        except AccessRefused as exc:
            raise TrialRefused(str(exc)) from None

    params = copy.deepcopy(trial.params)  # the study may change its copy, never the card's
    pit = None
    if trial.segment != "synthetic":
        guarded = trial.segment in trials.GUARDED
        allowed = (
            frozenset({"confirmation", "vault", "forward"})
            if is_blind
            else frozenset({trial.segment} if guarded else ())
        )
        pit = PointInTime(
            repo_root,
            store,
            universe=universe,
            purpose=trial.hypothesis if guarded else None,
            cache_dir=cache_dir,
            allowed=allowed,
            vault_receipt=vault_receipt,
            max_knowable_time=limit_dt if is_blind else None,
        )
    console = io.StringIO()
    try:
        raw_result = _invoke_study(package, trial.study, pit, params, console, silence_fds=is_blind)
        if is_blind:
            if type(raw_result) is not BlindMetrics:
                raise TypeError("盲复现研究必须返回内置 BlindMetrics")
            if ref is None or ref_bytes is None:
                raise RuntimeError("盲复现缺少已核对的对照基线")
            compared = blind.compare(
                trial.study,
                raw_result,
                ref_bytes,
                ref,
                trial.params,
                max_knowable_time=limit_dt,
            )
            metrics, sections, verdict = _outcome(compared)
            blind.assert_no_forbidden_keys(metrics)
        else:
            metrics, sections, verdict = _outcome(raw_result)
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
        # The exception's message or dynamic type name may carry numbers, so only a
        # sanitized type name is stored; the run still counts as a trial (decision 0025).
        # Blind replications discard console output completely and raise BlindTrialFailed
        # so the public CLI log reveals neither the error class nor any stack frames.
        failed = {**stamp, "at": _now(), "error": error_name(exc), **_run_info()}
        store.put(f"{LEDGER_PREFIX}{tid}.failed.json", ledger.canonical(failed), content_type=JSON)
        if not is_blind:
            _keep_console(store, tid, console)
            raise
        raise BlindTrialFailed("盲复现运行失败") from exc
    if not is_blind:
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
        "verdict": verdict,
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
    if verdict is not None:
        done.lines.append(f"判定：{verdict}")
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
