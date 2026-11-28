"""Trial cards (decision 0025): every card added by a pull request must be runnable as
written; the ledger-wide rules (one confirmation per hypothesis, repeats) cover all cards."""

from __future__ import annotations

from sqp.checks import Violation
from sqp.config import Governance
from sqp.repo import Repo
from sqp.research import trials


def check(repo: Repo, cfg: Governance, paths: list[str]) -> list[Violation]:
    found: list[Violation] = []
    if trials.APPROVALS in paths:
        ap_text = repo.text(trials.APPROVALS) or ""
        found.extend(
            Violation("vault-approval", trials.APPROVALS, p)
            for p in trials.approval_problems(ap_text)
        )
    if trials.TRIALS not in paths:
        return found
    text = repo.text(trials.TRIALS) or ""
    files = set(paths)

    def read(path: str) -> str | None:
        return repo.text(path) if path in files else None

    # Without a base (a local `sqp check`) only the ledger-wide rules apply: whether an
    # old card could still run is not the question, and the runner checks the card it
    # runs anyway. With a base, the cards this pull request adds must be runnable.
    new: set[str] = set()
    if repo.base is not None:
        base = (repo.base_read(trials.TRIALS) or b"").decode("utf-8", "replace")
        new = {t.id for t in trials.parse(text)} - {t.id for t in trials.parse(base)}
    found.extend(
        Violation("trial-card", trials.TRIALS, p) for p in trials.problems(text, read, strict=new)
    )
    return found
