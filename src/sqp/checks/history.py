"""Rules that compare the working tree with the base commit."""

from __future__ import annotations

import re
import tomllib

from sqp.checks import Violation
from sqp.checks.decisions import is_decision
from sqp.config import Governance, matches
from sqp.repo import Repo

MUTABLE_HYPOTHESIS_KEYS = {"status", "status_history"}
PATH_TOKEN = re.compile(r"[A-Za-z0-9_./\-]+")


def _path_tokens(text: str) -> set[str]:
    return {token.rstrip(".") for token in PATH_TOKEN.findall(text)}


def _mentioned(path: str, tokens: set[str]) -> bool:
    """A decision unlocks a file by naming it exactly, or a folder ending in '/'."""
    return path in tokens or any(t.endswith("/") and path.startswith(t) for t in tokens)


def _without_status(data: bytes) -> list[str]:
    lines = data.decode("utf-8", "replace").splitlines()
    return [line for line in lines if not line.startswith("- 状态：")]


def _frozen_part(card: dict) -> dict:
    return {k: v for k, v in card.items() if k not in MUTABLE_HYPOTHESIS_KEYS}


def _hypothesis(path: str, old: bytes, new: bytes) -> list[Violation]:
    try:
        before = tomllib.loads(old.decode("utf-8"))
        after = tomllib.loads(new.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return []  # format problems are reported by the registry check
    if before.get("status", "draft") == "draft":
        return []
    found = []
    if _frozen_part(before) != _frozen_part(after):
        found.append(
            Violation(
                "hypothesis-frozen", path, "注册之后只能改 status 和 status_history，其余冻结"
            )
        )
    if after.get("status") == "draft":
        found.append(Violation("hypothesis-frozen", path, "注册之后不能退回草稿"))
    return found


def _stage(repo: Repo, cfg: Governance, added_decisions: str) -> list[Violation]:
    new = (repo.text(cfg.stage_file) or "").strip()
    old_raw = repo.base_read(cfg.stage_file)
    if old_raw is None:
        if new == cfg.stages[0]:
            return []
        return [Violation("stage-gate", cfg.stage_file, f"第一次建立 STAGE 必须是 {cfg.stages[0]}")]
    old = old_raw.decode("utf-8", "replace").strip()
    i, j = cfg.stage_index(old), cfg.stage_index(new)
    if old == new or i < 0 or j < 0:
        return []  # invalid values are reported by the registry check
    found = []
    if j > i + 1:
        found.append(Violation("stage-gate", cfg.stage_file, f"阶段不能跳级：{old} → {new}"))
    stage_name = re.compile(rf"(?<![A-Za-z0-9]){re.escape(new)}(?![A-Za-z0-9])")
    if not any(
        "STAGE" in line and stage_name.search(line) for line in added_decisions.splitlines()
    ):
        found.append(
            Violation(
                "stage-gate",
                cfg.stage_file,
                f"改阶段（{old} → {new}）必须在同一个 PR 新增决策记录，"
                f"正文的同一行写明 STAGE 和 {new}",
            )
        )
    return found


def check(repo: Repo, cfg: Governance, paths: list[str]) -> list[Violation]:
    if repo.base is None:
        return []
    base, head = set(repo.base_files()), set(paths)
    added_decisions = "\n".join(
        repo.text(p) or "" for p in sorted(head - base) if is_decision(p, cfg)
    )
    tokens = _path_tokens(added_decisions)
    found = [
        Violation("no-delete", path, "这个文件不允许删除")
        for path in sorted(base - head)
        if matches(path, cfg.no_delete_globs)
    ]
    for path in sorted(base & head):
        old, new = repo.base_read(path), repo.read(path)
        if old is None or new is None or old == new:
            continue
        if matches(path, cfg.ledger_globs):
            if not new.startswith(old):
                found.append(
                    Violation(
                        "ledger-append-only",
                        path,
                        "账本只能在末尾追加，旧记录被改动或删除了。写错了就追加一条更正记录",
                    )
                )
        elif is_decision(path, cfg):
            if _without_status(old) != _without_status(new):
                found.append(
                    Violation(
                        "decision-frozen",
                        path,
                        "已合并的决策只能改“状态”一行；要改内容，新增一份决策取代它",
                    )
                )
        elif matches(path, (cfg.hypotheses_glob,)):
            found += _hypothesis(path, old, new)
        elif matches(path, cfg.frozen_globs) and not _mentioned(path, tokens):
            found.append(
                Violation(
                    "frozen-file",
                    path,
                    f"冻结文件被改动。确需改动：在同一个 PR 新增决策记录，"
                    f"正文写明完整路径 {path}（或以 / 结尾的所在目录）",
                )
            )
    return found + _stage(repo, cfg, added_decisions)
