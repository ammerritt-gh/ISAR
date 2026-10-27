# BL43 data-intake backend: external design review

> **Status:** live
> **Kind:** external design review the operator obtained (2026-10-27) of the BL43 data-intake backend design, kept as received; not maintained with the code

**I’d approve the direction, but ask for a focused revision before implementation.** The scope is sensible, the separation from the GUI job is clear, and the design captures your most important requirement: a saved value must have a predictable effect on subsequent work, while a running fit retains—and exposes—the inputs it started with.

The main gap is that **this guarantee must extend through the calibration and data actually consumed by the fit**. The second is that **“pending calibration” must remain something the colleague can start**, rather than accidentally becoming a reason the GUI disables analysis. Those are the two substantive amendments I would make. The remaining points are smaller scientific reporting and API clarifications.

I read both the [design](https://chatgpt.com/library?file_id=libfile_6fed14be50b881918f84535439b15aad&account_id=personal) and the [discussion](https://chatgpt.com/library?file_id=libfile_9f27295ffbc08191ac5eea318b90d3f1&account_id=personal), and checked the relevant driver, calibration, intake, and lineshape code at revision `1dbcc73961ef`.

## What I would keep

Several decisions are particularly good:

- **The thin format split.** For this job, making the shipped BL43 format definition authoritative delivers the useful part of the catalogue work without rebuilding the broader project.
- **File values and user values retained separately, with explicit precedence.** This supports both scientific inspection and the practical need to correct imperfect beamline metadata.
- **Explicit requeueing after changes.** Marking results stale without automatically starting expensive work is the right behavior for the fast/deep workflow.
- **Direct calls to a small backend writer, with a read-only** `workspace_api`**.** That is an appropriate architecture for the application you have described.
- **Per-use, per-analyzer readiness.** A missing resolution curve should have a precise consequence, while the acquired data remain accessible.
- **Separate freshness checks for the latest fast and deep results.** The design already specifies this in its assumptions; keep it explicit in the GUI contract.

The transcript also supports the change to rule 7. Your concern was always authoritative state and visible scientific inputs. The final design represents that concern much better than the old prohibition on GUI writes.

## 1. Strengthen “a fit uses the saved values” into a complete execution guarantee

This is the most important amendment.

### The current execution path makes this a real integration issue

The existing driver refreshes the tabulated calibrations during batch setup, constructs a run-level calibration view, and collects temperature into the task tuples before dispatching the fits. Consequently, adding a writer and having each result record the latest saved value will not, by itself, make a waiting task consume that value. The execution boundary has to change as well. See the current [batch preparation and task construction](https://github.com/ammerritt-gh/ISAR/blob/1dbcc73961efc08ae03835cddf0abd3eadd556bb/isar/session/driver.py#L260).

Consider an ordinary sequence:

1. A batch is prepared, and scan B is calibrated using an inferred `60 × 60 mm` slit.
2. Scan A starts fitting.
3. The colleague saves `40 × 40 mm` for B.
4. B starts fitting.

The required outcome is that B uses the saved `40 × 40 mm` slit, its corresponding resolution curve, and a compatible energy-zero calibration.

The dangerous implementation would update B’s displayed slit and recorded input metadata while still supplying the earlier calibration object. That would reproduce exactly the scientific desynchronization you are trying to prevent.

The current calibration cache is keyed by parsed scan content and the resolution-library fingerprint, and the run-level view holds the resulting selected slit and per-analyzer zero. Those dependencies need to incorporate the new effective inputs and conditions. See [calibration caching and resolution lookup](https://github.com/ammerritt-gh/ISAR/blob/1dbcc73961efc08ae03835cddf0abd3eadd556bb/isar/session/calibrations.py#L254).

### The design should require one coherent set of inputs

At the execution boundary for each analysis unit, the backend should obtain a consistent snapshot of:


| Input            | What the fit must be tied to                                                                                           |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Measurement data | The imported spectrum revision: energy coordinates, counts, exposure, masks, and other numerical inputs it consumes    |
| Effective facts  | The resolved temperature, declared slit, Q, and their sources                                                          |
| Conditions       | Applicable scan exclusions and dead-analyzer declarations                                                              |
| Calibration      | The selected slit, its selection reason, the actual energy-zero correction and source, and the resolution content used |
| Saved state      | The committed user-entry references needed to explain those choices                                                    |


Then it should check whether an existing calibration is compatible with that snapshot. If it is, reuse it. If it is not, rebuild the affected prerequisites using the existing calibration algorithm before fitting.

**Saving should invalidate the relevant derived state; it should not launch that work.** Recalculation happens as part of the analysis the colleague explicitly requests. That preserves both your no-automatic-refit ruling and the exclusion of scientific algorithm changes.

There is another reason to make the effective input object explicit: the model obtains temperature from `record.temperature.setpoint`. Updating a fact row or a task’s descriptive metadata is insufficient unless the numerical record supplied to the model agrees. The current [context construction](https://github.com/ammerritt-gh/ISAR/blob/1dbcc73961efc08ae03835cddf0abd3eadd556bb/isar/analyze/context.py#L197) makes this worth testing directly.

### Define when “started” occurs

The plan should identify one precise point at which the backend:

- acquires the committed inputs;
- records that the analysis unit has started;
- freezes those inputs for prerequisite calculations and fitting.

A save committed before that point must be included. A save committed afterward must leave that running calculation unchanged and produce the visible discrepancy.

Do this at the actual execution boundary, including where tasks wait inside a worker queue. Freezing everything when the batch is created would violate the agreed behavior for queued work.

Also expose the execution unit clearly. The current task structure is per scan × analyzer, while the design often uses “scan” for the whole analyzer set. The result record and changed-input status need sufficient granularity to explain which analyzer’s work used which revision, even when the GUI groups them together.

### Include changed data, not just changed metadata

The design’s enumerated fit inputs include facts, conditions, and resolution content, but do not explicitly include the measurement-data revision.

A preprocessor can regenerate a file with different counts, exposure, energy coordinates, or binning while retaining its filename, temperature, slit, and Q. After re-import, the old fit must become stale against the changed spectrum.

ISAR already has content hashing and canonical scan/analyzer identities. Reuse those mechanisms rather than introducing another identity system. The existing [session state](https://github.com/ammerritt-gh/ISAR/blob/1dbcc73961efc08ae03835cddf0abd3eadd556bb/isar/session/state.py#L43) and [ingest path](https://github.com/ammerritt-gh/ISAR/blob/1dbcc73961efc08ae03835cddf0abd3eadd556bb/isar/session/ingest.py#L368) provide the starting point.

A concise design amendment could say:

> Every fit is tied to the imported data revision, effective saved inputs, and calibration it actually consumes. When explicitly requested analysis begins, ISAR freezes those inputs and verifies that any reused calibration matches them. Outdated prerequisites are recomputed before fitting. Saving a change marks affected results and prerequisites outdated without starting work.

That closes most of the consequential gap without changing the architecture.

## 2. Separate “ready to produce a result” from “can start analysis”

The readiness section contains a potentially circular contract:

- Calibration remains **pending** until the colleague starts analysis.
- Fitting needs calibration.
- The GUI receives readiness and an “act-or-wait” indication.

A reasonable GUI implementation could enable analysis only for `ready` and `ready with fallback`. That would strand newly imported scans: they cannot become calibrated until analysis starts, and analysis cannot start until they are calibrated.

This is particularly relevant because the GUI and backend are being built separately.

**The backend should explicitly report whether work can be queued and which action resolves the outstanding state.** For example:


| Situation                                                                | Readiness information                 | Available action                  |
| ------------------------------------------------------------------------ | ------------------------------------- | --------------------------------- |
| Data and candidate curves are available; elastic calibration has not run | Calibration pending                   | Queue analysis                    |
| Required resolution curve is unavailable                                 | Held for calibration/fitting          | Locate or correct resolution data |
| A usable calibration exists with a declared fallback                     | Ready with fallback                   | Queue fitting; show the fallback  |
| Analysis is already running                                              | Running state separate from readiness | Observe progress or stop          |
| Analyzer is deliberately declared dead                                   | Excluded from applicable uses         | Reverse the declaration if needed |


A separate `can_start` field or structured action is sufficient. You do not need a larger workflow engine.

The phrase “act-or-wait” is slightly too narrow because a pending calculation is not necessarily progressing. The useful distinction is among **enter information**, **start analysis**, and **work is already running**.

I would also require readiness to use the same underlying checks as execution. Otherwise, import-time readiness and the later stage gates will gradually disagree. This is backend logic; the GUI should receive the conclusion and its reasons.

## 3. Make the existing scientific fallbacks explicit

Keeping the algorithms unchanged is reasonable. Their consequences still need accurate labels, especially for a colleague new to ISAR.

### Missing temperature invokes a physical-model fallback

The transcript says that fitting without temperature “gives the anti-Stokes side zero weight.” That wording is ambiguous.

The current implementation uses the **\\(T\\rightarrow0^+\\) limit of the Bose-factor expression** when temperature is missing, nonfinite, or nonpositive. The unconvolved anti-Stokes phonon contribution therefore vanishes. It does not, by itself, remove the negative-energy measurements from the likelihood; the context’s mask comes from the spectrum and exposure handling. Resolution tails and other components can still contribute there. See [the Bose-factor and DHO implementation](https://github.com/ammerritt-gh/ISAR/blob/1dbcc73961efc08ae03835cddf0abd3eadd556bb/isar/analyze/lineshapes.py#L224) and [context construction](https://github.com/ammerritt-gh/ISAR/blob/1dbcc73961efc08ae03835cddf0abd3eadd556bb/isar/analyze/context.py#L197).

That distinction matters for interpreting the result. Missing temperature changes the physical model being evaluated.

I would preserve the proposed nonblocking behavior, but require all of the following:

- Temperature remains recorded as **missing**.
- Fit readiness says **ready with fallback** when this is the applicable limitation.
- The completed fit retains the fallback reason.
- The explanation says that the existing zero-temperature Bose-factor limit was used.
- Supplying a temperature makes that earlier fit stale.

Do not create a `0 K` file or user fact to represent this fallback. The experimental temperature is unknown; the algorithm has adopted a limiting behavior.

### Missing slit has several distinct outcomes

The design repeatedly describes the missing slit as “inferred from the elastic lines.” The current code has more specific behavior:


| Situation                                        | Existing behavior                                                                |
| ------------------------------------------------ | -------------------------------------------------------------------------------- |
| A slit is declared                               | Retain it; warn if another candidate fits the elastic lines substantially better |
| No declaration, with a usable elastic comparison | Select the best candidate; report ambiguity when applicable                      |
| No usable elastic comparison                     | Select the setting with the broadest curves by a fallback rule                   |
| No usable required curve                         | Hold the affected analyzer                                                       |


The broadest-curve case is not a slit determined from the elastic data. The code already records that distinction in its warning, although its coarse source field uses `"inferred"`. Preserve the meaningful reason when exposing it through the new API. See [slit selection and its broadest-curve fallback](https://github.com/ammerritt-gh/ISAR/blob/1dbcc73961efc08ae03835cddf0abd3eadd556bb/isar/calibrate/tabulated.py#L217).

Before the calculation has run, the correct statement is that candidate curves are available and selection is pending. Afterward, show the selected slit and the actual basis for that choice.

### Candidate coverage is different from required coverage

The import-time coverage check also needs one precise distinction.

In the current algorithm, an analyzer needs curves at every candidate setting to participate in the common slit-comparison calculation. Once a slit has been chosen, that analyzer needs its curve at the **chosen setting** to undergo calibration. These are different requirements. The distinction appears in [the comparison-set construction](https://github.com/ammerritt-gh/ISAR/blob/1dbcc73961efc08ae03835cddf0abd3eadd556bb/isar/calibrate/tabulated.py#L229) and [per-analyzer calibration](https://github.com/ammerritt-gh/ISAR/blob/1dbcc73961efc08ae03835cddf0abd3eadd556bb/isar/session/calibrations.py#L211).

Therefore, a missing curve at an unused candidate slit must not automatically become a fitting hold for that analyzer. Report the incomplete candidate coverage and apply the existing selection behavior.

These are reporting and readiness corrections. They do not require changing the slit-selection algorithm.

## 4. Tighten the save and condition contracts

These are small additions, but they prevent normal beamtime mistakes from becoming authoritative state.

### Specify which things are scan-level and analyzer-level

The phrases “scans, and analyzers where relevant” and “facts for particular scans or analyzers” leave unnecessary room for the two jobs to implement different scopes.

For this job, I would put the following table directly into the contract:


| Item                            | Scope                    |
| ------------------------------- | ------------------------ |
| User temperature                | Scan                     |
| User slit                       | Scan                     |
| Excluded declaration            | Scan                     |
| Dead-analyzer declaration       | Scan × physical analyzer |
| Resolution folder               | Experiment               |
| Descriptive experiment metadata | Experiment               |


That matches the current scan-level temperature and slit paths. It avoids accidentally promising analyzer-specific temperature or slit overrides that the scientific execution path does not consume independently.

### “Parses in its unit” is too weak

Require finite, valid numerical values, not merely successful parsing.

For measured temperature, that means finite \\(T&gt;0\\) in kelvin. For slit, require two finite positive dimensions in the stated horizontal/vertical order.

This is especially important because the existing lineshape implementation treats nonpositive or nonfinite temperature as the cold-limit fallback. Accepting `-97` or `NaN` as a successfully saved temperature could otherwise produce a plausible-looking result with unintended physics.

A **valid slit for which no resolution curve exists** is a different case. Save the truthful experimental value, then report the missing-curve hold. Resolution availability should not determine whether the user is allowed to state the actual slit.

### A missing analyzer must still be a valid condition target

The design says the writer checks that an analyzer “exists.” Define that against the expected physical analyzer identities, including those whose files are absent.

Otherwise:

1. The preprocessing output omits dead analyzer `a14`.
2. The needs-attention list reports its missing file.
3. The colleague declares `a14` dead.
4. The writer rejects the declaration because `a14` is absent from the decoded spectra.

That would make the intended recovery action impossible.

The declaration should suppress that analyzer’s active requirements for the excluded uses while retaining the missing-file observation in the import history. If its file arrives later, the saved declaration should continue to apply until reversed.

Small editorial correction: the design’s example “analyzers 12 and 31” uses an invalid identity. The shipped BL43 list is `a03–a09`, `a14–a20`, `a25–a31`, and `a35–a41`; use `a14` and `a31` in the example. See the [shipped analyzer definition](https://github.com/ammerritt-gh/ISAR/blob/1dbcc73961efc08ae03835cddf0abd3eadd556bb/isar/parsers/cards/bl43lxu.policy.toml#L4).

### “Dead” must apply before shared slit inference

A dead declaration must do more than suppress that analyzer’s individual fit.

The current slit selection combines elastic-fit evidence from multiple analyzers. If a declared-dead analyzer still participates in that comparison, it can influence the resolution selected for its siblings. Exclude it from that input set to fulfill the design’s promise that it participates in no use beyond reading and display. This follows directly from [the shared slit-comparison calculation](https://github.com/ammerritt-gh/ISAR/blob/1dbcc73961efc08ae03835cddf0abd3eadd556bb/isar/calibrate/tabulated.py#L229).

This may legitimately invalidate calibration for other analyzers **within the same scan**. That remains consistent with the ruling that a scan-scoped change does not affect other scans.

## 5. Put these implementation details in the plan

I would settle the following as implementation choices and acceptance examples, without another round of architectural questions.

**One save should have one clear outcome.** For a value applied to ticked scans, validate all targets before committing and use an all-or-nothing operation. Return the accepted canonical value, affected identities, and committed revision or entry reference. A timestamp alone need not be the ordering mechanism.

**Compare effective scientific inputs separately from audit history.** Saving `97 K` again should retain the new audit entry without necessarily making a fit scientifically stale. Likewise, changing descriptive experiment metadata should affect no fit.

**Resolution changes need defined observation points.** Replacing a curve inside the existing `rf/` folder does not change the folder setting. Define when ISAR checks for this—at least during import/re-import and before starting new analysis. A live watcher is unnecessary. The curve content actually loaded for a running calculation must remain fixed.

**An experiment-wide resolution change has experiment-wide dependencies.** Recheck affected scans when the folder or relevant curve content changes. Keep the “one scan affects no other scan” promise specifically about scan-scoped edits.

**Preserve result identity and history explicitly.** The current scan ledger represents current state. The plan should say where earlier fit records live and how they remain tied to their input snapshots after re-imports and refits. A small immutable result record is sufficient; this need not become a new provenance platform.

**Re-import should update the current diagnostic view.** A newly supplied sidecar should resolve its active missing-sidecar item. An unrelated new batch should not erase unresolved gaps from earlier batches. After a shipped-format correction, explicit re-import must actually reparse the files.

Most of the implementation risk is in step 2 of the build order: connecting saved inputs to actual execution and historical results. I would retain the proposed order and give that step the clearest acceptance criteria.

## Acceptance tests I would want

These tests should observe the values consumed by the scientific worker as well as the records exposed to the GUI. Cheap synthetic data and a controlled worker are sufficient; they do not require expensive nested-sampling runs.


| Scenario                                                                     | Required outcome                                                                                  |
| ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| Queue A and B; start A; save a new temperature for both                      | A retains its original input and is marked changed; B starts with the newly committed temperature |
| Save a new slit after calibration exists but before the affected task starts | The task uses the new slit’s curve and compatible calibration                                     |
| Re-import changed counts with unchanged temperature, slit, and Q             | The old result becomes stale against the changed data revision                                    |
| Replace resolution content at the same path                                  | The next defined check detects the change and invalidates affected dependencies                   |
| Import a scan whose prerequisite calibration has not run                     | Analysis can be queued; the API identifies starting analysis as the resolving action              |
| Fit without temperature, or select slit through the broadest-curve fallback  | The result records and exposes the specific fallback                                              |
| Declare an absent expected analyzer dead, then import its file later         | The declaration is accepted, persists, and prevents participation until reversed                  |
| Submit an invalid value or a selection containing an invalid target          | No selected scan is silently modified; the save returns a precise error                           |


I would ask for those focused amendments, then proceed with the existing build order. The thin split, backend/GUI boundary, and scope do not need redesign. **The decisive acceptance criterion is agreement between the committed state, the inputs actually consumed by calibration and fitting, and the inputs reported for each result.**