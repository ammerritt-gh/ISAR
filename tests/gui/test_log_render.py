"""isar_gui.log_render — one rendered line per operator-facing session-log kind.

The payloads below are trimmed copies of real records from a completed campaign
(``report/session_log.jsonl`` of an O41 authoritative run), so a change in the engine's payload
shape shows up here as a changed sentence rather than as a silently emptier dock.

Pure functions only: no QApplication is needed, and none is created.
"""
from __future__ import annotations

import pytest

from isar_gui.log_render import (AUDIT_KINDS, BUDGET_KIND, BudgetAnnouncer, HIDDEN_KINDS,
                                 LOUD_KINDS, PROFILING_KINDS, budget_status_line, render_event)


def _rendered(kind, payload, ts=1785862676.0):
    event = render_event({"ts": ts, "kind": kind, "payload": payload})
    assert event is not None, f"{kind} must reach the feed"
    return event


# ---- one line per operator kind ----------------------------------------------
def test_propose_names_the_point_the_window_and_the_exposure():
    event = _rendered("propose", {
        "hkl": [2.15, 0.0, 0.0], "e_lo": -2.0, "e_hi": 7.0, "ncount": 10000000,
        "kind": "quicklook", "n_points": 32, "iter": 0,
        "acquisition_id": "d2f7be97454c:00000000",
        "scientific_action_fingerprint": "7e89a9327a0d762b"})
    assert event.text == ("Planning a quicklook scan at H=2.15 — 32 points, E -2.00…7.00 meV, "
                          "10M neutrons  [iter 0]")
    assert event.severity == "info" and not event.loud


def test_feasibility_trim_names_the_dropped_points_and_new_window():
    event = _rendered("feasibility_trim", {
        "old_e_lo": -9.0, "old_e_hi": 9.0, "new_e_lo": -8.65, "new_e_hi": 9.0,
        "dropped_point_count": 1, "iter": 13})
    assert event.text == ("Trimmed 1 unreachable point from the scan window; "
                          "scanning E -8.65…9.00 meV  [iter 13]")
    assert event.severity == "notice"


def test_validate_reports_reachability_and_flags_a_block():
    ok = _rendered("validate", {"would_queue": True, "blockers": [], "requested_points": 32,
                                "feasible_points": 32, "iter": 0})
    assert ok.text == "Instrument check passed — 32 of 32 points reachable  [iter 0]"
    assert ok.severity == "info"

    blocked = _rendered("validate", {"would_queue": False, "requested_points": 32,
                                     "blockers": ["target_core_truncated_after_domain_clip"],
                                     "iter": 4})
    assert blocked.text == ("Instrument check blocked — target core truncated after domain clip"
                            "  [iter 4]")
    assert blocked.severity == "warn"


def test_validate_names_a_blocked_alternative_window_as_info_not_a_warning():
    alternative = _rendered("validate", {
        "would_queue": False, "candidate_role": "target_core", "requested_points": 32,
        "blockers": ["target_core_truncated_after_domain_clip"], "iter": 0})
    assert alternative.text == ("Alternative window (target core) not available — "
                                "target core truncated after domain clip  [iter 0]")
    assert alternative.severity == "info"

    absent_role = _rendered("validate", {"would_queue": False, "requested_points": 32,
                                         "blockers": ["some_reason"], "iter": 1})
    assert absent_role.text.startswith("Instrument check blocked")
    assert absent_role.severity == "warn"

    legacy = _rendered("validate", {"would_queue": False, "candidate_role": "legacy_uniform",
                                    "blockers": ["some_reason"], "iter": 2})
    assert legacy.text.startswith("Instrument check blocked")
    assert legacy.severity == "warn"


def test_run_names_the_job_the_point_and_the_expected_time():
    event = _rendered("run", {
        "job_id": "j-0084", "state": "queued", "hkl": [2.15, 0.0, 0.0], "ncount": 10000000,
        "submitted_ncount": 10000000, "n_points": 32, "iter": 0,
        "eta": {"estimated_seconds": 240.0, "confidence": "high"}})
    assert event.text == ("Submitted scan j-0084 at H=2.15 — 10M neutrons, expected 4 min  "
                          "[iter 0]")


def test_analyze_counts_rows_and_names_the_verdicts():
    event = _rendered("analyze", {"job_id": "j-0084", "n_rows": 1, "iter": 0,
                                  "dispositions": ["resolved"], "errors_tier": "cheap"})
    assert event.text == "Analysed 1 result from scan j-0084 — resolved  [iter 0]"


def test_decide_renders_campaign_start_model_update_and_anything_else():
    start = _rendered("decide", {
        "event": "campaign_start", "action": "campaign_start",
        "source": {"kind": "goal", "sha256": "a" * 64},
        "objective": {"path": [[2.0, 0.0, 0.0]] * 21, "tolerance_meV": 0.5,
                      "budget": {"kind": "neutrons", "amount": 1e10}}})
    assert start.text == "Campaign started from a goal — 21 points, budget 10G neutrons"
    assert start.severity == "notice"

    update = _rendered("decide", {"action": "model_update", "converged": 3, "n_grid": 21,
                                  "n_observations": 12, "iter": 7})
    assert update.text == ("Dispersion model updated — 3 of 21 points certified, "
                           "12 observation(s)  [iter 7]")

    other = _rendered("decide", {"action": "sample_background_frozen"})
    assert other.text == "Decision: sample background frozen"


def test_escalate_fit_reads_as_a_promotion_then_an_outcome():
    promote = _rendered("escalate_fit", {"phase": "promote", "iter": 21, "hkl": [2.75, 0.0, 0.0],
                                         "from_tier": "fast", "to_tier": "full",
                                         "reason": "untrusted_hesse_no_reliable_interval"})
    assert promote.text == ("Refitting more carefully at H=2.75: fast → full — untrusted hesse "
                            "no reliable interval  [iter 21]")
    assert promote.loud                              # never filtered away

    outcome = _rendered("escalate_fit", {"phase": "outcome", "iter": 21, "job_id": "j-0106",
                                         "hkl": [2.75, 0.0, 0.0], "to_tier": "full",
                                         "outcome": "failed"})
    assert outcome.text == "The full refit at H=2.75 failed  [iter 21]"
    assert outcome.severity == "warn" and outcome.loud


def test_exception_leads_with_the_problem_and_clips_a_long_reason():
    event = _rendered("exception", {
        "action": "recenter", "iter": 3, "escalate": False,
        "reason": ("peak observed at deltaE=-1.419 meV, outside/near the window edge; "
                   "re-centering on the Stokes side (1.419 meV) and widening")})
    assert event.text.startswith("Problem: recenter — peak observed at deltaE=-1.419 meV")
    assert event.text.endswith("[iter 3]")
    assert "…" in event.text                         # long engine prose is clipped, not wrapped
    assert event.severity == "warn" and event.loud


def test_stop_states_the_outcome_and_the_reasons():
    event = _rendered("stop", {"state": "incomplete", "reasons": [
        "budget_insufficient_for_minimum_batch: need 7.75e+08 neutrons, have 4.45e+08"]})
    assert event.text == ("Campaign stopped: incomplete — budget_insufficient_for_minimum_batch: "
                          "need 7.75e+08 neutrons, have 4.45e+08")
    assert event.severity == "terminal"


def test_scan_design_reports_the_basis_not_its_fingerprints():
    event = _rendered("scan_design", {
        "iter": 0, "selection_basis": "shadow_legacy_control_unscored",
        "shadow_status": "unavailable",
        "scientific_action_fingerprint": "7e89a9327a0d762b83e20b6c5f531ff1",
        "selected_candidate_id": "a8194fe49c129f1adbb85b47ddfe1a95",
        "inventory_ref": {"kind": "scan_design_inventory", "sha256": "5c90c13ddd39d191"}})
    assert event.text == ("Scan window settled — shadow legacy control unscored "
                          "(shadow scoring unavailable)  [iter 0]")
    for fingerprint in ("7e89a9", "a8194f", "5c90c1"):
        assert fingerprint not in event.text
    assert "5c90c13ddd39d191" in event.raw_json()    # …but the audit detail is one click away


def test_inventory_says_what_was_weighed_and_what_was_chosen():
    event = _rendered("inventory", {
        "n_evaluations": 4, "n_eligible": 2, "is_complete": True, "terminal_cause": None,
        "selected_action_class": "quicklook", "selected_hkl": [2.15, 0.0, 0.0], "iter": 0,
        "selected_generator": "legacy_selector_adapter"})
    assert event.text == ("Weighed 4 candidate action(s), 2 usable — chose quicklook at H=2.15 "
                          "via legacy selector adapter  [iter 0]")

    blocked = _rendered("inventory", {"n_evaluations": 3, "n_eligible": 0, "iter": 9,
                                      "terminal_cause": "no_eligible_action"})
    assert blocked.text == "No usable next action — no eligible action  [iter 9]"
    assert blocked.severity == "warn"


def test_eig_plan_reaches_the_dock():
    assert "eig_plan" not in HIDDEN_KINDS


def test_activity_start_and_end_read_as_plain_progress():
    started = _rendered("activity_start", {"activity_id": "68ea8f78", "stage": "full_fit",
                                           "q": [2.15, 0.0, 0.0], "iteration": 0})
    assert started.text == "Started full fit at H=2.15"
    ended = _rendered("activity_end", {"activity_id": "68ea8f78", "stage": "full_fit",
                                       "q": [2.15, 0.0, 0.0], "status": "interrupted"})
    assert ended.text == "Finished full fit at H=2.15 — interrupted"
    assert ended.severity == "warn"


# ---- the fallback and the filters --------------------------------------------
def test_unknown_kind_falls_back_and_never_raises():
    event = _rendered("something_the_engine_added_later", {"whatever": 1})
    assert event.text == "event: something_the_engine_added_later"
    assert event.severity == "info"
    # A record that is not even shaped like an event must degrade, not explode.
    assert render_event("not a record") is None
    assert render_event({"kind": "propose", "ts": "not a number", "payload": None}) is not None


def test_a_payload_that_cannot_be_rendered_still_produces_a_line():
    class _Hostile(dict):
        def get(self, *_a, **_k):
            raise RuntimeError("payload access exploded")

    event = render_event({"ts": 1.0, "kind": "analyze", "payload": _Hostile()})
    assert event is not None and event.text == "event: analyze"


@pytest.mark.parametrize("kind", sorted(HIDDEN_KINDS))
def test_audit_and_profiling_kinds_do_not_reach_the_dock(kind):
    assert render_event({"ts": 1.0, "kind": kind, "payload": {"iter": 1}}) is None


def test_hidden_kinds_are_exactly_the_audit_and_profiling_sets():
    assert HIDDEN_KINDS == AUDIT_KINDS | PROFILING_KINDS
    assert not (HIDDEN_KINDS & LOUD_KINDS)           # nothing loud may ever be hidden


def test_geometry_renders_off_axis_points_in_full():
    event = _rendered("propose", {"hkl": [2.15, 0.5, 0.0], "kind": "production", "n_points": 4})
    assert "(2.15, 0.5, 0)" in event.text


# ---- synthesized budget status -----------------------------------------------
class _State:
    budget_kind = None
    budget_amount = None
    spent_neutrons = 0.0
    spent_seconds = 0.0
    analysis_limit_seconds = None
    analysis_spent_seconds = 0.0


def test_budget_line_reports_neutrons_only_under_a_neutron_budget():
    state = _State()
    state.budget_kind, state.budget_amount = "neutrons", 1e10
    state.spent_neutrons = 4.5e9
    state.analysis_limit_seconds, state.analysis_spent_seconds = 3600.0, 600.0
    assert budget_status_line(state) == ("Budget — 5.5G of 10G neutrons left; "
                                         "50 min of 1 h 00 m analysis time left")


def test_budget_line_reports_time_under_a_counting_seconds_budget():
    state = _State()
    state.budget_kind, state.budget_amount = "counting_seconds", 8 * 3600.0
    state.spent_seconds = 2 * 3600.0
    assert budget_status_line(state) == "Budget — 6 h 00 m of 8 h 00 m measurement time left"
    assert "neutron" not in budget_status_line(state)


def test_budget_line_is_absent_when_nothing_is_bounded():
    assert budget_status_line(_State()) is None


def test_budget_announcer_paces_itself_and_suppresses_repeats():
    state = _State()
    state.budget_kind, state.budget_amount = "neutrons", 1e10
    announcer = BudgetAnnouncer(interval_seconds=120.0, every_rows=5)

    assert announcer.maybe_line(state, now=0.0, rows=1) is not None      # first is always due
    assert announcer.maybe_line(state, now=1.0, rows=1) is None          # neither trigger fired
    assert announcer.maybe_line(state, now=200.0, rows=1) is None        # due, but unchanged text
    state.spent_neutrons = 4.5e9
    assert "5.5G" in announcer.maybe_line(state, now=400.0, rows=1)
    state.spent_neutrons = 6e9
    assert announcer.maybe_line(state, now=401.0, rows=1) is None        # not due yet
    assert "4G" in announcer.maybe_line(state, now=402.0, rows=5)        # row trigger fires

    announcer.reset()
    assert announcer.maybe_line(state, now=402.0, rows=0) is not None


def test_budget_kind_is_a_distinct_synthetic_kind():
    assert BUDGET_KIND not in HIDDEN_KINDS and BUDGET_KIND not in LOUD_KINDS


# ---- degraded payloads: what the operator sees when a field is absent ---------
def test_the_exception_line_carries_the_error_text_whichever_key_holds_it():
    """32 of 38 emitters write ``error``; only a handful write ``reason``.

    The authoritative-selection failure (``isar/drive/loop.py``) is one of the 32, so the operator
    used to see the action slug and nothing at all about what went wrong.
    """
    event = _rendered("exception", {"action": "authoritative_selection",
                                    "error": "ValueError: no scored grid join", "iter": 12})
    assert event.text == ("Problem: authoritative selection — ValueError: no scored grid join"
                          "  [iter 12]")
    legacy = _rendered("exception", {"action": "analyze", "reason": "fit did not converge"})
    assert legacy.text == "Problem: analyze — fit did not converge"


def test_a_low_severity_exception_reads_as_a_note_not_a_problem():
    low = _rendered("exception", {"action": "shadow_scoring_workspace_not_initialized",
                                  "severity": "low", "reason": "shadow_disagrees", "iter": 0})
    assert low.text == ("Note: shadow scoring workspace not initialized — shadow_disagrees"
                        "  [iter 0]")
    assert low.severity == "info" and low.loud       # still an exception kind: never filtered away

    ordinary = _rendered("exception", {"action": "recenter", "reason": "peak drifted"})
    assert ordinary.text == "Problem: recenter — peak drifted"
    assert ordinary.severity == "warn"


def test_missing_counts_read_as_a_dash_not_as_the_word_none():
    update = _rendered("decide", {"action": "model_update"})
    assert "None" not in update.text
    assert update.text == "Dispersion model updated — — of — points certified, — observation(s)"

    weighed = _rendered("inventory", {"selected_action_class": "quicklook"})
    assert "None" not in weighed.text
    assert weighed.text.startswith("Weighed — candidate action(s), — usable")

    planning = _rendered("propose", {"kind": "quicklook", "n_points": 8})
    assert "None" not in planning.text
    assert planning.text == "Planning a quicklook scan — 8 points"

    bare = _rendered("eig_plan", {"chosen": {}})
    assert "None" not in bare.text and "?" not in bare.text


def test_a_non_finite_budget_degrades_instead_of_killing_the_feed():
    """``duration_text`` reaches ``int(inf)`` → ``OverflowError``, inside an unguarded Qt slot."""
    state = _State()
    state.budget_kind, state.budget_amount = "counting_seconds", float("inf")
    assert budget_status_line(state) is not None          # no exception escapes
    state.analysis_limit_seconds = float("inf")
    assert budget_status_line(state) is not None


def test_the_eig_plan_line_names_the_choice_the_runner_up_and_the_prize():
    """The one line an operator reads to know why this scan and not another."""
    line = _rendered("eig_plan", {
        "iter": 3, "tolerance_meV": 0.05,
        "chosen": {"grid_index": 1, "hkl": [2.15, 0.0, 0.0], "e_lo": 0.6, "e_hi": 3.1,
                   "e_step": 0.2, "n_points": 13, "ncount": 10_000_000, "score": 4.21e-6,
                   "neutrons": 1.3e8, "price": 1.8e8},
        "per_point": [{"hkl": [2.10, 0.0, 0.0], "halfwidth_meV": 0.04},
                      {"hkl": [2.15, 0.0, 0.0], "halfwidth_meV": 0.31}],
        "ranked": [{"hkl": [2.15, 0.0, 0.0], "score": 4.21e-6},
                   {"hkl": [2.20, 0.0, 0.0], "score": 3.88e-6}],
        "prize_points_within_bar": 0, "prize_points_total": 20})
    assert line.text == ("EIG → ±0.31 → H=2.15: 0.60–3.10 meV, "
                         "13 pts × 10M = 1.3e+08 n, price 1.8e+08, 4.21 nats/1e6 n "
                         "— runner-up H=2.2 3.88 "
                         "— prize 0/20 within ±0.05 meV  [iter 3]")
    assert line.severity == "notice"


def test_a_design_record_missing_its_window_says_so_instead_of_printing_question_marks():
    line = _rendered("eig_plan", {"iter": 4, "chosen": {"hkl": [2.15, 0.0, 0.0]}})
    assert line.text == ("EIG — a design was chosen but its record carries no window or "
                         "exposure  [iter 4]")
    assert line.severity == "warn"


def test_the_eig_plan_stop_line_says_the_map_is_done():
    line = _rendered("eig_plan", {"tolerance_meV": 0.05, "stop": True, "chosen": None,
                                  "prize_points_within_bar": 20, "prize_points_total": 20})
    assert line.text == "EIG: hypothesis map at ±0.05 meV at every point — done"
    assert line.severity == "notice"


def _plan_payload(**extra):
    payload = {
        "iter": 3, "tolerance_meV": 0.05,
        "chosen": {"grid_index": 1, "hkl": [2.15, 0.0, 0.0], "e_lo": 0.6, "e_hi": 3.1,
                   "e_step": 0.2, "n_points": 13, "ncount": 10_000_000, "score": 4.21e-6,
                   "neutrons": 1.3e8, "price": 1.8e8},
        "per_point": [{"hkl": [2.15, 0.0, 0.0], "halfwidth_meV": 0.31}],
        "ranked": [{"hkl": [2.15, 0.0, 0.0], "score": 4.21e-6}],
        "prize_points_within_bar": 0, "prize_points_total": 20,
        "intensity_scale": {"per_ncount": 1e7,
                            "peak_counts_meV": {"acoustic": 17.0, "optic": 15.0},
                            "elastic_counts_meV": 30.0, "background_per_point": 0.3},
        "intensity_scale_changed": True}
    payload.update(extra)
    return payload


def test_the_intensity_map_is_said_once_when_it_changes():
    """It is the same three numbers every iteration otherwise, and a feed that repeats them teaches
    an operator to stop reading the line."""
    changed = _rendered("eig_plan", _plan_payload())
    assert "intensity map: acoustic 17, optic 15, elastic 30, bg 0.3 /1e+07" in changed.text
    unchanged = _rendered("eig_plan", _plan_payload(intensity_scale_changed=False))
    assert "intensity map" not in unchanged.text


def test_a_planner_note_rides_on_the_line_and_is_loud():
    """The planner says so when no design can complete a point; that is a warning, not a footnote."""
    line = _rendered("eig_plan", _plan_payload(
        intensity_scale_changed=False,
        note="no design is expected to complete a point; taking the cheapest probe"))
    assert line.text.endswith(
        "\u2014 no design is expected to complete a point; taking the cheapest probe  [iter 3]")
    assert line.severity == "warn"
    assert _rendered("eig_plan", _plan_payload(intensity_scale_changed=False)).severity == "notice"


def test_a_point_held_back_by_identity_says_so_beside_its_width():
    """A MEASURED point can sit inside the bar on width and still not be answered, because the mass
    is spread over rival identities, and showing only the half-width there reads as done when it is
    not. A point with no candidate yet is not held back by identity -- it has no identity to be
    unsure of -- and "identified 0.00" on every opening scan trains the operator to ignore the
    clause exactly where it matters."""
    def line(**point):
        return _rendered("eig_plan", _plan_payload(
            intensity_scale_changed=False,
            per_point=[{"hkl": [2.15, 0.0, 0.0], **point}])).text

    held = line(halfwidth_meV=0.03, identified_mass=0.71, n_candidates=2)
    assert "\u00b10.03 but identified 0.71 \u2192 H=2.15" in held

    clear = line(halfwidth_meV=0.03, identified_mass=0.98, n_candidates=1)
    assert "\u00b10.03 \u2192 H=2.15" in clear
    assert "identified" not in clear

    unmeasured = line(halfwidth_meV=4.5, identified_mass=0.0, n_candidates=0)
    assert "\u00b14.50 \u2192 H=2.15" in unmeasured
    assert "identified" not in unmeasured


def test_the_requested_dose_and_the_ranked_price_are_both_shown():
    """They are different numbers — the price carries the fixed cost of taking a scan at all — and
    only the price explains why a cheap-looking design lost."""
    line = _rendered("eig_plan", _plan_payload(intensity_scale_changed=False))
    assert "13 pts \u00d7 10M = 1.3e+08 n, price 1.8e+08" in line.text
