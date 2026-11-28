"""Round-four red-team regression tests (N5), originally authored by Model B at 3a62bbb.

All 7 Polars credential-provider and catalog client names are now blocked in
`research-boundary`'s `OTHER_NAMES`, while every real study module in
`src/sqp/research/studies/` (including `btc_eth_001.py`) passes with zero violations.
"""

from __future__ import annotations

import pathlib

import pytest

from sqp.checks import research as boundary

STUDY = "src/sqp/research/studies/btc_eth_001.py"
STUDIES_DIR = pathlib.Path(__file__).resolve().parents[1] / "src/sqp/research/studies"

N5_BLOCKED_NAMES = (
    "Catalog",
    "CredentialProvider",
    "CredentialProviderGCP",
    "CredentialProviderAWS",
    "CredentialProviderAzure",
    "CredentialProviderFunction",
    "DeltaLake",
)


def _study_src(name: str) -> str:
    return f"import polars as pl\n\n\ndef build(pit):\n    return pl.{name}\n"


@pytest.mark.parametrize("name", N5_BLOCKED_NAMES)
def test_n5_polars_credential_and_catalog_names_are_refused(name: str) -> None:
    viols = boundary._scan(STUDY, _study_src(name))
    assert viols, f"pl.{name} was not refused by research-boundary"
    from_viols = boundary._scan(STUDY, f"from polars import {name}\n")
    assert from_viols, f"from polars import {name} was not refused by research-boundary"


def test_n5_fix_blocks_names_without_breaking_real_studies() -> None:
    assert boundary._scan(STUDY, _study_src("CredentialProviderGCP")) != []
    assert boundary._scan(STUDY, _study_src("Catalog")) != []

    study_files = sorted(STUDIES_DIR.glob("*.py"))
    assert any(f.name == "btc_eth_001.py" for f in study_files)
    total = 0
    for f in study_files:
        total += len(boundary._scan(f"src/sqp/research/studies/{f.name}", f.read_text("utf-8")))
    assert total == 0
