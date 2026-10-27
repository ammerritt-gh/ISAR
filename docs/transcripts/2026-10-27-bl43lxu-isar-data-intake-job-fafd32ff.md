# BL43LXU ISAR data-intake job

**Date:** 2026-10-27  
**Session:** fafd32ff-8934-4d47-811d-0b1c4abfc6a7

*Cleaned transcript: tool calls, tool results, thinking and harness notices removed. Nothing else edited.*

## User

We are taking over job jc54d8ce88c0c. Now, this was the data-intake portion of ISAR. We have a beamtime coming up to handle this. Please check the other session that used to hold it (don't message it), and the other session that will be working on the GUI backend (isar-d9). Our goal here is to take that job and get the BL43LXU portion working, have the data ingestion all ready, and the other session will handle the GUI portion. We do not need to support the other beamlines at this time, BL43LXU only. So we redesign with the better scope, replan, and basically replace that job.

## Assistant

Reading the old job's record and the two peer sessions' board entries before I propose anything.

Still reading — now pulling the two peer sessions' transcripts (old holder isar-0b, GUI session isar-d9) to see where each left off.

Reading where the old holder stopped; then the current BL43 code on main.

Launching a read-only recon of today's BL43 intake code on main; while it runs I'll check the original design transcript for the BL43 rulings.

I haven't read the code yet. Here's where both sessions stand.

**The old intake job (`jc54d8ce88c0c`)** has been parked since 23 October. Nobody holds it now.
- **Built:** only its first step: catalogue entries and BL43 file recognition. That's two commits on a branch, nothing on main.
- **Stalled:** that step was marked failed because it touched files outside its planned file list. That check is now only advisory, so the original cause is gone.
- **Plan:** written before the newer planning standard.
- **Code:** main has since changed the same files (BL43 parser, scan record, ingest), so the branch is better used as reference than rebased.
- **Old design scope:** every facility, plus automatic re-reads on catalogue corrections.
- **Your rulings in it** still stand. They include: your values sit beside the file's values and yours win; experimental values like temperature and slit are never card edits; and the GUI is your only route.

**The GUI session (isar-d9)** is still in design, and it's waiting on you. Its last question was whether the intake job gets rebuilt as backend only, with its two GUI pieces moving into the GUI job. Those pieces are the needs-attention list with tick-the-scans value entry, and the setup section with resolution-folder Locate plus the fact table. What you've just told me is a yes to that. You'll want to give it that answer there; I haven't messaged it or the old session. Its other open question matters to us both: will next week's files carry temperature (`#T=`) and Q (`#Q=`) in each file, like the CsV₃Sb₅ samples do, or will Q come only from the separate `gpi.*` files?

**My plan:**
1. Open a new job for BL43LXU intake (backend only).
2. Cancel the old job once the new one is open, keeping its branch as reference.
3. Bring you the redesign one question at a time.

I'd recommend tier T1, since T2 and T3 no longer take new jobs; the choice is yours.

**Now:** a read-only survey of today's BL43 path on main is running: reader, card, ingest, resolution lookup, hold reasons, and what the fitting reads. When it's back I'll bring you the first real design question. I expect it to be whether the catalogue machinery is still worth building with only one instrument in scope, or whether BL43 should go on the existing reader with your values and settings layered on top.

The survey is back. BL43 already works end to end on main, so the new job is smaller than the old one. The work is in the gaps around the reader.

## What works today, with no new code
- **Reading:** both existing BL43 layouts are read, CsV₃Sb₅ (`_a03`…`_a41` plus `.var`) and Ba122. Analyzer files group into one scan.
- **Facts with sources:** temperature comes from `#T=`, Q per analyzer from `#Q=`, and filename and `.var` facts follow a fixed precedence. A missing fact is recorded as missing, never invented.
- **Calibration:** each analyzer's curve is picked by slit. The slit can be inferred from the elastic lines when none is given. If the elastic line is too weak, the file axis is used as a labelled fallback. An analyzer with no curve is held, and the reason is shown in `isar status`.

## What's missing for next week
1. **Nowhere to enter a value except by hand-editing the card.** That covers temperature, slit, a dead analyzer and an excluded scan. You ruled the GUI must be the route.
2. **The resolution-curve folder.** It is set only by the card line `rf`, resolved relative to the card's folder. The le Tacon `rf/` sits beside the data, so a new user gets every analyzer held.
3. **Gaps surface late.** A missing analyzer file or a missing sidecar is a parse flag that never reaches the status view. There is no needs-attention list for d9's GUI to show.
4. **The `gpi.*` files are never read**, which matters if Q will only be in them. And next week's format, "scan number + analyzer per file", matches neither existing layout.

The backend shape I'd propose:
- a per-scan store for your values, with the file's value kept beside yours and yours winning, as you ruled;
- an ISAR-recorded resolution-folder setting;
- a gap list that d9's GUI reads;
- the reader for whatever the beamline's preprocessing script writes.

## The first decision: how much catalogue to build (significant, and it shapes later work)

The old design split format knowledge out of the card into a shipped **catalogue entry** per instrument and format revision. Your reason was that fixes reach every workspace and copies don't drift. With BL43 the only instrument in scope, that machinery serves one format. The old branch built about a third of it, 1,466 lines, on code that has since moved.

- **(a) The full catalogue, BL43 only.** This means entry files, recognition of each file against the entries, a record of which entry version read each scan, and an automatic re-read when an entry is corrected. It is the end state for one instrument. It's the largest option, and the other facilities stay on the card indefinitely, so there are two intake paths.
- **(b) A thin split.** BL43's format knowledge (file patterns, columns, layouts) is read from the file shipped with ISAR, never from the copy in the workspace. The workspace card keeps only analysis settings. There's no multi-format recognition and no re-read machinery. A format fix then reaches every BL43 workspace, which is the part of your ruling that matters for this beamtime. It's a few dozen lines, and a later job can grow it into (a).
- **(c) No catalogue work now.** Format knowledge stays in the card copied into the workspace. The job builds only the four gaps above. If the format surprises us mid-beamtime, I edit that one workspace's card.

**I recommend (b).** It keeps your colleague away from formats, and a mid-beamtime format surprise becomes one fix in one place. Most of the job's time then goes to what the beamtime actually needs. Everything it builds is reused by (a) later. I rejected (a) as too much machinery for one instrument before next week. I rejected (c) because it moves nowhere toward the catalogue you asked for.

## The fact I most need
isar-d9 asked you this too, and one answer serves both: **will next week's files carry temperature and Q in each file** (`#T=` and `#Q=` trailers, like the CsV₃Sb₅ samples)? Or will they be bare, with only scan number and analyzer, and Q only in the `gpi.*` files? If Q comes from `gpi.*`, the reader has to tie each `gpi` file to its scans. You called that link fraught, so it's the riskiest piece of this job, and I'd want a sample file as early as you can get one.

**Also yours: the tier.** I'd open the new job as T1, since T2 and T3 no longer take new jobs. I'll cancel `jc54d8ce88c0c` once the new job is open, and keep its branch as reference.

## User

(b) then.

I don't know for *certain* what will be carried next week. I assume it will look the same as the data we have now; I think we can get *more* later, not less, as the safe assumption, but right now we build against what we have.
/t1

## Assistant

Another session's scratch file; I'll use my own name.

Opening the job's cold-reading panel on the rescoped request while I set up the first question.

The new job is open as **`jfada74237892`** (T1, design stage, bound here). The old **`jc54d8ce88c0c`** is cancelled. Its worktree was removed, but its branch `acs/jc54d8ce88c0c` is kept as reference: two unmerged commits, catalogue entries and BL43 recognition.

**Decision: a thin catalogue split for BL43** (2026-10-27)
- **Question, as put:** How much catalogue to build with BL43 the only instrument: (a) the full catalogue, (b) a thin split, or (c) no catalogue work now?
- **On the table:** (a) entry files, per-file recognition, a version record, automatic re-read: the end state, but large, and a second intake path indefinitely; (b) BL43 format knowledge read from the file shipped with ISAR, the workspace card keeps analysis settings only, a later job can grow it into (a); (c) the format stays in the workspace card copy, fixed by hand per workspace.
- **Ruling:** "(b) then."
- **Ruled by:** operator
- **Why:** a mid-beamtime format fix lands in one place, and your colleague never meets formats. Most of the effort goes to what the beamtime needs.
- **Constrains:** no recognition across several formats, no automatic re-read on a correction, no other facilities in this job.

**Decision: build against today's data** (2026-10-27)
- **Question, as put:** Will next week's files carry temperature and Q in each file, or be bare with Q only in the `gpi.*` files?
- **On the table:** in-file `#T=`/`#Q=` (the reader already handles them); bare files with Q from `gpi.*` (a new reader piece, with the scan link you called fraught).
- **Ruling:** "right now we build against what we have."
- **Ruled by:** operator. Your expectation that later files carry "more later, not less" stays an expectation, not a requirement.
- **Why:** nobody knows the new format for certain yet.
- **Constrains:** the reader targets the CsV₃Sb₅ layout (`#T=`, `#Q=`, `.var`) and keeps Ba122 working. Reading `gpi.*` files and entering Q by hand are out unless a sample says otherwise.

**isar-d9** messaged me with the split you agreed there: it builds the GUI, and I build what's underneath. I replied:
- the job was replaced and narrowed;
- my plan will hand it exact contracts for each read and write its screens need;
- my work stays off the four GUI files it is restructuring;
- your experiment metadata (title, proposal, sample) goes in the same experiment-settings record as the resolution folder.

That last point is our shared default, open until you approve the design.

**Size: a small region.** Your earlier rulings carry over: your values beside the file's, yours winning; readiness per use; the GUI as the only route. The cold-reading panel confirmed the shape and found three gaps, which I'll write in as defaults unless you object:
- **The curve folder is found automatically.** When the curve folder is not set, ISAR looks for `rf/` beside the data, and asks only if that fails.
- **Curve coverage is checked at import, not at fit time.** Each analyzer's curves are checked against its slit, so a missing curve shows up immediately.
- **An unrecognised header or trailer line is flagged**, so a renamed `#T=` reads as "unrecognised line", not silently as "temperature missing".

## Question 1: what happens to finished fits when your colleague enters a value

**Why now:** it decides whether this job only stores values or also tracks which results they invalidate. d9's queue design depends on the answer.

**The situation.** Batches arrive, your colleague imports them and runs the fast pass. Later they notice the slit on scans 12–30 was wrong, or a temperature is missing, and they enter it. The fast pass on those scans used the old value: the slit picks the resolution curve, and the temperature sets the anti-Stokes weight. Today ISAR has no record that a fit's inputs changed. Fast passes take minutes per scan and deep passes far longer, so redoing work has a real cost.

**My recommendation:** the scan is marked **"values changed since last fit"**, visibly, in its status and in the needs-attention list. Its earlier fits stay on record. Nothing reruns until your colleague puts it into a fast or deep batch from d9's queue, and that run uses the new values.
- **Trade-off:** one extra click per correction. In exchange, nothing expensive starts behind their back.

**Rejected:**
- **Automatic refit on the next run:** a slit fix on 200 scans would silently start hours of deep-pass work.
- **No marking at all:** the old fit would look current while built on a value the user has since corrected. That contradicts "never surprise the operator", your ruling on the old job.

**Where I want the push:** should the **fast** pass alone redo itself automatically on the next run, since it's cheap, while only the deep pass waits to be queued? I lean no, for one consistent rule, but that's the part I'm least sure of.

## User

Yes, your recommendation is good. Let's not do anything automatically for now, we can build that in as an option later.

## Assistant

**Decision: a changed value marks fits stale, nothing reruns on its own** (2026-10-27)
- **Question, as put:** When your colleague enters a value after fits ran: mark the scan "values changed since last fit" and leave rerunning to them, refit automatically on the next run, or not mark at all? And should the fast pass alone redo itself automatically?
- **On the table:**
  - mark and leave it to the user: one click per correction, nothing expensive starts unasked;
  - automatic refit: a slit fix on many scans silently starts hours of deep-pass work;
  - no marking: an old fit looks current.
- **Ruling:** "Yes, your recommendation is good. Let's not do anything automatically for now, we can build that in as an option later."
- **Ruled by:** operator
- **Why:** nothing expensive starts behind the user's back, and the stale result is never hidden.
- **Constrains:**
  - a scan whose values changed since its last fast or deep pass shows that state;
  - earlier fits stay on record;
  - neither pass reruns until the user queues the scan;
  - an "auto-redo" option is later work, outside this job.

## Question 2: how the GUI writes your colleague's values

**Why now:** it fixes the one contract d9's value-entry screen calls, and it touches a hard rule in `AGENTS.md`. That makes it yours.

**The situation.** Hard rule 7 says the GUI "reads science, it never writes science". Everything it shows comes through the read-only workspace interface (`workspace_api`). The one thing it may write is operational control: starting `isar run` as a subprocess, or dropping a "stop" file. The rule exists so that no number is ever interpreted outside the fitter. A temperature or slit typed by the user is neither a reading nor a control signal. It is a third thing: your colleague's statement, which the fit then uses. The rule has no slot for it.

**My recommendation: name a third channel in the rule.** A small Qt-free module, `isar/intake`, writes three things:
- **the user's values and conditions:** temperature, slit, dead analyzer, excluded scan, appended to a log in the workspace that is never rewritten;
- **the experiment settings:** resolution folder and metadata;
- **validation:** the module refuses an unknown scan or analyzer, or a value that doesn't parse in its unit.

The GUI calls it directly. Hard rule 7 gains one sentence:

> "Operator input is a third channel: the GUI records the user's values, conditions and experiment settings through `isar/intake`, which validates and appends them; it never computes or interprets a number."

`workspace_api` stays read-only, and the "interpret nothing outside the fitter" part of the rule is unchanged.

**Rejected: the GUI runs an `isar` command for each write**, with the ticked scans passed in a temporary file. That keeps the rule as written. But it adds a command, a file hand-off and a second path for reporting errors, and the protection it buys is the same, because neither route interprets a number.

**Where I want the push:** the wording. Is "operator input" the right name for that channel, and is the sentence above what you'd want the rule to say?

## User

We had discussed this in the other session, we keep a "file" copy of the values and a "user-given" copy; maybe you can check the transcript of the other session/job? I think this was settled nicely.

## Assistant

You're right that it's settled. The two copies stand exactly as you ruled on 22 October: every fact keeps the file's value and the user-given value side by side, the user's wins, and a disagreement is warned. I wasn't reopening that.

My question was narrower, and I put it badly: through which route the GUI *saves* the user-given copy. I checked the old transcript. That route was never put to you as a question: the old session showed you the wording only inside a long closeout report, and you never answered it. It only decides how to deliver what you already ruled, so I'm withdrawing it as a question:

- **Withdrawn:** "how the GUI writes values". This is mechanism, so it's my default. The GUI saves through a small module that checks each value and appends it to a log in the workspace that is never rewritten.
- **One line in `AGENTS.md` changes.** Hard rule 7 gains a sentence naming that route. The sentence will be in the design, so you'll see its exact wording when you approve, rather than as a separate question.

## Question 3: when it has to work, and what comes first if time runs short

**Why now:** this decides the build order and what I'd cut. It's also the premise I can least afford to guess.

**What I know:** the beamtime is "next week", the data arrive in batches after preprocessing, and your colleague runs ISAR from the GUI. What I don't know is the day the first batch reaches ISAR, or whether d9's GUI must also be in place by then. If both must be, d9 needs my backend earlier.

**My proposed order:**
1. **The resolution-curve folder.** It's found automatically as `rf/` beside the data, or set once in the GUI. Without it, every analyzer is held and nothing fits, so this comes first.
2. **Your colleague's values and conditions** (temperature, slit, dead analyzer, excluded scan), applied to fits, with the file's value kept beside each one.
3. **The list of what's missing.** Every gap appears at import, not at fit time: missing analyzer files and sidecars, curve coverage per analyzer and slit, unrecognised header lines, and scans whose values changed since their last fit.
4. **The thin catalogue split:** BL43's format knowledge read from the file shipped with ISAR.

The split comes last because the beamtime runs fine without it. It only changes where a format fix gets made.

**Where I want the push:** two things.
- **The date:** which day does the first batch reach ISAR?
- **The order:** is the list of what's missing (3) worth more to you than the values (2)? For example, if your colleague can live with you supplying a slit remotely for the first day, but needs to see at a glance what's wrong.

## User

I don't think that question Q2 is settled, give it back to me with more explanation.

## Assistant

Fair. That one was put too thinly. Here it is again, from the ground up. Question 3, the date and the build order, waits until this is settled.

## What is already settled, and what isn't

**Settled (your ruling, 22 October):** every fact keeps two copies, the value read from the file and the value the user gave. The user's copy is the one used, and a disagreement is warned. Nothing below changes that.

**Not settled:** what physically happens when your colleague ticks scans 12–30, types "60×60 mm" as the slit and clicks Save. Something has to write that user-given copy into the experiment's folder on disk. Right now no part of ISAR is allowed to, because of a rule in `AGENTS.md`.

## The rule in the way

`AGENTS.md` is the rules file every agent working on ISAR must follow. Hard rule 7 in it says: **"The GUI reads science, it never writes science."** In practice:
- **What the GUI shows.** Everything comes through `workspace_api`, a read-only layer that loads fits, scans and statuses from the experiment folder and is never allowed to change anything. Notebooks and scripts use the same layer, and they rely on it never changing the folder under them.
- **What the GUI may do besides reading.** It may start ISAR's command-line program as a separate process (`isar run` and so on), and it may drop a small "please stop" file to halt a running campaign. The rule calls this "operational control".
- **Why the rule exists.** No number gets computed or interpreted anywhere except in the fitting code. The real danger it names is a GUI that quietly works out a value itself.

A temperature your colleague types is neither something read nor a control signal. It's a statement from the user that the fit will then use. The rule never imagined that, so whichever route we pick, the rule either gains a sentence or gets stretched.

## The three routes I see

**(A) A small saving module the GUI calls directly. This is my recommendation.**
- **What happens on Save:** the GUI hands the ticked scans and the typed value to a new, small piece of ISAR, `isar/intake`. It has no window code. It checks that every scan and analyzer exists and that the value parses in its fixed unit (kelvin for temperature; millimetres, horizontal by vertical, for the slit). It then adds one line to a log file in the experiment folder. That log is only ever appended to, never edited or rewritten, so every value your colleague ever entered stays on record with its time. The GUI then shows "saved", or the exact reason it was refused, at once.
- **The cost:** hard rule 7 gains one sentence naming this as a third route besides reading and control. `workspace_api` stays strictly read-only. And the module only checks and records; it never calculates anything.
- **Commits us to:** one place in ISAR that writes user input, which d9's screens and any later tool (the bulk-correction tool you deferred) call.

**(B) The GUI runs an ISAR command for each Save.**
- **What happens on Save:** the GUI writes the ticked scans and the value to a temporary file. It then starts a new process, along the lines of `isar values set --from <that file>`, waits for it to finish, and reads back its success or error message. The same checking and the same log as in (A), done inside that command.
- **The gain:** hard rule 7 stays word for word. Saving becomes "operational control", like starting a run.
- **The cost:** about 0.7 s per Save before anything happens (I timed starting the program once; that's a single measurement). It also adds a temporary-file hand-off and a second way for errors to travel back to the screen. And it bends the meaning of "control" to cover what is really data entry.
- **What it protects:** nothing more than (A). In both routes the same code checks and records the value, and in neither does the GUI compute anything.

**(C) Make `workspace_api` able to write. Rejected.**
- The read layer becomes read-write. Notebooks, scripts and the GUI would all share one object that can now change the experiment folder. That throws away the one guarantee the read layer gives everyone else, to save adding a small module.

## What I'd write into the rule under (A)

> "Operator input is a third channel: the GUI records the user's values, conditions and experiment settings through `isar/intake`, which checks them and appends them to the experiment's records; it never computes or interprets a number. `workspace_api` stays read-only."

## Where I want the push

1. **(A) or (B)?** Do you see the rule as protecting *"the GUI never computes a number"*, in which case (A) honours it? Or as protecting *"the GUI never writes to the experiment folder except through ISAR's command-line program"*, in which case only (B) does?
2. **If (A), the sentence:** is "operator input" the right name, and does the sentence say what you want?

## User

This rule in AGENTS.md is fundamentally flawed I think, but I don't know where it came from. My thinking here is thatit was that the GUI was a process over ISAR, and I had said "we don't wrap up science and calculations *within the GUI process*" so that e.g. the GUI doesn't start writing things and desyncing stuff. And a good example where that can go wrong is we had issues in LLMM where I would write a value and it would get overwritten by a GUI refresh, we can't have that happen here. Writing a user value sends it to the underlying process. I don't think it was "the GUI can never interact with the science at all and remains a read-only process."

## Assistant

That changes the question. The rule I put to you was the wrong frame, so I've dropped the A/B/C list.

## Where the rule came from

You never ruled it. An agent wrote it on 12 July, in an "Instructions update" (`ed79aa7`), as a description of the GUI at the time: "`isar_gui` … is a **read-only viewer**". It was accurate then, because the GUI could only look. On 25 July (`96515a0`), after the GUI gained start/stop control for campaigns, a documentation cleanup made it a hard rule of its own. It patched in "operational control" as an exception rather than asking what the rule was for. So it's a description that hardened into a law, and it doesn't say what you meant.

## Your account of it

As you put it, the rule should mean:
- the GUI is a layer over ISAR, and no science or calculation happens in the GUI;
- the failure it guards against is a GUI that holds its own copy of state and writes it back, so a refresh wipes out a value you entered (what happened in LLMM);
- entering a user value is allowed: the GUI sends it down to ISAR underneath, and ISAR keeps it.

The guarantee against the LLMM failure doesn't come from where the code runs. It comes from three properties:
1. **ISAR's record is the only copy.** The GUI never stores values of its own.
2. **The GUI only ever sends changes, never a snapshot.** It sends "this slit on these scans", never "here is everything I'm showing, save it". A refresh only reads.
3. **Entries are only ever added.** Each saved value is a new line in a log that is never rewritten. The latest entry for a scan and fact wins, and a stale screen has nothing it could overwrite.

## Proposed rewrite of hard rule 7

> **7. The GUI is a thin layer over ISAR; it holds no science and no state of its own.** No calculation, fit or interpretation of a number happens in GUI code. The GUI shows what ISAR has recorded and sends the user's actions down to ISAR: a value, a condition, a setting, starting or stopping a run. ISAR checks each one and records it. ISAR's record is the only copy. The GUI sends changes, never a snapshot of what it displays, and a refresh only reads, so a refresh can never overwrite a value the user entered. `workspace_api`, the read layer the GUI and notebooks share, stays read-only.

The last sentence keeps a separate guarantee: a notebook reading the experiment can rely on that layer never changing anything.

## The one thing still open

When you say the value is "sent to the underlying process", which of these do you mean?

- **(i) ISAR's own code, called by the GUI.** On Save, the GUI hands the value to a small ISAR module (no window code). The module checks it and appends it to the log, and the GUI re-reads and shows "saved" at once. That module runs on the same machine process as the window, but it is ISAR's code, the same code a script or a later bulk-update tool would call. **My recommendation:** it satisfies all three properties above, and it's the "thin layer on the same engine" shape.
- **(ii) A separate ISAR program run for each Save,** as starting a campaign is today. The same three properties hold. It costs about 0.7 s per Save (one timing, not a benchmark), plus a temporary file to pass the ticked scans and a second path for errors back to the screen. It buys nothing (i) doesn't.

A third reading, a permanently running ISAR service the GUI talks to, would be a different architecture. ISAR has nothing like it today and the beamtime doesn't need it, so I'd rule it out unless you meant that.

**Where I want the push:**
1. Is the rewritten rule what you meant? If yes, I'll record it as your ruling and it goes into `AGENTS.md` in this job.
2. Is "underlying process" (i) or (ii)?

## User

I will leave the exact code implementation, I'm not well-versed in how to do this. I will say that a desync here is a very bad case; it can mean that the group reads a value on the GUI, ISAR runs a fit with a different value, and the science diverges; that must never happen. Let me ask: if I am running some fits or something and I manipulate a scan and e.g. change the temperature. I should know when it's saved and taken effect, it should be impossible for me to e.g. run a fit with desynced values; this means that we have to account for me changing the scan value whether the fit is in progress (and knowing that the fit was done with one value and we have a new one once it's done), or if it's in the queue, or separate from ongoing fits. We should mark values as unsaved or saved, so the operator knows "I entered this value but it hasn't entered the underlying state" versus "this value is now authoritative."

I think your rewriting of the rule is ok. But I'm questioning the rule as a hard limit on the GUI rather than a guide as to what we want and what problem we're trying to avoid; would that be better?

I for sure think (ii) is wrong, we don't want to relaunch every time a user hits save. ISAR should be an interactive process; I can have fits running and work on scans, and running a separate item every save will break.

## Assistant

**Decision: a save goes straight into the running ISAR, never through a new program** (2026-10-27)
- **Question, as put:** Is "sent to the underlying process" (i) ISAR's own code called by the GUI, or (ii) a separate ISAR program run for each Save?
- **On the table:**
  - (i): instant, the same code a script would call;
  - (ii): about 0.7 s per Save, a temporary file and a second error path, with nothing gained.
- **Ruling:** "I for sure think (ii) is wrong, we don't want to relaunch every time a user hits save. ISAR should be an interactive process; I can have fits running and work on scans, and running a separate item every save will break." You left the code itself to me.
- **Ruled by:** operator
- **Why:** ISAR is interactive: fits run while the user keeps working on scans.
- **Constrains:** a save is handled by ISAR's code inside the running application; how is my call.

**Decision: a displayed value and a fit's value may never silently diverge** (2026-10-27)
- **Ruling:** "a desync here is a very bad case; it can mean that the group reads a value on the GUI, ISAR runs a fit with a different value, and the science diverges; that must never happen."
- **Ruled by:** operator
- **Constrains:** the behaviour below, which is my reading of what you asked for. Correct whatever is wrong.

## How I read your requirement
1. **Saved, or not yet.** While a typed value is still only on screen, it is marked *unsaved*. It becomes *saved* only once ISAR has checked it and written it to disk. From that moment it is authoritative: every fit that starts afterwards uses it. Until then, nothing uses it.
2. **Every fit carries its inputs.** Each fit records the exact values it used (temperature, slit, and so on) and where each came from (the file, or which saved entry of yours). A fit uses one set of values from start to finish; it never mixes old and new.
3. **A fit already running** when you change a value finishes with the old one. While it runs, and once it's done, it shows "fitted with 97 K; you have since saved 100 K", and the result is marked stale, as you ruled for question 1.
4. **A fit in the queue** takes the values saved at the moment *that scan's* fit starts, not the values from when it was queued. Once it's done, the value on screen and the value the fit used are the same.
5. **Nothing else is affected.** A change on one scan touches no other scan and no other fit.

This job builds the backend half: the check-and-save, the recorded inputs for each fit, and the "values changed since this fit" comparison. d9 builds the *unsaved*/*saved* marks and the "fitted with … since saved …" display on top of it.

## Your question: a hard limit, or a guide to the problem?

**My view: keep a hard rule, but make it the thing that must never happen, not a fence around the GUI.** A ban like "the GUI never writes" breaks the first time the GUI legitimately needs to do something, as it did twice already, and then gets patched with exceptions. What you actually care about is an invariant: no science in the GUI, and no divergence between what the user sees and what a fit used. That deserves to be hard, by your own words "must never happen". *How* we keep it (ISAR keeps the only copy, the GUI sends changes rather than snapshots, a refresh only reads) belongs beneath it as current practice. A future design may change those means without breaking the rule.

> **7. What the user sees is what ISAR uses, and no science happens in the GUI.** The GUI computes, fits and interprets nothing. It shows what ISAR has recorded and sends the user's actions down to ISAR (a value, a condition, a setting, starting or stopping a run). A value shown as saved is the value every later fit uses, and every fit records the values it used. Today that holds because ISAR keeps the only copy, the GUI sends changes and never a snapshot, a refresh only reads, and `workspace_api` (the read layer the GUI and notebooks share) never writes. The failure this prevents: a group reads one value on screen while ISAR fits with another, as when a GUI refresh overwrote an entered value in LLMM.

**Where I want the push:**
1. Is the behaviour list right, especially point 4, where a queued fit takes the value saved when that scan's fit starts?
2. Do you adopt rule 7 as rewritten here?

The beamtime date and build order (question 3) comes next.

## User

1. Yes, I think that's right.
2. Ok, should be fine.

## Assistant

**Decision: no silent divergence, as specified** (2026-10-27)
- **Question, as put:** "Is the behaviour list right, especially point 4, where a queued fit takes the value saved when that scan's fit starts?"
- **On the table:** the five-point list:
  - an *unsaved* or *saved* mark, and a saved value is authoritative;
  - every fit records the values it used and where they came from;
  - a fit already running finishes with the old value and is marked stale;
  - a queued scan's fit takes the values saved when that scan's fit starts;
  - nothing else is affected.
- **Ruling:** "Yes, I think that's right."
- **Ruled by:** operator
- **Why:** a group must never read one value on screen while ISAR fits with another.
- **Constrains:**
  - this job: saving with checks, the recorded inputs on every fit, and the "values changed since this fit" comparison;
  - d9: the marks and messages on screen.

**Decision: hard rule 7 becomes an invariant** (2026-10-27)
- **Question, as put:** "Do you adopt rule 7 as rewritten here?"
- **On the table:** a hard limit on the GUI (today's "never writes science", already patched with exceptions twice); the invariant as rewritten ("What the user sees is what ISAR uses, and no science happens in the GUI"), with today's ways of keeping it given as current practice.
- **Ruling:** "Ok, should be fine."
- **Ruled by:** operator
- **Why:** the rule should name what must never happen and the failure it prevents. Today's way of keeping it can change in a later design without breaking the rule.
- **Constrains:** this job replaces hard rule 7 in `AGENTS.md` with the rewritten text, in the change that makes it true.

## Question 3: when it has to work, and what comes first if time runs short

**Why now:** it sets the build order and what gets cut. It's the last premise I can't look up.

**What I don't know:**
- the day the first batch reaches ISAR;
- whether d9's GUI must be in place that same day. d9 builds its value-entry screens on top of my backend, so if both must be ready, my backend has to land early enough to leave d9 time.

**My proposed order:**
1. **Resolution-curve folder:** found automatically as `rf/` beside the data, or set once in the GUI. Without it every analyzer is held and nothing fits.
2. **Your colleague's values and conditions** (temperature, slit, dead analyzer, excluded scan), saved with checks. Every fit records the values it used and is marked stale when one changes.
3. **The list of what's missing**, shown at import rather than at fit time:
   - missing analyzer files and sidecars;
   - curve coverage per analyzer and slit;
   - unrecognised header lines;
   - scans changed since their last fit.
4. **The thin catalogue split:** BL43's format knowledge read from the file shipped with ISAR. The beamtime runs without it; it only changes where a format fix is made.

**Where I want the push:**
- **The date** the first batch reaches ISAR.
- **Whether (3) outranks (2).** Your colleague could live a day with you supplying a slit remotely, but needs to see at a glance what's wrong; does that put the missing list first?

## User

Everything here is eminently possible to do today I think; this order is good, I am not stressed for time (but it behooves us to be efficient here) and everything should be feasible before the beamtime.

## Assistant

Design is written and registered; running the two required checks on it (cold read and fidelity against the transcripts) before I show it to you.

**Decision: build order and timing** (2026-10-27)
- **Question, as put:** When does the first batch reach ISAR, and does the missing-information list outrank your colleague's values?
- **On the table:**
  - the proposed order: (1) resolution folder, (2) your colleague's values and conditions, (3) the list of what's missing, (4) the thin format split;
  - the same order with (3) ahead of (2).
- **Ruling:** "this order is good, I am not stressed for time (but it behooves us to be efficient here) and everything should be feasible before the beamtime."
- **Ruled by:** operator
- **Why:** there's no deadline pressure, so the order follows what blocks the most.
- **Constrains:** the build order. Every piece is expected before the beamtime.

The discussion is done. I've written the design for job `jfada74237892`. Every ruling is in it, today's and the ones from 22 October that still hold. Two automatic checks are running on it now:
- **cold read:** does any wording allow two builds, one of which would disappoint you?
- **fidelity:** does it leave out, bend or invent any of your rulings?

When they're back I'll fix what they find and present the design for your `/approve`. Nothing is built before that.

Cold read came back with 9 wording ambiguities, all settled as clarifications; waiting on the fidelity check before editing the design once.

The design isn't in front of you yet. The second check is still running, and I'll wait for it before revising the design.

**Done so far:** the first check (cold read) found nine places where the design's wording allowed two different builds. I settled all nine as clarifications that follow from your rulings, so none needs a new decision from you.

The one worth knowing now: when a scan has no temperature, fitting goes ahead exactly as it does today, and the missing temperature just appears as an item your colleague can fix. Fitting today then gives the anti-Stokes side zero weight. This job doesn't change that.

**Still running:** the fidelity check, which compares the design against what you actually said.

**Next:** once it's back I'll fold both checks' fixes into the design and present it for your `/approve`.

Now folding the fixes into the design.

The design for job `jfada74237892` is ready for your `/approve`. Both checks have run, and their fixes are folded in.

Design: [design.md](file:///C:/Users/AMM/AppData/Local/ACS/state/v1/working/jfada74237892/design.md)

**What it builds**, in your order, with no deadline (you expect it all before the beamtime). Everything is BL43 only and backend only:
1. **The resolution-curve folder.** ISAR finds `rf/` beside the data and saves it as the experiment's setting. It never switches folders on its own after that. Each analyzer's curve is checked against its slit at import.
2. **Your colleague's values and conditions** (temperature, slit, dead analyzer, excluded scan):
   - Saved through ISAR's own code inside the running application, with checks. The file's value is always kept beside the user's, and the user's is the one used.
   - Every fast and deep pass records the exact values it used, at the moment it starts.
   - A scan whose inputs changed since its last fit says so ("fitted with 97 K; 100 K saved since"), while the fit runs and after it finishes. Nothing reruns on its own.
   - A mistaken condition is undone by saving the opposite declaration; nothing is erased.
3. **The needs-attention list**, built at import: missing analyzer files and sidecars, curve coverage, unrecognised fact lines, values changed since a fit.
4. **The thin split:** BL43's format knowledge is read from the file shipped with ISAR.

It also rewrites hard rules 1 and 7 in `AGENTS.md` as you ruled. And it hands isar-d9 exact, Qt-free reads and writers, with no window code (my plan lists the signatures).

**Behaviour you should know is unchanged:**
- A missing temperature or slit shows as something your colleague can fix, but it doesn't hold anything. With no slit, the slit is inferred from the elastic lines. With no temperature, fitting gives the anti-Stokes side zero weight, as it does today.
- When the elastic line is too weak, the file-axis zero fallback applies automatically and is labelled.

**Out of scope:** every GUI screen (d9's job), the other facilities, reading `gpi.*` files, entering Q by hand, clearing user values, and any change to fitting.

**Still open:** next week's exact file format. If it differs from today's data, send me a sample and I'll update the shipped BL43 format file.

The four review questions shown beside the design are this job's own checks, applied when its code is reviewed.

If you'd like this design discussion kept as a transcript, say `/transcribe`; I won't offer again.

## User

/transcribe
