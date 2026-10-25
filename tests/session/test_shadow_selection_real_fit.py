"""STAT-2a regression: a REAL fit's shadow-selection record must survive validation.

The producer normalizes Akaike weights in NumPy (`rel / rel.sum()`, pairwise summation) while
`isar.schema.statistics._validate_shadow_model_selection` recomputes them in pure Python
(`math.exp`, `sum()`). With enough candidates the two differ by 1 ULP, and a whole-list `!=`
comparison rejected the record — driving `selection_status` to "unavailable", which
`isar/eig/eligibility.py` then treats as a *science* blocker for O36 EIG eligibility.

Hand-built shadow dicts cannot catch this: the divergence only exists between the two independent
computations, so this gate runs the driver on a synthetic IN8 run and inspects what was persisted.
The seed/geometry below is pinned because the divergence is data-dependent: at
``seed0=54500, q=[2.25, 0.05, 0.0], n_scans=3`` the pre-fix validator rejected 2 of the 3 scans
(8-candidate denominators).
"""
from pathlib import Path

from isar.parsers import ill_in8
from isar.schema.statistics import validate_shadow_model_selection
from isar.session import driver
from isar.session.workspace import Workspace
from isar.synth_format import run_builder


# Deliberately NOT marked ``slow`` (3.3 s): this guards a defect that made two thirds of real fits
# publish ``selection_status=unavailable``, which ``isar/eig/eligibility.py`` treats as a *science*
# blocker.  A regression test for that has to run in the day-to-day ``-m "not slow"`` lane, because
# the failure it catches is data-dependent and invisible to every hand-built-record test.
def test_real_fit_shadow_selection_is_never_unavailable(tmp_path, monkeypatch):
    from isar.analyze import count_ladder

    monkeypatch.setenv(count_ladder.RUNG2_BUDGET_ENV, "0")   # rung 2 is not what this test is about: a zero budget leaves an escalated count search limited, unsampled
    run = run_builder.build_in8_run(
        "SHADOWREG", n_scans=3, seed0=54500, first_numor=54500, q_point=[2.25, 0.05, 0.0]
    )
    data = tmp_path / "SHADOWREG.numor"
    data.write_bytes(ill_in8.emit_file(run, ill_in8.card_template()))

    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.policy_path.write_text(
        Path(ill_in8._CARD_PATH).read_text(encoding="utf-8"), encoding="utf-8"
    )
    driver.run_batch(ws, [data], M=8, errors_tier="fast", workers=1, formats=())

    _card, session = ws.load()
    records = [
        (scan_id, detector_id, outcome["shadow_model_selection"])
        for scan_id, entry in session.scans.items()
        for detector_id, outcome in (entry.detectors or {}).items()
        if outcome.get("shadow_model_selection")
    ]
    assert records, "no shadow-selection record was persisted by the run"

    multi_candidate = 0
    for scan_id, detector_id, record in records:
        assert record["selection_status"] != "unavailable", (
            f"{scan_id}/{detector_id}: {record.get('unavailable_reason_code')}"
        )
        validated = validate_shadow_model_selection(record)
        multi_candidate = max(
            multi_candidate, len(validated["standard_model_weights"])
        )
    # Guard the guard: a run whose denominators are all singletons cannot exercise the
    # float-summation divergence, so it would pass for the wrong reason.
    assert multi_candidate >= 3, f"pinned run degenerated to {multi_candidate} weighted candidates"
