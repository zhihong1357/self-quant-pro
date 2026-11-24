"""Code rules: no trading code before the allowed stage, no fund-moving code ever."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from sqp.checks import Violation
from sqp.config import Governance
from sqp.repo import Repo


def check(repo: Repo, cfg: Governance, paths: list[str]) -> list[Violation]:
    stage = (repo.text(cfg.stage_file) or "").strip()
    trading_allowed = cfg.stage_index(stage) >= cfg.stage_index(cfg.order_code_from) >= 0
    order = [(p.name, re.compile(p.regex)) for p in cfg.order_patterns]
    never = [(p.name, re.compile(p.regex)) for p in cfg.never_patterns]
    found = []
    for path in paths:
        if PurePosixPath(path).suffix.lower() not in cfg.code_extensions:
            continue
        data = repo.read(path)
        if data is None:
            continue
        for number, line in enumerate(data.decode("utf-8", "ignore").splitlines(), start=1):
            for name, pattern in never:
                if pattern.search(line):
                    found.append(
                        Violation(
                            "never-code",
                            path,
                            f"第 {number} 行：{name}。这类代码在任何阶段都不允许",
                        )
                    )
            if trading_allowed:
                continue
            for name, pattern in order:
                if pattern.search(line):
                    found.append(
                        Violation(
                            "order-code",
                            path,
                            f"第 {number} 行：{name}。当前阶段 {stage or '未知'}，"
                            f"{cfg.order_code_from} 之前不允许",
                        )
                    )
    return found
