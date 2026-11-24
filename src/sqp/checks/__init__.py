"""Governance checks. Each module exposes check(repo, cfg, paths) -> list[Violation]."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqp.repo import Repo


@dataclass(frozen=True, order=True)
class Violation:
    rule: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.path}：{self.message}"


def run_all(repo: Repo) -> list[Violation]:
    from sqp.checks import code, decisions, files, history, registry, secrets
    from sqp.config import GOVERNANCE_PATH, load_governance

    text = repo.text(GOVERNANCE_PATH)
    if text is None:
        return [Violation("config", GOVERNANCE_PATH, "缺少治理配置文件")]
    cfg = load_governance(text)
    paths = repo.files()
    found: set[Violation] = set()
    for module in (files, secrets, code, registry, decisions, history):
        found.update(module.check(repo, cfg, paths))
    return sorted(found)
