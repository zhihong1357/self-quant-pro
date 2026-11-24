"""Decision records: file name, contiguous numbering, required metadata and sections."""

from __future__ import annotations

import re
from datetime import date

from sqp.checks import Violation
from sqp.config import Governance
from sqp.repo import Repo

FILENAME = re.compile(r"^(\d{4})-.+\.md$")
STATUSES = ("已接受", "已被取代", "已撤回")
HEADINGS = ("## 背景", "## 决定", "## 后果")


def meta(lines: list[str], key: str) -> str | None:
    prefix = f"- {key}："
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


def is_decision(path: str, cfg: Governance) -> bool:
    folder, _, name = path.rpartition("/")
    return folder == cfg.decisions_dir and name.endswith(".md") and name != "README.md"


def _valid_date(text: str | None) -> bool:
    try:
        date.fromisoformat(text or "")
    except ValueError:
        return False
    return True


def check(repo: Repo, cfg: Governance, paths: list[str]) -> list[Violation]:
    found = []
    numbers = []
    for path in paths:
        if not is_decision(path, cfg):
            continue
        match = FILENAME.match(path.rpartition("/")[2])
        if not match:
            found.append(Violation("decision-format", path, "文件名必须是 NNNN-标题.md"))
            continue
        numbers.append(int(match.group(1)))
        lines = (repo.text(path) or "").splitlines()
        if not lines or not lines[0].startswith(f"# {match.group(1)} "):
            found.append(
                Violation("decision-format", path, f"第一行必须是“# {match.group(1)} 标题”")
            )
        status = meta(lines, "状态") or ""
        if not status.startswith(STATUSES):
            found.append(
                Violation(
                    "decision-format", path, f"“- 状态：”必须以 {'/'.join(STATUSES)} 之一开头"
                )
            )
        if not _valid_date(meta(lines, "日期")):
            found.append(Violation("decision-format", path, "“- 日期：”必须是 YYYY-MM-DD"))
        for heading in HEADINGS:
            if heading not in lines:
                found.append(Violation("decision-format", path, f"缺少小节“{heading}”"))
    if sorted(numbers) != list(range(1, len(numbers) + 1)):
        found.append(
            Violation("decision-number", cfg.decisions_dir, "决策编号必须从 0001 起连续、不重复")
        )
    return found
