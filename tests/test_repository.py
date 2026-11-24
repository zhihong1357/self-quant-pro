"""This repository must pass its own rules."""

from __future__ import annotations

from conftest import ROOT
from sqp.checks import run_all
from sqp.cli import main
from sqp.repo import Repo


def test_this_repository_passes_its_own_rules():
    violations = run_all(Repo.open(ROOT))
    assert violations == [], "\n".join(map(str, violations))


def test_cli_check(capsys):
    assert main(["check", "--repo", str(ROOT)]) == 0
    assert "全部通过" in capsys.readouterr().out


def test_cli_power(capsys):
    assert main(["power", "--days", "639", "--trials", "100"]) == 0
    assert "2.90" in capsys.readouterr().out
