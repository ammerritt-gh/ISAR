# ISAR Acoustic Phonon Campaign Failure — Engineering Handoff

> **Status: historical — pre-repair analysis, scoped to commit `7b469154`.** This document records
> what was believed about the controller *before* the action-inventory repair. Every behaviour it
> describes as outstanding — the early `evaluate_stop()` ambiguity terminal, `identity_saturated`
> acting as campaign-level evidence, the acoustic 7 meV band hiding the rival — was subsequently
> repaired and ruled on: the P0 action-inventory plan below shipped as DECISIONS **O28** (a
> declarative `GENERATOR_REGISTRY`, exhaustion proved relative to it, the narrow rival probe, the
> separated identity search domain) and the terminal taxonomy was settled by **O29** (amended by
> **O31**). Do not read the findings as current defects or the plan as outstanding work; read
> `docs/MEASUREMENT_STEERING.md` and `docs/DECISIONS.md` for what the controller does today. Its
> value now is as a record of the diagnosis, including the parts that turned out to be right.

**Repository:** `ammerritt-gh/ISAR`  
**Branch:** `repair/ap3-global-evidence`  
**Inspected commit:** `7b469154e108566f3fdce844d909e218dbb9a9c2`  
**Primary area:** tracked acoustic branch acquisition and premature `branch_ambiguity` termination  
**Handoff goal:** make the campaign a reliable `scan → analyze → explain uncertainty → scan` loop that uses remaining beam time to resolve branch identity rather than stopping early.

## Evidence status

This diagnosis is based on close inspection of the repository at the named commit, including the drive loop, acquisition controller, stopping reducer, branch tracker, objective compilation, and related regression tests.

The failed campaign's concrete artifacts were not available during this review. In particular, the following were not inspected:

- `report/session_log.jsonl`
- `report/results.csv`
- `card.state.json`
- persisted pooled spectra and predictive artifacts

Therefore, the report identifies the most likely code paths and the instrumentation needed to prove the exact trigger. It does not claim that a particular terminal flag definitely fired in the failed acoustic run.

---

## Executive summary

The leading failure is architectural rather than a single bad numerical weight.

At the top of each drive iteration, `run_campaign()` calls `evaluate_stop()` before building the classification provider and before calling `propose_scan()`. `evaluate_stop()` can return terminal `incomplete / branch_ambiguity` from local ambiguity and saturation state even when substantial campaign budget remains. Once that happens, acquisition is never asked whether a different scan could resolve the ambiguity.

This allows the controller to treat:

> another comparable exposure at this exact q did not improve the identity posterior

as equivalent to:

> no feasible informative experiment remains anywhere.

That equivalence is false and is contrary to ISAR's core strength. Saturation of one action should remove that action from consideration; it should not terminate the whole campaign.

Several secondary issues make acoustic campaigns more vulnerable than optic campaigns:

1. The tracked acoustic workflow is new relative to earlier successful `main` campaigns, which commonly used local `lowest_energy` selection.
2. The acoustic default search band ends at 7 meV, which can hide an optic rival needed to establish acoustic identity.
3. The drive-side classification score does not directly use the tracker’s leading competing global trajectory assignments.
4. The current rival-gap reconnect logic was shaped around one optic failure pattern and is not a general boundary/crossing recovery strategy.
5. Focused midpoint or narrow-window scans are not first-class recovery actions at the moment they are needed.
6. There is a likely transient-candidate indexing defect in the tracked off-grid probe path.
7. A separate default one-hour deterministic analysis limit can also end an overnight measurement campaign while beam time remains.

The highest-priority repair is to make stopping depend on the same complete action inventory used by acquisition:

> A branch-ambiguity terminal is forbidden while an affordable, feasible, non-repeated information-gathering action exists.

---

## Current control flow and the premature-stop mechanism

### Relevant code

- `isar/drive/loop.py`
  - `run_campaign()`
  - `_blocked_frontier_decision()`
  - `_make_classification_providers()`
- `isar/drive/supervisor/stopping.py`
  - `GridQ.identity_saturated`
  - `GridQ.ambiguity_exhausted`
  - `GridQ.same_q_identity_work_remains`
  - `evaluate_stop()`
- `isar/drive/acquisition/acquire.py`
  - `_tracked_frontier_target()`
  - `_rival_gap_pick()`
  - `_broadening_classification_pick()`
  - `_driver_inserted_candidate()`
  - `propose_scan()`

### Current iteration ordering

The drive loop currently follows this effective ordering:

```text
rebuild pooled masters and branch track
record eligible crossings
evaluate_stop()

if met:
    certify or continue after certification regression
elif state != continue:
    break

build/update acquisition model
build classification provider
propose_scan()
execute scan
analyze and update evidence
```

The problem is the early terminal branch. If `evaluate_stop()` decides `branch_ambiguity`, the following logic never runs:

- predictive or classification scoring;
- midpoint/off-path proposal generation;
- rival-gap recovery;
- focused same-q recovery;
- the later proposal-less controller invariant.

### Why the existing controller invariant is insufficient

`_blocked_frontier_decision()` raises `ControllerInvariantError` when acquisition returns no proposal despite authorized same-q identity work. That is a useful invariant, but it is reached only after `propose_scan()` returns `None`.

A terminal `evaluate_stop()` result bypasses it entirely. The most important invariant currently lives too late in the control flow.

### Required behavioral invariant

Add and enforce this invariant before any scientific ambiguity terminal:

```text
if remaining budget can fund at least one minimum feasible action
and the action inventory contains at least one non-saturated action:
    terminal branch_ambiguity is illegal
```

This should be a hard controller assertion in development and a structured `controller_fault` terminal in production if an impossible state somehow escapes tests.

---

## Finding 1 — `identity_saturated` is action-specific but is used as campaign-level evidence

### Current behavior

`GridQ.identity_saturated` considers the most recent identity-bearing updates. It declares saturation when:

- there are enough updates;
- exposure increased by a configured ratio;
- target probability and identity entropy both improved less than the threshold.

It then contributes to `ambiguity_exhausted`, which can produce terminal `incomplete` in `evaluate_stop()`.

### Why this is not a valid campaign terminal

At most, the calculation says that another **comparable same-q count** is unlikely to sharpen the current identity posterior.

It does not evaluate whether the following could help:

- a midpoint q scan;
- a scan on the opposite side of a boundary;
- a narrow scan centered on the missing rival mode;
- a different q where branch character is clearer;
- a finer energy step around overlapping modes;
- a scan chosen to distinguish the two leading global path assignments;
- a scan that improves candidate admission rather than candidate precision.

### Comparability problem

The identity-saturation window does not require its updates to describe the same scientific state. It should at minimum check a stable key containing:

- q;
- candidate-set hash;
- selected target and rival candidate references;
- pooled-content or master revision;
- fit/model version;
- calibration version;
- branch-track hypothesis-set revision.

A newly appearing candidate or a global reassignment can make the posterior worse. Current logic clips a worsening metric to zero gain and treats it as stronger evidence of saturation. In many cases, a worsening posterior is instead evidence of a model conflict that should trigger a discriminator scan.

### Recommended change

Rename or reinterpret the state as:

```text
same_q_identity_count_saturated
```

Use it only to remove broad same-q counting from the action inventory. Do not let it directly produce campaign termination.

A true terminal state should be something like:

```text
global_action_exhausted
```

and should be produced only after every action class has been explicitly enumerated and rejected with a durable reason.

---

## Finding 2 — visible branches are not the same as confirmed tracked identity

The tracker deliberately keeps all fitted `branch_candidates` while clearing scalar target delivery for ambiguous or disconnected assignments. This means:

- a human can see two clear peaks;
- the pooled fit can be statistically precise;
- `omega0` and `branch_id` for the target can still be withheld;
- convergence remains blocked because tracked campaigns require a confirmed branch identity.

That fail-closed separation is appropriate. The incorrect behavior is stopping rather than gathering association evidence.

### Identity threshold is stricter than the headline probability threshold

The tracker requires both:

- target probability at least 0.85; and
- identity entropy at most 0.2 nats.

For a binary acoustic-versus-optic character decision, 0.2 nats corresponds to roughly 95% probability for the leading character. In practice the entropy threshold is therefore much stricter than the nominal 0.85 probability threshold.

Near a zone boundary, the “acoustic is lower energy” ordering cue is intentionally reduced. That makes continuity evidence more important and can leave two clearly separated peaks below the confirmation threshold.

### Recommended change

Separate two confidence gates:

1. **Continuation gate** — enough confidence to keep walking and gather evidence while preserving uncertainty.
2. **Certification gate** — stricter confidence required to publish and certify target delivery.

Requiring certification-grade confidence at every frontier makes the adaptive walk brittle. A likely continuation should be allowed to extend provisionally, provided the unresolved alternative remains represented in the global posterior.

---

## Finding 3 — tracked acoustic search-band asymmetry can hide the evidence needed for identity

### Current defaults

- acoustic search band: `(-2, 7)` meV;
- optic search band: `(-2, 12)` meV.

The energy-window builder can include a predicted rival band, but the final range is clamped back to the objective’s search band.

### Consequence

A tracked acoustic campaign cannot observe an optic rival above 7 meV under the default settings, even when that optic rival is essential to prove which visible feature is acoustic.

An optic campaign normally has enough search range to include a lower acoustic rival. This creates a practical optic/acoustic asymmetry.

### Recommended change

For `target_selector="tracked_branch"`, resolve the discovery/classification search range from the union of all active branch hypotheses, not from the target branch alone.

Suggested contract:

```text
precision window:
    may be target-focused after identity is confirmed

identity/discovery window:
    must cover all candidate branches required by the current ambiguity explanation
```

The proposal should record both ranges in provenance so a later postmortem can determine whether the rival was physically observable in the emitted scan.

---

## Finding 4 — the drive-side classifier does not score the ambiguity the tracker actually has

### Tracker behavior

`isar/session/branch_tracking.py` performs a global path assignment over all candidate modes. It has enough information to identify leading competing trajectory assignments and where those assignments disagree.

### Acquisition classifier behavior

`_make_classification_providers()` reconstructs generic acoustic and optic Gaussian components from persisted branch-belief rows and computes branch-versus-energy mutual information.

This is not the same question as:

> Which q and energy window would best distinguish the leading competing complete path assignments?

Potential mismatches include:

- branch weights based on `n_anchors + 1`, creating a self-reinforcing advantage for the already better populated branch;
- generic branch uncertainty replacing actual path-assignment posterior mass;
- mutual information becoming zero once branch predictive sigmas are sufficiently narrow, even when a label swap or connectivity ambiguity remains;
- selection of the highest complete temperature slice rather than an explicitly matching campaign slice.

### Recommended change

Have branch tracking publish an `AmbiguityExplanation` with at least:

```json
{
  "revision": 12,
  "type": "trajectory_association",
  "top_hypotheses": [
    {
      "posterior": 0.54,
      "assignments": {"...": "..."}
    },
    {
      "posterior": 0.41,
      "assignments": {"...": "..."}
    }
  ],
  "disagreement_intervals": [
    {
      "path_indices": [14, 15, 16],
      "target_energy_bands": [[5.3, 5.7], [7.5, 7.9]],
      "rival_energy_bands": [[7.4, 7.9], [5.2, 5.8]],
      "cause": "candidate association / missing continuity"
    }
  ]
}
```

Acquisition should score candidate scans by expected entropy reduction over these trajectory hypotheses.

---

## Finding 5 — the rival-gap repair is useful but too specific

The current `_rival_gap_pick()` repair captures a specific pattern:

- ambiguous frontier has at least two fitted candidates;
- a confirmed point behind the frontier has fewer fitted modes;
- a production scan there may restore the missing rival trajectory;
- attempts per gap q are capped.

This matched the documented OP10 optic failure.

It does not cover several acoustic-boundary cases:

- both points have two visible modes, but association remains ambiguous;
- the frontier has only one admitted candidate, disabling the reconnect trigger;
- the best discriminator lies on the other side of the boundary;
- a midpoint is more valuable than any existing grid point;
- the useful point is provisional or blocked rather than on the confirmed run;
- the missing weak mode needs a narrow window rather than another broad production scan;
- two fixed reconnect attempts are insufficient, but a different action would still be useful.

### Recommended change

Keep rival-gap reconnection as one action generator, but do not encode it as a fixed rung in a hard-coded priority ladder.

Every candidate action should be compared using a common utility measure:

```text
expected reduction in global branch/target uncertainty
------------------------------------------------------
expected measurement cost + weighted analysis cost
```

---

## Finding 6 — focused scans need to be first-class recovery actions

Current off-grid midpoint and extension probes are considered late and are not directly generated from a tracked ambiguity explanation.

The following focused actions should be explicit proposal types:

### 1. Missing-candidate scan

Use a narrow, higher-statistics window around a predicted weak rival mode.

Purpose:

- distinguish “mode absent” from “mode not admitted by the broad low-count fit.”

### 2. Separation scan

Move q to maximize:

```text
predicted branch separation / local resolution FWHM
```

Purpose:

- establish character away from a crossing or zone boundary.

### 3. Trajectory-disagreement midpoint

Insert a q where the two leading global path hypotheses predict the most different continuation energies or candidate ownership.

Purpose:

- maximize posterior discrimination directly.

### 4. Character-anchor scan

Temporarily move to a q where acoustic versus optic character is intrinsically clearer, then rerun global tracking.

### 5. Fine spectral scan

Spend the point budget over a compact energy region with a finer step instead of a broad contiguous target-plus-rival range.

### 6. Opposite-side boundary scan

Collect evidence on both sides of the boundary or zone fold when one-sided continuity is underdetermined.

After every focused scan, rerun the full tracker and classify whether the remaining uncertainty is:

- candidate detection;
- candidate precision;
- local character;
- trajectory association;
- continuity gap;
- model conflict;
- genuine action exhaustion.

---

## Finding 7 — likely tracked off-grid candidate indexing defect

`_driver_inserted_candidate()` creates several transient candidates with indices based on `len(grid) + offset`, but returns only the winning candidate. `propose_scan()` appends only that winner to the local grid.

Tracked broadening later passes the candidate’s stored `GridQ.index` into `_classification_link_diagnostics()`, which treats it as an actual list index.

If a later-generated transient candidate wins, its stored index can exceed the one appended list slot.

### Recommended fix

Do not overload `GridQ.index` with both:

- canonical objective-path index; and
- transient candidate identity/list position.

Introduce explicit fields or a distinct transient type:

```python
@dataclass
class AcquisitionCandidate:
    candidate_id: str
    path_index: int | None
    hkl: tuple[float, float, float]
    path_coordinate: float
    inserted: bool
```

Link diagnostics should operate on path coordinate and the set of visited objective intervals, not index directly into a list using a transient identifier.

Add a regression where the second or later generated midpoint/extension candidate wins in a tracked campaign.

---

## Finding 8 — the independent analysis limit can also waste an overnight campaign

Deterministic objectives default to a one-hour `analysis_limit_seconds`. The drive loop checks this separately from the measurement budget and can stop before another submission.

This is a valid protection against runaway analysis, but it conflicts with an overnight campaign if the user expects the instrument to continue gathering useful data.

### Recommended policy

Separate:

- per-analysis-operation timeout;
- rolling analysis duty-cycle or backlog policy;
- whole-campaign analysis hard stop.

A whole-campaign analysis hard stop should be explicit in the objective, not silently inherited as a one-hour default for an overnight measurement run.

At minimum:

- surface the resolved analysis budget prominently at campaign start;
- warn when it is much shorter than the measurement budget;
- do not describe this as scientific incompleteness without naming that analysis guardrail caused the stop.

---

## Target controller architecture

The controller should make one unified decision from a durable action inventory.

```text
1. Analyze all accumulated evidence.
2. Update the global branch-track posterior.
3. Check hard terminal conditions only:
     - operator stopped;
     - objective met and certified;
     - physical infeasibility;
     - measurement budget cannot fund any minimum action;
     - explicit campaign analysis hard stop.
4. Produce an ambiguity/deficit explanation.
5. Enumerate feasible actions from every action class.
6. Remove actions that are:
     - physically infeasible;
     - unaffordable;
     - exact repeats with unchanged evidence basis;
     - saturated for that action class.
7. Rank remaining actions by expected information gain per cost.
8. Execute the best action.
9. Repeat.
10. Return branch_ambiguity only when the durable action inventory is empty.
```

### Suggested action record

```json
{
  "action_id": "trackrev12:midpoint:14-15",
  "type": "trajectory_disagreement_midpoint",
  "hkl": [2.725, 0.0, 0.0],
  "energy_segments": [
    [5.1, 6.0, 0.08],
    [7.3, 8.1, 0.08]
  ],
  "expected_identity_information_nats": 0.31,
  "expected_target_information_nats": 0.04,
  "expected_seconds": 180.0,
  "feasible": true,
  "evidence_basis_revision": "...",
  "rejection_reason": null
}
```

Persist the full ranked inventory at each decision boundary. A terminal report should be able to prove why every action was rejected.

---

## Prioritized implementation plan

> **Historical.** P0 shipped as DECISIONS O28 — though not item-for-item: the terminal moved below
> action generation and now rests on a declarative registry (P0.1/P0.2), identity saturation became an
> action filter (P0.3), and the identity search domain was separated from the target one (P0.5). The
> parts still open are recorded in O28's own status, not here.

## P0 — prevent premature campaign termination

### P0.1 Move ambiguity termination below action generation

**Files:**

- `isar/drive/loop.py`
- `isar/drive/supervisor/stopping.py`

**Change:**

- keep `met`, physical infeasibility, operator stop, unrecoverable instrument state, and genuinely exhausted budget at the top;
- make local branch ambiguity remain `continue` until action enumeration proves no action exists;
- remove early branch-ambiguity terminal from `evaluate_stop()` or make it conditional on a supplied `action_inventory_empty=True` proof.

### P0.2 Make stopping consume acquisition’s inventory

**Files:**

- `isar/drive/acquisition/acquire.py`
- `isar/drive/loop.py`
- `isar/drive/supervisor/stopping.py`

**Change:**

Extract proposal enumeration from proposal selection:

```python
def enumerate_actions(...) -> list[AcquisitionAction]:
    ...


def select_action(actions: list[AcquisitionAction]) -> ScanProposal | None:
    ...
```

Both the loop and terminal decision should use the same returned inventory.

Delete or retire the independent approximation `classification_route_remains()` in `evaluate_stop()`.

### P0.3 Demote identity saturation to action suppression

**File:** `isar/drive/supervisor/stopping.py`

**Change:**

- rename/alias `identity_saturated` to `same_q_identity_count_saturated`;
- remove it as a direct campaign-terminal condition;
- keep it as a diagnostic and same-q action filter.

### P0.4 Fix transient/off-grid indexing

**File:** `isar/drive/acquisition/acquire.py`

**Change:**

- separate canonical path index from transient candidate identity;
- make link diagnostics use path coordinates or explicit interval endpoints.

### P0.5 Expand tracked acoustic identity search range

**Files:**

- `isar/drive/objective.py`
- `isar/drive/supervisor/goal_to_spec.py`
- `isar/drive/acquisition/acquire.py`

**Change:**

- for tracked campaigns, derive identity/discovery range from the union of target and rival hypothesis support;
- retain target-focused windows only after identity is confirmed.

### P0.6 Make analysis hard-stop explicit

**Files:**

- `isar/drive/objective.py`
- `isar/drive/supervisor/goal_to_spec.py`
- CLI/GUI campaign summary code

**Change:**

- reconsider the deterministic default of 3600 seconds;
- at minimum report it prominently and distinguish it from measurement budget exhaustion.

---

## P1 — make the uncertainty choose the scan

### P1.1 Publish a tracker ambiguity explanation

**File:** `isar/session/branch_tracking.py`

Add:

- top-K global track hypotheses;
- posterior mass;
- disagreement q intervals;
- candidate ownership differences;
- detection versus association versus character classification;
- suggested energy bands for discrimination.

Persist it in `card.state.json` alongside `branch_tracks`.

### P1.2 Add focused action generators

**File:** `isar/drive/acquisition/acquire.py`

Add generators for:

- missing-rival narrow window;
- midpoint/refinement q;
- maximum-separation q;
- opposite-side boundary q;
- fine-step overlap scan;
- character-anchor scan;
- current rival-gap reconnect;
- same-q exposure.

### P1.3 Score all action types through one utility function

Use a common score based on expected global posterior reduction and cost. Do not rely on fixed rung ordering except as a deterministic tie-breaker or safety policy.

### P1.4 Make identity saturation comparable

**File:** `isar/drive/supervisor/stopping.py`

Require stable candidate/model/track provenance. Reset the window when the scientific state changes.

### P1.5 Select the correct branch-belief slice

**File:** `isar/drive/loop.py`, `_make_classification_providers()`

Use campaign sample and campaign temperature explicitly. Do not select the numerically highest complete temperature slice as a proxy for the active campaign.

---

## P2 — improve branch character around boundaries and crossings

The current tracker strongly discourages acoustic/optic character handoff. That is useful against accidental relabeling but may be too rigid near a true crossing or avoided crossing.

Clarify the scientific target semantics:

- energy-ordered acoustic character;
- adiabatic continuous eigenmode;
- diabatic branch character;
- or an unresolved mixture retained until another observable distinguishes it.

Preserve multiple track hypotheses when the data cannot distinguish these meanings.

Where available, incorporate intensity, polarization, structure factor, or other character observables as evidence. These should improve the posterior but must not be mandatory for controller progress.

---

## Test plan

### Unit tests: stopping and controller invariants

**File:** `tests/drive/test_stopping.py`

Add tests asserting:

1. `identity_saturated=True` alone does not produce terminal `incomplete` when another action exists.
2. A large remaining budget plus nonempty action inventory forbids branch-ambiguity terminal.
3. A candidate-set change resets the identity-saturation window.
4. A track reassignment resets the identity-saturation window.
5. Worsening posterior under changed candidates becomes model conflict, not saturation.
6. Terminal branch ambiguity requires an empty durable action inventory.

### Unit tests: tracked frontier action generation

**File:** `tests/drive/test_tracked_frontier.py`

Add cases for:

1. two visible, precise branches with target probability between 0.85 and 0.95;
2. same-q saturation while a midpoint scan remains;
3. same-q saturation while an opposite-side scan remains;
4. both frontier and neighbors have two modes but ownership is ambiguous;
5. frontier has one admitted candidate and a predicted weak rival;
6. rival above 7 meV in an acoustic campaign;
7. later-generated transient candidate wins without indexing failure;
8. disconnected but locally clear frontier chooses connectivity repair, not more counting;
9. ambiguous local identity chooses information-gathering scan rather than terminal state.

### Unit tests: branch tracking

**File:** `tests/session/test_branch_tracking.py`

Add cases for:

1. top-K hypotheses and disagreement intervals are stable and deterministic;
2. acoustic trajectory near a zone boundary remains represented without forced premature confirmation;
3. crossing/avoided-crossing alternatives are retained when evidence is insufficient;
4. a missing mode is treated as detection uncertainty rather than evidence for character reversal;
5. the ambiguity explanation changes after a focused discriminator scan.

### Unit tests: classification provider

**File:** `tests/drive/test_loop.py` or a new focused test module

Add cases for:

1. active campaign temperature is selected explicitly;
2. branch weights reflect the relevant hypothesis posterior rather than anchor count only;
3. precise but label-swapped trajectories still have nonzero discrimination value;
4. acoustic identity windows can include an optic rival above 7 meV.

### Campaign replay gates

Required before declaring production safety:

1. Replay OP10 unchanged and retain success.
2. Replay AP1, AP2, and AP3 as tracked acoustic campaigns.
3. Replay prior successful `main` acoustic campaigns with original frozen selectors.
4. Replay equivalent objectives under new tracked semantics.
5. Run at the production entropy threshold, not only relaxed synthetic thresholds.

Record for every replay:

- terminal state;
- scans and total cost;
- action inventory at each ambiguity;
- target probability and identity entropy trajectory;
- candidate-set revisions;
- number and type of focused recovery scans;
- whether any action was repeated after its own saturation;
- optic regression status.

---

## Acceptance criteria

The repair is acceptable when all of the following hold:

1. No campaign returns `branch_ambiguity` while a feasible, affordable action remains in the inventory.
2. `identity_saturated` suppresses only comparable same-q counting.
3. Every ambiguity terminal contains a durable proof that all action classes were infeasible, unaffordable, repeated, or saturated.
4. Tracked acoustic scans can observe all rival hypotheses needed for identity.
5. Midpoint and focused-window actions can be selected before full-path survey completion.
6. The tracked off-grid candidate path is index-safe.
7. AP1/AP2/AP3 replays no longer terminate prematurely.
8. OP10 remains successful.
9. Earlier frozen `main` campaign semantics remain reproducible.
10. Analysis-limit termination is clearly distinguished from scientific or measurement-budget termination.
11. The controller never repeats an unchanged action after its evidence basis and expected utility have both remained unchanged.
12. Recovery scans measurably reduce posterior mass on competing track hypotheses or change the ambiguity type; otherwise that action class becomes saturated and another class is tried.

---

## Exact diagnostic checklist for the failed acoustic campaign

Use the failed workspace to determine which path actually fired.

### 1. Inspect terminal event

From `report/session_log.jsonl`, locate the final `stop` and preceding `decide` events.

Record:

- state and reasons;
- iteration;
- measurement budget amount and spent;
- analysis budget amount and spent;
- frontier indices;
- whether `proposal` was ever attempted on the final iteration.

### 2. Inspect frontier `per_q`

For each blocked or ambiguous point, capture:

```text
hkl
track_state
track_revision
target_probability
identity_entropy
local_identity_clear
identity_saturated
exposure_saturated
strategy_ceiling_reached
n_production
max_production_batches
evidence_branch_id
branch_candidates
selected_candidate_index
master_revision
```

### 3. Inspect the last identity-bearing pooled updates

Compare the last three `pooled_master_update` events at the frontier:

```text
effective_exposure
target_probability
identity_entropy
track_revision
selected_candidate_index
branch_candidates
content_hash
master_revision
candidate_state
pool_consistent
```

If candidate set, selected candidate, track revision, or content identity changed, the current saturation window was not comparable.

### 4. Inspect emitted windows

For every recovery attempt, record:

```text
e_lo, e_hi, e_step
n_points
predicted target band
predicted rival band
configured search band
feasible point manifest
```

Determine whether the rival branch was physically inside the submitted scan and whether the point cap coarsened the step enough to harm separation/admission.

### 5. Inspect branch-tracker alternatives

From `card.state.json`, inspect:

- `branch_tracks.assignments`;
- candidates and probabilities at the frontier and neighbors;
- confirmed/provisional/blocked segments;
- acquisition seed and reduced-q origin;
- branch belief models and their sample/temperature slices.

### 6. Check the analysis limit

Verify whether `analysis_limit_seconds` or an analysis checkpoint caused the stop independently of the measurement budget.

### Likely signature of the primary premature-stop path

```text
state = incomplete / branch_ambiguity
identity_saturated = true
local_identity_clear = false
n_production < maximum_production_batches_per_q
measurement spent << measurement budget
no action inventory was generated on the terminal iteration
```

---

## Suggested PR breakdown

### PR 1 — terminal safety and diagnostics

- move ambiguity terminal below action generation;
- add action-inventory invariant;
- demote identity saturation;
- fix transient indexing;
- improve terminal reporting;
- add P0 tests.

### PR 2 — tracked acoustic scan visibility

- union hypothesis search range;
- active temperature selection;
- target/rival window provenance;
- acoustic replay tests.

### PR 3 — ambiguity explanation and focused actions

- publish top-K track hypotheses;
- add midpoint, narrow-window, separation, and opposite-side actions;
- unify scoring.

### PR 4 — boundary/crossing semantics

- clarify acoustic/optic character target;
- preserve crossing alternatives;
- add character-observable hooks;
- full campaign regression matrix.

---

## Final design principle

The controller should never use “confused” as a stopping reason by itself.

Confusion is a model state. It should be converted into a concrete question, such as:

- Is the second mode physically absent or merely too weak for the current fit?
- Which continuation owns this candidate across the boundary?
- Are two global assignments still plausible, and where do they disagree most?
- Is the local branch character clear but the path disconnected?
- Has one specific action stopped paying, or are all possible actions exhausted?

The next scan should be chosen to answer that question. Only when every affordable way of answering it has been exhausted should the campaign stop incomplete.
