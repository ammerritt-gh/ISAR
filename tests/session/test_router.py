"""Step-2 gate: event routing partitions by scan_type; context updates mutate card state."""
from types import SimpleNamespace

from isar.schema import card as card_mod
from isar.session import router
from isar.session.state import SessionState


def _rec(n, scan_type, exp="E"):
    return SimpleNamespace(experiment_id=exp, scan_number=n, scan_type=scan_type)


def test_route_partitions_by_scan_type():
    recs = [
        _rec(1, "measurement"), _rec(2, "alignment"),
        _rec(3, "calibration"), _rec(4, "environment"), _rec(5, "mystery"),
    ]
    rb = router.route(recs)
    assert [r.scan_number for r in rb.measurements] == [1]
    assert [r.scan_number for r in rb.alignments] == [2]
    assert [r.scan_number for r in rb.calibrations] == [3]
    assert [r.scan_number for r in rb.environments] == [4]
    assert [r.scan_number for r in rb.other] == [5]     # unknown type surfaced, not fit


def _min_card():
    pol = card_mod.CardPolicy(
        facility="ILL", instrument="IN8", format_version="2025", plugin="ill_in8",
        detectors=[], energy_column="EN", monitor_column="M1", counting_mode="monitor",
        normalize_by="monitor", raw_abscissa_kind="energy", energy_status="absolute",
        measurement_pattern="bs", alignment_pattern="sc",
        temperature_rule={}, collation={}, sample={"name": "FeSe"},
    )
    return card_mod.Card(pol, card_mod.CardState())


def test_alignment_updates_sample_map():
    card = _min_card()
    rb = router.route([_rec(7, "alignment")])
    lines = router.apply_context_updates(rb, card, SessionState(), companion_files=[])
    assert card.state.sample_map[-1]["from_scan"] == 7
    assert card.state.sample_map[-1]["sample"] == "FeSe"
    assert any("alignment scan 7" in l for l in lines)
