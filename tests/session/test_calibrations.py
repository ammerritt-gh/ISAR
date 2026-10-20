"""Step-3 gates (the retired unification build order §10.3, git history): the calibration layer reproduces the gold-check recipe.

Fast unit tests cover the version-dedup logic and the state round-trip. The S30 gate runs on a
calibration set generated at test time (elastic herixE scans, companion SPEC, gold table); the IN8
gate runs on the committed IN8 copy's header. Neither reads real data.
"""
import pytest

from artificial import IN8_ANCHOR
from isar.schema import card as card_mod
from isar.session import calibrations
from isar.session.state import SessionState


# --------------------------------------------------------------------------- fast unit tests
def _s30_card():
    from isar.parsers import spec_s30
    return spec_s30.card_template()


def test_version_appended_only_on_change():
    card = _s30_card()
    card.policy.calibration = {"method": "cooper_nathans"}
    sess = SessionState()
    calibrations.refresh(card, sess)
    calibrations.refresh(card, sess)          # identical -> no new version
    assert len(card.state.resolution_versions) == 1
    assert card.state.resolution_versions[0]["version"] == "v1"


def test_calibration_view_rebuilds_from_state():
    """State, not memory, is authoritative — a view rebuilt from card.state matches (restart-safe)."""
    card = _s30_card()
    card.state.energy_zero_series = [{"key": 100.0, "e0": -0.1}, {"key": 200.0, "e0": -0.2}]
    card.state.resolution_versions = [{
        "version": "v1", "method": "bragg_companion",
        "resolution_by_slit": {"18": [0.9, 0.5]}, "slit_by_scan": {"150": 18},
        "default_resolution": [0.96, 0.60],
    }]
    rc = calibrations.calibration_view(card)
    assert rc.zero(150) == pytest.approx(-0.15)      # interpolated between the two points
    assert rc.slit_for(150) == 18
    assert rc.resolution_for_slit(18) == (0.9, 0.5)
    assert rc.resolution_for_slit(999) == (0.96, 0.60)   # unseen slit -> default


def test_missing_zero_is_held_never_zero():
    card = _s30_card()
    card.policy.calibration = {"method": "bragg_companion"}
    # No energy_zero_series -> view has no zeros -> resolve holds (never center=0.0).
    rc = calibrations.calibration_view(card)
    from types import SimpleNamespace
    rec = SimpleNamespace(scan_number=999, spectra={}, raw_header={})
    cr = calibrations.resolve(rec, "ana7", card, rc)
    assert cr.held and cr.center is None


# --------------------------------------------------------------------------- generated-data gates
def test_s30_resolve_matches_gold_check_recipe(tmp_path):
    from isar.analyze import gold
    from isar.calibrate import bragg
    from isar.calibrate.energy_zero import build_energy_calibration
    from isar.parsers import spec_s30
    from isar.synth_format import run_builder

    cs = run_builder.build_s30_calibration_set(tmp_path)
    card = spec_s30.card_template()
    by_scan = {r.scan_number: r for r in spec_s30.parse_file(cs.measurement_path, card)}
    calibrations.refresh(card, SessionState(), companion_files=[cs.companion_path])
    rc = calibrations.calibration_view(card)

    ref = build_energy_calibration(bragg.read_elastic_scans(cs.companion_path))
    for e in cs.elastic:    # the recipe reads each elastic line back where it was injected
        assert ref.zero(e.scan_number) == pytest.approx(e.zero, abs=0.02)

    n = held = 0
    for row in gold.load_gold(cs.gold_path):
        rec = by_scan.get(row.scan)
        if rec is None:
            continue
        n += 1
        cr = calibrations.resolve(rec, "ana7", card, rc)
        ref_zero = ref.zero(row.scan)
        assert (ref_zero is None) == (row.scan in cs.held_scans)
        if ref_zero is None:
            assert cr.held
            held += 1
            continue
        rgfw, rlfw = ref.resolution(row.slit)
        assert cr.center == pytest.approx(ref_zero)
        assert cr.gauss_fwhm == pytest.approx(rgfw)
        assert cr.lorentz_fwhm == pytest.approx(rlfw)
    assert n == cs.n_gold_matched
    assert 0 < held < n     # both branches ran: some scans held, some resolved


def test_in8_cooper_nathans_center_zero():
    from isar.parsers import ill_in8
    from isar.calibrate import tas

    card = ill_in8.card_template()
    rec = ill_in8.parse_file(IN8_ANCHOR, card)[0]
    calibrations.refresh(card, SessionState())
    rc = calibrations.calibration_view(card)
    cr = calibrations.resolve(rec, "det0", card, rc)
    assert cr.center == 0.0
    assert cr.gauss_fwhm == pytest.approx(tas.cn_energy_fwhm(rec, w=0.0))
    assert cr.resolution_source == "cooper_nathans"
