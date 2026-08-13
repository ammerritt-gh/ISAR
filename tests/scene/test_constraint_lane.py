"""SCENE-4 PR 4.0 (#21, Variant A): the constraint-origin lane at scene intake.

The WINNING comparison's constraint provenance decides scoring eligibility — actually the ARENA
aggregate over every USABLE candidate (see ``isar.scene.build._arena_constraint_origin``): a
declared/prespecified origin scores, a data-derived transported origin is experiment-map-only
with a recorded reason, and a missing or unrecognized origin refuses the whole build rather than
guessing a lane. One fixture per ``CONSTRAINT_ORIGINS`` member, plus the tri-state ``None`` and an
unrecognized string.
"""
import pytest

from isar.scene.build import build_scene
from isar.schema.statistics import CONSTRAINT_ORIGINS

from .test_build import cand, mk_obs

SCORING_ORIGINS = ("none", "user_declared", "prespecified_scientific", "catalogue_variant")
TRANSPORTED_ORIGINS = ("prior_predictive", "data_dependent_rescue", "post_selection", "mixed")

# The plan's lane map is exhaustive over the schema's enum; a stale test list would silently stop
# covering a new member.
assert set(SCORING_ORIGINS) | set(TRANSPORTED_ORIGINS) == CONSTRAINT_ORIGINS


@pytest.mark.parametrize("origin", SCORING_ORIGINS)
def test_a_scoring_origin_scores_normally(origin):
    obs = mk_obs("e:1:d", [cand("A", -10.0, constraint_origin=origin)])
    rev = build_scene([obs])
    assert rev.payload["non_scoring"] == []
    assert rev.payload["clusters"][0]["scan_refs"] == ["e:1:d"]


@pytest.mark.parametrize("origin", TRANSPORTED_ORIGINS)
def test_a_transported_origin_is_non_scoring_with_the_recorded_reason(origin):
    obs = mk_obs("e:1:d", [cand("A", -10.0, constraint_origin=origin)])
    rev = build_scene([obs])
    assert rev.payload["clusters"] == []
    assert rev.payload["non_scoring"] == [
        {"scan_ref": "e:1:d", "reason": "transported_conditioning"}]
    # Finite lnZ alone does not buy scoring — the whole point of the #21 ruling.
    assert obs.candidates[0].log_evidence is not None
    import math
    assert math.isfinite(obs.candidates[0].log_evidence)


def test_a_missing_semantics_record_refuses_the_build():
    obs = mk_obs("e:1:d", [cand("A", -10.0, constraint_origin=None)])
    with pytest.raises(ValueError, match="no constraint_origin"):
        build_scene([obs])


def test_an_unrecognized_origin_refuses_the_build():
    obs = mk_obs("e:1:d", [cand("A", -10.0, constraint_origin="something_new")])
    with pytest.raises(ValueError, match="not a member"):
        build_scene([obs])


def test_only_a_usable_candidates_origin_can_taint_the_arena():
    """An excluded/no-evidence candidate can never be summed into ``ln W(S)``, so its provenance
    carries no exactly-once risk — only a USABLE candidate's origin matters."""
    obs = mk_obs("e:1:d", [
        cand("A", -10.0, constraint_origin="none"),
        cand("B", None, constraint_origin="prior_predictive")])
    rev = build_scene([obs])
    assert rev.payload["non_scoring"] == []
    assert rev.payload["clusters"][0]["scan_refs"] == ["e:1:d"]


def test_one_transported_usable_candidate_taints_the_whole_arena():
    """The exactly-once risk is per ARENA: this arena enumerates every surviving candidate as an
    alternative, not only its named winner, so any usable candidate's transported provenance could
    be the number an alternative sums."""
    obs = mk_obs("e:1:d", [
        cand("A", -10.0, constraint_origin="none"),
        cand("B", -11.0, constraint_origin="prior_predictive")])
    rev = build_scene([obs])
    assert rev.payload["non_scoring"] == [
        {"scan_ref": "e:1:d", "reason": "transported_conditioning"}]


def test_two_different_but_individually_safe_origins_still_score():
    """Two candidates cleanly classified under DIFFERENT scoring-safe origins are not a mixed
    anything — they are two independent numbers, unlike one fit's own several constraint terms
    that jointly shape a single lnZ. Collapsing this heterogeneity to ``"mixed"`` would non-score
    an arena no candidate of which is actually tainted."""
    obs = mk_obs("e:1:d", [
        cand("A", -10.0, constraint_origin="none"),
        cand("B", -11.0, constraint_origin="catalogue_variant")])
    rev = build_scene([obs])
    assert rev.payload["non_scoring"] == []
    assert rev.payload["clusters"][0]["scan_refs"] == ["e:1:d"]
