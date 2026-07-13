"""Qt-free validation for human-authored card policy text."""
from __future__ import annotations

from isar.schema.card import validate_policy_text
from synthetic_ws import _POLICY_TOML


def test_policy_text_reports_unknown_top_level_keys_without_dropping_the_warning():
    result = validate_policy_text(_POLICY_TOML + '\n[fittng]\nmin_fit_points = 8\n')
    assert result.is_valid
    assert result.policy is not None
    assert result.warnings == ("unknown top-level key 'fittng'",)


def test_policy_text_blocks_syntax_construction_and_semantic_errors():
    syntax = validate_policy_text("not [valid")
    assert not syntax.is_valid and "TOML error" in syntax.errors[0]

    construction = validate_policy_text('facility = "TAVI"\n')
    assert not construction.is_valid and "construction error" in construction.errors[0]

    semantic = validate_policy_text(
        _POLICY_TOML.replace('counting_mode = "monitor"', 'counting_mode = "not-a-mode"'))
    assert not semantic.is_valid
    assert "counting_mode" in semantic.errors[0]


def test_policy_text_never_raises_for_wrong_detector_field_types():
    wrong_index = validate_policy_text(_POLICY_TOML.replace("value_index = 0", 'value_index = "zero"'))
    assert not wrong_index.is_valid
    assert "value_index" in wrong_index.errors[0]

    unhashable_id = validate_policy_text(
        _POLICY_TOML.replace('detector_id = "det"', 'detector_id = ["not", "a-string"]'))
    assert not unhashable_id.is_valid
    assert "detector_id" in unhashable_id.errors[0]
