# ISAR Phase 4 — Closing the measurement loop (brief notes)

**Status:** Forward notes only. Not designed. Recorded so Phase 3 is built *toward* it.

**What it is.** The first half of the floating R's "act in *resolution* mode." Phase 3 produces, per Q,
a fitted phonon with a decision-grade error, a computed confidence, and — where relevant — an
*underpowered* verdict ("expected ω here, needs ~N more counts") or a *fork* ("re-measure to separate
5 vs 5+8"). Phase 4 turns those into a **proposed next-measurement plan**: which Q points are
under-determined, where the dispersion is steep and under-sampled, which scans the confidence score says
need a repeat, and roughly how much more counting each needs to hit a target error. **Human-approved,
never auto-fired.**

**Why it's mostly already set up by Phase 3.** The planner's inputs are exactly the objects Phase 3 is
told to produce: the running dispersion prior (with confidence), the computed confidence (§9), the
non-result buckets (§6, especially *underpowered* with its detection-power estimate), and the correction
channel (`DESIGN.md` §10). Phase 4 is largely a consumer of these, plus a counts→error projection.

**The operating mode that shapes Phase 3.** "Refit everything after every point" is Phase 4's heart —
reduce wasted beam by watching the error shrink live. This is why Phase 3 must build the fit/error layer
**growing-point-set-native** (`PHASE_3_DESIGN.md` §11): a re-entrant pure function of points-so-far,
warm-started per point.

**The one trap to design against (already flagged in Phase 3 §11).** *Peeking / optional-stopping bias.*
Using the shrinking per-point error as a **stopping trigger** preferentially stops on downward
fluctuations, so stopped-early ω's carry a systematic pull. The live shrinking interval is a fine
*monitor*; as a *trigger* it needs a conservative margin or an alpha-spending-style correction. Phase 4
owns this correction; Phase 3 only has to carry the information needed to apply it.

**Open questions to inherit.** How to project counts→achieved-error reliably from a partial scan
(the bootstrap on the growing set gives this, but its early-point behaviour needs checking); how the
planner trades off "finish this Q" vs "start the next Q"; how human approval is presented fast enough to
matter mid-beamtime.
