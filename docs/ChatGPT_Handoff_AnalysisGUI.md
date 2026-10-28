# ISAR analysis queue GUI: external design review

> **Status:** live
> **Kind:** external design review the operator obtained (2026-10-27) of the right-hand analysis queue design (job j926265e2189c), kept as received; not maintained with the code

**I’d ask for a small revision, then approve this.** The design captures your main decisions faithfully. The remaining issues are concentrated in a few execution and display contracts: how fast and deep work interact for the same analyzer, which inputs a queued fit uses, and what the panel promises when it says “done.”

I reviewed the supplied [design](https://chatgpt.com/library?file_id=libfile_3a64a897a04081918bc354334693801e&account_id=personal) against the [transcript](https://chatgpt.com/library?file_id=libfile_7945f8157fe88191a496cc1cc1a1ca15&account_id=personal). The referenced HTML mockup wasn’t attached, so this is a review of behavior and scope rather than visual layout.

## What the design gets right

The important corrections you made during the discussion have survived into the design:


| Your decision                                                                            | Treatment in the design                                                                              |
| ---------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| The analyzer file is the atomic unit.                                                    | Selection expands into individual items; grouping by scan set is presentational.                     |
| Analysis is continuous.                                                                  | A long-lived process accepts work as resources become available, and results are saved individually. |
| One writer does not imply one analysis at a time.                                        | Concurrent workers feed a single writer.                                                             |
| Fast work takes priority without automatically interrupting deep work.                   | Waiting fast items take available capacity first; a running deep item continues.                     |
| Resource allocation belongs to ISAR as a whole.                                          | The cores setting goes in the top row, and an item may use multiple cores.                           |
| Automatic fast analysis starts enabled; deep analysis is manual.                         | Both are stated explicitly.                                                                          |
| This workflow excludes pooled masters, branch identity, dispersion-prior work, and LLMM. | These exclusions are retained.                                                                       |
| Re-adding current work produces a Log explanation.                                       | The design skips it, including duplicates already queued or running.                                 |


These correspond to the design’s numbered decisions and your explicit corrections in the transcript, particularly the discussions of analyzer granularity, continuous processing, and cross-scan work.

The design also correctly keeps your anticipated hourly scan cadence as **context**, rather than making the scheduler depend on that cadence. That matters: it should behave coherently when the actual beamtime is messier than the estimate.

## Changes I would request before implementation

### 1. Define the relationship between fast and deep work on the same analyzer

**References: design items 1, 4, 6–8; transcript’s initial engine findings, especially “Fast pass always happens.”**

This is the most important omission.

The transcript reports that fresh spectra receive a fast pass and that deep analysis runs on top of it. The design then treats fast and deep as separate queue items and deduplicates only within the same pass.

Consider an ordinary interaction:

1. A new analyzer file arrives and its automatic fast pass starts.
2. The operator selects that analyzer and presses **Deep pass**.
3. There are no more *waiting* fast items, but that analyzer’s fast pass is still running.
4. Some processor capacity remains available.

Under the written scheduling rules, the deep item can now start. The design does not say whether it waits for the fast result, repeats its prerequisite fast fit, uses an older fast result, or operates independently.

Likewise, pressing **Deep pass** on a completely fresh analyzer needs defined behavior.

**My recommended rule for this version: serialize analysis of the same analyzer file, while preserving concurrency across different analyzers.** Let the engine manage any prerequisite:

- Reuse a matching, current fast result when one exists.
- Wait for an already queued or running prerequisite instead of computing it twice.
- If a direct deep request needs a missing fast stage, perform that stage once as part of satisfying the request.
- Preserve a successfully saved fast result if the subsequent deep stage is stopped.

The user should still make one request. This does not need another queue or another control.

There is a small scheduling consequence: FIFO should mean **the oldest runnable item**. If the first deep item is waiting for its own fast result, a later independent deep item should be able to use available capacity. Otherwise the strict FIFO rule can conflict with the requirement to keep usable cores occupied.

### 2. Settle when inputs become fixed, and how results are merged back

**References: the design’s definition of “current,” item 4, and “Per-spectrum fit inputs stay the same”; transcript’s early question about settings being fixed when added or read when run, and your correction about avoiding desynchronization.**

The transcript raises this question explicitly, but the final design never answers it.

Suppose an analyzer is queued, then the operator corrects its temperature, energy zero, resolution assignment, or fitting settings. Does the waiting item use the corrected values? What happens if the correction occurs while it is already running?

This becomes particularly relevant with a long-lived analysis process, because an experiment snapshot loaded when that process started can become outdated.

I would specify:

> Waiting items use the latest committed inputs when execution begins. The engine fixes those inputs for the running task and records their existing provenance or fingerprint with the result. A result computed from older inputs must not be treated as current for newer inputs.

Also recheck currentness at dispatch. The state can change between **Add** and actual execution—for example, a prerequisite or another operation may already have produced the requested result.

The writer needs one related guarantee: **each completion updates its own result without replacing newer experiment metadata or unrelated results.** A single writer prevents simultaneous writes, but a writer can still overwrite newer information if it saves an old whole-experiment snapshot.

A useful ordinary-use scenario is:

- Analyzer A is fitting.
- Analyzer B is imported, or A’s metadata is corrected.
- A finishes.
- Saving A’s result preserves the new import and the corrected metadata.

This belongs to correctness even though visible out-of-date badges and automatic refitting are deferred. It should use the engine’s existing currentness machinery, with the GUI displaying what the engine reports.

### 3. Make “done” mean the writer has saved the result

**References: design items 8, 14–15, and “Export, Save copy and Rename while the queue runs.”**

The main description promises that a finished result is saved immediately, with only a tiny interval between computation and saving. The export proposal introduces a larger, deliberate interval: results may wait in memory while a copy runs, potentially for about a minute.

That is a reasonable implementation possibility, but the status and close behavior must account for it.

For example, several workers could finish while **Save copy** holds the writer. If the panel counts those items as done immediately, the close dialog can claim that every finished result is saved when some are still only in memory.

The minimal correction is:

> An item becomes done, contributes to saved-result counts, and is published to the scan list only after the writer acknowledges saving it. Computed results awaiting saving remain unfinished for shutdown accounting.

You can keep such an item in **running**, or give it a brief **saving** label; that is a presentation choice. The essential point is that it remains accounted for.

Also define ordinary shutdown handling for results already waiting at the writer. Draining that pending work before closing would be sensible. The stated acceptance of losing unsaved work in a crash can remain.

This needs a clear state boundary, rather than additional recovery machinery.

### 4. Preserve limited-result warnings through the Finished summary

**References: design items 4, 9, 15 and 17; “Assumptions and defaults” status vocabulary.**

There is a concrete contradiction here.

Item 9 says that a deep pass stopped by the sampling cap must explain that outcome and **never read as a plain success**. But item 15 compresses finished work into done counts and lists only **held, failed, or stopped** items as exceptions. The status vocabulary likewise has no explicit warning-bearing completion.

That leaves room for this sequence:

- A deep pass reaches its cap.
- Its detailed row initially says so.
- The Finished list aggregates it into an ordinary done count.
- Clearing the list, or reopening the experiment, removes the only visible explanation.

The correction should be simple:

> A saved result can be finished with a warning. Engine-reported limitations remain among the items needing attention and remain available in the stored result view after Finished history is cleared.

For example:

**“Deep pass finished — sampling cap reached”**

The GUI should carry the engine’s outcome and reason, including any other engine-reported incompleteness or trust flag. It should not invent its own scientific validity test.

There is also a necessary check against the duplicate rule: **does a capped or incomplete stored attempt count as an already-done current pass?** If it does, pressing **Deep pass** again will be refused until relevant inputs or settings change. That consequence needs to be explicit. It should not accidentally introduce a rerun feature you excluded, nor accidentally make incomplete work impossible to retry through the intended recovery path.

Retaining the engine cap in this job is a defensible scope boundary. It does, however, leave your concern about overnight incomplete analyses for the fitting work to resolve; removing the budget control from the panel does not resolve that concern by itself.

### 5. Cover leaving an experiment without closing ISAR

**References: design item 14, “Who acts,” and the experiment-folder assumptions.**

The design covers application close, rename, export, campaigns, and a second window. It does not state what happens when the operator chooses **Open another experiment**, **New experiment**, or an equivalent action that replaces the current experiment.

This is a meaningful boundary because the GUI owns the queue while the long-lived analysis process and writer remain attached to an experiment.

For this version, I would apply the same policy as closing the current experiment:

- Switching away with running or waiting work gets the same loss-of-work confirmation.
- Every request and completion remains bound to its originating experiment.
- Accepting the switch stops or disposes of the old experiment’s work according to that policy.
- Late completions cannot update the newly opened experiment.

That is a small lifecycle rule consistent with the existing scope.

## Smaller GUI clarifications worth adding

These are short wording fixes rather than reasons to revisit the overall layout.

### Give counts an explicit meaning

**References: design items 5, 15 and 17.**

The design requires the queue heading and scan-set row to show the same done/total count, but your expected workflow often selects only one analyzer for deep analysis.

For that workflow, both of these can be correct:


| Display                  | Meaning                                                     |
| ------------------------ | ----------------------------------------------------------- |
| **1 requested · 1 done** | The requested queue work is complete.                       |
| **1/28 deep-analyzed**   | One analyzer in the full scan set has a stored deep result. |


Those displays agree about the analyzer while answering different questions.

I would define scan-set coverage from unique stored results, and queue counts from requests. **Clear list** should affect history presentation only; it must not change coverage or duplicate detection. If you prefer whole-set coverage everywhere, label that explicitly so “1/28” does not imply that 27 unrequested deep passes are still outstanding.

### Make the collapsed rail explain inactivity

**References: design items 13 and 16; the opening description of the laptop layout.**

With the panel collapsed, **“0 running · 14 waiting”** does not tell the operator that the queue is paused.

Use **“Paused · 14 waiting”**, and consider a small **needs attention** count for held, failed, or warning-bearing outcomes. Otherwise automatically arriving files can all become held while the default collapsed panel looks merely idle.

### Keep the selected spectrum stable as results arrive

**References: design items 8 and 15.**

Specify that a saved result refreshes the centre display **when that analyzer is already selected**. An explicit **Show** action changes selection.

The present wording probably intends this, but “the centre display shows a result as soon as it is saved” could be implemented as repeatedly jumping to each completed analyzer. That would make reviewing one fit rather tiresome during a busy import.

### Resolve the Finished-list exception for deep results

**Reference: design item 15.**

This item says that only exceptional individual items appear under the summary, then says a finished deep item is listed with **Show**.

Choose one clear rule. Keeping individual deep completions visible may be useful for the anticipated workflow, but it is an exception to “only items needing attention.” The text should say so.

## Three choices are still proposed, rather than already approved

The transcript’s final presentation explicitly asks you to confirm these, and the supplied transcript ends with `/transcribe`:

1. Removing **Run analysis…** from the GUI.
2. Omitting confirmations for removing waiting work and stopping a fast pass.
3. Requiring manual re-addition of held analyzers after missing inputs are supplied.

I think all three are reasonable defaults for the stated first version. The design mostly labels their origin honestly; they should remain visible as choices included in your eventual approval.

One explanation should be corrected: **automatic retry of held work does not technically depend on visible out-of-date badges.** A readiness-change event and a badge are separable features. “Manual retry keeps this version smaller and predictable” is a sufficient justification.

## What planning should demonstrate first

The engine-first sequencing is appropriate. I would ask for a few focused demonstrations before spending much effort polishing the panel:


| Demonstration                                                                | What it establishes                                                                             |
| ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| Automatically fast-pass an analyzer, then request deep before fast finishes. | The prerequisite runs once, and the two requests cooperate correctly.                           |
| Run several analyzers and stop one deep item.                                | Only that item and its own workers stop; neighboring work continues and resources are released. |
| Correct inputs and import another analyzer while a fit runs.                 | The result retains correct provenance and saving it preserves newer experiment state.           |
| Complete fits during Save copy, then close or switch experiments.            | Saved status and lifecycle accounting remain truthful.                                          |
| Complete a capped deep attempt, clear Finished, and reopen its result.       | The limitation remains visible in the stored result.                                            |


Two other checks already fit within the design’s existing boundaries: nested deep worker pools must obey their allocated share of the global cores setting, and this analysis path must exclude **both applying and updating** a learned dispersion prior while retaining the declared per-spectrum hypotheses.

**My recommendation is to keep the design’s structure and scope, and amend these contracts before building.** It has captured the workflow you asked for. The remaining work is to make the transitions between queued, running, saved, and scientifically limited outcomes precise enough that the implementation cannot give the operator a misleading answer.