"""Trial cards (decision 0025): every card in the trials ledger must be runnable as written."""

from __future__ import annotations

from sqp.checks import Violation
from sqp.config import Governance
from sqp.repo import Repo
from sqp.research import trials


def check(repo: Repo, cfg: Governance, paths: list[str]) -> list[Violation]:
    if trials.TRIALS not in paths:
        return []
    text = repo.text(trials.TRIALS) or ""
    files = set(paths)

    def read(path: str) -> str | None:
        return repo.text(path) if path in files else None

    return [Violation("trial-card", trials.TRIALS, p) for p in trials.problems(text, read)]
