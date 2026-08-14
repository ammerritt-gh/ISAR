"""Declared per-catalogue-class placement rules (SCENE-4 PR 4.2a, commit A4).

Placement rules are ordering declarations ONLY, consumed later (A5) by the opening book. This
module pins: every default-profile class resolves explicitly (no fallback), the fallback
mechanism itself on a synthetic profile, unknown-profile refusal, manifest determinism/fingerprint
sensitivity, and dataclass immutability.
"""
import dataclasses

import pytest

from isar.analyze import catalogue


def test_every_default_class_has_an_explicit_rule():
    resolved = catalogue.placement_rules()
    default_classes = {d.name for d in catalogue.DEFAULT_CATALOGUE}
    assert set(resolved) == default_classes
    assert all(not r.used_fallback for r in resolved.values())


def test_bg_only_and_elastic_are_not_applicable():
    resolved = catalogue.placement_rules()
    assert resolved["bg_only"].rule.anchor_kind == "not_applicable"
    # not a "null" entry per se, but not a placement decision either (see rule note)
    assert resolved["elastic"].rule.anchor_kind == "not_applicable"


def test_acoustic_anchors_zone_centre_low_omega():
    rule = catalogue.placement_rules()["acoustic"].rule
    assert rule.anchor_kind == "zone_centre"
    assert rule.omega_band_hint_meV is not None
    lo, hi = rule.omega_band_hint_meV
    assert lo == 0.0 and hi > 0.0


def test_unknown_profile_raises():
    with pytest.raises(KeyError):
        catalogue.placement_rules("no.such.profile/1")


def test_fallback_resolution_on_a_synthetic_profile(monkeypatch):
    synth = "isar.catalogue.placement-fallback-test/1"
    monkeypatch.setitem(catalogue.PROFILES, synth,
                        (catalogue._UNKNOWN, catalogue._BG_ONLY))
    monkeypatch.setitem(catalogue.PLACEMENT_RULES, synth, {
        "unknown": catalogue.PlacementRule("uniform", None, 0.3, "explicit"),
        catalogue.FALLBACK_RULE_KEY: catalogue.PlacementRule(
            "not_applicable", None, 0.0, "generic fallback"),
    })

    resolved = catalogue.placement_rules(synth)

    assert resolved["unknown"].used_fallback is False
    assert resolved["bg_only"].used_fallback is True
    assert resolved["bg_only"].rule.note == "generic fallback"


def test_an_orphaned_rule_row_is_refused_not_silently_carried(monkeypatch):
    """A rule naming a class the profile does not declare is a typo or stale registration —
    it would silently never resolve, so placement_rules refuses it loudly (A4 review)."""
    synth = "isar.catalogue.placement-orphan-test/1"
    monkeypatch.setitem(catalogue.PROFILES, synth, (catalogue._UNKNOWN,))
    monkeypatch.setitem(catalogue.PLACEMENT_RULES, synth, {
        "unknown": catalogue.PlacementRule("uniform", None, 0.3, "explicit"),
        "acoustc": catalogue.PlacementRule("zone_centre", None, 1.0, "typoed class name"),
    })

    with pytest.raises(KeyError, match="acoustc"):
        catalogue.placement_rules(synth)


def test_profile_with_no_fallback_and_a_missing_class_raises(monkeypatch):
    synth = "isar.catalogue.placement-no-fallback-test/1"
    monkeypatch.setitem(catalogue.PROFILES, synth, (catalogue._BG_ONLY,))
    monkeypatch.setitem(catalogue.PLACEMENT_RULES, synth, {})   # no rows, no fallback

    with pytest.raises(KeyError):
        catalogue.placement_rules(synth)


def test_manifest_is_deterministic_across_calls():
    assert catalogue.placement_rules_manifest() == catalogue.placement_rules_manifest()
    assert catalogue.placement_rules_fingerprint() == catalogue.placement_rules_fingerprint()


def test_fingerprint_changes_when_a_rule_field_changes(monkeypatch):
    original_fp = catalogue.placement_rules_fingerprint()

    mutated = dict(catalogue._DEFAULT_PLACEMENT_RULES)   # mutate a copy, not the module constant
    mutated["acoustic"] = dataclasses.replace(mutated["acoustic"], order_weight=0.99)
    monkeypatch.setitem(catalogue.PLACEMENT_RULES, catalogue.DEFAULT_PROFILE, mutated)

    assert catalogue.placement_rules_fingerprint() != original_fp
    assert catalogue._DEFAULT_PLACEMENT_RULES["acoustic"].order_weight == 1.0   # untouched


def test_placement_rule_is_frozen():
    rule = catalogue.PlacementRule("uniform", None, 0.5, "note")
    with pytest.raises(dataclasses.FrozenInstanceError):
        rule.order_weight = 1.0


def test_placement_rule_rejects_bad_anchor_kind():
    with pytest.raises(ValueError):
        catalogue.PlacementRule("diagonal", None, 0.5, "note")
