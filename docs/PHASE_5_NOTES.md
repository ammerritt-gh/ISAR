# ISAR Phase 5 — Fully automated measurement (brief notes)

**Status:** Forward notes only. Not designed. The horizon the earlier phases are built toward.

**What it is.** The full reading of "act in *resolution* mode to solve a measurement problem":
Phase 4 *proposes* a measurement plan for a human to approve; Phase 5 **executes** it — the loop
runs measurement → reduction → analysis → next-measurement → instrument, autonomously, with the
human supervising rather than approving each step.

**Why it stays deferred (and gated).** A planner is only as good as the confidence it acts on
(`DESIGN.md` §15), and an *executor* is only as safe as the planner is trustworthy. Phase 5 is gated on
Phases 3–4 being scored and trustworthy on real data across instruments — autonomy multiplies the cost of
a wrong error bar or a mis-assigned peak, so it cannot precede a validated triage + planning loop. The
stated operating assumption that "the user is there to get things running" (the circularity guard in
Phase 3 §6) relaxes only here, and only behind hard supervision and abort paths.

**What it inherits, and the new problems it adds.** It inherits the whole spine: the deterministic
fit/error loop, the growing-point-set re-entrancy, the peeking-corrected stopping rule from Phase 4, the
computed confidence and the non-result buckets. It adds what none of the earlier phases touch:
instrument control and its safety envelope (motor limits, collision/exposure protection, sample
environment bounds), real-time scheduling against finite beam, fault detection and graceful abort, and a
supervision/override surface the human trusts enough to leave running. These are control-systems and
safety concerns, not analysis concerns — a different discipline bolted onto the analysis loop, which is
why they are out of scope until the analysis loop is proven.

**The single load-bearing inheritance.** Everything autonomous rests on the error bar being *right* — the
spine of `PHASE_3_DESIGN.md` §0.1. If Phase 3's uncertainty is trustworthy across instruments and
Phase 4's stopping rule is unbiased, Phase 5 is a control-and-safety problem on top of a sound loop. If it
isn't, Phase 5 cannot be attempted at all. That dependency is the reason the error layer got the design
budget it did.
