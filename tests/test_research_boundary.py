"""Study code reaches data only through sqp.data.pit (decision 0022, roadmap G2)."""

from __future__ import annotations

import pytest

STUDY = "src/sqp/research/studies/h0001.py"

GOOD = """from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

import polars as pl

from sqp import stats
from sqp.data.pit import AccessRefused, PointInTime
from sqp.stats import expected_max_sharpe

from . import common
from .common import helper


def run(pit: PointInTime) -> dict:
    df = pit.table("um-klines-1h", end=datetime(2024, 1, 1, tzinfo=UTC))
    return {"n": df.height, "x": math.sqrt(2), "e": expected_max_sharpe(9, 1.75)}
"""


def violations(sandbox, source: str, path: str = STUDY) -> list[str]:
    sandbox.write(path, source)
    return [v.message for v in sandbox.check() if v.rule == "research-boundary"]


def test_an_ordinary_study_passes(sandbox):
    assert violations(sandbox, GOOD) == []


@pytest.mark.parametrize(
    "line",
    [
        "from sqp.storage import GcsStore",
        "from sqp import storage",
        "import sqp.storage",
        "import sqp.data.pit",
        "from sqp.data import build",
        "from sqp.data import pit",
        "from sqp.data.pit import Store",
        "from sqp.data.pit import os",
        "from google.cloud import storage",
        "import google.cloud.storage",
        "import os",
        "import pathlib",
        "import subprocess",
        "import importlib",
        "import urllib.request",
        "import pyarrow.parquet",
        "from .. import run",
        "from ..run import Runner",
        "from polars import read_parquet",
        "from sqp.stats import *",
        "from typing import get_type_hints",
        "from typing import ForwardRef",
    ],
)
def test_reaching_around_the_interface_is_refused(sandbox, line):
    assert violations(sandbox, line + "\n")


@pytest.mark.parametrize(
    "code",
    [
        "pit._store.get('x')",
        "pit.__dict__",
        "type(pit).__init__",
        "pl.read_parquet('cache/part.parquet')",
        "pl.scan_parquet('x')",
        "df.write_csv('out.csv')",
        "open('/tmp/x').read()",
        "getattr(pit, 'store')",
        "eval('1')",
        "__import__('os')",
        "pl.DataFrame.deserialize('x.json')",
        "pl.sql('SELECT * FROM read_parquet(\\'x\\')')",
        "pl.SQLContext().execute('x')",
        "pl.Config.load_from_file('x')",
        "(x for x in ()).gi_frame.f_globals['open']",
        "e.tb_frame.f_back",
        "get_type_hints(f)",
        "typing.get_type_hints(f)",
    ],
)
def test_private_state_files_and_dynamic_access_are_refused(sandbox, code):
    assert violations(sandbox, f"def f(pit, pl, df):\n    return {code}\n")


def test_only_python_and_notes_in_the_studies_directory(sandbox):
    sandbox.write("src/sqp/research/studies/README.md", "# 说明\n")
    assert violations(sandbox, GOOD) == []
    assert violations(sandbox, "x = 1\n", "src/sqp/research/studies/run.sh") != []


def test_unparsable_code_is_reported(sandbox):
    assert violations(sandbox, "def broken(:\n")


def test_code_outside_the_studies_directory_is_not_restricted(sandbox):
    assert (
        violations(sandbox, "from sqp.storage import GcsStore\n", "src/sqp/research/run.py") == []
    )
