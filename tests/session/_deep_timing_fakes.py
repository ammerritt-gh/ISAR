"""Synthetic production retry/scheduling path for the service's two-level spawn regression."""
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path

from _service_fakes import FileItem


def _install_sampler(root, item_stderr_none):
    from isar.analyze import deep_pass as dp
    with Path(root, "streams.json").open("w", encoding="utf-8", newline="\n") as stream:
        json.dump({"item_stderr_none": item_stderr_none,
                   "worker_stderr_none": sys.stderr is None}, stream)
    dp.run_nested = _synthetic_run


def _synthetic_run(label, problem, *, settings, seed, reference_lnl):
    from isar.analyze import deep_pass as dp
    if label == "stopped":
        time.sleep(60)  # the service stop must kill the active item and its pool
    suspect = ("synthetic missed optimum",) if settings.step_factor == 4 else ()
    failure = "synthetic sampler failure" if settings.step_factor == 8 and settings.n_live == 400 else None
    return dp.RunRecord(label, seed, settings, 2, -2.0, 0.1, 1.0, 17, 5, -1.0,
                        2.5, None, suspect=suspect, failure=failure, evidence_converged=True)


def fit_file(root, item, *, progress, emit):
    from isar.analyze import deep_pass as dp
    from isar.analyze.progress import emitter, relay
    entries = [dp.ModelEntry("finished", 1, 2, 0.0, None, None),
               dp.ModelEntry("stopped", 2, 2, -1.0, None, None)]
    with ProcessPoolExecutor(1, mp_context=get_context("spawn"), initializer=_install_sampler,
                             initargs=(str(root), sys.stderr is None)) as pool:
        dp.deep_pass(entries, executor=pool, workers=1, seed=42,
                     progress=relay(emitter(progress, "deep")))
