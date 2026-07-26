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
"""

import os

# Import-time, not a fixture: the cap must be in place before any test module import that might
# resolve a worker budget at collection time.
os.environ.setdefault("ISAR_WORKERS", "2")
os.environ.setdefault("ISAR_BOOTSTRAP_WORKERS", "2")
