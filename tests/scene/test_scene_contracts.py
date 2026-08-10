"""The four branch-tracker contracts, restated as SCENE-level specifications (SCENE-3 PR 3c).

The tracker (``isar/session/evidence_update.py``, pinned by ``tests/session/test_branch_tracking.py``)
earned four scientific rules the hard way, on real campaigns. They are rules about *physics*, not
about that implementation, so the scene layer has to honour them too — and it reaches them by a
completely different route: no target, no anchor, no selected candidate, no per-point track state,
just a posterior over structural readings.

So these are PORTS OF THE SPECIFICATION, deliberately not oracle comparisons. Nothing here
imports the tracker or asserts equality against a tracker field; each test names the tracker
source lines it ports and states the rule in one sentence, then checks that the scene's own
vocabulary — visibility states, presence terms, marginals — says the same thing. A scene that
matched the tracker field-for-field would be a second implementation of it, which is exactly what
SCENE replaces.

Observations are hand-built, like the rest of ``tests/scene``: no fitter, no workspace.
"""
from dataclasses import replace

import pytest

from isar.scene.build import build_scene
from isar.scene.factors import PRESENCE_MATCH_NATS, PRESENCE_MISS_NATS
from isar.scene.model import SampleKnowledge, Visibility, observation_fingerprint

from .test_build import cand, mk_obs

# A declared dispersion support. Without one the scene refuses to charge a neighbour that holds
# OTHER peaks (``reachability_unknown``), so every test whose point is a charged absence beside a
# non-empty neighbour has to declare it — see ``test_factors_live``'s SLOPE.
SLOPE = 2.0


def q(h):
    return (h, 0.0, 0.0)


def _with(obs, **fields):
    """``obs`` with fields replaced and its fingerprint restamped — a different observation."""
    changed = replace(obs, **fields)
    return replace(changed, fingerprint=observation_fingerprint(replace(changed, fingerprint="")))


def _reports(rev):
    return [r for cluster in rev.payload["clusters"] for r in cluster["visibility"]]


def _endpoints(rev):
    return [r for r in _reports(rev) if r["visibility"] == Visibility.STRUCTURAL_ENDPOINT]


def _cluster_with(rev, scan_ref):
    return next(c for c in rev.payload["clusters"] if scan_ref in c["scan_refs"])


def _presence_by_label(rev, scan_ref):
    """``{label chosen at scan_ref: that alternative's presence term}``.

    Every fixture below has one linkage reading per assignment and one candidate per other arena,
    so the mapping is total; the assertion guards that, since a collision would silently compare
    the wrong two alternatives.
    """
    cluster = _cluster_with(rev, scan_ref)
    out = {}
    for alt in cluster["alternatives"]:
        label = dict(tuple(pair) for pair in alt["assignment"])[scan_ref]
        assert label not in out, f"two alternatives choose {label!r} at {scan_ref}"
        out[label] = alt["factor_terms"]["presence"]
    return out


def _descending_track(centre_h, *, step=0.1, ref=("e:1:d", "e:2:d", "e:3:d")):
    """A two-node descending track running into an elastic-bearing arena at ``centre_h``.

    The template is ``test_factors_live._endpoint_scene``: the mode at ``centre_h + 2·step`` links
    to the one at ``centre_h + step`` (0.9 meV apart, inside mk_obs's 1.0 meV resolution) and the
    track descends toward the elastic arena. At 1.5 meV the terminal mode sits OUTSIDE the elastic
    width, so its absence at the centre is a genuine searched-and-empty one until a declaration
    reinterprets it.
    """
    centre, lower, upper = ref
    return [mk_obs(centre, [cand("elastic+bg", -10.0, n_modes=0, omegas=(), extra=("elastic",))],
                   q=q(centre_h)),
            mk_obs(lower, [cand("stokes+bg", -10.0, omegas=(1.5,))], q=q(centre_h + step)),
            mk_obs(upper, [cand("stokes+bg", -10.0, omegas=(2.4,))], q=q(centre_h + 2 * step))]


# ----------------------------------------------------------------------------
# 1. inferred_unresolved — a structural endpoint claims no measured precision
# ----------------------------------------------------------------------------
def test_a_structural_endpoint_claims_no_energy_of_its_own():
    """Ports ``tests/session/test_branch_tracking.py`` lines 174-176 and 447-477
    (``test_inferred_gamma_endpoint_never_delivers_or_seeds_dispersion_prior``): the tracker's
    reduced-q origin is ``inferred_unresolved`` — its ω₀, Γ and branch id are cleared, it is
    ineligible for delivery, and it seeds nothing into the dispersion prior.

    **Specification:** an endpoint is an INFERENCE about where a branch goes, never a measurement
    of it, so the scene may attribute no fitted energy at the endpoint arena — no phantom
    zero-energy mode inside the elastic width, and no ω or Γ number anywhere in the annotation
    that carries the reading.

    What this does NOT say is that a low-energy mode may not be FITTED at a fold arena. A
    candidate that places one is an ordinary alternative, enumerated and priced on its own local
    evidence and its own absence evidence — the rule is only that the endpoint reading never
    manufactures one.
    """
    obs = _descending_track(2.0)
    knowing = build_scene(obs, sample=SampleKnowledge(zone_period=2.0))

    endpoint, = _endpoints(knowing)
    assert (endpoint["scan_ref"], endpoint["neighbour"]) == ("e:2:d", "e:1:d")

    # (a) The reading places no mode at the endpoint arena: the scene's whole belief there is
    # "zero modes", and it reports nothing anchored at that arena, because there is no mode of
    # its own for the fold to be a neighbour of.
    cluster = _cluster_with(knowing, "e:1:d")
    assert cluster["marginals"]["n_modes"]["e:1:d"] == {"0": 1.0}
    assert not [r for r in _reports(knowing) if r["scan_ref"] == "e:1:d"]

    # (b) The annotation carries the TERMINAL MODE's own measured energy, at its own arena, and no
    # number attributed to the endpoint: no width, no Γ, nothing at the fold.
    assert endpoint["omega"] == 1.5
    assert not [key for key in endpoint if "gamma" in key or "fwhm" in key]

    # (c) And the reading is licensed by the declaration alone: blind to it, the same scene reads
    # the same absence as ordinary evidence against the mode.
    blind = build_scene(obs)
    assert not _endpoints(blind)
    assert Visibility.SEARCHED_NO_SIGNAL in {r["visibility"] for r in _reports(blind)}

    # (d) The forbidden phantom, made a live competitor rather than assumed away: a candidate at
    # the fold arena that DOES place a mode inside the elastic width is enumerated against the
    # elastic-only one. The termination is still granted — it comes from the elastic response,
    # which both candidates carry — and it rescues nothing: the phantom's own unsupported mode is
    # charged its miss at its own neighbour, so it lands exactly one miss below and takes strictly
    # less posterior mass. An endpoint that could pay for the mode it terminates into would be the
    # scene laundering an inference into a measurement.
    fold = mk_obs("e:1:d", [cand("elastic+bg", -10.0, n_modes=0, omegas=(), extra=("elastic",)),
                            cand("elastic+low", -10.0, omegas=(0.3,), extra=("elastic",))],
                  q=q(2.0))
    # One searched, entirely empty arena on the FAR side of the fold, so the phantom mode has a
    # neighbour that can price it. Without it the phantom's only neighbour is the track's own
    # arena, whose other peaks make its silence uninformative with no declared slope
    # (``reachability_unknown``) — the two readings would then tie for a reason that has nothing
    # to do with the endpoint.
    beyond = mk_obs("e:0:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=q(1.9))
    contested = build_scene([beyond, fold, *obs[1:]], sample=SampleKnowledge(zone_period=2.0))
    assert [(r["scan_ref"], r["neighbour"]) for r in _endpoints(contested)] == [("e:2:d", "e:1:d")]
    presence = _presence_by_label(contested, "e:1:d")
    # The equality says the phantom's ONLY extra cost is its own unlinked mode's miss. Under
    # elastic+low the (e:2:d -> e:1:d) row is demoted to blocked_unresolved by
    # reachability_unknown (the fold now holds a mode, no slope declared) — a blocked row
    # prices 0.0 like an endpoint row, so this equality does not distinguish the two; the
    # endpoint's own contribution is pinned by the refiner-drop contrast in the (a) arm.
    assert presence["elastic+low"] == presence["elastic+bg"] + PRESENCE_MISS_NATS
    mass = _cluster_with(contested, "e:1:d")["marginals"]["label"]["e:1:d"]
    assert mass["elastic+bg"] > mass["elastic+low"]


# ----------------------------------------------------------------------------
# 2. missing — absence is meaningful only after a relevant search
# ----------------------------------------------------------------------------
def test_an_absence_is_evidence_only_where_the_neighbour_looked():
    """Ports ``tests/session/test_branch_tracking.py`` lines 303-318
    (``test_missing_q_stops_confirmed_segments_and_allows_only_one_bridge``): a q with no
    candidates is ``missing`` — the tracker charges the gap only because that q was actually
    searched and came back empty.

    **Specification:** an identical predicted continuation costs exactly one miss when the
    neighbour's footprint covers it and NOTHING when it does not, so the two scenes differ by the
    miss term and by nothing else. A coverage gap is not a measurement, and pricing it would let a
    campaign that spent its neutrons elsewhere come back as evidence against what it never looked
    for.
    """
    # The two-mode reading leads on its own local evidence in both scenes, so the payload's
    # visibility annotation — which is the leader's — is the one that carries the disputed band.
    disputed = mk_obs("e:1:d", [cand("one", -10.0, omegas=(2.0,)),
                                cand("two", -8.0, n_modes=2, omegas=(2.0, 8.0))], q=q(1.0))
    searching = mk_obs("e:2:d", [cand("stokes+bg", -10.0, omegas=(2.0,))], q=q(1.1))
    # The same neighbour, same exposure and resolution, whose kept axis never reaches 8 meV.
    blind = _with(searching, energy_window=(-3.0, 3.0), energy_bands=((-3.0, 3.0),))
    sample = SampleKnowledge(dispersion_slope_max=SLOPE)

    searched = build_scene([disputed, searching], sample=sample)
    censored = build_scene([disputed, blind], sample=sample)

    # The 8 meV band is the ONLY thing that differs, and it differs in exactly the intended way.
    def state_at_8(rev):
        row, = [r for r in _reports(rev) if r["component_id"] == "e:1:d#two#1"]
        return row["visibility"], row["reason"]

    assert state_at_8(searched) == (Visibility.SEARCHED_NO_SIGNAL, None)
    assert state_at_8(censored) == (Visibility.CENSORED_OUTSIDE_FOOTPRINT, None)

    # ... so the two readings of the disputed arena separate by one miss where the neighbour
    # looked, and not at all where it did not.
    hit = build_scene([disputed, searching], sample=sample)
    assert _presence_by_label(hit, "e:1:d") == {
        "one": 2 * PRESENCE_MATCH_NATS,
        "two": 2 * PRESENCE_MATCH_NATS + PRESENCE_MISS_NATS}
    gap = _presence_by_label(censored, "e:1:d")
    assert gap["two"] == gap["one"]


# ----------------------------------------------------------------------------
# 3. blocked — what more counting can and cannot repair
# ----------------------------------------------------------------------------
def _blocked_scene(omega, *, exposure=1.0, neighbour_gauss=None):
    """A mode at ``omega`` beside an elastic-bearing neighbour that holds no mode of its own."""
    seen = mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(omega,))], q=q(1.0))
    neighbour = mk_obs("e:2:d",
                       [cand("elastic+bg", -10.0, n_modes=0, omegas=(), extra=("elastic",))],
                       q=q(1.1), exposure=exposure)
    if neighbour_gauss is not None:
        neighbour = _with(neighbour, gauss_fwhm=neighbour_gauss)
    return [seen, neighbour]


def _row_for(rev, component_id):
    row, = [r for r in _reports(rev) if r["component_id"] == component_id]
    return row


def test_more_counts_at_the_same_q_cannot_repair_a_resolution_blockage():
    """Ports ``tests/session/test_branch_tracking.py`` lines 303-318 (the ``blocked`` half of
    ``test_missing_q_stops_confirmed_segments_and_allows_only_one_bridge``): once connectivity is
    broken, the blocked points stay blocked and never enter the connected prior.

    **Specification:** a mode hidden under the elastic response is unresolved for a RESOLUTION
    reason, so counting longer at the same q changes neither its state nor the posterior — only a
    narrower resolution does. This is the half an operator most needs stated, because "count
    longer" is the reflex the state has to argue against.
    """
    blocked = build_scene(_blocked_scene(0.5))
    row = _row_for(blocked, "e:1:d#stokes+bg#0")
    # Blocked by the elastic line itself, not by the detectability gate — no demotion reason.
    assert (row["visibility"], row["reason"]) == (Visibility.BLOCKED_UNRESOLVED, None)
    assert _presence_by_label(blocked, "e:1:d") == {"stokes+bg": 0.0}

    # A hundredfold deeper look at the same q: the elastic response is still on top of the band.
    deeper = build_scene(_blocked_scene(0.5, exposure=100.0))
    assert _row_for(deeper, "e:1:d#stokes+bg#0")["visibility"] == Visibility.BLOCKED_UNRESOLVED
    assert _presence_by_label(deeper, "e:1:d") == _presence_by_label(blocked, "e:1:d")
    assert [alt["posterior"] for alt in _cluster_with(deeper, "e:1:d")["alternatives"]] == \
        [alt["posterior"] for alt in _cluster_with(blocked, "e:1:d")["alternatives"]]

    # Better resolution is what changes it: at 0.2 meV FWHM the 0.5 meV band is clear of the
    # elastic line, the neighbour's silence becomes evidence, and the miss is charged.
    resolved = build_scene(_blocked_scene(0.5, neighbour_gauss=0.2))
    assert _row_for(resolved, "e:1:d#stokes+bg#0")["visibility"] == Visibility.SEARCHED_NO_SIGNAL
    assert _presence_by_label(resolved, "e:1:d") == {"stokes+bg": PRESENCE_MISS_NATS}


def test_more_counts_do_repair_a_detection_power_blockage():
    """The other honest half of the same rule (review finding 2's detectability gate, which the
    tracker predates): a shallow look is blocked for a COUNTING reason, and counting is exactly
    what repairs it.

    Stated as its own case so the ported "more counts cannot repair" spec above cannot be read as
    "exposure never matters" — the two blockages have opposite remedies, and an operator choosing
    between more time and finer resolution needs the difference.
    """
    seen = mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(5.0,))], q=q(1.0))
    empty = mk_obs("e:2:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=q(1.1), exposure=0.05)

    glance = build_scene([seen, empty])
    row = _row_for(glance, "e:1:d#stokes+bg#0")
    assert (row["visibility"], row["reason"]) == (Visibility.BLOCKED_UNRESOLVED,
                                                  "insufficient_detection_power")
    assert _presence_by_label(glance, "e:1:d") == {"stokes+bg": 0.0}

    counted = build_scene([seen, _with(empty, effective_exposure=1.0)])
    assert _row_for(counted, "e:1:d#stokes+bg#0")["visibility"] == Visibility.SEARCHED_NO_SIGNAL
    assert _presence_by_label(counted, "e:1:d") == {"stokes+bg": PRESENCE_MISS_NATS}

    # ... and the resolution-blocked lane is unmoved by the same repair, which is the contrast.
    assert _row_for(build_scene(_blocked_scene(0.5, exposure=100.0)),
                    "e:1:d#stokes+bg#0")["visibility"] == Visibility.BLOCKED_UNRESOLVED


# ----------------------------------------------------------------------------
# 4. O44 — the zone centre comes from the declared geometry, never from the data
# ----------------------------------------------------------------------------
def test_the_zone_centre_is_the_declared_fold_and_not_the_lowest_q_measured():
    """Ports ``tests/session/test_branch_tracking.py`` lines 74 and 381-407 (O44): the reduced-q
    origin is computed from the crystallographic period, so a path that never visits a fold has no
    origin to anchor on and the persisted anchor does not drift onto a newly-measured nearer q.

    **Specification:** the endpoint fires where ``round(q/period)·period`` says the centre is,
    anchored at ``q = 0``. Were the origin taken from the data — the lowest measured q, the first
    scan, the arena that happened to be selected — the geometry would confirm whatever the
    campaign happened to visit.
    """
    period = SampleKnowledge(zone_period=2.0)
    at_a_fold = build_scene(_descending_track(2.0), sample=period)
    assert [r["neighbour"] for r in _endpoints(at_a_fold)] == ["e:1:d"]

    # The same scene shape a third of a period away. Its lowest measured q would BE the origin
    # under any data-anchored rule, and there is no endpoint at all.
    off_fold = build_scene(_descending_track(1.4), sample=period)
    assert not _endpoints(off_fold)
    assert Visibility.SEARCHED_NO_SIGNAL in {r["visibility"] for r in _reports(off_fold)}

    # It is the DECLARED period that decides: the same arenas under a period of 0.5 put a fold at
    # 1.5, and the reading is granted there.
    assert [r["neighbour"] for r in
            _endpoints(build_scene(_descending_track(1.5), sample=SampleKnowledge(zone_period=0.5)))
            ] == ["e:1:d"]


@pytest.mark.parametrize("refs", [("e:1:d", "e:2:d", "e:3:d"),      # sorted along the path
                                  ("z:9:d", "m:5:d", "a:1:d")])    # sorted against it
def test_the_endpoint_survives_relabeling_and_reordering(refs):
    """The invariance half of O44: no measured or selected origin participates, so neither the
    scan labels, their sort order, nor which arena the input happens to list first can move the
    fold. The second parametrization deliberately sorts the arenas AGAINST the path direction, so
    the cluster id and the enumeration order both change while the physics may not.
    """
    obs = _descending_track(2.0, ref=refs)
    sample = SampleKnowledge(zone_period=2.0)
    rev = build_scene(obs, sample=sample)

    endpoint, = _endpoints(rev)
    # The endpoint is the arena AT the fold, identified by its position and not by its name.
    footprints = {f["scan_ref"]: f for f in rev.payload["footprints"]}
    assert footprints[endpoint["neighbour"]]["axis_coordinate"] == 2.0

    # Input order is not content: the same arenas in any order are the same revision, bit for bit.
    assert build_scene(list(reversed(obs)), sample=sample).digest == rev.digest
