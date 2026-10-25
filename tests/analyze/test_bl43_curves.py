"""The committed BL43LXU curves load through the card's pattern, and a missing one is a KeyError."""
import tomllib

import pytest

from isar.synth import bl43_curves


def _card_ids():
    with bl43_curves.CARD.open("rb") as f:
        return [d["detector_id"] for d in tomllib.load(f)["detectors"]]


def test_library_holds_every_card_analyzer_at_the_one_slit():
    lib = bl43_curves.library()
    assert len(lib) == 28
    assert lib.settings() == [bl43_curves.SETTING]
    assert all(lib.get(d, bl43_curves.SETTING) is not None for d in _card_ids())


def test_a06_curve_reads_its_sidecar_width():
    c = bl43_curves.curve()
    assert (c.detector_id, c.setting) == ("a06", "40x40")
    assert c.range == (-20.003, 59.997)
    assert c.facts["res_fwhm"]["value"] == 1.126
    # The table's own half-maximum width agrees with the sidecar's, so the file was read whole.
    assert c.fwhm == pytest.approx(1.126, abs=0.01)


def test_unknown_analyzer_is_a_key_error_naming_it():
    with pytest.raises(KeyError, match="a99"):
        bl43_curves.curve("a99")
