"""Read-only access to a git checkout: current files, the comparison base, added lines."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """A git command failed."""


def git(root: Path, *args: str, check: bool = True) -> bytes:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if check and proc.returncode != 0:
        raise GitError(proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout


def _split_nul(raw: bytes) -> list[str]:
    return [p for p in raw.decode("utf-8").split("\0") if p]


@dataclass
class Repo:
    root: Path
    base: str | None = None

    @classmethod
    def open(cls, path: str | Path, base_ref: str | None = None) -> Repo:
        root = Path(git(Path(path), "rev-parse", "--show-toplevel").decode().strip())
        base = None
        if base_ref:
            raw = git(
                root, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}", check=False
            )
            commit = raw.decode().strip()
            if not commit:
                raise GitError(f"找不到基准提交：{base_ref}")
            merge_base = git(root, "merge-base", commit, "HEAD", check=False).decode().strip()
            base = merge_base or commit
        return cls(root, base)

    def files(self) -> list[str]:
        """Tracked files plus untracked files that are not ignored."""
        raw = git(self.root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        return sorted(p for p in set(_split_nul(raw)) if (self.root / p).is_file())

    def read(self, path: str) -> bytes | None:
        target = self.root / path
        return target.read_bytes() if target.is_file() else None

    def text(self, path: str) -> str | None:
        data = self.read(path)
        return None if data is None else data.decode("utf-8", "replace")

    def base_files(self) -> list[str]:
        if self.base is None:
            return []
        return sorted(_split_nul(git(self.root, "ls-tree", "-r", "-z", "--name-only", self.base)))

    def base_read(self, path: str) -> bytes | None:
        if self.base is None:
            return None
        proc = subprocess.run(
            ["git", "-C", str(self.root), "show", f"{self.base}:{path}"], capture_output=True
        )
        return proc.stdout if proc.returncode == 0 else None

    def added_lines(self) -> list[tuple[str, str, str]]:
        """(commit, path, text) for every line added by the commits in base..HEAD."""
        if self.base is None:
            return []
        raw = git(
            self.root,
            "-c",
            "core.quotepath=false",
            "log",
            "-p",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--unified=0",
            "--format=commit %H",
            f"{self.base}..HEAD",
        ).decode("utf-8", "replace")
        added: list[tuple[str, str, str]] = []
        commit = path = ""
        in_header = False
        for line in raw.splitlines():
            if line.startswith("commit "):
                commit, in_header = line[len("commit ") :], False
            elif line.startswith("diff --git "):
                path, in_header = "", True
            elif in_header and line.startswith("+++ "):
                path = line[len("+++ b/") :] if line.startswith("+++ b/") else ""
            elif line.startswith("@@"):
                in_header = False
            elif not in_header and path and line.startswith("+"):
                added.append((commit, path, line[1:]))
        return added
