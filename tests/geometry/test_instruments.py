"""Instrument-profile Bragg-angle behavior."""
import pytest

from isar.geometry.instruments import PUMA


def test_bragg_two_theta_bogus_which_raises():
    # Anything other than "mono"/"analyzer" must raise, not silently select the analyzer.
    with pytest.raises(ValueError):
        PUMA.bragg_two_theta_deg(2.6634, "monochromator")


def test_bragg_two_theta_valid_which_ok():
    assert PUMA.bragg_two_theta_deg(2.6634, "mono") > 0
    assert PUMA.bragg_two_theta_deg(2.6634, "analyzer") > 0
