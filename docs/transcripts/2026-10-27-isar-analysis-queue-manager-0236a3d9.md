# ISAR analysis queue manager

**Date:** 2026-10-27  
**Session:** 0236a3d9-8a69-4cc8-b12f-3cb1e9214ca1

*Cleaned transcript: tool calls, tool results, thinking and harness notices removed. Nothing else edited.*

## User

We'll work on designing the analysis/processing section for ISAR; we have a session working on building the LHS, this session will then work on the RHS; design-only for now, because we'll have to build off of what the other session is handing, and wait until its slice 2 finishes.

Our goal: turn the RHS into a queue manager, letting us select scans on the LHS, and then add them to the queue to process with fast or deep analysis modes, any settings that need to be made readily available will go here too. But what does a user need besides selecting which scan(s) to add, as fast or deep, and managing the queue?

/dialogue 
/ui-professional

## Assistant

Reading the GUI design doc and today's left-side ruling before I size this; one more pass, then the first question.

Recon is reading the analysis path in the background; I'm sending the request to the cold-reading panel now.

I've read the window design (G24 in `docs/design/gui.md`, which puts Processing on the right), this morning's left-side transcript, and the current Run path (a Luna recon read it). I also ran a cold-reading panel on your question. Your question gets answered first, then the first decision.

## Size: a small region, close to large

The screen part is clear. The engine facts below make this bigger than a panel redesign:

- **`isar run` can't analyse chosen scans.** It takes file or folder paths plus detector IDs. There is no scan selector, so "add scans 412–418" needs a small engine change first.
- **Order can change the numbers.** Each run starts from the dispersion prior and cross-scan evidence the earlier runs left. Pooled masters and branch identity are recomputed across scans. So queue order isn't purely cosmetic.
- **Fast pass always happens.** Every fresh spectrum gets a fast pass on any run, and `--deep-pass` runs on top of it. The GUI doesn't offer deep pass today at all; the Run dialog offers uncertainty tiers instead.
- **Rough cost.** Fast pass takes about 80–90 s per analyzer spectrum (le Tacon handoff, measured). Deep pass is "tens of minutes per spectrum" (the CLI's own description, never timed) with a 60-minute budget each. A deep pass on a 28-analyzer set could plausibly take hours. That's my estimate.
- **Stop is a hard kill.** It empties the whole queue, kills the subprocess, and may lose fits that are mid-flight. There's no per-spectrum progress, only log lines.
- **The engine already knows what's stale.** It refits only when content, guidelines, calibration or deep settings changed. There's just no mark for it on screen yet.

## What a user needs besides picking scans, fast/deep, and the queue

The panel and I merged our lists. Strike what you don't want:

1. **How work arrives.** Either the user adds every scan by hand, or new scans are fast-passed automatically. ISAR already reads newly arrived files in on its own.
2. **What an item carries.** Its settings could be fixed when it's added or read when it runs. Some settings may also need overriding for one scan only.
3. **Order and priority.** Move an item to the front, or pause after the current item. This matters more because order can change results.
4. **Re-queueing a scan that already has a result.** The old result stays visible until the new one lands. Fast and deep already show side by side, with nothing picking one (G24).
5. **Three kinds of stop.** Stop now, stop after the current item, and clear what's waiting. Each says where it takes effect.
6. **The outcome of each item.** Done or failed with the reason, a click that opens the result in the centre plot, and *Re-queue*.
7. **Progress and time.** Count spectra done of the total (11/28), show elapsed time measured, and label any time estimate as an estimate.
8. **Queue state on the scan rows.** The left-side rows show queued or running too, so the two sides never disagree.
9. **Surviving a closed app.** A deep batch might run overnight.
10. **Out-of-date results.** When a fit's inputs change (temperature, slit, settings), the item is marked and offered for re-queueing. Stage two already plans the mark.

## The first question: how do scans get into the queue?

**Why now.** It decides the panel's main control. If the answer is by hand, the panel is built around *Add selected*. If new scans are processed automatically, the main control is a switch and the queue mostly shows what the switch did. Most of items 3, 5 and 9 depend on this.

**The situation.** During the beamtime, files land in a watched folder and ISAR reads them in without a click. Your goals file ("Collecting Data") describes ISAR "automatically work[ing] while an experiment is ongoing". This morning you said: "We need then to be able to make a batch of scans to process with fast or deep modes."

**My preference:** scans are added by hand, plus one switch per experiment, *Fast-pass new scans as they arrive*. Scans added automatically appear in the same queue, marked as automatic. Deep pass is only ever by hand, because it costs hours. The alternatives:
- **By hand only.** Simplest and fully predictable. But at 3 am every new scan needs a click before anyone sees a fit, which works against ISAR's purpose as fast feedback for steering.
- **Automatic rules first,** with the queue as their log. It's rejected because you asked for a queue you build, and a rule engine is a bigger feature.

**Where I want the push.** Should the switch start on or off for a new experiment, and is deep pass really by hand only? You may want a rule like "deep-pass anything the fast pass flags". I left that out as scope growth, but it's your call.

## User

/t1

## Assistant

I've opened the job as a T1 design: **`j926265e2189c`**, *Design the ISAR GUI's right-hand Processing panel as an analysis queue manager*. This session is bound to it.

- You typed `/t1` without a goal, so the goal is your opening message, close to word for word.
- When it's built, it lands only if the quick test suite (`pytest -m "not slow"`) passes. You don't sign the landing yourself.
- Nothing is built yet, and nothing will be until the left-side session's second slice (`isar-d9`) has landed. The design waits in the job's working file meanwhile.

**The first question is still open: how do scans get into the queue?**

This decides the panel's main control. If scans are added by hand, the panel is built around *Add selected*. If new scans are processed automatically, the main control is a switch and the queue mostly shows what the switch did. Priority, the kinds of stop, and surviving an app restart all depend on it.

During the beamtime, ISAR already reads newly arrived files in without a click. Your goals file describes ISAR "automatically work[ing] while an experiment is ongoing".

- **My preference:** scans are added by hand, plus one switch per experiment, *Fast-pass new scans as they arrive*. Scans added automatically sit in the same queue, marked as automatic. Deep pass is only ever by hand, because by my estimate it takes hours per 28-analyzer set (the CLI says "tens of minutes per spectrum"; never timed).
- **By hand only:** simplest and fully predictable. But at 3 am every new scan needs a click before anyone sees a fit.
- **Automatic rules first, with the queue as their log:** I rejected this. It's a bigger feature than the queue you asked for.

**Where I want the push:** should the switch start on or off for a new experiment? And should deep pass ever start on its own, say on anything the fast pass flags? I left that out as scope growth, but it's your call.

## User

Automatic is optional, a toggleable setting to automatically take new scans with a fast pass, otherwise manually. So your first option.

## User

Start on, deep pass does not start on its own.

## Assistant

Two rulings recorded: auto fast pass starts on, and deep pass never starts on its own.

**Decision: how scans enter the queue** (2026-10-27)

- **Question, as put:** How do scans get into the queue: by hand only, by hand plus a switch that fast-passes new scans as they arrive, or automatic rules first with the queue as their log?
- **On the table:**
  - By hand only (predictable, but every new scan needs a click).
  - By hand plus an automatic fast-pass switch (the queue holds both kinds, with the automatic ones marked).
  - Rules first (a bigger feature than a queue).
- **Ruling:** "Automatic is optional, a toggleable setting to automatically take new scans with a fast pass, otherwise manually. So your first option."
- **Ruled by:** operator
- **Why:** the experiment can keep up with arriving data on its own, while the user still decides everything else.
- **Constrains:**
  - The panel's main control is *Add selected*.
  - One automatic-fast-pass switch per experiment.
  - Scans added automatically sit in the same queue, marked as automatic.

**Decision: default and the limit on the automatic route** (2026-10-27)

- **Question, as put:** Should the switch start on or off for a new experiment, and should deep pass ever start on its own?
- **On the table:**
  - The switch starts on, or off.
  - Deep pass is by hand only, or it can also start automatically, for example on anything the fast pass flags.
- **Ruling:** "Start on, deep pass does not start on its own."
- **Ruled by:** operator
- **Why:** fast feedback is ISAR's job during a beamtime. Deep pass costs hours per scan set (my estimate, never timed), so a person always chooses it.
- **Constrains:**
  - A new experiment fast-passes arriving scans unless the user turns the switch off.
  - Nothing queues a deep pass except a person.

## Next question: is this queue meant for the le Tacon beamtime next week?

**Why now:** the answer sets how big the first version is, and everything after this depends on it. Two parts need engine work, not just panel work:

- **Choosing scans.** `isar run` can't be told to analyse particular scans today. It takes file or folder paths, so *Add selected* needs a scan selector added to the engine.
- **A gentle stop.** Stop today kills the run and empties the whole queue, and fits that are mid-flight may be lost. "Stop after the current item" is easy in the panel. "Stop between spectra and keep what's finished" needs the engine's cooperation, like the campaign's stop already has.

The left-side session's second slice has to land before any of this is built.

**If the queue is for the beamtime,** I'd cut the first version to:
- *Add selected* with fast or deep.
- The automatic fast-pass switch.
- The queue list, with each item's status and spectra done (11/28).
- *Remove* on a waiting item, plus *Stop after current* and *Stop now*, each saying what it keeps.
- Queue state shown on the left-side rows.

These wait for a second version: reordering, a queue that survives closing the app, settings overridden for one scan, and the out-of-date marks.

**If it's not for the beamtime,** I'd design all of it now, in one piece.

**Where I want the push:** whether "next week" still holds, and whether the le Tacon group would run deep passes at the beamtime at all. If deep pass is really an after-the-beamtime job, the first version can leave deep out of the panel entirely, and the engine work shrinks.

## User

Yes, it's meant for that. This minimal version seems reasonable; the optional parts are definitely secondary concerns.

## Assistant

Recorded. The minimal version is now the plan for the beamtime.

**Decision: the first version is for the le Tacon beamtime, cut to the minimum** (2026-10-27)

- **Question, as put:** Is this queue meant for the le Tacon beamtime next week?
- **On the table:**
  - For the beamtime: a minimal first version with *Add selected* (fast or deep), the automatic fast-pass switch, the queue list with status and spectra done, *Remove*, *Stop after current* and *Stop now*, and queue state on the left-side rows.
  - Not for the beamtime: design everything at once.
- **Ruling:** "Yes, it's meant for that. This minimal version seems reasonable; the optional parts are definitely secondary concerns."
- **Ruled by:** operator
- **Why:** the beamtime is the deadline. The extras don't change what the group can do there.
- **Constrains:** reordering, a queue that survives closing the app, settings overridden for one scan, and out-of-date marks are deferred to a second version. Deep pass stays in the first version.

## Next question: what happens to new scans while a deep pass is running?

**Why now:** this is the one place where your two rulings meet the engine's limits. It decides whether "automatic fast pass" actually gives fast feedback during the beamtime.

**The situation:**
- ISAR runs one analysis at a time per experiment, so two runs never write to the same folder at once.
- A deep item on a 28-analyzer scan set fits its spectra in parallel. Each spectrum has a sampling budget of up to 60 minutes, so the item could plausibly occupy the machine for about an hour. That's my estimate; deep pass has never been timed.
- Meanwhile new scans arrive, and the switch queues them for a fast pass, about 80–90 s per spectrum (measured).
- With a plain first-come queue, those fast passes wait behind the whole deep item.

**My preference: fast items go ahead of waiting deep items, and a running deep item is never interrupted on its own.** The panel then says plainly what the new scans are waiting for, for example "3 fast passes wait for the deep pass of scan 412, running since 20:14". If the group wants the fast feedback sooner, *Stop now* on the deep item lets the queue move on. The deep item can be re-added later.

**What I rejected:**
- **First come, first served:** at 3 am a deep item started before bed holds back every new scan for an hour.
- **Pausing the deep pass automatically when a scan arrives:** the engine can't pause and resume a deep pass today. Its stop is a kill. Building a cooperative stop would make this possible, but it's engine work the beamtime doesn't need.
- **Running fast and deep side by side:** that breaks the one-writer-per-experiment rule that protects the results. Removing it is a much larger change.

**One fact still to check (planning checks it before the build):** I don't know whether a killed deep run keeps its finished spectra, so that a re-added item only redoes the unfinished ones. If it doesn't, *Stop now* on a deep item throws away up to an hour's work. The panel would have to say so before it stops.

**Where I want the push:** would the le Tacon group actually start deep passes while scans are still arriving? Or are deep passes for the gaps and overnight? If the latter, this question mostly goes away, and first-come order is enough.

## User

I think this is reasonable; we have a fast and deep queue, and the fast queue drains ahead of the deep without interrupting any ongoing deep queue job without the user's intervention. My guess is that we will have close to an hour or more between scans, so my *guess* of their workflow is take in a single scan (28 analyzers), run a fast pass (one analyzer or all), then a deep pass on the main analyzer, meanwhile they are working on the next scan to get to temperature and align; all in all, I think as one scan finishes, we will have at least one hour before the next scan finishes. That gives ample time to finish one deep pass and possibly more. Our goal time for the deep pass was one minute each, and we have a bunch of optimizations to make, but we will see. The experiment will be running 24 hours a day, so it's more how they want things going during the experiment and where their priorities lie, but they will also have multiple laptops, and their experiment is not waiting on the results from these analyses to proceed.

## Assistant

Recorded. Your workflow guess helps, and I'm keeping it labelled as a guess.

**Decision: a fast queue and a deep queue** (2026-10-27)

- **Question, as put:** What happens to new scans while a deep pass is running?
- **On the table:**
  - First come, first served (new scans wait behind a deep item).
  - Fast goes ahead of waiting deep items, and a running deep item is never interrupted on its own.
  - The deep pass pauses automatically (needs engine work).
  - Fast and deep run side by side (breaks one writer per experiment).
- **Ruling:** "we have a fast and deep queue, and the fast queue drains ahead of the deep without interrupting any ongoing deep queue job without the user's intervention."
- **Ruled by:** operator
- **Why:** fast feedback stays fast. A deep item someone started is never undone behind their back.
- **Constrains:**
  - The panel shows two queues, fast and deep.
  - The next item is always taken from the fast queue while it holds anything.
  - Only the user stops a running deep item.

**What I'm carrying from your account, as a report, not a requirement:**
- One scan set of 28 analyzers arrives about every hour or more (your guess).
- The group fast-passes one analyzer or all of them, then deep-passes the main analyzer.
- The experiment runs around the clock, on several laptops, and never waits on these results.
- The deep pass aims at about 1 minute per spectrum. That's a target, not met yet; today's per-spectrum budget allows up to 60 minutes.

**One consequence I've taken as given, which you can correct:**
- A queue item is one scan set plus the analyzers chosen. Selecting the set row means all 28 analyzers, and selecting analyzer rows means just those. The engine can already limit a run to chosen analyzers.
- Automatic fast pass takes all analyzers of a new scan.
- Each laptop runs its own ISAR on its own experiment. The queues don't coordinate across laptops.

## Next question: which settings go in the panel?

**Why now:** this is the second half of your request ("any settings that need to be made readily available will go here too"). It also decides what happens to today's *Run analysis…* dialog.

**What exists today:**
- The *Run analysis…* dialog offers a data folder, four uncertainty methods (full, fast, cheap, bayes), the number of bootstrap draws, the analyzer IDs, an opt-in LLM check of quality flags, and *Force refit*.
- Nothing in the GUI offers the deep pass's time budget per spectrum, or how many processor cores ISAR uses.
- The fitting choices themselves live in the experiment's hand-edited `card.policy.toml`: the energy window, the maximum number of peaks, the expected excitations. Hard rule 1 says code never writes that file, so the GUI can't edit them.

**My proposal:**
- **Always visible:** fast or deep, chosen when adding; the automatic fast-pass switch; the analyzers, taken from the selection.
- **Under a *More settings* section, closed by default** (it affects items added after the change):
  - The deep-pass time budget per spectrum, because the 1-minute target and the 60-minute default are far apart.
  - How many cores ISAR may use, because the laptop also runs the alignment software.
- **Dropped from the panel:**
  - The data folder: data comes from the experiment.
  - The uncertainty method and bootstrap draws: the fast pass keeps its default and the deep pass brings its own.
  - The LLM check.
  - *Force refit*: re-adding a scan does the same thing (next question).
- **The *Run analysis…* dialog goes away.** The command line keeps every option.

**When I'd change this:** if the group needs to change a fitting choice at the beamtime, such as the energy window or the maximum number of peaks. That can't go in this panel without breaking hard rule 1. It would need its own design, moving those choices into the store the GUI is allowed to write, and that's a separate job.

**Where I want the push:** does the group need any of the card's fitting choices during the beamtime, or is the card set once before they start?

## User

We had this discussion with the other session about hard rule 1, the GUI can edit settings and things, my goal with that was to avoid desyncs. I'm going to be blunt here that "budgets" have no real use for an operator; what is your expectation here, that a user will set a queue with a 60 minute budget per item, have each 70-minute analysis fail at 60 minutes and come back in the morning to a half-dozen failed analyses instead of four done ones? Come on...

Number of cores should be a top-row config setting, same as in TAVI, because it's not specific to analysis but overall for ISAR. That leaves no setting that the users need visible here (for right now), which is somewhat what I expected.

The operators will NOT have access to LLMM or anything with it, so we do not include LLMM at all. It has to assume that's not available, and can't fail because of it.

## Assistant

You're right on both counts, and both were my mistakes.

- **Hard rule 1.** I quoted it as "the GUI can't edit settings", which isn't what it says. It forbids ISAR overwriting a user's values on its own. Hard rule 7 has the GUI send the user's settings down to ISAR, which is what you and the other session built it for: one copy, so the screen and the fit can't disagree. A setting the group needs can live in the GUI. It just isn't needed in this panel yet.
- **The budget.** A time limit only turns a slow analysis into a failed one, and you'd find out the next morning. It doesn't belong on the user's screen. Whether the engine's own 60-minute cap should exist at all is a question for the fitting work, not this panel.

**Decision: no analysis settings in the panel for now** (2026-10-27)

- **Question, as put:** Which settings go in the panel?
- **On the table:**
  - The deep-pass time budget and the core count, under *More settings*.
  - The uncertainty method, the bootstrap draws, the LLM check and *Force refit*, dropped.
  - The *Run analysis…* dialog, removed.
- **Ruling:** "Number of cores should be a top-row config setting, same as in TAVI, because it's not specific to analysis but overall for ISAR. That leaves no setting that the users need visible here (for right now)"
- **Ruled by:** operator
- **Why:** the number of cores is a setting for all of ISAR, not for analysis, and nothing else on the list helps the group at the beamtime.
- **Constrains:**
  - The panel holds no settings section.
  - Cores go in the window's top row, as in TAVI.
  - No time budget appears anywhere for the user.
  - Removing the *Run analysis…* dialog wasn't objected to, so I'm carrying it forward.

**Decision: no LLMM** (2026-10-27)

- **Ruling:** "The operators will NOT have access to LLMM or anything with it, so we do not include LLMM at all. It has to assume that's not available, and can't fail because of it."
- **Ruled by:** operator
- **Constrains:**
  - Nothing in the queue or the panel calls or offers LLMM (ISAR's gateway to language models).
  - Analysis runs without it, and doesn't fail when it's absent.

**One scope note.** I'm putting the cores setting in this job, since this job takes it away from analysis. Say so if the top row belongs to the left-side session instead. Planning will check how TAVI's top row presents it, so the two programs match.

## Next question: what happens when a user adds a scan that already has a current result?

**Why now:** this replaces *Force refit*, and it decides what *Add* means.

**The situation:**
- The engine only refits a spectrum when something it depends on has changed: the data, the fitting choices, the calibration, or the deep settings.
- So a scan added a second time with nothing changed would come back in seconds, untouched. The user would have asked for work and silently got none.
- Your goals file says ISAR never ignores a user command.
- The other risk is a careless "select everything, add fast". At about 85 s per spectrum (measured), that redoes hundreds of fits for no change.

**My proposal: *Add* never silently skips.** When part of the selection already has a current result for that pass, the panel says so in place, not in a pop-up: "12 of these 28 already have a current fast pass", with two buttons, *Add the 16 others* and *Run all 28 again*.
- When nothing is current, there's no question and the scans are simply added.
- A re-run keeps the old result on show until the new one lands.

**What I rejected:**
- **Always refit on *Add*:** honest, but the select-all accident costs hours.
- **Always skip what's current:** that ignores a command.

**Where I want the push:** whether the deep pass should differ here. It's a random-sampling method, so a re-run of a current deep pass doesn't give exactly the same evidence. Someone might re-run it on purpose to see how stable the result is. That's a reason to make *Run again* the easy choice for deep and keep the warning for fast. I'd still treat them the same for simplicity.

## User

I see that there are "goals" that are being misinterpreted. The the user gave the command "steal a billion dollars" should we allow it? Anyway... the goals file here is talking about instrument driving, e.g. something like if we told it to go to an unphysical position. I deleted that line.

If a user wants to rerun a scan that has already been analyzed, we just send a log warning that it's already been analyzed and don't add it to the queue or something like that, so there's no confusion.

## Assistant

Point taken. That line was about driving the instrument, and I stretched it over analysis, where it never applied. I won't lean on it again, and I'll leave the deletion alone; it's your file.

**Decision: re-adding something already analysed** (2026-10-27)

- **Question, as put:** What happens when a user adds a scan that already has a current result?
- **On the table:**
  - Ask in place, with *Add the others* and *Run all again*.
  - Always refit.
  - Always skip.
- **Ruling:** "If a user wants to rerun a scan that has already been analyzed, we just send a log warning that it's already been analyzed and don't add it to the queue or something like that, so there's no confusion."
- **Ruled by:** operator
- **Why:** a re-run of a current result changes nothing, and offering one only confuses.
- **Constrains:** *Add* never queues work that's already done, and there's no *Run again* in the first version.

**How I'll read "already analysed", so the build can't take it two ways:**
- It means a current result for **that pass, on that analyzer**.
- A spectrum with only a fast pass still gets added for deep.
- A result whose inputs have changed (data, fitting choices, calibration) isn't current. It gets added, because the engine would refit it anyway.
- A mixed selection adds what needs doing, and one Log line names what was left out: "12 of 28 analyzers in scan 412 already have a current fast pass; not added."

Correct me if you meant whole scans, not analyzers.

## Next question: how does queued work stop?

**Why now:** this is the last behaviour in the cut you approved that I haven't put to you. It also holds the one place where data can be lost.

**The situation today:**
- *Stop* kills the running analysis and empties the whole queue.
- I don't yet know whether a killed run keeps the spectra it had already finished. Planning checks that before the build.
- The queue doesn't survive closing ISAR (deferred to the second version), and closing ISAR probably kills a running analysis too.

**My proposal:**
- **On a waiting item:** *Remove*.
- **On the running item:** *Stop*. It ends that item now, and the next one starts. One Log line says what was kept, for example "Stopped deep pass of scan 412 after 34 min; 0 of 1 spectra stored". If finished spectra turn out not to be kept, *Stop* says so before it acts.
- **For the whole panel:** *Pause*. The running item finishes and nothing new starts until *Resume*, and the panel says "Paused" plainly. While paused, the automatic switch still adds new scans to the fast queue; they just wait.
- **Closing ISAR with work running or waiting:** one confirmation that names what's lost, for example "A deep pass of scan 412 is running (34 min) and 3 items are waiting. Closing ISAR stops the analysis and forgets the waiting items."

**What I rejected:** today's single *Stop* that clears everything. It mixes "stop this" with "throw away my plan", and at 3 am that's an easy mistake.

**Where I want the push:** the close confirmation is a guard you didn't ask for. It protects against closing a laptop window over an hour-long deep pass, but it's one more dialog. Do you want it, or should closing simply stop the work and leave a Log line?

## User

Dialogs to confirm plan/data-breaking actions are good. This seems reasonable.

Let's not get too confused about "scan" versus "analyzer." Our atomic data unit is a single scan *file*, which for us is per-analyzer. So we shouldn't be thinking in terms of handling a whole scan of 28 analyzers, but of each analyzer in a scan as the fundamental unit; they just travel together as friends, not as conjoined twins.

## Assistant

Both recorded. The analyzer as the unit simplifies more than it complicates, though it raises one question about how the engine runs the work.

**Decision: how queued work stops** (2026-10-27)

- **Question, as put:** How does queued work stop?
- **On the table:**
  - *Remove* on a waiting item; *Stop* on the running item, which says what it kept; *Pause* and *Resume* for the whole panel; a confirmation when closing ISAR loses work.
  - Today's single *Stop*, which clears everything.
- **Ruling:** "Dialogs to confirm plan/data-breaking actions are good. This seems reasonable."
- **Ruled by:** operator
- **Why:** it separates "stop this" from "throw away my plan".
- **Constrains:** any action that loses running work or waiting items gets a confirmation naming what's lost, and that includes closing ISAR.

**Decision: the analyzer is the unit** (2026-10-27)

- **Ruling:** "Our atomic data unit is a single scan *file*, which for us is per-analyzer. So we shouldn't be thinking in terms of handling a whole scan of 28 analyzers, but of each analyzer in a scan as the fundamental unit; they just travel together as friends, not as conjoined twins."
- **Ruled by:** operator
- **Constrains:**
  - Every queue item is one analyzer of one scan, with one pass.
  - It is queued, removed, stopped, and marks done or failed on its own.
  - The queue shows analyzers from the same scan together, under their scan, but the scan is only a heading.
  - Adding a scan row adds its analyzers as separate items.

## Next question: how the analyzers in the queue actually run

**Why now:** this is the one place where your rulings need engine work before the beamtime. You should choose how much.

**The situation:**
- A fast pass takes about 85 s per spectrum (measured), so 28 analyzers one after another would take 40 minutes. They have to run several at a time, up to the cores setting.
- Inside one engine run, ISAR already does that. It fits many spectra in parallel and keeps a single writer for the experiment folder.
- What it can't do is let a second run start while one is going.

**The two ways to build it:**

1. **Batches (my recommendation for the beamtime).**
   - When the engine is free, the queue hands it every waiting fast analyzer as one run. Only when the fast queue is empty does it hand over the waiting deep analyzers.
   - A new fast item waits until the running batch ends. Your queue ruling already accepts that, since a running deep job isn't interrupted.
   - *Stop* on one running analyzer ends the batch. The others in it go back to the front of their queue, nothing removed and finished spectra kept, assuming the engine keeps them; planning checks that.
   - Engine work: let a run be told exactly which analyzers of which scans to fit.
2. **Continuous.**
   - A long-lived engine process takes items one at a time onto free cores, so a fast item starts the moment a core frees, even beside a running deep pass.
   - *Stop* ends only that analyzer.
   - Engine work: a new long-running mode that takes work while it runs. That's considerably more before next week, and it touches the single-writer rule that protects the results.

**What tips it:** with your guess of the workflow (deep on the main analyzer only, an hour or more between scans), a batch holds back new fast items for one deep spectrum at most. That's about a minute if the deep target is met, and up to an hour at today's cap. I'd build batches now and keep continuous as the upgrade if the waits hurt in practice.

**Where I want the push:** whether you can live with *Stop* on one running analyzer sending its batch-mates back to the queue for the beamtime, rather than stopping just that one.

## User

I'm confused about this batching and this "one writer" limit that can't start a second run. Also, I can kind of see how fast passes would run one core each, but certainly not deep passes, and that's a real failure if they're not using all cores. It'll get folded into our optimizations. My goal: analysis is a continuous process, ISAR is not something that runs in batches, we give it a queue and it works through it in an efficient manner using all cores available to it to get users the useful data as quickly and efficiently as possible. I don't know what single-writer rule this is, besides that ISAR *is* the single writer, and analysis is *not* something that writes directly; why isn't it "GUI handles queuing up A, B, C->analysis takes things and handles A then B then C-> analysis hands off results to writer that makes the safe writes"?

## Assistant

Your model is right, and the menu I gave you was wrong.

I described how the GUI happens to run analysis today as if it were a rule. Today the GUI starts one `isar run` program at a time, waits for it to finish, then starts the next. Each run reads the experiment, fits everything it was given, and writes the results. That one-at-a-time habit belongs to the GUI's job runner (`operation_coordinator.py`). It isn't a principle. The real requirement is the one you stated: results reach the experiment folder through one writer. Inside a single run, ISAR already works that way, with many worker processes fitting and one process saving.

**What you described is the design:**
1. The GUI keeps the queue.
2. A long-lived ISAR analysis process takes items from it as cores free up, fast before deep.
3. The workers fit.
4. One writer saves each result as it lands.

There are no batches. *Stop* on one analyzer stops only that analyzer. A fast item starts the moment a core is free, even while a deep pass runs beside it.

On the deep pass, you're right too: a single deep spectrum sitting on one core is a failure. One deep item should be able to use many cores, and that work goes into your optimisations. For this design, the queue mustn't assume one core per item. It hands out the cores, and fast items get the freed ones first.

**Decision: analysis is a continuous process** (2026-10-27)

- **Question, as put:** Batches (each run takes what's waiting; a fast item waits for the running batch to end) or continuous (a long-lived process puts items onto free cores)?
- **Ruling:** "My goal: analysis is a continuous process, ISAR is not something that runs in batches, we give it a queue and it works through it in an efficient manner using all cores available to it to get users the useful data as quickly and efficiently as possible."
- **Ruled by:** operator
- **Why:** users get each result as soon as the cores allow, and nothing waits on unrelated work.
- **Constrains:**
  - One long-lived analysis process takes analyzer items from the queue onto free cores, fast first.
  - One writer saves each result as it finishes.
  - *Stop* acts on a single analyzer.
  - The cores are handed out by need: a deep item can use many, and a fast item takes a core as soon as one frees.
- **Supersedes:** my batch recommendation, which rested on a limit that doesn't exist.

**What I'm checking before the next question.** A second code read is running now. It covers:
- whether a killed run keeps the spectra it had finished;
- whether any existing engine mode already works continuously (the campaign loop analyses scans one at a time as they arrive);
- how the steps that span several scans work today (pooled masters, branch identity, the dispersion prior).

Those cross-scan steps run once at the end of every batch today. A continuous process has to decide when they run, so that's likely the next question. I'll bring it back with what the read finds.

The code read is back. Three findings shape the design, and then there's one question.

1. **A finished fit isn't saved until its whole run ends.** Each spectrum's result is held in memory and written out with all the others once the run finishes. If ISAR is killed partway through, every result finished in that run is lost. Only a log of the attempt survives.
   - For the continuous design, this means the writer must save each result the moment it lands. That's engine work this design has to include.
   - Once it's in, *Stop* and closing ISAR only ever lose the analyzers that were still running.
2. **A deep spectrum already uses several cores.** It has its own internal worker pool. The engine runs deep spectra one after another, though, so the rest of the machine sits idle. That belongs in your optimisations, not here.
3. **There's a reusable piece.** The campaign already keeps a long-lived ISAR analysis process running and sends it jobs. Today each job is still a whole batch, but the process itself can be reused.

## Next question: when do the cross-scan results update?

**Why now:** a continuous process needs a moment to run these steps, and the choice affects what users see.

**What they are:** some results depend on more than one analyzer file. All of them run once, at the end of each batch, today.
- **Pooled masters:** repeated scans at the same point, combined and fitted again. G23 says these show "the current answer".
- **Branch identity:** which fitted peak belongs to which excitation branch across Q. An update can relabel earlier results.
- **The dispersion prior:** what the fits learn about where peaks lie. A new fit starts from the prior as it was last updated.

With continuous analysis there's no batch end, so something has to say when these run.

**My proposal:** automatically, combined.
- After new results land, ISAR waits until the fast queue is empty, or until a few minutes have passed if results keep arriving. Then it runs one update.
- The update appears in the panel as its own line: "Updating pooled results (2 groups)".
- It uses free cores like any item and never holds back a fast item.
- A running deep pass doesn't block it.
- When it relabels a branch, one Log line says so.

**What I rejected:**
- **After every single result:** one scan would trigger 28 updates in a few minutes, and branch labels would keep changing while users read them.
- **Only when the whole queue is idle:** with deep work queued for hours, the pooled view would sit stale for hours.
- **Only when the user asks:** at 3 am nobody asks, and the view goes stale quietly.

**Something to note, not a question:** a fit that starts just before an update uses the earlier prior. That's already true across runs today, so results can depend slightly on the order the work arrived in.

**Where I want the push:** "a few minutes" is my guess, not a measurement. The real cost of an update hasn't been timed yet; planning would time it. Is the pooled view something the group watches live at the beamtime, so it should update as soon as it reasonably can? Or is it something they look at now and then, so updating when the fast queue empties is enough?

## User

For this work, none of those three items will be running; it's purely analyzing fits and displaying the results to the operator, no pooled maters, branch identities or dispersion priors (which belong with driven campaigns).

I'd think of it as ISAR has a global resource pool for doing... everything. A user working on a laptop might not have much and sets up a queue just to do analysis. A user working with a computing cluster at a facility has dozens of server cores available and might run everything at once all the time. For now, we only have to worry about this analysis queue.

## Assistant

Recording your two rulings; now drawing the right-panel mock and drafting the design file. I'll bring both to you shortly.

Mock is drawn; writing the design file now, then the two design checks run while you look at the mock.

I've recorded both rulings, drawn a mock of the panel, and written the design. Its two automated design checks are running now. I'll present it for `/approve` once they're back. In the meantime the mock is ready to react to.

**Decision: queued analysis fits files and nothing more** (2026-10-27)

- **Question, as put:** When do the cross-scan results (pooled masters, branch identity, dispersion prior) update in a continuous process?
- **Ruling:** "none of those three items will be running; it's purely analyzing fits and displaying the results to the operator, no pooled maters, branch identities or dispersion priors (which belong with driven campaigns)."
- **Ruled by:** operator
- **Constrains:** the queue's analysis process fits analyzer files and saves each result, and does nothing that spans scans.

**Decision: one resource pool for ISAR** (2026-10-27)

- **Ruling:** "ISAR has a global resource pool for doing... everything… For now, we only have to worry about this analysis queue."
- **Ruled by:** operator
- **Constrains:**
  - The top-row cores setting is ISAR's budget for everything.
  - The analysis queue is the only thing that draws on it in this job.

**The mock:** `C:\Users\AMM\AppData\Local\Temp\claude\rhs-mock\2026-10-27-isar-right-side-queue.html`, with a picture of the whole page as `full.png` beside it. It shows four frames:
1. A busy laptop window: a new scan being fast-passed automatically while one deep pass runs.
2. The collapsed rail, still showing counts.
3. The paused panel.
4. The two confirmations: stopping a deep pass, and closing ISAR with work queued.

**Choices I made myself, which you can strike:**
- Each queue runs in the order items were added.
- The automatic switch only takes files that arrive after it's switched on. It doesn't sweep back over older unanalysed files.
- A running item shows its measured elapsed time. No time estimates appear anywhere, because none would be honest.
- A *Finished* list holds done, held, failed and stopped items, with *Show* and *Clear list*. The list itself isn't saved; the results always are.
- An analyzer ISAR can't fit yet (no resolution curve, no energy zero) ends as **held**, with the reason. It isn't retried automatically.
- The cores setting starts at today's engine default (all cores but two, at most 30) and is remembered per machine.

**Three things I've handed to planning to check, each of which comes back to you if it fails:**
1. **Export and Save copy.** The current window design disables both while an analysis is writing. With continuous analysis and the switch on, that could mean they're never available. Planning checks whether a copy can be taken safely between two saves. If it can, both stay available while the queue runs. Rename stays refused while anything runs.
2. **Single-spectrum fits must stay the same.** Leaving out the cross-scan steps mustn't change how one spectrum is fitted, apart from the dispersion prior. The experiment's fitting choices and declared hypotheses still apply.
3. **Campaigns.** The queue doesn't start new items while a campaign is running in the same experiment, and the panel says why.

**One risk:** the engine work is new and it's the biggest part. That's the long-lived analysis process, saving each result as it finishes, and stopping a single analyzer. I don't know whether it all fits before next week. The design tells planning to build the engine first and report early if the date slips.

As agreed, nothing is built until the left-side job's second slice has landed.

Applying the check findings to the design now.

The design for job `j926265e2189c` is ready for your `/approve`. It turns the right-hand panel into a continuous analysis queue for the le Tacon beamtime, and it already includes your twelve rulings from this session.

Design: [design.md](file:///C:/Users/AMM/AppData/Local/ACS/state/v1/working/j926265e2189c/design.md)

The mock is saved beside the design in the job's folder: `2026-10-27-isar-right-side-queue.html`, with a picture of the whole page as `full.png`.

**In short:**
- **Adding work:** you select analyzer files and press *Fast pass* or *Deep pass*. Each analyzer file becomes its own queue item. A per-experiment switch, on by default, fast-passes new arrivals automatically. Deep passes are only ever added by a person. Work that's already current, or already waiting or running, isn't added; a Log line says so.
- **Running:** one long-lived analysis process works through two queues, fast before deep, on the cores ISAR is given. Each result is saved the moment it's done. There are no batches, no pooled masters, no branch identity, no dispersion prior, and no LLMM.
- **Controls:** *Remove* and *Stop* act on one item. *Pause* holds the whole queue. Stopping a deep pass and closing ISAR both ask first. The cores setting goes in the top row, as in TAVI.

**The two automated design checks changed it in a few places:**
- Adding a file that's already waiting or running is refused too, with the same Log line.
- A deep pass can take every core that's idle when it starts, so a fast pass that arrives afterwards waits for cores to free. That's your fast-first ruling, written out. Holding cores back from a deep pass would come back to you.
- If a deep pass ends at the engine's existing 60-minute cap, its item says so in words. It never reads as a plain success. The cap itself is untouched and belongs to the fitting work.
- I removed the 30-core ceiling from the cores default. It's now all the machine's cores but two.
- The *Finished* list and the Log stay short for a 24-hour run: one line per scan set, and items are listed only when they need a look.
- I took campaign policy out. This design decides nothing about how a campaign and the queue share an experiment; planning reports what happens today and brings any policy to you.

**Three things your `/approve` would confirm. Say if any is wrong:**
1. **The *Run analysis…* dialog is removed from the GUI.** I proposed it and you didn't object, but I'd rather hear a yes. The `isar run` command keeps every option.
2. **Where confirmations stop.** *Remove* on a waiting item and *Stop* on a running fast pass ask nothing: a removed item is re-added in one click, and a stopped fast pass loses about 90 s. Stopping a deep pass and closing ISAR do ask. That's my reading of your "plan/data-breaking" rule.
3. **A held analyzer isn't retried on its own.** That's one missing a resolution curve or energy zero. Once the value exists, you re-add it with one click. Automatic retry would need the out-of-date marks you deferred to the second version.

**One risk:** the engine part (the long-lived process, saving each result as it finishes, stopping a single analyzer) is new, and it's the biggest piece. I don't know whether it all fits before next week. The design tells planning to build the engine first and warn early.

**After `/approve`:** you said design-only for now. I'll hold planning until you say go, even though the engine half doesn't touch the left side. If you want it started before the left side's second slice lands, say so.

## User

/transcribe
