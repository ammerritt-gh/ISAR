"""scan_keys tests — the one source of truth for the atomic scan-unit key (Pass 2b, Qt-free).

Covers key construction, tolerant splitting across the three accepted forms, the scan-level prefix,
and the legacy-bridge :func:`member_matches` semantics (an ``exp:scan`` v1 membership must cover every
``exp:scan:det`` unit of that scan, while a unit membership matches only its own unit).
"""
from __future__ import annotations

from isar.workspace_api.scan_keys import split_key as facade_split_key
from isar_gui.scan_keys import member_matches, scan_prefix, split_key, unit_key


def test_unit_key_builds_canonical_form():
    assert unit_key("tavi", 469, "ana5") == "tavi:469:ana5"


def test_split_full_unit_key():
    assert split_key("s30:12:ana3") == ("s30", 12, "ana3")


def test_split_legacy_scan_level_key():
    assert split_key("tavi:469") == ("tavi", 469, None)


def test_split_bare_scan_number():
    assert split_key("469") == (None, 469, None)


def test_split_experiment_id_with_colon():
    # Right anchoring keeps colon-bearing experiment ids intact.
    assert split_key("beam:run:88:d1") == ("beam:run", 88, "d1")


def test_split_numeric_detector_not_mistaken_for_scan():
    # The detector can itself be numeric because the scan is immediately before it.
    assert split_key("tavi:469:5") == ("tavi", 469, "5")


def test_right_anchoring_preserves_numeric_experiment_components():
    key = "IN8:2026:17:4"
    assert split_key(key) == ("IN8:2026", 17, "4")
    # The GUI is only a compatibility re-export; the Qt-free facade must agree exactly.
    assert facade_split_key(key) == split_key(key)


def test_scan_prefix_drops_detector():
    assert scan_prefix("s30:12:ana3") == "s30:12"
    assert scan_prefix("s30:12") == "s30:12"
    assert scan_prefix("469") == "469"


def test_member_matches_exact_unit():
    assert member_matches("s30:12:ana3", "s30:12:ana3") is True
    assert member_matches("s30:12:ana3", "s30:12:ana4") is False


def test_member_matches_legacy_scan_level_covers_every_unit():
    # A v1 scan-level membership covers all analyzer units of that scan.
    assert member_matches("tavi:469", "tavi:469:d1") is True
    assert member_matches("tavi:469", "tavi:469:d9") is True
    # ...but not a different scan.
    assert member_matches("tavi:469", "tavi:470:d1") is False


def test_member_matches_unit_membership_does_not_cover_siblings():
    # A precise unit membership pins only its own unit, not the whole scan.
    assert member_matches("tavi:469:d1", "tavi:469:d2") is False
