"""Research code. Each module here is one study with a single entry point:

    def run(pit: PointInTime | None, params: dict) -> Outcome

`pit` is None for studies on synthetic data. A study returns its metrics and the
text of the nine sections of the standard report (protocol section 9); the runner
adds the facts it knows itself (code commit, data fingerprint, trial count) and
stores everything in the private bucket. Metrics must be deterministic for the
same code and inputs: no timestamps, and round floats.

Everything in this directory is checked by `research-boundary`.
"""

from __future__ import annotations

from dataclasses import dataclass

SECTIONS = (
    "结论",
    "假说与机制",
    "数据",
    "方法",
    "结果",
    "边际贡献",
    "成本与容量",
    "稳健性",
    "适用范围与停止条件",
)


@dataclass(frozen=True)
class Outcome:
    metrics: dict  # name -> number, string, bool or None; stays in the private bucket
    sections: dict  # every name in SECTIONS -> text; "不适用：…" is a valid text
