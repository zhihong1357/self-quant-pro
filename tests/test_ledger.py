from __future__ import annotations

import json

import pytest

from sqp import ledger


def test_append_builds_a_chain(tmp_path):
    path = tmp_path / "l.jsonl"
    first = ledger.append(path, {"kind": "note", "text": "第一条"})
    second = ledger.append(path, {"kind": "note", "text": "第二条"})
    assert first["seq"] == 1 and first["prev"] == ledger.GENESIS
    assert second["seq"] == 2 and second["prev"] == first["hash"]
    assert ledger.verify(path.read_text("utf-8")) == []


def test_editing_an_old_line_breaks_the_chain(tmp_path):
    path = tmp_path / "l.jsonl"
    ledger.append(path, {"kind": "note", "value": 1})
    ledger.append(path, {"kind": "note", "value": 2})
    lines = path.read_text("utf-8").splitlines()
    record = json.loads(lines[0])
    record["value"] = 999
    lines[0] = ledger.canonical(record).decode()
    assert ledger.verify("\n".join(lines) + "\n")


def test_reserved_fields_cannot_be_written_by_hand(tmp_path):
    with pytest.raises(ValueError):
        ledger.append(tmp_path / "l.jsonl", {"seq": 5})


def test_appending_is_allowed(sandbox):
    path = sandbox.root / "registry/ledger/trials.jsonl"
    ledger.append(path, {"kind": "trial"})
    base = sandbox.commit()
    ledger.append(path, {"kind": "trial"})
    assert sandbox.rules(base) == set()


def test_a_broken_chain_is_reported(sandbox):
    sandbox.write("registry/ledger/trials.jsonl", '{"seq":1}\n')
    assert "ledger-chain" in sandbox.rules()


def test_deleting_a_ledger_is_blocked(sandbox):
    ledger.append(sandbox.root / "registry/ledger/trials.jsonl", {"kind": "trial"})
    base = sandbox.commit()
    sandbox.remove("registry/ledger/trials.jsonl")
    assert "no-delete" in sandbox.rules(base)
