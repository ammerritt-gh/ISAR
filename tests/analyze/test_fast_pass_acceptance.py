"""The fast pass's acceptance table (coordinator decision 9), run deliberately, never by a routine check.
It is opt-in by environment (skipped otherwise); run it with

    set ISAR_FAST_PASS_ACCEPTANCE=1
    python -m pytest tests/analyze/test_fast_pass_acceptance.py -m slow -s -p no:xdist

It runs its own pool of at most six processes (single-thread BLAS, 600 s kill per row), once with the
delta-twin rule on and once with it off. Both tables and the aims as reached are printed (the operator's
ruling: a numerical aim is reported, never a gate). The only assertion is that every row ran.
"""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fast_pass_table as table  # noqa: E402


def test_unpriced_count_reporting_uses_evidence_status_and_counts_a_priced_sibling():
    def model(k, status, ln_z=None):
        return SimpleNamespace(n_peaks=k, evidence_status=status, ln_z=ln_z)

    models = [model(0, "no_evidence"), model(1, "no_evidence"), model(1, "ok", -12.0),
              model(2, "not_fitted"), model(3, "degenerate_twin", -10.0),
              model(4, "ok_boundary", -20.0), model(5, "ok", float("nan"))]
    assert table.unpriced_counts(models) == [0, 2, 3, 5]
    rows = [{"counts_considered": list(range(6)), "unpriced_counts": [0, 2, 3, 5]},
            {"counts_considered": [0, 1], "unpriced_counts": [0]}, {"state": "killed"}]
    assert table.coverage_summary(rows) == {
        "rows_measured": 2, "rows_with_unpriced_count": 2, "unpriced_count_instances": 5,
        "count_instances": 8, "unpriced_by_count": {0: 2, 2: 1, 3: 1, 5: 1}}


@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("ISAR_FAST_PASS_ACCEPTANCE") != "1",
                    reason="opt-in: set ISAR_FAST_PASS_ACCEPTANCE=1 (a ~7 minute table)")
@pytest.mark.timeout(2400)
def test_acceptance_table(tmp_path):
    on_jobs, off_jobs = table.table_jobs(True), table.table_jobs(False)
    rows = table.run_table(on_jobs + off_jobs, tmp_path)
    with (tmp_path / "acceptance-table.json").open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(rows, stream, indent=2)
    on, off = rows[:len(on_jobs)], rows[len(on_jobs):]
    print()
    print(table.format_table(on, "TWIN RULE ON"))
    print(table.format_table(off, "TWIN RULE OFF"))
    for label, group in (("on", on), ("off", off)):
        passed, unresolved, narrow, median, longest = table.summary(group)
        print("twin rule {}: {} of 10 draws pass (aim >= {}), {} unresolved (aim 0), {} of 2 narrow cases "
              "(aim 2), wall median {:.1f} s, max {:.1f} s (aim {} s)".format(
                  label, passed, table.PASS_AIM, unresolved, narrow, median, longest, table.WALL_AIM_S))
        coverage = table.coverage_summary(group)
        print("twin rule {}: wholly unpriced {}/{} count instances across {}/{} measured rows; "
              "unpriced frequency by count {}".format(
                  label, coverage["unpriced_count_instances"], coverage["count_instances"],
                  coverage["rows_with_unpriced_count"], coverage["rows_measured"],
                  coverage["unpriced_by_count"]))
    print("JSON rows: {}".format(tmp_path / "acceptance-table.json"))
    assert all(r["state"] not in ("killed", "error") for r in rows), "a row did not run"
