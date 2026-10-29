"""Repo-root pytest configuration.

This file lives at the repository root rather than under ``tests/`` on purpose: ``testpaths`` in
``pyproject.toml`` names two collection roots (``isar/synth/tests`` and ``tests``), and the root
conftest is the only one whose fixtures and import-time setup cover both.

Nested-pool guard
-----------------
``isar.session.driver.run_batch`` resolves its scan-level pool to ``min(30, cpu - 2)`` children per
call when no explicit ``--workers``/``workers=`` is given (``driver._resolve_workers``), and the
parametric bootstrap can claim the same budget outside a scan-pool worker
(``driver._resolve_boot_workers``). That is right for production, where one ``isar run`` owns the
machine. Under pytest-xdist it is a fork bomb: every xdist worker that reaches a ``run_batch`` call
would spawn its own 30-child pool, so ``-n 16`` can put ~480 processes on a 32-core box.

Capping the two documented env knobs keeps the real multiprocessing code path exercised (the pool
is still spawned, pickling and result reassembly still happen) while bounding the total to a few
children per xdist worker. ``setdefault`` means an operator or CI that sets either variable keeps
full control, and a test that needs a real pool size can ``monkeypatch.setenv`` it.

The cap has a second effect worth knowing: with ``ISAR_WORKERS=2``, ``driver.run_batch``'s
``use_workorder = is_full and len(tasks) <= scan_workers`` means any full-tier batch of 3+ tasks
can no longer take the single-work-order path and deterministically falls to the Layer-A scan-pool
branch under pytest — previously this was machine-dependent (it flipped with the ambient CPU
count).

No test currently needs an opt-out. The three parallel-equivalence tests all pin their worker
counts as explicit call arguments, which take precedence over the environment:
``tests/session/test_master_fit.py::test_parallel_masters_match_serial_end_to_end`` passes
``workers=1``/``workers=4`` to ``run_batch``; ``tests/analyze/test_bootstrap.py`` passes ``workers=``
(and sometimes an ``executor=``) straight to ``parametric_bootstrap``, which never reads the
environment; ``isar/synth/tests/test_score.py::test_serial_equals_parallel_tiny`` passes
``n_workers=`` to ``score.run_scorer``. Any future test that relies on the *default* budget to
exercise fan-out must set the variable itself and say why.

Escalation sampler cap
----------------------
Under evidence selection a campaign-style test pays a dynesty escalation per contested candidate
per scan, and that sampler is ~90% of such a test's wall clock. Measured 2026-08-09:
``test_shadow_selection_is_invariant_to_delivery_constraints`` takes 348 s at ``n_live=128`` and
35.6 s at ``n_live=8``, passing either way. ``ISAR_ESCALATION_N_LIVE=32`` keeps the escalation rung
fully exercised — it still samples, still gates on convergence, still records the n_live it used in
the candidate's validity provenance — at a quarter of the cost.

That is legitimate because of what the suite actually asserts about escalations: that they happen,
that their outcome is recorded honestly, and that selection is *identical* between serial and
parallel arms — never that lnZ takes a particular value. The Occam gaps selection turns on are
10–40 nats while logzerr at this n_live stays ≲1, so a decision that flips at 32 but not at 128
would be a finding worth chasing, not noise to tune away. Tests that do assert sampler numerics
pass ``n_live`` explicitly (``tests/analyze/test_evidence.py``'s ``FAST``, ``test_bayes.py``), and
an explicit argument always wins: ``candidate_set_evidence`` consults the environment only through
``evidence.escalation_n_live()``, and only when no ``n_live`` was supplied.

``setdefault`` again, so an operator or a test needing the production value sets it themselves.
Production leaves it unset and samples at ``ESCALATION_N_LIVE`` (32 since 2026-09-03, 128 before;
the cap below now coincides with production and stays as the explicit contract). The *environment* rather
than a fixture patching the module constant is what matters here: a spawned pool child re-imports
``isar.analyze.evidence`` and would otherwise silently sample at 128, which is exactly the arm
these tests exist to compare.

Mode ceiling cap
----------------
The peak-count ladder tries every count from 0 to the ceiling for every spectrum, so each extra
count multiplies the fit cost of every test that fits. The ceiling resolves card
``[fitting] max_modes`` -> ``ISAR_MODE_CEILING`` -> 6 (``isar.session.guidelines.mode_ceiling``).
``ISAR_MODE_CEILING=3`` is one above the largest count any routine fixture holds (two), so no
two-mode fixture sits *at* the ceiling, which would trigger rung 2 and the "search limited" state
and change what those tests mean. Tests of the at-ceiling trigger and state pass the ceiling
explicitly (a card with ``max_modes``), and an explicit value always wins over the environment.

Same route and reasoning as ``ISAR_ESCALATION_N_LIVE``: the *environment*, so a spawned pool child
that re-imports the fit code sees the same ceiling instead of silently searching up to 6, and
``setdefault`` so an operator or a test needing the production value sets it themselves.
Production leaves the variable unset and uses 6.

Operator-profile guard
----------------------
The GUI writes into the operator's real profile: ``%APPDATA%\\isar_gui`` (the session file and the
log), ``%LOCALAPPDATA%\\isar_gui`` and, for a new experiment, ``Documents\\ISAR``. ``tests/gui/conftest.py``
points all three at a per-test folder, but a per-test ``monkeypatch`` is undone before pytest-qt's
teardown closes a window left open, and a write made then resolves the *real* profile. That is how the
operator's ``session.json`` was rewritten by a test run (2026-10-27). So the profile is redirected for
the whole session here, at import time: ``APPDATA``, ``LOCALAPPDATA`` and ``ISAR_EXPERIMENTS_ROOT`` are
overridden (never ``setdefault``: on Windows the real ones are always set) to a fresh folder removed at
the end. Each xdist worker imports this file too, and finds the variable already inherited from the
controller. The originals are kept in ``ISAR_TEST_REAL_<name>`` for ``tests/test_profile_guard.py``.
"""

import os
import shutil
import tempfile

# Import-time, not a fixture: the cap must be in place before any test module import that might
# resolve a worker budget at collection time.
os.environ.setdefault("ISAR_WORKERS", "2")
os.environ.setdefault("ISAR_BOOTSTRAP_WORKERS", "2")
os.environ.setdefault("ISAR_ESCALATION_N_LIVE", "32")
os.environ.setdefault("ISAR_MODE_CEILING", "3")

# Operator-profile guard (see the docstring). The controller creates the folder; workers inherit it.
_PROFILE_OWNER = False
if not os.path.isdir(os.environ.get("ISAR_TEST_PROFILE_ROOT", "")):
    for _name in ("APPDATA", "LOCALAPPDATA", "ISAR_EXPERIMENTS_ROOT"):
        os.environ["ISAR_TEST_REAL_" + _name] = os.environ.get(_name, "")
    os.environ["ISAR_TEST_PROFILE_ROOT"] = tempfile.mkdtemp(prefix="isar-pytest-profile-")
    _PROFILE_OWNER = True
_profile = os.environ["ISAR_TEST_PROFILE_ROOT"]
os.environ["APPDATA"] = os.path.join(_profile, "appdata")
os.environ["LOCALAPPDATA"] = os.path.join(_profile, "localappdata")
os.environ["ISAR_EXPERIMENTS_ROOT"] = os.path.join(_profile, "experiments")


def pytest_unconfigure(config):
    if _PROFILE_OWNER:
        shutil.rmtree(_profile, ignore_errors=True)  # scratch folder in the system temp; a leftover is harmless


# Marker -> the option that opts it in. Without the option its tests are deselected, even when named.
# The first marker an item carries governs it: a statistical campaign (also marked slow) needs only
# --run-statistical.
_OPT_IN = {"statistical": "--run-statistical", "slow": "--run-slow"}


def _opt_in_option(item):
    return next((option for marker, option in _OPT_IN.items() if item.get_closest_marker(marker)), None)


def pytest_addoption(parser):
    parser.addoption("--run-statistical", action="store_true", default=False,
                     help="Include repeated-fit coverage/bias campaigns (explicit opt-in).")
    parser.addoption("--run-slow", action="store_true", default=False,
                     help="Include the slow checks (run deliberately, never in the routine gate).")


def pytest_collection_modifyitems(config, items):
    """Statistical campaigns and slow checks never run as an accidental side effect of a suite
    run; what is left is the routine gate (operator ruling 2026-10-29)."""
    kept, dropped = [], []
    for item in items:
        option = _opt_in_option(item)
        (dropped if option and not config.getoption(option) else kept).append(item)
    if dropped:
        items[:] = kept
        config.hook.pytest_deselected(items=dropped)
