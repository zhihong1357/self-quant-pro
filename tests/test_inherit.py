from __future__ import annotations

import json

import pytest

from conftest import git
from sqp import inherit

CONFIG = {
    "source": [
        {
            "name": "myquant",
            "url": "https://example.invalid/myquant",
            "generation": 2,
            "role": "测试",
            "format": "myquant",
            "pin": ["research/*/trials.jsonl"],
            "vendor": ["research/*/trials.jsonl"],
            "trials": ["research/*/trials.jsonl"],
        }
    ],
    "studies": {"myquant": {"s1": {"domain": "crypto", "family": "crypto.t"}}},
}

TRIAL = {
    "trial_id": "T0001",
    "hypothesis_id": "H0001",
    "status": "completed",
    "recorded_at": "2026-11-18T00:00:00+00:00",
    "result": {"metrics": {"x": 1}, "panel": {"first": "2023-01-02", "last": "2026-12-31"}},
}


def make_source(tmp_path, study="s1"):
    src = tmp_path / "myquant"
    (src / "research" / study).mkdir(parents=True)
    (src / "research" / study / "trials.jsonl").write_text(json.dumps(TRIAL) + "\n", "utf-8")
    git(src, "init", "-q", "-b", "main")
    git(src, "config", "user.email", "test@example.com")
    git(src, "config", "user.name", "test")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "init")
    return src


def test_scan_is_deterministic_and_clamps_dates(tmp_path):
    src = make_source(tmp_path)
    first, second = tmp_path / "a" / "inherited", tmp_path / "b" / "inherited"
    inherit.scan(CONFIG, {"myquant": src}, first)
    inherit.scan(CONFIG, {"myquant": src}, second)
    for name in ("manifest.json", "trials.json", "myquant/research/s1/trials.jsonl"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    trials = json.loads((first / "trials.json").read_text("utf-8"))
    assert trials["summary"]["counted_by_domain"] == {"crypto": 1}
    assert trials["records"][0]["data"] == {"first": "2023-01-02", "last": "2026-11-18"}


def test_unclassified_study_is_refused(tmp_path):
    src = make_source(tmp_path, study="unknown")
    with pytest.raises(RuntimeError):
        inherit.scan(CONFIG, {"myquant": src}, tmp_path / "out" / "inherited")


def test_dirty_source_is_refused(tmp_path):
    src = make_source(tmp_path)
    (src / "research" / "s1" / "trials.jsonl").write_text("{}\n", "utf-8")
    with pytest.raises(RuntimeError):
        inherit.scan(CONFIG, {"myquant": src}, tmp_path / "out" / "inherited")
