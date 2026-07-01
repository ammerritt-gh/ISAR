"""Cooper-Nathans energy resolution (isar.synth.resolution) on committed IN8 numors.

The Phase-1 check (docs/in8_phase1_check.py) established that the elastic energy FWHM for
this DIR-397 configuration is ~0.58 meV, computed from the numor header by a port of ResCal
`rc_cnmat` (Cooper & Nathans 1967). Step 8 promotes that code to isar/synth/resolution; this
test pins the promoted function to the Phase-1 value and exercises the no-triangle fallback.
"""
from pathlib import Path

from isar.parsers import ill_in8
from isar.synth import resolution

REPO = Path(__file__).resolve().parents[2]
IN8 = REPO / "test_data" / "ILL IN8"


def _record(numor):
    card = ill_in8.card_template()
    return ill_in8.parse_file(IN8 / numor, card)[0]


def test_cn_energy_fwhm_recovers_elastic_width_on_055182():
    fwhm = resolution.cn_energy_fwhm(_record("055182"), w=0.0)
    assert 0.45 < fwhm < 0.75  # Phase-1: ~0.58 meV at the elastic line


def test_cn_energy_fwhm_falls_back_when_triangle_cannot_close():
    rec = _record("055182")
    rec.raw_header["params"]["KFIX"] = 0.001  # too small to close the triangle at this |Q|
    assert resolution.cn_energy_fwhm(rec, w=0.0, fallback=1.23) == 1.23


def test_in8_card_enables_a_quasielastic_component():
    """The IN8 card declares the broad quasi-elastic width (§8), which is what makes
    enumerate_structures admit the QE hypothesis on TAS; S30's card leaves it unset."""
    card = ill_in8.card_template()
    assert card.policy.quasielastic_fwhm == 1.5
