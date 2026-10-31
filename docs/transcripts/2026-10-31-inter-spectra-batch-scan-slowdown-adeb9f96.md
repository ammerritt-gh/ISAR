# Inter-spectra batch scan slowdown

**Date:** 2026-10-31  
**Session:** adeb9f96-65f6-4664-808a-693a315add00

*Cleaned transcript: tool calls, tool results, thinking and harness notices removed. Nothing else edited.*

## User

Ok, we've got some performance optimizations to make with the inter-spectra fitting. I set up a run with two anchors, so 6 scans were going, it was using all workers and after 13 minutes not a single scan is done yet. When the last tuning ran, it took about five hours or something to do three rings. So: what's going on? Since a single solo scan takes ~1 minute, and we already have quite good guesses for each scan from the solo and the neighboring scans, my expectation was that the batch scanning would be very fast, when instead it's the opposite and very slow. What's wrong? Let's discuss the profiling and see where that time is going, what's making the batch scans so much longer than expected versus the solo scans.

/t1 
/dialogue

## Assistant

Opening the job record now, then I'll go read the inter-spectra code and any profiling we have.

Reading the runner's timing report and checking whether the operator's live run can be sampled directly.

No ISAR run is live now, so I'll work from the last run's records. Finding where the runner writes its timing report.

Parsing the run's event log to see how far each ring got and when.

The event log already shows a pattern. Now the earlier run's saved timing blocks, which have per-stage numbers.

Today's log shows the shape clearly. Now reading the stage-1 loop that produced it.

The stopped run saved no timing block, so I'll measure directly. First, what one "price" job actually does.

Writing the instrumented core (profiles each real price job in its worker) and a ring-1-only driver over a copy of the experiment.

While ring 1 runs, reading how the search sequences its passes.

Pass 1 (seed jobs) runs 1–13 s per job. Looking at the profile of the slowest while later passes run.

Clear first signal: 86% of a job is not the TRF fits (1.5 s of 22 s) but the per-basin posterior-mode optimisation in `fast_pass.price` — L-BFGS-B with finite-difference gradients. Reading it.

The split integral is 53 of 57 s: 30 quadrature nodes per split basin, each a full numeric-gradient L-BFGS-B "polish". Reading it.

While pass 2 finishes, a quick count of the model size behind each cost evaluation, so the gradient arithmetic is concrete.

I'm profiling ring 1 of your batch on a scratch copy of today's experiment, with every price job profiled inside its worker. Your workspace isn't touched. Early numbers are already pointing somewhere. I'll give you the full breakdown once the ring finishes, probably in about 10–15 minutes.

Still running: 59 price jobs profiled so far.

Still running at 91 jobs. So far about three quarters of the compute goes to one step, the split integral for a mode hidden under another. Details once the ring finishes.

Still running: 119 jobs done.

Still running: 136 jobs done.

Still running: 182 jobs done.

I have the profile numbers. Before writing them up I'm running one quick independent reading of your request, so I'm not answering only my own framing of it.

The short answer: the second pass isn't "refine from good guesses". For every spectrum it runs a full search over mode count and branch labels, and it prices each candidate from scratch with the full Bayesian evidence. Most of that cost comes from one step.

**What I measured** (ring 1 of your batch: 6 spectra, run on a scratch copy of today's experiment with 16 workers and every job profiled; your files untouched):

- **Job count.** After 11 minutes there are about 210 price jobs and 110 CPU-minutes of compute, and ring 1 still isn't finished. A solo fit of the same 6 is about 6 CPU-minutes. (A "price job" is one candidate, meaning a count plus labelling, fitted from its starts and given an evidence.)
- **Uneven spectra.** One spectrum, a05, has used about 55 CPU-minutes alone. Two spectra finished after 2 jobs each.
- **Where the time goes:**
  - **About 80%: the split integral.** This prices a mode hidden under another by integrating over how the intensity splits between them. It needs 18 to 90 inner fits per candidate, and each inner fit is a full optimisation at very tight tolerance.
  - **About 20%: the posterior-mode search** for each candidate.
  - **The actual fits: 2%.**
  - Inside both, about 75% of the time goes to finite-difference gradients: every gradient costs about 20 full model evaluations.
- **Why splits are everywhere.** A step up deliberately starts a new branch as a split whenever it lands within 2 FWHM of an existing mode. Candidates also go up to 7 modes.
- **Barrier.** Each round of jobs waits for its slowest job. In round 1, 11 jobs finished in under 13 s, then the pool sat idle for 45 s waiting on one. Pool use is about 60% at 16 workers, and lower at your full width.

Your own log shows the same pattern: rounds lasting 4, 7 and 5 minutes.

**The levers:**
- **A. Same results, cheaper arithmetic.** Analytic gradients instead of finite differences would help everywhere; my untimed guess is 5–15×. Separately, the split integral's inner fits can be made cheaper: looser tolerances, one pass instead of two.
- **B. Remove the barrier.** Feed each spectrum's search as soon as its own jobs return. That's about 1.5× (estimate), cheap, and changes no results.
- **C. Do less work.** This one changes what the pass decides. For example, run the split integral only for candidates still in contention after a cheap Laplace screen. Or make the pass a local refinement around the solo and neighbour answer, which only steps the count or relabels when the evidence or residuals ask for it.

**Your call:** should the second pass stay a full re-search of count and labels, just made faster (A+B only)? Or become a local refinement that trusts the solo and neighbour answer unless something says otherwise (A+B+C)? I lean towards A+B first, because it changes no results, then C judged on the timing that's left. C is a science decision about what the pass is for, so it's yours.

The profiling run is still finishing ring 1; I'll add its total when it lands. I've opened a job for this work (j99c66fa6d3eb); nothing has been built yet. If you'd rather talk this through properly at the desk, say so.

The question above is still open. The profiling run is still finishing ring 1, and I'll add its total when it lands.

The profiling run is still in ring 1, now 256 jobs in.

Still in ring 1, now 277 jobs in.

Still in ring 1, now 293 jobs in.

Still in ring 1, now 310 jobs in.

Welcome back. The profiling run is 17 minutes in and still hasn't finished ring 1 (6 spectra, 16 workers). It has done 326 price jobs and used 207 CPU-minutes; a solo fit of the same 6 spectra is about 6. In that time:

- **The split integral has grown to 86% of all compute.** That's the step that prices a mode hidden under another by integrating over how the intensity splits between them, with 18 to 90 inner fits per candidate.
- **One spectrum, a05, has used 130 CPU-minutes on its own** over 101 jobs. The second-heaviest, a37, used 36.
- **The fits themselves are still 2%.** Gradients estimated by finite differences are about 75% of everything.

So the picture from my earlier report holds, only sharper. The bulk of the time is the full Bayesian price of hidden-mode candidates, repeated across a wide search. The number of good starting points barely matters.

The question is still open:

- **A+B: stay a full re-search, made faster.** The second pass keeps re-searching mode count and branch labels for every spectrum, with no change to results. That means cheaper gradients and cheaper split-integral fits (A), and removing the wait for each round's slowest job (B).
- **A+B+C: also do less work.** Something changes what the pass decides. Either run the split integral only for candidates still in contention after a cheap screen, or make the pass a local refinement around the solo and neighbour answer.

I lean towards doing A+B first, then deciding on C from the timing that's left. Push hardest on this: whether you think of the second pass as a fresh search, or as polishing an answer you mostly already trust.

## User

Let's discuss more. Let me give my understanding or my conception of how this worked. When we're doing these sorts of things, we're looking at all of phase space here with many different possible modes and things like this and basically searching around that in some sort of method to find the optimal fit, right, based on number of modes and the parameters for those modes, things like this. My initial view was that the batch modes would be cheaper because now instead of having to search over a massive area, right, we're basically already well seated, right? We have a very tightly scoped in portion of phase space now because we have any neighboring anchors or any neighboring fits that it's using as well as the initial solo fit. So we have a pretty well bounded area of phase space that we need to look at instead of trying to look at everything all at once. And why that takes so much effort there is my confusion. I'm very confused why the split integral is really a big issue. If we have two modes that are really hidden underneath each other, we can't really differentiate them, right? And the goal of the anchoring here, when we do it from one scan to the next, is that at that point it doesn't really matter. As long as we have the modes, I think we're actually very safe. We don't need to get exact parameters here. This is more of a quick guess. It's a simultaneous one that's going to be able to tell us, well, this mode here and in scan one and scan three gives this and this. So this and scan two we have really strong prior that it's within this region. And that could be over another mode then, right? So that's kind of the benefit of that. The initial analytic one is just so that if we go from one to two then to three, right, we might see a mode above, you know, mode A above mode B and scan one. And scan two they're on top of each other and in scan three now mode A is below mode B and we don't want to be trying to create and destroy the modes there without good reason and that's kind of the penalty for that.

A+B right now seem like "easy wins" that we'll take regardless.

## Assistant

Your call that A+B go ahead regardless is recorded as a ruling. Before I answer the split-integral question, let me check the design doc's stated reason for it, so I explain what was actually decided rather than guess.

Checking one number before I answer: how many of the expensive hidden-pair candidates ever came close to winning.

**Decision: take the speed fixes that change no results** (2026-10-31)

- **Question, as put:** should the second pass stay a full re-search of count and labels, just made faster (A+B), or also do less work (C)?
- **On the table:**
  - A: cheaper arithmetic, same results (analytic gradients, cheaper split-integral fits).
  - B: remove the per-round wait on the slowest job.
  - C: change what the pass decides.
- **Ruling:** "A+B right now seem like "easy wins" that we'll take regardless."
- **Ruled by:** operator
- **Why:** neither changes a result.
- **Constrains:** A and B are in the job; C is still being discussed.

Your picture is right about where the pass *starts*. It's not how the pass *spends* its time. Your two points in turn:

**1. "We're already well seated."**

Neighbours and the solo fit do bound the problem, but only in two places:
- the starting points;
- the priors on each mode's energy.

They don't limit how many candidates get priced, or how carefully each one is priced. Each candidate (a mode count plus a branch labelling) still gets:
- every model variant fitted from every start;
- a full evidence calculation.

The search then steps the count up and down, and tries relabellings in batches of six. That's the rule from your search ruling: start at the seeds, go outward while the evidence stays within 5 nats of the best, no caps.

Ring 1 so far, measured on the scratch copy: 25 to 123 candidates per spectrum. About 90% of them ended more than 5 nats behind their spectrum's best. So most of the work goes to confirming that alternatives lose.

**2. "Why is the split integral such a big issue?"**

You're right that it isn't there to get the parameters; nobody needs the split of a hidden pair. It's there for one number. To compare "both modes, the weak one held by neighbours" against "one merged mode", the both-modes candidate needs an evidence value.

The usual shortcut fails here. That shortcut is the Laplace approximation: it treats the posterior as a bell curve around its peak. Along the intensity-split direction the posterior is flat, so the bell curve has no width and the shortcut breaks. That's why on 2026-10-30 you ruled "Ok, let's integrate over the split then."

The cost comes from how that integral is done:
- it tries 6, then 12, then 24 (up to 48) split values until two rounds agree within 0.1 nats;
- at each split value it re-fits all ~20 other parameters from scratch, at very tight tolerance;
- it then builds a finite-difference curvature matrix there.

So one hidden-pair candidate costs 18 to 90 full fits. And hidden pairs are common: a step up deliberately starts a new branch as a split whenever it lands within 2 FWHM of an existing mode.

**The number that matters** (ring 1, measured, run still going):

| Hidden-pair candidates | Count | CPU-minutes |
|---|---|---|
| All | 117 | 192 |
| Ended within 5 nats of the best (in contention) | 7 | 11 |
| Ended more than 5 nats behind | 110 | 181 |

In two of the six spectra the winner does contain a hidden pair. So the exact integral decides real outcomes; it's just spent almost entirely on losers.

**The question:** may every candidate first get a cheap price, with the exact split integral run only for those that come within the margin of the best?

- **Cheap price:** for example, hold the other parameters at the joint fit and just evaluate across the split, a handful of evaluations instead of 18–90 fits.
- **My estimate, not timed:** most of that 181 CPU-minutes goes.
- **What it keeps:** every decision that matters still rests on the exact integral, so your hidden-mode ruling stands where it counts.
- **What it risks:** the cheap price must not put a real contender far behind. I'd add a safety allowance beyond the 5 nats and check it on this ring's data before trusting it.

The other road is the cheap price everywhere, with no exact integral at all. That's closer to your "quick guess" view. It would save only about another 11 CPU-minutes here, and close calls would then hinge on an approximation we haven't measured. Your rule that a count change must win by 0.1 nats makes close calls sensitive to that.

Push hardest on this: is "quick guess" what you want the batch to be overall? If so, this question is too small. The real one becomes how wide the search should be, and I'd rather ask that next than assume it.

The profiling run is still in ring 1, about 410 jobs in, and I'll give its total when it finishes.

The profiling run is still in ring 1, about 426 jobs in. The question above is still open: should every candidate get a cheap price first, with the exact split integral run only for those that come within the margin of the best?

I've switched the profiling watch to report only when the run ends, so the job-count updates will stop. The question above is still open.

## User

My view of it was that when we set an anchor, generally it's something with well-defined modes and we say, okay, this is telling us that at least in this Q zone, we have, say, five modes or something like that. Then we look at the next ones here, and that should kind of be the seed, right? And compared to them, right, so we have the next Q zone at least is just a dispersion away. So it shouldn't move a huge amount, we don't think, at least here. I think the most important thing that we can do with the anchoring is just to be able to label the modes and know which ones are which, or have good guesses for that. We're basically prepping for the simultaneous fit, which is actually going to be able to tell us across three scans what the scan in the middle looks like. And we haven't even gotten to that point yet, right? So I think the first thing is that we just need to get the anchored batches fast enough that it's looking reasonable, give us the number of modes. We don't need exactly everything to be right. And then from there, once we have kind of the summary of the full batch of analyzers, then we do the simultaneous one that's now much better seated, hopefully. And then we're not fighting over exactly which mode is which and trying to figure out the dispersions then across mislabeled modes.

## Assistant

That changes the picture. Let me check I have it right.

**What you've said** (your account of what the anchored batch is for, not yet a ruling):
- An anchored batch exists to carry the count and the labels outward from a clean anchor. It works ring by ring, and each ring is "just a dispersion away" from the last.
- It's a preparation step. It only needs to give a reasonable count and the right labels, not exact parameters.
- The simultaneous batch afterwards is where precision belongs: three spectra informing the middle one. It should start well seated, so it isn't fighting over mislabelled modes.

**What that changes:** today, the anchored batch runs the same full search as the simultaneous one:
- the open outward walk over counts and labellings;
- an exact evidence for every candidate, including the split integral.

That's the right tool for the careful pass and the wrong one for a labelling pass. It explains why the cheap-screen question from my last message felt too small to you.

**What's still open:**
- what "fast enough" means for an anchored batch;
- whether the simultaneous batch keeps its full search.

I'll ask about those later.

One measured fact supports your view (ring 1, best-priced candidate so far, run still going). In all six spectra the winning count was already one of the seed counts, either the spectrum's own solo fit or a neighbour's. Of the 25 to 123 candidates priced per spectrum, the outward walk added nothing to the count decision.

**The question:** for an anchored batch, should each spectrum weigh only a handful of candidates, with the full outward search kept for the simultaneous batch? The handful would be:
- its own solo count, and each neighbour's count;
- each labelled the cheapest way against the neighbour branches;
- plus one count step up only where the residuals show an unexplained peak.

**What it buys** (my estimate, not timed): about 3–6 candidates per spectrum instead of 25–123. Combined with the cheap hidden-pair price and fixes A+B, a ring should cost about what the solo fits cost, so minutes rather than an hour.

**What it gives up:** an anchored batch can't find a count that neither the solo fit nor any neighbour has, nor a labelling far from the cheapest match. For example, if the solo fit says 4, the neighbours say 5 and the truth is 6, it stays at 5. Catching that would be the simultaneous batch's job.

**Alternative rejected:** keep one search for both kinds and just shrink its settings (a smaller margin, fewer relabellings per batch). It's less code, but it still walks outward and still prices losers carefully. And the two batch kinds would stay coupled, when you want them to do different jobs.

**This changes earlier rulings.** On 2026-10-30 you ruled that the search moves outward as far as the evidence pulls, with no caps, and that hidden modes are priced by the exact integral. Under this proposal both would hold for simultaneous batches only.

Push hardest on the "residual shows a peak" step up. Is that enough of an escape hatch for a ring where a new branch enters, or would you rather the anchored batch never changes the count beyond what the seeds offer?

## User

And so this is something we discussed before as well. Basically last night I had the question there, can we just limit it so that the count changes by one? And I don't see any reason for that. It's very confusing for me because when I think of it, I think of it as first kind of defining the very broad sections of the fitness landscape. Does it look like five or six modes? And one mode, there's no reason for us to disallow that. And it should be such a bad region of parameter space then that we don't spend really any time searching there. And so that's the confusing part for me is that we're not searching all the phase space here. In principle, I think this is okay, right? If the solo fit says four, the neighbors say five, I think that's fine. But I would like a little bit of extra explanation here as to why my conception of this is wrong. I can't see how allowing, you know, if the truth is six, I can't see how allowing one or twenty really causes to pay anything because they're such bad fits that they're going to be discarded so quickly it doesn't even matter in my view.

## Assistant

Your picture is close to how the search already works. Let me split the cost by mode count and by kind of candidate so the answer rests on numbers, not my framing.

Your conception is right about counts, and my last question pointed at the wrong lever. I've withdrawn it.

*Withdrawn: should anchored batches weigh only the seed counts? Because the numbers below show the count walk isn't where the time goes.*

**What the search already does with counts.** It works the way you describe. It steps one count up or down at a time, and stops a direction as soon as a count lands more than 5 nats behind the best. So it never goes near 20; in ring 1 it reached 7 and stopped. All the count steps together cost 12% of the compute.

**Where the time actually goes** (ring 1, 501 candidates so far, 324 CPU-minutes):

| Kind of candidate | Candidates | CPU-minutes |
|---|---|---|
| Seeds (solo fit, neighbours) | 12 | 2 |
| Count steps (up or down one) | 95 | 38 |
| Labelling moves | 394 | 285 |

A labelling move keeps the count and the fitted modes but changes which branch each mode is called. That covers swapping two labels, calling one mode a new branch, or giving it a branch nobody has used yet. Each live labelling gets batches of six such moves, repeated while they keep turning up anything within 5 nats.

**Why a bad candidate isn't discarded quickly.** This is the part your picture misses, and it isn't obvious. There's no cheap glance at a region of the landscape. The only way the code learns a candidate is bad is to:

1. fit it, every model variant from every start;
2. find its posterior mode;
3. compute its evidence, with the full split integral if it has a hidden pair.

The evidence does punish a bad candidate heavily, but producing that number costs nearly as much as for a good one:

| Candidate | Seconds each |
|---|---|
| Contender, with a hidden pair | 131 |
| Loser, with a hidden pair | 98 |
| Contender, no hidden pair | 17 |
| Loser, no hidden pair | 8 |

That's 461 losers paid for at nearly full price, which is the 290 CPU-minutes.

Hidden pairs come up so often in relabellings for a specific reason. Moving a label onto a branch the neighbours expect somewhere else drags that mode under another one, which is exactly what triggers the split integral.

**So your idea is the fix**: survey the whole landscape cheaply, and spend effort only where it's competitive. The question is whether to make that the rule.

**The question:** should every candidate (count step, relabelling, hidden pair) first get a cheap screening price? Only candidates landing within 5 nats of the best, plus a safety allowance, would get the full price. The search itself would stay as you ruled it: open, outward as far as the evidence pulls, the exact integral wherever it decides something.

- **What the screen is:** for a relabelling, re-score the parent's already-fitted modes under the new labels' priors, instead of fitting again from scratch. For a hidden pair, take a few evaluations across the split instead of 18–90 fits.
- **What it buys** (estimate, not timed): the contenders' full prices are about 35 of the 324 CPU-minutes. The screens should add only a fraction of that, and fixes A+B then make everything several times cheaper again. A ring should land in single-digit CPU-minutes per spectrum, near solo-fit cost.
- **What it risks:** a screen that underrates a real contender would drop it. A re-score without re-fitting tends to *under*estimate a candidate's evidence, which is the dangerous direction. The safety allowance covers that, and I'd measure the screen's error on this ring's candidates before trusting it.
- **What it no longer needs:** separate behaviour for anchored and simultaneous batches. With cheap screens the open search is affordable in both, so your 2026-10-30 rulings stand as they are.

Push hardest on the safety allowance. If you'd rather the screen be provably optimistic (never underrate a candidate), that's possible but costs more per screen. I'd only go there if the measured screen error turns out to be large.

## User

I think this is reasonable. It is definitely something overall that I think that when we gain more spectra we will have a better idea how to run different types of fits in different situations and really cover all possible bases. But I think it is right for us to try this now, see how well it works on the data that we have, and tune as we need to as we encounter problems rather than trying to guess what kind of problems we'll have and solve them before we even see anything.

## Assistant

I've written the design down. Now I'm registering it and running the two design checks before showing it to you.

**Decision: screen first, full price for contenders** (2026-10-31)

- **Question, as put:** "should every candidate (count step, relabelling, hidden pair) first get a cheap screening price?" Only those within 5 nats of the best, plus a safety allowance, would get the full price, and the open search stays as ruled.
- **On the table:**
  - Screen first, full price for contenders. This keeps the 2026-10-30 rulings on the open search and the exact split integral.
  - A lighter search for anchored batches only. Dropped, because with cheap screens the open search is affordable in both batch kinds.
- **Ruling:** "I think this is reasonable. … I think it is right for us to try this now, see how well it works on the data that we have, and tune as we need to as we encounter problems"
- **Ruled by:** operator
- **Why:** almost all the compute goes to pricing candidates that lose. Your picture of the search — survey cheaply, then spend effort only where a candidate is competitive — is the fix.
- **Constrains:**
  - every candidate gets a screen;
  - the full price, including the exact split integral, goes only to contenders;
  - the safety allowance is tuned on real data, not guarded in advance.

The design is written, and two independent checks are reading it now: one for wording that allows two builds, one against what you actually said. I'll show it to you for `/approve` once they're back. Meanwhile the profiling run is still finishing ring 1; its decisions become the baseline the new code is compared against.

The two design checks are back with eight findings. I'm folding the valid ones into the design now, while the baseline run finishes ring 1 (pass 5, three spectra still searching).

The design is ready for your `/approve`. It went through both design checks, and their findings are folded in.

Design: [design.md](file:///C:/Users/AMM/AppData/Local/ACS/state/v1/working/j99c66fa6d3eb/design.md)

**What it builds:**
- **Screen first, full price for contenders.** Every candidate gets a cheap evidence estimate first. A full price, including the exact split integral, goes only to candidates within 5 nats of the best plus a 5-nat safety allowance. A full price is also required wherever a candidate could keep a search direction open, not just where it could win. The search rules from 2026-10-30 stay unchanged.
- **A, cheaper arithmetic:** exact gradients instead of finite differences in the posterior search and the split integral, and no needless second polish pass. The rule is the same count, labels and mask everywhere.
- **B, no barrier:** each spectrum's search waits only for its own jobs. Every spectrum sees its prices in the same order as today, so results don't depend on which job finishes first.

**How it's checked:**
- **Baseline:** the current code's ring 1, run to completion; the profiling run is finishing that now.
- **Screen error:** measured on every ring-1 candidate, priced both ways.
- **Decisions:** compared with the screens off (A+B alone must match the baseline) and with them on.
- **Gradients:** checked against finite differences on this experiment's models, which carries "same results" past ring 1.
- **Timing:** the full 28-spectrum batch, timed against your 30-minute target, which stays a target and never a limit.

**Choices made on your behalf, which you can overrule:**
- The allowance starts at 5 nats. If the measured error doesn't fit inside it with room to spare, the build comes back to you for the value.
- The solo fast pass is untouched.
- The full old-code batch isn't re-run, because that would take hours.

The job is j99c66fa6d3eb. Nothing has been built yet.

## User

/transcribe

## Assistant

The transcript is saved, 11 turns: `docs\transcripts\2026-10-31-inter-spectra-batch-scan-slowdown-adeb9f96.md`.

The design still needs your `/approve` before anything is built. The baseline run is still finishing ring 1 in the background.

## User

/ChatGPT-Pro

## Assistant

Pro is permitted on this job. I'll send ChatGPT Pro the design for an independent review: the transcript, the profiling numbers and the code it touches.

## User

Stop the ring 1 profiling; we know it takes a long time, we have the design, what is it still running for?

## Assistant

Stopping it now. You're right: it had nothing left to tell us except a baseline, and there's a cheaper way to get one, below.

The run and all its workers are gone; nothing of it is left running. Now updating the design so it no longer depends on a completed old-code ring 1.

## User

There is absolutely no need for "another baseline" we have run it several times, we know it's unbearably slow, what will it tell us that's useful here besides "yes, faster than the bad initial work"?

## Assistant

Agreed on timing: no baseline run, old code or new. Let me separate two things, because only one of them survives your point.

You're right, and that baseline was my overbuilding: it would have shown only "faster than before", which we already know. I've stopped the ring-1 profiling run, and none of its processes are left. The design no longer runs the old code for any comparison.

What I kept, because it isn't a baseline:

- **The gradient check.** The new gradients are compared directly against the finite-difference arithmetic they replace, on this experiment's models. It's seconds of work, not a batch run, and it's what backs "same results" for A.
- **The screen error, measured once.** One ring-1 run under the new code, pricing every candidate both ways (cheap screen and full price). It's the one guard against the screen dropping a real contender, and it produces the numbers you'd tune the safety allowance from. It's a separate run from the timing, so it doesn't skew the timing.
- **The timing of the full 28-spectrum batch,** measured against your 30-minute target. That's the result you actually want to see.

Design: [design.md](file:///C:/Users/AMM/AppData/Local/ACS/state/v1/working/j99c66fa6d3eb/design.md)

On Pro: my pick is to use it on the build plan rather than on this design. The hard part is the numerics of the screens and gradients, and the plan is where those get worked out. Say so if you'd rather have it read the design now.

The design needs your `/approve` before anything is built.

## User

Use it on this design as well, then the plan too

## Assistant

Re-writing the transcript first so the review sees your latest rulings, then assembling the packet.
