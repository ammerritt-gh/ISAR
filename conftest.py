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
"""

import os

# Import-time, not a fixture: the cap must be in place before any test module import that might
# resolve a worker budget at collection time.
os.environ.setdefault("ISAR_WORKERS", "2")
os.environ.setdefault("ISAR_BOOTSTRAP_WORKERS", "2")
os.environ.setdefault("ISAR_ESCALATION_N_LIVE", "32")
