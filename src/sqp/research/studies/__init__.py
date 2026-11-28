"""Research code. Each module here is one study with a single entry point:

    def run(pit: PointInTime | None, params: dict) -> Outcome

`pit` is None for studies on synthetic data. A study returns its metrics, the text
of the nine sections of the standard report (protocol section 9) and, optionally, a
verdict from VERDICTS for the public log; the runner
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


# The only words a study may send to the public log (decision 0026). Logs are public,
# so a study's numbers never go there; a verdict from this short list carries a bit
# or two and lets the owner and agents see whether a control passed without
# downloading anything.
VERDICTS = ("通过", "不通过", "一致", "不一致")


@dataclass(frozen=True)
class Outcome:
    metrics: dict  # name -> number, string, bool or None; stays in the private bucket
    sections: dict  # every name in SECTIONS -> text; "不适用：…" is a valid text
    verdict: str | None = None  # one of VERDICTS, printed in the public log


@dataclass(frozen=True, repr=False)
class BlindMetrics:
    """In-memory raw estimates from a blind replication study (decision 0031).

    The runner hands this directly to the framework-side blind comparator and
    discards it; its string forms are redacted so logs or tracebacks never print
    raw numbers from guarded segments.
    """

    values: dict
    quality: dict

    def __repr__(self) -> str:
        return "<BlindMetrics redacted>"

    def __str__(self) -> str:
        return "<BlindMetrics redacted>"
