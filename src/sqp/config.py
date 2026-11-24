"""Load the governance and protocol configuration of a checkout."""

from __future__ import annotations

import fnmatch
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass

GOVERNANCE_PATH = "config/governance.toml"
PROTOCOL_PATH = "config/protocol.toml"


def matches(path: str, globs: Iterable[str]) -> bool:
    """Glob match on posix paths; '*' also crosses directories."""
    return any(fnmatch.fnmatchcase(path, g) for g in globs)


@dataclass(frozen=True)
class Pattern:
    name: str
    regex: str


@dataclass(frozen=True)
class Governance:
    stage_file: str
    stages: tuple[str, ...]
    order_code_from: str
    code_extensions: tuple[str, ...]
    order_patterns: tuple[Pattern, ...]
    never_patterns: tuple[Pattern, ...]
    max_file_bytes: int
    data_extensions: tuple[str, ...]
    fixture_dirs: tuple[str, ...]
    secret_file_globs: tuple[str, ...]
    secret_file_allow: tuple[str, ...]
    secret_patterns: tuple[Pattern, ...]
    derived_jsonl_globs: tuple[str, ...]
    derived_forbidden_keys: tuple[str, ...]
    ledger_globs: tuple[str, ...]
    frozen_globs: tuple[str, ...]
    no_delete_globs: tuple[str, ...]
    decisions_dir: str
    hypotheses_glob: str
    universes_glob: str
    inherited_trials: str

    def stage_index(self, stage: str) -> int:
        return self.stages.index(stage) if stage in self.stages else -1


def _patterns(items: list[dict]) -> tuple[Pattern, ...]:
    return tuple(Pattern(item["name"], item["regex"]) for item in items)


def load_governance(text: str) -> Governance:
    data = tomllib.loads(text)
    stage, code, files = data["stage"], data["code"], data["files"]
    secrets, derived, history = data["secrets"], data["derived"], data["history"]
    return Governance(
        stage_file=stage["file"],
        stages=tuple(stage["order"]),
        order_code_from=stage["order_code_allowed_from"],
        code_extensions=tuple(code["extensions"]),
        order_patterns=_patterns(code["order_patterns"]),
        never_patterns=_patterns(code["never_patterns"]),
        max_file_bytes=int(files["max_bytes"]),
        data_extensions=tuple(files["data_extensions"]),
        fixture_dirs=tuple(files["fixture_dirs"]),
        secret_file_globs=tuple(files["secret_file_globs"]),
        secret_file_allow=tuple(files["secret_file_allow"]),
        secret_patterns=_patterns(secrets["patterns"]),
        derived_jsonl_globs=tuple(derived["jsonl_globs"]),
        derived_forbidden_keys=tuple(derived["forbidden_keys"]),
        ledger_globs=tuple(history["ledger_globs"]),
        frozen_globs=tuple(history["frozen_globs"]),
        no_delete_globs=tuple(history["no_delete_globs"]),
        decisions_dir=history["decisions_dir"],
        hypotheses_glob=history["hypotheses_glob"],
        universes_glob=history["universes_glob"],
        inherited_trials=history["inherited_trials"],
    )


def load_protocol(text: str) -> dict:
    return tomllib.loads(text)
