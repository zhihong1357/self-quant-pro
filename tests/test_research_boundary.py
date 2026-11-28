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

from sqp.data.pit import AccessRefused, PointInTime
from sqp.stats import expected_max_sharpe


def run(pit: PointInTime) -> dict:
    df = pit.table("um-klines-1h", end=datetime(2024, 1, 1, tzinfo=UTC))
    picked = df.select(cs.numeric()).with_columns(pl.col("close").rolling_mean(3))
    return {"n": picked.height, "x": math.sqrt(2), "e": expected_max_sharpe(9, 1.75)}
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
        "pl.Config.set_verbose(True)",  # prints from Rust, past the console capture
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


def test_studies_may_use_the_engine_but_only_its_own_functions(sandbox):
    assert (
        violations(sandbox, "from sqp.research.engine import quantile_weights, summarize\n") == []
    )
    assert violations(sandbox, "from sqp.research.engine import pl\n")  # a re-export
    assert violations(sandbox, "from sqp.research.engine import sharpe_standard_error\n")
    assert violations(sandbox, "from sqp.research import engine\n")


# --- known static-boundary escape corpus (PR #29 red-team rounds 1 & 2) ------

KNOWN_ESCAPE_CORPUS = [
    ("str_format_attr", 'def run(pit, p):\n    return "{0._store}".format(pit)\n'),
    ("str_format_map", 'def run(pit, p):\n    return "{x}".format_map({"x": 1})\n'),
    (
        "match_class_pattern",
        "def run(pit, p):\n    match pit:\n        case object(_store=s):\n            return s\n",
    ),
    (
        "make_dataclass_namespace",
        "import dataclasses\n"
        'M = dataclasses.make_dataclass("M", [], namespace={"__gt__": lambda s, o: False})\n',
    ),
    ("type_dynamic_class", 'def run(pit, p):\n    return type("M", (Exception,), {})\n'),
    ("dunder_method_def", "class M:\n    def __gt__(self, other):\n        return False\n"),
    ("metaclass_abcmeta", 'from typing import ABCMeta\nM = ABCMeta("M", (Exception,), {})\n'),
    ("internal_function_type", "from dataclasses import FunctionType\n"),
    (
        "enum_pickle_by_enum_name",
        "import enum\ndef run(pit, p):\n    g, _ = enum.pickle_by_enum_name(p, 0)\n    return g\n",
    ),
    (
        "functools_update_wrapper",
        "import functools\n"
        'def run(pit, p):\n    functools.update_wrapper(p, pit, assigned=("_store",))\n',
    ),
    (
        "functools_wraps",
        "import functools\n"
        'def run(pit, p):\n    return functools.wraps(pit, assigned=("_store",))(p)\n',
    ),
    (
        "enum_global_enum",
        "import enum\n"
        'def run(pit, p):\n    e = enum.Enum("K", [("X", 1)], module="builtins")\n'
        "    enum.global_enum(e)\n",
    ),
    (
        "monkeypatch_framework_attr",
        "from sqp.data.pit import PointInTime\n"
        "def run(pit, p):\n    PointInTime.touched = lambda s, a, b: []\n",
    ),
    (
        "monkeypatch_polars_attr",
        "import polars as pl\ndef run(pit, p):\n    pl.DataFrame.filter = lambda s, *a, **k: s\n",
    ),
    (
        "polars_show_graph",
        'import polars as pl\ndef run(pit, p):\n    pl.LazyFrame().show_graph(output_path="x")\n',
    ),
    (
        "polars_credential_provider_gcp",
        "import polars as pl\ndef run(pit, p):\n    return pl.CredentialProviderGCP()()\n",
    ),
    (
        "polars_catalog",
        'import polars as pl\ndef run(pit, p):\n    return pl.Catalog("https://example.test")\n',
    ),
]


@pytest.mark.parametrize(
    ("case", "source"), KNOWN_ESCAPE_CORPUS, ids=[c for c, _ in KNOWN_ESCAPE_CORPUS]
)
def test_known_escape_corpus_is_refused(sandbox, case, source):
    assert violations(sandbox, source), f"escape pattern {case!r} was not refused"


@pytest.mark.parametrize(
    "source",
    [
        "import enum\n",
        "import functools\n",
        "import typing\n",
        "import collections\n",
        "import collections.abc\n",
        "from collections import defaultdict\n",
        "def f(x):\n    x.a = 1\n",
        "def f(x):\n    x.a += 1\n",
        "def f(x):\n    del x.a\n",
    ],
)
def test_removed_modules_and_attribute_mutations_are_refused(sandbox, source):
    assert violations(sandbox, source)
