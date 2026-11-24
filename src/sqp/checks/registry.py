"""Registry rules: STAGE value, universe cards, hypothesis cards, ledger chains."""

from __future__ import annotations

import itertools
import json
import re
import tomllib
from datetime import date, timedelta
from pathlib import PurePosixPath

from sqp import ledger
from sqp.checks import Violation
from sqp.config import PROTOCOL_PATH, Governance, load_protocol, matches
from sqp.repo import Repo

SEGMENTS = ("exploration", "confirmation", "vault")
STATUSES = ("draft", "registered", "confirmed", "inconclusive", "rejected", "retired")
HYPOTHESIS_ID = re.compile(r"^H-\d{4}$")
REQUIRED = {
    "id": str,
    "title": str,
    "family": str,
    "universe": str,
    "status": str,
    "created": date,
    "mechanism": str,
    "predictions": list,
    "falsification": str,
}
THRESHOLD_KEYS = ("t_stat", "deflated_sharpe", "pbo", "cost_multiplier")
DAY = timedelta(days=1)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _load(repo: Repo, path: str, rule: str) -> tuple[dict | None, list[Violation]]:
    try:
        return tomllib.loads(repo.text(path) or ""), []
    except tomllib.TOMLDecodeError as exc:
        return None, [Violation(rule, path, f"TOML 格式错误：{exc}")]


def _universe(path: str, card: dict) -> list[Violation]:
    found = []
    stem = PurePosixPath(path).stem
    if card.get("id") != stem:
        found.append(Violation("universe-schema", path, f"id 必须等于文件名 {stem}"))
    for key in ("name", "domain", "scope"):
        if not isinstance(card.get(key), str) or not card[key]:
            found.append(Violation("universe-schema", path, f"缺少文字字段 {key}"))
    segments = card.get("segments", {})
    spans = []
    for name in SEGMENTS:
        span = segments.get(name, {})
        start, end = span.get("start"), span.get("end")
        if not (isinstance(start, date) and isinstance(end, date) and start <= end):
            return [*found, Violation("universe-schema", path, f"segments.{name} 需要 start ≤ end")]
        spans.append((name, start, end))
    for (first, _, first_end), (second, second_start, _) in itertools.pairwise(spans):
        if second_start != first_end + DAY:
            found.append(
                Violation("universe-schema", path, f"{second} 必须在 {first} 结束的第二天开始")
            )
    if segments.get("forward", {}).get("start") != spans[-1][2] + DAY:
        found.append(
            Violation("universe-schema", path, "segments.forward.start 必须是金库结束的第二天")
        )
    return found


def _exposure(repo: Repo, cfg: Governance, path: str, card: dict) -> list[Violation]:
    raw = repo.text(cfg.inherited_trials)
    if raw is None:
        return []
    families = json.loads(raw)["summary"]["families"]
    confirmation_start = card["segments"]["confirmation"]["start"].isoformat()
    domains = {card["domain"], "mixed"}
    seen = {
        name: fam
        for name, fam in families.items()
        if fam["domain"] in domains and (fam.get("last") or "") >= confirmation_start
    }
    exposure = card.get("exposure", {})
    found = []
    missing = sorted(set(seen) - set(exposure.get("families_burned", [])))
    if missing:
        found.append(
            Violation(
                "universe-exposure",
                path,
                "前代看过确认集或金库的假说族没有在 exposure.families_burned 里声明："
                + "、".join(missing),
            )
        )
    if seen:
        latest = max(fam["last"] for fam in seen.values())
        through = exposure.get("burned_through")
        if not isinstance(through, date) or through.isoformat() < latest:
            found.append(
                Violation(
                    "universe-exposure",
                    path,
                    f"exposure.burned_through 不能早于前代用到的最后日期 {latest}",
                )
            )
    return found


def _thresholds(path: str, thresholds: dict, registration: dict, default: dict) -> list[Violation]:
    found = []
    for key in THRESHOLD_KEYS:
        if not _is_number(thresholds.get(key)):
            found.append(Violation("hypothesis-schema", path, f"thresholds.{key} 必须是数字"))
    if found:
        return found
    t_stat = thresholds["t_stat"]
    if t_stat < default["t_stat"]:
        exception = registration.get("mechanism_exception")
        if t_stat < default["t_stat_with_mechanism"]:
            found.append(
                Violation(
                    "hypothesis-schema",
                    path,
                    f"t_stat 不能低于 {default['t_stat_with_mechanism']}",
                )
            )
        elif not (isinstance(exception, str) and exception):
            found.append(
                Violation(
                    "hypothesis-schema",
                    path,
                    f"t_stat 低于 {default['t_stat']} 时，"
                    "registration.mechanism_exception 必须写明机制理由",
                )
            )
    if thresholds["deflated_sharpe"] < default["deflated_sharpe"]:
        found.append(Violation("hypothesis-schema", path, "deflated_sharpe 只能比默认更严"))
    if thresholds["pbo"] > default["pbo"]:
        found.append(Violation("hypothesis-schema", path, "pbo 只能比默认更严"))
    if thresholds["cost_multiplier"] < default["cost_multiplier"]:
        found.append(Violation("hypothesis-schema", path, "cost_multiplier 只能比默认更严"))
    return found


def _hypothesis(path: str, card: dict, universes: set[str], protocol: dict) -> list[Violation]:
    found = []
    for key, kind in REQUIRED.items():
        value = card.get(key)
        if not isinstance(value, kind) or (kind in (str, list) and not value):
            found.append(Violation("hypothesis-schema", path, f"缺少字段或类型不对：{key}"))
    if found:
        return found
    stem = PurePosixPath(path).stem
    if not HYPOTHESIS_ID.match(card["id"]) or card["id"] != stem:
        found.append(
            Violation("hypothesis-schema", path, f"id 必须形如 H-0001，并等于文件名 {stem}")
        )
    if card["status"] not in STATUSES:
        found.append(
            Violation("hypothesis-schema", path, f"status 必须是 {'/'.join(STATUSES)} 之一")
        )
    if card["universe"] != "synthetic" and card["universe"] not in universes:
        found.append(Violation("hypothesis-schema", path, f"研究宇宙 {card['universe']} 不存在"))
    if card["status"] == "draft":
        return found
    registration, thresholds = card.get("registration"), card.get("thresholds")
    if not isinstance(registration, dict) or not isinstance(thresholds, dict):
        return [
            *found,
            Violation("hypothesis-schema", path, "已注册的假说需要 [registration] 和 [thresholds]"),
        ]
    if not isinstance(registration.get("registered_at"), date):
        found.append(Violation("hypothesis-schema", path, "registration.registered_at 必须是日期"))
    names = protocol["segments"]["names"]
    segments = registration.get("segments")
    if not isinstance(segments, list) or not segments or any(s not in names for s in segments):
        found.append(
            Violation("hypothesis-schema", path, f"registration.segments 只能取 {names} 中的值")
        )
    effect = registration.get("min_effect_sharpe")
    if not _is_number(effect) or effect <= 0:
        found.append(
            Violation(
                "hypothesis-schema",
                path,
                "registration.min_effect_sharpe 必须是正数（事先认为合理的最小年化夏普）",
            )
        )
    if not isinstance(registration.get("test_plan"), str) or not registration["test_plan"]:
        found.append(Violation("hypothesis-schema", path, "registration.test_plan 不能为空"))
    return found + _thresholds(path, thresholds, registration, protocol["thresholds"])


def check(repo: Repo, cfg: Governance, paths: list[str]) -> list[Violation]:
    found = []
    stage = (repo.text(cfg.stage_file) or "").strip()
    if stage not in cfg.stages:
        found.append(
            Violation(
                "stage-value",
                cfg.stage_file,
                f"STAGE 必须是 {'/'.join(cfg.stages)} 之一，现在是 {stage!r}",
            )
        )

    universes: set[str] = set()
    for path in (p for p in paths if matches(p, (cfg.universes_glob,))):
        card, problems = _load(repo, path, "universe-schema")
        if card is not None:
            problems = _universe(path, card)
            if not problems:
                universes.add(card["id"])
                problems = _exposure(repo, cfg, path, card)
        found += problems

    protocol_text = repo.text(PROTOCOL_PATH)
    hypothesis_paths = [p for p in paths if matches(p, (cfg.hypotheses_glob,))]
    if protocol_text is None:
        found.append(Violation("config", PROTOCOL_PATH, "缺少科研规程配置"))
        hypothesis_paths = []
    for path in hypothesis_paths:
        card, problems = _load(repo, path, "hypothesis-schema")
        if card is not None:
            problems = _hypothesis(path, card, universes, load_protocol(protocol_text))
        found += problems

    for path in (p for p in paths if matches(p, cfg.ledger_globs)):
        data = repo.read(path) or b""
        found += [
            Violation("ledger-chain", path, problem)
            for problem in ledger.verify(data.decode("utf-8", "replace"))
        ]

    for path in (p for p in paths if matches(p, cfg.derived_jsonl_globs)):
        found += _derived(repo, path, cfg.derived_forbidden_keys)
    return found


def _derived(repo: Repo, path: str, forbidden: tuple[str, ...]) -> list[Violation]:
    found = []
    lines = (repo.read(path) or b"").decode("utf-8", "replace").splitlines()
    for number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # malformed ledger lines are reported by ledger-chain
        keys = sorted(set(record) & set(forbidden)) if isinstance(record, dict) else []
        if keys:
            found.append(
                Violation(
                    "derived-results",
                    path,
                    f"第 {number} 行带有数值结果字段 {', '.join(keys)}。"
                    "由第三方数据算出的数值放私有存储桶，公开仓库只记指纹",
                )
            )
    return found
