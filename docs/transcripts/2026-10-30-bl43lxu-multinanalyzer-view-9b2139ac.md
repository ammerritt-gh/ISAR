# BL43LXU multinanalyzer view

**Date:** 2026-10-30  
**Session:** 9b2139ac-6a61-4c9d-9fff-742507124dbf

*Cleaned transcript: tool calls, tool results, thinking and harness notices removed. Nothing else edited.*

## User

While I test out the interspectra fitting, I'd like to work on a new view, a BL43LXU-specific "multinanalyzer" view (although we should keep in mind that other IXS beamlines like S30 have multiple analyzers, and similar things may exist in the future for INS instruments we bring in, so t's not specific to it alone, just for now). The screenshot gives an idea what I want: the 4x7 array of analyzers (I believe it is 7 in-plane and 4 out-of-plane (scattering plane that is)), given as a grid of squares, that we can use to view a single scan's full set of data at once. Here are the important elements:
- The view is a separate dock a user can open in "View" that is popped out by default, although it acts like a normal dock and they can move it around and dock it as they wish.
- The grid shows the analyzer array (BL43LXU for now, others later), with the analyzers labelled
- We will use a dual naming system: the internal naming I will use is A-Z (in plane row) 1-N (out of plane row), so BL43LXU goes from A1 to G4 I believe. We will also have multiple instrument-derived names for analyzers; we have analyzers labelled a03 to a41 in the scans, I have attached an old logbook image that apparently shows only a 4x6 grid (so obviously we'll need to include versioning and attach this as part of the instrument catalogue...).
- Clicking a single scan or scan set fills in all available loaded analyzer positions on that scan; the array only works with one scan at a time (for now) so if the user selects multiple scans, it shows the first picked and says exactly which scan it's using at the top (the header gives the scan information) and that multiple scans are selected so it's only showing one
- The default view is q position below each analyzer, then the "loaded" fit shown within each grid square, so we have a grid of fits that a user can see at a glance.
- We try to label the in-plane and out-of-plane axes, if that's feasible, so we know what the out-of-plane and in-plane directions are, as axis labels on the X and Y axis, letting us see the general relationship for in-plane versus out-of-plane analyzer q vectors
- When marking anchors or fixed fits, we mark those grids clearly, with clear outlines around them and small icons in the top-left
- When running batch inter-spectrum fits, we show the active and already fit analyzers versus to-be-done, so the user can see active progress within one scan (when doing across-q spreading).

/ui-professional 
/dialogue

## Assistant

Reading up on the existing pieces before I ask you anything: launching a codebase recon now.

Recon is back; reading the top half, then I'll bring you the first question.

A worthwhile view, and most of what it needs is already in place. Before any options, here's what's missing. ISAR doesn't know where any analyzer physically sits in the array. And during a batch it reports progress as counts for the whole batch, never per analyzer.

**Size: small-to-medium.** The overall shape is clear, but a few forks would change what gets built. I read the code through a recon pass, then ran a five-model cold reading of your request as written. The facts below come from the code, checked at the cited lines where I lean on them.

**What exists today**
- **Scan model.** One BL43LXU scan is a set of files, one per analyzer, and ISAR makes one spectrum per analyzer. The format card (`isar/parsers/cards/bl43lxu.policy.toml:11`) lists 28 analyzers: a03–a09, a14–a20, a25–a31 and a35–a41. That is four runs of 7 consecutive numbers, which fits your 7 in-plane × 4 out-of-plane.
- **Your logbook photo** shows rows of 6: a35–a40 on top, a03–a08 at the bottom, with the numbers running right to left. That is my reading of the photo, not checked against any beamline document. Nothing in the repo records analyzer positions, so the catalogue would be new. It fits the "translation catalogues with revisions by date" section of DESIGN_GOALS.
- **q per analyzer.** Each spectrum already carries its own Q/HKL. A crystal-frame Cartesian Q exists inside inter-spectra (the second pass that fits neighbouring spectra together) once a lattice is set. Nothing yet says which direction is in-plane and which is out-of-plane.
- **Fit states.** Each spectrum already has:
  - an *authoritative* fit (the one ISAR uses);
  - a *shown* fit, picked in the Scan(s) inter-spectra bar;
  - *locked*, *stale* and *flagged* marks.
- **Anchors.** Anchors (spectra you trust, which an anchored batch grows outward from) and locks are kept per spectrum. You set them today in the scan list's Anchor column or the Processing dock's Anchors box. "Spread in q" already means all analyzers of the anchor's scan, which is exactly this grid's scope.
- **Live progress.** Per-analyzer live state needs a small backend addition: the batch tracks each spectrum's state internally but only sends counts. That's my inference from `runner.py`, not yet confirmed.

**Question 1: what can you do in the grid, beyond looking?**

This sets most of the build. A view you only look at is one size of job. A grid you act in has to stay consistent with the scan list, the Anchors box and the inter-spectra bar.

My recommendation is **a viewer that drives selection**:
- Clicking a cell makes that analyzer's spectrum the one shown in the main window, where the fit menu, Make authoritative and Lock already live.
- Selecting several cells (Ctrl- or Shift-click) selects the same spectra in the scan list. "Add selected scans as anchors" then works from a grid selection with no new button.
- The grid only *shows* anchors and locks. It never sets them itself.
- *What this commits to:* the grid and the scan list share one selection.
- *What I rejected:* Anchor, Lock and Refit actions on each cell. That would put a third place to set an anchor beside the scan-list column and the Anchors box. My recommendation changes if you'd routinely pick anchors by eye in the grid, say "these three corners", where a right-click Anchor on the cell would save a real trip.

**Where I want the push:** do you picture yourself picking anchors *in* the grid, or only checking them there? From here I can't tell which is your working habit.

## User

Q1: I'd say that the initial view here is read-only; further jobs will be to make it more interactive, but for now just a view is the MVP. Being able to choose selection and have the scan list and the analyzer view match is good, so let's make that the minimal interactivity.

## Assistant

Recorded.

> **D1 · Ruling (operator, 2026-10-30): interaction scope for the first version.** The grid is read-only. Its one interaction is selection, shared both ways with the scan list: what's selected in one is selected in the other. Anchoring, locking or refitting from the grid is left to later jobs. *Why:* a working view is the minimum product; more interaction comes later.

**Question 2: how is the array physically arranged, and which analyzer is A1?**

*Why now:* the grid's layout, the A1–G4 names and the axis labels all rest on this. If I guess wrong, the names stick to the wrong analyzers, and a name pinned to the wrong analyzer is the worst error this view can make. Only you, or a beamline drawing, can answer it.

*What I know:*
- The format card lists four runs of 7 consecutive numbers (a03–a09, a14–a20, a25–a31, a35–a41).
- Your logbook printout shows a35… on the top row and a03… on the bottom, with numbers rising right to left.
- Your first image shows a grid of squares with a red line through the middle row. I read that as the scattering plane drawn across the grid. Is that right?

*What I'd propose, for you to correct:*
- Draw the grid exactly as the beamline printout has it, so the screen and the logbook agree at a glance.
- Letters A–G run left to right and rows 1–4 run bottom to top. That makes **A1 = a09, G1 = a03, A4 = a41, G4 = a35**.
- The scattering plane is a line across the grid where it physically lies.
- *The alternative I rejected:* A1 = a03. The internal names would then follow the instrument's numbering order, but letters would run right to left on screen, which reads badly.

*What I need from you* (point me to a drawing or file if you don't know these offhand):
1. Does the grid as printed match the physical layout, and from which side is it seen?
2. Where does the scattering plane sit relative to the four rows: through one row, or between rows 2 and 3?
3. Along a row, does 2θ (and so |Q|) increase toward a03 or toward a09?

**Where I want the push:** whether letters should follow the screen (my proposal) or the instrument numbering. You'll be saying "C2" aloud with colleagues, so the convention is yours to set.

## User

Q2:
1. The grid as printed should match the layout, and we are looking from the sample towards it. So A3 is the bottom-right, a8 the bottom left, and a9 is part of a new column to the left of a8.
2. The scattering plane passes through the bottom row, a8 to a3
3. I believe we normally set a center analyzer as the "0" main analyzer; this is clear in the sample data we have the a06 is the main one (it has a nice round hkl of (0.5, 0.5, 3), so increasing is to the left (towards a9) I believe. However, you can and should double-check this against the reported q values for analyzers and ensure this matches.

## Assistant

Checked against the CsV3Sb5 `gpi` macro, the beamline's gnuplot file that lists each analyzer's hkl and |Q| (`real_data/leTacon_SP8_CsV3Sb5/gpi_files/`). I read it only; none of these numbers goes into the repo. Your reading holds on all three points:

- **|Q| rises along a row toward a09.** In the a03–a09 row it climbs steadily, about 1.5 nm⁻¹ per analyzer, from a03 up to a09. a06 sits exactly on the nominal (0.5, 0.5, 3), so it is the main analyzer.
- **The scattering plane runs through the bottom row.** Going up a column (a03 → a14 → a25 → a35), |Q| grows *quadratically* from the a03 row: steps of roughly 0, 1, 4 and 9 times the same small amount. That pattern is what a vertical offset from a plane lying in that row produces.
- **The old 4×6 sheet** is the same array without the a09/a20/a31/a41 column, which was added later on the left.

One bonus: the hkl step between neighbouring analyzers is nearly constant, both along a row and up a column. So the axis labels can show the actual direction in reciprocal-lattice units, taken straight from that scan's own hkl values with no lattice needed. For example: "in-plane: +(0.04, 0.03, 0.18) r.l.u. per analyzer". That makes the label feasible, and specific to each scan.

> **D2 · Ruling (operator, 2026-10-30; checked against the CsV3Sb5 gpi macro): BL43LXU array geometry.** The grid is drawn as seen from the sample looking at the analyzers. The bottom row is a09 a08 … a03 from left to right, and the top row is a41 … a35. The scattering plane passes through the bottom row. |Q| increases leftward, toward a09. a06 is the main (centre) analyzer. The old 4×6 printout is the same array before the a09/a20/a31/a41 column was added.

**Question 3: which end of a row is letter A?**

*Why now:* this fixes every internal name, and the names go into what you and colleagues say aloud and write in the logbook. Rows are settled either way: row 1 is the in-plane bottom row and row 4 the top.

Last turn I proposed A at the screen's left. Your answer changes my recommendation:
- **(b), my recommendation now: A1 = a03.** Letters follow |Q| and the instrument's own numbering: A1 = a03 at low |Q|, D1 = a06 the centre analyzer, G1 = a09 at high |Q|, and G4 = a41. On screen, A sits at the right. That costs little, because every cell prints its own name. The names also hold if the array is ever drawn from the other side.
- **(a) A1 = a09.** Letters read left to right on screen, but A then means the highest |Q| and runs against the instrument numbers. A future 8th column would shift every letter by one.

**Where I want the push:** whether you think of the row by screen position or by |Q|. If you already say things like "the left-hand analyzers" at the beamline, (a) may suit how you talk.

## User

I gave backwards instructions for the letters/numbers. Looking at the logbook picture, A1=a03, A2=a04, ... then B1=a14, B2=a15... so letters are out-of-plane, numbers are in-plane, we go from bottom-right, read right-to-left and bottom-to-top.

## User

APS has a single row of analyzers, I believe they also number it the same, so this should work out fine: low number is lowest 2theta

## Assistant

Corrected. That also settles last turn's question, which only existed because of the mix-up. Your scheme maps cleanly onto the instrument numbering, and it covers APS's single row as row A.

> **D3 · Ruling (operator, 2026-10-30): internal analyzer names. Supersedes the A1 = a09 / A1 = a03 question.** Letters are out-of-plane rows, starting at A for the bottom row (the one the scattering plane passes through). Numbers are in-plane positions, starting at 1 for the lowest 2θ. On the grid as seen from the sample, names read right to left and bottom to top. BL43LXU: A1–A7 = a03–a09, B1–B7 = a14–a20, C1–C7 = a25–a31, D1–D7 = a35–a41, so a06 (the centre analyzer) is A4. APS S30 is one row, A1…An, with low numbers at low 2θ. *Why:* it matches the beamline logbook's reading order, and the numbers follow 2θ.

**My default, for you to correct: the fit a cell shows is the one ISAR uses for that spectrum.** That is the authoritative fit, or the fast solo fit when no inter-spectra fit exists. It's also what the main plot shows by default (`scans_page.py:273`). A fit you pick from the main window's fit menu is held only for that session, and only for the spectrum you're looking at, so the grid doesn't follow it.

**Question 4: what does each cell plot, and on what scale?**

*Why now:* this is the view's main promise, a grid of fits you can read at a glance, and the choice decides what the eye can compare across the 28 cells. Your printout uses a log intensity axis, with roughly the same decades in every cell and the same energy range throughout.

My recommendation:
- **Energy (x) is the same range in every cell:** the scan's own window. Cells line up column by column, so a peak's position can be compared across the grid.
- **Intensity (y) is a log axis scaled per cell,** like your printout. Each cell fits its own data, so a weak analyzer still shows its line shape rather than a flat smear.
- **In each cell:** data as points without error bars (too dense at this size) and the fit as a line. A spectrum with no fit shows its data and the words "no fit". A cell with no file for this scan is drawn empty and labelled "no file". Hard rule 4 is respected: a spectrum held for missing energy calibration says "not zeroed" rather than plotting on a guessed zero.
- *The alternative I rejected:* one intensity scale shared across all cells. It looks comparable, but the analyzers differ in efficiency (your printout's "Eff" column), so equal heights don't mean equal signal. It would also need counts per monitor to be honest at all.
- My recommendation changes if you actually read relative intensities across the grid at the beamline. Then a shared, per-monitor scale earns its place.

**Where I want the push:** log against linear. Log suits elastic-dominated IXS spectra like your printout's. On a linear axis the weak phonon wings almost vanish beside the elastic line, but linear is how the main plot draws them. Do you want log only, or a log/linear switch in the dock's header?

## User

Energy must line up because of the way an IXS instrument works, they can't measure different energies even if we wanted to. Intensity is linear; log scales are horrific and I'd complain to the beamline scientist. No error bars, agreed, total fit as a line, small filled symbols, we default to normalized, total fit as one dashed line and each individual fit as thin solid lines.

## Assistant

Linear it is. I'll spare the beamline scientist the complaint.

> **D4 · Ruling (operator, 2026-10-30): how a cell is plotted.** Energy is the same in every cell, because the instrument measures every analyzer at the same energies. Intensity is linear, never log. Data are drawn as small filled symbols with no error bars. The total fit is one dashed line and each fit component is a thin solid line. Intensity defaults to normalised. *Why:* log scales are unreadable for this, and IXS physics fixes the energy axis.

"Normalised" already has one meaning in ISAR, so the grid reuses it. The main plot has a Raw / Normalised switch, and normalised there means counts per reference monitor: one monitor value for the whole experiment, so every spectrum is on the same footing (`scans_page.py:398`). My default is to give the dock the same two-way switch, set to Normalised. It covers the whole grid and is remembered like the main plot's.

**Question 5: does each cell get its own intensity scale, or do all 28 share one?**

*Why now:* this is the last open property of the cell plot, and it decides what a glance tells you. With linear intensity the choice matters more than it did with log.

- **Per cell, my lean:** each cell runs from zero to its own maximum. Every analyzer's line shape is readable, including a weak one at high |Q| or far out of plane. The cost is that two cells of equal height can differ in intensity several times over, so you can't read intensity across the grid. The peak value would need printing in the cell to recover that, the way your printout prints "Pk".
- **Shared:** one zero-to-maximum scale for the whole grid. Since normalised data share a reference monitor, heights then compare honestly, apart from analyzer efficiency. The cost is that cells far from the strongest one, usually the elastic-heavy ones, shrink toward a flat line, and their fits can't be judged by eye.
- *What I'd leave out unless you ask:* a per-cell/shared switch. You didn't ask for one, and a second header control makes the five-second scan slower.

**Where I want the push:** when you look at this grid at the beamline, are you judging *fit quality per analyzer* (per cell wins) or *how the signal moves across the array* (shared wins)? And if per cell, do you want each cell's peak value printed?

## User

Q5: Make it a toggle, all same or individual auto.

## Assistant

Done.

> **D5 · Ruling (operator, 2026-10-30): intensity scale.** A toggle in the dock: every cell on one shared scale, or each cell scaled to its own data.

Two small choices of mine, for you to correct. The toggle starts on per-cell and is remembered between sessions. I've left out printing each cell's peak value, since you didn't take that up.

**Question 6: what text and marks does each cell carry?**

*Why now:* this is the rest of what you read at a glance, and each mark you approve becomes a promise the grid has to keep current.

*My proposal:*
- **Top of the cell:** the internal name, prominent (A4), with the instrument name small beside it (a06).
- **Below the cell:** the analyzer's hkl in r.l.u. to two decimals, with full precision on hover. That matches the scan list since this morning's change. |Q| also goes below, in Å⁻¹. Both come from the analyzer's accepted hkl (the gpi macro first, else the file), and a cell with none says "no hkl" rather than showing a blank.
- **Anchor and lock marks.** I'm reading your "fixed fits" as ISAR's **locked** fits: a spectrum whose fit no batch will refit. Correct me if you meant something else. An anchor and a lock each get a clear outline and a small icon top-left. The two outlines are different (say, solid for anchor and dashed for lock), so they're told apart by shape and icon, never by colour alone.
- **Selection.** A cell selected (shared with the scan list) uses the window's ordinary selection highlight. That is a different channel from the outlines, so a selected anchor reads as both.
- **Absences are stated** (house rule: missing is never drawn as zero): "no file", "no fit", and "not zeroed" for a spectrum held without an energy zero.

*What I'm unsure belongs: two states ISAR already records per fit but that you didn't name.*
- **Stale:** the fit's inputs changed after it was made.
- **Flagged:** ISAR's verdict marked the fit for review.

Both say the drawn curve shouldn't be trusted as it stands. Leaving them off means a grid of good-looking curves can hide a stale one. I'd add each as a small word in the cell's corner, with no outline, so they don't compete with anchor and lock.

**Where I want the push:** stale and flagged, in or out? And is hkl plus |Q| too much text for a cell this small? If one has to go, I'd keep hkl.

## User

I'd say it's a global toggle: either each cell gets its own auto-scaled Y axis, or they share one fixed Y axis limit. No peak values printed

I don't like stale or flagged, leave them off. hkl is much more important than Q, but we should be able to fit both.

## Assistant

Noted.

> **D5 (restated) · Ruling (operator, 2026-10-30): intensity scale.** One global toggle for the whole grid: either every cell auto-scales its own Y axis, or all cells share one Y-axis limit. No peak values are printed.

> **D6 · Ruling (operator, 2026-10-30): cell labels and marks.** Each cell carries:
> - the internal name (A4) with the instrument name (a06);
> - hkl below the cell as the main label, and |Q| as well, both fitting in the cell;
> - an outline and a top-left icon for an anchor or a locked fit;
> - absences stated in words ("no file", "no fit", "not zeroed").
>
> Stale and flagged fits are **not** marked. The window's ordinary selection highlight shows the selection.

One check on wording. I read "one fixed Y axis limit" as one limit for every cell, set automatically to the largest value in the shown scan. If you meant a limit *you type in*, say so and the toggle gets a number box beside it.

**Question 7: during a batch, which states does a cell show, and how?**

*Why now:* it's the last behaviour you named, and it needs a small backend addition. A batch currently reports counts only (for example "9 of 24 done"), not which spectrum is in which state. How much state the cells show decides how big that addition is.

*What the batch actually does, from the inter-spectra design doc:*
- An **anchored** batch grows outward from the anchors in rings of neighbours, fitting one ring at a time.
- A **simultaneous** batch refits all its members together, in repeated sweeps.
- A refit that is accepted becomes the spectrum's authoritative fit the moment it's saved. So the grid's curve changes to the new fit as each cell finishes, unless the cell is locked.
- A refit can also be **refused** (it failed an acceptance check), **fail**, or be **not reached** (the batch ended first). In all three cases the old fit stays.

*My proposal:* the batch's members in the shown scan carry a state word with a matching mark:
- **Waiting:** the cell is slightly dimmed.
- **Fitting now:** a subtle, slow ring on the cell. In a simultaneous batch, every waiting cell is fitting at once in each sweep, and the dock's header says "sweep 2".
- **Done:** the new fit is drawn and the mark settles.
- **Fixed:** anchors and locks keep their usual marks; the batch never touches them.
- **Header:** one line summarising the batch, for example "Spread in q · ring 2 · 9 of 24 done". Members in other scans are counted there but not drawn.

*Where I need your ruling:* you named active, done and to-do. **Refused, failed and not reached** are different outcomes from "done", because the curve you see is the *old* fit. Shown as "done", they would quietly claim a refit happened. I'd mark each with its word and give the reason on hover, and clear all batch marks when you start the next batch or select another scan.

**Where I want the push:** whether those three outcomes earn a mark at all, or whether one "not refit" word with the reason on hover is enough.

## User

A limit the user can type in, so a box next to it.
Q7: This seems reasonable, the three states are good to make visible.

## Assistant

Both recorded.

> **D5 (amended) · Ruling (operator, 2026-10-30): intensity scale.** One global toggle: either each cell auto-scales its own Y axis, or all cells share one Y limit **that the user types into a number box beside the toggle**. No peak values are printed.

> **D7 · Ruling (operator, 2026-10-30): live batch progress.** During an inter-spectra batch, the shown scan's member cells carry one of these states:
> - **waiting:** the cell is dimmed;
> - **fitting now:** a subtle, slow ring on the cell;
> - **done:** the new fit is drawn;
> - **refused, failed** or **not reached:** each marked in words, with the reason on hover.
>
> Anchors and locks keep their usual marks. A header line summarises the batch, for example "Spread in q · ring 2 · 9 of 24 done". Batch marks clear when the next batch starts or another scan is shown. This needs a small backend addition so that a batch reports each spectrum's state, not only counts.

**Question 8: what do the in-plane and out-of-plane axis labels say?**

*Why now:* you asked for them "if feasible", and it is feasible. What they should *say* is the open part. This is the last behaviour item before I write the design up.

*What the data allow:* each scan's own analyzer hkl values give the step between neighbours. In the CsV3Sb5 file I checked, one analyzer to the next along a row is a nearly constant step in hkl, and so is one row to the next. The axis labels can therefore carry real reciprocal-space directions for *that scan*, with no lattice or orientation needed. ISAR would compute them in its read layer (`workspace_api`), not in the GUI, per hard rule 7.

*My proposal:*
- **Bottom axis:** "In plane — 2θ increases ←, Δhkl ≈ (+0.04, +0.03, +0.18) per analyzer". The figures here are illustrative only; each scan supplies its own.
- **Left axis:** "Out of plane ↑, Δhkl ≈ (−0.08, +0.12, −0.05) per row".
- Hovering an axis label gives the exact per-step values. "≈" is there because the step is an average and drifts slightly across the array.
- **A thin line through row A, labelled "scattering plane".** That's my reading of your first image, the squares with a red line through one row; I asked earlier and you didn't confirm. Is that what the image was for?
- **If a scan has no per-analyzer hkl,** the labels give the direction words alone and say "no hkl to compute the step".
- *What I rejected:* naming a crystal direction such as "along (H H 0)". These steps aren't round directions, so rounding them to one would show more certainty than the data have.

**Where I want the push:** is a raw Δhkl step what you'd want to read off the axis? You might prefer a normalised direction, or |Q| change per step on the in-plane axis, which is the quantity that actually varies along a row.

## User

Q8: Raw delta-hkl is fine, and it's useful because sometimes we'll get off-axis shifts.

## Assistant

> **D8 · Ruling (operator, 2026-10-30): axis labels.** The bottom axis reads "In plane — 2θ increases ←" and the left axis "Out of plane ↑". Each carries that scan's mean raw Δhkl per step, with the exact steps on hover. If a scan has no per-analyzer hkl, the label says so. *Why:* the raw step shows off-axis shifts, which do occur.

Still open from last turn: the red line in your first image. Is it the scattering plane, which you'd like drawn through row A? (a) yes, a thin labelled line; (b) no, that image meant something else.

**Question 9: how much layout versioning does this job build?**

*Why now:* you said the layout needs versioning in the instrument catalogue, so it goes in the design. The data change what it can do, though.

*What I found:*
- **The old 4×6 sheet doesn't need a version of its own.** It is today's array minus the leftmost column (a09/a20/a31/a41). Every old analyzer keeps its position and name, so old data drawn on today's layout is correct. The missing column shows as "no file".
- **Nothing to choose a version by.** A version would normally be picked by the date a scan was measured, but BL43 files carry no date. Neither the format card nor the parser reads one (`sp8_bl43.py` has no date handling).

*My recommendation:*
- The layout is stored as a named, dated entry in the BL43 format card. The card is the file that already holds the analyzer list and is read by every BL43 workspace. The entry is something like *BL43LXU, 4 × 7, in use from <date>*.
- The dock's header names the layout the grid was drawn with.
- Exactly one entry exists now. Logic to choose between entries is built when a second layout that actually moves an analyzer appears, because only then is there something to choose and a known way to choose it (a date, or an experiment setting).
- *What this commits to:* a place and a format for versions, and honesty about which layout was used, but no version-picking code.
- *What I rejected:*
  - **A per-experiment layout setting in the GUI now:** a control with one possible value.
  - **Guessing the version from which analyzer files are present:** a dead analyzer would silently switch layouts.

**Where I want the push:** do you know of BL43 data where an analyzer number sat in a *different position*, not just a missing column? That is the case that would make a version selector needed now rather than later.

## User

The red line was a paper schematic thing, we don't need it. I just was X/Y axes outside the grid
Q9: Let me return the question. Let's say an instrument has a new analyzer setup. We load data, but it's not clear which analyzer setup it's using. We can try checking dates and such, ask the user, etc. I don't know of anytime the analyzer numbers changed, but it's not something we can assume. We don't need heavy versioning right now, but we need to be ready when it becomes an issue so we can say "these scans were made with analyzer setup A-123 and these ones with A-456" and have everything work.

## Assistant

> **D9 · Ruling (operator, 2026-10-30):** no scattering-plane line. The in-plane and out-of-plane labels sit outside the grid as its X and Y axes.

Fair to hand Q9 back; here is what "ready" means concretely. The key property is one that ISAR already almost has.

**Identity never depends on the layout.**
- Every spectrum, fit, anchor and lock is keyed by scan and *instrument* analyzer name, e.g. `exp1:13:a03` (`analysis_service.py:559`).
- The internal names (A1…D7) and grid positions are labels the layout *derives*. They are never stored against data.
- So when you later say "scans 40–60 were setup A-456", every cell is re-labelled and re-placed, and no fit, anchor or lock moves or needs migrating.
- That is what makes later versioning cheap, and it's free as long as nothing ever stores "A1" as a key.

**What this job builds:**
1. **A layout entry with a stable ID** (say `BL43LXU-7x4`) in the BL43 format card. It holds the internal-name ↔ instrument-name table and the grid positions. More entries are just more blocks in the card.
2. **One function that answers "which layout does this scan use?"** It sits in the read layer and returns the layout ID *and how it was chosen*. Today the only answer is "the instrument's only layout". Every consumer asks through it, so a later rule changes one place.
3. **The dock's header shows both**, e.g. "Layout BL43LXU-7x4 (the only BL43 layout)".

**What waits until a second layout exists:**
- **A per-scan assignment the user makes in the GUI** ("these scans: A-456"). It would be stored like the other values you enter (temperature, dead analyzers), in ISAR's user-value store, never in the card (hard rule 1). The resolver's order would then be: your assignment, then a date rule where files carry dates, then the only layout, else **"layout unknown — choose one"**. The grid never guesses.
- *Why not now:* a control and stored records whose only possible value is the one layout. It would be about a day's work and tests for nothing selectable.

**Out of scope (my call; say if you disagree):** showing internal names anywhere other than this dock, for example in the scan list.

**Where I want the push:** do you adopt this split, building the ID, the resolver and the header now and the assignment when needed? The part I'm least sure of is whether you want to see the "unknown layout" state exercised now, with a test layout, so it's proven before the day it matters.

## User

This is fine.

## Assistant

> **D10 · Ruling (operator, 2026-10-30): layout versioning.**
> - **Built now:** a layout entry with a stable ID in the BL43 format card; one read-layer function that answers which layout a scan uses and how it was chosen; and the dock header, which names both.
> - **Identity stays the instrument analyzer name.** Internal names and grid positions are derived labels, never keys.
> - **Deferred until a second layout exists:** the per-scan user assignment, and the order the resolver tries things in (your assignment, then a date rule, then the only layout, else "unknown — choose").
>
> No test layout exercises the "unknown" state now (my reading: you didn't take that up).

That settles every behaviour you raised. Here's the shape before I write it up.

**Easy:**
- The dock and its View-menu entry.
- Shared selection with the scan list, which already broadcasts the spectrum keys it selects.
- Cell plots: the main plot's Normalised scaling and fit-component data already exist to reuse.
- The hkl labels and the Δhkl axes, taken from the analyzer hkl ISAR already accepts.
- The layout entry and resolver.

**Harder:**
- **Live per-cell batch states.** The batch runner tracks each spectrum's state internally but sends only counts. It needs a small addition to its progress messages, and the dock needs to follow those messages without lagging the window.
- **Drawing 28 small plots quickly enough** that clicking through scans feels immediate. My target is under half a second per scan switch. That's an estimate to measure, not a requirement you set.

**My defaults, to be written into the design as mine:**
- The dock floats on first open, then remembers wherever you put it.
- With several scans selected, the grid shows the scan of the scan list's current row. The header says "Showing scan N — 3 scans selected, showing one".
- The header gives scan, sample, temperature, nominal hkl and the layout.
- Cells redraw when a fit changes outside a batch too, for example after Make authoritative.

**Out of scope:**
- Actions in the grid (anchor, lock, refit) beyond selection.
- Other instruments' layouts, including the S30 row.
- The version-assignment control.
- Internal names anywhere outside this dock.

**Next step — your call on the tier.** I'd recommend **T1**: a managed job with planning, review seats and a pull request. This touches the GUI, the read layer, the format card and the batch runner's progress messages, which is more than a jam should carry. Once you name the tier I'll open the job and write the design as a self-contained file carrying D1–D10. It then goes through its design checks before you see it for `/approve`. Nothing is built before that.

If you'd like a transcript of this discussion kept, I can run `/transcribe` now.

## User

/transcribe

## Assistant

Transcribing this session, then opening the T1 job.
