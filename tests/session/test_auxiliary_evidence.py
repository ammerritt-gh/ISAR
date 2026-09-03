"""The evidence-sink contract and on-path auxiliary-probe ingestion (PR2.6).

A focused rival scan asks a different question than a target measurement: it exists to decide *which
mode* a candidate is, not to deliver a better ``omega0`` at a point the campaign already owns. Pooling
it into the canonical master would mix a deliberately off-target window into the exposure that certifies
the target, and dropping it on the floor would waste the beam time it cost.

``evidence_sink`` names the destination, and this module pins what each destination actually does:

* ``canonical_master`` — the ordinary path, pooled, certification-eligible. Unchanged.
* ``auxiliary_track_node`` — fit independently, never pooled, never certification-eligible, and merged
  into the tracker's evidence at its path index as a provenance-tagged *candidate*. It can change the
  identity assigned at that point; it can never become the point's ``authority`` and never supplies
  ``omega0``.

The rule that keeps that honest: an auxiliary probe merges only where a pooled master authority already
exists. At an index with no master it stays out, because admitting it would make a non-pooled,
non-certification-eligible fit the *sole* identity evidence at that point — exactly what
:func:`_tracked_authorities` exists to prevent.
"""
from __future__ import annotations

from isar.session.evidence_update import _tracked_authorities, _tracked_authority_signature
from isar.session.state import ScanEntry, SessionState

PATH = [[2.8, 0.0, 0.0], [2.9, 0.0, 0.0], [3.0, 0.0, 0.0]]


def _outcome(q, energy, **kw):
    outcome = {"q": list(q), "modes": [{"omega0": energy, "omega0_err": 0.05, "gamma": 0.3}],
               "omega0": energy, "gauss_fwhm": 0.4}
    outcome.update(kw)
    return outcome


def _session(*rows) -> SessionState:
    session = SessionState()
    for index, (scan_id, outcome) in enumerate(rows):
        session.scans[scan_id] = ScanEntry(scan_id=scan_id, scan_number=100 + index,
                                           scan_type="scan", status="fitted",
                                           detectors={"det": outcome})
    return session


def _master(q, energy, **kw):
    return _outcome(q, energy, pooled=True, n_members=3, effective_exposure=1.0, **kw)


def _probe(q, energy, **kw):
    return _outcome(q, energy, evidence_sink="auxiliary_track_node", **kw)


# -- the sink decides what the evidence may become ---------------------------------------------------

def test_an_auxiliary_probe_adds_a_candidate_without_becoming_the_authority():
    session = _session(("exp:1:det", _master(PATH[1], 5.754)),
                       ("exp:2:det", _probe(PATH[1], 7.115)))
    observations, _refs = _tracked_authorities(session, PATH)

    assert observations[1]["authority"] == "exp:1:det:det", "the pooled master stays the authority"
    energies = sorted(round(float(c["energy_meV"]), 3) for c in observations[1]["candidates"])
    assert energies == [5.754, 7.115], "the rival the probe was fired to observe must be visible"


def test_the_auxiliary_candidate_is_tagged_and_the_canonical_ones_are_not():
    """Absence of the tag means canonical. Tagging the pooled candidates too would have been tidier
    and would have changed the authority signature of every existing campaign for no new evidence."""
    session = _session(("exp:1:det", _master(PATH[1], 5.754)),
                       ("exp:2:det", _probe(PATH[1], 7.115)))
    observations, _refs = _tracked_authorities(session, PATH)
    by_energy = {round(float(c["energy_meV"]), 3): c for c in observations[1]["candidates"]}

    assert by_energy[7.115].get("evidence_source") == "auxiliary_probe"
    assert "evidence_source" not in by_energy[5.754]


def test_auxiliary_candidate_carries_its_persisted_authorized_search_window():
    session = _session(
        ("exp:1:det", _master(PATH[1], 5.754)),
        ("exp:2:det", _probe(
            PATH[1], 7.115, search_energy_lo=7.0, search_energy_hi=7.3)),
    )
    observations, _refs = _tracked_authorities(session, PATH)
    auxiliary = next(candidate for candidate in observations[1]["candidates"]
                     if candidate.get("evidence_source") == "auxiliary_probe")

    assert auxiliary["authorized_energy_window_meV"] == [7.0, 7.3]


def test_an_auxiliary_probe_is_never_pooled_into_the_master():
    """The probe's own outcome must not be marked pooled, or the certification machinery would count
    a deliberately off-target window as target exposure."""
    session = _session(("exp:1:det", _master(PATH[1], 5.754)),
                       ("exp:2:det", _probe(PATH[1], 7.115)))
    assert session.scans["exp:2:det"].detectors["det"].get("pooled") is not True


def test_an_auxiliary_probe_alone_does_not_fill_an_unmeasured_index():
    """No master at this index: the probe stays out rather than becoming the sole identity evidence."""
    session = _session(("exp:2:det", _probe(PATH[2], 7.115)))
    observations, _refs = _tracked_authorities(session, PATH)

    assert observations[2]["authority"] is None
    assert observations[2]["candidates"] == []


def test_an_off_path_probe_reaches_no_observation_at_all():
    session = _session(("exp:1:det", _master(PATH[1], 5.754)),
                       ("exp:2:det", _probe([2.95, 0.0, 0.0], 7.115)))
    observations, _refs = _tracked_authorities(session, PATH)

    assert [len(o["candidates"]) for o in observations] == [0, 1, 0]


def test_an_unrecognised_sink_is_treated_as_canonical_and_not_merged():
    """A sink value this version does not know must not silently gain auxiliary privileges."""
    session = _session(("exp:1:det", _master(PATH[1], 5.754)),
                       ("exp:2:det", _outcome(PATH[1], 7.115, evidence_sink="from_the_future")))
    observations, _refs = _tracked_authorities(session, PATH)

    assert len(observations[1]["candidates"]) == 1


# -- the probe is real evidence: it must invalidate the cached track ---------------------------------

def test_a_new_probe_changes_the_authority_signature():
    """If the signature ignored auxiliary candidates the cached track would survive the very evidence
    that was acquired to revise it, and the probe would have cost beam time for nothing."""
    kw = dict(target_branch="acoustic", period=1.0, hypotheses=[], entropy_stop=0.35,
              )
    before, _ = _tracked_authorities(_session(("exp:1:det", _master(PATH[1], 5.754))), PATH)
    after, _ = _tracked_authorities(_session(("exp:1:det", _master(PATH[1], 5.754)),
                                             ("exp:2:det", _probe(PATH[1], 7.115))), PATH)

    assert _tracked_authority_signature(before, **kw) != _tracked_authority_signature(after, **kw)


def test_a_campaign_with_no_probes_carries_no_trace_of_the_auxiliary_channel():
    """The identity gate for every existing campaign, stated where it can be checked rather than as a
    pinned hash: a track that never used the channel must produce observations byte-identical to the
    ones it produced before the channel existed. Anything the signature hashes lives in these dicts,
    so no new key here means no perturbed signature there."""
    observations, _ = _tracked_authorities(_session(("exp:1:det", _master(PATH[1], 5.754))), PATH)

    assert set(observations[1]) == {"candidates", "authority", "gauss_fwhm"}
    for candidate in observations[1]["candidates"]:
        assert "evidence_source" not in candidate


# -- the delivery boundary: identity evidence, never a certifiable number ----------------------------

def _tracked_card(path):
    """An acoustic tracked campaign whose declared branch runs up toward the rival energy, so the
    auxiliary candidate is the one the tracker prefers for the target."""
    from types import SimpleNamespace

    from isar.schema.card import CardState

    state = CardState(
        campaign_target={"branch": "acoustic", "label": "acoustic", "selector": "tracked_branch",
                         "target_energy_meV": None, "energy_search_meV": [-2.0, 7.0], "path": path},
        hypothesis_snapshot={
            "hypotheses": [
                {"name": "optic", "zc_energy": {"mean": 20.0, "sigma": 1.0},
                 "slope_zc": {"mean": 0.0, "sigma": 1.0}},
                # Predicts ~7 meV at the probed q: the auxiliary candidate, not the canonical one.
                {"name": "acoustic", "zc_energy": {"mean": 7.0, "sigma": 0.3},
                 "slope_zc": {"mean": 0.0, "sigma": 0.3}},
            ],
            "target_hypothesis": "acoustic",
            "steering": {"assignment_entropy_stop_nats": 2.0}})
    return SimpleNamespace(state=state, policy=SimpleNamespace(
        dispersion={"zone_period": 2.0}, collation={"t_round": 5.0}))


def _pooled_master_outcome(q, energy):
    return {"detector_id": "det", "q": list(q), "T": 200.0, "sample": "sample",
            "omega0": energy, "omega0_err": 0.05, "gamma": 0.3,
            "interval": [energy - 0.1, energy + 0.1], "interval_reliable": True,
            "pooled": True, "n_members": 3, "pool_consistent": True, "effective_exposure": 1.0,
            "fit_kind": "unconstrained", "verdict": "resolved", "modality": "fold",
            "gauss_fwhm": 0.4, "model_robust": True,
            "model_interval": [energy - 0.08, energy, energy + 0.08],
            "modes": [{"omega0": energy, "omega0_err": 0.05, "gamma": 0.3, "amplitude": 10.0}]}


def _auxiliary_probe_outcome(q, energy):
    return {"detector_id": "det", "q": list(q), "T": 200.0, "sample": "sample",
            "omega0": energy, "omega0_err": 0.05, "gamma": 0.3,
            "evidence_sink": "auxiliary_track_node", "gauss_fwhm": 0.4,
            "verdict": "resolved", "modality": "fold",
            "modes": [{"omega0": energy, "omega0_err": 0.05, "gamma": 0.3, "amplitude": 8.0}]}


def _tracked_session_with_probe():
    """The reviewer's reproduction: the auxiliary 7 meV candidate wins the target selection at the
    middle q, where the canonical master only ever saw 4 meV."""
    from isar.session.evidence_update import _apply_tracked_branch

    session = SessionState()
    for index, q in enumerate(PATH):
        session.put(ScanEntry(scan_id=f"master:q{index}", scan_number=9_000_000 + index,
                              scan_type="energy", status="fitted",
                              detectors={"det": _pooled_master_outcome(q, 7.0 if index != 1 else 4.0)}))
    session.put(ScanEntry(scan_id="probe:q1", scan_number=9_100_001, scan_type="energy",
                          status="fitted",
                          detectors={"det": _auxiliary_probe_outcome(PATH[1], 7.0)}))
    card = _tracked_card([list(q) for q in PATH])
    track = _apply_tracked_branch(session, card, t_round=5.0)
    return session, card, track


def test_an_auxiliary_candidate_may_win_the_identity_but_never_delivers_omega0():
    """The P0. The probe's candidates are merged into the canonical observation and the tracker's
    selection was copied straight onto the pooled master, so a scan that is never pooled and never
    certification-eligible supplied the certified omega0 and its intervals — a hybrid row that claims
    exposure the target window never spent, and the exact thing the auxiliary sink exists to prevent."""
    session, _card, track = _tracked_session_with_probe()
    master = session.scans["master:q1"].detectors["det"]
    assignment = track["assignments"][1]

    selected = assignment["candidates"][assignment["selected_candidate_index"]]
    assert selected.get("evidence_source") == "auxiliary_probe", "fixture must reproduce the case"

    assert master["omega0"] is None, "the auxiliary energy must not become the master's delivery"
    assert master["interval"] is None and master["omega0_err"] is None
    assert master["interval_reliable"] is False
    assert master["target_delivery_eligible"] is False


def test_the_identity_conclusion_survives_the_withheld_delivery():
    """Withholding the number must not throw away the answer. The point *is* identified — that is what
    the probe bought — and acquisition needs to know it, or it cannot tell a settled label with no
    canonical measurement from an unsettled one that needs more counting."""
    session, _card, track = _tracked_session_with_probe()
    master = session.scans["master:q1"].detectors["det"]

    assert track["assignments"][1]["track_state"] == "confirmed"
    assert master["identity_branch_id"] == "acoustic", "the tracker's conclusion is retained"
    assert master["target_probability"] is not None
    assert master["branch_id"] is None, (
        "branch_id means 'this master supplies the target delivery'; it must not claim that here")


def test_a_withheld_delivery_is_not_certifiable_and_seeds_no_prior():
    """The two leaks a scalar clear does not close. ``_master_obs_from_row`` takes omega0 from
    ``model_interval_med`` whenever the row is model-robust, so a cleared scalar still certified; and
    ``_rebuild_tracked_prior`` reads the tracker's selected candidates directly, so the auxiliary
    energy could enter the running dispersion prior as a fold from a pooled master."""
    from isar.drive.acquisition.calibration import Calibration
    from isar.drive.loop import _master_obs_from_row

    session, card, _track = _tracked_session_with_probe()
    master = session.scans["master:q1"].detectors["det"]

    assert master.get("model_robust") is True, "withholding must not destroy fitted model evidence"
    assert master.get("model_interval") == [3.92, 4.0, 4.08]
    row = {"h": PATH[1][0], "k": 0.0, "l": 0.0, "omega0": master["omega0"],
           "model_interval_lo": master["model_interval"][0],
           "model_interval_med": master["model_interval"][1],
           "model_interval_hi": master["model_interval"][2],
           "model_robust": master.get("model_robust"), "disposition": "resolved",
           "target_delivery_eligible": False}
    obs = _master_obs_from_row(row, Calibration(inflation=1.0))
    assert obs["certifiable"] is False and obs["usable"] is False and obs["omega0"] is None

    prior_qs = [point.get("q") for point in (card.state.dispersion_prior or [])]
    assert all(q != list(PATH[1]) for q in prior_qs), (
        "an auxiliary candidate must never enter the running dispersion prior")


def test_a_later_canonical_selection_clears_withholding_and_reuses_model_delivery():
    from isar.drive.acquisition.calibration import Calibration
    from isar.drive.loop import _master_obs_from_row
    from isar.session.evidence_update import _select_mode

    session, _card, track = _tracked_session_with_probe()
    master = session.scans["master:q1"].detectors["det"]
    assignment = track["assignments"][1]
    canonical_index = next(
        index for index, candidate in enumerate(assignment["candidates"])
        if candidate.get("evidence_source") != "auxiliary_probe")
    canonical = assignment["candidates"][canonical_index]

    _select_mode(master, canonical, canonical_index, assignment, track["revision"], "acoustic")

    assert master["target_delivery_eligible"] is True
    assert master["identity_branch_id"] is None
    assert master["delivery_withheld_reason"] is None
    assert master["model_robust"] is True
    assert master["model_interval"] == [3.92, 4.0, 4.08]
    row = {
        "h": PATH[1][0], "k": 0.0, "l": 0.0, "omega0": master["omega0"],
        "model_interval_lo": master["model_interval"][0],
        "model_interval_med": master["model_interval"][1],
        "model_interval_hi": master["model_interval"][2],
        "model_robust": master["model_robust"], "disposition": "resolved",
        "target_delivery_eligible": master["target_delivery_eligible"],
    }
    obs = _master_obs_from_row(row, Calibration(inflation=1.0))
    assert obs["certifiable"] is True


def test_new_ambiguity_withdraws_the_old_auxiliary_identity_but_keeps_delivery_gated():
    from isar.session.evidence_update import _clear_tracked_selection

    session, _card, track = _tracked_session_with_probe()
    master = session.scans["master:q1"].detectors["det"]
    ambiguous = {
        **track["assignments"][1],
        "track_state": "ambiguous",
        "target_probability": 0.5,
        "identity_entropy": 0.7,
        "local_identity_clear": False,
    }

    _clear_tracked_selection(master, ambiguous, track["revision"] + 1)

    assert master["target_delivery_eligible"] is False
    assert master["identity_branch_id"] is None
    assert master["delivery_withheld_reason"] is None
    assert master["omega0"] is None
