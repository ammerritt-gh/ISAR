"""The LLM supervisor (LLM_HARNESS_DESIGN §2.1) — the ONLY place a language model sits.

It never picks routine points and never produces a number. It owns the judgment calls: goal →
objective spec (:mod:`goal_to_spec`), the stopping criterion (:mod:`stopping`), and exception
handling (:mod:`exceptions`) — plus warm-start layouts and renegotiation. All stubs this round.

When implemented, the LLMM HTTP transport is reached through the shared
:class:`isar.reduce.llm_client.Dispatcher` seam (the one analysis-side import the boundary rule
permits), so the supervisor stays offline-testable with a fake dispatcher.
"""
