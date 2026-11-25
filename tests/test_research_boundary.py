"""Study code reaches data only through sqp.data.pit (decision 0022, roadmap G2)."""

from __future__ import annotations

import pytest

STUDY = "src/sqp/research/studies/h0001.py"

GOOD = """from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

import polars as pl
import polars.selectors as cs
from typing import Any, cast

from sqp.data.pit import AccessRefused, PointInTime
from sqp.stats import expected_max_sharpe



def run(pit: PointInTime) -> dict:
    df = pit.table("um-klines-1h", end=datetime(2024, 1, 1, tzinfo=UTC))
    picked = df.select(cs.numeric()).with_columns(pl.col("close").rolling_mean(3))
    return cast(Any, {"n": picked.height, "x": math.sqrt(2), "e": expected_max_sharpe(9, 1.75)})
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


# --- found by the independent review of PR #16 -------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "from typing import operator\ndef f(pit):\n    return operator.attrgetter('x')(pit)\n",
        "from dataclasses import builtins\n",
        "from dataclasses import inspect\n",
        "from dataclasses import sys\n",
        "import dataclasses\ndef f(pit):\n    return dataclasses.inspect.getmembers(pit)\n",
        "import typing\nx = typing.types\n",
        "import enum\nx = enum.bltns\n",
        "import polars as pl\npl.os.system('true')\n",
        "import polars as pl\nx = pl.config\n",
        "import polars as pl\nx = pl.contextlib\n",
        "import polars.io\n",
        "from polars import dependencies\n",
        "from math import nothere\n",
        "from sqp.stats import NormalDist\n",
        "from sqp.stats import math\n",
        "from sqp import stats\n",
        "def f(x, pit):\n    return x.getattr(pit, 'p')\n",
        "def f(x):\n    return x.open('/etc/passwd')\n",
    ],
)
def test_reexported_modules_and_attribute_forms_are_refused(sandbox, source):
    assert violations(sandbox, source)


@pytest.mark.parametrize(
    ("path", "source", "refused"),
    [
        ("src/sqp/research/studies/h0001__init__.py", "from .. import run\n", True),
        ("src/sqp/research/studies/h0001.py", "from ...... import x\n", True),
        ("src/sqp/research/studies/__init__.py", "from .. import run\n", True),
        ("src/sqp/research/studies/__init__.py", "from . import helpers\n", True),
        ("src/sqp/research/studies/sub/mod.py", "from .. import helpers\n", True),
        ("src/sqp/research/studies/sub/mod.py", "from ... import run\n", True),
        ("src/sqp/research/studies/sub/mod.py", "from ...studies import helpers\n", True),
        ("src/sqp/research/studies/b.py", "from .a import helper\n", True),
        ("src/sqp/research/studies/b.py", "from .a import typing\n", True),
    ],
)
def test_relative_imports_resolve_like_python(sandbox, path, source, refused):
    assert bool(violations(sandbox, source, path)) is refused


# --- found by the second review of PR #16 ------------------------------------


def test_ordinary_local_names_and_polars_list_eval_pass(sandbox):
    source = """import polars as pl


def _helper(df):
    load_window = 24
    for _ in range(2):
        a, _ = (1, 2)
        df = df.with_columns(pl.col("xs").list.eval(pl.element() * 2))
        df = df.with_columns(pl.col("ys").arr.eval(pl.element() + a + load_window))
    return df


def run(pit, params):
    return _helper(pl.DataFrame({"xs": [[1, 2]]}))
"""
    assert violations(sandbox, source) == []


@pytest.mark.parametrize(
    "source",
    [
        "import typing\ndef run(pit):\n    t = typing\n"
        "    return t.warnings.formatwarning('', UserWarning, '/etc/hostname', 1)\n",
        "import typing\nx = typing.warnings\n",
        "import polars as pl\ndef f(m):\n    return m\nf(pl)\n",
        "import polars as pl\nmods = [pl]\n",
        "import typing as m\nimport math as m\n",
        "from math import sqrt\nfrom math import sqrt\n",
        "import polars as pl\npl = 1\n",
        "x.eval('1')\n",
        "from linecache import getline\n",
    ],
)
def test_modules_as_values_and_rebinding_are_refused(sandbox, source):
    assert violations(sandbox, source)


def test_the_named_pit_names_and_stats_functions_are_importable(sandbox):
    source = "from sqp.data.pit import AccessRefused, IntegrityError, PointInTime, Segment\n"
    source += "from sqp.stats import expected_max_sharpe, min_detectable_sharpe\n"
    assert violations(sandbox, source) == []


def test_the_study_conventions_are_importable(sandbox):
    source = "from sqp.research.studies import SECTIONS, Outcome\nfrom . import Outcome as O\n"
    assert violations(sandbox, source) == []
    assert violations(sandbox, "from sqp.research.studies import dataclass\n")
