"""Trading code is gated by STAGE; fund-moving code is never allowed."""

from __future__ import annotations

ORDER_CALL = "exchange." + "create" + "_order('BTC/USDT', 'market', 'buy', 1)"
CAMEL_CALL = "await ex." + "create" + "Order(symbol, 'limit', 'sell', 1, 100)"
ENDPOINT = 'requests.post(BASE + "/fapi/v1/' + 'order", data=body)'
MOVE_FUNDS = "client." + "with" + "draw(coin='USDT', amount=1)"


def test_order_code_allowed_from_g7(sandbox):
    sandbox.write("STAGE", "G7\n")
    sandbox.write("src/bot.py", ORDER_CALL + "\n")
    assert "order-code" not in sandbox.rules()


def test_camel_case_and_raw_endpoints_are_caught(sandbox):
    sandbox.write("app.ts", CAMEL_CALL + "\n")
    sandbox.write("b.py", ENDPOINT + "\n")
    hits = {v.path for v in sandbox.check() if v.rule == "order-code"}
    assert hits == {"app.ts", "b.py"}


def test_moving_funds_is_never_allowed(sandbox):
    sandbox.write("STAGE", "G8\n")
    sandbox.write("src/bot.py", MOVE_FUNDS + "\n")
    assert "never-code" in sandbox.rules()


def test_documents_may_discuss_it(sandbox):
    sandbox.write("notes.md", ORDER_CALL + "\n" + MOVE_FUNDS + "\n")
    assert sandbox.rules() == set()


def test_ordinary_research_words_pass(sandbox):
    sandbox.write("src/r.py", "sort_order = 1\npost_order_traversal(tree)\norder_book = {}\n")
    assert sandbox.rules() == set()
