"""The one atomic-replace step the session writers share, tolerant of a concurrent reader.

Every session artifact is published the same way: write the full content under a temporary name,
flush, fsync, then ``os.replace`` it over the live file so a reader never sees half a file. On
Windows the last step is not unconditional: ``os.replace`` fails with ``PermissionError`` while any
other process holds the target open — and a plain ``open()`` for reading (the GUI on every refresh,
a notebook, an operator's script) is exactly such a hold. A reader holds the file for milliseconds;
the campaign writes it every iteration for hours. Measured live on 2026-09-05: one such collision
on ``results.csv`` killed the analysis worker and, through it, the whole campaign.

So the swap retries. The wait is bounded well under a second in total, which covers any honest
reader; a hold that outlives it (a file open in an editor, a stuck process) is not something a
retry should hide, and the error propagates with the retries already logged.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

#: How many swap attempts before the error propagates, and the pause between them. 20 x 0.04 s is
#: 0.8 s worst case: far longer than any reader's hold, far shorter than an iteration.
REPLACE_ATTEMPTS = 20
REPLACE_DELAY_SECONDS = 0.04


def replace_retrying(temporary: str | Path, target: str | Path, *,
                     attempts: int = REPLACE_ATTEMPTS,
                     delay_seconds: float = REPLACE_DELAY_SECONDS) -> None:
    """``os.replace(temporary, target)``, retried while a concurrent reader holds ``target``.

    Every retry is logged (a silent retry would hide a reader that is holding the file too long);
    the final failure re-raises the original ``PermissionError`` so the caller's contract is
    unchanged.
    """
    for attempt in range(1, int(attempts) + 1):
        try:
            os.replace(temporary, target)
            return
        except PermissionError as error:
            if attempt >= attempts:
                log.error("could not replace %s after %d attempts (a reader is holding it): %s",
                          target, attempt, error)
                raise
            log.warning("replace of %s blocked by a concurrent reader (attempt %d/%d): %s",
                        target, attempt, attempts, error)
            time.sleep(float(delay_seconds))


# --- C-lock: the workspace write lock -----------------------------------------------------------
#: The lock file at the workspace root. The writers that run beside the analysis queue
#: (``isar.session.writer.save_results`` and ``isar ingest``) hold it for their whole
#: read-modify-write; readers never take it.
WRITE_LOCK = ".isar-write.lock"
#: Longer than the one long hold (a folder copy under the analysis service's ``hold_writes``, about a
#: minute), so a writer arriving during a copy waits rather than fails. An algorithm setting.
WRITE_LOCK_TIMEOUT_S = 120.0


class WorkspaceBusy(TimeoutError):
    """The workspace write lock was not obtained within its timeout; nothing was written."""


@contextmanager
def workspace_write_lock(root: str | Path, *, timeout_s: float = WRITE_LOCK_TIMEOUT_S) -> Iterator[None]:
    """Hold ``<root>/.isar-write.lock``: exclusive across processes, released by the OS if the holder
    dies, not reentrant (a nested take waits, then raises ``WorkspaceBusy``)."""
    from isar.intake import store   # lazily: intake/scans.py imports this module lazily; keep it acyclic
    path = Path(root) / WRITE_LOCK
    with ExitStack() as stack:
        try:
            stack.enter_context(store.file_lock(path, timeout_s=timeout_s))
        except store.StoreBusy:      # only the take: a StoreBusy from the body propagates as itself
            raise WorkspaceBusy(f"{path} held by another ISAR writer for {timeout_s} s") from None
        yield
