"""The fast pass's acceptance table (coordinator decision 9), run deliberately, never by a routine check:

    python -m pytest tests/analyze/test_fast_pass_acceptance.py -m slow -s -p no:xdist

It runs its own pool of at most six processes (single-thread BLAS, 600 s kill per row), once with the
delta-twin rule on and once with it off. Both tables are printed; the requirement is asserted on the
twin-on table only, after both are printed. The disabled table and the wall times (aim 60 s) are
reported, not asserted.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fast_pass_table as table  # noqa: E402


@pytest.mark.slow
@pytest.mark.timeout(2400)
def test_acceptance_table(tmp_path):
    on_jobs, off_jobs = table.table_jobs(True), table.table_jobs(False)
    rows = table.run_table(on_jobs + off_jobs, tmp_path)
    on, off = rows[:len(on_jobs)], rows[len(on_jobs):]
    print()
    print(table.format_table(on, "TWIN RULE ON"))
    print(table.format_table(off, "TWIN RULE OFF (reported, not asserted)"))
    for label, group in (("on", on), ("off", off)):
        passed, unresolved, narrow, median, longest = table.summary(group)
        print("twin rule {}: {} of 10 draws pass, {} unresolved, {} of 2 narrow cases, wall median {:.1f} s, max {:.1f} s"
              " (aim {} s)".format(label, passed, unresolved, narrow, median, longest, table.WALL_AIM_S))
    passed, unresolved, narrow, _median, _longest = table.summary(on)
    assert passed >= table.PASS_AIM, "{} of 10 draws pass".format(passed)
    assert unresolved == 0, "{} rows unresolved".format(unresolved)
    assert narrow == 2, "{} of 2 narrow cases pass".format(narrow)
