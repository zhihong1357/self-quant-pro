from __future__ import annotations


def test_contiguous_decisions_pass(sandbox):
    sandbox.decision(1, "一。")
    sandbox.decision(2, "二。")
    assert sandbox.rules() == set()


def test_numbering_gap(sandbox):
    sandbox.decision(1, "一。")
    sandbox.decision(3, "三。")
    assert "decision-number" in sandbox.rules()


def test_missing_section(sandbox):
    sandbox.decision(1, "一。")
    path = sandbox.root / "docs/decisions/0001-test.md"
    path.write_text(path.read_text("utf-8").replace("## 后果", "## 其他"), "utf-8")
    assert "decision-format" in sandbox.rules()


def test_unknown_status(sandbox):
    sandbox.decision(1, "一。")
    path = sandbox.root / "docs/decisions/0001-test.md"
    path.write_text(path.read_text("utf-8").replace("已接受", "随便"), "utf-8")
    assert "decision-format" in sandbox.rules()


def test_readme_is_not_a_decision(sandbox):
    sandbox.write("docs/decisions/README.md", "# 决策索引\n")
    assert sandbox.rules() == set()
