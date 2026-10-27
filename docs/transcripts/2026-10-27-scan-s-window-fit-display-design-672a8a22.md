# Scan(s) window fit display design

**Date:** 2026-10-27  
**Session:** 672a8a22-c249-4af6-a91d-456b4582a20e

*Cleaned transcript: tool calls, tool results, thinking and harness notices removed. Nothing else edited.*

## User

Alright, we are designing how a user can see the result of analysis. Currently, we just have a "scan window" that shows the I vs. E plot, then a waterfall to show multiple scans together, then a dispersion plot.

Ok, let's look at PUFFIN's GUI and how it displays scans and fits, as I think this will be more useable. My goal is to show the actual data in an easy-to-view format (which PUFFIN does basically), and then we add the fits at the user's request. I am thinking that there is a toggle button in the top-left that the user can select to display fast and deep fits, or none, with the legend kept in the top-right.  That covers the Scan window quite well.

Now, for waterfall, it's basically the same, although each scan will show its own fits then with the offsets. I found a bug here where I selected one scan and it didn't appear in the waterfall; I should be able to select multiple scans to bring them into the waterfall plot, or also the option to turn off offsets. So "waterfall" becomes "multiple scan comparison." Actually, I'm not sure we have much use for waterfall anymore; the base option should just be in "Scan(s)" to select multiple scans and have them displayed with offset or overplotted (different symbols then).

The final part, the dispersion, would be when selecting multiple scans to show E vs. q; this will be a bit complicated if we don't know the analyzer q positions, so it's less of an issue, and we don't track which phonons are which. Ideally, the analysis would let us connect a phonon in one analyzer to the same phonon in another, but this comes later. For now, let's focus on the central "Scan(s)" window to show results at a glance.

/dialogue 
/ui-professional

## Assistant

Reading both GUIs before I ask you anything: PUFFIN's plot view and ISAR's current scan, waterfall and dispersion panes. I'm sending two parallel reads so neither repo fills this conversation.

Still reading. There was a GUI redesign session earlier today for the le Tacon file handling, so I'm checking what of it actually landed before I build on it.

Nearly there: one feasibility check left — whether the deep pass stores parameters ISAR can actually draw as a curve, or only a status word.

I've read both GUIs. A few things change the picture, one of them significantly.

**What I found**

- **PUFFIN is simpler than you might remember.** It shows one dataset at a time, picked from a single-selection list. It has no overplot, no offsets and no fit toggle, and its legend sits top-left. What makes it easy to read is that one big plot owns the centre: filled markers with error bars, the total fit as a line, components in colour, and the parameter form in a side panel. So what we can borrow is the uncluttered plot, not the multi-scan handling.
- **ISAR's Scan page is crowded.** The plot shares the page with a residuals strip and four side tables (interval ladder, model candidates, verdict, provenance). The design notes measured the plot at about 86 px wide on a laptop with both side panels open.
- **The waterfall behaviour is a design gap, not a small bug.** The waterfall never looks at the scan list. It picks the q-bucket holding the most scans, shows that bucket's temperatures, and silently skips any scan with no temperature. Your scan appeared only if it happened to be in that bucket.
- **Deep fits can be drawn.** The deep pass stores the chosen model's parameters, so the existing curve evaluator can draw them. It's still labelled "untrusted" for now.
- **The overlap.** The paused le Tacon fitting job, whose GUI part isn't built yet, plans two centre-panel pieces:
  - an **analyzer overview**: a 7×4 grid with one tile per analyzer, small spectra with fits;
  - an **analyzer inspector**: one analyzer at full size with data, fit, components, residuals and the table of fitted modes. That is very close to your Scan(s) window.
- **Goals check.** This fits your goals file: "fits are the human-readability layer", and data shown without fits matches the reduce-only mode.

**Size:** a small region, but with that overlap. I also ran a cold reading past the five-model panel. It agreed on the open items below, and there are no surprises beyond them.

**Still to settle, one at a time:**
- whether the fit toggle is exclusive (Fast *or* Deep) or can show both at once;
- whether components and residuals come with "show fit";
- what happens to the side tables;
- how scans get selected;
- raw counts or monitor-normalised intensity when overplotting.

---

**First question: how does Scan(s) relate to the fitting job's planned overview and inspector?** It decides whether this is a new job or a re-plan of that job's GUI part, and which job owns the centre plot.

**My recommendation:**
- Scan(s) becomes the one full-size spectrum view. It replaces today's Scan and Waterfall pages, and it also replaces the fitting job's planned analyzer inspector, so that job no longer builds one.
- The overview tiles stay with the fitting job as the survey screen. That's the "all 28 analyzers at 300 K" case, which picking scans one by one handles badly. Clicking a tile brings that analyzer's spectra into Scan(s).

**What this commits to:**
- The fitting job's plan changes. Its inspector piece goes, and naming peaks (calling a mode "A") would happen inside Scan(s).
- The two jobs share the centre, so whichever builds first sets the plot the other builds on.

**What I rejected:**
- *Building Scan(s) and letting the fitting job also build its inspector.* That gives two full-size spectrum views that will drift apart.
- *Folding Scan(s) into the fitting job.* One owner, but it ties this work to a paused job's schedule.

**Where I'd like your push:** I'm least sure whether the overview tiles still earn their place once Scan(s) can overplot freely. You may prefer Scan(s) alone, with a quick "select all at this temperature" in place of the grid.

## User

PUFFIN is certainly simpler but shows the proper method, and is more useful than ISAR's view at this point. We take it as our starting point/inspiration.

Q1: I don't know what fitting job you're talking about. Our two other jobs are first, the scan intake GUI LHS (finishing soonish) and second the analysis GUI RHS (getting designed). Any other job is stale (j313eac923389? It's old).

## Assistant

*Withdrawn: how Scan(s) relates to the le Tacon fitting job's planned overview and inspector, because that job (`j313eac923389`) is stale.* I'll ignore its plans. It's still on the board as active, with five slices pending, so you may want to cancel it; that's yours to do, and I won't touch it.

**Decision: PUFFIN's display is the model** (2026-10-27)
- **Ruling:** "PUFFIN is certainly simpler but shows the proper method, and is more useful than ISAR's view at this point. We take it as our starting point/inspiration."
- **Ruled by:** operator
- **Constrains:** Scan(s) starts from one large plot that owns the centre: data markers with error bars, fits drawn over them, legend on the plot. Anything else on the page has to earn its space against the plot.

---

**Next question: how do scans get into the Scan(s) plot?** It decides the fix for the waterfall complaint, and it touches the right-hand analysis panel being designed in the other session.

**The situation:**
- The scan list on the left, from the intake job that's finishing, is a tree. A scan-set row (one scan number) has its analyzer spectra beneath it. Today it allows only one selection, and a click sends that one spectrum to the Scan page.
- The waterfall ignores the scan list completely, which is why your scan didn't appear.
- The right-hand panel's design says "scans selected in the left scan list are added to a queue for fast or deep pass". So that panel will read the same selection.

**My recommendation:** the plot shows exactly what is selected in the scan list, nothing more and nothing hidden.
- Ctrl-click and Shift-click select several spectra. Clicking a scan-set row selects all its analyzers, so one click on a scan number gives the whole 28-analyzer stack.
- One selected spectrum gives the single-scan view; several give the comparison.
- A selected spectrum that can't be drawn isn't skipped silently. The plot says how many of the selected spectra it couldn't draw, and why.
- One selection, two readers. The centre draws it; the right panel queues it only when you press its add button. Looking at scans never queues them.
- The Waterfall page goes, as you suggested, along with its automatic grouping by Q and temperature.

**What this commits to:**
- Clicking somewhere else replaces the comparison. The plot follows your clicks, so a set you've built up is lost when you click away.
- A scan-set click can put 28 spectra on screen. With offsets that's a tall stack, which I'd rather show than cap.

**What I rejected:**
- *A separate "in plot" checkbox per row.* It survives clicking around, but it's a second kind of selection beside the highlight, and two disagreeing selections are exactly what caused the waterfall confusion.
- *Keeping the automatic Q/temperature grouping as a mode.* The scan-set click covers most of what it did, and it's the rule that surprised you.

**Where I want your push:** your normal comparison habit. Do you pick a handful, look, and move on, which the follow-your-clicks rule serves? Or do you build a comparison set and keep it while you click through other scans? The second needs a way to pin a set, and that changes the design.

## User

Clicking a single scan with 28 analyzers will not be useful, so let's drop that clicking a scan-set row selects all the analyzers.

Otherwise this looks fine. I think a way to pin a set of scans would be useful. Let's take a page from Starcraft: CTRL+# creates a group (also with a GUI), clicking that group or number brings it up (only for the central plot), then users can add/remove from a group and resave the group. Sound reasonable? That may be fairly useful.

## Assistant

Yes, it's reasonable. It gives the expert fast keys and the newcomer a visible button, and ISAR already has most of the storage for it. I have one real concern, and it's the next question.

**Decision: the plot shows exactly the scan-list selection, and a scan-set click selects nothing extra** (2026-10-27)
- **Question, as put:** the plot shows exactly what is selected in the scan list, multi-select by Ctrl/Shift-click, clicking a scan-set row selects all its analyzers, the Waterfall page and its automatic Q/temperature grouping go.
- **Ruling:** "Clicking a single scan with 28 analyzers will not be useful, so let's drop that clicking a scan-set row selects all the analyzers. Otherwise this looks fine."
- **Ruled by:** operator
- **Constrains:**
  - Selecting several spectra is by Ctrl-click and Shift-click on analyzer rows. A scan-set row still only expands and collapses.
  - Spectra that can't be drawn are counted and explained on the plot, never dropped silently.
  - The right-hand panel queues the selection only on its own add action.
  - The Waterfall page is removed.

**Decision: pinned sets work like StarCraft control groups** (2026-10-27)
- **Ruling:** "I think a way to pin a set of scans would be useful. Let's take a page from Starcraft: CTRL+# creates a group (also with a GUI), clicking that group or number brings it up (only for the central plot), then users can add/remove from a group and resave the group."
- **Ruled by:** operator
- **Constrains:**
  - Ctrl+1…9 saves the current selection as group N.
  - Pressing N, or clicking its button, brings the group up in the centre plot.
  - You edit what's shown, then Ctrl+N saves it again.
  - Groups are a viewing tool and never queue analysis.

**How I'd fill in the details** (my defaults; overrule any of them):
- **Storage.** Groups belong to the experiment and survive a restart. ISAR already keeps named scan groups per experiment, built for the old Scans panel, by stable spectrum key. Numbered groups reuse that store with a slot number, so there's no new file. A member that disappears shows as "4 of 5 present", never as a silently shorter group.
- **The buttons** sit in a strip on the plot's header row, beside the fit toggle. Each chip shows the number and its count ("3 · 5 spectra"), the tooltip lists the members, and a "+" saves the current selection to the next free number.
- **Edited state.** Once you've changed what's shown after bringing up group 3, its chip says *edited* until you press Ctrl+3. Nothing changes the saved group behind your back.
- **Keys and typing.** The bare digit keys work only when no text field has focus, so typing "3" into a number box in the Processing panel never switches the plot. Ctrl+digit and the chips always work.
- **Overwrite.** StarCraft replaces a group without asking. For a colleague who hits Ctrl+3 by accident, the strip shows "Group 3 replaced · Undo" for a few seconds, with no confirm dialog. It's a small cost; cut it if you'd rather.

---

**Next question: when you bring up a group, does the scan list's highlight move to it?** It decides whether the window ever holds two selections at once.

**The situation:** you said "only for the central plot". I read that as: a group is for looking, and it never feeds the analysis queue. That holds either way, because the right panel queues only when you press its button. What's open is what the scan list shows after you press 3.

**My recommendation: yes, the highlight moves.** Pressing 3 selects those five rows in the list, expands their scan sets and scrolls to the first one.
- The list and the plot always agree, so a Ctrl-click to add or remove a spectrum works on the group straight away. That's your "add/remove … and resave".
- The cost: whatever you had selected before is replaced. It isn't lost, though; it's one click or one group away.

**What I rejected:** leaving the list's highlight alone while the plot shows the group. Your previous selection survives, but the plot and the highlight then disagree, and editing the group would need its own add/remove controls. That's the two-selections problem behind the waterfall confusion.

**Where I want your push:** whether "only for the central plot" meant something stronger. For example, that bringing up a group should leave what the right panel sees untouched, even before you press its add button. If so, the right panel needs to keep its own selection, and I'd raise that with the session designing it.

## User

Yes, we never hold a highlight that doesn't match the window.

Now that I thought about it, we do need a way to send a whole scan to analysis, so we should have that clicking a scan line highlights all the scans (analyzers) in it. Let's ensure that clicking on the left part of the scan line just opens it without selecting them all; in fact, we can make a small designed area to allow selection of the full set versus opening it, or otherwise making that visually clear how they differ in UI/UX interaction.

## Assistant

**Decision: the highlight always matches the window** (2026-10-27)
- **Question, as put:** when you bring up a group, does the scan list's highlight move to it?
- **Ruling:** "Yes, we never hold a highlight that doesn't match the window."
- **Ruled by:** operator
- **Constrains:** bringing up a group selects its rows in the list, expands their scan sets and scrolls to them. There is only ever one selection in the window.

**Decision: a scan-set row selects its whole set, and a separate area only opens it** (2026-10-27)
- **Ruling:** "we do need a way to send a whole scan to analysis, so we should have that clicking a scan line highlights all the scans (analyzers) in it. Let's ensure that clicking on the left part of the scan line just opens it without selecting them all"
- **Ruled by:** operator
- **Why:** sending a whole scan to analysis needs one gesture.
- **Supersedes:** the entry above, "a scan-set click selects nothing extra", because of this need.
- **Constrains:** the scan-set row has two zones that look and behave differently. I've told the session designing the right panel, since it reads the same selection.

**How I'd make the two zones obvious** (my proposal; I'll mock it before anything is built):
- **Open zone.** A wide chevron gutter on the left, about the width of a fingertip rather than Qt's tiny arrow. On hover it gets its own shaded patch with an open-or-close cursor. Clicking it never changes the selection.
- **Select zone.** The rest of the row. On hover, the whole row tints the way a selection looks, and the row always carries "28 analyzers" in its text, so the effect is printed on the row, not hidden in a tooltip.
- **Partial selection is always visible.** A collapsed scan set shows a mark for *all*, *some* (with "3 of 28") or *none* of its analyzers selected. Otherwise a selection hidden inside a collapsed row is invisible, and the queue would take spectra you can't see.
- Ctrl-click on a scan-set row adds or removes the whole set, as on an analyzer row.

---

**Next question: what does the plot draw when a whole scan set is selected?** Your new ruling sends 28 spectra to the centre plot, and you said earlier that 28 at once "will not be useful" there. The two rulings meet here, and the rule "the plot shows exactly the selection" needs an answer for this case.

**My recommendation: draw all of them, but laid out for a check rather than a comparison.**
- Above a small count (I'd say 8), the plot switches to an offset stack ordered by analyzer number. Each trace gets a label at its right end (analyzer and Q) in place of the legend, which is useless at 28 entries.
- The overplot option is unavailable at that size, with the reason beside it ("too many spectra to tell apart by symbol"). There are only about 8 marker symbols you can tell apart.
- The fit toggle still works. At 28 traces it shows whether everything landed and fitted sanely, which is about the only question you'd ask of 28 at once.

**What this keeps:** the plot never disagrees with the highlight, which you just ruled on.

**What it costs:** a crowded plot whenever you select a set to queue it. Selecting for analysis and selecting for looking are the same act, so you can't queue a scan without also drawing it.

**What I rejected:** holding the plot back above a threshold with a note ("28 spectra selected; pick fewer to plot"). The plot stays clean, but it hides part of the selection, which is the waterfall problem again.

**Where I want your push:** whether a crowded plot every time you queue a set will annoy you in practice. If it will, the honest fix sits in the right panel ("add whole scan" acting on a scan number without selecting it), not in the plot hiding things.

## User

A tough item. I agree that trying to plot too many items at once would just break things, not only be unusable. First, whether to overplot or waterfall plot with offset is a toggle (right click? not sure). We keep the maximum number of plots in the center window to... 10, I think, which works ok if we e.g. cover a full Brillouin zone in 10 steps. Beyond that, it just doesn't display anything with a note to reduce the number of selected plots (it can maybe list the selected scans/analyzers, which could be useful for selecting large batches). No fitting either, it's not like 28 fits is really useful to display either.

## Assistant

A hard cap is the honest answer. Ten is also comfortable for symbols: ten spectra in overplot can each get a distinct colour and marker pair.

**Decision: the centre plot shows at most 10 spectra** (2026-10-27)
- **Question, as put:** what does the plot draw when a whole scan set is selected?
- **On the table:** draw everything, with an offset stack and direct labels past 8 (the plot never disagrees with the highlight, but it's crowded); hold the plot back above a threshold with a note (clean, but it hides part of the selection).
- **Ruling:** "We keep the maximum number of plots in the center window to... 10, I think, which works ok if we e.g. cover a full Brillouin zone in 10 steps. Beyond that, it just doesn't display anything with a note to reduce the number of selected plots … No fitting either, it's not like 28 fits is really useful to display either."
- **Ruled by:** operator
- **Why:** ten covers a Brillouin zone in ten steps, and more than that breaks the plot, not just its readability.
- **Constrains:**
  - Ten is a hard limit, not a target.
  - With 11 or more spectra selected, the centre draws no data and no fits, and says how many are selected and why nothing is drawn.
  - The highlight still shows everything selected, so the plot agrees with it by saying it's over the limit, never by drawing a subset.

**Decision: offset or overplot is a user toggle** (2026-10-27)
- **Ruling:** "whether to overplot or waterfall plot with offset is a toggle"
- **Ruled by:** operator
- **Constrains:** both layouts are available for any selection of 2 to 10 spectra. *Where the toggle sits is still open (below).*

**Where the toggle goes** (my proposal; you said "right click? not sure"): a visible two-option switch, *Offset | Overlay*, on the plot's header row beside the fit toggle, with right-click offering the same choice as a shortcut. A right-click menu as the only route works for you, but a colleague will never find it, and that's the one placement rule I'd hold firm on for a tool other people use. The switch is greyed out, with its reason, when only one spectrum is shown.

---

**Next question: is the list of selected spectra in or out?** You said the over-limit note "can maybe list the selected scans/analyzers". That's a maybe, so I've recorded it as a lean, not a ruling. It decides whether the centre has anything to show when you've selected a batch to queue.

**My recommendation: in, read-only, grouped by scan set.**
- The over-limit page shows the note, then one line per scan set, for example "Scan 12 · 300 K · 28 analyzers (a03–a41)". Each line expands to its analyzers with their fast and deep pass states.
- It's a checklist of what the right panel will queue, which is the moment you'd want one. That matters most when part of the selection sits inside a collapsed scan set.
- It edits nothing. Selection is changed only in the scan list, so the list can never become a second selection.

**What I rejected:** making the list clickable to narrow the selection. It's handy, but it puts a second selection-editing surface in the window, right after you ruled there's only ever one selection.

**Where I want your push:** whether the pass-state columns earn their place there, or whether a plain list of names is all you'd use at that moment.

## User

Ok, in and read only. Definitely no writing, just something to make it minimally useful.

## Assistant

**Decision: the over-limit page lists the selection, read only** (2026-10-27)
- **Question, as put:** is the list of selected spectra in or out?
- **Ruling:** "Ok, in and read only. Definitely no writing, just something to make it minimally useful."
- **Ruled by:** operator
- **Constrains:**
  - Over 10 spectra, the centre shows the note and one line per scan set (scan number, temperature, analyzer count and range), expandable to analyzer names.
  - Nothing on it edits anything.
  - From "minimally useful" I've dropped the fast and deep pass-state columns; say if you wanted them.

The *Offset | Overlay* switch on the plot's header row, with right-click as a shortcut, stands as my default since you didn't object. It's marked as mine in the design, not yours.

---

**Next question: what states does the fit toggle have?** Your words were "display fast and deep fits, or none". That reads either as three exclusive states, or as fast and deep switched on and off independently, which adds "both". It decides whether you can see the two passes disagree on one plot.

**The situation:**
- The fast pass is the quick fit ISAR runs on everything. The deep pass is the slow nested-sampling refit with better uncertainties; ISAR still labels it "untrusted", and most spectra won't have one.
- Either pass can be missing for a given spectrum: not run, failed, or a raw file in reduce-only use.

**My recommendation: two independent buttons, *Fast* and *Deep*, in the top-left, both off by default.**
- Off/off is your "none", and on/on puts both on the plot. While the deep pass is untrusted, seeing it next to the fast fit is the check you'd actually make.
- Each spectrum's fits take its own data colour: fast as a solid line, deep as a dashed one. The legend in the top-right names them per spectrum.
- A missing fit draws nothing, and the legend says so on that spectrum's line ("a14 300 K · deep: not run"). A missing fit never shows as a flat line or as the other pass standing in.
- Both buttons remember their last setting, so turning fits on once keeps them on as you click through scans.

**What this commits to:** with 10 spectra and both passes on, that's 20 curves. The user asked for it, so I wouldn't stop it.

**What I rejected:** a three-way *None | Fast | Deep* switch. It's simpler to read, but you can never see the two passes on one plot, only flick between them and compare from memory.

**Where I want your push:** the default. Off honours "data first, fits on request", but a colleague who doesn't notice the buttons may conclude ISAR fitted nothing. The alternative is Fast on by default, with Deep off.

## User

I think this is good, and will help users see if another peak comes in from deep vs. fast. Off by default is fine. I'd keep it as an ISAR toggle, not a per-view toggle, as you said.

## Assistant

**Decision: Fast and Deep are two independent buttons, off by default, one setting for the whole of ISAR** (2026-10-27)
- **Question, as put:** two independent buttons, *Fast* and *Deep*, in the top-left, both off by default, or a three-way *None | Fast | Deep* switch?
- **Ruling:** "I think this is good, and will help users see if another peak comes in from deep vs. fast. Off by default is fine. I'd keep it as an ISAR toggle, not a per-view toggle, as you said."
- **Ruled by:** operator
- **Why:** showing both passes together shows where the deep pass finds a peak the fast pass missed.
- **Constrains:**
  - The two buttons are one window-wide setting. It doesn't change with the selection, a group or the layout, and it survives a restart (the restart part is my default).
  - Fast draws solid and Deep dashed, each in its spectrum's own colour.
  - A missing pass draws nothing and the legend says so on that spectrum's line.

---

**Next question: what does a shown fit draw besides its total curve?** Your reason for the ruling, seeing whether deep finds another peak, makes this matter: a second peak under a shoulder often barely changes the total curve, and the components are what show it.

**My recommendation: the full picture for one spectrum, totals only for several.**
- **One spectrum, a fit switched on:**
  - the total curve;
  - each component as a thin line in the same style as its pass (solid for fast, dashed for deep), with the background dotted;
  - a short residuals strip under the plot, sharing its energy axis.
  
  The residual is today's: data minus fit, in counts, with the data's own error bars. A zero-count point keeps its zero-size error bar, under ISAR's no-floor rule. With both passes on, the strip shows two residual series, filled markers for fast and hollow for deep.
- **Two to ten spectra:** total curves only, with no components and no residuals strip. Ten spectra × two passes × three or four components is around 70 lines, and nothing could be read.
- The legend carries the component names for a single spectrum ("fast · DHO 1, DHO 2, elastic").

**What this commits to:** comparing a peak count across a Brillouin-zone series means looking at each spectrum alone; the series view shows only the totals.

**What I rejected:**
- *Components in offset mode too.* The stack separates them a bit, but at 10 traces with both passes it's still around 70 lines.
- *A separate Components button.* It's one more control for a choice that follows from the spectrum count anyway.

**Where I want your push:** offset mode with just a few spectra, say 3 to 5. Do you want components there? That's exactly where "did a peak come in across this series" gets asked. If so, I'd draw components in offset mode up to some small count, and that count would be yours to set.

## User

There is no "the" total curve for a spectra with deep and fast; we use deep if it's on, fast if it's the only one selected. For the two to ten spectra, we do draw components (it can be **critical** to see how one phonon in the set moves!), again preferring deep before fast.

## User

For overplotting (no offset) we draw total curves if those are selected, in waterfall (offset) it would be fine to do components as well. We can double up on the legend, so total and phonon 1...N share lines across spectra, only symbols defer.

## Assistant

You interrupted before my last reply printed, so here are both of your last two answers, recorded together.

**Decision: one pass supplies the components, deep before fast** (2026-10-27)
- **Ruling:** "There is no "the" total curve for a spectra with deep and fast; we use deep if it's on, fast if it's the only one selected."
- **Ruled by:** operator
- **My reading, correct me if it's wrong:**
  - With both buttons on, both total curves are drawn, so you can see the passes disagree.
  - The components and the single-spectrum residuals come from one pass per spectrum: deep when *Deep* is on, fast when only *Fast* is on.
  - If *Deep* is on but a spectrum has no deep pass, that spectrum falls back to its fast components, and its legend line says so.

**Decision: components in the offset view, totals only when overlaid** (2026-10-27)
- **Ruling:** "For the two to ten spectra, we do draw components (it can be **critical** to see how one phonon in the set moves!), again preferring deep before fast." Refined to: "For overplotting (no offset) we draw total curves if those are selected, in waterfall (offset) it would be fine to do components as well."
- **Ruled by:** operator
- **Why:** following one phonon across a series is the main point of the comparison.
- **Supersedes:** my proposal of totals only for several spectra, which you rejected before it printed.
- **Constrains:**
  - One spectrum: totals, components and residuals.
  - Offset view, 2–10 spectra: totals and components.
  - Overlay view: totals only.

**Decision: curve roles share a style across spectra, and symbols tell the spectra apart** (2026-10-27)
- **Ruling:** "We can double up on the legend, so total and phonon 1...N share lines across spectra, only symbols defer."
- **Ruled by:** operator
- **Constrains:** the legend has two parts. One lists the spectra by data symbol. The other lists the curve roles (total, phonon 1 … N), each with one line style used on every spectrum.

---

I have two objections to that last scheme. The first is the next question; the second follows it.

**Next question: what does "phonon 2" mean when two spectra have different counts?** The fit has no idea which peak is which phonon. All it can do is number a spectrum's modes, most likely by energy. Suppose the deep pass finds a third peak at a14 between the two it found at a13. Then a14's "phonon 2" is a different excitation from a13's, yet they share a colour. That's the exact moment you're looking for, a new peak coming in, and the colours would point you the wrong way. Real matching of a peak across spectra is the later feature you mentioned in your first message.

**My recommendation: number by energy, and say so on screen.**
- Call them *Mode 1 … N*, numbered by energy within each spectrum. The legend's role section carries one line: "numbered by energy within each spectrum, not matched across spectra".
- The elastic line and the background keep fixed roles of their own and are never numbered.
- When the spectra on screen don't all have the same number of modes, each odd one's legend line says so ("a14 · 3 modes, others 2"). Then you know the colours shift there, and that's also your "a peak came in" signal.

**What I rejected:**
- *All modes in one neutral colour.* It's honest, but it loses the moving-phonon picture you called critical.
- *Matching modes across spectra by nearest energy.* That's the deferred tracking feature, and a wrong match drawn confidently is worse than numbering it admits to.

**The second objection, next unless you settle it now:** in the overlay view only totals are drawn. If every total has the same line and only the data symbols differ, ten crossing totals can't be tied to their data. Overlay may need each total to take its spectrum's colour.

## User

Hm, in a single spectra the total curves are going to barely move between deep and fast I expect, right? So I had meant we show only the deep if (deep AND fast are on) OR (deep is on), if fast is on we show that.

Number by energy for now; it's actual work we want to do later to track phonons across scans and consistently label them, but by energy is the fallback.

We don't adds this legend note, that could get horrific if peaks start differing. Each spectra just simply numbers its peaks outwards in energy from 0 in order, and we let the operator read the honest legend *per spectrum* without drawing conclusions between spectra.

## Assistant

Yes, mostly. Both passes fit the same counts, so where the data are good the two totals sit almost on top of each other, and the real difference is in how the peaks are split up. They can part visibly in two places: where counts are low, and at a shoulder where the deep pass chose a different number of peaks. Even there, the components show it better than the totals do. So your rule loses very little.

**Decision: one pass drawn at a time, deep winning** (2026-10-27)
- **Ruling:** "we show only the deep if (deep AND fast are on) OR (deep is on), if fast is on we show that."
- **Ruled by:** operator
- **Supersedes:** "both on puts both on the plot" from the two-buttons decision, and my reading of the components decision, because the totals barely differ and the comparison lives in the components.
- **Constrains:**
  - Each spectrum draws a single pass: its totals, components and residuals all come from it.
  - My reading of your earlier "preferring deep before fast": when deep is chosen but a spectrum has no deep pass, that spectrum shows its fast fit, and its legend line says "fast (no deep pass)". Say if you'd rather it draw nothing.

**A consequence for the control** (my recommendation; overrule if you like): with that rule, two independent buttons have a dead state. Pressing *Fast* while *Deep* is on changes nothing on screen, and a colleague will think it's broken. A three-way switch, *None | Fast | Deep*, in the top-left, does exactly what you ruled with no dead state. It also matches your first description, "display fast and deep fits, or none". Off by default and one setting for the whole of ISAR stay as you ruled.

**Decision: modes numbered by energy within each spectrum, with no cross-spectrum notes** (2026-10-27)
- **Ruling:** "Number by energy for now; it's actual work we want to do later to track phonons across scans and consistently label them, but by energy is the fallback. We don't adds this legend note, that could get horrific if peaks start differing. Each spectra just simply numbers its peaks outwards in energy from 0 in order, and we let the operator read the honest legend *per spectrum* without drawing conclusions between spectra."
- **Ruled by:** operator
- **Constrains:**
  - Mode 1 is the mode closest to zero energy, and the numbers count outward from there. I read "from 0" as counting outward from zero energy, with the elastic line and the background named rather than numbered; say if you meant the elastic line as mode 0.
  - The legend makes no comparison between spectra, so both notes I proposed are gone.
  - Matching phonons across spectra is later work.

---

**Next question: in the overlay view, how is each total curve tied to its own data?** Overlay draws totals only. Under your shared-styles rule every total uses the same line, and only the data symbols differ. Ten curves crossing near a peak then can't be traced back to their data.

**My recommendation: colour means the spectrum in overlay, and the curve role in offset.**
- **Overlay:** each spectrum has its own colour as well as its own symbol, and its total curve takes that colour. The legend lists the spectra.
- **Offset:** your scheme exactly. Data markers are neutral and differ by symbol, each trace is labelled at its end, and Total, Mode 1, Mode 2 … share one colour across every trace, so a mode's colour can be followed up the stack.
- The legend heading says which it is ("Colour: spectrum" or "Colour: curve").

**What this commits to:** switching views changes what colour means. I don't like that in general, but within each view it's consistent and labelled.

**What I rejected:**
- *Overlay totals in one shared style.* It's consistent, but you can't tell the curves apart.
- *Colour by spectrum in offset too.* Then mode colours can't be shared, and that sharing is your moving-phonon picture.

**Where I want your push:** whether overlay should draw the totals at all once fits are on, or whether its job is purely to put the data on top of each other.

## User

In overlay (overplotting, no offset) let's drop showing the curves at all, since it's going to be very confusing; it's a raw-data comparison tool only.

## Assistant

**Decision: overlay is a raw-data comparison only** (2026-10-27)
- **Question, as put:** in the overlay view, how is each total curve tied to its own data?
- **Ruling:** "In overlay (overplotting, no offset) let's drop showing the curves at all, since it's going to be very confusing; it's a raw-data comparison tool only."
- **Ruled by:** operator
- **Supersedes:** "components in the offset view, totals only when overlaid" for the overlay half.
- **Constrains:**
  - Overlay draws data only, each spectrum with its own colour and symbol.
  - In overlay the fit switch is greyed out, with the reason beside it ("Overlay compares raw data; fits show in Offset or with one spectrum"). The setting itself is kept for when you switch back.
  - Offset and single-spectrum views keep the fit rules you ruled.

The three-way *None | Fast | Deep* switch and the "fast (no deep pass)" fallback stand as my defaults, since you didn't object.

**A default I'm setting** (it follows from ISAR's own energy rule): the spectra on one plot must share an energy axis. A spectrum whose energy zero isn't established yet can't be drawn on a calibrated energy axis next to calibrated ones, so it's counted among the "not drawn" spectra, with "energy zero not yet known" as its reason. It is never plotted with zero assumed.

---

**Next question: is the comparison in raw counts or normalised to the monitor?** "Raw-data comparison" can mean un-fitted or un-normalised. It matters because two spectra counted for different times differ in height for no physical reason, and a raw-count overlay invites reading that as an intensity change.

**The statistics, briefly:**
- Dividing a spectrum's counts by its monitor count M scales each point and its error bar by the same 1/M, so the relative error √N/N of each point is unchanged. A zero-count point stays at zero with a zero error bar, under ISAR's no-floor rule.
- Normalising affects only the display. The fits stay on raw counts, where the Baker–Cousins likelihood (the fit's measure of goodness, valid only on raw counts) holds, and a drawn fit curve is scaled by the same 1/M.
- One caveat: the monitor count has its own Poisson error. Typically it's large enough to ignore, so I'd ignore it, and say so in the axis tooltip.

**My recommendation: a *Counts | Per monitor* switch on the plot's header row, one setting for the whole window like the fit switch, defaulting to *Per monitor*.**
- The y-axis label always names its unit: "Counts" or "Counts per 10⁶ monitor".
- The reference is fixed for the experiment, so the numbers don't jump when the selection changes. ISAR's plane view uses the median of what's shown, which shifts as you click.
- A spectrum with no monitor recorded isn't drawn in *Per monitor*, and it's listed with that reason, never treated as M = 1.

**What I rejected:**
- *Raw counts only.* It's simplest, but every comparison across counting times misleads.
- *Normalising automatically whenever two or more spectra are shown.* The units would change under you as the selection grows.

**Assumption to check before building:** that every BL43 spectrum carries a monitor count. If it doesn't, the planner comes back to you before building *Per monitor*.

**Where I want your push:** the default. *Per monitor* is right for comparisons, but on a single spectrum you may prefer to read raw counts, to judge the Poisson noise by eye.

## User

A switch is right. So raw vs. normalized.

## Assistant

**Decision: a Raw | Normalised switch** (2026-10-27)
- **Question, as put:** a *Counts | Per monitor* switch on the plot's header row, one setting for the whole window, defaulting to *Per monitor*?
- **Ruling:** "A switch is right. So raw vs. normalized."
- **Ruled by:** operator
- **Constrains:**
  - The switch reads *Raw | Normalised*, sits on the plot's header row, and is one setting for the whole window.
  - The y-axis label always names its unit.
  - The reference monitor is fixed per experiment.
  - A spectrum with no monitor isn't drawn under *Normalised*, and it's listed with that reason.
  - Defaulting to *Normalised* is my choice, since you didn't take up that point. Flip it if you'd rather start on *Raw*.

---

**Next question: where do the fit numbers go?** It's the last big piece of the page. Today's Scan page gives most of its width to four side tables: the interval ladder, the model candidates, the verdict and the provenance. PUFFIN's lesson is that the plot owns the centre and the numbers sit in a side panel.

**My recommendation: the centre is the plot and nothing else; the numbers go into the scan inspector.** The scan inspector is the panel that slides out beside the scan list when you open a spectrum, built by the intake job.
- The inspector gains a *Fit* section for one spectrum. It shows the fast and deep pass side by side, each with:
  - the number of modes it chose and its state;
  - each mode's energy, linewidth and amplitude, with uncertainties.
  
  That follows the earlier ruling that both passes are shown side by side and labelled, with neither picked as "the" result. It's independent of the plot's *None | Fast | Deep* switch: numbers don't clutter the way curves do.
- The four existing tables move below it, collapsed by default, unchanged in content.
- **Which spectrum it shows when several are selected:** the one you clicked last. The inspector's header names it ("a14 · 300 K · 1 of 5 selected"), and its entry in the plot's legend is bold, so you can always tell which one the numbers belong to.
- With the inspector closed, the centre is pure plot. Opening it is the existing double-click or *Inspect*.

**What this commits to:**
- On a laptop, reading the numbers costs plot width. The existing rule already collapses the Processing panel when the inspector opens, to protect the plot.
- The inspector's content changes after the intake job lands, so this job builds on that job's finished panel and can't run alongside it.

**What I rejected:**
- *A results strip under the plot.* It takes height from the plot, and the residuals already sit there.
- *Keeping the side tables in the centre behind a "Details" toggle.* Opening it squeezes the plot again, which is the problem PUFFIN avoids.
- *Putting the numbers in the right-hand panel.* That's the analysis queue being designed in the other session.

**Where I want your push:** the "last clicked" rule. The alternative is that the inspector refuses to show numbers when more than one spectrum is selected. That's stricter, but then you can't read one spectrum's modes while comparing it against others.

## User

Agreed, keep fit numbers out of the center plot. Last clicked is fine, we clearly title the scan inspector so we know what info is there. I would rather keep the other fields up top, and replace the small plot below (which is being overtaken by the central plot anyway), the fits go below the metadata so we are never confused about which scan is selected.

## Assistant

**Decision: fit numbers live in the scan inspector, under its metadata** (2026-10-27)
- **Question, as put:** the centre is plot only; the numbers go into the scan inspector for the last-clicked spectrum, with the four existing tables below, collapsed.
- **Ruling:** "Agreed, keep fit numbers out of the center plot. Last clicked is fine, we clearly title the scan inspector so we know what info is there. I would rather keep the other fields up top, and replace the small plot below (which is being overtaken by the central plot anyway), the fits go below the metadata so we are never confused about which scan is selected."
- **Ruled by:** operator
- **Constrains:**
  - The inspector's title names its spectrum.
  - Its metadata stays at the top, and the inspector's small counts plot is removed.
  - The *Fit* section (fast and deep side by side) takes the plot's place, with the four detail tables collapsed below it.

That settles every question with real consequences that I've found. Below is everything that isn't yours: my defaults, marked as mine, followed by a summary of where we've landed.

**My defaults** (object to any one of them):
1. **Trace order in the offset stack** follows the scan list's own order, so sorting the list by analyzer or by temperature re-orders the stack. What you see in the list is the arrangement.
2. **Offsets** keep the waterfall's existing rule: automatic at 1.2 × the tallest trace, with a number box to set it by hand.
3. **Live updates.** When a fit lands or new data arrives for a spectrum on screen, it redraws in place, keeping zoom and selection.
4. **The other pages are untouched.** That's the plane, the dispersion, the sandbox, and the old hidden panels with their named groups. Only the Waterfall page goes.
5. **The two-zone scan-set row, and the whole Scan(s) page, get a mockup** at laptop size before anything is built, for you to react to.

**Where we've landed:**
- **Easy:**
  - the three header switches (*None | Fast | Deep*, *Offset | Overlay*, *Raw | Normalised*);
  - the 10-spectrum cap and its read-only list;
  - moving the tables into the inspector;
  - removing the Waterfall page.
- **Harder:**
  - the scan list's multi-select, with the two-zone row and the partial-selection marks. That's a custom row widget, built into a list the intake job has only just finished.
  - the control groups' keyboard rules;
  - drawing the deep pass's curves and components. It stores the parameters, but the curve evaluator today draws only the fast fit.
- **Assumptions the planner checks before building on them:**
  - that every BL43 spectrum carries a monitor count;
  - that the deep pass's stored parameters can drive the existing curve evaluator.
- **Deferred:**
  - the dispersion view;
  - matching phonons across spectra, which mode numbering by energy stands in for meanwhile.
- **Sequencing:** this builds after the intake job lands, because it changes that job's scan list and inspector. The session designing the right-hand panel knows the selection is shared.

---

**Last question: may I open the job and write the design?** This is past what an informal session should carry. It changes three regions of the window and a just-landed widget, and its design is a document, not a paragraph.

I recommend **T1**, the standard managed job: a design file checked twice before you see it, then a plan, a build and reviews. Once you name the tier, I'll open it, write the design for a reader who wasn't here, and mock the page. Then I'll present it for your `/approve`. Nothing gets built before that.

If this conversation is worth keeping, `/transcribe` will save it alongside the design.

## User

Go ahead

## Assistant

I've opened the job (`jf2633b9c3c77`, T1) and bound it to this session. Next, I'm handing the laptop-size mockup to a helper while I write the design document myself.

The design is written and registered. Now I'm running the two required checks on it: a cold read by a fresh reader, and a check against your own words.

I've opened the job as `jf2633b9c3c77` (T1) and bound it to this session. The design document is written and registered, covering all 14 of your rulings plus my defaults, each marked as mine.

Three things are running now:
- a fresh reader checking that the design makes sense without this conversation;
- a check of the design against your own words, looking for anything I bent, added or overstated;
- a helper drawing the laptop-size mockup, with six frames: one spectrum with a deep fit, the offset stack, the overlay, the over-the-limit list, the inspector with its new *Fit* section, and a close-up of the scan-set row's two zones.

Once the checks and mockup are back, I'll resolve whatever the checks find, look over the mockup myself, and present the design for your `/approve`. Nothing gets built before that.

## User

/transcribe

## Assistant

The transcript is saved, 28 turns: `docs/transcripts/2026-10-27-scan-s-window-fit-display-design-672a8a22.md`.

The two design checks and the mockup are still running. I'll present the design once they're all back and I've resolved what the checks find.

Now §5's defaults list, to match.

Both design checks are back. Between them they found 14 things; I've fixed 12 and rejected 2 with reasons. The check against your words caught me twice, and it was right both times:

- **I'd swapped your two buttons for a three-way switch.** That was my proposal, never your ruling, so the design now has your *Fast* and *Deep* buttons back. The switch is question A below.
- **I'd let *Deep* fall back to the fast fit when a spectrum has no deep pass.** That contradicted the rule you accepted with the two buttons: a missing fit draws nothing and never has the other pass standing in. It's reverted. Such a spectrum shows data only, and its legend line says "no deep pass".

**Other gaps closed in the text:**
- **Ctrl-click on a partly selected scan set** adds the rest of the set; on a fully selected set it removes them all.
- **Shift-click** selects only the rows you can see. It never reaches inside a collapsed set.
- **The scan inspector** never shows a spectrum outside the selection. After a group recall it shows the selection's first spectrum.
- **An untrusted deep pass** appears in the legend as "Deep (untrusted)" for as long as ISAR's record says so.
- **Residuals** use the plot's unit, so they scale with *Normalised*.
- **The normalisation reference** is the power of ten nearest the experiment's median monitor count, and the axis label states it, so the unit is always the true one.
- **Monitor error is now included, not assumed away.** The normalised point is N × R / M, where N is the counts, M the spectrum's monitor count and R the reference. Its error is (N × R / M) × √(1/N + 1/M): the usual result for a ratio of two independent Poisson counts. M is much larger than N, so the second term is usually tiny, but it costs nothing to keep. At N = 0 the error is still exactly 0, so the no-floor rule holds.

**Rejected:**
- *Drop the manual offset box.* Kept, because a strong elastic line makes the automatic offset so large that every trace's inelastic features flatten.
- *The legend should list each spectrum's own modes.* Kept as you ruled: curve roles share a style across spectra, and each trace draws only its own modes.

---

**Three small questions before I present it** (all reversible; answer with letters if you like):

**A. The fit control's shape.** Under your rule, with *Deep* on, pressing *Fast* changes nothing on screen, and a colleague will think it's broken.
- (a) Keep your two buttons.
- (b) One three-way switch, *None | Fast | Deep*, which behaves identically but has no dead state. **My pick.**

**B. The default intensity scale.**
- (a) *Normalised*, right for comparisons. **My pick.**
- (b) *Raw*, honest counts for judging the noise by eye.

**C. The default layout when several spectra are selected.**
- (a) *Offset*, where fits and components can be drawn. **My pick.**
- (b) *Overlay*, data only.

The mockup is still being drawn. I'll present the design together with it once you've answered.

The mockup is drawn: `docs/design/mockups/2026-10-27-isar-scans-view.png` (the HTML sits beside it). I've looked through all six frames and nothing is clipped. Two points to know before you open it:

- **It predates the design checks.** It shows the three-way switch, and a16 falling back to its fast fit under *Deep*. Once you've answered below, I'll have both corrected (a16 showing data only with "no deep pass", plus the "Deep (untrusted)" label) and present the design and the corrected mockup together.
- **Worth a look now:** frame 2, the offset stack with Mode 1 and Mode 2 in shared colours up the five traces, and frame 6, the scan-set row's two click zones.

The three questions still open, all reversible:

- **A. Fit control:** (a) your two buttons, *Fast* and *Deep*, or (b) one three-way switch, *None | Fast | Deep*, which behaves the same but has no dead button. **I'd pick b.**
- **B. Default intensity scale:** (a) *Normalised* or (b) *Raw*. **I'd pick a.**
- **C. Default layout with several spectra:** (a) *Offset*, where fits can be drawn, or (b) *Overlay*, data only. **I'd pick a.**

## User

A b
B a
C a

## Assistant

Recorded all three: one three-way *None | Fast | Deep* switch, *Normalised* by default, and *Offset* by default when several spectra are selected. The design is updated and registered again.

The mockup is being corrected now. a16 will show its data only, labelled "no deep pass", and the deep pass will appear as "Deep (untrusted)". When it's back I'll check it and present the design for your `/approve`.

The Scan(s) design is ready for your approval. Type `/approve` (or `/approve 573d983f0a89`) when you're happy with it; nothing gets built before that.

Design: [design.md](file:///C:/Users/AMM/AppData/Local/ACS/state/v1/working/jf2633b9c3c77/design.md)
Mockup: `docs/design/mockups/2026-10-27-isar-scans-view.png`, six laptop-size frames, with the HTML beside it.

**What it builds**
- **One page.** *Scan(s)* replaces the Scan and Waterfall pages and shows exactly what's selected in the scan list. There is only ever one selection in the window.
- **Scan list.** It becomes multi-select. A scan-set row has a wide chevron that only opens it, and a body that selects all its analyzers. A partial selection inside a collapsed set is always marked.
- **Header row:**
  - *None | Fast | Deep*, default None, one setting for all of ISAR. One pass is drawn at a time.
  - *Offset | Overlay*, default Offset.
  - *Raw | Normalised*, default Normalised.
- **Fits and the legend:**
  - A missing fit draws nothing and the legend names it.
  - The deep pass is labelled "Deep (untrusted)" while ISAR records it as untrusted.
  - Components are drawn for one spectrum and in Offset; Overlay is data only.
  - Modes are numbered outward from zero energy within each spectrum, with no matching across spectra.
- **Spectrum limit.** At most 10 spectra are plotted. Above that the centre shows a note and a read-only list of the selection.
- **Saved groups, StarCraft-style.** Ctrl+1…9 saves, 1…9 or a chip recalls, and a chip shows *edited* after changes. Undo appears briefly after an overwrite.
- **Fit numbers** move into the scan inspector, below its metadata, in place of its small plot, with fast and deep side by side.

**Since my last message**
- The mockup now matches the checked design. I fixed one line its helper had invented ("right-click on a chip overwrites its group"); the caption now says right-click on the plot is the Offset/Overlay shortcut.
- Normalised error bars include the monitor's own Poisson error: the propagated error is (N·R/M)·√(1/N + 1/M), and it is still exactly 0 at zero counts.

**Before building, the planner checks:**
- that every BL43 spectrum has a monitor count;
- that the deep pass's stored parameters can drive the existing curve evaluator;
- that components can be ordered by energy.

If any check fails, the work comes back to you.

**Sequencing.** The build starts only after the intake job's second slice lands, since this changes its scan list and inspector. The session designing the right-hand panel knows the selection is shared.

**Two things for you**
- The new mockup files, and this session's transcript, are uncommitted in the main checkout. They'll go in with this job's first commit.
- The old le Tacon fitting job (`j313eac923389`) is still listed as active on the board, with five slices pending. You called it stale; cancelling it is up to you.

## User

ChatGPT's feedback is in @C:/Users/AMM/Documents/Github/Science/ISAR/docs/ChatGPT_Handoff_CentralPlot.md
