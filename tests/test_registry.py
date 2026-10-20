"""Versioned parser registry (§6): detect (facility, instrument, format_version) from a file's
header/date, dispatch the matching plugin. A drifted format is a new registry entry keyed to its
date range, never an edit to a working parser.
"""
import pytest

from artificial import IN8_MEAS as IN8_FILE, S30 as S30_FILE
from isar.parsers import registry


@pytest.fixture(autouse=True)
def _isolate_registry():
    snap = dict(registry.REGISTRY)
    yield
    registry.REGISTRY.clear()
    registry.REGISTRY.update(snap)


class _DummyPlugin:
    name = "dummy"
    version = "0.0.0"


def test_detect_classifies_s30_file():
    assert registry.detect(S30_FILE) == ("APS", "S30", "2025")


def test_detect_classifies_in8_file():
    assert registry.detect(IN8_FILE) == ("ILL", "IN8", "2025")


def test_detect_distinguishes_spec_from_numor():
    # The two formats must not collide — different facility/instrument tuples.
    assert registry.detect(S30_FILE)[:2] != registry.detect(IN8_FILE)[:2]


def test_default_plugin_dispatches_each_copy_to_its_parser():
    assert registry.default_plugin_name(*registry.detect(S30_FILE)) == "spec_s30"
    assert registry.default_plugin_name(*registry.detect(IN8_FILE)) == "ill_in8"


def test_register_and_get_round_trip():
    plugin = _DummyPlugin()
    registry.register(plugin, "APS", "S30", "2025")
    assert registry.get("APS", "S30", "2025") is plugin


def test_get_unknown_key_raises():
    with pytest.raises(KeyError):
        registry.get("NOPE", "NOPE", "0000")


def test_plugin_for_dispatches_by_detect():
    plugin = _DummyPlugin()
    registry.register(plugin, "APS", "S30", "2025")
    assert registry.plugin_for(S30_FILE) is plugin
