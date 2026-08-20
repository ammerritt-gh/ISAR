# HANDOFF — documentation rebuild

> **Status:** live
> **Owner:** Adrian (@ammerritt-gh); executing agent rewrites this file to the current frontier
> **Exit criteria:** see *End condition* below; this file is deleted when it closes

Assume zero context. This is the rolling handoff for the 2026-08-20 documentation rebuild: ISAR's
documentation is migrating from the register/ledger system (DOC_INVENTORY, DECISIONS.md,
four-value lifecycles, tombstones) to the RP-style system now written into `AGENTS.md`
(three statuses, `docs/READING_GUIDE.md` as the single map, git as cold storage, living
`docs/design/<system>.md` docs that own their roadmaps, plans that die when work lands).

Receiving sessions: verify claims below against git/code before trusting them. The constitutional
rulings behind this rebuild were made by the operator in the 2026-08-20 planning session; the two
curation razors are recorded in `docs/DESIGN_GOALS.md` (Auditing section) and its Inbox:
**data must be completely auditable; ISAR's steering choices need not be** ("the scan is a
scan"), and **methodology choices must be defensible from the podium** (the audience test —
rationale for statistical-method choices is first-class; rationale for steering choices is not).

## End condition

1. The approved target map exists: slimmed `ARCHITECTURE.md`, seven `docs/design/*.md` living
   docs, user-reference tier intact, plans in `docs/plans/`, DECISIONS/registers/campaign
   logs/archive absorbed and deleted.
2. Every retained document is `live` or `draft`, on the map, and verified against code.
3. The operator closeout list below is empty or explicitly carried elsewhere by the operator.
4. **Acceptance gate:** a full walkthrough session — the agent explains ISAR end to end (parse →
   fit/verdict → steering → SCENE-4 discovery → campaign threshold) using only the rebuilt
   documents as script. Every reach past the docs is a defect fixed on the spot. Closes when the
   operator can defend the methodology choices from the podium.

## Frontier (rewrite this section as slices land)

- **Slice A (mechanical layer) — DONE 2026-08-20:** status vocabulary converted on all
  maintained docs (live/terminal); drift test retargeted (status banners, single-map coverage,
  links, stale phrases, CLI block, assets hashes); `DOC_INVENTORY.md`, `DECISION_INDEX.md`, the
  index generator and its test deleted; `DESIGN_GOAL_INBOX.md` merged into `DESIGN_GOALS.md`
  `## Inbox`; this handoff created.
- **Next — Slice B:** campaign logs (16 + README): irreplaceability read, rehome sole-source
  evidence, delete.
- **Then:** design clusters (steering → scene → statistics → pipeline/gui/reporting/instrument),
  each cluster created from its source docs *and* absorbing that domain's DECISIONS rulings in
  the same commit (final-state rulings only — the amendment chains are mapped in the 2026-08-20
  session's reader digest; re-derive from the ledger if lost). Then `DESIGN.md` →
  `ARCHITECTURE.md` slim-down; then `docs/archive/` absorption with the UNIFICATION docstring
  repair sweep (~40 `§N` citations, mech pass) before its deletion; last, the front-door rewrite
  of `READING_GUIDE.md` (three doors: returning builder, user, contributor), `GLOSSARY.md`
  vocabulary for escaped decision IDs (O41 authority, H1, …), `AGENTS.md` routing refresh, and a
  full reference sweep.

## Operator closeout list (bounded; shrinks only)

Open decisions harvested from the retiring DECISIONS.md ledger and this workstream. Each carries
its context so it can be ruled on cold. New questions do NOT join this list — they are decided
in-session with a full briefing, or documented in the owning design doc's roadmap.

1. **User-facing campaign report design.** ISAR's campaign diagnostic is contributor-facing;
   a report a *scientist user* reads after a campaign was explicitly deferred by you until
   post-testing. Ruling needed only when testing ends: what does the user-facing report owe its
   reader, and does the LLM narrative layer (planned in CAMPAIGN_DIAGNOSTIC_SPEC, unbuilt)
   participate?
2. **Renegotiation protocol (was O3).** When a campaign's declared objective proves unreachable
   mid-run (budget, physics), the driver currently stops honestly. A protocol for *renegotiating*
   the objective with the operator instead was named future work. Needs a design discussion, not
   a quick ruling.
3. **tas4d engine choice.** The 4-D resolution-convolution engine choice was left "decide when
   scheduled." TAVI is the single resolution authority (it supplies ellipsoids); the open half is
   what engine ISAR's tas4d convolution uses long-term.
4. **BACKGROUND_DESIGN fate.** Approved 2026-07-19; zero implementation since. It remains the
   named answer to the largest real-data risk (flat-constant backgrounds bias amplitudes — see
   STATS_PRIMER §7). Keep as a live plan, or descope until the SCENE program settles?
5. **N-mode growth policy (was H7).** How many DHO modes a catalogue candidate set may grow to is
   an open *physics* policy — priors and pricing exist, the growth ceiling does not.
6. **Acoustic/optic/unknown identity framing (was H4(b)).** Deferred to its own design
   discussion: identity currently *records* and branch assignment *drives*; the swap is
   SCENE-6-adjacent. Needs a discussion when SCENE-6 planning starts.
7. **Identity-conditioned prior payloads (was H18(c)).** Whether declared priors may condition on
   identity assignments. Open owner item from the question-layer build.
8. **Retroactive `profile/2` minting (was H19(g)).** Whether re-minting a versioned profile for
   already-persisted decisions is worth it for its own sake. Low stakes.
9. **Penalty re-anchoring (was O30).** MISSING_LOG_PENALTY / UNASSIGNED_LOG_PENALTY predate the
   uniform-baseline confidence scaling and were never re-anchored against it. Open work item —
   belongs to design/steering.md's roadmap once that doc exists; listed here so it is not lost in
   the ledger absorption.
10. **`calibration/README.md` fate.** A dated 2026-07-06 calibration record whose *sibling JSON*
    is loaded by `isar.drive.acquisition.calibration` at runtime. The JSON is live config; the
    README describing it is a dated record. Proposal: fold what the code-consumer needs into the
    owning design doc, keep the JSON, delete the README — confirm or overrule.
11. **`Science\AGENTS.md` retirement (cluster level, not ISAR).** Ruled 2026-08-20: no top-level
    Science repo. Both load-bearing warnings are now confirmed in repo floors: the ISAR↔PUFFIN
    variance trap is in both floors (ISAR Hard Rule 3; PUFFIN rule 3), and the TAVI consumer
    sweep list was promoted into TAVI's AGENTS.md Quick Reference (TAVI commit `c96b941c`,
    2026-08-20). Remaining: delete `Science\AGENTS.md` and `Science\CLAUDE.md` — **your action**,
    because the Science root is not a git repo, so this deletion has no history to recover from.
