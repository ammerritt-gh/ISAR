"""The class-list-blind placement registry (SCENE-4.2a A5 boundary repair).

``isar.analyze.catalogue``'s ``placement_rules``/manifest/fingerprint wrappers (tested in
``tests/analyze/test_placement_rules.py``) already exercise the class-list-aware completeness
and orphan-refusal semantics end to end. This module pins only what's specific to the RAW,
class-list-blind layer: the caller supplies the class list, and the ordering-only accessor
never raises — it has to stay inert so an opening-book proposal's ordering can never block a
survey on missing placement data.
"""
import pytest

from isar.schema import placement


def test_resolve_placement_rules_with_a_caller_supplied_class_list():
    resolved = placement.resolve_placement_rules(
        "isar.catalogue.default/1", ["acoustic", "optic", "elastic", "quasielastic",
                                     "unknown", "bg_only"])
    assert resolved["acoustic"].rule.anchor_kind == "zone_centre"
    assert not resolved["acoustic"].used_fallback


def test_resolve_placement_rules_orphan_and_unregistered_profile_refuse():
    with pytest.raises(KeyError, match="not declare"):
        placement.resolve_placement_rules("isar.catalogue.default/1", ["acoustic"])  # missing 5
    with pytest.raises(KeyError, match="no placement rules registered"):
        placement.resolve_placement_rules("no.such.profile/1", [])


def test_zone_centre_order_weight_picks_the_max_and_ignores_other_anchors():
    weight = placement.zone_centre_order_weight("isar.catalogue.default/1")
    assert weight == 1.0  # acoustic (1.0) beats optic (0.6), the only two zone_centre rows


def test_zone_centre_order_weight_never_raises_on_an_unregistered_profile():
    assert placement.zone_centre_order_weight("no.such.profile/1") == 0.0
