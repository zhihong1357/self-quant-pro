"""Frozen files, stage transitions and accepted decisions, compared with a base commit."""

from __future__ import annotations

from conftest import UNIVERSE


def test_frozen_file_needs_a_decision_naming_it(sandbox):
    sandbox.write("registry/universes/U9.toml", UNIVERSE)
    base = sandbox.commit()
    sandbox.write("registry/universes/U9.toml", UNIVERSE.replace("测试宇宙", "改过的名字"))
    assert "frozen-file" in sandbox.rules(base)
    sandbox.decision(1, "修正 registry/universes/U9.toml 的名称。")
    assert sandbox.rules(base) == set()


def test_mentioning_a_sibling_path_does_not_unlock(sandbox):
    sandbox.write("registry/universes/U9.toml", UNIVERSE)
    base = sandbox.commit()
    sandbox.write("registry/universes/U9.toml", UNIVERSE.replace("测试宇宙", "改过的名字"))
    sandbox.decision(1, "说明见 registry/README.md，和 registry/universes/U9.toml.bak 无关。")
    assert "frozen-file" in sandbox.rules(base)


def test_a_folder_ending_in_slash_unlocks_its_files(sandbox):
    sandbox.write("registry/universes/U9.toml", UNIVERSE)
    base = sandbox.commit()
    sandbox.write("registry/universes/U9.toml", UNIVERSE.replace("测试宇宙", "改过的名字"))
    sandbox.decision(1, "整体修订 `registry/universes/` 下的名称。")
    assert sandbox.rules(base) == set()


def test_stage_decision_must_name_stage_on_one_line(sandbox):
    base = sandbox.commit()
    sandbox.write("STAGE", "G1\n")
    sandbox.decision(1, "这份决策提到了 STAGE。\n\n另一段话里提到 G1。")
    assert "stage-gate" in sandbox.rules(base)


def test_governance_config_is_frozen(sandbox):
    base = sandbox.commit()
    path = sandbox.root / "config/governance.toml"
    path.write_text(path.read_text("utf-8").replace("1_000_000", "9_000_000"), "utf-8")
    assert "frozen-file" in sandbox.rules(base)


def test_stage_moves_one_step_with_a_decision(sandbox):
    base = sandbox.commit()
    sandbox.write("STAGE", "G1\n")
    assert "stage-gate" in sandbox.rules(base)
    sandbox.decision(1, "STAGE 从 G0 进入 G1。")
    assert sandbox.rules(base) == set()


def test_stage_cannot_skip(sandbox):
    base = sandbox.commit()
    sandbox.write("STAGE", "G7\n")
    sandbox.decision(1, "STAGE 直接进入 G7。")
    assert "stage-gate" in sandbox.rules(base)


def test_first_stage_must_be_g0(sandbox):
    sandbox.remove("STAGE")
    base = sandbox.commit()
    sandbox.write("STAGE", "G7\n")
    assert "stage-gate" in sandbox.rules(base)


def test_invalid_stage_value(sandbox):
    sandbox.write("STAGE", "G99\n")
    assert "stage-value" in sandbox.rules()


def test_accepted_decision_body_is_frozen_but_status_may_change(sandbox):
    sandbox.decision(1, "原来的决定。")
    base = sandbox.commit()
    path = sandbox.root / "docs/decisions/0001-test.md"
    original = path.read_text("utf-8")
    path.write_text(original.replace("原来的决定。", "悄悄改过的决定。"), "utf-8")
    assert "decision-frozen" in sandbox.rules(base)
    path.write_text(original.replace("- 状态：已接受", "- 状态：已被取代（见 0002）"), "utf-8")
    assert sandbox.rules(base) == set()


def test_decisions_cannot_be_deleted(sandbox):
    sandbox.decision(1, "原来的决定。")
    base = sandbox.commit()
    sandbox.remove("docs/decisions/0001-test.md")
    assert "no-delete" in sandbox.rules(base)
