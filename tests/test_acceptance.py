"""G0 acceptance: the three planted violations named in docs/roadmap.md are each blocked.

Suspicious strings are assembled at run time so that this file itself stays clean.
"""

from __future__ import annotations

from sqp import ledger

FAKE_GOOGLE_KEY = "AIza" + "Sy" + "B" * 33
ORDER_CALL = "exchange." + "create" + "_order('BTC/USDT', 'market', 'buy', 1)"


def test_rewriting_a_ledger_is_blocked_even_with_a_valid_chain(sandbox):
    path = sandbox.root / "registry/ledger/trials.jsonl"
    ledger.append(path, {"kind": "trial", "sharpe": 0.4})
    base = sandbox.commit()
    path.write_text("", "utf-8")
    ledger.append(path, {"kind": "trial", "sharpe": 2.4})  # a "better" result, chain recomputed
    assert ledger.verify(path.read_text("utf-8")) == []
    assert "ledger-append-only" in sandbox.rules(base)


def test_a_fake_secret_is_blocked(sandbox):
    sandbox.write("notes.md", f"GEMINI_API_KEY={FAKE_GOOGLE_KEY}\n")
    assert "secret-pattern" in sandbox.rules()


def test_order_code_is_blocked_before_g7(sandbox):
    sandbox.write("src/bot.py", ORDER_CALL + "\n")
    assert "order-code" in sandbox.rules()
