"""The inter-spectra fit bar's action rules: pure, no Qt."""
from __future__ import annotations

from gui.inter_fakes import make_entry, make_history
from isar.workspace_api.inter_spectra import FlagInfo, FlagReason
from isar_gui.inter_words import CONDITIONAL_ERRORS, ERRORS_UNAVAILABLE
from isar_gui.viewmodels.inter_fits import bar_state, entry_tip

B1 = ("b1", "Batch 1")
B2 = ("b2", "Batch 2")


def _refit(fit_id, batch, stage, **overrides):
    batch_id, batch_label = batch
    return make_entry(fit_id, f"{batch_label} · {stage}", batch_id=batch_id, batch_label=batch_label,
                      stage_label=stage, **overrides)


def _history():
    lost = FlagInfo("serious", 4.1, 2.9, 1.2, (FlagReason("branch_lost_mid_window", "serious", "branch B, 3.2 meV"),),
                    flagged=True)
    return make_history(_refit("f1", B1, "ring 1"),
                        _refit("f2", B1, "ring 2", changed=False),
                        _refit("f3", B2, "sweep 3", authoritative=True, flag=lost))


def test_the_tip_gives_the_reasons_for_a_change_and_for_a_flag():
    tip = entry_tip(_history()[3])
    assert "not seen here" in tip and "branch B, 3.2 meV" in tip
    assert entry_tip(make_history()[0]) == "Fitted on its own, with no neighbours."


def test_make_authoritative_is_greyed_with_its_reason_when_already_authoritative_or_stale():
    assert bar_state(_history(), "f3").authoritative_off == "This fit is already the one ISAR uses for this spectrum."
    assert bar_state(_history(), "f1").authoritative_off is None
    stale = make_history(_refit("f1", B1, "ring 1", stale=True, stale_text="its own inputs changed since the refit"))
    assert "stale" in bar_state(stale, "f1").authoritative_off


def test_a_locked_spectrum_offers_unlock_and_refuses_another_pick():
    state = bar_state(_history(), "f1", locked=True, locked_fit="f3")
    assert state.lock_text == "Unlock" and state.lock_off is None
    assert state.authoritative_off == "Locked to f3; unlock first."


def test_loading_and_fast_only_states_say_why():
    loading = bar_state(None, "fast")
    assert loading.entries is None and "Reading" in loading.menu_off
    assert bar_state(make_history(), "fast").menu_off == (
        "This spectrum has no inter-spectra fits yet. Fast solo is the only fit.")


def test_the_info_line_names_the_fit_and_its_state_and_errors():
    state = bar_state(_history(), "f3", locked=True, locked_fit="f3")
    assert state.info == ("Showing Batch 2 · sweep 3 · authoritative · locked · flagged serious · "
                          + CONDITIONAL_ERRORS)
    assert "not seen here" in state.info_tip
    assert CONDITIONAL_ERRORS not in bar_state(_history(), "fast").info
    unavailable = make_history(_refit("f1", B1, "ring 1", errors_available=False))
    assert ERRORS_UNAVAILABLE in bar_state(unavailable, "f1").info
