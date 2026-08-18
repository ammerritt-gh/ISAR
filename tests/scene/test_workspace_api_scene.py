"""The scene adapter (``isar.workspace_api.scene``) over a stamped synthetic workspace."""
import json
import logging

import pytest

from isar.schema.measurement import MeasurementArena
from isar.schema.statistics import FIT_SELECTION_SCHEMA, authority_scopes
from isar.workspace_api import Workspace
from isar.scene.model import (
    ComponentDescriptor, SampleKnowledge, covers, footprint_of, partition_key,
)
from isar.workspace_api.scene import sample_knowledge, scene_observations
from synthetic_ws import _POLICY_TOML, FULL_COLUMNS, stamp_ws

# The adapter reads persisted columns the shared stamper's base header predates.
COLUMNS = FULL_COLUMNS + ["energy_lo", "energy_hi", "energy_bands", "evidence_sink",
                          "effective_exposure", "fit_selection_semantics", "content_hash"]


def _selection(method="laplace_evidence_selection", fallback=None,
               weight_kind="posterior_probability", arena="c" * 64):
    """A minimal ``isar.fit_selection_semantics/2`` record the schema reader accepts.

    Only the selection-lane triple and the arena fingerprint matter here; the record is written
    through the real validator's rules so a lane the writer could never produce cannot be pinned.
    """
    record = {"schema": FIT_SELECTION_SCHEMA,
              "authority_scopes": authority_scopes(single_fit_delivery="active"),
              "model_selection_fingerprint": "b" * 64,
              "selection_eligibility": "eligible", "selection_ineligibility_reasons": [],
              "selection_method": method, "selection_fallback": fallback,
              "model_weight_kind": weight_kind}
    if arena is not None:
        record["evidence_arena_fingerprint"] = arena
        record["evidence_revision"] = "rev-1"
    return json.dumps(record)


def _candidate(label, **kw):
    row = {"label": label, "k": 5, "n_modes": 1, "log_evidence": -12.5,
           "evidence_method": "nested", "posterior_probability": 0.7, "usable": True,
           "identity": None, "variant": None, "omegas": [4.0],
           "components": [{"kind": "mode", "slot": 0}],
           # SCENE-4 PR 4.0 (#21): "recorded, unconstrained" — an ordinary current-contract row,
           # never the tri-state ``None`` production reserves for a pre-H9 delivery. A test that
           # wants the #21 refusal lane passes ``constraint_origin=None`` explicitly.
           "constraint_origin": "none"}
    row.update(kw)
    # SCENE-4 PR 4.2a: honestly-empty by default (no writer-side interval declared) — a test
    # that wants a populated slot passes ``mode_intervals=`` explicitly.
    row.setdefault("mode_intervals", [None] * len(row["omegas"]))
    return row


def _row(scan, *, candidates, scan_id=None, content_hash="ch-1", **kw):
    row = {
        "scan": scan, "detector": "det", "scan_id": scan_id or f"tavi:{scan}:det",
        "sample": "tavi", "T": 200.0, "h": 2.0, "k": 0.0, "l": 0.0, "status": "fitted",
        "verdict": "resolved", "best_model": "phonon+bg", "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0,
        "energy_lo": -5.0, "energy_hi": 10.0,
        "energy_bands": json.dumps([[-5.0, 10.0]]), "effective_exposure": 1000.0,
        "evidence_sink": "canonical_master",
        "fit_selection_semantics": _selection(),
        "amplitudes": json.dumps({"bg.BG": 3.0, "phonon.A": 20.0}),
        "candidates": candidates if isinstance(candidates, str) else json.dumps(candidates),
        "content_hash": content_hash,
    }
    row.update(kw)
    return row


def _write_measurements(root, rows) -> None:
    """The counts-authoritative record (SCENE-4 PR 4.0, #26) a real ``isar run`` would have
    written BEFORE every delivered-fit row in ``rows`` — every test in this file predates that
    contract, so this fixture backfills it to keep them exercising the SAME adapter path a real
    campaign does, rather than one this file happens not to need. A pooled master carries no
    record of its own (masters never go through ``isar.session.scan_delivery`` — out of this work
    package's scope), so pooled rows are skipped, matching production."""
    lines = []
    for r in rows:
        if r.get("candidates") is None or r.get("pooled"):
            continue
        scan = r.get("scan")
        det = r.get("detector") or "det"
        scan_id = r.get("scan_id") or f"tavi:{scan}:{det}"
        h, k, l = r.get("h"), r.get("k"), r.get("l")
        elo, ehi = r.get("energy_lo"), r.get("energy_hi")
        # Parsed loosely: a MALFORMED cell here exercises the FIT ROW's own parse-failure path
        # (isar.workspace_api.scene._bands), not this fixture's — a real record's bands are
        # derived independently of the delivered CSV cell (isar.session.scan_delivery), so a
        # synthetic fixture that cannot parse the row's cell just leaves the record's bands None
        # rather than raising here, one test-setup step before the assertion under test.
        bands = None
        bands_cell = r.get("energy_bands")
        if bands_cell is not None:
            try:
                parsed = json.loads(bands_cell) if isinstance(bands_cell, str) else bands_cell
                bands = tuple((float(b[0]), float(b[1])) for b in parsed)
            except (TypeError, ValueError, IndexError, json.JSONDecodeError):
                bands = None
        record = MeasurementArena(
            scan_ref=scan_id, scan_id=scan_id, scan_number=scan, detector=det,
            sample=r.get("sample"),
            conditions=(("temperature_K", float(r["T"])),) if r.get("T") is not None else (),
            q=(None if h is None or k is None or l is None else (float(h), float(k), float(l))),
            lattice_epoch=None,
            energy_window=(None if elo is None or ehi is None else (float(elo), float(ehi))),
            energy_bands=bands, effective_exposure=r.get("effective_exposure"),
            exposure_provenance=None, count_trust="trusted", reasons=(),
            written_at="2026-01-01T00:00:00+00:00", content_hash=r.get("content_hash") or "ch-1")
        lines.append(json.dumps(record.to_dict(), sort_keys=True))
    if lines:
        rd = root / "report"
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "measurements.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ws(root, rows, columns=COLUMNS):
    root.mkdir(parents=True, exist_ok=True)
    stamp_ws(root, rows=rows, columns=columns)
    _write_measurements(root, rows)
    return Workspace.open(root)


def test_row_becomes_an_observation_with_parsed_candidates(tmp_path):
    cands = [_candidate("phonon+bg"),
             _candidate("2phonon+bg", n_modes=2, omegas=[4.0, None]),
             _candidate("bg", n_modes=0, omegas=[], usable=False, posterior_probability=None)]
    obs, = scene_observations(_ws(tmp_path, [_row(1, candidates=cands)]))

    assert obs.scan_ref == "tavi:1:det"
    assert obs.q == (2.0, 0.0, 0.0)
    assert obs.temperature_K == 200.0
    assert obs.energy_window == (-5.0, 10.0)
    assert (obs.gauss_fwhm, obs.lorentz_fwhm) == (1.2, 0.0)
    assert obs.effective_exposure == 1000.0
    assert obs.background == 3.0
    assert obs.best_label == "phonon+bg"
    assert len(obs.fingerprint) == 64
    # sorted by label, not stamped order
    assert [c.label for c in obs.candidates] == ["2phonon+bg", "bg", "phonon+bg"]
    by_label = {c.label: c for c in obs.candidates}
    assert by_label["2phonon+bg"].omegas == (4.0, None)     # None slot survives the round trip
    assert by_label["bg"].omegas == ()                      # [] null stays empty, not (None,)
    assert by_label["bg"].usable is False                   # unusable rows are kept as evidence


def test_unplaceable_window_and_missing_q_degrade_to_none(tmp_path):
    ws = _ws(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")],
                             energy_lo=None, energy_hi=10.0, h=None, k=None, l=None)])
    obs, = scene_observations(ws)
    assert obs.energy_window is None and obs.q is None


def test_deterministic_across_calls_and_candidate_permutation(tmp_path):
    cands = [_candidate("phonon+bg"), _candidate("bg", n_modes=0, omegas=[])]
    first = scene_observations(_ws(tmp_path, [_row(1, candidates=cands)]))
    assert [o.to_dict() for o in first] == [
        o.to_dict() for o in scene_observations(_ws(tmp_path, [_row(1, candidates=cands)]))]

    permuted = scene_observations(_ws(tmp_path / "b", [_row(1, candidates=list(reversed(cands)))]))
    assert [o.fingerprint for o in permuted] == [o.fingerprint for o in first]


def test_master_supersedes_its_member_scans(tmp_path):
    cands = [_candidate("phonon+bg")]
    master = _row(9_000_001, candidates=cands, scan_id="master:tavi_2.0,0,0_300",
                  pooled=True, n_members=2, members="tavi:1:det;tavi:2:det")
    refs = [o.scan_ref for o in scene_observations(
        _ws(tmp_path, [_row(1, candidates=cands), _row(2, candidates=cands),
                       _row(3, candidates=cands), master]))]
    # Members 1 and 2 fitted the same neutrons the master pooled; scan 3 is its own arena.
    assert refs == ["master:tavi_2.0,0,0_300:det", "tavi:3:det"]


def test_master_supersedes_a_member_on_a_detector_it_is_not_stamped_with(tmp_path):
    """The collation key is (sample, q, T) and can span detectors, while the master row carries
    only members[0]'s detector. The qualified member cell is what makes det2 findable — missing it
    would let scan 2's neutrons enter the scene twice."""
    cands = [_candidate("phonon+bg")]
    master = _row(9_000_001, candidates=cands, scan_id="master:tavi_2.0,0,0_300",
                  detector="det1", pooled=True, n_members=2,
                  members="tavi:1:det1;tavi:2:det2")
    refs = [o.scan_ref for o in scene_observations(_ws(tmp_path, [
        _row(1, candidates=cands, detector="det1", scan_id="tavi:1:det1"),
        _row(2, candidates=cands, detector="det2", scan_id="tavi:2:det2"),
        master]))]
    assert refs == ["master:tavi_2.0,0,0_300:det1"]


def test_a_non_member_on_another_detector_survives(tmp_path):
    """Same scan number, different detector, NOT in the member cell (different q → different
    group): its arena stands."""
    cands = [_candidate("phonon+bg")]
    master = _row(9_000_001, candidates=cands, scan_id="master:tavi_2.0,0,0_300",
                  detector="det1", pooled=True, n_members=1, members="tavi:1:det1")
    refs = [o.scan_ref for o in scene_observations(_ws(tmp_path, [
        _row(1, candidates=cands, detector="det1", scan_id="tavi:1:det1"),
        _row(1, candidates=cands, detector="det2", scan_id="tavi:1:det2"),
        master]))]
    assert refs == ["master:tavi_2.0,0,0_300:det1", "tavi:1:det2"]


def test_a_bare_member_label_is_refused_not_guessed_at(tmp_path):
    """A workspace written before member labels were qualified. A bare label names no detector and
    the collation key can span detectors, so there is no honest reading: guessing wide drops an
    arena, guessing narrow counts one dataset's evidence twice. It is re-delivered instead."""
    cands = [_candidate("phonon+bg")]
    master = _row(9_000_001, candidates=cands, scan_id="master:tavi_2.0,0,0_300",
                  detector="det1", pooled=True, n_members=1, members="1")
    ws = _ws(tmp_path, [_row(1, candidates=cands, detector="det1", scan_id="tavi:1:det1"),
                        master])
    with pytest.raises(ValueError, match=r"master:tavi_2.0,0,0_300:det1.*re-deliver"):
        scene_observations(ws)


def test_candidateless_master_supersedes_nothing(tmp_path):
    cands = [_candidate("phonon+bg")]
    master = _row(9_000_001, candidates=None, scan_id="master:tavi_2.0,0,0_300",
                  pooled=True, n_members=1, members="tavi:1:det")
    refs = [o.scan_ref for o in scene_observations(
        _ws(tmp_path, [_row(1, candidates=cands), master]))]
    assert refs == ["tavi:1:det"]


@pytest.mark.parametrize("sink", ["auxiliary_track_node", "track_edge_evidence",
                                  "branch_belief_only", "some_future_routing"])
def test_a_non_canonical_sink_keeps_its_candidates_and_its_sink(tmp_path, sink):
    """Auxiliary sinks are scientific evidence channels (identity, connectivity, transition), not
    lesser evidence. The adapter preserves what they measured verbatim — including a sink string it
    does not recognize — and the builder is what decides to defer or quarantine it."""
    ws = _ws(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")], evidence_sink=sink)])
    obs, = scene_observations(ws)
    assert [c.label for c in obs.candidates] == ["phonon+bg"]
    assert obs.evidence_sink == sink
    assert obs.energy_window == (-5.0, 10.0)      # still footprint-bearing


def test_a_barred_master_neither_supersedes_its_members_nor_scores(tmp_path):
    """A master routed to a non-canonical sink never became the authoritative reading of those
    neutrons. Erasing its members would delete the only canonical evidence for them; scoring the
    master itself would score barred evidence. It stays an arena, and the builder declines it."""
    cands = [_candidate("phonon+bg")]
    master = _row(9_000_001, candidates=cands, scan_id="master:tavi_2.0,0,0_300",
                  pooled=True, n_members=1, members="tavi:1:det",
                  evidence_sink="auxiliary_track_node")
    by_ref = {o.scan_ref: o for o in scene_observations(
        _ws(tmp_path, [_row(1, candidates=cands), master]))}
    assert sorted(by_ref) == ["master:tavi_2.0,0,0_300:det", "tavi:1:det"]
    assert by_ref["master:tavi_2.0,0,0_300:det"].evidence_sink == "auxiliary_track_node"
    assert by_ref["tavi:1:det"].evidence_sink == "canonical_master"


@pytest.mark.parametrize("cell,status,fallback", [
    (_selection(), "complete", None),
    (_selection("aic_fallback", "evidence_budget_exhausted", "akaike_relative_weight"),
     "incomplete", "evidence_budget_exhausted"),
    (_selection("aic_fallback", "aic_no_evidence_inputs", "akaike_relative_weight", arena=None),
     "unavailable", "aic_no_evidence_inputs"),
    (_selection("aic_fallback", "aic_all_evidence_invalid", "akaike_relative_weight", arena=None),
     "unavailable", "aic_all_evidence_invalid"),
    (_selection("none", None, "none", arena=None), "unavailable", None),
    ("", "unavailable", None),                      # no record at all
])
def test_evidence_comparison_status_maps_the_persisted_selection_lane(tmp_path, cell, status,
                                                                     fallback):
    """H7: only the Bayesian lane with no fallback is a COMPLETE candidate comparison. Every other
    lane, and every unreadable record, fails closed to unavailable — never to complete."""
    obs, = scene_observations(_ws(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")],
                                                  fit_selection_semantics=cell)]))
    assert (obs.evidence_comparison_status, obs.selection_fallback) == (status, fallback)


def test_an_unrecognized_selection_fallback_fails_closed_and_warns(tmp_path, caplog):
    """The fallback vocabulary is validated on read, so an unknown value cannot reach here through
    a valid record — but the mapping must still refuse to invent completeness if it ever does."""
    from isar.workspace_api import scene as scene_mod
    fit = type("F", (), {"fit_selection_semantics": {
        "selection_method": "aic_fallback", "selection_fallback": "invented_lane",
        "evidence_arena_fingerprint": None}})()
    with caplog.at_level(logging.WARNING, logger="isar.workspace_api.scene"):
        assert scene_mod._evidence_comparison(fit, "tavi:1:det")[0] == "unavailable"
    assert any("invented_lane" in r.getMessage() for r in caplog.records)


def test_the_budget_exhausted_arena_does_not_score(tmp_path):
    """The reviewer's scenario end to end: candidate A resolved, B unresolved, budget exhausted.
    The scene must record an incomplete comparison, not a singleton P(A)=1 posterior."""
    from isar.scene.build import build_scene
    cands = [_candidate("A"), _candidate("B", log_evidence=None, posterior_probability=None)]
    obs = scene_observations(_ws(tmp_path, [_row(1, candidates=cands, fit_selection_semantics=(
        _selection("aic_fallback", "evidence_budget_exhausted", "akaike_relative_weight")))]))
    payload = build_scene(obs).payload
    assert payload["clusters"] == []
    assert payload["non_scoring"] == [{"scan_ref": "tavi:1:det", "reason": "evidence_incomplete"}]
    assert obs[0].arena_fingerprint == "c" * 64


def test_conditioning_provenance_survives_the_adapter(tmp_path):
    """H9's per-candidate conditioning record is carried verbatim, so the builder can tell whether
    a transported constraint has already been priced in this arena."""
    constraints = [{"constraint_kind": "hard", "parameter": "phonon.Gamma",
                    "origin": "transported_prior", "value": 0.5}]
    obs, = scene_observations(_ws(tmp_path, [_row(1, candidates=[
        _candidate("phonon+bg", constraint_origin="transported_prior",
                   effective_constraints=constraints),
        _candidate("bg", constraint_origin="none", effective_constraints=[])])]))
    by_label = {c.label: c for c in obs.candidates}
    assert by_label["phonon+bg"].constraint_origin == "transported_prior"
    assert by_label["phonon+bg"].effective_constraints == tuple(constraints)
    # "recorded, unconstrained" stays distinguishable from "no semantics record".
    assert by_label["bg"].effective_constraints == ()


@pytest.mark.parametrize("cell", ["{not json", json.dumps([{"k": 5}])])
def test_malformed_candidates_refuse_the_build_with_a_redeliver_instruction(tmp_path, cell):
    """SCENE-4 PR 4.0 (#26): a DELIVERED fit whose candidate cell is corrupt is a damaged
    interpretation artifact, not a fitless arena — demoting it to the "no interpretation" lane
    would silently drop local evidence that a clean re-delivery restores. Refuse loudly (H1),
    same posture as the stale-delivery and missing-measurements refusals. The legit fitless lane
    (no delivered fit at all) is covered separately and stays quiet."""
    with pytest.raises(ValueError, match="re-deliver"):
        scene_observations(_ws(tmp_path, [_row(1, candidates=cell)]))


def test_candidate_rows_carry_their_component_descriptors(tmp_path):
    """Review finding 9: the scene reads what a candidate is MADE OF from the row, never from the
    label. The descriptors survive the CSV JSON cell as typed DTOs."""
    cands = [_candidate("phonon+bg"),
             _candidate("elastic+2phonon+bg", n_modes=2, omegas=[4.0, 8.0],
                        components=[{"kind": "mode", "slot": 0}, {"kind": "mode", "slot": 1},
                                    {"kind": "elastic", "slot": None}])]
    obs, = scene_observations(_ws(tmp_path, [_row(1, candidates=cands)]))
    by_label = {c.label: c for c in obs.candidates}
    assert by_label["phonon+bg"].components == (ComponentDescriptor("mode", 0),)
    assert by_label["elastic+2phonon+bg"].components == (
        ComponentDescriptor("mode", 0), ComponentDescriptor("mode", 1),
        ComponentDescriptor("elastic"))


def test_energy_bands_survive_the_csv_and_are_the_footprints_coverage(tmp_path):
    """Review finding 4: the kept runs, not the envelope. The hole between them covers nothing."""
    obs, = scene_observations(_ws(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")],
                                                  energy_lo=1.0,
                                                  energy_bands=json.dumps([[1.0, 4.0],
                                                                           [6.0, 10.0]]))]))
    assert obs.energy_window == (1.0, 10.0)
    assert obs.energy_bands == ((1.0, 4.0), (6.0, 10.0))
    fp = footprint_of(obs)
    assert covers(fp, 3.0) and covers(fp, 8.0)
    assert not covers(fp, 5.0)


@pytest.mark.parametrize("kw,message", [
    ({"candidates": [{k: v for k, v in _candidate("phonon+bg").items() if k != "components"}]},
     "component descriptors"),
    ({"candidates": [{k: v for k, v in _candidate("phonon+bg").items()
                     if k != "mode_intervals"}]},
     "mode interval projections"),
    ({"candidates": [_candidate("phonon+bg")], "energy_bands": None}, "energy_bands"),
])
def test_a_row_predating_the_delivery_contract_is_refused_not_guessed_at(tmp_path, kw, message):
    """H1, the same posture as the bare-member-label refusal: a row written before these fields
    existed cannot be read faithfully. Inferring components from the label would decide physics on
    a display string, and reading the window as hole-free coverage would price absence against a
    region no counting reached — so the workspace is re-delivered instead."""
    ws = _ws(tmp_path, [_row(1, **kw)])
    with pytest.raises(ValueError, match=rf"tavi:1:det.*{message}.*re-deliver"):
        scene_observations(ws)


@pytest.mark.parametrize("cell", ["{not json", json.dumps({"lo": 1.0}), json.dumps([[1.0]])])
def test_a_malformed_energy_bands_cell_is_named_as_malformed_not_as_stale(tmp_path, cell):
    """H1's point is the RIGHT diagnosis, not merely a refusal. An absent field means re-deliver a
    stale workspace; a present-but-unreadable cell is a corrupt row, and sending its owner after a
    re-delivery would send them after the wrong problem."""
    ws = _ws(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")], energy_bands=cell)])
    with pytest.raises(ValueError, match="malformed energy_bands"):
        scene_observations(ws)


def test_a_header_without_the_candidates_column_is_refused(tmp_path):
    """Such a file can score nothing anywhere. Reading it as an empty scene would present a whole
    campaign as unmeasured, so it is refused with the same re-deliver instruction."""
    legacy = [c for c in COLUMNS if c != "candidates"]
    ws = _ws(tmp_path, [_row(1, candidates=None), _row(2, candidates=None)], columns=legacy)
    with pytest.raises(ValueError, match=r"no candidates column.*re-deliver"):
        scene_observations(ws)


def test_rows_that_never_had_a_fit_are_silent(tmp_path, caplog):
    """A real campaign carries many held/context/excluded ledger rows (written detectorless).
    They yield no observation, and must not drown the malformed-cell warning in noise."""
    held = {"scan": 7, "scan_id": "tavi:7:", "status": "held_for_calibration",
            "reasons": "no energy zero"}
    with caplog.at_level(logging.DEBUG, logger="isar.workspace_api.scene"):
        assert scene_observations(_ws(tmp_path, [held])) == []
    assert [r.levelname for r in caplog.records] == ["DEBUG"]


# ---------------------------------------------------------------------------
# Measurement-arena records (SCENE-4 PR 4.0, #26): counts-authoritative intake
# ---------------------------------------------------------------------------
def _write_measurement(root, record: MeasurementArena) -> None:
    rd = root / "report"
    rd.mkdir(parents=True, exist_ok=True)
    with open(rd / "measurements.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")


def _measurement(**over) -> MeasurementArena:
    base = dict(scan_ref="tavi:4:det", scan_id="tavi:4:det", scan_number=4, detector="det",
                sample="tavi", conditions=(("temperature_K", 200.0),), q=(2.0, 0.0, 0.0),
                lattice_epoch=None, energy_window=(-5.0, 10.0), energy_bands=((-5.0, 10.0),),
                effective_exposure=1000.0, exposure_provenance=None, count_trust="trusted",
                reasons=(), written_at="2026-01-01T00:00:00+00:00", content_hash="ch-1")
    return MeasurementArena(**{**base, **over})


def test_a_trusted_fitless_arena_becomes_a_synthetic_observation(tmp_path):
    """A scan whose fit crashed (or converged to nothing) never produces a detector-qualified
    results.csv row (the ledger writes it with an EMPTY ``detectors`` cell) — but its
    measurement-arena record still says the counts were trusted, and the adapter must surface it
    so the scene layer can fill the experiment map with it."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    stamp_ws(tmp_path, rows=[], columns=COLUMNS)
    _write_measurement(tmp_path, _measurement())
    obs, = scene_observations(Workspace.open(tmp_path))
    assert (obs.scan_ref, obs.candidates, obs.count_trust) == ("tavi:4:det", (), "trusted")
    assert obs.q == (2.0, 0.0, 0.0)
    assert obs.energy_bands == ((-5.0, 10.0),)
    assert obs.sample == "tavi"
    assert obs.conditions == (("temperature_K", 200.0),)


def test_a_count_untrusted_arena_becomes_a_synthetic_observation(tmp_path):
    """A gate refusal (excluded / held_for_calibration / undersampled) never reaches a fit either,
    but it is a DIFFERENT claim than a trusted-but-fitless arena — the adapter carries the
    distinction through verbatim; deciding what it means for scoring is the builder's job."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    stamp_ws(tmp_path, rows=[], columns=COLUMNS)
    _write_measurement(tmp_path, _measurement(
        scan_ref="tavi:5:det", scan_id="tavi:5:det", scan_number=5,
        count_trust="held_for_calibration", energy_window=None, energy_bands=None,
        effective_exposure=None, reasons=("no energy zero",)))
    obs, = scene_observations(Workspace.open(tmp_path))
    assert (obs.scan_ref, obs.candidates, obs.count_trust) == (
        "tavi:5:det", (), "held_for_calibration")


def test_missing_measurements_log_with_delivered_candidates_is_refused(tmp_path):
    """H1, the same posture as the missing-candidates-column and missing-energy_bands refusals: a
    results.csv that already carries delivered candidates but no measurements.jsonl at all is a
    stale delivery, not a silently empty counts ledger."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    stamp_ws(tmp_path, rows=[_row(1, candidates=[_candidate("phonon+bg")])], columns=COLUMNS)
    with pytest.raises(ValueError, match=r"measurements\.jsonl.*re-deliver"):
        scene_observations(Workspace.open(tmp_path))


def test_a_delivered_row_with_no_matching_measurement_record_is_refused(tmp_path):
    """A partially-rebuilt or pre-4.0 log: measurements.jsonl exists but does not name this
    scan_ref. Guessing "trusted" would let a broken arena's evidence silently score."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    stamp_ws(tmp_path, rows=[_row(1, candidates=[_candidate("phonon+bg")]),
                             _row(2, candidates=[_candidate("phonon+bg")])], columns=COLUMNS)
    _write_measurement(tmp_path, _measurement(scan_ref="tavi:1:det", scan_id="tavi:1:det",
                                              scan_number=1))
    with pytest.raises(ValueError, match=r"tavi:2:det.*measurement-arena record.*re-deliver"):
        scene_observations(Workspace.open(tmp_path))


# ---------------------------------------------------------------------------
# Provenance join hardening (SCENE-4 PR 4.0 review response, W3)
# ---------------------------------------------------------------------------
def test_a_content_hash_mismatch_between_fit_row_and_record_is_refused(tmp_path):
    """A record from a LATER delivery generation must never join a fit row from an earlier one
    (or vice versa) — the join key is scan_ref AND content_hash, not scan_ref alone."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    stamp_ws(tmp_path, rows=[_row(1, candidates=[_candidate("phonon+bg")],
                                  content_hash="ch-generation-2")], columns=COLUMNS)
    _write_measurement(tmp_path, _measurement(scan_ref="tavi:1:det", scan_id="tavi:1:det",
                                              scan_number=1, content_hash="ch-generation-1"))
    with pytest.raises(ValueError, match=r"tavi:1:det.*different delivery generation"):
        scene_observations(Workspace.open(tmp_path))


def test_a_legacy_row_with_no_content_hash_skips_the_join_check(tmp_path):
    """A row predating the column carries ``content_hash=None`` — nothing to compare, not a
    license to guess a mismatch. The record still supplies the observation's facts."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    columns_no_hash = [c for c in COLUMNS if c != "content_hash"]
    stamp_ws(tmp_path, rows=[_row(1, candidates=[_candidate("phonon+bg")])],
             columns=columns_no_hash)
    _write_measurement(tmp_path, _measurement(scan_ref="tavi:1:det", scan_id="tavi:1:det",
                                              scan_number=1, content_hash="ch-record-only"))
    obs, = scene_observations(Workspace.open(tmp_path))
    assert obs.scan_ref == "tavi:1:det"


def test_counts_own_the_facts_the_record_wins_over_the_fit_row(tmp_path):
    """W3, disposition 4: for a row WITH a record, q/temperature/conditions/sample/window/bands/
    exposure come FROM THE RECORD — the fit row may not silently disagree with what was measured.
    The fixture stamps the record with values that deliberately differ from the row's."""
    record = _measurement(
        scan_ref="tavi:1:det", scan_id="tavi:1:det", scan_number=1, sample="record-sample",
        conditions=(("temperature_K", 77.0),), q=(1.0, 1.0, 1.0),
        energy_window=(-2.0, 2.0), energy_bands=((-2.0, 2.0),), effective_exposure=42.0)
    tmp_path.mkdir(parents=True, exist_ok=True)
    stamp_ws(tmp_path, rows=[_row(1, candidates=[_candidate("phonon+bg")], sample="row-sample",
                                  T=200.0, h=2.0, k=0.0, l=0.0, energy_lo=-5.0, energy_hi=10.0,
                                  energy_bands=json.dumps([[-5.0, 10.0]]),
                                  effective_exposure=1000.0)], columns=COLUMNS)
    _write_measurement(tmp_path, record)
    obs, = scene_observations(Workspace.open(tmp_path))
    assert obs.sample == "record-sample"
    assert obs.conditions == (("temperature_K", 77.0),)
    assert obs.temperature_K == 77.0
    assert obs.q == (1.0, 1.0, 1.0)
    assert obs.energy_window == (-2.0, 2.0)
    assert obs.energy_bands == ((-2.0, 2.0),)
    assert obs.effective_exposure == 42.0
    # The fit row still contributes candidates/resolution/best_label/evidence_sink — unaffected.
    assert obs.best_label == "phonon+bg"


def test_an_empty_but_parseable_candidates_list_on_a_delivered_winner_is_refused(tmp_path):
    """A DELIVERED non-pooled winner (detector + best_label set) whose candidates cell parses to
    an EMPTY list is a contradictory interpretation artifact — something won, so a table must
    exist — and gets the same refuse-don't-degrade posture as the absent/malformed cases."""
    ws = _ws(tmp_path, [_row(1, candidates=[])])
    with pytest.raises(ValueError, match=r"tavi:1:det.*empty candidate table.*re-deliver"):
        scene_observations(ws)


def test_a_master_only_workspace_needs_no_measurements_log(tmp_path):
    """The missing-measurements refusal requires >=1 NON-POOLED candidate-bearing fit; a
    workspace whose only candidate-bearing rows are pooled masters (which legitimately carry no
    measurement-arena record) must proceed with an empty measurements dict, not refuse."""
    cands = [_candidate("phonon+bg")]
    master = _row(9_000_001, candidates=cands, scan_id="master:tavi_2.0,0,0_300",
                  pooled=True, n_members=0, members="")
    tmp_path.mkdir(parents=True, exist_ok=True)
    stamp_ws(tmp_path, rows=[master], columns=COLUMNS)
    assert not (tmp_path / "report" / "measurements.jsonl").exists()
    obs, = scene_observations(Workspace.open(tmp_path))
    assert obs.scan_ref == "master:tavi_2.0,0,0_300:det"


def test_compatible_master_members_derive_provenance_and_stay_suppressed(tmp_path):
    """W3, disposition 5: sample/conditions/lattice_epoch come from the QUALIFIED MEMBERS'
    records, not the current frame, when every member's record agrees."""
    cands = [_candidate("phonon+bg")]
    master = _row(9_000_001, candidates=cands, scan_id="master:tavi_2.0,0,0_300",
                  pooled=True, n_members=2, members="tavi:1:det;tavi:2:det")
    ws = _ws(tmp_path, [_row(1, candidates=cands, sample="tavi", T=200.0),
                        _row(2, candidates=cands, sample="tavi", T=200.0), master])
    by_ref = {o.scan_ref: o for o in scene_observations(ws)}
    assert sorted(by_ref) == ["master:tavi_2.0,0,0_300:det"]   # members suppressed
    m = by_ref["master:tavi_2.0,0,0_300:det"]
    assert m.count_trust == "trusted"
    assert m.sample == "tavi"
    assert m.conditions == (("temperature_K", 200.0),)


def test_incompatible_master_members_degrade_and_their_arenas_survive(tmp_path):
    """W3, disposition 5: members whose records DISAGREE (mixed conditions here) mean the pooled
    interpretation cannot be presented under one frame — the master degrades to non-scoring
    (count_trust off "trusted") rather than guessing, and its members are NOT erased: an
    incompatible master must not both fail AND suppress the only evidence its members have."""
    cands = [_candidate("phonon+bg")]
    master = _row(9_000_001, candidates=cands, scan_id="master:tavi_2.0,0,0_300",
                  pooled=True, n_members=2, members="tavi:1:det;tavi:2:det")
    ws = _ws(tmp_path, [_row(1, candidates=cands, sample="tavi", T=200.0),
                        _row(2, candidates=cands, sample="tavi", T=250.0), master])
    by_ref = {o.scan_ref: o for o in scene_observations(ws)}
    assert sorted(by_ref) == ["master:tavi_2.0,0,0_300:det", "tavi:1:det", "tavi:2:det"]
    assert by_ref["master:tavi_2.0,0,0_300:det"].count_trust != "trusted"
    assert by_ref["tavi:1:det"].count_trust == "trusted"
    assert by_ref["tavi:2:det"].count_trust == "trusted"


# ---------------------------------------------------------------------------
# A pooled master's declared-path identity (SCENE-4 PR 4.3 review response, D3)
# ---------------------------------------------------------------------------
_PATH_A, _PATH_B = "a" * 64, "b" * 64


def _master_ws(root, member_provenance):
    """A two-member canonical master whose members' records carry ``member_provenance`` (one dict
    of campaign-provenance fields per member). The master itself carries no record, as production
    writes it."""
    cands = [_candidate("phonon+bg")]
    master = _row(9_000_001, candidates=cands, scan_id="master:tavi_2.0,0,0_300",
                  pooled=True, n_members=2, members="tavi:1:det;tavi:2:det")
    root.mkdir(parents=True, exist_ok=True)
    stamp_ws(root, rows=[_row(1, candidates=cands), _row(2, candidates=cands), master],
             columns=COLUMNS)
    for n, prov in zip((1, 2), member_provenance):
        _write_measurement(root, _measurement(scan_ref=f"tavi:{n}:det", scan_id=f"tavi:{n}:det",
                                              scan_number=n, **prov))
    return Workspace.open(root)


def _key(obs):
    return partition_key(obs.sample, obs.conditions, obs.lattice_epoch, obs.scan_ref, 1.0,
                         obs.topology_role, obs.path_fingerprint)


def test_a_master_inherits_the_declared_path_its_members_agree_on(tmp_path):
    """The pooled reading must land in the SAME partition as the arenas it pools — a master
    severed from its members' declared path would normalize against a different one."""
    prov = dict(topology_role="canonical_path", path_fingerprint=_PATH_A)
    m, = scene_observations(_master_ws(tmp_path, [prov, prov]))
    assert (m.topology_role, m.path_fingerprint) == ("canonical_path", _PATH_A)
    # Not one program step's product, so no program identity is inherited.
    assert (m.program_fingerprint, m.program_step) == (None, None)
    member = _measurement(**prov)
    assert _key(m) == partition_key(member.sample, member.conditions, member.lattice_epoch,
                                    "tavi:1:det", 1.0, "canonical_path", _PATH_A)


def test_members_declaring_different_paths_leave_the_master_provenance_less(tmp_path):
    """Pooling across two declared paths at one q-point is legitimate, so this is the None lane
    rather than a refusal — but the master may not claim either path as its own."""
    m, = scene_observations(_master_ws(tmp_path, [
        dict(topology_role="canonical_path", path_fingerprint=_PATH_A),
        dict(topology_role="canonical_path", path_fingerprint=_PATH_B)]))
    assert (m.topology_role, m.path_fingerprint) == (None, None)
    assert _key(m)[3] is None


def test_a_master_whose_members_carry_no_provenance_stays_in_the_none_lane(tmp_path):
    """Manual or pre-4.3 members invent nothing (H1)."""
    m, = scene_observations(_master_ws(tmp_path, [{}, {}]))
    assert (m.topology_role, m.path_fingerprint) == (None, None)
    assert _key(m)[3] is None


# ---------------------------------------------------------------------------
# Declared sample knowledge
# ---------------------------------------------------------------------------
_SAMPLE_POLICY = _POLICY_TOML + """
[dispersion]
zone_period = 1.0

[sample]
name = "Al"
reflections = [2.0, 3.0]
"""


def _knowledge_ws(root, policy_toml=_POLICY_TOML, state=None):
    root.mkdir(parents=True, exist_ok=True)
    stamp_ws(root, policy_toml=policy_toml, state=state or {}, rows=[], columns=COLUMNS)
    return Workspace.open(root)


def test_sample_knowledge_reads_the_two_declarations_and_the_lattice_epoch(tmp_path):
    """Both keys are HAND-AUTHORED policy and the epoch series is machine-written state; the
    adapter reads all three and writes none of them (card hard rule 1)."""
    epochs = [{"version": 1, "lattice": {"a": 4.0, "b": 4.0, "c": 4.0,
                                         "alpha": 90.0, "beta": 90.0, "gamma": 90.0}},
              {"version": 2, "lattice": {"a": 4.05, "b": 4.05, "c": 4.05,
                                         "alpha": 90.0, "beta": 90.0, "gamma": 90.0}}]
    ws = _knowledge_ws(tmp_path, _SAMPLE_POLICY, {"lattice_epochs": epochs})
    know = sample_knowledge(ws)
    assert know.zone_period == 1.0
    assert know.reflections == (2.0, 3.0)
    # The LATEST epoch, not the first written: a scene stamped against a superseded lattice
    # would claim a provenance the workspace no longer holds.
    assert know.lattice_epoch == 2
    assert know.lattice == (4.05, 4.05, 4.05, 90.0, 90.0, 90.0)


def test_a_card_that_declares_nothing_yields_empty_knowledge(tmp_path):
    """No new REQUIRED card key: an ordinary card is a legal scene input, and the builder records
    the resulting inertness rather than the adapter guessing a period or a reflection."""
    assert sample_knowledge(_knowledge_ws(tmp_path)) == SampleKnowledge()


def test_an_unreadable_sample_declaration_is_refused(tmp_path):
    """H1: no tolerant fallback between the card and ln W(S). Silently dropping a malformed
    reflection list would make the ledger claim nothing was ever declared."""
    bad = _POLICY_TOML + '\n[sample]\nreflections = ["not-a-number"]\n'
    with pytest.raises(ValueError, match="unreadable sample geometry"):
        sample_knowledge(_knowledge_ws(tmp_path, bad))


def test_a_partially_declared_lattice_cell_is_refused(tmp_path):
    """The one that would otherwise pass as silence. A cell missing gamma is a card someone tried
    to fill in, so reading it as "no lattice declared" hides a real error behind a legal state —
    while an absent cell really is nothing declared and stays None."""
    half = [{"version": 1, "lattice": {"a": 4.05, "b": 4.05, "c": 4.05,
                                       "alpha": 90.0, "beta": 90.0}}]
    with pytest.raises(ValueError, match="expected all of"):
        sample_knowledge(_knowledge_ws(tmp_path, state={"lattice_epochs": half}))

    bare = [{"version": 1, "method": "manual"}]
    know = sample_knowledge(_knowledge_ws(tmp_path / "bare", state={"lattice_epochs": bare}))
    assert (know.lattice_epoch, know.lattice) == (1, None)


def test_the_latest_epoch_is_chosen_by_version_number_not_by_string_order(tmp_path):
    """A JSON-typed version is a real possibility, and lexical order gets it backwards: "9" sorts
    above "10", so the newest epoch would silently lose to a superseded one. A version that is not
    a number at all fails with the geometry message rather than a bare TypeError."""
    typed = [{"version": "9"}, {"version": "10"}]
    assert sample_knowledge(
        _knowledge_ws(tmp_path, state={"lattice_epochs": typed})).lattice_epoch == 10

    junk = [{"version": 1}, {"version": "epoch-two"}]
    with pytest.raises(ValueError, match="unreadable sample geometry"):
        sample_knowledge(_knowledge_ws(tmp_path / "junk", state={"lattice_epochs": junk}))


# ----------------------------------------------------------------------------
# Campaign provenance (SCENE-4 PR 4.3, D2): the record owns it, the adapter carries it
# ----------------------------------------------------------------------------
_PROVENANCE = dict(topology_role="canonical_path", program_fingerprint="p" * 64,
                   program_step="step-1", path_fingerprint="f" * 64)


def test_campaign_provenance_reaches_a_delivered_fits_observation(tmp_path):
    """The delivered-fit construction site: all four fields verbatim off the record, which is the
    only writer of campaign identity — the scene never derives one."""
    ws = _ws(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")])])
    _write_measurement(tmp_path, _measurement(scan_ref="tavi:1:det", scan_id="tavi:1:det",
                                              scan_number=1, **_PROVENANCE))
    obs, = scene_observations(Workspace.open(tmp_path))
    assert (obs.topology_role, obs.program_fingerprint, obs.program_step,
            obs.path_fingerprint) == ("canonical_path", "p" * 64, "step-1", "f" * 64)


def test_campaign_provenance_reaches_a_synthetic_observation(tmp_path):
    """The fitless construction site — a driver-initiated probe whose fit produced nothing still
    carries its role, or the builder could not withhold it from path derivation."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    stamp_ws(tmp_path, rows=[], columns=COLUMNS)
    _write_measurement(tmp_path, _measurement(**{**_PROVENANCE, "topology_role": "context_probe"}))
    obs, = scene_observations(Workspace.open(tmp_path))
    assert obs.topology_role == "context_probe"
    assert (obs.program_fingerprint, obs.program_step, obs.path_fingerprint) == (
        "p" * 64, "step-1", "f" * 64)


def test_a_record_with_no_campaign_provenance_reads_none_everywhere(tmp_path):
    """A manual or pre-4.3 scan (H1: absent field reads None, no shim) — the provenance-less lane
    the geometric path fallback governs."""
    obs, = scene_observations(_ws(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")])]))
    assert (obs.topology_role, obs.program_fingerprint, obs.program_step,
            obs.path_fingerprint) == (None, None, None, None)
