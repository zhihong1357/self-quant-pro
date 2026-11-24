"""A throwaway git repository with this project's governance config, for planting violations."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from sqp.checks import Violation, run_all
from sqp.repo import Repo

ROOT = Path(__file__).resolve().parents[1]

DECISION = """# {number:04d} 测试决策

- 状态：已接受
- 日期：2026-11-24

## 背景

测试用。

## 决定

{body}

## 后果

无。
"""

UNIVERSE = """id = "U9"
name = "测试宇宙"
domain = "crypto"
scope = "测试"
created = 2026-01-01

[segments.exploration]
start = 2020-01-01
end = 2020-12-31

[segments.confirmation]
start = 2021-01-01
end = 2021-06-30

[segments.vault]
start = 2021-07-01
end = 2021-12-31

[segments.forward]
start = 2022-01-01
"""


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


class Sandbox:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, relative: str, text: str) -> Path:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, "utf-8")
        return target

    def remove(self, relative: str) -> None:
        (self.root / relative).unlink()

    def decision(self, number: int, body: str) -> None:
        self.write(
            f"docs/decisions/{number:04d}-test.md", DECISION.format(number=number, body=body)
        )

    def commit(self, message: str = "commit") -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def check(self, base: str | None = None) -> list[Violation]:
        return run_all(Repo.open(self.root, base))

    def rules(self, base: str | None = None) -> set[str]:
        return {v.rule for v in self.check(base)}


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "test")
    git(root, "config", "commit.gpgsign", "false")
    shutil.copytree(ROOT / "config", root / "config")
    (root / "STAGE").write_text("G0\n", "utf-8")
    return Sandbox(root)
