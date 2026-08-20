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

1. The approved target map exists: `docs/ARCHITECTURE.md` law file, the `docs/design/*.md`
   living docs (final roster: scene, steering, eig, gui, reporting, plus STATS_PRIMER.md in
   place — two deviations from the approved seven, both recorded in the Frontier), the
   user-reference tier intact, plans in `docs/plans/`, DECISIONS/registers/campaign logs/archive
   absorbed and deleted.
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
- **Slice B (campaign logs) — DONE 2026-08-20 (`654b2ec`):** 19 narratives absorbed and
  deleted after per-file irreplaceability reads (two scouts, cross-checked against the ledger);
  sole-source facts rehomed (first-run accuracy → steering doc; HESSE mis-certification case →
  STATS_PRIMER §3.2; Phase-4 open questions → steering §8); campaigns/README.md rewritten as a
  live index of the retained frozen evidence bundles. Held for later slices: MVP_ZONE_RETEST +
  EIG_EXPOSURE_BASIS (absorb in slice C), PHASE_4_NOTES (archive slice).
- **Slice C (steering cluster) — DONE 2026-08-20 (`1d690f1`):** MEASUREMENT_STEERING.md →
  design/steering.md (spine; STEERING_DESIGN's A–C roadmap absorbed as §10; zone-retest's four
  branch-disambiguation rules absorbed as §7.9); EIG_STEERING_DESIGN.md → design/eig.md as its
  own living doc (deliberate deviation from the seven-doc map — merging 650 lines of EIG
  contract into steering would rebuild the monolith; owner-count grows to eight), absorbing the
  exposure-basis spike's evidence into its Predictive-artifact rationale.
- **Slice D (scene + plans) — DONE 2026-08-20 (`9f33b80`):** SCENE_DESIGN.md → design/scene.md;
  the four live plans → docs/plans/ (SCENE4_EXECUTION_PLAN.md, speedup.md, background.md,
  stats-audit.md), owner/exit-criteria now test-enforced there.
- **Slice E (gui/reporting/statistics) — DONE 2026-08-20:** GUI_DESIGN.md → design/gui.md;
  CAMPAIGN_DIAGNOSTIC_SPEC.md → design/reporting.md; **STATS_PRIMER.md deliberately stays at
  docs/STATS_PRIMER.md** as the statistics design doc — the frozen `assets/` FINDINGS reports
  cite that path in prose and ruling S10 forbids renewing them; its banner declares the role.
  Code comments citing the two moved docs repaired (comments only; gui suite green).
- **Slice F (architecture rename) — DONE 2026-08-20:** `DESIGN.md` → `docs/ARCHITECTURE.md` (git
  mv, section numbering unchanged); title and the file's own now-relative links repaired; the
  reference sweep repointed every other `.md` mention/link and every `.py` comment/docstring
  mention, per-location relative paths, `DESIGN §N` → `ARCHITECTURE §N`. **Ruled during this
  slice: `design/pipeline.md` and `design/instrument.md` are NOT created** — a full read showed
  ARCHITECTURE's §5–§6/§9 already sit at law altitude with interiors deliberately owned by
  module docstrings, WORKSPACE_AND_CLI.md, SCHEMA_LANDSCAPE.md, and design/steering.md; minting
  two more docs would be one-document-per-noun. The design/ roster closes at five
  (scene, steering, eig, gui, reporting) plus STATS_PRIMER in place. P/T/W-series ledger
  rulings absorb into ARCHITECTURE's own sections during the DECISIONS sweep.
- **Slice G (ledger retirement) — DONE 2026-08-20:** `DECISIONS.md` and the PRE_EIG3 handoff
  deleted. Every live citation converted to the self-contained form: "ruling ID" as a stable
  historical name, with GLOSSARY §Ruling IDs defining the system (content lives in the owning
  design doc; originals in git history). Load-bearing pointers rewritten individually (gui.md's
  G-series is now canonically homed there; scene.md records future H-rulings as dated
  amendments; SCENE4/background plans likewise; ARCHITECTURE's appendix and vocabulary sections
  self-contained). Py comment/docstring citations swept (29 sites, curated — `adjudicate.py`'s
  functional `DECISIONS` enum untouched). Deliberate leftovers: two *emitted* CLI/error strings
  (`isar/cli/main.py:567`, `isar/schema/campaign_program.py:65`) still say "(DECISIONS Hxx)" —
  their text may be test-pinned; the GLOSSARY entry makes them legible. Absorption-gap check
  rests on the living docs having been maintained as the contracts all along (verified for the
  load-bearing spots; the reader-digest absorption map is in the session record).
- **Slice H (archive) — DONE 2026-08-20 (`4506ee2`):** five rationale absorptions into the
  living docs (inverse crime + error-spine → STATS_PRIMER; mask-not-delete loop closure +
  lossless-store → ARCHITECTURE; peeking bias → design/steering.md §8); ~37 UNIFICATION
  docstring citations repointed (retired-plan + git-history form, current contract per module
  family); `docs/archive/` (4 files), `UNIFICATION_DESIGN.md`, `PHASE_4_NOTES.md` deleted.
- **Slice I (front door) — DONE 2026-08-20:** READING_GUIDE's map rewritten as the three doors
  (returning builder → user → contributor) plus a reference shelf; AGENTS routing rows trimmed
  to pure routing (capability status lives only in ARCHITECTURE §15).

## What remains before this handoff closes

1. **The walkthrough** (End condition #4): a session where the agent explains ISAR end to end
   using only these documents; the operator's confusion points become defects fixed on the
   spot. This doubles as the currency pass on the user-tier docs (USING_ISAR, WORKED_EXAMPLE,
   INTERPRETING_A_VERDICT, WORKSPACE_AND_CLI carry July/August verification dates and were
   reference-repaired, not re-verified line-by-line, during the rebuild).
2. **Operator homework:** the DESIGN_GOALS.md body rewrite (the Inbox is stocked, including the
   2026-08-20 interview entries); the closeout list below.
3. **DEFECT-4** (TODO.md): pre-existing float-exact assert in
   tests/session/test_branch_persist.py — the one standing test failure, unrelated to docs.

## Operator closeout list (bounded; shrinks only)

Open decisions harvested from the retiring DECISIONS.md ledger and this workstream. Each carries
its context so it can be ruled on cold. New questions do NOT join this list — they are decided
in-session with a full briefing, or documented in the owning design doc's roadmap.

1. **User-facing campaign report design.** ISAR's campaign diagnostic is contributor-facing;
   a report a *scientist user* reads after a campaign was explicitly deferred by you until
   post-testing. Ruling needed only when testing ends: what does the user-facing report owe its
   reader, and does the LLM narrative layer (planned in `docs/design/reporting.md`, unbuilt)
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
