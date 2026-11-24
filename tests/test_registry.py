"""Universe and hypothesis cards: schema, stricter-only thresholds, freeze after registration."""

from __future__ import annotations

from conftest import UNIVERSE

DRAFT = """id = "H-0001"
title = "测试假说"
family = "crypto.test"
universe = "U9"
status = "draft"
created = 2026-11-24
mechanism = "谁在付钱，为什么会一直付"
predictions = ["主要预测"]
falsification = "什么结果算否定"
"""

REGISTERED = (
    DRAFT.replace('status = "draft"', 'status = "registered"')
    + """
[registration]
registered_at = 2026-11-25
segments = ["confirmation"]
min_effect_sharpe = 1.5
test_plan = "冻结的检验方案"

[thresholds]
t_stat = 3.0
deflated_sharpe = 0.95
pbo = 0.10
cost_multiplier = 2.0
"""
)

CARD = "registry/hypotheses/H-0001.toml"


def setup(sandbox, card: str) -> None:
    sandbox.write("registry/universes/U9.toml", UNIVERSE)
    sandbox.write(CARD, card)


def test_valid_cards_pass(sandbox):
    setup(sandbox, REGISTERED)
    assert sandbox.rules() == set()


def test_segments_must_be_contiguous(sandbox):
    sandbox.write("registry/universes/U9.toml", UNIVERSE.replace("2021-01-01", "2021-01-05"))
    assert "universe-schema" in sandbox.rules()


def test_unknown_universe(sandbox):
    setup(sandbox, DRAFT.replace('universe = "U9"', 'universe = "U404"'))
    assert "hypothesis-schema" in sandbox.rules()


def test_id_must_match_file_name(sandbox):
    setup(sandbox, DRAFT.replace('id = "H-0001"', 'id = "H-0002"'))
    assert "hypothesis-schema" in sandbox.rules()


def test_thresholds_cannot_be_loosened(sandbox):
    setup(sandbox, REGISTERED.replace("t_stat = 3.0", "t_stat = 2.0"))
    assert "hypothesis-schema" in sandbox.rules()
    setup(sandbox, REGISTERED.replace("pbo = 0.10", "pbo = 0.30"))
    assert "hypothesis-schema" in sandbox.rules()


def test_mechanism_exception_needs_a_reason(sandbox):
    loosened = REGISTERED.replace("t_stat = 3.0", "t_stat = 2.5")
    setup(sandbox, loosened)
    assert "hypothesis-schema" in sandbox.rules()
    reason = 'test_plan = "冻结的检验方案"\nmechanism_exception = "做市商对冲有明确的公开机制"'
    setup(sandbox, loosened.replace('test_plan = "冻结的检验方案"', reason))
    assert sandbox.rules() == set()


def test_registered_card_is_frozen_except_status(sandbox):
    setup(sandbox, REGISTERED)
    base = sandbox.commit()
    sandbox.write(CARD, REGISTERED.replace("谁在付钱", "换了一个机制"))
    assert "hypothesis-frozen" in sandbox.rules(base)
    sandbox.write(CARD, REGISTERED.replace('status = "registered"', 'status = "rejected"'))
    assert sandbox.rules(base) == set()
    sandbox.write(CARD, REGISTERED.replace('status = "registered"', 'status = "draft"'))
    assert "hypothesis-frozen" in sandbox.rules(base)


def test_drafts_may_change(sandbox):
    setup(sandbox, DRAFT)
    base = sandbox.commit()
    sandbox.write(CARD, DRAFT.replace("谁在付钱", "换了一个机制"))
    assert sandbox.rules(base) == set()


def test_derived_numbers_stay_out_of_public_ledgers(sandbox):
    from sqp import ledger

    path = sandbox.root / "registry/ledger/trials.jsonl"
    ledger.append(path, {"kind": "trial", "result_fingerprint": "sha256:" + "0" * 64})
    assert sandbox.rules() == set()
    ledger.append(path, {"kind": "trial", "metrics": {"sharpe": 1.2}})
    assert "derived-results" in sandbox.rules()


def test_hypotheses_cannot_be_deleted(sandbox):
    setup(sandbox, DRAFT)
    base = sandbox.commit()
    sandbox.remove(CARD)
    assert "no-delete" in sandbox.rules(base)
