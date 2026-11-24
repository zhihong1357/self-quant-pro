"""File-level rules: size limit, data formats, file names that usually hold secrets."""

from __future__ import annotations

from pathlib import PurePosixPath

from sqp.checks import Violation
from sqp.config import Governance, matches
from sqp.repo import Repo


def check(repo: Repo, cfg: Governance, paths: list[str]) -> list[Violation]:
    found = []
    for path in paths:
        size = (repo.root / path).stat().st_size
        if size > cfg.max_file_bytes:
            found.append(
                Violation(
                    "file-size",
                    path,
                    f"{size:,} 字节，超过上限 {cfg.max_file_bytes:,}。大文件和数据放在仓库外",
                )
            )
        suffix = PurePosixPath(path).suffix.lower()
        in_fixtures = any(path.startswith(d) for d in cfg.fixture_dirs)
        if suffix in cfg.data_extensions and not in_fixtures:
            found.append(
                Violation(
                    "data-file",
                    path,
                    f"数据格式文件（{suffix}）不进仓库；测试用的合成小样本放 tests/fixtures/",
                )
            )
        if matches(path, cfg.secret_file_globs) and not matches(path, cfg.secret_file_allow):
            found.append(Violation("secret-file", path, "这类文件通常装着密钥或凭证，不允许提交"))
    return found
