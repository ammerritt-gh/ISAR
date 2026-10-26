"""The fast pass's acceptance table (coordinator decision 9), run deliberately, never by a routine check.
It is opt-in by environment (skipped otherwise); run it with

    set ISAR_FAST_PASS_ACCEPTANCE=1
    python -m pytest tests/analyze/test_fast_pass_acceptance.py -m slow -s -p no:xdist

It runs its own pool of at most six processes (single-thread BLAS, 600 s kill per row), once with the
delta-twin rule on and once with it off. Both tables and the aims as reached are printed (the operator's
ruling: a numerical aim is reported, never a gate). The only assertion is that every row ran.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fast_pass_table as table  # noqa: E402


@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("ISAR_FAST_PASS_ACCEPTANCE") != "1",
                    reason="opt-in: set ISAR_FAST_PASS_ACCEPTANCE=1 (a ~7 minute table)")
@pytest.mark.timeout(2400)
def test_acceptance_table(tmp_path):
    on_jobs, off_jobs = table.table_jobs(True), table.table_jobs(False)
    rows = table.run_table(on_jobs + off_jobs, tmp_path)
    on, off = rows[:len(on_jobs)], rows[len(on_jobs):]
    print()
    print(table.format_table(on, "TWIN RULE ON"))
    print(table.format_table(off, "TWIN RULE OFF"))
    for label, group in (("on", on), ("off", off)):
        passed, unresolved, narrow, median, longest = table.summary(group)
        print("twin rule {}: {} of 10 draws pass (aim >= {}), {} unresolved (aim 0), {} of 2 narrow cases "
              "(aim 2), wall median {:.1f} s, max {:.1f} s (aim {} s)".format(
                  label, passed, table.PASS_AIM, unresolved, narrow, median, longest, table.WALL_AIM_S))
    assert all(r["state"] not in ("killed", "error") for r in rows), "a row did not run"
