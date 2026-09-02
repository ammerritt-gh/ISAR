# HANDOFF — automated campaign pipeline audit

> **Status:** live (audit closed 2026-09-02; remediation tracked in [plans/campaign-authorization-repair.md](plans/campaign-authorization-repair.md))
> **Owner:** Adrian (operator). Auditor: Codex (closed 2026-09-02); second-opinion review: ChatGPT (Appendix A)
> **Purpose:** accumulate the evidence-backed audit of objective compilation, scientific-action
> selection, scan-window realization, scene-EIG correctness, and campaign performance.
> **Exit criteria:** every production seam in §3 has a PASS / FAIL / INCONCLUSIVE disposition;
> the all-mode, acoustic-target, and optic-target calibration objectives have been exercised at
> the strongest available local level; every reported issue has a reproduction and second-pass
> recheck; and remaining unknowns name the evidence required to close them.

This is a working audit ledger, not a source of scientific authority. It records what was actually
checked, against which revision and artifacts, so candidate problems cannot quietly become findings.
No source-code or configuration changes belong to this workstream.

## Executive outcome

**Overall verdict: not certified for unattended, beam-efficient operation on the requested
calibration objectives.** The deterministic reduction/fitting core can resolve the scientifically
important zone-boundary spectrum, and several lower-level contracts work correctly. The failure is
the acquisition workflow around that core: it cannot yet prove that a frozen request, selected
question, realized scan, evidence update, completion decision, and neutron charge remain the same
scientific action end to end.

Final disposition count: **34 issue records = 32 FAIL + 2 INCONCLUSIVE (F14/F15)**, alongside four
explicitly refuted hypotheses (R01–R04). Of 18 production seams, 16 fail and two pass (S03 objective/
hypothesis resolution and S12 instrument-grid adaptation). A FAIL count is not a count of 32
independent root causes: several records are distinct consequences or acceptance/reporting gaps
which the repair gates intentionally consolidate.

- **ALL objective:** the exact two-mode fitter recovers 6.086 and 8.060 meV at H=3 with posterior
  0.99999975, and question-conditioned narrow/mirrored windows are legitimate. But the frozen
  deliverable has no runtime consumer, declared acoustic/optic identities do not attach to real fit
  evidence, a true two-mode fit lacks the per-mode intervals required by the agenda, and the strongest
  closed-loop harness ignores selected windows/counts. A frame-compatible exact-truth run remained
  honestly open after eight observations and ten minutes without selecting H=3. False completion was
  not observed; endpoint completion and neutron economy remain unproved.
- **Acoustic-only objective:** compilation, exact-origin exclusion, and wrong-branch completion
  isolation are sound. Neutron-efficient completion is not certified: resolved optic rivals can keep
  widening scans, a real target-core winner has not been proven through the instrument request, and
  recovery can leave the authorized energy domain. A confirmed target which has reached its declared
  12-batch same-Q ceiling remains eligible for another production scan, and pooled optic evidence can
  overwrite acoustic decision authority. The exact full-line end-to-end outcome remains unproved,
  but these two acquisition-rule failures are independently reproduced.
- **Optic-only objective:** the bounded synthetic tracker reached 21/21 Q seats with preserved optic
  branch identity. It did so through the broad legacy 0.4…12 meV windows, not a demonstrated O43
  target-core request, and it can repeatedly buy an acoustic-origin probe where the acoustic target
  is structurally unobservable. Completion logic is stronger than its demonstrated efficiency.
- **Performance:** authenticated retained timing confirms 20,625 s of late planner work versus
  1,969 s of reduction/fitting—91.2% versus 8.7% of reconstructed wall time. A single late
  4,096-alternative design took 165.2 s; full decisions took 2,436–4,830 s while using roughly one
  effective core. Planning is uncharged, cannot be interrupted by its analysis ceiling, and can be
  followed by one more physical scan after a stop arrives.
- **Neutron selection:** discovery maximizes raw noisy MI, not information per realized neutron. A
  21-point window beat a 5-point window with identical MI despite costing 4.2× more, and identical
  cold-start predictions were ordered by independent Monte-Carlo noise. The retained handoff's
  claimed ~4× ALL-window saving also omitted the optic branch; the objective-correct point reduction
  is 1.72× for the tested zero-anchored family (single-branch windows remain about 3.5×).
- **Operational integrity:** invalid/inverted requests, returned-grid/count disagreement, crash
  windows, silent log-line loss, optional-manifest recovery checks, invisible discovery state, and
  incorrect/missing terminal diagnostics prevent a safe unattended authorization even before the
  science-efficiency gaps are considered.

The safe present use is supervised dry-run/proposal review and deterministic post-acquisition
analysis. Do not grant either discovery or targeted recovery unattended physical-scan authority for
these objectives until the request/replay/stop gate in §8 passes. This recommendation does not imply
that existing fitted results are invalid; it limits what the controller may autonomously buy next.

## 1. Audit contract

- **Stable readiness unit:** `ISAR-CAMPAIGN-AUDIT-01` (plan revision R2, reviewed `READY` in a
  second Codex context before execution).
- **Audit start:** 2026-09-01.
- **Current source at start:** `796a84b`.
- **Pre-audit worktree:** no tracked or untracked entries reported by `git status --short`; Git
  emitted only a sandbox warning while trying to read the user-level excludes file.
- **Reference campaign:** `test_workspaces/2026-09-01-llmm-role/ws-discovery`, declared by
  `HANDOFF-scene-tuning.md` as a run of source `97ee835`. Its retained artifacts passed the §4
  authentication gate; the workspace-to-commit association remains documentary rather than
  cryptographically embedded.
- **Allowed work:** read-only inspection; deterministic local tests and probes; disposable copies
  of non-golden retained evidence; bounded profiling with hard wall limits; edits to this ledger
  and its reading-guide link.
- **Forbidden work:** Python/source/configuration edits; live TAVI activity; sealed golden evidence;
  external mutation; dependency installation; commits; a six-hour campaign rerun without a new
  decision gate.

### Evidence and disposition rules

Every material claim ends in exactly one state:

- **PASS:** governing contract + static production trace + dynamic production-seam evidence (or an
  authenticated retained execution) agree.
- **FAIL:** expected and actual behavior differ in a reproducible dynamic check, corroborated by an
  independent trace or test. A TODO, xfail, or suspicious source shape alone is not a finding.
- **INCONCLUSIVE:** the required evidence is unavailable or the bounded probe budget is exhausted;
  the missing evidence and retry condition are recorded.

Measurements record source identity, environment, timeout, repetitions, median/range where relevant,
and whether the result is calibration-specific or synthetic. Findings use P0–P4 priority, confidence,
exact evidence, expected/actual behavior, impact, remedy options, and a recheck.

## 2. Calibration decision oracle

The same words can otherwise hide three different contracts. These are the pass/fail semantics used
throughout this audit.

### All phenomena

`discover_and_map` over **Q = (2,0,0) … (3,0,0)** and intrinsic **0 … 10 meV**. Completion requires
both acoustic and optic tracks plus the declared completeness/unknown-policy questions. A one-branch
truth cannot prove this objective.

Q placement passes when endpoints and enough connected interior coverage map both tracks, every
repeat or off-path action has a durable open-question/corroboration/recovery purpose, and lexical
fallback is used only after a recorded software fault. An individual action may be conditioned on
one ready question; it need not remeasure every other open mode on that scan. Its window passes when
it retains the complete support required by the selected question and no narrower authorized
contiguous instrument grid retains that support with fewer points. Campaign completion, rather than
every individual action, must still account for both tracks and the completeness/unknown policy.

### Acoustic target

The frozen targeted objective names `target_branch=acoustic`. “Ignore optic” means optic evidence may
remain temporarily as a rival needed to establish identity, but optic precision cannot satisfy the
target, seat completion, or continue consuming Q/window coverage after differentiation. For a
gapless acoustic branch, (2,0,0) is not a precision seat; every other declared grid Q is.

The executed window must retain acoustic support. The authoritative window choice must follow the
restricted O41 evidence profile as amended by O43: after all structural gates pass, a statistically
indistinguishable result (`|delta| <= t(df=7, 3-sigma two-sided tail) * SE`) or a partition-offset
disagreement is broken by executed cost, which selects the strictly smaller target core; outside that
band the sign of `delta` decides. Evidence unavailability or any failed structural gate retains legacy.

### Optic target

The frozen targeted objective names `target_branch=optic`; acoustic is rival-only under the same
rule. The finite-energy optic mode at (2,0,0) is a valid precision seat. Window support and O41
selection obey the same contract.

### Efficiency and performance

- An exact repeated scientific action is efficient only when its durable purpose is corroboration,
  bridge, recovery, or added exposure.
- Window support is an invariant, not a percentage. For comparison with the retained campaign the
  secondary predeclared metric is the number of executed points within ±1 meV of each relevant truth
  mode. A window is called inefficient only when a strictly dominating authorized instrument grid
  can be demonstrated.
- Planner work must be timed, charged to a declared analysis clock, and interruptible by its ceiling.
- `docs/design/steering.md` §3 declares a 10–30 s real-tier decision target. A same-state median at or
  below 30 s passes that target; above 30 s fails it; a timeout supplies only a lower bound.
- In the retained run, planner work at or below 50% of controllable wall time passes the deliberately
  loose non-domination check; above 50% fails. The exact fraction is always reported, without treating
  deterministic TAVI timing as real beam timing.
- CPU utilization is diagnostic, not an invented threshold. High-performance execution passes if the
  latency target is met or bounded scaling demonstrates that it is reachable on the 32-thread
  baseline.

## 3. Production-seam matrix

| ID | Production seam | Static trace | Dynamic evidence | State |
|---|---|---|---|---|
| S01 | Natural-language goal compiler | source and schema traced | 101 focused tests + deterministic/live three-intent and unregistered-profile probes | FAIL (F04, F20) |
| S02 | Campaign-program schema and resume | frozen-program, deliverable, counters, consultation, and recovery paths traced | 7 focused crash/resume probes, deliverable A/B/C, and incomplete-frozen-target probe | FAIL (F09–F12, F30, F33) |
| S03 | Objective and hypothesis resolver | target/rival/default and completion-isolation paths traced | three-intent, full-line, and wrong-branch probes | PASS |
| S04 | Targeted action planner and stopping | target/rival retirement, reconnect, probe, progress, and stopping paths traced | 56 focused tests plus resolved-rival, exact-origin, tracked-world, same-Q-ceiling, and evidence-authority probes | FAIL (F05, F06, F32, F35) |
| S05 | Discovery cold start | declared partition, opening book, profile, and first-scene transition traced | focused suite, retained first decision, condition/profile mismatch probes | FAIL (F20, F22) |
| S06 | Scene agenda and question policy | opener, predicate, dependency, completion, and exhaustion paths traced | 122 focused tests, two-mode/deliverable A/Bs, and the shipped FCC challenge run | FAIL (F07, F29, F30) |
| S07 | Candidate generators and Q placement | readiness, full/narrow window, path-spacing, and exhaustion paths traced | full 21-node boundary plus generator probe | FAIL (F17) |
| S08 | Scene build, evidence, linkage, enumeration | candidate product, factor bounds, cap, linkage, and authority traced | exact retained cap transition plus 2/4/6-candidate scaling worlds | FAIL (F16) |
| S09 | Scene-EIG scoring and fallback | belief, condition partition, authority, hypothesis, estimator, cost, and fallback paths traced | retained temperature/cap attribution, 720/4,096 profiles, hypothesis A/B, equal-rate seed and equal-MI cost controls | FAIL (F08, F16, F18, F23, F24); temperature claim REFUTED (R02) |
| S10 | Targeted physical windows and O41/O43 selection | complete source/test trace; real target swap unproved | 234 focused tests plus branch worlds, ceiling/authority probes, and exhaustive recovery-support replay | FAIL (F13, F31, F32, F35); F14 remains INCONCLUSIVE |
| S11 | Discovery intent/window realization | generator/realizer source and focused tests traced | narrow/full-domain generator matrix plus equal-MI broad/narrow pair; no production narrow-wire test | FAIL (F23); production wire proof remains INCONCLUSIVE (F15) |
| S12 | Instrument grid adapter | `%g` wire, TAVI count/rounding, returned-axis lineage, and point-cap paths traced | 8 focused tests plus exact returned-axis and cap-edge probes | PASS |
| S13 | Geometry feasibility and request realization | feasibility trim, target-run preference, proposal validation, and recovery paths traced | trim/recenter/cap composite, inverted-range POST, and two-recovery target-support probes | FAIL (F27, F31) |
| S14 | Submission, returned-grid reconciliation, budget charge | durability ordering, executed-grid comparison, and executed-count fallbacks traced | two-fsync crashes, shifted/nonuniform grids, and short-result accounting | FAIL (F10, F25, F26) |
| S15 | Fitting, pooling, certification, scene hook | profile/enumeration, returned-axis, pooling, and candidate interval contracts traced | retained rows, unregistered profile, two-mode interval A/B, mismatched-grid collation, and pooled-branch authority A/B | FAIL (F07, F20, F25, F35) |
| S16 | Analysis/measurement clocks, stop, exhaustion | analysis operation, discovery call ordering, and stop-token boundaries traced | recovered attempt plus retained final-score/stop timeline | FAIL (F12, F18) |
| S17 | Persistence, replay, recovery | log reader, unmatched submission, counters, prompts, fingerprint progress, and recovery traced | corruption, crash-window, prompt-restart, repeated-action, authority-identity, and recenter probes | FAIL (F09–F12, F31, F32, F35) |
| S18 | Operator reports and diagnostics | discovery segment, condition provenance, automatic-render, diagnostic JSONL, status/report, and wall-split contracts traced | retained log through GUI/timing reducers, two disposable `isar diagnose` renders, source dispatch, and 300/200 join | FAIL (F09, F19, F21, F22, F28, F34) |

`PENDING` is ledger state only. Final seam dispositions use PASS / FAIL / INCONCLUSIVE.

## 4. Retained-campaign authentication gate

Before the retained campaign supports any finding, capture a SHA-256 manifest and verify:

1. local Git contains declared source commit `97ee835`;
2. `card.policy.toml`, `card.state.json`, `session.json`, `data/`, `report/program/`,
   `decisions.jsonl`, `measurements.jsonl`, `session_log.jsonl`, `run_timings.json`, and `scene/`
   exist and parse under their declared formats;
3. the frozen program contains the reference goal, Q region, and 0…10 meV domain;
4. delivery/decision identities are sequential and the current scene pointer names an existing
   digest;
5. scan counts/windows and timing totals reconcile with the handoff's 22,636 s wall,
   20,625 s planner, and 1,969 s fitting figures, allowing only the handoff's stated rounding and
   derivation;
6. profiling occurs only on a disposable authenticated copy, followed by a re-hash of the original.

Failure predetermines calibration-specific **INCONCLUSIVE**. Synthetic/world evidence may still
close general production seams.

### Gate result — PASS with one provenance limitation

- Aggregate pre-probe manifest: **153 files**, **49,485,548 bytes**, SHA-256
  `8e96dbc49715f0339c6b40ee58532c30e5bc2650ded2b557f5b26e0de46760ca`.
- Format validation: 142 JSON files, 7 JSONL files (494 nonblank records), and 1 TOML file parsed
  without error.
- Program, scene, and agenda current pointers all resolve; in every case the target file's raw
  SHA-256 exactly equals the pointer digest. Current program `8d7e8820…e4ef` contains the exact
  reference goal, Q endpoints, 0…10 meV domain, default catalogue, `include_unknown=true`, and the
  `phenomenon_map` deliverable. Current scene is `ef6b2bf1…655`.
- Local Git contains full commit `97ee8355923b3969db40adc19931ba9e43adb385`. The workspace does
  not itself embed that source revision, so the workspace→commit association remains documentary
  provenance from `HANDOFF-scene-tuning.md`, not a cryptographic assertion.
- The log contains 10 discovery decisions, 10 proposals, 11 physical scans, 10 completed analysis
  results plus one analysis-limit failure, and a terminal logical-delivery count of 9. The unequal
  counts are expected: one logical delivery performed a second physical missed-window attempt and
  the final selected proposal failed before becoming a completed delivery (F03).

Independent timing reconstruction from epoch timestamps and timing records:

| Quantity | Reconstructed seconds | Handoff value | Result |
|---|---:|---:|---|
| first event → terminal event | 22,635.589 | 22,636 | agrees by rounding |
| 11 `isar_run` records | 1,969.426 | 1,969 | agrees by rounding |
| 10 `scene_build` records | 27.434 | 27 | agrees by rounding |
| 11 `wait_scan` records | 2.983 | 3.0 | agrees by rounding |
| large scene→proposal gaps, iterations 4–9 | 20,625.065 | 20,625 | agrees by rounding |
| all ten scene→proposal gaps | 20,633.568 | not separately stated | first four add 8.503 s |

The analysis ledger contains eleven stable operation IDs whose maximum elapsed times sum to
1,969.095 s: ten completed, then iteration 9 failed after the cumulative 1,800 s ceiling was
crossed. This confirms that fitting was charged and planner gaps were not. Profiling ran only on an
authenticated disposable copy. A second, domain-separated binary full-tree manifest matched the
original-before, copy-before, copy-after, and original-after states exactly: 153 files,
49,485,548 bytes, aggregate `d2e99d4d…a6370e`. That digest is deliberately non-comparable with the
textual pre-probe aggregate `8e96dbc4…760ca`; equality of every path, length, and content hash—not
coincidental aggregate equality—proves the retained original was unchanged.

## 5. Candidate hypotheses and their dispositions

These hypotheses came from the calibration handoff and first static reconnaissance. They remain
unreported until the evidence rule in §1 is met.

| ID | Hypothesis | Required reproduction |
|---|---|---|
| H-A | **CONFIRMED (F18):** discovery planner work is invisible to the analysis clock and cannot be interrupted by its limit. | Exact call/clock/stop trace plus retained terminal iteration. |
| H-B | **REFUTED (R03; F15 remains):** discovery has real question-conditioned narrow generators; their agenda-to-wire flow lacks one integration proof. | Generator matrix, realizer, and production submission coverage trace. |
| H-C | **REFUTED (R01):** unbounded assignment truncation is presented as ordinary near-certain posterior mass. | Stored payload through workspace/report operator surfaces, not only internal flags. |
| H-D | **CONFIRMED (F08):** declared acoustic/optic hypotheses cannot affect real-fit-derived discovery placement because identities are unattached. | Production-shaped A/B world or the named strict-xfail tripwire plus downstream trace. |
| H-E | **CONFIRMED (F18):** quadratic single-core 4,096-alternative MI scoring causes the retained 91% planner share. | Profile and factor-isolated scaling measurements. |
| H-F | **CONFIRMED (F05, F06):** acoustic-only/optic-only can retain a scientifically retired or structurally unobservable rival in later acquisition work. | Representative frozen objectives and dynamic selected-window/action sequences. |
| H-G | **CONFIRMED (F20):** a frozen discovery catalogue profile is neither registry-validated nor passed to production fit enumeration. | Unregistered program/profile probe plus wrapper/call-chain trace. |
| H-H | **REFUTED (R02):** the retained card/measurement mismatch does not make scene-EIG score later decisions at 300 K. | Join every persisted intent partition to its scene footprint and inspect all reconstructed designs. |
| H-I | **CONFIRMED (F02):** the calibration's single-mode useful-point calculation materially overstates the benefit of narrowing an all-mode discovery window. | Two-branch TAVI truth, retained fitted modes, and objective-aware window dominance calculation. |
| H-J | **CONFIRMED (F03):** the resolving −E scan was recovery, not the scene-EIG corroboration action claimed by the handoff. | Decision→proposal→attempt join and recovery event. |
| H-K | **CONFIRMED (F16):** the 4,096 assignment cap makes the reference ALL campaign authority-unresolved before line completion, with no scientific offramp. | Exact retained transition plus bounded candidate-count worlds and agenda/generator trace. |
| H-L | **CONFIRMED (F17):** the exact 0.05-r.l.u. path grid remains coverage-incomplete because raw float gaps exceed the declared spacing. | Full 21-node equality-boundary predicate and generator probe. |
| H-M | **CONFIRMED (F22):** the delivered measurement condition can violate the declared cold-start partition without suspending the campaign. | Retained 300 K declaration→200 K arena transition plus integration-contract trace. |
| H-N | **CONFIRMED (F23):** discovery ranks raw MI rather than information per neutron and can prefer a strictly dominated broad window. | Equal-information broad/narrow production-scorer pair plus cost and planner trace. |
| H-O | **CONFIRMED (F24):** the retained first-decision Q ranking is Monte-Carlo seed noise, not physical discrimination. | Exact predictive-rate equality, draw-count/seed controls, and retained placement join. |
| H-P | **CONFIRMED (F25):** an executed-grid mismatch is detected but remains canonical and EIG-applicable at runtime. | Shifted/nonuniform grids through comparison metadata and collation gates. |
| H-Q | **CONFIRMED (F26):** an ambiguously short returned grid is charged at the requested point count. | Six-returned-versus-ten-requested execution-count and budget-reconciliation probe. |
| H-R | **CONFIRMED (F27):** an inverted proposal survives ISAR's pre-submit guards. | Exact proposal→body probe plus current TAVI parser refusal. |
| H-S | **CONFIRMED AS AN ACCEPTANCE-EVIDENCE FAILURE (F29); exact-world runtime outcome INCONCLUSIVE:** generic coverage completes on interior nodes, the shipped FCC run never measures H=3, and its epoch-mismatched fixture disables the exact-anchor route. A frame-compatible exact-truth run stayed honestly open but did not select H=3 before its ten-minute bound. | Exact empty-scene inventory, compatible-frame agenda control, shipped FCC closed loop, and authenticated eight-observation exact-truth prefix. |
| H-T | **CONFIRMED (F28):** the architect campaign diagnostic reads a valid discovery program as an unrecorded/crashed targeted campaign. | `isar diagnose` on an authenticated disposable copy of the retained campaign. |
| H-U | **CONFIRMED (F29):** the shipped FCC challenge battery does not represent or assert the requested two-branch endpoint map. | World truth, selected-Q sequence, agenda fixture, and test-assertion trace. |
| H-V | **REFUTED (R04):** a narrow scan does not turn an out-of-window rival into negative evidence in the scene. | Footprint visibility, presence scoring, and contract tests. |
| H-W | **CONFIRMED (F30):** the frozen discovery deliverable has no effect on agenda or completion semantics. | Full call-site map plus phenomenon/empty/optic-dispersion agenda A/B/C. |
| H-X | **CONFIRMED THROUGH PHYSICAL SUBMISSION (F31):** missed-window recovery can leave the authorized domain; continued edge recovery also removes target support, which the real loop refuses only when validation includes a point manifest. | Exhaustive recovery math over 20 acoustic and 21 optic points plus production-loop mock-client submission replay with and without manifest evidence. |
| H-Y | **CONFIRMED BY THE REAL SELECTOR (F32):** `maximum_production_batches_per_q` is reported as reached but does not exclude a confirmed, still-unconverged target from another same-Q production action. | Production-shaped confirmed-master `_select_target` probe at exactly 12/12 batches, plus source predicate trace. |
| H-Z | **CONFIRMED (F33):** an incomplete payload naming the frozen targeted schema acquires current read-time defaults instead of being refused. | `CampaignProgram.from_dict` → `spec_from_dict` A/B against compact authoring. |
| H-AA | **CONFIRMED (F34):** a terminal discovery step emits no automatic campaign report, and manual diagnosis then misclassifies it under the targeted schema. | Retained pre-render artifact inventory, terminal event, renderer dispatch trace, and independent disposable render. |
| H-AB | **CONFIRMED (F35):** pooled evidence from the rival branch can overwrite target evidence authority, flip Q selection, and change the scientific-action identity. | Acoustic/optic pooled-row artifact plus filtered-versus-overwritten real-selector A/B. |

## 6. Audit budget and command log

Program ceiling: **17.5 audit-hours / 80 substantive commands**. At 75%, record the remaining budget.
Lower slice caps win: A0 0.5 h/4 commands; compiler 1 h/8; Q placement 2 h/12; windows
2 h/12; scene correctness 2.5 h/14; performance 3.5 h/16; end-to-end/recovery 3 h/14;
finding investigation 2 h (original + at most two materially different rechecks per finding);
fresh verification 1 h (one pass and one evidence-changing retry). The retained late-decision
attempt is capped at 10 minutes; other individual probes are capped at 10 minutes and routine tests
target one minute.

When a slice exhausts its budget without changing a disposition, mark the seam INCONCLUSIVE with the
missing evidence and retry condition; do not keep probing.

| Seq | Slice | Command/probe | Limit | Result | Elapsed |
|---:|---|---|---:|---|---:|
| 1 | A0 | `git status --short`; `git rev-parse --short HEAD` | 10 s | clean entries; `796a84b` | <1 s |
| 2 | A0 | retained-workspace existence and top-level inventory | 10 s | required top-level directories exist; authentication pending | <1 s |
| 3 | discovery | governing-doc reader pass over seven routed documents | 10 min | contract brief received; no disposition | 93 s |
| 4 | A0 | retained SHA-256 manifest plus JSON/JSONL/TOML validation | 60 s | PASS; 153 files, no parse failures | <1 s |
| 5 | A0 | program/decision/measurement and session-timing reconciliation | 5 min | PASS with source-link caveat; three new discrepancies opened | 150 s |
| 6 | A0 | pointer target/raw-digest verification | 10 s | PASS for program, scene, agenda | <1 s |
| 7 | A3 | two-branch truth and ±1 meV useful-point recomputation | 60 s | handoff's single-mode metric REFUTED | <1 s |
| 8 | A3 | iteration-6 proposal/submission/recovery join | 60 s | handoff's H23 attribution REFUTED | <1 s |
| 9 | A1 | compiler/source reader pass over objective and program contracts | 10 min | three compile lanes and duplicate fallback located | 75 s |
| 10 | A1 | focused compiler/schema suite with isolated temporary root | 60 s | 101 passed | 1.25 s |
| 11 | A1 | deterministic ALL/ACOUSTIC/OPTIC `goal_to_program` probe | 60 s | exact frozen intents reproduced; S01 PASS | 0.96 s |
| 12 | A2 | O44/frontier/stopping source-and-test reader pass | 10 min | branch-specific origin invariant traced | 75 s |
| 13 | A2 | full-line tracker probe + O44/stopping/frontier suites | 60 s | 21-point eligibility reproduced; 110 passed | 1.12 s |
| 14 | A1 | live LLMM inventory + two deployed-supervisor batches | 2 min | named-target range/tolerance translation varied; F04 | 12 s |
| 15 | A2 | resolved-rival provider/window and optic-origin reconnect probes | 60 s | both H-F failure mechanisms reproduced | <1 s |
| 16 | A2 | O44/rival-generator/merge/replay suite plus repeated optic-origin probe | 60 s | 56 passed; identical auxiliary probe re-emitted | 2.33 s |
| 17 | A4 | delegated scene/agenda/generator/scorer source pass | 10 min | 50 files; completion and identity candidates isolated | 122 s |
| 18 | A4 | agenda/predicate/contract focused suite | 60 s | 122 passed | 1.52 s |
| 19 | A4 | production-shaped two-mode agenda and refinement probes | 60 s | both mode-precision questions remain open; same physical actions recur | <1 s |
| 20 | A4 | real-fit declared-acoustic hypothesis A/B tripwire | 3 min | expected strict XFAIL; placements byte-identical | 67.65 s |
| 21 | A6 | focused replay/recovery adjudication suite | 60 s | 7 passed; four production-shaped failure states reproduced | 4.22 s |
| 22 | A3 | six targeted-window/O41 authority, realization, fallback, generator, and agenda runs | 60 s test / 120 s session | 234 passed, 0 skipped; source path complete, no real target-core wire case | 9.61 s cumulative pytest |
| 23 | A4 | retained intent-partition→footprint→design temperature join and counterfactual | 60 s | 19 cold designs `None`; 184 later designs 200 K; zero at 300 K | <1 s join; counterfactual included in profile run |
| 24 | A6 | retained discovery log through `CampaignViewModel` reducer/readouts | 60 s | 431 events and 11 scans render as no campaign / idle / no limits | 1.3 s |
| 25 | A4 | scene build/predicate/agenda/EIG/planner suite | 60 s test / 120 s session | 256 passed, 0 skipped | 4.49 s wrapper |
| 26 | A4 | live-factor/generator/exhaustion suite | 60 s test / 120 s session | 139 passed, 0 skipped | 2.04 s wrapper |
| 27 | A4 | retained cap transition, candidate-scaling worlds, exhaustion, and score-attribution probes | 60 s each | F16 reproduced; 12×2 exact, 13×2 unresolved | 8.65 s live-factor batch; other joins <1 s each |
| 28 | A4 | exact 21-node 0.05-r.l.u. predicate and generator probes | 60 s | OPEN at rounded 0.05; eight false surveys | 4.2 s predicate; 4.1 s generator |
| 29 | A5 | retained 720-alternative one-design replay and cProfile | 60 s | 3.442 s direct; `_mi_from_rates`/logsumexp dominate | 4.397 s profiled |
| 30 | A5 | retained 4,096-alternative one-design replay on disposable copy | 240 s | 165.202 s wall, 185.75 s CPU; 1.124 effective cores | 165.202 s |
| 31 | A5 | SCENE-EIG, same-action scorer, and deadline-boundary tests | 30 s tests / 120 s session | 81 passed | 2.85 s cumulative pytest |
| 32 | A5 | same-action window-EIG profile/scaling | 60 s | representative 0.928 s; 384 scenarios/200 points 0.421 s | <1 s per case |
| 33 | A4 | unregistered catalogue-profile compile/placement/fit-enumeration probe | 60 s | arbitrary profile accepted, flat placement, production fit wrapper has no profile | 1.6 s |
| 34 | A6 | retained log through diagnostic `_wall_split` | 60 s | only 8.84% of epoch wall represented; fitting reported 98.44% instead of 8.70% | 1.2 s |
| 35 | A6 | delegated adapter/feasibility/reconciliation verification plus focused grid suite | 60 s tests / 10 min task | 8 passed; returned-axis lineage and safe over-cap refusal passed; F25–F27 reproduced | 7.32 s tests |
| 36 | A4 | exact and truncated retained-scene link→two-branch-truth join | 60 s | every stored top-alternative link stayed within acoustic or optic truth; no cross-branch link found | 1.1 s |
| 37 | A4 | equal-MI broad/narrow discovery-score control | 60 s | both 0.693147181 nats; 0…10 meV/21-point design beats 7…9 meV/5-point design by hash despite 4.2× cost | 1.1 s |
| 38 | A4 | retained cold-start predictive-rate, draw-count, and alternate-seed controls | 60 s | 19 composed 21×205 rate vectors byte-equal; R=32 scores 0.0610…0.1382; all 19 Q values win under alternate seeds at R=32 and R=4,096 | 3.4 s |
| 39 | A4 | common-draw cold-start counterfactual | 60 s | all 19 MI estimates become exactly 0.107860158; current hash tie selects H=2.35, not placement rank 0 | 1.0 s |
| 40 | A4 | out-of-window footprint/presence contract tests | 60 s | 3 passed; an unmeasured energy band is censored and contributes zero absence evidence | 0.94 s |
| 41 | A7 | shipped FCC closed loop plus opening-book/agenda/boundary verification | 600 s | 3 slow cases and 94 focused cases passed, but the successful run never measured H=3 and the assertions do not require it | 253.08 s |
| 42 | A6 | `isar diagnose` on an authenticated disposable retained-workspace copy | 120 s | rendered successfully but reported objective unrecorded, 0/0, crashed, and 98.4% fit share | 1.87 s |
| 43 | A4 | frozen-deliverable agenda A/B/C | 60 s | `phenomenon_map`, empty, and optic-only `dispersion_map` produced byte-identical agendas and assessments | 1.46 s |
| 44 | A7 | exact branch-specific worlds, target-window suites, and exhaustive two-recovery replay | 10 min worlds / 60 s suites | optic 21/21 converged through legacy windows; tracked acoustic did not terminate; 41/41 eligible target-core mutations lost support after two edge recoveries; physical-submit boundary sent for adversarial recheck; 510 focused passes, 2 skips across overlapping suites | 23.11 s optic world; 44.74 s recorded suites |
| 45 | A1 | incomplete frozen-targeted payload versus compact-authoring probe | 60 s | frozen payload accepted and defaulted to −2…7 meV, no hypotheses, empty steering; compact acoustic authoring froze −9…9 meV, two hypotheses, authoritative O41 | 1.4 s |
| 46 | A6 | second-pass discovery-report outcome verification | 10 min | F28 reproduced; terminal discovery has no automatic summary/diagnostic, while status/report expose only their narrower ledger/triage contracts | 3.8 s render |
| 47 | A8 | five-lens repair-order council | 2 min | 3 yea / 2 nay; moved crash/replay and operator stop into the first safety gate, and coupled uncertainty/RNG/cost/latency design before scorer optimization | 31.4 s |
| 48 | A7 | frame-compatible exact two-branch ALL closed loop and selected-question adjudication | 600 s | interrupted during planning pass 9 after eight committed observations; every selected window supported its selected question, no H=3 action occurred, and the agenda remained honestly open | 600 s hard limit |
| 49 | A5 | second-pass performance-remedy and acceptance-contract review | 10 min | sequenced interruption safety, allocation-free hot-path work, replay-stable fixed-total estimation, realized-neutron pricing, bounded parallelism, and concrete latency/memory/replay gates | read-only synthesis |
| 50 | A8 | second-pass report consistency and documentation-policy review | 10 min | found and corrected S10's stale F13 disposition; F31/F32 pending language flagged for final adjudication; banner/link policy passed | read-only review |
| 51 | A7 | second-pass frozen-target schema and recovery-to-submit verification | 10 min | F33 reproduced; missing-manifest recovery physically submitted four progressively widened scans, including out-of-domain and targetless requests; complete manifest blocked low support but not domain escape | bounded in-memory/mock-client probes |
| 52 | A7 | tracked-acoustic fixture-causality review and production-faithful rerun | bounded rerun | original trace contained 76 physical actions, but its missing `scan_id` guaranteed whole-batch fallback; interrupted follow-ups temporarily downgraded F32 pending the direct rule probes in rows 53–54 | bounded run interrupted |
| 53 | A7 | orphaned faithful-run containment and frozen-prefix audit | 5 min | stopped only the two identified audit PIDs; frozen acoustic/optic prefixes contained 515/489 physical submissions and valid master keys, but omitted tracker-state columns made their repeated nonconvergence fixture-induced | <1 s parsing after containment |
| 54 | A7 | confirmed-master ceiling and pooled-branch authority A/Bs | 60 s each | at 12/12 batches the real selector emitted another same-Q production scan; replacing only acoustic evidence authority with an optic row flipped the selected Q and action authority identity | 1.1 s / 1.2 s |
| 55 | A7 | interrupted-process containment and disposable-workspace cleanup | 30 s | exact audit PIDs 63564/17672 stopped; acoustic workspace removed after evidence capture; optic workspace retained when recursive deletion safety review refused it | 12.9 s stop; cleanup partial |
| 56 | A8 | documentation drift suite | 60 s | 12 passed | 1.08 s wrapper |
| 57 | A8 | initial parallel not-slow suite with repository-local `--basetemp` | 180 s | 5,255 passed, 9 skipped, 1 xfailed; three GUI failures isolated to the invocation—two require OS temp and one process-tree timing case | 83.57 s |
| 58 | A8 | serial reproduction of the three failures under their intended OS-temp contract | 120 s | sandbox first denied OS temp; authorized rerun passed 3/3 | 2.28 s wrapper |
| 59 | A8 | corrected parallel not-slow suite, real OS temp | 180 s | 5,258 passed, 9 skipped, 1 xfailed; 21 warnings | 76.56 s |
| 60 | A8 | full parallel suite, real OS temp | 1,200 s | 5,332 passed, 9 skipped, 2 xfailed; 21 warnings | 896.74 s |
| 61 | A8 | fresh-context final report verification | 10 min | CONFIRMED with no required corrections; counts, seam states, calibration boundaries, F31/F32/F35 scope, remedy tense, and test rows reconciled | bounded review |
| 62 | A8 | final post-closure documentation drift suite | 60 s | 12 passed | 1.13 s wrapper |

### Compiler-slice evidence

The exact three calibration intents compile as follows at source `796a84b` when the supervisor
reply is frozen deterministically:

| Intent | Frozen kind and Q contract | Frozen energy contract | Branch/question contract |
|---|---|---|---|
| ALL | `discover_and_map`; endpoints (2,0,0)→(3,0,0), max initial spacing 0.05 r.l.u. | intrinsic 0…10 meV | default catalogue, phenomenon map, `include_unknown=true` |
| ACOUSTIC | targeted; 21 points including both endpoints | signed search −9…+9 meV, source `compiler` | target acoustic, optic rival, tracked branch, authoritative O41 |
| OPTIC | targeted; 21 points including both endpoints | signed search −2…+12 meV, source `compiler` | target optic with 6 meV zone-centre anchor, acoustic rival, tracked branch, authoritative O41 |

The compact-program and resume paths converge on the same compiler or
read back an already frozen `ObjectiveSpec`; a resume never asks the LLM to reinterpret the goal.
The compiler's omission defaults are explicit and the focused suite pins malformed-value refusal,
signed-vs-intrinsic energy semantics, frozen-program round trips, and one repair attempt.

That deterministic compiler contract does **not** make the natural-language boundary deterministic:
see F04. S01 therefore fails even though its post-LLM compiler and frozen replay paths pass.

There is a second acoustic omission fallback, −2…+7 meV, inside direct `ObjectiveSpec`
construction and the shipped-hypothesis helper. The production goal compiler always overrides it
with −9…+9 meV, and the discovery carrier's target fields are documented as inert. But the public
frozen-program reader also reaches the fallback: an incomplete payload which already names
`isar.campaign-step.targeted/1` is treated as frozen, then silently receives that energy band and
other current defaults. That is the fail-open contract defect in F33.

The branch-specific Q invariant also reproduces on the exact 21-point calibration line. The tracker
publishes H=2.0 as reduced-Q origin index 0 for both branches. Acoustic precision eligibility is
exactly indices 1…20; optic eligibility is 0…20. H=3.0 is a measurable zone-boundary endpoint for
both. The acoustic exemption is an exact-origin test (`1e-9` floating tolerance), propagates through
the target completion denominator, tracked-frontier walk, and general acquisition ranking, and does
not spread into the H=2.05 neighbourhood.

Target completion itself is branch-isolated: a wrong-branch master remains evidence but cannot
become the target master, seed target model points, satisfy convergence, or enter the dispersion
prior, and an auxiliary-only selection is not certification-eligible. The resolver intentionally
retains the other standard branch as a rival hypothesis. The failures are downstream acquisition
lifecycle errors: resolved-rival retirement is inconsistent (F05), and the target's origin
eligibility is incorrectly reused as the rival's observability test (F06). Legacy result rows which
lack `branch_id` remain accepted by a compatibility path; fresh frozen campaigns carry the field.

## 7. Verified findings

Numbering note: the finding series starts at F02; the F01 identifier was not assigned and is not reused.

### F02 — The handoff's discovery-window efficiency calculation omitted the optic branch

- **Disposition:** FAIL (calibration interpretation, not fitter behavior) · **Priority:** P1 ·
  **Confidence:** high.
- **Expected:** an “all phenomena” truth/efficiency calculation covers both requested branches and
  preserves energy range for unresolved unknown/completeness questions.
- **Actual:** authoritative TAVI input `Al_test_phonons_centered.dat` declares two equally weighted
  branches: acoustic `6·|sin(π(H−2)/2)|` and optic
  `6 + 2·|sin(π(H−2)/2)|` meV. The retained fitter independently recovered two modes near both
  truths in six of the first ten fitted scans (for example 4.838/7.589 meV at H=2.6 against
  4.854/7.618 meV). `calibration/phonon_dft_deterministic.json` and the handoff table score only the
  scalar acoustic truth and consequently call 81–87% of points irrelevant to “the only mode.”
- **Measured correction:** across the ten ordinary 0…10 meV scans, 79/210 points (37.6%), not
  39/210 (18.6%), lie within ±1 meV of at least one requested branch. A contiguous zero-anchored
  0.5 meV grid retaining ±1 meV support for both known branches would use 122 points, a **1.72×**
  point reduction, not the handoff's suggested ~4×. For a genuinely single-branch objective the
  comparable target band uses about 6 points per scan and a ~3.5× reduction remains plausible.
- **Impact:** the handoff's proposed “clip to the predicted band” can discard the optic branch and
  violates `include_unknown=true` while completeness remains open. The broad first visit is
  defensible for all-mode discovery; objective/question-aware refinement is the missing lever.
- **Remedies:** calibrate window economy separately for ALL, ACOUSTIC, and OPTIC. For ALL, retain a
  broad/coarse discovery pass until local completeness is answered, then use a dual-band or
  question-conditioned refinement window. For branch-specific targeted runs, use the existing O41
  target-core machinery and stop spending coverage on the rival once identity is resolved.
- **Recheck:** TAVI source truth, retained fit outputs, and raw executed grids agree. A production
  realization probe still must prove which narrower grids current interfaces can express.

### F03 — The only resolving −E scan was missed-window recovery, not a scene-EIG H23 choice

- **Disposition:** FAIL (provenance claim) · **Priority:** P2 · **Confidence:** high.
- **Expected:** an H23 thermal-correspondence action is selected by `presence_probe`, carries the
  `−E mirror` reason, and has a scientific-action fingerprint distinct from its +E sibling.
- **Actual:** discovery decision 7 selected `opening_book_survey` at H=2.138. Iteration 6 first ran
  j-0011 over 0…10 meV. After its 258.097 s analysis, the legacy missed-window guard logged an
  `exception` — “re-centering on the Stokes side (1.5 meV) and widening” — and attempt 1 ran
  j-0012 over −6…9 meV under the **same** acquisition and scientific-action fingerprint. That retry
  resolved the acoustic mode. It was not the mirrored `presence_probe` action.
- **Impact:** the positive evidence supports adaptive recovery, but it does not validate scene-EIG
  corroboration selection. The handoff's strongest claimed evidence for SCENE-5 is therefore absent.
- **Remedies:** correct the campaign interpretation; join decision association, proposal, attempts,
  and recovery events in diagnostics so operator reports label why a physical scan occurred. Keep a
  separate behavioral world as the evidence for genuine H23 mirror selection.
- **Recheck:** exact decision fingerprint, proposal, two attempts, recovery event, and windows joined.

### F04 — Named-target goals do not deterministically preserve an energy cap or an unstated tolerance

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** a named acoustic/optic goal carrying “up to 10 meV” freezes one defined target-search
  meaning, and a missing per-point precision is either rejected for clarification or filled by one
  documented deterministic default. Fields chosen by the supervisor are not misrepresented as
  explicit operator policy.
- **Actual:** the supervisor prompt defines `[0,10]` only for discovery. It says targeted goals use
  branch defaults unless a range is supplied, but never defines how the intrinsic phrase “up to
  10” maps to a signed measurement/search band. `tolerance_meV` is required in the reply schema but
  has no source-level default. Two zero-temperature live batches against the deployed
  `scientific_interpreter` role produced:

  - acoustic: −9…+9 meV in every sample, ignoring the stated +10 cap;
  - optic: either −2…+10 or −2…+12 meV for the identical explicit ±0.5-meV prompt;
  - no stated tolerance: invented 1.0 meV for acoustic in both samples, and 0.5 then 1.0 meV for
    identical optic prompts.

  Every emitted range was marked `target_search_source="operator"`, because the deterministic
  compiler can see only that the LLM emitted a field—not whether the human said it. That provenance
  can turn a supervisor-chosen band into a hard authorization which runtime identity work may not
  widen.
- **Calibration-specific effect:** every sampled band still contains both requested toy target
  tracks (acoustic ≤6 meV, optic ≤8 meV), so this probe does not show a missed phonon here. It does
  make stopping tolerance, point count, anti-Stokes admissibility, and outside-band identity work
  depend on an unrepeatable translation. CLI `--goal` has no GUI confirmation gate; the GUI preview
  mitigates but does not repair it.
- **Remedies:** the minimal safe option is to reject a named-target command that omits tolerance or
  uses an ambiguous unsigned phrase, with a message requesting an explicit precision and signed
  search rule. A deeper option is to represent the user's intrinsic target-energy cap separately
  and deterministically derive the physical ±E authorization; either way, distinguish human,
  compiler-default, and supervisor-inferred provenance. Prompt wording alone is not a sufficient
  guard because identical zero-temperature prompts already varied.
- **Recheck:** source/schema trace, two independent live batches, and the frozen post-LLM compiler
  agree on the failure boundary.

### F05 — A scientifically resolved rival still widens the legacy target scan

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** once durable branch evidence says the rival at a Q point is resolved, its
  classification value and its requested energy coverage both retire. Later target-precision work
  should cover the target unless a new, explicit differentiation action reopens the rival question.
- **Actual:** `_make_classification_providers` applies `rival_resolved_hkls` only in `classify`, which
  returns zero, while its sibling `rival_band` closure still returns the frozen rival prediction.
  Final scan design calls that closure independently and freezes the result into the legacy window.
  In the reproduced acoustic-target state, the already-resolved optic prediction was
  6.992 ± 0.300 meV. Keeping it changed the legacy request from −6…4.6 meV (142 points) to
  −6…7.592 meV (182 points): **40 extra points, or 28.2%**, for a rival whose classification value
  was explicitly zero.
- **Impact boundary:** authoritative O41 can still select the narrower target-core request, so this
  does not mean every physical submission pays the 28%. The widened legacy request is nevertheless
  the required control and the production fallback whenever authoritative evidence is unavailable
  or refused, and it may itself win the authority comparison. The workflow therefore has a valid
  production path that spends avoidable neutron time after differentiation has finished. Target
  completion remains branch-isolated; this is an acquisition-efficiency error, not cross-branch
  certification.
- **Remedies:** make `rival_band` return `None` under the same durable retirement predicate used by
  `classify`. Add an end-to-end plan/realization test for a resolved rival covering both an O41 grant
  and the authenticated legacy fallback; asserting only that `classify == 0` misses the leak.
- **Recheck:** provider closure, frozen scan-design context, and exact adapter point counts reproduced;
  the focused branch/window suite passed.

### F06 — An optic campaign can repeatedly probe an unobservable acoustic rival at H=2

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** optic precision at the zone centre is measurable, but a gapless acoustic *rival* at
  that same reduced-Q origin is degenerate with the elastic line. Rival reconnect and discrimination
  actions must test the rival's observability, independently of whether the target is measurable,
  and a clean null must durably exhaust an identical auxiliary action.
- **Actual:** `_rival_gap_pick` uses the target-oriented `precision_coverage_eligible` flag as its
  structural-rival guard. At H=2 this is true for an optic objective, so the reproduced optic state
  selected H=2 for a production reconnect; the otherwise identical acoustic control correctly
  selected nothing. That reconnect rung is capped at two production attempts. The separate
  `narrow_rival_probe` generator has the same physics hole and emitted an auxiliary scan over
  −0.45…+0.45 meV at H=2 for the gapless acoustic prediction.
- **Retry defect:** `identity_rival_probe` is not a production kind, has no attempt/null guard, and a
  clean null suppresses missed-window recovery but not future generation of the action. Two
  consecutive calls on unchanged scientific state emitted the same action fingerprint
  `75173c4a…4c97` and the same grid. A duplicate null can revise tracker bookkeeping without changing
  the pooled content hash or adding identity information. Campaign budget, operator stop, or the
  10,000-iteration emergency ceiling—not scientific exhaustion—are the remaining bounds.
- **Impact:** this is optic-specific wasted beam at the reference line's zone-centre endpoint. The
  auxiliary sink cannot certify the target or seed the dispersion prior, which protects the result
  but also means the expenditure cannot advance the requested optic map.
- **Remedies:** add an exact-origin gapless-acoustic observability guard shared by reconnect and
  narrow-probe generation, independent of target precision eligibility. On a successful clean null,
  durably suppress the scientific-action fingerprint and deduplicate identical null windows. Pin the
  behavior with a real loop-and-resume test rather than a test that manually supplies suppression.
- **Recheck:** exact optic/acoustic controls, repeated generator output, suppression/state trace, and
  56 focused O44/rival/merge/replay tests agree.

### F07 — A true two-mode fit cannot supply the uncertainties discovery requires to finish

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** an ALL campaign which correctly selects a two-mode acoustic+optic structure can
  demonstrate the requested precision for each resolved mode, or stop and name an unavailable
  analysis capability before buying more indistinguishable scans.
- **Actual:** `_mode_intervals_of` deliberately returns one `None` per mode whenever a candidate has
  more than one fitted omega, even when the fit carries a sound `freq_interval`. The analysis
  currently tracks only one internal primary phonon and does not persist which slot it was; guessing
  slot zero would be wrong, so the conservative persistence rule is locally correct. Downstream,
  every confirmed phenomenon opens `EstimateParameter`, whose closure requires a trusted,
  slot-matched `ModeInterval`. A production-shaped two-mode scene at H=2.5 therefore confirmed the
  acoustic-like 4.2 meV and optic-like 7.4 meV tiles but left both precision questions open on
  `within_candidate_uncertainty_unavailable`. Supplying otherwise-identical sound intervals in slots
  0 and 1 closed both questions, proving that the missing producer contract—not the predicate's
  tolerance arithmetic—is the blocker.
- **Calibration evidence:** six of the retained scene's nine best candidate rows are two-mode
  `stokes2+bg` fits with posterior 0.9987–0.999999; every one carries `[None, None]`. None of the nine
  best rows carries any trusted interval. This is the expected outcome for data which really contain
  both requested branches, not an exotic model corner.
- **Neutron-time effect:** `parameter_refine` handles the missing-interval state by remeasuring the
  same witness. The exact probe emitted H=2.5 windows 3.2…5.2 and 6.4…8.4 meV again after a scene
  revision. Their physical action keys stayed identical while their scientific fingerprints changed
  with the new scene digest, so a successful trusted delivery does not enter the broken-scan retry
  bound. More exposure cannot create the absent per-mode persistence field; the step tends toward
  budget or analysis-limit exhaustion rather than completion.
- **Impact boundary:** this blocks precision-complete discovery of the reference two-branch truth.
  It does not falsify the fitted centres, and a genuinely single-mode candidate can use the existing
  one-slot interval path. The current agenda tests prove only that *some* phenomenon-bearing campaign
  can complete from a hand-populated single-mode interval; they do not prove the multimode workflow.
- **Remedies:** persist producer-sound intervals for every fitted mode with an explicit slot identity
  (or compute a scientifically equivalent per-mode uncertainty contract); a single unnamed primary
  interval is insufficient for an ALL objective. Until that exists, classify a dominant multimode
  `within_candidate_uncertainty_unavailable` state as an analysis-capability consultation/offramp so
  the controller does not repeatedly spend beam on information its writer cannot express. Do not
  weaken the closure predicate to accept point estimates or guess a slot.
- **Recheck:** retained rows, producer source and its existing multimode test, exact agenda A/B,
  repeated generator identities, and 122 focused scene tests agree.

### F08 — Declared acoustic/optic hypotheses do not affect real discovery placement

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** when discovery is supplied an acoustic or optic hypothesis/priority, that declared
  physics changes at least one real-fit-derived belief or placement when the alternatives warrant it.
- **Actual:** the production catalogue declares no identity-conditioned parameter priors, so
  `_identity_candidates()` returns no split and every real phonon fit has `identity=None`.
  `scene.phenomena._resolve_hypothesis` can match only a named identity. The hypothesis mapping and
  its fingerprints reach the planner, but no real-fit-derived belief can consume their contents.
  The repository's production-shaped closed-loop A/B—identical world and seed, with versus without
  a declared acoustic hypothesis—therefore produces byte-identical placement sequences. Its strict
  XFAIL reproduced in 67.65 s; the hand-identified scoring-engine unit test remains sensitive, which
  localizes the gap to fit→identity attachment rather than EIG mathematics.
- **Impact boundary:** class-agnostic ALL discovery can still find physical modes, but it cannot use
  known acoustic/optic dispersion physics to prioritize neutron time or label the resulting tracks.
  The separately compiled ACOUSTIC and OPTIC objectives use the targeted branch-tracking lane and are
  not defeated by this SCENE identity gap.
- **Remedies:** either land identity-conditioned catalogue priors and a per-mode identity contract
  (multimode candidates need acoustic⊗optic, not one identity for the whole row), then require the
  real closed-loop A/B to move; or describe discovery as explicitly class-agnostic and reject claims
  that a declared branch priority influences it until that contract exists. Prompt or score-weight
  tuning cannot bridge a missing identity.
- **Recheck:** source trace, engine positive control, and the named strict-XFAIL world agree.

### F09 — One malformed interior log line is silently discarded during resume

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** the append-only session log may tolerate an incomplete final write, but corruption
  between valid records must stop resume visibly. Counters, charged exposure, submission recovery,
  suppression, and timing cannot be reconstructed from a log whose middle silently vanished.
- **Actual:** `read_log` catches `ValueError` independently for every JSONL line and continues. A
  valid/malformed/valid three-line probe returned the two valid events without any error. A torn
  tail followed by a restart append fused the old fragment and the new event into one permanently
  malformed line; the resumed state reconstructed **one scan / 10 s** where the intended history was
  **two scans / 30 s**. Corrupting an interior `physical_submission` similarly removes the only
  handle by which unmatched work can be recovered even when adjacent events survive.
- **Impact:** after a disk fault or hard kill, the controller can understate neutron spend and scan
  count, forget a submitted job, lose suppressions or clock records, and buy an action again. The
  behavior is silent, so an operator cannot distinguish a clean resume from an incomplete history.
- The post-mortem duplicates the same bug in its private `_read_jsonl`: its docstring promises only
  truncated-final tolerance, but it catches and skips `ValueError` on every line. The diagnostic
  therefore cannot serve as an independent corruption alarm; it can summarize the same incomplete
  history without naming the missing record.
- **Remedies:** fail loudly on malformed non-final lines. Treat only the final incomplete record as a
  recoverable tail, and repair or quarantine that tail before any append; do not append behind bytes
  that failed to parse. A small offline/manual repair path is sufficient for this single-user tool.
  Pin valid/malformed/valid, truncated-final, restart-append, and zero-new-submission resume cases.
- **Recheck:** byte-level log probes and the focused recovery suite reproduced the loss; seven
  recovery/adjudication tests passed but none asserts interior-corruption rejection.

### F10 — A crash between two fsyncs can charge a scan without leaving a recoverable submission

- **Disposition:** FAIL · **Priority:** P2 · **Confidence:** high.
- **Expected:** once an external job has been submitted, the first durable campaign record must be
  enough for restart either to recover that job or to stop for explicit reconciliation. A crash
  boundary must not leave a charged-looking action with no external-job identity.
- **Actual:** the accepted `run` event is appended and fsynced before the detailed
  `physical_submission` event. An injected crash between those writes leaves the former, including
  spend, but no job handle for `_recover_unmatched_submissions`; restart may therefore submit the
  same scientific action again under a fresh key. The inverse state—durable detailed submission but
  no legacy run event—is already the shape the unmatched-submission recovery can reason about.
- **Impact boundary:** this is a two-fsync crash window after successful external submission. It is
  distinct from the still-unavoidable submit-to-first-local-write window, which needs an instrument
  idempotency/reconciliation contract rather than log ordering alone.
- **Remedies:** persist and fsync `physical_submission` first, then write the derived/legacy `run`
  event, and make resume reconstruct counters and spend exactly once from either complete or
  recoverable partial state. Add injected-crash tests on both sides of each write. Update the stale
  hard-kill limitation text once the promised no-double-spend invariant is actually pinned.
- **Recheck:** exact event ordering, both injected partial states, and unmatched-recovery behavior
  were traced; the ordinary seven-test recovery subset passed.

### F11 — Restart resets the age of an unanswered consultation prompt

- **Disposition:** FAIL · **Priority:** P2 · **Confidence:** high.
- **Expected:** an unanswered prompt's delivery-based timeout is durable. Restarting the driver must
  not postpone its default settlement or re-enable an action held by that consultation.
- **Actual:** channel reload reconstructs settlements and epochs but not open prompt issuance. The
  prompt ID is stable, yet its original `prompt_issued_delivery` is lost and age restarts from the
  current delivery. In a timeout-two probe, default settlement occurred one delivery late after one
  restart; repeated restarts can defer it indefinitely. A broken-scan retirement that is pending the
  answer is not frozen until settlement, so the held action can become eligible again.
- **Impact:** campaigns that restart during human absence can wait or remeasure longer than the
  frozen policy allows. This is deterministic restart drift, not a wall-clock scheduling nuance.
- **Remedies:** rebuild open prompt issuance from the durable consultation event stream (including
  the original delivery index), or persist it as explicit channel state. Test an exact timeout across
  one and repeated restarts, including the broken-action eligibility boundary.
- **Recheck:** stable prompt identity, delivery counter, settlement timing, and restart state were
  exercised; existing focused tests do not preserve prompt age across construction.

### F12 — Recovered analysis work is relabelled as attempt zero

- **Disposition:** FAIL · **Priority:** P3 · **Confidence:** high.
- **Expected:** recovery of a recenter or missed-window attempt must retain the submitted attempt
  number in analysis operation IDs, timing, and escalation provenance.
- **Actual:** `_recover_unmatched_submissions` calls `_finalize_completed_submission` without the
  payload's attempt, so its default zero wins. A recovered attempt-two payload reached finalization
  with no attempt argument and produced a `reduce:...:0` operation identity instead of
  `reduce:...:2`.
- **Impact boundary:** physical/scientific keys, fitted values, and the cumulative analysis-clock
  total remained unchanged in the reproducer; this is provenance and per-attempt diagnostic drift,
  not evidence that the numerical result changes. It can nevertheless misattribute an escalation or
  obscure which recovery attempt consumed analysis time.
- **Remedies:** pass `attempt=int(payload.get("attempt") or 0)` through recovery and pin a nonzero
  recovered-attempt test at the operation/timing event boundary.
- **Recheck:** direct payload capture plus the focused seven-test recovery subset agree.

### F13 — Maintained O41 documentation still states two incompatible authority rules

- **Disposition:** FAIL (documentation contract) · **Priority:** P2 · **Confidence:** high.
- **Actual:** current source and tests implement O43's two-sided indistinguishability band followed by
  the lower-cost target-core tie break. `docs/ARCHITECTURE.md` and the synopsis of
  `docs/design/eig.md` say the same. Later normative passages in `docs/design/eig.md` still state the
  superseded one-sided rule under which unresolved superiority retains legacy.
- **Impact boundary:** the audited runtime path follows O43, so this is not evidence of a wrong
  submitted window today. It is nevertheless a live-contract contradiction that caused this audit's
  first oracle to be wrong and can misdirect the next implementation or review.
- **Remedy:** make every maintained O41/O43 passage state the exact implemented two-sided t-band,
  offset-disagreement, cost-tie, and fail-closed rules; retain the old rule only as dated rationale.
  Add a doc-drift assertion for the operative rule or remove duplicate normative prose.
- **Recheck:** selector source, authority tests, architecture statement, and all conflicting design
  passages were compared; the audit oracle above now follows O43.

### F14 — No production-shaped test proves a real target-core winner reaches the instrument request

- **Disposition:** INCONCLUSIVE (verification gap) · **Priority:** P1 · **Confidence:** high.
- The source chain is coherent: authority selects a realized role, persists it, overwrites proposal
  geometry/exposure/identities, reconstructs the adapter grid, authenticates every request field, and
  returns the selected validation. Failures restore authenticated legacy and emit visible exceptions.
- However, every production-shaped authority test either retains legacy, refuses/falls back, or
  injects metadata which merely claims a target application. Serialization tests construct target
  DTOs directly. No acoustic or optic case builds a genuine complete comparison whose O43 tie or
  positive delta selects `target_core`, passes `_prepare_authoritative_submission`, and asserts the
  exact wire request equals that target.
- **Impact:** the most neutron-efficient branch-specific window swap is the sole high-value outcome
  of this machinery, yet this audit cannot dynamically verify it at the submission boundary. This is
  not a demonstrated runtime defect; it is a P1 gap in the evidence needed to certify S10.
- **Remedy:** one parameterized acoustic/optic integration regression using the real comparison and
  request path, asserting selected role, proposal, adapter grid, authenticated request, and submitted
  body. No new abstraction is warranted.
- **Recheck:** 234 focused window/authority/realization tests passed; source-to-wire coverage audit
  found no genuine target winner.

### F15 — Narrow discovery windows are not tested through agenda-to-wire production flow

- **Disposition:** INCONCLUSIVE (verification gap) · **Priority:** P2 · **Confidence:** high.
- **Actual design:** discovery is not fixed at 0…10 meV. Opening-book Q gaps, exposure fill, and
  track bridges use the full intrinsic domain; presence, identity, mismatch, missing-energy, and
  parameter-refinement actions use question-conditioned bands. Realization chooses a local-FWHM
  nice step, clips inward, refuses fewer than two points, and never widens the declared band.
- Unit tests cover those generators and grid rules, but the only real program-runner submissions use
  cold-start/full-domain or a hand-built 0…10 intent. No test takes an agenda-produced narrow intent
  through planner, realization, proposal, adapter, and submitted wire body.
- **Impact:** the source supports the efficient semantics adopted in the ALL oracle above, but S11's
  decisive economy claim remains dynamically unverified. A regression in plumbing could silently
  turn a narrow refinement back into a broad scan without failing the present suite.
- **Remedy:** one production-flow test for a real narrow `parameter_refine` or presence action,
  asserting the selected question's required support, exact executed endpoints/point count, and wire
  request. Separately assert that other still-open modes remain on the agenda; do not require every
  individual ALL action to remeasure every mode.
- **Recheck:** complete generator/realizer source matrix and focused tests agree; no narrow-wire case
  exists.

### F16 — The scene alternative cap destroys completion authority, but EIG keeps steering from it

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** an ALL campaign with ordinary competing fit structures either retains a bounded
  posterior sufficient to answer its agenda, or stops explicitly at a model-authority limitation.
  A posterior declared unable to support presence/completeness decisions must not silently regain
  authority only inside the scan-ranking layer.
- **Exact retained transition:** with four masters the candidate product was
  `5×6×6×4 = 720`, fully enumerated. The next master made it
  `5×6×6×6×4 = 4,320`; only 4,096 assignments were retained with no discarded-mass bound. Six
  answered `ConfirmPresence` questions reopened, eight dependent track/parameter questions
  dissolved, and three questions opened. Subsequent products grew through 25,920, 129,600, 518,400,
  and **3,110,400**, always capped at 4,096 and authority-unresolved.
- **Why ordinary data cannot repair it:** `presence.score_bound=None` disables finite-evidence margin
  pruning, so an arbitrarily poor but finite candidate survives. Active linkage proposers are also
  unbounded under the current global-span interface. Sharpening a losing model by 10,000 log units
  did not reduce the product; only marking candidates unusable or genuinely splitting the cluster
  restored exact authority. A 4–6-candidate scene crosses the cap by arena 5–7; even two candidates
  per point exceed it at 13 arenas, well before a 21-node line can finish.
- **No scientific offramp:** current retained evaluation has 18 open presence questions, one region
  question, one completeness question, and 19 `posterior_truncation_unresolved` assessments.
  That blocker is `model_authority`, not durable, so exhaustion rejects it as a generator defect and
  consultation does not emit. Presence generation always offers canonical and mirrored scans. Only
  budget/max-delivery/operator/machine limits end the loop, all as incomplete rather than objective
  completion.
- **Unsafe scoring split:** scene predicates correctly fail closed, but `scene.eig.score` does not
  check cluster authority. It renormalizes the retained top 4,096 as if complete. In retained
  decisions 6–10, unauthoritative conditional `ConfirmPresence` terms contributed **95.73%, 97.13%,
  97.61%, 97.37%, and 98.01%** of each selected design's total. The later placement happened to
  remain spread out; its claimed neutron efficiency is nevertheless unsupported because omitted
  assignments may arbitrarily change MI and rank.
- **Minimal high-value remedy:** compute a dynamic one-sided omitted-mass bound per actual cluster,
  anchored to the scored retained leader, then enumerate adaptively. For the final retained cluster,
  at most 32 directed presence reports bound that factor by 16 nats and the linkage prior cannot add
  positive mass; this proves a log tail bound of **−40.711** (about `2.1×10⁻18` relative to the
  leader), despite today's `null`. On the same data K=128/256/512/1,024/4,096 gives proposed bounds
  −6.07/−11.31/−17.53/−24.45/−40.71. Start small and double until both predicate and rank tolerance
  are certified. When a bound remains vacuous, refuse scene-EIG and issue an explicit
  model-authority consultation/offramp.
- **Longer-term option:** exploit the path's chain factorization (forward/backward or equivalent)
  instead of constructing a Cartesian global assignment set. Simply raising `SCENE_ALT_CAP` cannot
  scale—the 21-node products are 2,097,152 even at two candidates and `4.4×10¹²` at four—and it has
  no operator configuration surface.
- **Recheck:** exact retained revisions/transition log, live factor worlds at 12×2/13×2/21×2 and
  6×4/7×4, exhaustion/consultation probe, score attribution, and 395 focused tests agree.

### F17 — The exact 0.05-r.l.u. Q grid is judged incomplete and buys eight extra surveys

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** measurements at `round(2 + i·0.05, 12)` for `i=0…20` exactly satisfy the frozen
  maximum-initial-spacing contract. No missing-Q action remains once those 21 nodes exist.
- **Actual:** eight adjacent subtractions evaluate as `0.050000000000000266`.
  `coverage_complete` compares `widest > spacing` without a tolerance, so it returns OPEN while its
  rounded diagnostic says `widest_unsurveyed_gap_rlu: 0.05`. The production generator then offers
  exactly eight `opening_book_survey` actions, one for each numerical artifact; presence work stays
  dependency-blocked until those false gaps are filled.
- **Impact:** for this reference ALL objective, the controller can buy eight additional full-domain
  0…10 meV Q surveys after already completing the declared 21-node grid. The fault is deterministic
  at the objective's exact boundary and directly wastes neutron time before later questions run.
- **Remedies:** compare canonical path-grid indices, or use a scale-appropriate absolute/relative
  tolerance consistent with the Q canonicalization contract. Pin the entire 21-node equality
  boundary and agenda→generator behavior; a test containing only a genuine 0.2 gap cannot catch it.
- **Recheck:** full-line predicate, exact raw gaps, assessment note, eight emitted intents, and the
  focused predicate/generator suites agree.

### F18 — Discovery EIG is quadratic, effectively single-core, unclocked, and stop-unsafe

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** the real-tier decision loop meets the declared 10–30 s target on the 16-core/32-thread
  host, charges planning to a visible analysis allowance, and cannot buy another physical scan after
  an operator stop or exhausted analysis ceiling arrives during scoring.
- **Retained accounting:** scene-build→proposal gaps grow from 0.094/0.128/0.528/7.754 s at
  0/6/24/144 alternatives to 131.464 s at 720 and then **2,436–4,830 s** at the 4,096 cap. The six
  late gaps total 20,625.065 s; all ten total 20,633.568 s. `isar_run` reduction/fitting totals
  1,969.426 s, so the reported 91.2%/8.7% split is sound and slightly conservative.
- **Direct attribution:** one iteration-4 720-alternative, 21-point design takes 3.442 s;
  ×39 designs predicts 134.2 s versus the observed 131.5 s whole gap. One exact late
  4,096-alternative design takes **165.202 s wall / 185.75 s CPU**; ×17 predicts 2,808 s, within 15%
  of the observed 2,436 s gap. Profile time is dominated by question/cluster MI, `_mi_from_rates`,
  log-sum-exp, and composed predictive rates—not agenda derivation or persistence.
- **Root cause:** for every one of A generating alternatives, `_mi_from_rates` draws R Poisson
  observations and evaluates each against all A rates: **Θ(A²·R·P)** per contributing
  cluster/question/design. At A=4,096, R=32, P=21 this is about 11.27 billion multiply-add-scale
  operations per MI term. The inner operation is NumPy, but outer alternative/question/design loops
  are serial; CPU/wall is 0.995 at A=720 and 1.124 at A=4,096 despite 24 OpenBLAS threads—roughly one
  effective core, or 3–7% of this workstation.
- **Clock/stop defect:** discovery checks the stop token only at the top of its delivery loop, then
  synchronously calls `plan_next_action` with no timer, `analysis_operation`, deadline, subprocess,
  or callback. It does not recheck before proposal/submission. A stop during scoring waits for return
  and can be followed by one more scan; the analysis ceiling neither charges nor blocks it. The
  retained terminal iteration demonstrates the sequence: 4,830 s of uncharged planning, physical
  acquisition, then a fit which discovers that the 1,800 s analysis ceiling is exceeded.
- **First remedy—make long planning safe:** pass a pure checkpoint callback/deadline through
  `plan_next_action`, selection, scoring, and estimator blocks; the outer loop owns the monotonic
  deadline and stop token. Reuse the injected-deadline pattern already present in the shadow EIG
  lane. Bracket planning with durable activity, timing, and analysis-clock events, and recheck stop
  immediately after planning and before submission. Move agenda/program pointer publication until
  after scoring succeeds, so cancellation publishes neither a decision nor a scientific-state
  advance; inventory blobs may remain as unreferenced diagnostics. Return `over_budget` for deadline
  expiry and `operator_stopped` for a stop token. Do not return a partial best candidate: that would
  make the beam decision depend on processor speed.
- **Second remedy—remove avoidable work, then remeasure:** discard exactly zero posterior mass before
  prediction rather than clipping it to `1e-300`; expose an expected-rate-only phenomenon path so
  `predict(seed=0)` does not allocate discarded random tuples; cache the
  design×cluster×alternative rate matrix across questions; and evaluate likelihood rows in bounded
  matrix blocks. Preserve the current streams while making these mechanical changes, then profile
  the retained 720/4,096 shapes again.
- **Owner-gated estimator change:** replace R draws *per alternative* with a fixed total K of
  deterministic posterior draws shared across competing designs and questions, reducing work to
  Θ(K·A·P). K=512 is about 256× less likelihood work here. Key common random numbers to the
  cluster/base scientific problem and latent alternative/simulation/energy coordinates—never the
  design or question identity—so equal predictions tie. Persist estimator revision, seed, K, block
  layout, and backend; use paired block uncertainty to decide whether a winner is separated. An
  unresolved tie falls through to realized cost, then placement/locality, and only then a stable
  fingerprint. Because `mi_n_draws` currently means per-alternative draws, this needs a new frozen
  policy field/revision, not a silent reinterpretation. Calibrate K against exact small scenes,
  medium scenes, and retained-decision rank/stability before authorizing it.
- **Coupled authority and pricing:** implement F16's rigorous retained-mass bound and refuse scoring
  while authority remains vacuous; an A≤512 cap is safe only if that bound proves it. Persist both raw
  weighted information and information per *realized* neutron, where cost comes from the prospective
  snapped grid and exposure rather than the scorer's approximate window. Re-score after material
  realization changes. Keep absolute worthwhileness/overhead policy separate from the statistical
  comparison; F23 owns that policy decision.
- **Parallelism is the last multiplier:** if the serial fixed-K/vectorized implementation still
  misses the target, use one persistent bounded executor over designs, deterministic ordered gather,
  and one BLAS thread per child. Honor `ISAR_WORKERS`, cancel/drain workers before returning, and pin
  the existing pytest nested-pool guard. Parallelizing the current A² kernel alone still leaves
  multi-minute late decisions and multiplies memory pressure.
- **Acceptance gate on the retained workload:** one A=4,096 design kernel ≤2 s and the full 17-design
  decision ≤30 s in three fresh hard-timed runs; temporary memory <256 MiB per worker and total
  scorer process RSS <1 GiB; byte-identical score/block records under repeat, reversed candidate
  order, and 1-versus-N workers; exact equality for the 19 `[205]×21` cold-start designs followed by
  the declared placement winner; the equal-MI 21-point/5-point pair chooses the 4.2× cheaper action
  while retaining raw MI and realized cost; and stop/deadline cancellation within one score block
  (target <1 s) publishes no decision or pointer advance.
- **Boundary:** same-action scan-window O41 EIG is a different, bounded workload. Representative
  two-variant scoring is 0.928 s and the tested 384-scenario/200-point case 0.421 s. Its five-second
  deadline can overshoot one inner call and then fail open to legacy, but it did not cause this run.
- **Recheck:** retained timings, exact one-design replays, cProfile, CPU/BLAS inventory, stop/call
  trace, byte-identical source at retained/current commits, stable disposable manifests, and 81
  focused tests agree. Existing tests use tiny scenes and have no 4,096-scale or mid-score-stop gate.

### F19 — The GUI campaign monitor does not recognize a discovery-program campaign

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** the same GUI which starts and controls an automated `discover_and_map` run shows its
  objective, budget, current work, exceptions, analysis allowance, and terminal state while it owns
  the process.
- **Actual contract split:** `program_runner` starts a discovery segment with the fsynced
  `decide/action="program_step_start"` event. `CampaignViewModel.reduce_campaign` recognizes only
  `decide/action="campaign_start"`; if none exists it returns `has_campaign=False` before reducing
  segment state. All GUI reducer/dock fixtures use the targeted-lane marker, and no GUI test contains
  `program_step_start`.
- **Retained proof:** its real log has 431 events, one `program_step_start`, zero `campaign_start`
  records, and eleven physical scans. Feeding that exact log through the production reducer yields
  `has_campaign=False`, **“Now: campaign idle”**, **“analysis: no limit”**, no budget readout, and no
  exceptions. Whole-log scan counting happens before the early return and sees eleven, but the
  campaign surface is still classified absent.
- **Compounding gap:** discovery scene-EIG itself emits no activity/clock event. The retained
  scene-build→proposal gaps of 131–4,830 s therefore have no durable “planning/scoring” activity for
  a corrected reducer to display; during the work that consumed 91% of wall time the event stream is
  simply silent.
- **Impact:** the operator surface is blind precisely for the ALL-mode workflow under audit. A live
  process can spend tens of minutes in scoring, be blocked by scene authority, or approach a budget
  while the monitor says idle/no campaign. This does not alter submitted scans, but it removes the
  human oversight promised by the operator contract and obscures whether the controller is making
  progress.
- **Remedies:** establish one shared segment-start contract. Prefer emitting the canonical
  `campaign_start` envelope (full frozen objective/program source and analysis limit) before the
  discovery-specific `program_step_start`; alternatively teach every GUI start/resume/reducer path
  to normalize both events. Add real program-runner→workspace-facade→viewmodel coverage. Separately
  bracket planner scoring with activity and charged timing events so a long synchronous call is
  visible even before cancellation is implemented. F28/F34 own the post-mortem/reporting repair.
- **Recheck:** writer and reducer source, all GUI fixtures, the retained event vocabulary, and the
  exact production reducer output agree.

### F20 — A frozen catalogue profile can name nothing while fitting silently uses the default

- **Disposition:** FAIL · **Priority:** P2 · **Confidence:** high.
- **Expected:** `catalogue_profile` is a frozen scientific reference. It must either name a live
  registry entry which consistently governs placement and fit enumeration, or be refused before a
  campaign starts.
- **Actual:** the supervisor reply schema and `DiscoverAndMapSpec` accept any nonempty string. An
  exact `isar.catalogue.unregistered/1` program constructed successfully; its planner manifest had
  an empty rule set and zone-centre weight 0, so cold start silently flattened. Production
  `phonon.enumerate_phonon_structures(ctx)` exposes no profile argument and calls catalogue
  enumeration at its default. Evidence selection explicitly documents the same default-until-a-
  second-profile shortcut, and scene persistence stamps `DEFAULT_PROFILE` until per-fit profile
  provenance exists.
- **Impact boundary:** only `isar.catalogue.default/1` is currently live, and the reference campaign
  names it, so its structures are not changed by this gap. But an LLM-authored or hand-authored
  unknown reference can run while its frozen program claims one catalogue, opening-book placement
  uses no registered rules, and fitting/scene use another. Any future second profile would also be
  inert until the fit contract is plumbed.
- **Remedies:** while there is one live profile, validate equality with `DEFAULT_PROFILE` at compile
  and full-program read boundaries and fail loudly—simpler and more honest than a generic registry
  seam. When a second profile is deliberately made live, pass its ID through the supervised fit
  invocation, structure/prior enumeration, fit result, and scene catalogue version before relaxing
  validation. Pin an unregistered-profile refusal and a future distinguishable-profile A/B.
- **Recheck:** registry, schema, planner manifest, opening-book fallback, fit wrapper, scene stamp,
  direct unregistered-profile probe, and the existing unregistered cold-start test agree.

### F21 — The campaign diagnostic inverts the planner/fitter wall-time conclusion

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** a chart labelled campaign wall accounts for the event-span wall clock, or explicitly
  displays the uninstrumented remainder. Percentages must not use a partial sum as the denominator.
- **Actual:** `_wall_split` sums non-overlapping `timing` records and names that sum total wall. Since
  discovery planning emits none, the retained diagnostic reducer reports **2,000.676 s total**:
  1,969.535 s fitting, 3.532 s instrument, and 27.610 s overhead. The actual first→terminal event
  span is **22,635.589 s**. Recorded stages cover only **8.84%** of wall time.
- The resulting chart/table says fitting is **98.44%** of campaign wall. Against the real event span
  it is **8.70%**—the same 8–9% measured independently in the calibration handoff. Thus the official
  diagnostic reverses the central performance conclusion rather than merely omitting a category.
- **Impact:** an operator or maintainer using the generated report would optimize fitting, while the
  verified bottleneck is single-core scene-EIG. This also masks hangs: any unclocked gap disappears
  from “total” instead of appearing as unattributed time.
- **Remedies:** derive segment wall from durable start/terminal timestamps (or current time for an
  active segment), subtract only demonstrably non-overlapping spans, and render the positive remainder
  as **unattributed/uninstrumented**. Once F18 adds planner timing, classify that span explicitly.
  Never compute `% campaign wall` from the sum of known categories without a coverage check. Pin the
  retained shape: large event gap, small recorded sum, and an unattributed remainder.
- **Recheck:** source arithmetic and the exact retained event stream reproduce all figures above.

### F22 — A delivered scan can violate the declared condition partition without suspending steering

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** cold-start's documented/tested invariant says the first measurement lands in the
  declared sample/condition/path partition “by construction.” More fundamentally, a 300 K scientist
  setpoint must not silently become a 200 K campaign; the measured readback may differ within the
  declared binning tolerance, but a different bin needs an explicit operator decision.
- **Actual retained transition:** `card.policy.toml`, cold-start, and all ten decision bases declare
  300 K under `t_round=5 K`. Every one of 22 attempted/trusted measurement records is 200 K. The first
  scene consequently contains no footprint in the declared partition; from decision 1 onward,
  intents correctly attach to a new measured 200 K partition while the decision basis continues to
  record the original 300 K declaration. No exception, consultation, or stop records the 100 K
  disagreement.
- **Impact boundary:** R02 proves the scorer does *not* combine 300 K predictive rates with 200 K
  evidence; after one scan it safely scores at 200 K. The defect is instead experiment authority: if
  the operator intended 300 K, the controller can spend the full beam budget mapping the right modes
  at the wrong thermodynamic condition. The retained simulation's truth itself is 200 K, so its fit
  accuracy remains interpretable, but it is not evidence that the declared-condition contract works.
- **Remedies:** after the first trusted arena—and on later condition changes—compare the arena's
  canonical condition set with the frozen declaration using the same binning policy. On a different
  partition, stop before the next acquisition and issue a visible consultation: correct the
  instrument/setpoint or explicitly start/adopt a new condition slice. Never rewrite
  `card.policy.toml` automatically. Include declared versus measured conditions in the program GUI
  and terminal summary.
- **Recheck:** retained card, decision bases, all arena records, scene/intents, scorer temperatures,
  and the existing integration test which asserts equality only for a matching fixture agree.

### F23 — Discovery can prefer a strictly more expensive window with identical information

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high · **Capability status:** explicitly
  Planned as SCENE-5.5, not an accidental regression.
- **Expected:** discovery placement maximizes useful scientific information per realized neutron
  cost, and a strictly cheaper design wins when the scientific information is equal. A broad
  window may still win when its additional support answers additional open questions.
- **Production contract:** `_design_for_evaluation` supplies window-derived `n_points` and the
  frozen per-point `ncount`, but `scene.eig.score` computes only
  `sum(priority_weight × MI)` and sorts on `(-total, design_key)`. The production planner takes
  `table.rows[0]`. Actual `n_points × ncount` cost is considered only at proposal finalization and
  budget charge, after selection. `steering.scheduler` states this omission directly, and the
  governing design documents classify the priced envelope as Planned.
- **Dominance reproduction:** in a production-shaped `ConfirmPresence` scene with a mode at 8 meV,
  the 0…10 meV/21-point design and a 7…9 meV/5-point design each score exactly
  **0.6931471806 nats** at 100,000,000 neutrons per point. The broad design wins by its content-hash
  tiebreak even when input order is reversed. It costs 2.1×10⁹ versus 0.5×10⁹ monitor neutrons:
  **4.2× the cost for identical information**, or 4.2× worse information per neutron.
- **Retained boundary:** every one of ten ordinary selected proposals used the full 0…10 meV,
  21-point window; the recovery attempt used 31 points. The first five inventories did contain
  some narrow parameter-refinement candidates, while the late inventories contained only broad
  opening-book candidates. The audit therefore does not attribute every retained broad winner to
  this one defect—the open-question set and F16/F17 also constrain the pool—but no retained
  decision could reward a cheaper candidate *because* it was cheaper.
- **Impact:** the core “efficient use of neutron time” objective is not implemented in discovery.
  Equal-information waste is proven; near-equal information can buy an arbitrarily wider scan, and
  there is no absolute worthwhileness threshold saying that a positive MI is too small for its beam
  cost. The targeted O43 lane already has a symmetric cost tie, but discovery never consumes it.
- **Remedies:** first add a non-negotiable equal/indistinguishable-information cost tie using the
  *realized* snapped point count and exposure. The full SCENE-5.5 envelope should compare calibrated
  utility per expected realized neutron (or counting time when that is the budget currency), include
  overhead where material, and refuse below a declared worthwhileness floor. Re-score after material
  feasibility/grid changes. Validate ranking and campaign completion on broad-versus-narrow worlds;
  simply dividing by intended window width is insufficient because the adapter, exposure, and
  overhead determine actual cost.
- **Recheck:** independent source trace and reversed-order planner reproduction confirm the result;
  23 focused scene-EIG/FCC tests pass but none asserts cost-aware discovery ranking.

### F24 — The retained first-Q “discrimination” is Monte-Carlo seed noise

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** physically identical predictive designs tie, then the declared catalogue placement
  rank/locality/cost policy chooses. A stochastic estimator may approximate a difference; it must not
  manufacture a stable scientific ordering from independent random streams with no uncertainty gate.
- **Exact retained proof:** the empty-scene first decision contains 19 H=2.05…2.95 designs with the
  same 0…10 meV window, 21 points, exposure, and `temperature_K=None`. The synthetic unknown-family
  contribution is `[5.0] × 21`; after its common 200-count background, the composed predictive rate
  vector is byte-for-byte identical for all 19: `[205.0] × 21`. Their true MI is therefore identical.
- **Actual ranking:** because `_derive_seed` includes `design_key`, the 32-draw estimates use
  independent Poisson streams and spread from **0.0609704 to 0.1382156 nats**. The winner is H=2.6,
  catalogue placement rank **11**, not rank 0. Across 100 alternative base seeds every one of the 19
  H values becomes the winner (2–9 times each). Raising the estimator to 4,096 draws narrows the
  retained-seed range only to 0.0972053…0.1060502 and still produces arbitrary winners. Across 100
  base seeds at R=4,096, all 19 H values still win at least once and the full score envelope is
  0.0929491…0.1088885. Reusing one draw stream for these equal rate vectors makes all 19 values
  exactly 0.1078602.
- **Second failure at a true tie:** even after equal predictions share a stream, `ScoreTable` breaks
  the tie by `design_key`; the counterfactual chose H=2.35, still not the generator's physics-based
  placement rank. The planner carries association ranks but never uses them after a successful
  scene-EIG call.
- **Impact:** the retained campaign's first causal scan—and therefore every subsequent scene,
  candidate count, and placement—was selected by a hash-seeded estimator fluctuation. The calibration
  handoff's statement that the smooth 0.138→0.087 spread demonstrated healthy discrimination is
  false: at that decision the predictive distributions are exactly equal. This does not prove H=2.6
  was a bad measurement after the fact; it proves the controller had no scientific basis for choosing
  it over the declared zone-centre ordering.
- **Remedies:** use common random numbers keyed by the predictive-rate/class problem rather than the
  design identity so identical predictions tie exactly. Attach an estimator error/stability measure
  and adaptively refine only contenders until separated; unresolved candidates fall through to
  realized cost and the existing aggregate placement/locality rank, never a hash. A deterministic
  stratified/quasi-Monte-Carlo estimator can address this together with F18's quadratic cost. Pin an
  exact equal-rate invariant and a near-tie seed-sensitivity world.
- **Recheck:** retained bytes replay exactly at the production base seed; exact rate-array comparison,
  32/4,096-draw sweeps, 100 alternate seeds, and the common-stream control all agree.

### F25 — An executed-grid mismatch remains canonical and EIG-applicable

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** if the returned axis differs materially from the grid whose information/cost was
  authorized, preserve the measurement but invalidate that design authority until the exact returned
  grid is re-evaluated. It must not silently enter certification/pooling as though the selected design
  ran.
- **Actual:** same-length shifted and midpoint-nonuniform returned grids are correctly classified
  `("mismatch", "executed_grid_mismatch")`, and the formal external gate refuses such a case. The
  runtime nevertheless persists `scan_design_executed_grid_status="mismatch"` while leaving
  `scan_design_eig_applicability="applicable"`. Collation filters on `evidence_sink`, not mismatch
  status, so a proposal already marked `canonical_master` remains poolable and can feed later evidence.
- **Important positive boundary:** parsing and fitting use `result.scan_values_1`, not the requested
  grid. A nonuniform `[-1, 0.25, 2.5, 6.75]` axis survived parser→`ScanContext`→merged spectrum→fit
  unchanged. The defect is therefore false selection/certification authority, not fitting counts
  against the wrong x-axis.
- **Impact:** an adapter/instrument deviation can make the exact O43 or discovery design comparison
  inapplicable while downstream state still calls the scan canonical. Depending on the shift it may
  omit target support, change cost/information, or pool a scientifically valid but differently posed
  measurement into the selected action's evidence. The retained reference run did not exhibit this
  injected mismatch, so its numerical fits are not invalidated by F25.
- **Remedies:** on mismatch, atomically mark EIG authority inapplicable and quarantine the record from
  target certification. Prefer retaining it as explicitly auxiliary evidence rather than discarding
  it. If the exact returned grid can be authenticated, recompute realized support, cost, and the
  relevant comparison before promoting it back to canonical. Make runtime and the formal gate consume
  one shared eligibility predicate, and integration-test mismatch through `isar run`→pool→next EIG.
- **Recheck:** shifted and nonuniform dynamic probes, persisted metadata, formal-gate behavior,
  collation source, and returned-axis lineage agree.

### F26 — A short result can be charged as though every requested point executed

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** the measurement ledger and prospective budget reconcile to an authenticated executed
  point count. If the result is ambiguous, fail closed and ask for recovery; do not invent execution.
- **Actual:** with ten requested points, a result carrying six `scan_values_1` values but no executed
  mask/feasible-count field makes `_executed_point_count(..., 10)` return **10**. Budget reconciliation
  consequently charges `10 × ncount`, not `6 × ncount`; no later correction uses the returned-axis
  length.
- **Impact:** neutron accounting can conservatively overstate spend by 67% in the reproduced case and
  stop a campaign before its objective is met. The data axis itself remains six points and fits
  honestly; this is a budget/completion error, not fabricated counts. A payload with contradictory
  lengths also lacks an explicit typed refusal.
- **Remedies:** authenticate executed length from the returned axis/count arrays when their contract is
  internally consistent, record the source of that count, and reconcile both neutrons and elapsed-time
  estimates. Contradictory or absent execution evidence should produce a visible recovery/blocker
  rather than fall back to the requested count. Pin requested 10/returned 6 with and without masks.
- **Recheck:** direct execution-count and `_charge_budget` probe reproduces 1,000 versus the correct
  600 cost units; the focused reconciliation suite has no case for this payload shape.

### F27 — Inverted energy bounds can reach the instrument POST

- **Disposition:** FAIL · **Priority:** P2 · **Confidence:** high.
- **Expected:** `e_lo <= e_hi` and a positive realizable step are proposal invariants checked before
  transport, including replay/recovery inputs. A malformed action must fail locally with a typed
  program/controller error.
- **Actual:** `ScanProposal`, `_guard_point_count`, and request-body validation do not reject
  `(e_lo, e_hi, step)=(7, -2, 0.2)`. ISAR computes one requested point and constructs
  `deltaE 7 -2 0.2` for POST. Current TAVI then raises because its sample count is −44; it does **not**
  silently execute the one-point scan originally suspected.
- **Impact boundary:** all normal live generators inspected normalize, clip, or reject their own
  bounds, so this is a malformed/replayed/recovery controller state and transport-failure risk, not a
  demonstrated reference-campaign beam waste. The downstream TAVI refusal prevents a bad physical
  scan today, but ISAR can misclassify its own invalid request as an instrument outage and retry it.
- **Remedy:** put one finite/order/positive-step invariant at the `ScanProposal`→request boundary and
  reuse it for fresh, resumed, and recovery proposals. Retain generator-local repairs for good error
  messages, but do not rely on every producer being perfect. Pin “no POST” for inverted bounds.
- **Recheck:** exact body-generation probe and current TAVI parser behavior independently reproduce
  the ISAR acceptance and downstream refusal. The nearby legacy coarsening edge is safe: it can
  calculate cap+1 points, but the later point guard refuses before POST.

### F28 — The campaign diagnostic misclassifies a discovery program as an unrecorded crash

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** the shipped campaign diagnostic either understands a `discover_and_map` run—its
  frozen program, agenda state, selected requests, neutron spend, planner/EIG time, fitting time,
  retries, and terminal reason—or refuses explicitly that this campaign kind is unsupported. It
  must not render targeted-lane defaults as facts about discovery.
- **Actual:** `isar diagnose` completed successfully in 1.87 s on an authenticated disposable copy
  of the retained workspace, then reported all of the following in
  `CAMPAIGN_DIAGNOSTIC.md`: objective `(objective unrecorded)`; path `0 grid points`; `0 / 0`
  converged; stop state `(no summary — crashed campaign)`; and `33.3 min total`, of which `32.8`
  min was fitting. It also said there were “No failure zones” while recommending repair of an
  absent targeted `campaign_summary.json`. The same workspace has a valid digest-pinned discovery
  program and agenda, 10 decisions, 11 physical scans, one re-center retry, and a final recorded
  analysis-limit failure.
- **Timing contradiction:** authenticated epoch reconstruction is **22,635.589 s** (377.26 min),
  with **20,625.065 s** in the six dominant scene→proposal gaps and **1,969.426 s** in fitting.
  The diagnostic instead describes only its recognized timing records and presents fitting as
  98.4% of that truncated denominator. F21 is the arithmetic root cause; this finding is the
  broader campaign-kind/schema failure that also loses the objective and stop state.
- **Impact:** the architect-facing report gives exactly the wrong optimization advice for the
  retained campaign: it points at fitting while hiding the 91% planner/EIG bottleneck. It also
  makes a scientifically valid frozen program look absent and gives the operator no decision-level
  view of selected Q/energy grids, EIG uncertainty, information per neutron, or why the analysis
  limit fired.
- **Remedies:** the minimum safe repair is to detect a discovery program and refuse the targeted
  renderer with one explicit “discovery diagnostics unsupported” result. The useful repair is a
  discovery adapter over `report/program`, `decisions.jsonl`, agenda/scene pointers,
  `measurements.jsonl`, and the session log, using epoch wall time as the denominator and reporting
  every selected realized grid, charged cost, selection score/uncertainty, recovery attempt,
  condition partition, agenda blockers, and terminal limit. Keep one renderer only after both
  campaign kinds have an explicit input contract; do not synthesize a targeted
  `campaign_summary.json` from discovery records.
- **Recheck:** source input contracts and the rendered retained artifact agree. The run was made on
  a full disposable copy; the authenticated source workspace was not modified.

### F29 — The shipped FCC acceptance battery does not test the requested endpoint map

- **Disposition:** FAIL (acceptance evidence) · **Priority:** P1 · **Confidence:** high.
- **Expected:** the strongest closed-loop reference world for this audit represents acoustic
  `6·|sin(π(H−2)/2)|` and optic `6 + 2·|sin(π(H−2)/2)|` on H=2…3; exercises ALL, acoustic-only, and
  optic-only objectives; executes the selected windows; and cannot pass “mapped” without accepted
  evidence for every required branch at the H=3 zone boundary.
- **Actual world:** `isar.synth.worlds` gives the FCC acoustic branch the correct 0→6 meV shape but
  defines the optic branch as a flat **9 meV** level. It provides an optic-only `fcc_no_acoustic`
  world but no acoustic-only counterpart for the requested pair.
- **Actual assertion:** `test_full_range_discovery_finds_and_maps_both_phonons` requires only one
  selected fitted energy below 7 meV, one at or above 7 meV, and at least one answered
  `ConfirmPresence` question anywhere. A fresh successful six-round run selected H=2.1, 2.357,
  2.443, 2.529, 2.7, and 2.8; it never measured H=3.0 and still passed. It does not compare fitted
  energies to either declared branch or require the two branches at an endpoint.
- **Window/cost blind harness:** the closed-loop helper intentionally does not execute its selected
  energy window or `ncount`. `isar.synth.generate` supplies the whole fixed-count spectrum, so every
  persisted observation spans approximately −10…+10 meV at `effective_exposure=1.0` regardless of
  whether the selected intent was broad, narrow, or mirrored. The test can exercise planner→fit→scene
  causality, but it cannot certify literal scan-window support or neutron economy.
- **Endpoint mechanism disabled in the fixture:** `fcc_sample_knowledge()` leaves
  `lattice_epoch=None`, while `run_closed_loop()` declares epoch 1 by default.
  `_frame_compatible()` requires the partition epoch to equal the sample-knowledge epoch, so the
  reference run receives no exact fold-anchor ordering/questions. Separately, generic opening-book
  coverage for [2,3] at 0.1 r.l.u. was satisfied by 17 interior nodes from H=2.05 through 2.95,
  with neither endpoint measured.
- **Exact-world bounded follow-up:** after aligning the lattice epoch and substituting the requested
  two-branch truth, a run committed eight observations at H=2.333, 2.667, 2.000, 2.333, 2.000,
  2.333, 2.333, and 2.210 before the 600 s hard limit interrupted planning pass nine. It never
  selected H=3. Every persisted broad, narrow, or negative-mirror window did support the question
  selected for that action; in particular, the −4…−3 meV scan was a legitimate negative mirror of
  a declared 3…4 meV emergent-band hypothesis, not a construction error. The final H=3 presence and
  completeness questions remained open, with no exhaustion evidence. This is honest
  noncompletion, not proof of false completion, but it leaves endpoint completion unverified.
- **Repeated-refinement advisory:** the same optic-parameter question at H=2.333 with the same
  5.980…7.980 meV window was selected three times. The scientific-action fingerprints changed only
  with their evidence bases, while the candidate outputs and effective exposure remained identical.
  Because this harness fixes the full spectrum and cannot add exposure, the trace cannot distinguish
  a production progress defect from a synthetic-acquisition limitation.
- **Impact boundary:** this finding invalidates an acceptance claim; it does not by itself prove the
  production agenda would stop early. In a frame-compatible control, generic region coverage
  closed, H=2 anchor presence closed, H=3 anchor presence remained
  `no_matched_evidence`, and `VerifyCompleteness` stayed open. That is a useful fail-safe. But the
  anchor is class-agnostic, so F08 still prevents it from certifying that *both named branches* were
  mapped to H=3, and no shipped end-to-end test settles the exact objective.
- **Positive fitter boundary:** with the requested exact two-branch truth substituted, direct
  production fits at H=3 recover 6.086 and 8.060 meV with posterior 0.99999975 and no selection
  fallback. At H=2 they recover the finite optic mode at 6.016 meV; the zero-energy acoustic endpoint
  is physically degenerate with the elastic line and should be asserted as a structural endpoint,
  not demanded as a second resolved inelastic peak. The failure is therefore in orchestration and
  acceptance evidence, not demonstrated inability of the fitter to resolve the zone boundary.
- **Remedies:** add one branch-faithful two-mode world and acoustic-only/optic-only controls; align
  the fixture's sample and arena epochs; execute literal selected windows and exposure; assert the
  optic H=2 mode plus acoustic structural zero, both H=3 modes, connected per-branch truth error,
  terminal agenda status, neutron points, and wall split.
  Keep generic `DiscoverRegion` endpoint-agnostic, but add an objective-specific acceptance
  condition for required boundary branches rather than redefining spatial coverage globally.
- **Recheck:** 3 production-shaped closed-loop cases passed in 251.83 s and 94 focused
  opening-book/agenda/boundary cases passed in 1.25 s. A separate exact-truth follow-up reached its
  600 s hard limit after eight committed observations and no H=3 selection; the persisted prefix and
  final open agenda were internally honest. These results reproduce the acceptance and performance
  evidence gaps; they do not satisfy the stronger oracle.

### F30 — The frozen discovery deliverable has no completion semantics

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** `deliverables=[{"kind":"phenomenon_map"}]` is the machine-readable statement of
  what the ALL campaign must produce. Changing it to no deliverable or to an optic-only dispersion
  map must change—or be rejected by—the question and stopping contract. A frozen request must not
  certify a product no runtime stage knows how to fulfil.
- **Actual contract:** `DiscoverAndMapSpec` requires and fingerprints arbitrary non-empty `kind`
  records, describing them as “what the campaign is asked to produce” and assigning semantics to the
  fulfilling stage. There is no such consumer: the only production references to `deliverables`
  are compiler/schema serialization. The schema test explicitly freezes fields “nothing reads yet.”
  Agenda derivation instead constructs its fixed family set from region, energy domain, catalogue,
  `include_unknown`, and question policy.
- **Dynamic isolation:** with the same empty scene, step ID, and program fingerprint, three frozen
  payloads—`phenomenon_map`, an explicit empty list, and
  `dispersion_map` restricted to `branches=["optic"]`—produced the same canonical agenda digest
  `ee746682…fd54b`, the same four question families, and byte-identical assessments. Thus the
  declared deliverable cannot affect what opens, what closes, or when the step reports complete.
  In an actual recompiled program the content fingerprint changes and may perturb F24's random seed;
  that is provenance/Monte-Carlo noise, not fulfillment semantics.
- **Impact:** the retained ALL program's apparent `phenomenon_map` promise is decorative. Its fixed
  class-agnostic agenda can complete under exactly the same criteria as a program asked to deliver
  nothing or only an optic dispersion. This is the contract-level reason “map both branches from
  zone centre to zone boundary” cannot currently be certified, even if the fitter happens to see
  both along the way. F08 (no production identity attachment), F07 (missing two-mode intervals),
  and F29 (weak acceptance world) are downstream manifestations, not substitutes for a deliverable
  contract.
- **Remedies:** first reject every unsupported deliverable kind and either reject an empty list or
  define its honest no-product meaning. Bind the one supported `phenomenon_map` kind to an explicit
  product schema and terminal predicate. For this calibration, that predicate must state whether
  ALL means every represented branch at both endpoints plus connected coverage, while
  acoustic/optic variants name one required branch and allow the other to remain censored. Designing
  those semantics is a foundational owner decision; until it is made, describe `discover_and_map`
  as a fixed generic agenda, not fulfillment of arbitrary frozen deliverables.
- **Recheck:** whole-tree call-site search, schema comments/tests, and the agenda A/B/C agree. No
  code path consumes even `deliverable.kind`.

### F31 — Missed-window recovery can leave its authorized domain and relies on optional validation data for target support

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** every recovered targeted request still contains the eligible target with the same
  minimum grid support required of the original candidate and remains inside the objective's
  authorized energy domain. These are local scientific invariants, not facts which may depend on an
  optional instrument-validation field. If they cannot be met, recovery must refuse and request a
  fresh branch-aware plan.
- **Actual source path:** `handle_missed_window()` receives the proposal and measured maximum but no
  target-family/domain contract; it widens by 1.5× and recenters on that maximum. `loop.py` mutates
  the proposal. Local proposal finalization rechecks point/count/budget/provenance mechanics but not
  the target-core family gates or `energy_search_meV`. Every retry does pass through instrument
  validation before physical submission; that later loop check counts target-support points only
  when the validation reply supplies `point_manifest`. Absence returns `None` and bypasses the
  refusal. Neither layer enforces the frozen energy domain on recovery.
- **Dynamic reproduction:** at a 6 meV acoustic target,
  `[4.4, 7.6] → [5.2, 10.0] → [6.4, 13.6]`. The first recovery already exceeds the compiled acoustic
  domain's +9 meV ceiling while retaining the target, so an otherwise feasible manifest does not
  prevent its physical submission. The second mutation no longer contains the target and exceeds
  both branch domains. The same two-edge replay made **20/20** eligible acoustic and **21/21** optic
  target-core mutations fail their original support invariant. With a complete point manifest, the
  execution loop rejects the low-support mutation before POST; with no manifest, the support gate
  fails open.
- **Physical-submit reproduction:** a production-shaped validator returned `would_queue`, counts,
  and `planned_feasible_mask` but no `point_manifest`. The real campaign loop reached the mock
  client's `submit_scan` four times, for `deltaE -2 7 0.2`, `0.25 13.75 0.2`,
  `3.725 23.975 0.2`, and `8.7375 39.1125 0.2`. These are recorded submissions, not validation
  request bodies. The latter retries are both outside the compiled acoustic domain and targetless.
  Supplying a complete manifest makes the low-support branch refuse once only three support points
  remain, but the first recovery can already exceed +9 meV with adequate support and still submit;
  therefore manifest completeness does not repair the domain defect.
- **False-alarm boundary:** `[0.4, 13.6]` still contains every eligible target center. It is not a
  center-exclusion defect, although at acoustic H=2.05 it supplies only five supporting grid points
  where six are required.
- **Impact:** recovery can spend beam outside the frozen scientist-authorized domain. Its protection
  against a wholly targetless retry depends on richer validation data than the local request contract
  requires, so an adapter omission changes scientific safety from refuse to submit.
- **Remedies:** regenerate or clip every recovery through the frozen target family/domain and run the
  same local minimum-support gate before validation. Treat a missing validation manifest as
  unavailable corroboration, not authorization. If no valid recovered member remains, refuse the
  mutation and issue a new proposal. Keep the attempt lineage, but do not let lineage substitute for
  scientific reauthorization.
- **Recheck:** exhaustive production-grid mutation over every exact-line eligible target and the
  source retry trace establish the domain/fail-open defects. An independent in-memory/mock-client
  verifier reached the real `submit_scan` boundary four times without a manifest and separately
  confirmed the complete-manifest support refusal. No live instrument or source modification was
  involved.

### F32 — The declared same-Q production ceiling does not stop another same-Q scan

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** `maximum_production_batches_per_q=12` is the scientist's bounded same-Q production
  allowance. Once a target has consumed all 12 batches without satisfying a legitimate precision or
  certification gate, the ordinary selector must move, consult, or stop visibly; it must not keep
  emitting the same production action until the global neutron budget happens to run out.
- **Actual rule:** `GridQ.same_q_ceiling_reached` becomes true at `n_production >= maximum`, and the
  session emits a `strategy_ceiling` diagnostic. The real candidate filter excludes that point only
  if it is evidence-only or its track is ambiguous, missing, provisional, or blocked. A confirmed
  target which is still legitimately nonconverged remains eligible with no further same-Q bound.
- **Dynamic reproduction:** a production-shaped confirmed acoustic target had
  `n_production=12`, maximum 12, master halfwidth 0.75 meV, and tolerance 0.5 meV. The real
  `_select_target` returned the *same object* at H=(2,0.1,0), `kind="production"`, method `counts`,
  with a reason requesting another production scan. Both `same_q_ceiling_reached` and the next action
  were therefore true simultaneously.
- **Impact:** a difficult but valid target can consume an unbounded number of identical broad scans
  beyond its frozen per-Q strategy allowance. On the calibration objectives this defeats both the
  declared progress bound and the reason for moving along the dispersion path; only the larger
  campaign neutron/stop limit remains. This is independent of the two synthetic fixture artifacts
  below.
- **Fixture boundaries:** the earlier 76-submission trace omitted `scan_id`, guaranteeing a
  missing-master-key whole-batch fallback. Two later 515/489-submission traces carried valid
  `scan_id` and master keys, but their custom CSV omitted the canonical confirmed-track fields, so
  their 511/485 identical-action runs were also forced nonconvergent. They are not used as causal
  proof. They did reveal F35, and they demonstrate why acceptance fixtures must use the real
  evidence-update producer rather than approximating lineage column by column.
- **Remedies:** make the same-Q ceiling an unconditional exclusion for ordinary production once it
  is reached. If science needs an override, require an explicit separately budgeted action kind with
  a durable reason and operator policy; do not reinterpret 12 as a warning. Route a still-open target
  to a distinct Q/bridge, a model/identity consultation, or a terminal blocked state. Pin confirmed
  and ambiguous tracks at 11/12/13 batches, including a still-wide master and a stale crossing.
- **Recheck:** the 1.1 s real-selector probe supplies the required dynamic mismatch; source traces the
  exact conditional at `acquisition/acquire.py` and the ceiling declaration in
  `supervisor/stopping.py`. No filesystem fixture is needed to reproduce it.

### F33 — The frozen targeted-step reader silently supplies missing scientific policy

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** a payload naming `isar.campaign-step.targeted/1` is frozen effective intent. Missing
  effective fields must be rejected before beamtime, just as the discovery `/2` reader rejects any
  omitted catalogue, deliverable, unknown-policy, or question-policy field. Compact authoring may
  receive compiler defaults; a frozen record may not acquire reader defaults.
- **Actual:** `CampaignStep` validates a targeted payload only as a mapping with the right optional
  schema identity. `spec_from_dict()` treats any schema-bearing payload as already frozen and sends
  it to `ObjectiveSpec.from_dict()`, which requires only `path` and `tolerance_meV`; nearly every
  other effective field is read with `.get(...)` defaults. A valid campaign program containing only
  those fields was accepted as acoustic with `energy_search_meV=(-2,7)`, no provenance source, no
  hypotheses, empty steering, and default scan-window policy. Serializing it afterward materialized
  those choices as if they had been frozen in the input.
- **A/B:** compact acoustic authoring with the same declared branch/tolerance compiled to the
  intended −9…+9 meV search, two frozen branch hypotheses, and authoritative O41. Official compiler
  output is therefore safe; truncated, hand-authored, or third-party frozen programs are not
  fail-closed.
- **Impact:** the sole accepted program contract can begin a targeted beam campaign under scientific
  window, hypothesis, and acquisition policy that the stored document never declared. This also
  makes the file's meaning depend on the reader revision, contradicting its frozen identity and H1.
- **Remedies:** define the full required key set for targeted `/1` and reject every omission before
  constructing `ObjectiveSpec`; keep compact input schema-less and compiler-owned. If backwards
  compatibility truly requires a different field set, mint a new targeted schema and refuse the old
  one by name rather than adapting it at read time.
- **Recheck:** direct `CampaignProgram.from_dict` → `spec_from_dict` reproduction and source trace
  agree; no code modification was used.

### F34 — Discovery completion does not automatically produce a campaign report

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** the reporting contract promises an automatic diagnostic for every terminal campaign.
  A discovery step ending normally—or by a declared budget/analysis limit—must therefore create a
  discovery-aware report, or at minimum a durable explicit statement that this campaign kind is not
  yet reportable.
- **Actual:** the retained workspace ended with `program_step_end`, verdict `over_budget`, reason
  `analysis limit exhausted`, and nine logical deliveries. Before this audit manually invoked the
  diagnostic on a copy, it contained no `campaign_summary.json`, `CAMPAIGN_DIAGNOSTIC.md`, or
  ordinary rendered report outputs. `loop.py` invokes reporting for targeted `run_campaign`;
  `program_runner.py` returns the discovery outcome without invoking either renderer. `isar status`
  and ordinary `isar report` correctly serve their narrower ledger/triage purposes and do not fill
  this campaign-reporting role.
- **Impact:** a completed ALL-mode beam campaign silently leaves no operator post-mortem. If the
  operator discovers and runs `isar diagnose` manually, F28 then labels the valid terminal step a
  crash and reverses its timing attribution. Absence and misclassification are distinct failures:
  fixing only the renderer does not make it automatic, and invoking the current renderer
  automatically would merely automate the false report.
- **Remedies:** add one terminal reporting dispatch shared by both run kinds, with an explicit typed
  input adapter per kind. Land discovery-aware rendering before enabling that dispatch for
  discovery; until then, emit a small honest unsupported artifact rather than nothing. Pin a bounded
  `discover_and_map` integration case from `program_step_end` through artifact creation and content.
- **Recheck:** an independent verifier reproduced F28 in 3.8 s, confirmed the terminal event and
  open agenda, inventoried the pre-render artifacts, and traced the absent discovery dispatch. The
  retained workspace remained untouched and its disposable copy was removed.

### F35 — Rival pooled evidence can overwrite target decision authority

- **Disposition:** FAIL · **Priority:** P1 · **Confidence:** high.
- **Expected:** a targeted grid may retain rival fits as evidence, but the target master, predictive
  score, authority revision, and action identity must all refer to the selected target branch.
  Pooled-row order must not change Q selection.
- **Actual reducer:** `summarize_grid` correctly branch-filters `grid[i].master`, then unconditionally
  overwrites `grid[i].evidence_master` for every pooled row. With ordinary acoustic-then-optic rows,
  the optic row wins. Stop reporting, the target selector, intent construction, and proposal
  authority all prefer `evidence_master or master`, so the rival becomes decision authority even
  while target energy/window prediction continues to use the branch-filtered master.
- **Concrete manifestation:** a frozen acoustic trace reported omega0=1.2 meV from its acoustic
  master but `evidence_branch_id="optic"`; its proposal likewise carried target role acoustic and
  1.2 meV prediction with the optic master's authoritative evidence hash. This state arose with
  valid production-style `scan_id` and master keys; the fixture's missing tracker columns explain
  noncompletion, not the pooled-row overwrite.
- **Decision A/B:** two confirmed, nonconverged acoustic Q candidates had equal cost and stable target
  masters. With filtered target evidence their predictive information was 0.1 versus 0.2 nats and
  `_select_target` chose H=2.2. Replacing only H=2.1's `evidence_master` with an optic row carrying
  0.3 nats flipped the choice to H=2.1. A second A/B kept the target master fixed: target window
  center/spread stayed at 1.2/0.25 meV, but the evidence basis and scientific-action fingerprint
  changed from acoustic to optic.
- **Impact boundary:** this can change which Q/action is purchased and corrupt its durable authority
  revision/fingerprint. It does not directly recenter or resize an already-confirmed same-Q target
  window, because that geometry correctly reads the branch-filtered `master`.
- **Remedies:** branch-filter `evidence_master` under the same target identity as `master`, or store
  evidence masters explicitly per branch and require the caller to name one. Assert target/evidence
  branch and revision consistency at summary, intent, proposal, and replay boundaries. Add reversed
  pooled-row-order and competing-Q A/B tests; never use “last pooled row wins” as authority.
- **Recheck:** production artifact, reducer/consumer trace, 1.2 s real-selector A/B, and the fixed-Q
  authority/fingerprint A/B agree. No source modification was used.

### R01 — Unbounded scene truncation is conservatively qualified, not advertised as certainty

- **Disposition:** candidate H-C **REFUTED** · **Priority:** P3 documentation caveat only ·
  **Confidence:** high.
- The retained current cluster is indeed unbounded: 4,096/3,110,400 assignments,
  `truncated=true`, `truncation_unbounded=true`, no discarded-mass bound, and conditional retained-set
  mode masses up to 0.9999999999998894.
- Production predicates convert this state to vacuous [0,1] intervals and add
  `posterior_truncation_unresolved`; generators do the same. `workspace_api.scene_view` exposes
  `cluster_authority="unresolved"`, sets `conditional_on_retained_set=true`, suppresses the winner
  curve, and retains only qualified markers. No current GUI consumer bypasses that facade.
- The raw numbers need the caveat when quoted manually (as the calibration handoff did), but they do
  not close questions or authorize placements in the production workflow.

### R02 — The retained 300 K declaration did not make scene-EIG score 200 K data at 300 K

- **Disposition:** candidate H-H **REFUTED** · **Confidence:** high.
- The artifact discrepancy is real: `card.policy.toml` and every decision basis declare 300 K,
  while every attempted/trusted arena and all scene footprints are 200 K. The audit initially made
  the invalid inference that the declared-partition value feeds the predictive design.
- Source does something more conservative: `_design_for_evaluation` extracts the partition ID from
  each scientific intent and `_first_partition_temperature` resolves it only against matching scene
  footprints. The empty-scene first decision therefore built 19 Bose-blind designs at `None`; every
  one of the 184 designs in decisions 1–9 joined the measured partition
  `9f6a9c30…784bb` and used **200 K**. No retained design used 300 K.
- An exact counterfactual confirmed the boundary. Changing only the declared partition to 200 K left
  decision 1 byte-identical. It changed cold-start basis/Monte-Carlo seeds and hence the selected
  member of 19 otherwise identical-temperature designs, but it did not turn a 300 K predictive model
  into a 200 K one: both cold starts were Bose-blind.
- **Remaining caveat:** the card's intended setpoint and delivered measurement condition disagree
  without an immediate refusal. Discovery safely moves into a new measured partition after the first
  scan, so this is an operator/provenance visibility question for S18, not evidence that the
  calibration rankings used the wrong thermal rates.

### R03 — Discovery does not always realize the full intrinsic energy domain

- **Disposition:** candidate H-B **REFUTED** · **Confidence:** high.
- Opening-book missing-Q scans, exposure fill, and track bridges use the full domain. Presence,
  identity, mismatch, missing-energy, and parameter-refinement generators declare narrower
  question-conditioned bands. The realizer clips them inward to the TAVI grid and never widens them.
- This is the efficient ALL-campaign semantic adopted in §2: one action may answer one ready
  question while the agenda retains responsibility for every other mode and completeness.
- F15 remains an evidence gap: no production integration test proves an agenda-produced narrow
  action reaches the submitted wire body unchanged. Refuting “always full domain” therefore does not
  upgrade S11 from INCONCLUSIVE.

### R04 — A narrow scan does not erase a mode it never covered

- **Disposition:** candidate H-V **REFUTED** · **Confidence:** high.
- `visibility_of()` classifies a hypothesized band outside the executed footprint as
  `censored_outside_footprint`, not `searched_no_signal`. The presence factor assigns that state
  exactly zero evidence, and a coverage-only arena cannot enter the ambiguity cluster through the
  empty-arena coupling path unless its footprint covers the disputed mode.
- A broad two-mode observation followed by a narrow scan covering only the low branch therefore
  leaves the out-of-window high branch's relative scene score unchanged. The same narrow scan does
  penalize the high branch once its executed bands actually cover that energy and find no mode.
- Three focused footprint/factor/scene-contract tests passed in 0.07 s (0.94 s wrapper). This is the
  correct foundation for branch-specific neutron economy: ignoring the other branch by window is a
  censored measurement, not evidence that the other branch vanished.
- The current `ARCHITECTURE.md` status sentence still calls “out-of-window mode treatment” deferred,
  while H9(e), the live scene design, source, and tests assign and implement it in SCENE-3. That
  wording is stale capability documentation, not a runtime failure established here.

## 8. Recommended repair and authorization gates

The order below is dependency-driven. It deliberately separates independent safety slices so a
large statistical redesign cannot hold crash safety, request validation, or operator stop hostage.

### Gate 0 — contain acquisition authority now

Run discovery and targeted campaigns only in simulation/proposal-only mode or with a human approving
the exact realized request. Keep deterministic reduction/fitting available. Recovery proposals need
the same approval as initial proposals; lineage is not scientific reauthorization.

### Gate 1 — make every purchased scan authorized, recoverable, and stoppable

Land these as independently reviewable slices:

1. **Frozen intent:** reject incomplete frozen targeted payloads, validate the one live catalogue,
   and require the compiler/operator boundary to freeze explicit branch, path, energy, tolerance,
   condition, deliverable, and policy fields (F04, F20, F33).
2. **One pre-POST scientific validator:** on every initial and recovered request, enforce ordered
   finite bounds, the frozen energy domain, target support on the exact snapped grid, point/count
   caps, and authenticated identity. Missing validation evidence must refuse, not authorize. Recheck
   material instrument realizations before submission (F14, F15, F25–F27, F31).
3. **Atomic acquisition/replay ledger:** make submission intent, returned measurement, budget charge,
   analysis attempt, and consultation age replayable and idempotent across every injected crash.
   Interior corruption stops visibly; no paid scan becomes neither recoverable nor attributable
   (F09–F12).
4. **Cooperative stop:** checkpoint planner/score blocks, publish scientific pointers only after a
   successful decision, recheck stop/deadline before POST, and expose discovery activity/limits in
   the live monitor (F18, F19).

Gate 1 exits only when crash injection at every fsync boundary, stop/deadline injection in every
score block, missing-manifest recovery, inverted/domain-escaping requests, and returned-grid/count
disagreement all fail closed without an extra physical submission or double charge.

### Gate 2 — define and prove the scientific product

Bind each supported deliverable to a typed terminal predicate. For this calibration, ALL must require
connected acoustic and optic evidence through H=3 plus an explicit completeness/unknown decision;
acoustic-only must encode the H=2 structural zero and the finite branch through H=3; optic-only must
require its finite H=2 anchor through H=3 while retiring acoustic precision work after identity is
settled. Attach declared branch identities to real evidence, persist per-mode uncertainty for true
  two-mode fits, retire or justify rivals, enforce the condition partition, and give unresolved model
  authority a durable consultation/offramp. Enforce the declared same-Q ceiling and keep pooled
  decision authority branch-aligned (F05–F08, F16, F17, F22, F29, F30, F32, F35).

The exit battery must acquire literal selected windows and exposures in branch-faithful ALL,
acoustic-only, and optic-only worlds. It asserts exact endpoint semantics, connected branch error,
question-specific support, no purposeless repeated action, terminal status, executed neutron charge,
and no false completion. A bounded noncompletion is acceptable only when it names a truthful blocker
and does not keep buying equivalent scans.

### Gate 3 — make EIG statistically comparable and neutron-priced

This is an owner-reviewed statistical-method decision, not a mechanical speed patch. Establish a
replay-stable fixed-total estimator with common random numbers, paired block uncertainty, a persisted
revision/K/seed/backend contract, and a rigorous retained-mass authority bound. Indistinguishable
scores fall through to realized neutron cost and then declared placement/locality; identical
predictions must tie exactly. Persist both raw information and information per realized neutron,
re-score material grid changes, and define an explicit worthwhileness/overhead policy (F16, F23,
F24). Do not silently reinterpret today's per-alternative `mi_n_draws` field.

### Gate 4 — meet the real-tier performance contract

After Gate 3 freezes the estimator semantics, remove zero-mass and discarded-prediction work, cache
rates across questions, and batch the likelihood matrix with bounded memory. Add a persistent
deterministic design executor only if the serial fixed-K/vectorized implementation still misses the
target; use one BLAS thread per child and honor the nested-pool guard. The retained-shape gate is the
F18 contract: ≤2 s for one A=4,096 design, ≤30 s for all 17 designs in three fresh runs, <256 MiB
temporary memory per worker/<1 GiB total scorer RSS, byte-stable replay across order and worker count,
and cancellation within one block (target <1 s) without decision/pointer publication.

### Gate 5 — make the outcome legible

Render event-span wall time with an explicit unattributed remainder, support discovery-program
segments in diagnosis, and invoke one typed terminal reporting dispatch for both campaign kinds.
Status, GUI, and reports must agree on objective, delivered/attempted counts, limits, active work,
terminal reason, condition, and raw-versus-priced selection evidence (F19, F21, F22, F28, F34).

**Authorization sequence:** Gate 1 permits controlled physical integration tests; Gate 1 plus the
relevant Gate 2 branch world permits supervised targeted use; unattended targeted authority also
requires its literal O43 wire and bounded-progress gates. ALL/discovery authority requires Gates
1–4 and the exact two-branch world. Gate 5 is required before calling either mode operator-ready.

## 9. Audit closure and residual unknowns

The audit contract is complete at source `796a84b`. A second-pass verifier in a fresh Codex context returned **CONFIRMED**
with no required corrections: all 34 dispositions and 18 seam states reconcile; the numerical and
calibration-specific boundaries are supported; F31/F32/F35 do not exceed their evidence; and every
remedy is written as proposed work rather than current capability. Documentation drift passed before
and after consolidation. The corrected not-slow and full parallel suites passed with the counts in
rows 59–60. No source or configuration file was changed.

Four limits remain explicit rather than being hidden by a completion claim:

1. The exact two-branch ALL run stayed honestly nonterminal and did not select H=3 before its
   ten-minute bound. The direct fitter resolves both H=3 modes, and no false completion was seen, but
   eventual endpoint selection/completion was not proven.
2. F14 remains INCONCLUSIVE until a genuine O43 target-core winner traverses comparison, proposal,
   adapter, validation, and the submitted request in one production-shaped test.
3. F15 remains INCONCLUSIVE until an agenda-generated narrow discovery action reaches the submitted
   request unchanged while other modes remain correctly open/censored.
4. Live TAVI and sealed held-out evidence were deliberately excluded. They are deployment/milestone
   gates, not missing development evidence which this audit was authorized to consume.

Cleanup note: the interrupted acoustic disposable workspace was removed after its relevant evidence
was summarized. The matching optic workspace `.audit_tmp_f32_faithful_l7tr3cbw/` remains untracked:
recursive deletion was refused because it still contains evidence-bearing logs, and this audit did
not treat silence as authority to destroy them. It is stopped, stable, and not part of production or
the reported test counts. The retained directory contains 988 files / 15,221,959 bytes;
`session_log.jsonl` is 12,208,403 bytes with SHA-256
`2a5a236999c8e8e471a663be85d6ecc27226d046ce681a32b735795a7bfca87f`, and `results.csv` is
142,363 bytes with SHA-256
`d0c1029994d06c8c4acc1ff1f5c63f2e70d7ffd6aa06eb1fd9c5193e6e4ecb20`. Remove it only after the
operator explicitly decides the captured evidence is no longer needed.


## Appendix A — ChatGPT second-opinion review (2026-09-02)

Reviewed the closed ledger and the audited source at `796a84b`; the reviewer did not possess the
retained workspace, so its empirical claims are audit-led. Dispositions of its amendments are recorded
in [plans/campaign-authorization-repair.md](plans/campaign-authorization-repair.md).

### Overall assessment

**Codex’s central verdict is correct:** ISAR at `796a84b` is not ready for unattended, beam-authoritative operation. The audit identifies several independent ways in which the controller can purchase the wrong scientific action, repeat an exhausted action, lose or misattribute a submitted scan across restart, steer from statistically unauthoritative state, or leave the operator unable to see what it is doing. Those are genuine authorization blockers, not cosmetic reservations. 

I checked the corresponding source paths at the audited commit rather than merely reviewing the prose.  The most important findings survive that scrutiny.

That said, I would **accept the audit as the evidence base, but not adopt it verbatim as the implementation plan**. Three things need revision:

1. Its “32 FAIL” headline conflates runtime defects, known unimplemented capabilities, acceptance-test gaps, documentation defects, and corrections to the earlier campaign interpretation.
2. Several remedies—especially F16 and F18—advance from “confirmed failure” into a fairly specific statistical architecture that has not itself received the same level of proof.
3. A few fixes need a better data model than the local patch proposed, particularly multimode identity, rival evidence authority, and acquisition recovery.

There is also an evidence boundary worth stating plainly: I independently checked the source-level mechanisms, but I did not possess the authenticated retained workspace described in §4. I therefore cannot independently attest its manifests, exact 2,436–4,830-second measurements, 300 K→200 K artifact history, or live LLMM samples. Those empirical claims are internally coherent and often strongly corroborated by source, but they remain audit-led rather than independently reproduced here.

#### What the audit does especially well

The audit is unusually disciplined about distinguishing candidate hypotheses from findings and recording refutations. Its corrections of the earlier calibration handoff materially increase my confidence in it:

* F02 correctly notices that the claimed fourfold window saving was calculated against only the acoustic branch even though the ALL objective contains both acoustic and optic modes.
* F03 correctly separates a missed-window recovery scan from a scene-EIG-selected negative-energy corroboration action.
* R02 withdraws the initially tempting—but wrong—claim that 200 K data were being scored using 300 K predictive rates.
* R03 and R04 correctly preserve two useful design properties: discovery really can generate narrow question-conditioned windows, and an out-of-window mode is censored rather than treated as absent.

That willingness to refute its own hypotheses is more valuable than another dozen red findings. It shows Codex was auditing rather than prosecuting.

The dependency order in §8 is also broadly right:

> authorize and recover scans first → define the scientific product → repair statistical comparison → optimize → make it legible.

Correctness before speed. Rather fetching, really.

### Findings I consider decisively confirmed

#### 1. Acquisition, replay, and stopping safety

F09–F12 are real and deserve early repair.

`read_log()` catches a JSON parse error independently for every line and continues, even though its intended tolerance is a torn final append. That means interior history can disappear silently. The diagnostic has essentially the same weakness, so it cannot independently flag the damaged ledger.

The submission sequence also has the crash window described by F10. ISAR writes a pre-submit `submission_intent`, calls the external submission, charges the budget, and then writes both `run` and `physical_submission`, with `run` first. The detailed recovery reducer treats `physical_submission` as the durable job-bearing authority. Consequently, a crash after `run` but before `physical_submission` can preserve spend while losing the record needed to recover the external job.

F11 is similarly direct: `ConsultationChannel` reloads settlements and epochs, but not unresolved prompt issuance. After restart, an unresolved prompt can be re-issued using the current delivery as its new age origin.

F12 is a small but genuine provenance bug: recovered submissions call the common finalizer without restoring the recorded attempt number, so the default attempt zero enters operation identity and timing.

##### My amendment to the remedy

Writing `physical_submission` before `run` is a reasonable immediate patch, but the durable design should not retain two competing “this scan happened” records.

Use one authoritative acquisition lifecycle keyed by `acquisition_id` and idempotency key:

* `prepared`: exact authorized request and idempotency key;
* `submitted`: returned external job ID;
* `completed`: authenticated executed-grid/count facts;
* `analyzed`: analysis operation and outcome.

Spend, scan counters, recovery, and reports should reduce from that lifecycle rather than choosing between `run` and `physical_submission`. Compatibility `run` events can be derived or retained as non-authoritative output. This is not enterprise ceremony; it removes the duplicated authority that caused F10.

The unavoidable submit→first-local-write gap still requires TAVI-side idempotency lookup or reconciliation by idempotency key. Codex acknowledges that correctly.

For F09, the exact repair should be:

* reject malformed non-final records;
* recognize at most one incomplete final record;
* quarantine or truncate that tail before any subsequent append;
* never append behind unparseable bytes.

#### 2. Targeted acquisition authority

F05, F06, F32, and F35 are all genuine, high-value findings.

F32 is particularly clean. The ordinary candidate filter does not unconditionally exclude `same_q_ceiling_reached`; it excludes a ceiling-reached point only for certain evidence-only or ambiguous track states. A confirmed but still nonconverged target can therefore remain eligible after the declared production ceiling.

F35 is equally clear. `summarize_grid()` branch-filters `master`, but overwrites `evidence_master` for every pooled row at that Q. Whichever branch row is encountered last becomes the general evidence authority, while downstream selection frequently prefers `evidence_master or master`.  The audit’s A/B showing a rival row changing Q selection is therefore entirely plausible and source-consistent.

F05 exposes a related lifecycle split: rival classification value can be retired while the rival prediction band remains available to widen the fallback window. F06 shows that target eligibility is being reused as a proxy for rival observability, which fails for the gapless acoustic rival at the optic zone centre.

##### My amendment to the remedy

For F35, do **not** merely branch-filter the one `evidence_master` field. That risks throwing away rival evidence needed for branch discrimination.

Replace the scalar ambiguity with something like:

```text
target_master
evidence_masters_by_branch[branch_id]
unassigned_evidence_master
```

Every consumer must explicitly request either target authority or rival/identity evidence. A target action fingerprint may reference only target-aligned authority; an identity action may cite the complete rival set. Add an invariant that the selected target role, target master branch, authority revision, and action identity agree.

Likewise, F05 and F06 should be fixed through a shared hypothesis lifecycle rather than another special case:

* Is this hypothesis observable at this Q and under this instrument resolution?
* Is it active, resolved, retired, or explicitly reopened?
* Has this exact null-search action already been completed?

The acoustic-at-origin rule can be one implementation of `observable_at`, but should not become another isolated `if acoustic and H == 2` seam.

For F32, Codex’s remedy is right: the ordinary production ceiling must be unconditional. Additional same-Q work should require a distinct, separately authorized override action with a durable reason—not a diagnostic warning that the selector may ignore.

#### 3. Discovery’s scientific completion contract

F07, F08, F16, F17, and F30 together show that discovery can find good peaks without yet possessing a trustworthy definition of “the requested scientific map is complete.”

F07 is source-explicit: `_mode_intervals_of()` returns one `None` per fitted mode whenever a candidate has more than one omega. That is conservatively better than assigning the one existing primary interval to an arbitrary mode, but it makes genuine two-mode precision closure impossible.

F08 is also real. The existing production catalogue does not provide the per-mode identity structure needed for declared acoustic/optic hypotheses to attach to real fitted modes. The repository’s own strict-XFAIL integration test documents that declared acoustic priority does not move real closed-loop placements.

F17 is the straightforward floating-point boundary defect: the exact nominal 0.05-r.l.u. grid can fail the raw `widest > spacing` comparison because some binary differences are slightly above 0.05.

F30 is the more foundational contract problem. `deliverables` are frozen and fingerprinted as what the campaign is asked to produce, but they have no runtime consumer. Changing `phenomenon_map` to an empty or optic-only deliverable does not alter agenda or completion semantics. The field is therefore presently declarative decoration rather than effective frozen intent.

##### My amendment to F07 and F08

Codex proposes “per-mode intervals with explicit slot identity.” The first half is right; raw slot identity is not enough.

Modes can reorder, cross, split, or exchange posterior association. Persist uncertainties against a stable candidate/phenomenon identity, for example:

```text
mode_evidence_id
local_mode_index
energy posterior or interval
covariance/joint-posterior reference
association probabilities to scene phenomena
```

The fitter should publish **anonymous local mode evidence**, not pronounce that slot 0 is acoustic and slot 1 is optic. The scene layer should associate those local modes into persistent phenomena and maintain posterior identities across Q. That preserves the architecture you actually want for ISAR: evidence-backed identity rather than hard-coded energy ordering.

A multimode covariance or posterior reference is also important. Independent scalar intervals can be misleading when two fitted centres are correlated or label-switching.

##### My amendment to F16

The core failure is serious and confirmed: predicates correctly become conservative when the retained assignment set is unauthoritative, but EIG still renormalizes and scores that retained set. The scorer therefore has a more permissive authority contract than the agenda it is supposed to serve. Its cost also scales through an alternative-by-alternative likelihood calculation.

The **immediate repair** should be deliberately boring:

> When any question’s contributing belief cluster lacks certified posterior authority, that question contributes no actionable EIG. If no authoritative question remains, stop with a durable model-authority blocker or consultation.

That makes the current controller safe before solving approximate inference.

The proposed dynamic omitted-mass bound is promising, but the retained-case value such as `−40.711` is not yet a general architectural result. Before adopting it, require:

* a written derivation for every live factor type;
* proof that linkage and future factor additions satisfy the required bound assumptions;
* property tests comparing the bound with exact enumeration on tractable scenes;
* one shared posterior-authority object consumed by both predicates and EIG.

Longer term, factorized inference is sensible, but I would not prematurely promise “forward/backward” specifically. That is ideal for a strict chain; variable elimination, a junction tree, or bounded top-K message passing may better survive richer topology. The contract should be “posterior queries with a certified approximation error,” not a particular algorithm.

#### 4. EIG statistics, cost, and performance

F18, F23, and F24 are among the best parts of the audit.

The source confirms all three structural points:

* the Monte Carlo seed incorporates `design_key`, so scientifically identical predictions receive independent random streams;
* `_mi_from_rates()` compares draws generated under each alternative against all alternatives, producing the reported quadratic scaling;
* final ranking uses weighted raw MI and then a stable design-key tie-break, not realized neutron cost.

The discovery loop checks the stop token before entering planning, but planning is synchronous and there is no corresponding stop/deadline check inside scoring or immediately after planning before continuing toward acquisition. The planner also writes several content-addressed artifacts and pointers during the planning operation rather than publishing one atomic completed decision afterward.

That is enough to uphold the audit’s basic conclusion even without independently reproducing the retained timings.

##### What I would change in the proposed statistical solution

**Common random numbers:** yes—but first detect exactly identical predictive distributions and force exact ties without Monte Carlo. Hash or canonicalize the rate arrays and score one representative. Common random numbers then handle near-equal designs and correlated score differences.

**Uncertainty:** compare candidates using paired uncertainty on the *score difference*, not two independent standard errors. With shared latent draws, the covariance is the useful part.

**Fixed-total K:** a new fixed-total estimator is a sound direction and must indeed receive a new frozen policy revision. But `K=512` and the claimed speedup should remain calibration hypotheses, not architecture constants. Choose K adaptively or from a validated policy table.

**Determinism versus deadlines:** the audit’s wall-clock cancellation proposal needs care. A wall deadline can make the same seed select on a fast machine and time out on a slow one. For replay-deterministic scientific behavior:

* use a deterministic work budget—number of blocks, samples, or likelihood evaluations—as the scientific limit;
* use wall time only as an emergency watchdog;
* a watchdog expiry produces a machine/analysis failure and no decision, never a partial winner.

**Publishing:** rather than moving several pointers one by one after scoring, publish one authoritative decision-commit record that references the already-written immutable inventory, agenda, scores, and selected intent. Unreferenced blobs are harmless. A half-published set of current pointers is not.

**Cost-aware selection:** blindly maximizing `MI / neutrons` is also not quite enough. Ratios can favor tiny, nearly useless scans. A better sequence is:

1. reject actions lacking required scientific support;
2. reject or defer actions below an absolute worthwhileness threshold;
3. compare scientifically distinguishable utilities;
4. when utilities are indistinguishable, select lower realized cost;
5. otherwise optimize a declared constrained or priced objective.

The proven equal-MI, 4.2×-cost case should absolutely choose the cheaper window. That does not require committing the entire system to a ratio objective.

The ≤30-second full-decision target is a sensible authorization gate. The proposed ≤2-second one-design microbenchmark is useful diagnostic scaffolding, but should not become a permanent scientific contract if the representation later changes.

#### 5. Request realization and recovery

F27 and F31 are correctly framed as local authorization defects.

The request body is built from `ScanProposal.scan_command()` without a final universal finite/order/positive-step invariant.  A generator may usually normalize its own values, but replay and recovery must still pass one last controller-owned validator.

For F31, target support is checked only when validation supplies a point manifest; absence yields `None`, which bypasses the refusal. Missed-window recovery then replaces the energy bounds and re-finalizes the proposal without checking the frozen objective domain or re-establishing target support locally.

Codex’s “one pre-POST scientific validator” is exactly the right consolidation point.

I would make the recovery rule stronger than clipping:

> A recovered request must be newly authorized as a member of the frozen objective’s allowed request family.

If recentering around observed counts cannot produce a domain-contained, target-supporting request, recovery ends. The controller then returns to the planner or raises a consultation saying that the apparent signal lies outside the authorized search domain. Silently clipping an invalid recovery may leave a formally valid but scientifically nonsensical fragment.

Missing manifest information must mean “support unverified,” not “support permitted.”

#### One finding whose wording should be corrected: F26

The source supports the defect: `_executed_point_count()` uses an execution mask when present and otherwise falls back to the requested point count; it does not use the returned axis length.

But the audit calls six returned axis values “the correct 600” rather too confidently. Without a contract saying `len(scan_values_1)` is authenticated proof of physically executed points, there are at least two possibilities:

* six points executed;
* ten points executed but only six results were returned because of truncation or transport failure.

So the finding should read:

> Execution accounting is ambiguous and is silently resolved as requested count.

The repair should retain three separate facts:

* requested points;
* instrument-reported executed points;
* returned data points.

Charge from authenticated execution evidence. When those disagree or execution evidence is absent, reserve the conservative upper bound if necessary but **block further unattended acquisition pending reconciliation**. Do not silently substitute returned-array length unless TAVI’s API contract explicitly makes it authoritative.

### Findings that need reclassification rather than rejection

The audit admits that 32 FAILs are not 32 root causes, but the presentation still invites that interpretation. I would reclassify these:

| Finding | Better classification                                                         |
| ------- | ----------------------------------------------------------------------------- |
| F02     | Correction to the earlier calibration interpretation                          |
| F03     | Correction to action provenance in the earlier handoff                        |
| F13     | Normative documentation contradiction                                         |
| F14     | Missing integration proof; remain INCONCLUSIVE                                |
| F15     | Missing integration proof; remain INCONCLUSIVE                                |
| F23     | Known planned capability gap plus demonstrated consequence                    |
| F29     | Acceptance-test insufficiency, not a demonstrated production false completion |
| F30     | Unimplemented product contract, not a local regression                        |

F08 also deserves a “known capability gap” tag because the repository already carries a strict XFAIL explaining it. That does not make it less important; it simply distinguishes unfinished design from a newly introduced defect.

The current P1 population is so large that P1 no longer ranks anything. I would attach two orthogonal labels:

* **authorization effect:** blocks unattended / blocks supervised / does not block;
* **finding type:** runtime defect / missing capability / acceptance gap / observability / documentation / audit correction.

Then group the findings by root cause. For example:

* F07 + F08 + F30: incomplete phenomenon-product contract;
* F16 + F18 + F23 + F24: scene posterior/scoring contract;
* F09 + F10 + F11 + F12: event/replay contract;
* F05 + F06 + F32 + F35: targeted branch-authority lifecycle;
* F19 + F21 + F28 + F34: discovery observability/reporting contract.

That would turn an intimidating red wall into a repairable architecture.

### Changes I would make to the repair gates

#### Gate 0

Keep exactly as written. Proposal-only or exact human request approval is the right current boundary. Deterministic reduction and fitting remain usable.

#### Gate 1

The audit’s Gate 1 is fundamentally correct. I would add two items.

First, bring the **condition mismatch portion of F22** into Gate 1. A declared 300 K campaign that receives a trusted 200 K first measurement should stop before buying scan two unless the frozen policy explicitly permits adoption of measured conditions. That is purchase authorization, not merely later scientific completion.

Second, bring forward two minimal reporting safeguards:

* discovery must appear as active in the GUI;
* `isar diagnose` must refuse unsupported discovery input rather than emit a false targeted report;
* campaign wall reporting must include an unattributed remainder rather than calling the sum of known timers “total wall.”

A rich discovery report can remain Gate 5. Preventing an actively misleading report is safety work.

#### Gate 2

Keep the product-definition gate before major scene-EIG redesign. Until “phenomenon map” has typed completion semantics, it is impossible to say what the optimized planner is optimizing toward.

Within Gate 2, separate targeted and discovery milestones. Targeted operation should not have to wait for the whole ALL-mode scene redesign:

1. fix branch authority, rival retirement/observability, same-Q ceiling, recovery validation, and F14;
2. qualify one branch-faithful targeted world;
3. permit supervised targeted use;
4. continue with multimode phenomenon identity and ALL deliverable semantics.

#### Gates 3 and 4

Correct order: freeze estimator semantics before optimizing its implementation.

Treat Codex’s fixed-K/common-random-number design as a design brief requiring mathematical review, not as an already selected implementation. Preserve a small exact estimator as the validation oracle.

#### Gate 5

Full reporting belongs here, but reports should consume the same canonical acquisition and decision records introduced earlier. Do not build a separate reconciliation model just for diagnostics.

### Acceptance testing should be layered

F29 is right that the shipped FCC battery cannot certify literal energy-window or neutron behavior. The harness itself says it fits the world’s complete spectrum and does not execute selected window or `ncount`.  Its “both phonons mapped” assertion checks only that some selected fitted energy falls below 7 meV, another lies above it, and some presence question closed; it does not require both branches at H=3.

Codex’s proposed branch-faithful end-to-end world is appropriate, but it should not become one enormous test on which every concern depends. I recommend four levels:

| Level                          | Purpose                                                                                                        |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------- |
| Fixed-Q fitter tests           | Prove exact acoustic/optic spectra and per-mode intervals at key endpoints                                     |
| Deterministic planner worlds   | Prove question, identity, censoring, and stopping semantics without expensive full fitting                     |
| Small literal-acquisition loop | Execute selected windows, counts, returned grids, recovery, and charging over perhaps 5 representative Q nodes |
| Full 21-node calibration       | Nightly/milestone authorization evidence for endpoint and connected-map behavior                               |

That gives failures a useful location and keeps ordinary development tolerable.

### Document-level revisions

The audit ledger itself needs a final editorial pass:

* Change **Status: live** to closed/final, or explicitly distinguish “audit closed; remediation open.” Section 9 already says the audit contract is complete.
* Rename “Candidate issues—not findings”; nearly every row in that section now says confirmed or refuted.
* Explain the missing F01 with a tombstone or retired-ID note.
* Use “second-pass” or “fresh-context recheck” rather than “independent” when the verifier is another Codex context. Context isolation reduces anchoring, but it is not independent replication by a different implementation or reviewer.
* Put a two-page decision memo ahead of this ledger: root cause, affected finding IDs, authorization consequence, proposed owner, repair slice, and exit test. Preserve the current document as the evidence appendix.

The audit’s sheer thoroughness is an asset, but right now it makes every loose thread look equally capable of strangling the experiment. They are not.

### Recommended disposition by finding

| Disposition                                                   | Findings                                                                                           |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| **Accept substantially as written**                           | F04, F05, F06, F09, F10, F11, F12, F17, F19, F20, F21, F22, F24, F25, F27, F28, F31, F32, F34, F35 |
| **Accept the finding; revise the remedy or wording**          | F07, F08, F16, F18, F23, F26, F30, F33                                                             |
| **Reclassify as audit/documentation/acceptance correction**   | F02, F03, F13, F29                                                                                 |
| **Keep INCONCLUSIVE and require the stated wire-level proof** | F14, F15                                                                                           |

For F33 specifically, making the targeted `/1` reader strict is scientifically right, but consider compatibility deliberately. If real frozen `/1` programs exist, either mint targeted `/2` and require explicit migration, or provide a one-time offline migration that records exactly which former defaults were materialized. Do not make an old document silently change meaning, but also do not strand existing evidence without a named path.

### Bottom line

**Use this audit to stop unattended authority and to drive the next repair phase.** Its major safety, scientific-authority, and EIG findings are real, and the gate order is mostly excellent. It is considerably better than the calibration handoff it audits because it catches several of that handoff’s own overclaims.

Before handing it to an implementer, I would require:

1. the taxonomy/root-cause rewrite;
2. the remedy amendments for F07, F08, F16, F18, F23, F26, F31, F33, and F35;
3. condition reconciliation and minimal honest diagnostics moved into the first safety gate;
4. a staged targeted-versus-discovery authorization path;
5. F14 and F15 retained as explicit proof obligations rather than quietly converted into presumed fixes.

So: **excellent audit, sound release-blocking conclusion, not yet a fully safe repair specification.** Codex found the right exposed seams; it occasionally became a little too enamoured of choosing the replacement machinery itself. That part still needs your architectural hand on it.
