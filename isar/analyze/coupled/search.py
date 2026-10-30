"""One spectrum's adaptive candidate search: the C-search boundary (S3U1 stubs the members, S3U2 fills them).

The search asks for a few prices, reads them, then asks for the next ones (``next_jobs`` and ``feed``) until
``done``; ``result`` then gives the frozen decision. The stop codes of ``Assignment.coverage["stopped"]`` are
``behind_margin:<k>``, ``collapsed:<k>``, ``unresolved:<k>``, ``no_structure:<k>``, ``floor``, ``no_parent``,
``no_step:<k>`` and ``full_search``. ``drive`` is the one inline loop over this protocol.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from isar.analyze.coupled.types import Assignment, Priced, PricingJob, Snapshot


class Search:
    """One spectrum's adaptive candidate search on one frozen snapshot: search, price, resume."""

    def __init__(self, snapshot: Snapshot) -> None:
        raise NotImplementedError("S3U2")

    def next_jobs(self) -> tuple[PricingJob, ...]:
        """Every job to price next, deterministic, distinct keys; () if and only if done. Idempotent without a feed."""
        raise NotImplementedError("S3U2")

    def feed(self, priced: Sequence[Priced]) -> None:
        """One Priced per job of the last next_jobs(), in that order; ValueError on a protocol violation."""
        raise NotImplementedError("S3U2")

    @property
    def done(self) -> bool:
        raise NotImplementedError("S3U2")

    def result(self) -> Assignment:
        """The frozen decision; only once done."""
        raise NotImplementedError("S3U2")


def drive(search: Search, price_one: Callable[[PricingJob], Priced]) -> tuple[Assignment, tuple[Priced, ...]]:
    """The inline loop: next_jobs, price each job with price_one, feed, until done. Returns result() and every
    Priced it was given, in the order asked. The one driver of refit.assign and of the scripted tests."""
    used: list[Priced] = []
    while not search.done:
        jobs = search.next_jobs()
        if not jobs:
            raise RuntimeError("the search asked for no job while it is not done")
        got = [price_one(j) for j in jobs]
        used += got
        search.feed(got)
    return search.result(), tuple(used)
