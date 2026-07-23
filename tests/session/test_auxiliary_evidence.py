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

from isar.session.driver import _tracked_authorities, _tracked_authority_signature
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
              maximum_bridge_steps=2)
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
