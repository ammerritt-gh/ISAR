**I would keep the approved layout and request a focused revision before approving the design.** The main workflow is well chosen. The changes I would insist on concern resolution setup, correcting user-entered facts, and showing whether stored fits still correspond to the current inputs.

I reviewed the [design](https://chatgpt.com/library?file_id=libfile_47ea8d01ca4c8191bc0460c6561f0f2a&account_id=personal) against the [attached discussion](https://chatgpt.com/library?file_id=libfile_1122390e721c8191be45d677b1351b1a&account_id=personal). It preserves your substantive decisions well; I found no material reversal of what you agreed to. The findings below concern the written design and its integration contracts. I have not inspected the current implementation or the HTML mockup, which is referenced but not attached.

## What I would preserve

The overall arrangement answers the problems you raised:

- **The full-height scan column gives the principal task the space it needs.** Moving watched-folder controls into a dialog and making metadata inspection optional are sensible ways to recover that space.
- **Creating a persistent experiment on the first drop removes an unnecessary decision from the first minute.** Naming and descriptive metadata can follow once the user has seen their data.
- **Keeping the same window structure before and after ingestion makes the application easier to understand.** The empty interface explains the workflow before anything has arrived.
- **Separating Parsed from the fitting states is scientifically important.** A successful read establishes much less than a successful calibration or a trustworthy fit.
- **Keeping Campaign and the old panels accessible is a reasonable deadline tradeoff.** Their temporary duplication is explicitly accepted, and the design avoids making the BL43 work depend on rebuilding unrelated campaign functionality.
- **The backend/GUI split is appropriate.** The GUI should display stored facts, readiness and results, and use backend writers for scientific inputs. The requirement for named contracts and worked examples is a particularly useful part of the design. [Design, §§2.2–2.11](https://chatgpt.com/library?file_id=libfile_47ea8d01ca4c8191bc0460c6561f0f2a&account_id=personal)

The main concern is that several individual screens are specified more completely than the transitions between them. Those transitions are where a newcomer can get an apparently successful outcome that means something different from what they expect.

## Changes I would request before approval

### 1. Trigger the resolution-folder fallback on the delivered GUI capability

This is the clearest delivery defect.

Section 7 says to add the fallback if the **intake backend has not landed** before beamtime. But the *Locate…* control belongs to **stage two of the GUI**. That leaves an ordinary scheduling outcome uncovered:

1. The intake backend lands.
2. Stage one of the GUI is ready to ship.
3. Stage two of the GUI is still unfinished.
4. The fallback is skipped because the backend exists.
5. The group receives a GUI without a way to select its resolution folder.

That defeats the claim that stage one alone provides the working beamtime path. [Design, §§2.8, 2.11 and 7](https://chatgpt.com/library?file_id=libfile_47ea8d01ca4c8191bc0460c6561f0f2a&account_id=personal)

The discussion makes this consequential: it reports that the existing relative `resolution_folder = "rf"` setting resolves against the copied card’s location, while the actual curves sit beside the incoming data. The proposed chooser addresses a known problem in the described workflow. [Discussion, resolution-folder setting](https://chatgpt.com/library?file_id=libfile_1122390e721c8191be45d677b1351b1a&account_id=personal)

**The correction:** make the release condition about what the group can actually do:

> The beamtime build must let a user select a resolution folder, retain that selection after reopening the experiment, and use the selected curves in analysis. If the complete stage-two GUI does not provide this capability in the delivered build, stage one includes the minimal chooser.

If the backend is available, the minimal chooser calls it. If it is unavailable, the already-described fallback supplies the setting in the agreed format.

This preserves the stage split and avoids unnecessary duplication. It also gives the coordinator a much better question than “Has the backend merged?”: **“Can someone using the packaged application complete this operation?”**

### 2. Make temperature and slit edits correctable and removable

The specified entry route is the needs-attention list, grouped by missing facts. The inspector’s fact table displays file, user and effective values, but no editing actions are specified there. [Design, §2.11](https://chatgpt.com/library?file_id=libfile_47ea8d01ca4c8191bc0460c6561f0f2a&account_id=personal)

That leaves two common cases without a defined interaction:

- A value exists in the file, but the scientist knows it is wrong.
- Someone enters a wrong override, perhaps by typing `79` instead of `97`, or applying a value to the wrong selected spectra.

In the second case, the missing-value item disappears as soon as the value is entered. The user has successfully removed the very entry point they need to correct their mistake.

**The correction:** add two actions to editable rows of the inspector’s fact table:

- **Edit user value**
- **Clear user value**, restoring the file value if one exists

The needs-attention list remains the efficient route for filling many gaps. The inspector provides the continuing route for correcting a particular spectrum. Both use the same backend validation and writer.

For the batch action, show the number and identity of the targeted spectra alongside the field, value and unit before applying it. This can be part of the existing entry surface; it does not need another confirmation dialog. The relevant guarantee is that users can see what they are changing.

Clearing an override should preserve the file fact. If no file value exists, the effective value becomes missing again and the corresponding attention item returns. This makes the file/user/used distinction an operational feature rather than merely a provenance display.

### 3. Connect displayed fit status to the inputs that produced the fit

This is the most important scientific correctness connection.

The scan list displays the states stored by the fitting passes. Stage two permits changes to temperature, slit and the resolution-folder setting. Export reads stored results without refitting. The design does not specify how those three behaviors interact. [Design, §§2.6, 2.10 and 2.11](https://chatgpt.com/library?file_id=libfile_47ea8d01ca4c8191bc0460c6561f0f2a&account_id=personal)

Consider a normal correction:

1. A spectrum is fitted using one slit or resolution curve.
2. The user corrects the effective input.
3. The inspector now shows the corrected value.
4. The previous fit still carries its stored `resolved` state.
5. The user exports.

Without an explicit relationship between the result and its inputs, the interface can put the corrected metadata beside a fit that used something else. Export can preserve the same misleading association.

**The correction:** require the backend to expose whether a stored result matches the current scientific inputs, with a reason when it does not. The GUI consumes that information; it does not work out scientific invalidation independently.

There are three distinct questions to preserve:


| Question                                                    | Example answer                                                       |
| ----------------------------------------------------------- | -------------------------------------------------------------------- |
| What did the fitting procedure conclude?                    | `resolved`, `close_call`, `search_limited`                           |
| What qualification or trust status accompanies that result? | Whatever provisional or trust information the result actually stores |
| Does it correspond to the current inputs?                   | Current, or out of date because the resolution input changed         |


These need not become three extra columns. A row could show **“resolved · inputs changed”**, with the reason and original inputs in the inspector.

The associated behavior should be explicit:

- An old result remains inspectable with the inputs it actually used.
- A relevant input change makes its lack of currency visible.
- Export records the fit’s actual inputs and currentness.
- If old results are included in an export, they remain identifiable as old.
- Changing descriptive details such as the experiment’s notes should not invalidate a fit.

The backend can implement this using whatever provenance or revision mechanism fits the existing architecture. The design needs the guarantee, not a particular hashing scheme.

This also warrants checking how the existing fast- and deep-pass records relate to each other. A later run must not cause the GUI or export to silently combine a selected fit from one run with qualifications or inputs from another. That is a result-selection contract for the backend, with faithful presentation in the GUI.

### 4. Remove “one per temperature” from the analyzer grouping

The analyzer sort is described as making each analyzer a top-level row, with its scan sets, “one per temperature,” underneath. [Design, §2.6](https://chatgpt.com/library?file_id=libfile_47ea8d01ca4c8191bc0460c6561f0f2a&account_id=personal)

Temperature cannot define the identity of those children. Repeated measurements and different Q settings at the same temperature must remain separate scan sets. The example filenames already distinguish more than temperature.

**The correction is a sentence:**

> Grouping by analyzer shows every distinct scan set beneath that analyzer, labelled by its scan-set identity, with temperature and Q shown separately.

Keep selection and result lookup attached to stable spectrum identities when the tree changes orientation. The invented scan number can remain hidden without replacing the underlying identity with a display label.

I would also avoid having a parent row display a single apparently authoritative temperature if its children have differing effective temperatures after overrides. The backend may prevent or resolve that situation; otherwise the parent should say **“mixed”**. That follows directly from permitting edits on exactly the selected spectra.

## Requirements I would make explicit in the implementation plan

These do not require another round of aesthetic decisions. They are bounded choices the plan should settle.

### 5. Give readiness an explicit place in the GUI

The design correctly says that Parsed does not mean calibrated, resolution-matched or ready to fit. It also names the backend’s per-use readiness check. However, the stage-two screens are described mainly in terms of what was read, where the resolution folder was found, and which scalar facts are missing. [Design, §§2.6 and 2.11](https://chatgpt.com/library?file_id=libfile_47ea8d01ca4c8191bc0460c6561f0f2a&account_id=personal)

**Finding a folder is different from finding the required curve for a particular spectrum.** Likewise, having a temperature and slit does not by itself establish that analysis can proceed.

The plan should explicitly use backend readiness in the attention surface and the Run analysis flow. For example:

> **56 spectra read · 52 ready for analysis · 4 need resolution curves**

The affected spectra should be reachable from that summary, with the backend’s reason and the relevant action.

This does not require another permanent scan-list column. A compact summary in Processing or setup is sufficient. Parsed can retain the meaning you approved.

Readiness should also remain specific to the requested operation. If the backend says a missing Q prevents a reciprocal-space display but permits spectral fitting, the GUI should communicate that distinction. It should not turn every missing fact into a universal prohibition.

For stage one, preserve access to the existing held reasons. For stage two, make use of the readiness contract an explicit acceptance requirement rather than assuming the needs-attention wording will cover it.

### 6. Define what “Save copy as…” does to the active experiment

Section 2.4 calls *Save copy as…* the route for relocating an experiment. Section 2.10 says it copies the folder. Those descriptions leave the most important subsequent behavior undecided: **where does the next imported scan or fitted result go?** [Design, §§2.4 and 2.10](https://chatgpt.com/library?file_id=libfile_47ea8d01ca4c8191bc0460c6561f0f2a&account_id=personal)

Two behaviors are possible:


| Behavior        | After the copy succeeds                                          |
| --------------- | ---------------------------------------------------------------- |
| Save a copy     | The original remains active; subsequent work continues there     |
| Copy and switch | The copied experiment becomes active; subsequent work goes there |


I would use the first behavior for a button named **Save copy as…**, and offer **Open copy** in the completion message. Then remove the statement that the action itself relocates the active experiment.

If the intended behavior is copy-and-switch, say so explicitly and make the changed active folder conspicuous. Either choice is workable. Leaving it implicit is what creates the problem.

The plan should settle two related details.

**A coherent copy/export boundary.** The application writes results as they arrive and can ingest watched files. Rename is explicitly refused during an engine command, but copy and export have no corresponding concurrency rule. A completed file copy can still contain a mixture of files from different stages of an update.

For this release, reuse existing command serialization or an existing snapshot mechanism. If the simple implementation makes copy/export unavailable while a modifying command is active, show that reason. There is no need to introduce an elaborate transaction system, but the operation must have a defined consistency boundary.

**External dependencies.** The design preserves existing ingest storage and allows resolution curves to live in a separately selected folder. Consequently, a “whole experiment folder” should not be assumed to be fully portable without checking what existing ingest and calibration storage actually contain.

The plan should establish:

- What can be viewed and exported after the copied experiment is opened elsewhere.
- What source or calibration files are still needed to reprocess it.
- How missing external references are presented.

That is a verification of the promised copy behavior. It need not broaden this GUI job into redesigning raw-data storage.

### 7. Finish the export-selection contract

The export contents are unusually well specified already: all modes, uncertainty kinds, fit parameters, evidence, priors, seeds, masks, calibration references, spectrum data and a units note. Keeping export in the engine and reading persisted results is the right arrangement. [Design, §2.10](https://chatgpt.com/library?file_id=libfile_47ea8d01ca4c8191bc0460c6561f0f2a&account_id=personal)

The remaining questions concern **which records those files describe**.

First, define the export scope. My recommendation is that the current action exports the experiment’s stored results; the selected row and the current tree filter should not silently narrow it. If a future dialog supports selected-spectrum export, that scope can be made explicit there.

Second, name the backend rule that chooses a result when fast- and deep-pass records both exist. The GUI should not introduce a second rule based on whichever row or pass was most recently displayed.

Third, account for spectra with no mode rows. A spectrum with zero accepted modes, a held spectrum, and a spectrum that has never been fitted can all contribute **zero rows to** `modes.csv`. Those outcomes need to remain distinguishable.

A small solution is to require one per-spectrum status record in the existing JSON export, including spectra without a selected fit. It can identify the outcome and reference any available result. This does not need another substantial export format or fabricated zero-amplitude modes.

Finally, make the promise about provisional results concrete: the writer must preserve whatever outcome, trust, qualification and currentness information the persisted result requires. A single count-outcome string should only be treated as sufficient if the actual result schema establishes that it is.

### 8. Protect the scan list’s space when stage two arrives

The design fixes the crowded left column in stage one, then adds a setup section and an expandable needs-attention list in stage two. Without a size rule, the second stage can recreate the original problem. [Design, §§2.2 and 2.11](https://chatgpt.com/library?file_id=libfile_47ea8d01ca4c8191bc0460c6561f0f2a&account_id=personal)

Specify that the persistent setup summary stays compact, and that detailed attention editing has bounded or separately scrollable space. The scan list must retain a useful minimum height even when many spectra need attention.

Likewise, “nothing clips” is weaker than “the application remains useful.” On a laptop, both the inspector and the expanded Processing panel take width from the display. The acceptance case should include both being open together, with the optional columns enabled.

The plan can choose the appropriate splitter limits or collapse behavior within the approved arrangement. What matters is that the centre still supports meaningful spectrum inspection and the scan list still supports navigation.

This is also a reason to test the real Qt widgets in the combined states. The list of screen sizes is good, but dimensions alone do not exercise the difficult arrangement.

### 9. Resolve the interaction between filtering and background updates

The design promises both status filtering and that background updates never lose selection. Those requirements meet in an ordinary case:

- The user filters to spectra that have not run.
- They select one and inspect its spectrum.
- A fit finishes, so that spectrum no longer matches the filter.

The row should cease matching the filter, but the application should not silently replace the displayed spectrum with a different one. [Design, §2.6](https://chatgpt.com/library?file_id=libfile_47ea8d01ca4c8191bc0460c6561f0f2a&account_id=personal)

Keep the displayed spectrum anchored to its stable identity. If it leaves the filtered tree, indicate that the selected spectrum is outside the current filter, or use an equivalent explicit policy.

Also define the filter counts as counts of **spectra**, rather than generic tree rows. Otherwise changing between scan-set and analyzer grouping can change the reported total merely because the number of parent rows changes.

There is a similar distinction in parse summaries: **“27/27 received files parsed”** does not establish that an expected 28-analyzer set is complete. Show completeness separately when the backend supplies an actual expectation; do not infer it from the parse-success denominator.

### 10. Preserve the approved mockup with the design

The written design delegates appearance and arrangement to an HTML mockup in a temporary directory. The discussion explicitly notes that it has not yet been preserved elsewhere. [Design, §2; discussion, final mockup note](https://chatgpt.com/library?file_id=libfile_47ea8d01ca4c8191bc0460c6561f0f2a&account_id=personal)

Copy the approved HTML and representative frames into the durable job material before implementation. Keep the listed textual overrides alongside them.

This is a small housekeeping task, but the mockup is part of the specification. Its availability should not depend on the lifetime of a temporary directory.

## What the backend handoff needs to guarantee

The promised exact signatures and examples are useful. I would make sure their semantics cover the following, because function names alone will not resolve the issues above.


| Contract              | Required meaning                                                                                                                                      |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Scan-list read        | Stable spectrum and scan-set identities, display labels, effective facts, and enough information to distinguish received files from expected coverage |
| Readiness read        | Eligibility and actionable reasons for the particular requested operation                                                                             |
| Fact writer           | Validated set and clear operations on exactly the requested spectrum identities                                                                       |
| Result read           | The stored outcome, any qualifications, actual scientific inputs, and whether those inputs still match                                                |
| Result selection      | One backend rule for the persisted fit that the display and export should use                                                                         |
| Workspace copy/export | Defined active-workspace behavior and a coherent read boundary                                                                                        |


These are additions to the **meaning of the existing cross-job handoff**, not a request for a larger architecture.

## Focused acceptance scenarios

I would add the following cases to the plan. They test the places where this design makes new promises and should expose the consequential mistakes without turning the job into an exhaustive GUI campaign.


| Scenario                                                                            | Required outcome                                                                                          |
| ----------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Backend is available, but the full stage-two GUI is absent                          | The shipped application still lets the user select, retain and use the resolution folder                  |
| Import two scan sets at the same temperature with overlapping analyzer names        | Both remain distinct in every grouping; selecting either displays the correct spectrum                    |
| Enter a missing temperature, correct it, then clear the override                    | Each operation is available; file facts remain intact; readiness updates appropriately                    |
| Change a scientifically relevant input after fitting                                | The old result remains inspectable, its lack of currency is visible, and export retains its actual inputs |
| All files parse, but some required resolution curves are missing                    | The analysis flow identifies the affected spectra and their actionable reasons                            |
| Save a copy, then import another scan                                               | It is unambiguous which experiment is active and which folder receives the new scan                       |
| A selected spectrum stops matching a status filter after a background update        | The displayed spectrum does not silently change to another one                                            |
| Export an experiment containing fitted, zero-mode, held and never-fitted spectra    | The exported records distinguish those outcomes without manufacturing mode rows                           |
| Open the inspector, Processing panel and stage-two attention details at laptop size | The spectrum display and scan list retain useful space, with reachable controls                           |


**My approval conditions would be the first three substantive corrections—resolution delivery, reversible scientific-input editing, and result currentness—plus the scan-identity wording fix.** The remaining points should be explicit obligations in the plan. That gives the implementation team a clear path forward while preserving the layout and scope you have already settled.