"""Secret patterns, in the current files and in every line added since the base."""

from __future__ import annotations

import re

from sqp.checks import Violation
from sqp.config import Governance
from sqp.repo import Repo

MAX_SCAN_BYTES = 5_000_000


def _first_hit(line: str, patterns: list[tuple[str, re.Pattern[str]]]) -> str | None:
    for name, pattern in patterns:
        if pattern.search(line):
            return name
    return None


def check(repo: Repo, cfg: Governance, paths: list[str]) -> list[Violation]:
    patterns = [(p.name, re.compile(p.regex)) for p in cfg.secret_patterns]
    found = []
    for path in paths:
        data = repo.read(path)
        if data is None or len(data) > MAX_SCAN_BYTES or b"\0" in data[:8192]:
            continue
        for number, line in enumerate(data.decode("utf-8", "ignore").splitlines(), start=1):
            name = _first_hit(line, patterns)
            if name:
                found.append(
                    Violation(
                        "secret-pattern",
                        path,
                        f"第 {number} 行像是{name}。删掉它，并到服务商那里作废这把密钥",
                    )
                )
    for commit, path, line in repo.added_lines():
        name = _first_hit(line, patterns)
        if name:
            found.append(
                Violation(
                    "secret-history",
                    path,
                    f"提交 {commit[:8]} 加入过像是{name}的内容。"
                    "推送过的密钥必须立刻作废换新，只删文件不够",
                )
            )
    return found
