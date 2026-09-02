# Campaign authorization repair — program plan

> **Status:** live
> **Verified against code revision:** `e12dd0d` (2026-09-02)
> **Owner:** @ammerritt-gh · **Exit criteria:** §1 below
> **Evidence base:** [../HANDOFF-campaign-pipeline-audit.md](../HANDOFF-campaign-pipeline-audit.md)
> (the closed Codex audit, 34 findings F02–F35, refutations R01–R04, repair gates §8, and the
> ChatGPT second-opinion review in its Appendix A)

The audit's verdict stands: at `796a84b` the acquisition workflow around the deterministic fitting
core cannot prove that a frozen request, selected question, realized scan, evidence update,
completion decision, and neutron charge remain the same scientific action end to end. Sixteen of
eighteen production seams fail. This plan turns that evidence into ordered slices, each approved
on its own once its verifier seats are filled. It adopts the audit's gate order, adopts about two
thirds of the ChatGPT amendments, and rejects the rest as machinery beyond a single-operator research tool's need.
Every finding below carries a written disposition; every open decision is briefed for the owner
and settled in conversation, never in this file alone.

## 1. Envelope

**Outcome.** The controller may again be granted unattended physical-scan authority, first for
the targeted lane under supervision, then for discovery, each only after the corresponding gate's
exit battery has passed and every durable blocker it can raise has a declared default that is
safe unattended.

**Exit criteria.**

1. Gates 1–5 of the audit's §8, as amended in §4 below, each pass their exit battery on a
   recorded commit.
2. The `ARCHITECTURE.md` §15 row "Unattended physical-scan authority" flips from **Not
   certified** to the authority actually earned, with the gate that earned it named.
3. F14 and F15 are closed by the wire-level proofs the audit names, not by inference.
4. The durable content of this plan (constraints, rulings, acceptance layers) has moved into the
   owning design docs (`design/scene.md`, `design/steering.md`, `design/eig.md`,
   `design/reporting.md`) and this file is deleted in the same change.

**Non-goals.** Live TAVI reruns and sealed golden evidence stay out until a milestone names them
(S10). No compatibility readers for retired contracts (H1). No enterprise-grade acquisition
lifecycle unless the boring fix demonstrably fails its crash tests (D5).

**Tier and seats.** T3 program; each slice is a T2 branch with its own PR. The two outside
voices the tier asks for on the envelope are already spent — Codex authored the gates and ChatGPT
critiqued them — so no further envelope critique is bought. Per slice: fresh-context
`plan-verifier` before approval, `executor` for implementation, fresh `verifier` after, and a
Claude verifier for the pre-PR full-diff review. Slice 5 (EIG statistics) gets its own council
before it starts because its choices must survive the audience test (`DESIGN_GOALS.md`).

## 2. Constraints every slice inherits

- **Four reasons end a campaign** — user choice, campaign goal met, budget exhausted, machine
  failure (`DESIGN_GOALS.md` §Automated Scans; ruling D2 below). Every audit remedy phrased as
  "stop with a blocker" is implemented as a *non-ending* durable blocker or a non-blocking
  consultation with a declared default; the planner continues on whatever remains authoritative.
  "Machine-failure class" means what beam-off means in the design goals: acquisition holds and
  waits for the go-ahead; it is classified with machine failure for reporting and budgeting, and
  it ends the campaign only if the owner chooses to stop.
- **One analysis clock.** Planner and scene-EIG time *is* analysis time; it is timed, charged
  to the existing analysis budget, and interruptible by that ceiling (ruling D4). No second budget.
- **Replay determinism.** The scientific limit on any scoring computation is a deterministic work
  budget (blocks, samples, likelihood evaluations); wall time is only an emergency watchdog whose
  expiry yields a machine/analysis failure and no decision, never a partial winner.
- **Counts are authoritative; decisions are diagnostic.** How ISAR chose a scan is never a
  scientific claim (`DESIGN_GOALS.md` §Auditing). Replay and recovery machinery is sized to "no
  paid scan is lost or double-charged and no action is resubmitted unknowingly", not to forensic
  completeness.
- **The card is never written by code** (Hard Rule 1). Condition mismatch is reported and held,
  never reconciled by editing `card.policy.toml`.
- **No LLM in the runtime path of a number** (P1). Compiler repairs (F04) freeze explicit,
  provenance-labelled fields; they do not consult a model at run time.
- **No compatibility shims** (H1). A frozen payload that omits required policy is refused by
  name; the fix is a new schema revision, not read-time defaulting.
- **Scope.** Single operator, research code. Prefer the one-predicate fix in the shared function
  over a new data model; record the larger design as a requirement for the slice that owns it.

## 3. Root-cause groups

The audit's 34 records are nine problems. A finding's group decides its slice; its priority label
no longer does.

| Group | Findings | Slice |
|---|---|---|
| Log, replay and recovery contract | F09 F10 F11 F12 | 1 |
| Request validation and execution accounting | F17 F25 F26 F27 F31 | 1 |
| Condition-partition enforcement | F22 (with R02) | 1 |
| Cooperative stop, planner charging, honest minimal diagnostics | F18(a) F19 F21 F28 F34 | 2 |
| Targeted branch authority and frozen intent | F04 F05 F06 F13 F14 F20 F32 F33 F35 | 3 |
| Discovery product contract and phenomenon identity | F07 F08 F15 F16(immediate) F29 F30 | 4 |
| EIG comparability and neutron pricing (= SCENE-5.5) | F16(bound) F23 F24 | 5 |
| Scene-EIG performance | F18(b)–(e) | 6 |
| Full discovery reporting | F19 F28 F34 (remainder) | 7 |
| Corrections to the earlier calibration interpretation | F02 F03 R01 R03 R04 | 0 |

## 4. Slices

Order is the audit's dependency order: authorize and recover scans, define the product, repair the
statistics, then optimize, then make it legible. Slices 1 and 2 are independent of every open
decision except D5 and can start on approval.

### Slice 0 — docs only (this change)

Commit the closed ledger with its editorial pass (status gloss, appendix heading, second-pass
wording, F01 numbering note); this plan; the `ARCHITECTURE.md` §15 Gate 0 row; `.gitignore` for
`.audit_tmp_*`; the reading-guide links. No code. Acceptance: `tests/docs/test_doc_drift.py`
passes.

### Slice 1 — every purchased scan authorized, recoverable, correctly charged

- F09: the resume log reader fails loudly on a malformed *interior* line; at most one incomplete
  final record is recognized as a torn tail and is quarantined or truncated before any append;
  never append behind unparseable bytes.
- F10: persist and fsync the durable submission record before the derived legacy run event;
  counters and spend reduce exactly once from complete-or-recoverable state.
- F11: rebuild open prompt issuance (with its original delivery index) from the consultation event
  stream on reload.
- F12: pass the recorded attempt number through recovery finalization.
- F27: one finite / ordered / positive-step invariant at the `ScanProposal` → request boundary,
  reused by fresh, resumed and recovery proposals.
- F31: a recovered request must be newly authorized as a member of the frozen objective's request
  family (domain-contained, target-supporting on the exact snapped grid). A missing validation
  manifest means *unverified*, never permitted. If no valid member exists, the recovery attempt
  is abandoned, the current request is rejected, and the planner is asked for a new proposal or a
  consultation is raised — the campaign continues.
- F25: an executed-grid mismatch atomically marks EIG authority inapplicable and quarantines the
  row from target certification, through one predicate shared by runtime and the formal gate.
- F26: keep three facts — requested points, instrument-reported executed points, returned data
  points — and charge from authenticated execution evidence. Contradictory or absent evidence
  reserves the conservative upper bound and raises a non-ending blocker on further unattended
  acquisition pending reconciliation.
- F22: after the first arena and on every later condition change, compare the canonical
  condition set with the frozen declaration under the same binning policy. A mismatch is
  *machine-failure class* — the environment is not delivering the commanded condition — so
  acquisition holds and a consultation is issued with a declared default of *wait*, exactly as
  beam-off is handled. `card.policy.toml` is never rewritten.
- F17: compare canonical path-grid indices, or a scale-appropriate tolerance, in the coverage
  predicate; pin the 21-node equality boundary.

Exit battery: crash injection on both sides of every fsync boundary; inverted and
domain-escaping requests; returned-grid and count disagreement; missing-manifest recovery — all
fail closed with no extra physical submission and no double charge.

### Slice 2 — stoppable, charged, and visibly running

- F18(a): a checkpoint callback and deterministic work budget pass through `plan_next_action`,
  selection, scoring and each score block; stop and deadline are rechecked after planning and
  before POST. Planner time is charged to the analysis clock (D4). Planning publishes one
  decision-commit record after scoring, referencing the already-written immutable inventory,
  agenda, scores and selected intent, instead of moving several current pointers mid-flight.
- Planner timing and activity events are emitted so the wall split can see them.
- F19: discovery emits the canonical campaign-start event the GUI reducer already understands.
- F28: `isar diagnose` detects a discovery program and refuses with an honest "discovery
  diagnostics unsupported" message instead of rendering a false targeted report.
- F21: the wall split derives segment wall from durable start and terminal timestamps, subtracts
  only demonstrably non-overlapping spans, and renders the remainder as unattributed.
- F34: a terminal discovery step writes a small, honest "unsupported report" artifact until
  slice 7 lands the real one.

Exit: stop or deadline injected in every score block halts within one block without decision or
pointer publication; the retained reference log renders with planning visible and charged.

### Slice 3 — targeted-lane authority and frozen intent (pending D3, D6)

- F05: `rival_band` returns nothing under the same durable retirement predicate that already
  zeroes the rival's classification value.
- F06: one exact-origin gapless-acoustic observability guard shared by reconnect and narrow
  probe, independent of target-precision eligibility; a clean null durably suppresses the
  identical action fingerprint.
- F32: the declared same-Q production ceiling is an unconditional exclusion for ordinary
  production; further same-Q work needs a distinct, separately budgeted action kind with a
  durable reason.
- F35: branch-filter `evidence_master` under the same target identity as `master`, plus an
  invariant asserting target and evidence branch agree at summary, intent, proposal and replay
  boundaries. Per-branch evidence masters are recorded as a SCENE-6 design requirement.
- F04: the compiler refuses named-target commands that omit tolerance or use unsigned phrases
  and asks for the explicit value; provenance is labelled human / compiler-default / supervisor;
  the physical ±E domain derives deterministically from the intrinsic cap.
- F33: mint targeted step schema `/2` with the full required key set and refuse `/1` by name
  (H1). See D6.
- F20: validate `catalogue_profile` against the one live profile at compile and read boundaries
  and pass it through supervised fit invocation.
- F13: make every maintained O41/O43 passage state the implemented two-sided rule; add the
  doc-drift assertion.
- F14: one parameterized acoustic/optic production-shaped test in which a genuine target-core
  winner traverses comparison, proposal, adapter, validation and the submitted body. This is a
  proof obligation; the slice is not done without it.

The hypothesis lifecycle ChatGPT proposes (`observable_at`, active / resolved / retired /
reopened, completed null-search) is written into `design/scene.md` as a SCENE-6 requirement,
not built here.

### Slice 4 — the discovery product contract (pending D1)

- F30: bind the `phenomenon_map` deliverable to a typed terminal predicate; reject every
  unsupported deliverable kind. For the calibration objective, ALL requires connected acoustic and
  optic evidence through H=3 plus an explicit completeness/unknown decision.
- F07: persist a producer-sound interval per fitted mode keyed by candidate row and local mode
  index; the scene's existing component→phenomenon association consumes them. The fitter
  publishes anonymous local mode evidence and never pronounces identity. A joint-posterior or
  covariance reference is deferred until a consumer needs it.
- F08: hypothesis attachment lands here or discovery is declared class-agnostic for v1 with the
  tripwire kept as documentation — D1 decides.
- F16 (immediate): a question whose contributing belief cluster lacks certified posterior
  authority contributes no actionable EIG. If no authoritative question remains, a durable
  model-authority blocker and consultation are raised and the planner continues on any other
  authoritative work; the campaign does not end.
- F29 and F15: a branch-faithful two-branch world with acoustic-only and optic-only controls,
  aligned fixture epochs, literal windows and exposure executed; the narrow agenda-to-wire test
  closes F15. Acceptance is layered: fixed-Q fitter tests; deterministic planner worlds; a small
  literal-acquisition loop over about five Q nodes; the full 21-node calibration as milestone
  evidence.

### Slice 5 — EIG comparability and neutron pricing (this is SCENE-5.5)

An owner-reviewed statistical-method decision with its own grill and council before work starts.

- F24: detect exactly identical predictive distributions by canonical rate hash and force exact
  ties without Monte Carlo; then common random numbers keyed by the rate problem; paired
  uncertainty on the score *difference*.
- F23: selection order becomes — reject actions lacking required support; reject or defer
  below an absolute worthwhileness floor; compare scientifically distinguishable utilities;
  when indistinguishable, take lower realized cost; otherwise optimize a declared priced
  objective. Persist raw information and information per realized neutron; re-score after
  material grid changes.
- F16 (bound): the omitted-mass authority bound is a *certified-approximation contract* —
  written derivation per live factor type, property tests against exact enumeration on tractable
  scenes, one shared posterior-authority object consumed by predicates and EIG. No particular
  algorithm is promised.
- Fixed-total-K estimation is a calibration hypothesis validated against a small exact
  estimator kept as the oracle; a new frozen policy revision records revision, K, seed and
  backend.
- The `scene.md` slice table gains a SCENE-5.5 row when this slice is approved.

### Slice 6 — performance

After slice 5 freezes semantics: drop zero-mass and discarded-prediction work, cache rates across
questions, batch the likelihood with bounded memory, and add a bounded parallel design executor
only if the serial fixed-K path still misses the target. Gate: ≤30 s for a full decision at the
retained shape in three fresh runs; byte-stable replay across order and worker count;
cancellation within one block. The ≤2 s single-design microbenchmark is diagnostic scaffolding,
not a contract.

### Slice 7 — legible outcome

Discovery-aware diagnosis and terminal reporting dispatched for both campaign kinds, consuming
the acquisition and decision records slices 1 and 2 introduced — no separate reconciliation
model for diagnostics. Then the cheap re-run of the reference discovery goal, compared on rate
vectors rather than raw MI at decision one, which retires the scene-tuning baseline and feeds
SCENE-7.

## 5. Decisions

Settled decisions are rulings; open ones are briefed here and decided in conversation.

- **D2 — stop law. SETTLED 2026-09-02.** Only the four reasons end a campaign. The audit's
  "stop with a blocker" remedies (unauthoritative scene, condition mismatch, ambiguous execution
  count, invalid recovery, unavailable uncertainty) are implemented as non-ending blockers or
  consultations as written in §2 and §4; condition mismatch is machine-failure class.
- **D4 — planner time. SETTLED 2026-09-02.** Planner time *is* analysis time: one combined
  budget, charged and interruptible.
- **D1 — the product contract (Tier 3, open).** What "the phenomenon map is complete" means,
  coupled with whether declared acoustic/optic hypotheses attach to real fits (F08) and whether
  the fitter or the scene owns mode identity (F07). Recommended shape: the fitter publishes
  anonymous local modes, the scene associates them into persistent phenomena, attachment is
  structural/geometric matching rather than identity priors. Its own grill; does not block
  slices 0–3.
- **D3 — targeted lane versus SCENE-6 (Tier 2, open).** Four of slice 3's fixes live in the
  tracker SCENE-6 deletes. Recommended: land the one-predicate fixes now because the acoustic and
  optic calibration objectives run through that lane today, and record the lifecycle design as a
  SCENE-6 requirement. Deciding fact: whether any beamtime is expected before SCENE-6.
- **D5 — acquisition record (Tier 2, open).** Boring reorder (slice 1) versus one acquisition
  lifecycle keyed by acquisition id. Recommended: the reorder plus crash injection on both sides
  of every write; the lifecycle only if those tests find a hole the reorder cannot close.
- **D6 — frozen targeted payloads (Tier 2, open).** Mint `/2` and refuse `/1` by name, on the
  unverified assumption that no frozen `/1` program exists outside test workspaces. Verify before
  slice 3.
- **D7 — housekeeping (Tier 1, open).** (a) `HANDOFF-scene-tuning.md`: delete and absorb its
  surviving facts here (the audit reproduces its timing table and refutes its interpretation:
  F02, F03, R03, F24, plus the 200 K measured versus 300 K declared provenance) versus correct in
  place — recommended delete. (b) The audit's evidence-bearing optic workspace
  `.audit_tmp_f32_faithful_l7tr3cbw/` (988 files, hashes recorded in the ledger §9): keep or
  delete — the audit declined to delete it without the owner's word.
- **D8 — slice 5 method choices.** Scheduled as their own grill before slice 5.

## 6. Disposition ledger

FIX = adopt the audit remedy; FIX-min = the minimal form of a larger proposed remedy; FIX-amd =
the ChatGPT amendment adopted; PROOF = INCONCLUSIVE proof obligation; RECLASS = reclassified as a
correction, documentation or acceptance item; DEC = waits on a decision; DEFER = recorded, not
scheduled.

| ID | Disposition | Slice | Basis |
|---|---|---|---|
| F02 | RECLASS correction | 0, D7 | window prize is 1.72× for the two-branch truth |
| F03 | RECLASS correction; genuine-H23 world | 0 / 4 | the resolving −E scan was recovery |
| F04 | FIX | 3 | refuse ambiguous named-target commands; provenance labels |
| F05 | FIX-min | 3 | rival band retires with classification; lifecycle → SCENE-6 |
| F06 | FIX-min | 3 | shared observability guard; null fingerprint suppression |
| F07 | FIX-amd | 4 | per-mode intervals keyed by row + local index; scene associates |
| F08 | DEC D1 | 4 | attach, or declare class-agnostic with tripwire kept |
| F09 | FIX-amd | 1 | strict interior, one torn tail |
| F10 | FIX-min | 1 | reorder + derive; lifecycle only per D5 |
| F11 | FIX | 1 | prompt age from event stream |
| F12 | FIX | 1 | attempt passthrough |
| F13 | RECLASS documentation | 3 | O41/O43 passages + drift assertion |
| F14 | PROOF | 3 | real target-core winner to wire |
| F15 | PROOF | 4 | real narrow discovery action to wire |
| F16 | FIX-amd (immediate) / contract (bound) | 4 / 5 | no actionable EIG without authority; certified bound later |
| F17 | FIX | 1 | grid-index or tolerance comparison |
| F18 | FIX split | 2 / 6 | (a) stop, charge, work budget; (b)–(e) after slice 5 |
| F19 | FIX-min / full | 2 / 7 | canonical campaign-start event; rich monitor later |
| F20 | FIX | 3 | profile equality + passthrough |
| F21 | FIX | 2 | timestamp-derived wall with unattributed remainder |
| F22 | FIX-amd, per D2 | 1 | machine-failure-class hold + consultation, default wait |
| F23 | FIX-amd | 5 | exact tie → cost → placement; priced objective is SCENE-5.5 |
| F24 | FIX-amd | 5 | canonical-rate exact ties, then CRN, paired difference |
| F25 | FIX | 1 | mismatch → EIG inapplicable + quarantine, one predicate |
| F26 | FIX-amd, per D2 | 1 | three facts; ambiguous → non-ending blocker |
| F27 | FIX | 1 | one boundary validator reused by all producers |
| F28 | FIX-min / full | 2 / 7 | honest refusal; discovery adapter later |
| F29 | RECLASS acceptance gap | 4 | branch-faithful world; layered levels |
| F30 | DEC D1 | 4 | typed terminal predicate for the deliverable |
| F31 | FIX-amd | 1 | recovery re-authorized as family member; unverified ≠ permitted |
| F32 | FIX | 3 | unconditional ceiling; override is a separate action kind |
| F33 | DEC D6 | 3 | mint `/2`, refuse `/1` |
| F34 | FIX-min / full | 2 / 7 | honest artifact; typed dispatch later |
| F35 | FIX-min | 3 | branch filter + invariant; per-branch masters → SCENE-6 |
| R01 | recorded | — | truncation already qualified; caveat where raw numbers are quoted |
| R02 | recorded | 1 | condition mismatch is a visibility failure, not a scoring one |
| R03 | recorded | 4 | narrow generators exist; F15 proves the wire |
| R04 | recorded; doc drift | 4 | `ARCHITECTURE.md` out-of-window wording to be reconciled |

ChatGPT amendments **rejected** for this program: the single acquisition lifecycle as a first
move (D5); per-branch evidence masters now (SCENE-6); a one-time offline migration for `/1`
payloads (H1); the multimode covariance reference before a consumer exists; the ≤2 s microbench
as a permanent contract.

## 7. Verification discipline for this program

Targeted tests while iterating; one parallel not-slow run and one parallel full run at each slice
boundary; serial full run before merge to `main` (AGENTS.md §Verification). Every slice's PR body
names which review seats were filled and by whom. A slice may not be reported done while its
proof obligation (F14 in slice 3, F15 in slice 4) is open.
