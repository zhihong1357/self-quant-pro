"""Trial cards (decision 0025): every card added by a pull request must be runnable as
written; the ledger-wide rules (one confirmation per hypothesis, repeats) cover all cards."""

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

    base = (repo.base_read(trials.TRIALS) or b"").decode("utf-8", "replace")
    old = {t.id for t in trials.parse(base)} if repo.base is not None else set()
    new = {t.id for t in trials.parse(text)} - old
    found = trials.problems(text, read, strict=new)
    return [Violation("trial-card", trials.TRIALS, p) for p in found]
