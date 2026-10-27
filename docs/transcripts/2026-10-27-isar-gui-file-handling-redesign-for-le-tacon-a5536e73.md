# ISAR GUI file-handling redesign for le Tacon

**Date:** 2026-10-27  
**Session:** a5536e73-6a38-4f7e-8cb0-963b0370fd46

*Cleaned transcript: tool calls, tool results, thinking and harness notices removed. Nothing else edited.*

## User

We've got a lot of work to do to get the file-handling prepared for the le Tacon experiment next week. I've attached screenshots of a non-maximized initial screen, and a maximized screen after loading one file.

Let's take inspiration from the recent GUI work done on TAVI, especially around handling more compact laptop screens. ISAR does not have the multitude of docks, but we should ensure that the screens will work on a smaller laptop monitor while still taking advantage of my desktop-sized monitor when available.

/ui-professional 

We should remove the console window that stays blank anyway.

First issue: the initial screen is a little unclear, and goes into a different mode in the file system as soon as the first set of data is dropped; this is a bit confusing, because it feels like it's hiding things away. It does make it clear that we can drop data in the middle, but the left-hand-side could be more clear from the get-go. And it is somewhat campaign-shaped, which we don't need for this experiment.

Second, the right-hand-side, currently the campaign, should be used as the "working" area. So the LHS is data/scans, middle is display, RHS is processing/driving/working/analysis, bottom is logging and saving (log bottom left, saving bottom right).

Ok, I'm thinking in terms of a new user, coming upon this screen. They'll be dropping in data, so we need to make it clear that the LHS/top left is where they drop data, it's the "data ingestion point," so they have a place to drop it; they can drop it on the files ideally, but without being too restrictive in that "only here is a valid place to drop it." This starts ingestion.

So the first action is actually to get them to set up a workspace; they'll label the experiment, give details, we basically give them options for adding metadata that they can carry away from ISAR. This helps collect the data as one set too.

We should try to get resolution files for the experiment, this can be logged with it then and used for the scans later, it should be one set for the whole experiment.

Then they can start adding scans to it. Scans will come as a scan # then a bunch of analyzer, similar to what is shown in the sample data. We work off of the given parameters, and ideally I can get them to give us better filenames or actual headers, but at a minimum we get scan number and analyzer per file; if we can get temperature, great, and similarly we can use the gpi files to get the per-analyzer Q point I believe, but these are a bit fraught because they're not inherently tied to the data files themselves.

Ok, let's say they've loaded a scan in. Our current GUI is more set up for a driven campaign, so it includes things like omega/branch, which they won't need. Core items: scan # (top level organization group) with temperature, then per-scan we have q and analyzer number. We give a small indicator that each scan is parsed correctly.

Now we need to make this scan navigation area usable for the users. Critically, we have a very limited window to actually see scans, which is the most important element. So we have to really compact the folders (we generally watch only one folder, and that can go into a dialog box), we have a second log at the top for it which isn't necessary when we have one below, we have groups and a big area just for trying to sort scans... the primary concern should be making a good view where there is a list of the scans, we can click on a top-level scan to expand into the analyzers, and those analyzers are shown underneath with their statuses. Sorting by analyzer or temperature is useful and secondary.

The large "scan metadata" should be an optional box that can open next to the LHS to inspect a particular scan, a "scan inspection" tool, it takes up a lot of space that we don't need all the time.

The new addition will be how we handle the scans. We'll have the option(?) to turn on a column for processing to show that status; parsed is on by default so we know that's clear. Then we have the fast pass and the deep pass, eventually we'll have other items.

We need then to be able to make a batch of scans to process with fast or deep modes. That queue and that status of that queue will go in the RHS, and is not something we worry about now.

/t1 
/dialogue 
Can you get your design partner's views as well?

## Assistant

Job opened (`ja0cd202ecb67`); binding it and reading the current GUI before I bring you the first question.

Writing two read-only research briefs (current ISAR GUI, and TAVI's laptop-screen work) for the scout so I don't have to page 13k lines into this conversation.

Scratchpad it is.

Found an overlap worth knowing about: the paused le Tacon fitting job already has a designed-but-unbuilt GUI slice (analyzer overview + inspector). Reading that section now.

## User

A GUI-friendly analyzer overview and such is more of a middle screen item, which we'll leave; we focus on the LHS right now, just the raw scan list view/

## Assistant

Noted on the overview: the analyzer overview stays a centre item and out of this job. We're working on the left side and the raw scan list.

I've opened the job (`ja0cd202ecb67`), read the current GUI and TAVI's laptop-layout work, and run the cold-reading panel. Two other jobs already touch this ground, and one of them changes what we're designing:

- **Catalogue-intake job (`jc54d8ce88c0c`):** approved but not landed. One part is built on a branch and nothing is on main. Its design already puts three things in the Files panel:
  - a setup step that shows what ISAR detected and lets the user find the resolution-curve folder;
  - a needs-attention list ("Temperature: 214 scans");
  - entry of temperature and slit for selected scans.

  Your "workspace first, one resolution set for the experiment" sits on exactly that ground.
- **Le Tacon fitting job (`j313eac923389`):** its paused overview and inspector work is the centre item you just said to leave.

I've sent your design partner a blind reading of the request: the material only, none of my views. I'll bring you the first question once its answer is back, with the intake-job overlap briefed properly.

Your design partner has answered. It mostly agrees with the panel, and with me, which is convenient for once.

**Size: a small region with one large dependency.** The layout, the scan tree, the console and the optional inspector are well understood. The real uncertainty is how this job relates to the unlanded catalogue-intake job, because both reshape the same left panel. That question comes first, since it decides what this job builds at all.

**What stands today.** The catalogue-intake job (`jc54d8ce88c0c`) is approved and planned as three parts. Only the first piece of the first part is built, on a branch, and it is not yet accepted. Nothing is on main. Its first part ("BL43 complete in the GUI") still has five pieces to go, and it already covers:
- the setup step when you point ISAR at data: what was detected, plus "locate the resolution-curve folder";
- experiment settings recorded by ISAR, such as where the resolution curves live. Today that location is a hand-edited card line, `resolution_folder = "rf"`, which code must never write;
- the needs-attention list in the Files panel;
- entering temperature and slit for selected scans;
- the inspector showing each fact with its source.

Your "set up a workspace first, one resolution set for the experiment" is that same setup step. The only new part is the experiment metadata (title, proposal, people, sample), which nothing in ISAR stores today.

**The question:** who builds the setup step and the resolution-set choice, given the beamtime next week?

**My recommendation, matching the partner's.** One setup flow, owned in two halves:
- **This job owns the shape:** the window arrangement, the first screen, the workspace-creation step with optional experiment metadata, the compact scan tree, the watch-folder dialog, the inspector as an optional side panel, laptop/desktop layouts, and the console. It leaves marked places for the intake job's pieces.
- **The intake job owns the substance:** detection, the resolution-set setting, the needs-attention list and fact provenance. These slot into the places this job leaves.

This avoids two stores for the same setting and two places to fix the same missing temperature. The cost is a dependency: if the intake job's first part doesn't land before the beamtime, the le Tacon group gets a clean left side but still can't point ISAR at their resolution curves from the GUI. Those curves are per analyzer per slit, so the "set" has to be a folder covering every analyzer and slit, not one curve.

**The alternative I'd take if the intake job won't make it:** pull a minimal resolution-folder pick into this job, stored where the intake job can take it over later. The cost is building part of the intake job's design twice and amending that job.

**Where I want the push.** Is the intake job's first part still meant to be in hand for the beamtime, and is another session driving it? Only you know that. Your answer decides whether the resolution set is in this job or not.

The design is held here until you answer.

## User

Great. The other job was paused, I'm not sure if it's up-to-date, I thought it was a bit more of the backend portion; actually getting the GUI looking nice can be here then. But the old job may have been done under an older system, so it may need replanning anyway (the old system had skimpier plans and less focus on connections; when I tried to start that job is stalled about 30 minutes in after I had gone to bed).

## Assistant

Good. That settles the GUI side, and I'll record it:

**Decision: the GUI is built here, not in the intake job** (2026-10-27)
- **Question, as put:** Who builds the setup step and the resolution-set choice, given the beamtime next week, this job or the paused catalogue-intake job?
- **On the table:** split ownership (this job builds the shape, the intake job builds the substance and its GUI pieces); this job pulls in a minimal resolution pick.
- **Ruling:** "I thought it was a bit more of the backend portion; actually getting the GUI looking nice can be here then."
- **Ruled by:** operator
- **Why:** The intake job is paused, possibly stale, and was planned under the older, thinner planning system, so it may need replanning anyway.
- **Constrains:** This job owns the left-side GUI, setup screens included. The intake job's replan should drop its own GUI pieces or build them inside this job's layout.

Your remark about the intake job's planning is a report, not an instruction. I'm not touching that job; its replan is a separate conversation.

**Next question: where exactly the line runs, because a GUI needs something underneath it.** Three of the intake job's GUI pieces only show data its backend produces:
- the needs-attention list, from its "readiness per use" piece;
- entering temperature and slit, from its user-value store;
- the inspector's "fact and its source" view, from its new scan record.

None of that backend exists. Building those screens here would mean building the backend here too, which is most of the intake job.

The two things the le Tacon group actually need at the beamtime are small, though:
1. **Somewhere to keep experiment metadata.** Title, proposal, sample, people, dates: a small file in the workspace, written by the GUI. It's descriptive only and never used by any fit.
2. **A resolution-folder setting chosen in the GUI.** Today the only route is the card line `resolution_folder = "rf"`, resolved relative to the card's folder. The card is copied into the workspace, so that path misses the le Tacon `rf/` folder, which sits beside the data. A new user gets every analyzer held as "missing resolution", and code may not edit the card. One function reads that setting (`isar/session/calibrations.py`). Teaching it to prefer a workspace-recorded folder over the card's is a few lines plus the store.

**My recommendation:** this job builds the whole left-side GUI plus those two small stores. I'd name and shape them to match the intake design's `intake.settings.json`, so the intake job adopts them rather than replacing them. The needs-attention list, value entry and fact provenance stay with the intake job's backend. Their GUI comes with that backend into places this layout leaves for it. The new scan list shows a missing temperature or Q as a plain "—" with a reason on hover, never as a blank.

**What I rejected.** Building the needs-attention list here against today's data: it would be a second implementation the intake job throws away.

**When my recommendation would change:** if you want the le Tacon group able to type a missing temperature into ISAR at the beamtime. Their example files carry `#T=` and `#Q=` in each file, so I'm assuming the real files will too.

**Where I want the push:** that assumption. Will next week's files carry temperature and Q the way the CsV₃Sb₅ examples do, or do you expect bare files, with Q only in the separate `gpi.*` files?

## User

No, wait. I specifically was saying that *the other job might be under an old planning system and there's no point in trying to rerun it* or *it might be fine and just need some replanning* or something, I don't know, please *look at it.*

## Assistant

You're right, I misread that. Looking at the intake job now.

## User

I have opened that job up to look at it, you do NOT message it or anything right now.

## Assistant

Understood: I won't message that session or touch the job. My check is read-only. The scout is reading the plan file, the design and git history, and nothing goes to the session you have open.

So far:
- **The plan predates the new planning standard.** It was written 22–23 October; the blueprint standard landed in Workflow on 24 October. So yes, it was planned under the older system.
- **The branch has barely moved.** It holds two commits (19 files, +1466/−300) for the first piece, catalogue entries and BL43 recognition. Main is 186 commits ahead, but only about 43 lines in parsers, calibration, schema and GUI changed under it. A rebase looks plausible; that's my estimate, not yet tried.
- **The stall had a mechanical cause.** That first piece was marked red because it touched files outside its plan list, and the check passed otherwise. Since 23 October that file-list check is only advisory, so the stall's cause is gone.
- **The beamtime was already in that job's scope.** In its discussion you said: "we have an experiment in a week so we need to be ready to support them where they can edit what they need (slits, temperature) and not what they don't (scan file formats)." So typing a temperature or slit into the GUI was meant for this beamtime.

When the scout reports, I'll give you a verdict: build as written, refresh the plan, or replan from the design. I'll also say what that means for where our line runs.

The intake job (`jc54d8ce88c0c`) is worth keeping, but its plan needs a refresh before anyone builds from it. That's not a rerun from scratch. I only read files and git history; nothing was sent to the session you have open.

**The design still holds.** Nothing on main contradicts its scope or your rulings in it. Those were catalogue entries per format revision, your values kept beside the file's values with yours winning, per-use readiness, and ticked-scan entry for temperature and slit.

**The plan is the older, thinner kind.** It was written 22–23 October, a day before the new planning standard landed. Each piece describes what it should do and when it's done. But none spells out how it connects to the others: exact function contracts, which existing callers and tests it reaches. Its test commands are also sized for 16 workers rather than about four. A planning coordinator can refresh it against the approved design without reopening the design with you.

**The built code is better redone than rebased.** The two commits added a new `isar/catalogue/` package and reworked the BL43 parser. Main has since changed the same files: the BL43 parser, the scan record, ingest and the card, during the fast-pass and deep-pass work. Merging them would mean reconciling behaviour, not just accepting one side. The scout's judgement, which I share, is that a fresh build on today's main, using the old commits as reference, is cheaper. That piece's test folder doesn't exist on main either.

**Where it overlaps this job.** The first part of the intake plan has six pieces: four backend, two GUI.
- *Backend:* the catalogue and BL43 recognition; a store for your values and the experiment settings, including where the resolution curves live; ingest and init on the catalogue; readiness per use.
- *GUI:* (5) a needs-attention list in the Files panel, with tick-the-scans value entry; (6) a setup section above it showing what was detected, with "Locate" for the resolution folder, plus an inspector table of each fact: "from the files / yours / used".

Both GUI pieces sit on exactly the left side we're redesigning. Under your ruling that the GUI is built here, I'd move them into this job.

**My recommendation:**
1. **Intake job:** refresh its plan against the existing design, drop its two GUI pieces, and build its four backend pieces fresh on main.
2. **This job:** in two stages.
   - *First, nothing it waits on:* the window layout, the first screen, workspace creation with optional experiment metadata, the compact scan list, the watch-folder dialog, the inspector as an optional side panel, laptop/desktop layouts, and the console.
   - *Then, on the intake backend once it lands:* the setup section with resolution-folder Locate, the needs-attention list with value entry, and the fact table.
3. **If the intake backend slips past the beamtime:** stage one alone still ships a clean left side. The fallback for resolution curves is the one I raised before: a small resolution-folder setting stored in the intake job's settings format, so that job adopts it later.

That is my call on mechanics. The choice is yours, since you have the intake job open.

**The question:** do you agree that the intake job is refreshed and built as backend only, and that its two GUI pieces move into this job as a second stage? Or would you rather handle that job differently?

My earlier question still stands too, because it decides how much the fallback matters. Will next week's files carry temperature and Q inside each file, as the CsV₃Sb₅ examples do with `#T=` and `#Q=`? Or do you expect bare files, with Q only in the separate `gpi.*` files?

## User

Ok, I will get the other intake job running then. It's starting.

Your recommendation is taken; we have the intake job working, then you can work on the GUI, you're free to message the other session too (isar-c6 I believe)

Regarding what we get for file information, I will tell you the same thing I told the other job: I don't know for certain what will be carried next week. I assume it will look the same as the data we have now; I think we can get more later, not less, as the safe assumption, but right now we build against what we have.

## Assistant

Good. I've recorded both rulings and messaged isar-c6.

**Decision: the intake job builds the backend, this job builds its GUI as a second stage** (2026-10-27)
- **Question, as put:** Should the intake job be refreshed and built as backend only, with its two GUI pieces moving into this job as a second stage?
- **On the table:** intake job keeps its GUI pieces; GUI moves here in two stages, with a small resolution-folder fallback here if the backend slips.
- **Ruling:** "Your recommendation is taken; we have the intake job working, then you can work on the GUI"
- **Ruled by:** operator
- **Constrains:** Stage one of this job (layout, first screen, workspace creation, scan list, watch dialog, inspector panel, laptop layouts, console) depends on nothing. Stage two (setup section with resolution Locate, needs-attention list, value entry, fact table) is built on the intake backend's contracts once they land.

**Decision: build against today's file contents** (2026-10-27)
- **Ruling:** "I assume it will look the same as the data we have now; I think we can get more later, not less, as the safe assumption, but right now we build against what we have."
- **Ruled by:** operator
- **Constrains:** The scan list reads temperature and Q from what the CsV₃Sb₅ files carry (`#T=`, `#Q=` per file). Parsing the `gpi.*` files is not in this job.

**The message to isar-c6.** I told it the ruling and asked for three things:
- the exact contracts my stage-two GUI will call, written into its refreshed plan;
- to keep its units off the four GUI files I'm restructuring;
- where experiment metadata should live: I lean towards its experiment-settings file, so there's one place.

The message was delivered to its inbox but isn't read yet. No reply is needed for us to go on.

One fact I checked that affects the scan list. The CsV₃Sb₅ files are summed sets with no single scan number, so ISAR invents a ten-digit placeholder. That's the "scan 1842762476" in your screenshot. My default, which is mechanics: the top-level row shows the real scan number when the file has exactly one, otherwise the set's name (`CsV3Sb5_97K_0p5_0p5_3_sum`), with the summed scan numbers in the inspector. The placeholder never appears.

---

**Next question: what the first minute looks like.** This decides the left side's top half and how saving works.

**Today:** The app opens with only Files and Log. The first drop silently creates a *temporary* workspace and switches to the full layout. That's the mode change you disliked, and the "Unsaved workspace ●" in your title bar. Nothing is kept until the user remembers Save As, which copies everything to a real folder.

**What I propose:**
1. **The window opens in its final layout every time.** Left, centre, right and bottom are all present, empty where there's nothing yet, so nothing appears or rearranges later.
2. **The top left carries the experiment.** With none open, it shows two clear actions, *New experiment…* and *Open…*. The files area directly under it is labelled as the drop zone.
3. **New experiment is a short dialog:**
   - **Name**, required.
   - **Folder**, required, defaulting to a standard location plus the name.
   - **Details**, optional and collapsed: proposal, sample, people, dates, notes.
   - Stage two adds the resolution folder here.
4. **A real folder from the start.** The experiment is a named folder the moment it's created, and everything ISAR does is written there as it goes. The temporary workspace and the "unsaved" state disappear.
5. **Dropping data with no experiment open opens the same dialog.** The name is filled in from the dropped folder, and the files wait. *Create* ingests them; *Cancel* ingests nothing. With an experiment open, a drop anywhere in the window ingests, and the Files list highlights what arrived so the user sees where it went.

**What this commits to:** one up-front decision, a name and a folder, before the first scan appears. In exchange, nothing is ever "unsaved", a crash loses nothing, and the experiment is one folder the group can carry away.

**What I rejected:**
- *A blocking questionnaire.* The partner and the panel both warned that forced detail at a beamline gets filled with "test" and "asdf", so details stay optional.
- *Keeping the temporary workspace.* It's what produced the hidden mode change, and the risk of losing everything if Save As is forgotten.

**Where I want the push:**
- **The required folder choice up front.** If you'd rather the group can drop and go, with ISAR naming the folder itself and allowing a rename later, that's the other honest version. It's faster to start, but it leaves experiments wherever the default put them.
- **What you meant by "saving" at the bottom right.** If the folder is always current, I'd read it as a status line: which experiment folder, last written when. Did you mean that, or an explicit Save or Export action?

isar-c6 replied, and the backend job has changed under us:

- **New job, narrower scope.** The old intake job (`jc54d8ce88c0c`) is cancelled. Its replacement is `jfada74237892`, now in design with you: BL43LXU only, backend only, with BL43's format read from the file shipped with ISAR.
- **Stage two shrinks.** With no multi-format recognition, the setup section only has to show what was read (scans, analyzers, incomplete sets, unread files) and where the resolution curves were found. Nothing about choosing a format.
- **Its side of the coordination is agreed.**
  - It will send exact contracts once its plan is accepted.
  - It stays off the four GUI files I'm restructuring.
  - Experiment metadata goes in its one experiment-settings record, provisional until you approve its design.

None of this changes the question in front of you, which is the first minute:

- **My proposal.**
  - The window opens in its final layout every time.
  - The top left shows *New experiment…* / *Open…* above a labelled drop zone.
  - *New experiment* asks for a required name and folder; proposal, sample, people, dates and notes are optional and collapsed.
  - The experiment is a real folder from the moment it's created, so there's no temporary workspace and no "unsaved" state.
  - Dropping data with no experiment open opens that same dialog with the name filled in, and *Cancel* ingests nothing.
- **Push wanted on two points:**
  - Should the folder choice be required up front, or should ISAR name it and let the group rename it later?
  - By "saving" at the bottom right, do you mean a status line (which folder, last written when), or an explicit Save or Export action?

## User

Your proposal looks ok. I think I'll need to see a mockup to really settle it though.

ISAR can name it first and the group renames it later if they want.
Saving should be a status line if they have a folder set up to automatically dump outputs to, and explicit save/export actions

## Assistant

Mock rendered; my own laptop frames clip the Parsed column, which is exactly the fault the GUI rules forbid. Fixing it before you see it.

I'll use the Edit tool directly instead.

The mockup is ready. Open the HTML in a browser; the desktop frame is drawn at half size, so zoom in to read it:

- `C:\Users\AMM\AppData\Local\Temp\claude\ja0cd202ecb67\mock\isar-lhs-mock.html`
- a picture of the first two frames: `…\mock\mock-laptop.png`

It's a static picture; nothing is built. It shows five frames:
1. first launch on a laptop (1108×851, TAVI's laptop test size);
2. after one drop;
3. the laptop with the inspector open and the Fast/Deep columns switched on;
4. the desktop (2560×1392);
5. the Details… and Watch… dialogs.

Scan names and temperatures follow the CsV₃Sb₅ set. Every Q is invented. "a16: no Q" and the two unread files are made-up hostile cases, there to show how gaps look. My first draft clipped the Parsed column at laptop width, which is exactly the fault the GUI rules forbid. I widened the scan column and shortened the long set names, with the full name on hover.

First, your two answers, recorded:

**Decision: ISAR names the experiment; the group renames it later** (2026-10-27)
- **Question, as put:** Should the folder choice be required up front, or should ISAR name it and let the group rename it later?
- **Ruling:** "ISAR can name it first and the group renames it later if they want."
- **Ruled by:** operator
- **Constrains:** There's no dialog before the first scan. A drop with no experiment open creates one, named after the dropped folder in a default location, then ingests. A one-time note offers *Rename* and *Add details*.

**Decision: output status line plus explicit Save and Export** (2026-10-27)
- **Ruling:** "Saving should be a status line if they have a folder set up to automatically dump outputs to, and explicit save/export actions"
- **Ruled by:** operator
- **Constrains:** The bottom right shows where output goes and when it was last written, beside *Save copy as…* and *Export…* buttons.

**Still a lean, not a ruling:** the overall first-minute flow. You said "looks ok" but want the mockup to settle it, so it stays open until you've looked.

**What I most want your reaction on, beyond the general look:**
- **Laptop space.** On the laptop, the processing panel collapses to a thin "Processing ‹" strip on the right edge. The inspector opens as its own column between the scan list and the display, squeezing the display (frame 3). The alternative is the inspector floating over the display, which keeps the display's width but hides part of it.
- **Columns.** Parsed is on by default; Fast pass, Deep pass, slit and file name are switched on from *Columns ▾*. Sets show "28/28" and a running count like "11/28"; analyzers show a status word with a coloured dot.

**One scope point I need you to rule on: auto-export.** Your "a folder set up to automatically dump outputs to" reads as a new feature: ISAR writing export files to a chosen folder every time a result lands. Three things bear on it:
- No export exists in ISAR yet. It's part of the paused fitting job's export piece (`modes.csv`, `fits.json` and the spectra, one timestamped folder per request).
- What does exist: the experiment folder already receives every result as it's computed (`report/results.csv` and the rest), automatically.
- So either:
  - **(a)** the status line reports *that* folder, the job builds the *Export…* button by pulling in the paused export design, and auto-export waits for later; or
  - **(b)** auto-export is in scope too, which makes the job larger.

I recommend (a). The group still gets everything written automatically, just in ISAR's own folder layout. A plain export on demand covers taking results to their own programs.

Which do you want, and how does the mockup sit with you?

## User

Looks good. One item, we should let the scan column go top-to-bottom, so the log moves slightly right.
(a) is fine. We don't need auto-export right now if it's a bigger hassle.

## Assistant

Recorded. The first-minute flow is now settled, not a lean.

**Decision: the mockup's layout, with the scan column full height** (2026-10-27)
- **Ruling:** "Looks good. One item, we should let the scan column go top-to-bottom, so the log moves slightly right."
- **Ruled by:** operator
- **Constrains:** The left column (experiment, files strip, scan list) runs from the top of the window to the bottom. The bottom bar (log on the left, output status on the right) spans only the centre and right.

**Decision: export on demand, no auto-export** (2026-10-27)
- **Question, as put:** (a) the status line reports the experiment folder, the job builds *Export…* from the paused export design, and auto-export waits; or (b) auto-export is in scope too.
- **Ruling:** "(a) is fine. We don't need auto-export right now if it's a bigger hassle."
- **Ruled by:** operator
- **Constrains:** This job builds *Export…*, writing one timestamped folder with `modes.csv`, `fits.json`, each spectrum's data and a units note. That's the paused fitting job's export piece, so its replan should drop that piece.

---

**Next question: what happens to the campaign-era features.** This decides how much existing code the job removes, and whether a campaign can still be run from the new window.

**What exists today, and isn't in the mockup:**
- **The Campaign dock:** start, stop and resume an `isar drive` campaign. A standing ruling (G19 in `docs/design/gui.md`) says it exists in every window state, because a campaign can start with no data.
- **The old Scans dock:** about 2000 lines. It holds *pooled masters* (several scans at the same temperature and Q combined into one spectrum before fitting), manual *groups* (drag scans into a named set, then drop the set on the waterfall, plane or dispersion views), *lenses*, and the Home/Scans/T/q tabs.
- **Browser, Card and Scan Table:** three docks currently tabbed with Scans.

None of this matters for the le Tacon data. Each analyzer file set is one temperature, so there's nothing to pool. But it is how a TAS campaign is run and read.

**My recommendation: keep it all reachable but out of the way, and delete nothing in this job.**
- The Campaign panel becomes a second tab of the right-hand panel, *Processing | Campaign*, so "driving" lives where your spatial grammar puts it. It still works with no experiment open, as G19 requires.
- The old Scans dock, Browser, Card and Scan Table go to the *View* menu, hidden by default, with their code unchanged.
- The new scan list is the left side's list. When a workspace has pooled masters (not BL43), they show as a "Pooled" section at the bottom of the list, like "Files not read", so the list never hides what was fitted.

**What this commits to:** for a while, two scan lists in the code, the old one hidden. That's a maintenance cost and a clean-up job later. In return there's nothing to rebuild before the beamtime, and no campaign capability is lost.

**What I rejected:** removing the old Scans dock and its groups now. It's cleaner, but it deletes the waterfall/plane/dispersion "drop a group" route. Replacing that route properly is a design question in its own right, and not one for this week.

**Where I want the push:** whether keeping the old dock hidden under *View* is acceptable, or whether you'd rather it go now and accept losing group drops until a later job.

One small default, unless you object: *Sort by analyzer* flips the tree, so each analyzer becomes a top-level row with its scans (one per temperature) underneath. *Sort by temperature* just orders the scan sets.

## User

I think your recommendation is fine, if we keep the old view hidden for now it should be fine. The campaign as a second tab we don't open right now is good.

## Assistant

Writing up the design document from everything you've ruled.

I've recorded your last answer, and the design is written.

**Decision: campaign tools kept, hidden; Campaign as a second right-hand tab** (2026-10-27)
- **Ruling:** "I think your recommendation is fine, if we keep the old view hidden for now it should be fine. The campaign as a second tab we don't open right now is good."
- **Ruled by:** operator

**The design document is written** from your rulings, for a reader who wasn't here. It covers:
- **Stage one, for the beamtime:**
  - the launcher with no console;
  - one window arrangement from first launch;
  - laptop and desktop layouts;
  - an experiment ISAR creates and names on the first drop, with no temporary workspace;
  - a one-line files strip, with the watched folder in a dialog;
  - the scan list;
  - the inspector as an optional column;
  - the right panel with *Processing* and *Campaign* tabs;
  - the bottom bar with Log, plus Output with *Save copy as…* and *Export…*.
- **Stage two, on the intake backend:** the setup section with resolution *Locate…*, the needs-attention list with value entry, and the fact table.

Two questions are left for planning, each with its options and costs written out:
- where the experiment details are stored;
- the resolution-folder fallback if the intake backend misses the beamtime.

The two mandatory checks on the design are running now:
- a cold reading for wording that two reasonable builds could read differently;
- a fidelity check of the design against this conversation.

When both are back, I'll fix what they find and present the design for your `/approve`.

Both checks are back: 13 findings, all small wording or scope tightenings. Applying them now.

The design is presented and waiting for your `/approve`.

Design: [design.md](file:///C:/Users/AMM/AppData/Local/ACS/state/v1/working/ja0cd202ecb67/design.md)

**What it is:** ISAR's window and left side reworked for the BL43LXU beamtime, in two stages.

**Stage one** depends on nothing and is meant to be ready before the beamtime:
- the launcher opens no console window;
- the window keeps one arrangement from first launch, with laptop and desktop layouts chosen from the screen;
- dropping data creates an experiment that ISAR names, as a real folder, with no "unsaved" state; *Rename* and optional *Details…* come after;
- a one-line files strip, with watched folders in a dialog;
- the scan list: scan sets expanding to analyzers with Q and status, Parsed on by default, Fast and Deep pass switchable, sort by scan, temperature or analyzer;
- the inspector as an optional column;
- the right panel with *Processing* (today's Run analysis) and a *Campaign* tab you don't open by default;
- the bottom bar with Log, and Output with *Save copy as…* and *Export…*.

The old campaign panels move to *View*, hidden; nothing is deleted.

**Stage two** comes after the intake backend (job `jfada74237892`) lands: the setup section with resolution *Locate…*, the needs-attention list with tick-and-enter values, and the inspector's fact table.

**What the two design checks changed.** Both checks came back with 13 findings between them, all tightenings I adopted:
- **"Move" is gone.** You only ruled *Rename*, so relocating an experiment is now *Save copy as…*. *Rename* is refused while an analysis is running in that folder.
- **The "queue is planned" note is gone** from the Processing panel. The mockup showed it; the design no longer does.
- **The watch dialog keeps today's list of folders**, so more than one remains possible, rather than the single folder the mockup drew.
- **The design now says plainly what stage one alone gives the beamtime.** A missing slit is inferred from the elastic lines, as today. A scan missing a resolution curve is held, as today, and the fallback covers that. Typing a temperature or slit by hand is stage two.
- **The export contents are sourced.** They come from the fitting job's design you already approved, not new requirements.
- **Where experiment details are stored is marked as an agent lean,** not your ruling. It waits on your approval of the intake job's design.

**One harmless leftover.** One of the job's five review questions still says "Rename, Move and Save copy as". My correction was refused because you have the design open in Orca's pane. The reviewers will simply find no Move to check. If you'd rather it be clean, say so and I'll fix it once you've closed the pane.

**The mockup lives in a temporary folder:** `C:\Users\AMM\AppData\Local\Temp\claude\ja0cd202ecb67\mock\isar-lhs-mock.html`. If you want it kept for the record, I can copy it somewhere durable.

Nothing is built until you approve.

## User

/transcribe
