# Le Tacon dataset ISAR fitting

**Date:** 2026-10-22  
**Session:** 328afc0b-94bb-4400-979b-b9662b92be01

*Cleaned transcript: tool calls, tool results, thinking and harness notices removed. Nothing else edited.*

## User

Let's work on the analysis portion of the le Tacon dataset; we have an experiment upcoming that we will help them use ISAR for the automated analysis, and this dataset is an example of what they will be looking at. Our goal: help them analyze all analyzers at once, presenting our fits clearly and coherently, and making it possible for them to make modifications. Let's take a look at the data then and get working on that. First here, just getting automated fits going; modifications to a fit attempt come after.

/dialogue 

I think a core element is to ensure that the fitting is unified across ISAR; there is no "take this data and fit it" versus "drive the instrument and fit the data." We preserve one method for it.

We had some sort of Occam penalty implemented instead of the Aikieke weights, to help determine e.g. how many peaks to use. How goes that? It's a critical part to ensure that we can take any dataset, not know e.g. how many peaks there are, and fit them appropriately.

How will it integrate into e.g. the hypothesis map? How do we determine whether we're looking at a phonon or a magnon or something else? How can an operator say "we're looking at phonons, fit those" versus what it does on its own?

/t3

## Assistant

Opening the T3 job now, then reading the le Tacon data and the fitting/model-selection code before the first question.

Job `j313eac923389` is open and bound. Reading the design goals and the existing BL43LXU state now, and sending the broad code reads to scouts.

Three scouts are reading the parser, the fit path and the hypothesis code; I'm waiting on their answers before forming a view.

Still reading. The job is open (`j313eac923389`, T3, heavy design depth), and three scouts are going through the le Tacon parser, the fit path and the hypothesis code. I'll come back with the standing and the first question once they report.

Facts are in. Running the required brainstorm panel and the design partner's blind take before I put the first question to you.

Running the current pipeline on the le Tacon set in a scratch workspace, so the discussion rests on what ISAR actually does with it today.

I'm running today's ISAR on both le Tacon temperatures, all 28 analyzers each, in a scratch workspace outside the repo. That way the first question rests on what the fits actually look like now. The full standing and the first question will come once that run finishes.

## Where things stand

I've opened job `j313eac923389` (T3, heavy design depth) and bound it to this session. The scouts have read the code, and I ran today's ISAR on the le Tacon folder in a scratch workspace outside the repo: both temperatures × 28 analyzers = 56 spectra, 4¼ minutes on 12 workers. The report is at `C:\Users\AMM\AppData\Local\Temp\claude\letacon_ws\report\report.html` if you want to look.

**Your three questions first, since they're facts, not choices:**

**1. One fitting method.** That already holds. `isar run` (files) and `isar drive` (campaign) both fit through one function, `scan_delivery.fit_one`, and pooled masters use it too. Calibration fits (energy zero, resolution) are separate jobs, not a second science path. This job keeps it that way: every new capability goes into that path, and nothing gets a le Tacon-specific fitter.

**2. The Occam penalty.** It's in production. Since ruling H4, the number of peaks is chosen by Bayesian evidence: each candidate model's likelihood averaged over its whole parameter space, so a peak the data doesn't need costs evidence. AIC and Akaike weights are now only diagnostics, plus a labelled fallback when evidence can't be computed. **But it never gets to choose beyond two modes.** The code caps the candidate set at two phonon modes ("values >2 are treated as 2"), even though `HYPOTHESES.md` claims no cap is needed. A second mode is only offered when the one-mode fit looks bad. On le Tacon:
- 55 of the 56 fits chose two modes, which is the ceiling.
- ISAR's own model-free peak finder sees 4–9 significant peaks per spectrum (counting the elastic line and anti-Stokes peaks).
- The fit-quality number (cost per degree of freedom, about 1 for an adequate model) runs from 1.8 to 16, and 9 analyzers were flagged "structured residual, missing component".

So the Occam machinery is sound, but it's choosing from a list that's too short. That's the main thing to fix.

**3. The hypothesis map, and phonon vs magnon.** Declared hypotheses (`[hypotheses.*]` in the card) steer where to measure and label peaks after the fit. They don't change what the fitter tries. The fitter on its own tries:
- damped-oscillator phonons, with anti-Stokes tied by detailed balance;
- an elastic line;
- a flat background;
- a quasi-elastic term, only if the card declares its width.

There's no magnon class, and nothing in ISAR tells a phonon from a magnon. For IXS, I'd assume magnetic signal is out of reach anyway, but that's your call on the physics.

**A presentation problem the run exposed:** the "primary" mode flips between the ~5 meV and ~12 meV peaks from one analyzer to the next. Le Tacon's own `.pkpos` file, which picks one peak per analyzer, flips the same way. Tabled as they come out, 28 fits won't read coherently.

**What looks easy:** batch fitting all analyzers (it already works) and a shared all-analyzer view. **What looks hard:** an open-ended peak count that stays honest and affordable, since each extra mode costs evidence computations on 56 spectra. Also hard: per-analyzer Q, which ISAR doesn't know yet (computing it from the six-circle angles is the open TODO item BL43-1).

**Questions in the order I'll take them:**
1. What one fit covers: a single analyzer, or the set.
2. How the number of modes grows past two.
3. What an operator's declaration does to the fit.
4. Where phonon/magnon/other identification lives, and whether it's in this job.
5. The all-analyzer presentation.

Modifying a fit stays out of scope, as you said, but the design mustn't block it.

---

## Question 1: is the unit of fitting one analyzer or all 28 together?

**Why now:** questions 2 and 5 both build on it. A joint fit would decide the peak count once for the whole set, and would need a new multi-spectrum fit structure.

**The situation:** each of the 28 analyzers records its own spectrum at a slightly different momentum transfer around (0.5, 0.5, 3), with its own measured resolution curve. ISAR knows only the arm-centre position from the filename, not each analyzer's actual Q.

**Option A: independent fits.** Each analyzer is fitted on its own through the one fit path, all 28 in parallel, as now. Coherence comes afterwards, from a shared view and a cross-analyzer check that flags an analyzer whose modes disagree with the rest.
- Locks in almost nothing.
- At one month: ready for the beamtime.
- At six months: once per-analyzer Q exists, the same fits feed dispersion plots.
- The cost: a weak analyzer can't borrow strength from strong neighbours.

**Option B: one joint fit across all 28, sharing parameters.** For example, a mode's energy varies smoothly with Q across the analyzers. Physically it's the stronger statement, and weak spectra gain from strong ones.
- Needs a new multi-spectrum structure inside the fit path, evidence computed over the joint dataset, and a new outcome record.
- Needs per-analyzer Q, so it's blocked behind BL43-1.
- At one month: a method-development project competing with beamtime readiness.
- The risk: a wrong sharing assumption gives precise, mutually consistent wrong answers, and one bad analyzer contaminates all 28.

**Option C: A now, with B recorded as a later opt-in mode,** taken up once per-analyzer Q exists and the independent fits show a concrete need for it.

**My recommendation: C.** That means independent fits in this job, and the door to a joint mode stated and left open. The design partner (a second model given the question without my view) reached the same answer on its own. The five-model brainstorm panel split 3 to 2: three for independent fits plus a consistency layer, two for a hierarchical joint fit. The two were outvoted on the Q problem: without per-analyzer Q there is nothing to share along.

**Where I want the push:** did "analyze all analyzers at once" mean something about the physics, with analyzers sharing parameters? Or did it mean one operation and one coherent view? And would the le Tacon group expect a joint fit from us at this beamtime?

Question 1 is still with you: should each analyzer be fitted on its own, or all 28 fitted jointly?

## User

3. You're correct, IXS inherently *cannot* see magnetic signal, we should see only phonons and other structural signal
We are working on better formatting so that all scans are labelled with important information including Q point for each one; I would assume that will be solved before the upcoming experiment in some form.
Q1: A is probably right just to be prepared for the experiment and because we have strong hypotheses about what the spectrum looks like, but I'd like to push that this isn't looking at it from the right direction; what I like to iterate is that ISAR should work by keeping a hypothesis map (what our models show) and the intensity map (what our data shows). One "joint fit" B is kind of getting the right idea, we want to make the hypothesis map match the intensity map, but it's stronger than just fitting everything at once; it's understanding that an optic phonon produces a spectrum like *this* and and acoustic like *that*, so we can label them, then fitting e.g. the energy or linewidth is a product of understanding that it's a phonon and where it is. The two feed together, but the hypothesis map doesn't see "energy per Q" it sees "dispersion" and "projected intensity map." Does that make sense?

## Assistant

The design partner is answering your reframing blind. Meanwhile I'm checking how ISAR's hypothesis map is updated from data today.

It makes sense, and I think it's the right direction. It's also your own words from the 2026-08-20 walkthrough, in the Design Goals Inbox: "the map is challenged by intensity; parametric fits are a downstream presentation and analysis product." So my first question was asked from the wrong end.

*Withdrawn: "is the unit of fitting one analyzer or all 28", because the unit you mean is neither a spectrum nor the set. It's the hypothesis map, judged against the intensity map.*

## What you settled, and what's still open

**Decision: IXS admissible signal** (2026-10-22)
- **Question, as put:** whether magnetic signal is out of reach for IXS ("that's your call on the physics").
- **Ruling:** "IXS inherently *cannot* see magnetic signal, we should see only phonons and other structural signal"
- **Ruled by:** operator
- **Constrains:** an IXS card offers phonons, elastic, central-peak and quasi-elastic terms, and background. No magnetic classes.

**Per-analyzer Q** comes from the other ISAR job that's in design right now: `jc54d8ce88c0c`, "unified catalogue-driven data intake", run by session isar-23. This design takes Q as an input from that job and doesn't build it. One rule applies: we don't build on that job's calls until you've accepted them.

**Independent per-analyzer fits:** a lean ("A is probably right just to be prepared"), not a ruling. It stays open inside the bigger question below.

## Your architecture, in my words. Check that I have it right

- **The hypothesis map** is a set of phenomena in a region of momentum–energy space. Each phenomenon has a class (acoustic phonon, optic phonon, elastic/central peak, background) and smooth fields over Q: dispersion ω(Q), linewidth Γ(Q), intensity I(Q).
- **Projection** turns the map into expected counts at every measured point: each analyzer's Q, its measured resolution, the Bose factor and the exposure.
- **The test** is one Poisson likelihood over all raw counts at once. The Occam evidence then chooses between maps (how many branches, which classes), not between peak counts per spectrum.
- **Per-spectrum fits stay, with two jobs.** They're the bottom-up proposals ("there's something at 12 meV here"), and they're the human-readable layer: each spectrum drawn with the map's projection over it, every peak labelled by its branch, energy and linewidth read off the map.

**Where ISAR is today:** both maps exist by name (`docs/design/eig.md`), and the scan planner already projects the map forward into expected counts. But the arrow from data back to the map runs the other way. Fitted peak energies become anchor points, and a regression through those points *is* the hypothesis map (`evidence_update.py:103`, `session_prior.add_point` at `:655`). That is exactly the "energy per Q" you say the map shouldn't see. The scene layer is richer, but it still combines per-scan results rather than testing one shared model against the counts. **So the missing piece is the count-level update from intensity map to hypothesis map.** That's the core of ISAR's inference, not a fitting feature.

## Where I'm pushing back

**1. One spectrum can't tell acoustic from optic.** At a single Q, both are a damped oscillator convolved with resolution. The class lives in how the branch behaves *across* Q: whether it goes to zero at the zone centre, how its intensity varies. That makes it a property of the map, which fits your framing. But near the M point, over the narrow Q spread of 28 analyzers, the data may not discriminate much. Labels there would largely come from the group's declared hypotheses, and ISAR must say so. Did you mean something visible in the spectrum itself? Detailed balance between your two temperatures, for instance.

**2. At any one moment, a map fit is mathematically a well-built joint fit.** What makes it more than that, and keeps it honest, is three things:
- it **persists and predicts before the data arrives** (the planner already uses it that way);
- it's **checked on data it wasn't built from**, for example predicting one analyzer from the other 27;
- it carries an **"unexplained" component**, so a wrong map shows up as a residual rather than bending to fit.

Without those, a wrong map gives precise, mutually consistent wrong answers. That was the one dissent on the five-model council (4 yea, 1 nay), and I think it's right. ISAR's catalogue already has an "unknown" competitor; it would need to exist at the map level too.

**3. The peak-count problem doesn't disappear. It becomes "which branches are present, and where is each visible."** Two branches can make one apparent peak, and a branch can be real but too weak to show in any single spectrum. The Occam evidence still decides. But the per-spectrum proposals must never veto a branch that's only visible collectively. The two-mode cap still matters for the proposal layer.

## The question: is the count-level map needed at this beamtime?

This answer changes the job's size. The design partner and I reached the same split independently:

- **Layer I (beamtime-ready):** per-analyzer fits through the one fit path, with the two-mode cap lifted by evidence-driven growth. Q comes from the intake job. All 28 analyzers are presented coherently, with modes labelled consistently, residuals shown, and ambiguity visible. This isn't throwaway: it *is* the proposal layer and the readability layer of your architecture.
- **Layer II (the architecture):** the count-level local map fit. For le Tacon that means a few branches, each with a low-order dispersion over the analyzers' Q, analyzer-specific amplitude and background, one likelihood over all 28 spectra per temperature, and the evidence choosing the branch set. It changes who owns the evidence, and the scene layer's contract.

My recommendation: this job designs both, but builds Layer I first as its own slice. Layer II gets its own slice after that, possibly its own approval, because it rewrites how ISAR proves things.

Two things decide whether Layer II has to be ready by the experiment:
- **When is the beamtime?**
- **What would the le Tacon group actually do with the output there?** If they're steering on "where are the modes in each analyzer", Layer I serves them. If they need branch-level answers live, Layer II moves up.

**Where I want the push:** pushback 1, the labels. If you meant that class is readable from a single spectrum, I'm missing something, and it changes what the map has to carry.

## User

Whoa there, let's not force this admissible signal item; I didn't ask for this. We just don't need to worry about it right now, we shouldn't see any e.g. magnons. This comes back to the hypothesis map, that we have very low priors for any magnetic signal with IXS.

Ok, I believe your reading is right of the idea. And you're right that single scans can't generally be built into full hypothesis maps. That's part of the benefit of having 28 analyzers here. But you're discounting the operator here, who has much more context, and might combine it with e.g. DFT calculations; and generally an operator won't make decisions from a single scan either, so it's a moot point, I'm not asking to assign acoustic vs. optic from one scan. From just a spectrum we wouldn't even know that they're phonons definitively; we take the operator hypotheses and priors for that. But we can assign peaks at least.

For this beamtime, we just need layer 1: we get spectra, we fit a bunch of peaks, we display those fit results in an easy-to-read format, ideally we can do some helpful analysis/display for it both across analyzers and e.g. across temperatures. Good example: if we want to track one moving peak, we can try tracking it automatically, and also allow operator input. Layer 2 them comes later, but I don't want to build against it; we built with it in mind, and see what we can bring along now, what groundwork we can lay.

The beamtime is in a bit more than a week, so that's the layer 1 focus for now.

## Assistant

You're right, and I'm sorry: I turned an aside into a rule you never made, and added a constraint ("no magnetic classes") on top. That entry is withdrawn. What you actually said is that magnetic signal is a very low prior for IXS in the hypothesis map, and nothing gets built for it now.

Checking how far the two-mode limit runs through the code before I put the next question to you.

## What landed this round

**Decision: magnetic signal under IXS** (2026-10-22)
- **Question, as put:** whether magnetic signal is out of reach for IXS.
- **Ruling:** "We just don't need to worry about it right now, we shouldn't see any e.g. magnons. This comes back to the hypothesis map, that we have very low priors for any magnetic signal with IXS."
- **Ruled by:** operator
- **Constrains:** nothing is built for it. It's a prior in the hypothesis map, not a rule.
- **Supersedes:** "IXS admissible signal", because I had turned an aside into a rule.

**Decision: beamtime scope is Layer I** (2026-10-22)
- **Question, as put:** is the count-level map (Layer II) needed at this beamtime?
- **Ruling:** "For this beamtime, we just need layer 1: we get spectra, we fit a bunch of peaks, we display those fit results in an easy-to-read format, ideally we can do some helpful analysis/display for it both across analyzers and e.g. across temperatures. … Layer 2 them comes later, but I don't want to build against it; we built with it in mind, and see what we can bring along now, what groundwork we can lay."
- **Ruled by:** operator
- **Why:** the beamtime is a little over a week away.
- **Constrains:** the build is Layer I, which is four things: open peak count, readable display, cross-analyzer and cross-temperature views, and peak tracking (automatic plus operator input). Nothing in it may assume per-spectrum fits are the final authority. The design names the groundwork it lays for Layer II.

**Decision: who labels what** (2026-10-22)
- **Question, as put:** can class be read from a single spectrum?
- **Ruling:** "From just a spectrum we wouldn't even know that they're phonons definitively; we take the operator hypotheses and priors for that. But we can assign peaks at least."
- **Ruled by:** operator
- **Constrains:** ISAR finds peaks and assigns them to tracks. Class names (phonon, acoustic, optic) come from the operator's hypotheses, never inferred by Layer I.

Agreed on the operator point, too: you'll bring context ISAR never sees (DFT, the literature), and the design should treat your input as the stronger source.

---

## Question 2: how does the peak count become open-ended?

**Why now:** "fit a bunch of peaks" is the first item on your list, and the run showed it's the binding failure. 55 of the 56 spectra hit the two-mode ceiling, with 4–9 significant features visible. Tracking and display both consume whatever this produces.

**The situation:** the fitter tries one mode first. If that fit is poor or its residual is structured, it offers two-mode models, seeded by scanning the residual for the best spot for the extra peak. Bayesian evidence picks the winner. The code stops at two. One "mode" here already includes its anti-Stokes partner through detailed balance, and the elastic and central-peak terms are counted separately. So 4–9 visible features probably means 2–4 phonon modes per spectrum, not 9.

**Option A: sequential growth.** This generalises the existing loop.
- After the N-mode winner, offer N+1 when the fit still looks inadequate, or when you've declared more peaks than it found. The new peak is seeded on the residual; then every parameter is freed and refitted jointly.
- The evidence compares every count tried so far, so a weaker mode never wins just by being added.
- To guard against a false stop (sometimes one broad peak needs two replacements before the bigger model wins), it tries one count beyond the first decline.
- A configurable ceiling and time budget apply. When either binds, the record says "search limited" rather than "settled".
- Locks in: the existing two-stage machinery, extended. It's honest about where it stopped.
- At six months: the same seam serves as Layer II's proposal generator.

**Option B: a fixed window around a count.** Take the model-free peak finder's count, or your declared count, and fit, say, count−1 to count+1.
- It's cheaper and more predictable in time.
- But the peak finder splits broad features, misses overlaps, and counts anti-Stokes and elastic peaks. A wrong count quietly bounds the answer.
- Locks in a candidate set chosen by a heuristic rather than by the data.

**Option C: a full search over all counts, 0 to some maximum, every time.** It's the cleanest statistically and the most expensive. Errors would have to be computed for every rejected count unless split off. Too slow for live use, and nothing it gains here is worth the cost.

**My recommendation: A, with B's counts used only as seeds and look-ahead targets, never as a bound.** The design partner reached the same hybrid on its own. A few rules come with it:
- **Error bars only for the contenders.** The expensive error calculation (bootstrap and profile, about 75% of today's time) runs on the selected model, not on every count tried. That keeps a 28-analyzer set within minutes.
- **Your declared peaks** (the existing `[fitting]` peak hints in the card) seed modes and make growth more willing. They never force a mode to survive.
- **Every mode is kept and exported**, in a mode-per-row table beside `results.csv`. No more "primary/secondary" columns: that split is part of why the le Tacon output reads incoherently.
- **The weak point I'll be honest about:** the Occam penalty is only as honest as the priors on a new peak's energy, width and amplitude. Today some of those bounds come from the data (amplitude from the observed maximum). For two modes that was tolerable; at four it will bias the count. I'd fix the prior recipe and record it, and check one alternative prior width on the le Tacon set before trusting the counts.

**The risk to the week:** nothing in the code stops `phonon3` onward from being generated. The open question is whether the evidence calculation stays reliable and fast once four overlapping modes are in play. I'd measure that on the le Tacon set in the first two days and tell you what it found.

**Where I want the push:**
- Is a recorded "search limited at N=5" acceptable at the beamtime, or should live runs stop lower, say 4, for speed?
- Do you want the elastic line and the central peak treated as candidates in every spectrum? Today the central peak (the quasi-elastic term) appears only if the card declares its width. For a CDW material near its transition, that may be the physics rather than a nuisance term, and that's your call.

## User

Hm, let me give you my hesitancy here. We've had scans where we tried fitting with one peak and found that instead two close together worked better, see e.g. C:/Users/AMM/Documents/Github/Science/Data_Handling/Claude outputs/Waterfalls_by_setting_all.pdf the 18.6% S (S2) data. How do we know to do two peaks there? It took me some looking at closely to think about it, because we expected one; one benefit of this system is it should catch this sort of issue. But how do we insure that it doesn't just double up on peaks? The Occam penalty helps. Perhaps this is a good first test; can we reproduce those results in ISAR, with this fitting method? Sequential is... fine, but I do worry about traps.

Let's discuss this more. What options do we have? There's more than just "try more peaks until something works."

Here, let me propose a challenge: if we had a program that randomly generated peaks within a range, of various parameters, up to some reasonably finite number, ISAR should be able to fit them to statistical thresholds; it's not magic, it can't fit past noise or e.g. a tiny peak overwhelmed by the tail of a larger one next to it, but it should be able to fit any spectra *that a human could fit.* If we showed the real peaks in an A/B test, we shouldn't be able to say that one is better than the other.

## User

And this is most certainly a *solved problem*, so let's not reinvent the wheel.

## Assistant

The operator's right that this is a solved problem. I found the standard methods and I'm getting the design partner's blind view on them and on your benchmark before I answer.

Agreed, it's a solved problem, and your worry about traps points at exactly where ISAR falls short of the solved version.

## Where ISAR sits today

ISAR already uses Sivia's method, the classic answer from the ISIS QENS work (Sivia et al. 1992; it ships in Mantid as `quasielasticbayes`). For each candidate count, the evidence is computed by approximating the likelihood as a Gaussian bump around the best fit the optimizer found (the Laplace approximation). That's fast and sound **when the optimizer found the right optimum**.

That's the trap you're describing. If the one-peak seed never leads the optimizer to the close doublet in your S2 data, the evidence never sees the two-peak solution, however good it is. "Sequential growth" inherits the same blind spot, because it seeds each new peak from the previous fit. I had a second problem in the code too. ISAR skips expensive evidence calculations using the bound Z ≤ L(best fit), which is only valid if the best fit is the *global* maximum. A local optimum isn't. The design partner found it and I checked it (`evidence.py:330`). Separately, nested sampling refuses any candidate whose optimizer didn't converge (`bayes.py:156`), so a model that's hard to optimize can never be judged.

## The established options

**1. Laplace evidence per count (Sivia).** What we have. It decides in seconds. It misses whatever the optimizer misses: close doublets, and broad features that need splitting.

**2. Nested sampling for each count separately.** For each N from 0 to N_max, run nested sampling and compare the evidences.
- Nested sampling doesn't start from one fit. It begins with points spread across the whole prior range and climbs inward, so it finds a two-peak solution whether or not a one-peak fit pointed there. That addresses the missed doublet.
- It addresses doubling-up through the Occam factor working as intended: a redundant second peak spreads probability over a volume the data doesn't use, and loses evidence. When N and N+1 are genuinely close, the answer is reported as ambiguous with both probabilities, not forced.
- It's established for counting spectral components (NestFit; Sokolov et al. 2020). ISAR already depends on `dynesty`, which does it.
- The cost is seconds to tens of seconds per count per spectrum.
- Peak identities must be made unambiguous, by ordering the energies, so the sampler doesn't count the same solution twice under swapped labels.

**3. Reversible-jump MCMC.** The count itself becomes a parameter, and the sampler jumps between counts, giving P(N | data) directly. De Francesco, Guarini et al. (PRE 2016) built it for *exactly* this case: damped-oscillator excitations whose fits get trapped in local minima, in neutron Brillouin spectra. It's the most direct match to your worry. But it needs tuned birth/death moves and checks that the sampler mixes, which is real build risk in one week.

**4. Trans-dimensional nested sampling** (Brewer's diffusive NS). Elegant, but the most new code.

**My recommendation: option 2 decides the count; option 1 previews.** At the beamtime, a spectrum shows its quick Laplace answer within seconds, and the settled answer replaces it once nested sampling finishes, minutes later on the workers. If the settled answer runs out of time, it says "unresolved". The two shortcuts above get fixed. The seeds include *splitting an existing broad peak*, not just adding peaks in the residual. Option 3 stays in reserve if the benchmark shows nested sampling missing narrow solutions. The partner reached the same choice independently. The risk to the week is latency, so the first thing built is a timing comparison on le Tacon and S2.

## Your challenge, as a pass/fail benchmark

I think this is the right acceptance test. In computable form:

**The generator.** Spectra with random counts (0 to N_max) and random energies, widths, amplitudes, temperature, elastic and central-peak strength, background and exposure. Each mode carries its detailed-balance partner. Everything is convolved with a realistic resolution and drawn as Poisson counts. Hard cases are deliberately over-represented: close doublets, broad singles, a weak peak on a strong tail, features near the elastic line. ISAR's synthetic "worlds" generator and the STAT-2b scorer already do most of this, so it's an extension, not new machinery.

**"A human could fit it", made computable.** A truth counts as resolvable if a high-budget reference fit can tell N peaks from N−1 at least 90% of the time when the extra peak is really there, while wrongly splitting a single peak at most 5% of the time. Both are measured by simulation. On resolvable cases, the result must be the right count. On borderline cases, "ambiguous" is the correct answer, and exact recovery isn't demanded.

**Proposed acceptance numbers:**
- At least 90% correct counts on resolvable cases.
- At most 5% confident extra peaks on single-peak and empty spectra.
- Abstentions are reported, never hidden.

**Your A/B test, made precise.** Draw a fresh spectrum from either the truth or ISAR's fit, and give it to the best possible statistical test (the likelihood ratio between the two curves). If that test picks the source right no more than 60% of the time (50% is a coin flip), the two are indistinguishable at the data's own noise. It's stricter than looking at them by eye. A blind visual A/B on a sample, by you, is a worthwhile second check.

**The S2 doublet** becomes the first real-data proof (under `proofs/`, from the raw S30 spectra in `Data_Handling`, not from the PDF). ISAR compares one, two and three modes without being told the answer, and we see whether it reaches your two-peak reading on its own. Your reading is the reference to check against, not a truth to force.

**Where I want the push:**
- The numbers. Do 90% / 5% / 60% match your sense of "a human could fit it"?
- The latency trade. Is a settled count arriving minutes after the preview acceptable at the beamtime?

Sources:
- [Sivia et al. 1992, Bayesian analysis of QENS data (PDF)](https://bayes.wustl.edu/sivia/quasielastic.neutron.pdf) · [quickBayes QENS example](https://quickbayes.readthedocs.io/en/stable/examples/QENS.html)
- [De Francesco et al., PRE 94, 023305 (2016)](https://flore.unifi.it/handle/2158/1046871) · [ILL BRISP Bayesian example](https://www2017.ill.eu/for-ill-users/instruments/former-instruments/brisp/examples/a-bayesian-approach-in-the-analysis-of-inelastic-neutron-brillouin-spectra)
- [Sokolov et al. 2020, probabilistic detection of spectral line components](https://arxiv.org/pdf/2003.07644) · [NestFit (AAS 2021)](https://baas.aas.org/pub/2021n1i311p02)
- [Brewer, trans-dimensional diffusive nested sampling](https://arxiv.org/pdf/1411.3921)

## User

Yes, we can expect most fits to be good and clear, and then we break out the harder calculations for tougher problems, similar to the error calculations. 1 now, 2 is the next rung up. Another item is that, to a human, just showing a spectra and having them click near the center of every obvious (to them) peak would give us enormous priors, and we'll need something like that anyway for the user to view the fits and edit them or e.g. assign labels (so we can track if peak A moves across scans for example).

The number seem right to me. Minutes is fine, it takes a human far longer; assume a scan can take 30 minutes on its own, minimum (I've had scans run for hours!) and factor in time to decide and realign and whatever, so we can spare the compute.

Please pin these references in the docs, we'll need them at some point I'm sure.

## Assistant

Recording the rulings. Before the next question I'm checking where the references should live, and getting the partner's blind take on how your clicks should enter the fit.

## Recorded

**Decision: count-method ladder** (2026-10-22)
- **Question, as put:** which established method decides the peak count.
- **On the table:**
  - Laplace evidence per count (Sivia; fast, but blind to optima the fitter never finds);
  - nested sampling per count (explores the whole prior; seconds to minutes);
  - reversible-jump MCMC (posterior over N directly; more build risk);
  - trans-dimensional nested sampling.
- **Ruling:** "Yes, we can expect most fits to be good and clear, and then we break out the harder calculations for tougher problems, similar to the error calculations. 1 now, 2 is the next rung up."
- **Ruled by:** operator
- **Constrains:** Laplace per count is rung 1. Nested sampling per count is the escalation for hard spectra, the same shape as the error-bar ladder.

**Decision: benchmark thresholds** (2026-10-22). On synthetic spectra:
- at least 90% correct counts where the truth is resolvable;
- at most 5% confident extra peaks on single-peak and empty spectra;
- the best statistical A/B test tells ISAR's fit from the truth at most 60% of the time.

Ruling: "The number seem right to me." Ruled by: operator.

**Decision: latency** (2026-10-22). Ruling: "Minutes is fine, it takes a human far longer; assume a scan can take 30 minutes on its own, minimum." Ruled by: operator. It constrains the count decision, which may take minutes per scan.

**References:** they go into `STATS_PRIMER.md` §2, the statistics document where you'd look when defending the method, as a short "established methods" list. That edit lands with the job's first change rather than as a separate commit on `main`, since nothing lands outside the job's PR. One correction to last round: the nested-sampling line-counting paper is Sokolov, Pineda, Buchner & Caselli, ApJ Letters 2020. I cited it by surname only before; that's now confirmed.

**How rung 1 catches your doublet** (my proposal, open to challenge in the design): rung 1 itself won't be allowed to stop at "one peak looks fine". For every fitted peak, it also tries *split* seeds: two peaks either side of it, closer than the resolution width, with unequal heights. It escalates to nested sampling when:
- the split stays competitive;
- different starting points disagree on the count;
- modes merge or their amplitudes vanish;
- the best count sits at the search ceiling.

That turns the S2-style question from luck into an explicit test, and the benchmark measures how often it works.

---

## Question 3: what does a click mean, and is it in the beamtime week?

**Why now:** you've tied clicks to three things — seeding fits, editing fits later, and labelling peaks for tracking. Each meaning is treated differently in the statistics, and tracking is in the beamtime list.

**The statistical catch.** A click made while looking at these counts is information taken *from* the same counts. If it became a narrow prior on the peak energy, the second peak would skip most of its Occam penalty. The penalty is precisely the cost of the energy range the peak *could* have occupied, and the click would shrink that range using the data itself. The evidence would then reward exactly the doubling-up you worried about, because the data gets counted twice: once by the click, once by the likelihood.

**Three meanings a click could carry:**
1. **"Try a peak here" (a seed).** It starts the optimizer there and makes sure a model with that count is tried. The priors and the Occam penalty stay as they are, and the one-peak alternative still competes. This gives you the "enormous" help you mean — the human solves the search problem — without contaminating the evidence. Two clicks *ask for* a two-peak model; they don't *conclude* one.
2. **"This is peak A" (a label).** It assigns a fitted peak to a track across scans. No statistical effect. This is what tracking needs.
3. **"Theory predicts a peak here" (a prior).** Reserved for knowledge from outside these counts, such as DFT or an earlier experiment. It's stated with its source and width, and applied once.

**Storage, under ISAR's two hard rules:** the GUI never writes science, and code never writes the card. Clicks become a small *request* file on the same control channel the GUI already uses to start and stop runs: "refit spectrum X with these seeds", or "label mode 2 of fit Y as peak A". The engine validates the request, refits through the one fit path, and keeps the automatic result beside the human-seeded one. You can always see what the machine found alone.

**My recommendation:**
- **Meaning:** a click is a seed or a label by default. It becomes a prior only when you name an outside source.
- **Beamtime-week scope:** first, the automatic fitting and display, so clicks never have to rescue a fit. Then labelling fitted peaks ("this is A") with automatic tracking suggestions, which is your tracking item. Click-to-seed and refit requests would follow straight after the beamtime, built on the same request record.

**Where I want the push:** the cut line. If you expect the le Tacon group to need click-to-seed *during* the run to steer, rather than labelling alone, it moves into the week and something else moves out. My first candidate to move out is cross-temperature display beyond a simple overlay.

Sources:
- [Sokolov et al. 2020, Probabilistic detection of spectral line components (arXiv:2003.07644)](https://www.arxiv.org/abs/2003.07644)

## User

Let me break it into three modes, so we don't mix up a click in mode A from a click in mode B. Let's say we have one plot of a spectrum, I vs. E:
Mode 1, fitting: we have a bare spectrum, no fitting done; the user is prompted to click where they see peaks. This seeds the peak fitting with centers; we can have them click at the top of the peak to get heights as well. From there, we can expand the search, and exactly as you say, it starts with e.g. a two-peak model, but it should still be able to try other peaks. It may disagree with the user. This is optional.
Mode 2, modification: now we have a fit, the user is presented with the spectrum *plus* the fits. They can remove, add or move peaks around and retry the fitting, e.g. "what if we tried another peak here?". This refits and represents.
Mode 3, label: like 2, we have a spectrum plus fit, now the user is just labelling the peaks (peak A our phonon of interest, peak B not so interesting, for example), this is just to help the user organize things.

Question on my end then: can we toggle on/off forcing the fitting? Can I force N peaks, or force one here? I don't want to make it too complicated, the goal of ISAR is not to be a fitting program. See PUFFIN (C:\Users\AMM\Documents\Github\Science\PUFFIN) for a fitting program... By "can" here, I know we can do it technically, more *should* we allow it, and if so in what format...

## Assistant

**Decision: three click modes** (2026-10-22)
- **Ruling:** "Mode 1, fitting: … the user is prompted to click where they see peaks. This seeds the peak fitting with centers … It may disagree with the user. This is optional. Mode 2, modification: … They can remove, add or move peaks around and retry the fitting … Mode 3, label: … this is just to help the user organize things."
- **Ruled by:** operator
- **Constrains:**
  - A click is read only by the mode it was made in.
  - Mode 1 clicks are seeds (centres, and heights if clicked at the top), and the search may overrule them.
  - Mode 2 is a refit request.
  - Mode 3 changes no science.
  - Alongside that: "the goal of ISAR is not to be a fitting program". PUFFIN is the fitting program.

## Should ISAR allow forcing?

**Yes, but only two narrow controls, only in Mode 2, and never in place of the automatic answer.** The design partner reached the same answer independently.

**Why allow it at all.** Your own Auditing goal says "ISAR never ignores a user command, it at best logs an exception or a warning, and proceeds anyway." And forcing is exactly how you ask the S2 question: "the machine says one peak; what does the data say *if* there are two?" A forced fit isn't a wrong fit. It's a conditional answer: "given three modes, here they are."

**The two controls:**
1. **Peak count: Automatic / Exactly N.** N counts phonon modes, each with its anti-Stokes partner tied as always. The elastic and central-peak terms are separate.
2. **This peak: Free / Keep within an interval.** You drag a small energy band on the plot, and one mode must stay inside it. I'd use a band rather than an exact position, because the visible maximum of a damped peak convolved with resolution isn't its true ω₀. Locking the centre where it *looks* to be would bias it.

**How a forced result is shown and kept:**
- **Side by side.** It sits next to the automatic result, labelled as constrained, for example "Exactly 3 modes (operator)". Both are kept, with what was forced and when.
- **The evidence is still reported**, for example "the automatic comparison prefers 2 by a decisive margin". It's information, not a veto.
- **It isn't pooled into the evidence comparison.** You chose the constraint after looking at these counts, so it competes as your hypothesis, not as an independent model. That's the same double-counting point as before.
- **A forced mode whose amplitude collapses** is reported as "requested, not supported by the data". There's no artificial height floor to keep it alive.
- **You may adopt the forced result** for a track or a display. It stays visibly marked as forced in plots and exports.

**What stays out, and goes to PUFFIN:** fixing individual parameters, ties between parameters, custom lineshapes, and editing constraints across spectra. That's a fitting workbench, and ISAR shouldn't become a second one. If we ever hand a spectrum to PUFFIN, the variance convention must not travel with it: PUFFIN floors σ at 1 and ISAR deliberately doesn't (hard rule 3).

**A defect this turned up:** today's card-level way to restrict models (`[fitting]` structures) quietly falls back to *all* models, with only a warning, when the restriction matches nothing (`guidelines.py:168`). That's ignoring the command in all but name. "Exactly N" must attempt N, or say plainly that it couldn't. It must never quietly hand back an unrestricted fit.

**Where I want the push:**
- **Band or exact lock?** Is "keep it within this band" what you mean by "force one here"? Or do you sometimes need an exact centre held fixed, which I'd rather leave to PUFFIN?
- **The week.** Does Mode 2, with "Exactly N", belong in the beamtime week? My earlier cut put Modes 1 and 2 just after it, with Mode 3 (labels) and tracking inside it. "Exactly N" is cheap once Mode 2's refit request exists, but Mode 2 itself is the bigger piece.

## User

Let's be very careful here, and focus on the fitting; PUFFIN shows that opening up options to let the user do fitting is a real can of worms, and needs its own program. It is more likely that ISAR would talk to PUFFIN for real fitting than we rebuild fitting in ISAR. Let's keep it focused: mode 1 should be in (click to seed peaks), mode 2 with only forcing peak count possible (not ranges, not fixing any parameters, nothing complicated), mode 3 with labels. And honestly, for this week, I think we can just pin modes 1 and 3; 2 doesn't provide much, if they want to start fitting we can export the fit parameters and let them use their own program. Is that easier?

## Assistant

Folding the partner's display-and-tracking answer into a plan you can check.

**Decision: click modes for the week** (2026-10-22)
- **Ruling:** "mode 1 should be in (click to seed peaks), mode 2 with only forcing peak count possible (not ranges, not fixing any parameters, nothing complicated), mode 3 with labels. And honestly, for this week, I think we can just pin modes 1 and 3; 2 doesn't provide much, if they want to start fitting we can export the fit parameters and let them use their own program."
- **Ruled by:** operator
- **Constrains:**
  - This week: Mode 1 (seeds) and Mode 3 (labels), plus an export of fit parameters.
  - Mode 2 comes later, as "exactly N" only.
  - Real fitting work goes to another program, likely PUFFIN, not into ISAR.

**Is that easier?** Yes, noticeably. What drops out is the "edit an existing fit, refit, keep before and after" machinery and the forced-count path. One honest caveat: Mode 1 still needs a small "fit this spectrum with these seeds" request from the GUI to the engine, since the GUI may not fit anything itself. Mode 2 would later be the same request made on a spectrum that already has a fit, so nothing built now is wasted.

The defect from last round goes on the TODO list rather than into this job: the card's model restriction quietly falls back to all models when it matches nothing.

---

## Question 4: what the le Tacon group sees, and what drops if time runs short

**Why now:** this is the rest of your beamtime list ("easy-to-read", across analyzers and temperatures, tracking a peak), and the week can't hold everything at full polish. One fact sets the baseline: today's report for this dataset is a single empty "dispersion summary" plot, because ISAR has no per-analyzer Q. Nothing currently shows the 56 fits.

**What I'd build** (the design partner proposed nearly the same, independently):

**The overview (GUI).** One tile per analyzer, in a 7×4 grid. Each tile holds two small spectra stacked, 302 K above 97 K, on the same energy axis, with:
- the analyzer number, and its Q once the intake job supplies it;
- a fit-status mark (resolved / flagged / held);
- the total fit in black, and named peaks in fixed colours. Unlabelled peaks are grey: "third-lowest peak" never gets an identity colour by accident.

Nothing is hidden. Held and failed analyzers keep their tiles.

**The inspector.** Click a tile and the two spectra open full size: data with error bars, total fit, each component, residuals, and the full table of every mode. Mode 1 (click to seed) and Mode 3 (label a peak) both happen here.

**Tracking.** You label one fitted peak "A". ISAR then suggests the matching peak in the other analyzers and at the other temperature, from energy continuity with its uncertainty, allowing for "missing here".
- Suggestions are dashed; your labels are solid and always win.
- Close calls stay marked ambiguous, and a suggestion is never presented as a probability.
- It's a small new matcher. The existing campaign tracker assumes a declared path through Q and acoustic/optic roles, which is more than "follow my peak A".

**The HTML report.** The same overview, the per-analyzer pages, the mode tables and the tracked-peak plot, as a snapshot to send or archive. It's built from the same saved results, so it's never a second analysis.

**The export.** One folder:
- `modes.csv`: one row per mode, with energy, linewidth, amplitude, uncertainties, your label and the suggested track;
- `fits.json`: every component and parameter, plus the model-selection alternatives;
- each spectrum with its counts, exposure, mask and resolution curve, so another program can reproduce the fit;
- a short note on units and conventions.

**Priority order. If the week runs short, I cut from the bottom:**
1. Open-ended peak count: rung 1 with split seeds, rung 2 escalation, all modes saved. Plus the benchmark's quick subset, the S2 proof and the references.
2. The overview grid and inspector.
3. Export bundle.
4. Mode 3 labels.
5. Mode 1 click-to-seed.
6. Tracking suggestions.
7. HTML report pages.

Items 1–3 are what the group can't work without. Labels and seeds are what make it yours rather than the machine's. Tracking and the report are the first to slip.

**Where I want the push:**
- **The grid.** The analyzer numbers come in four runs of seven (a03–09, a14–20, a25–31, a35–41). I'm guessing that's the physical 4×7 array, so neighbouring tiles would be neighbouring Q. Do you know whether it is?
- **Who drives it at the beamtime.** Will the le Tacon group drive the GUI themselves, or will you? That decides how much polish and guidance the first screen needs.
- **The order.** Does it match yours? In particular, should tracking rank above Mode 1, since it's the only cross-analyzer analysis on the list?

The other ISAR session that is redesigning how instrument files come in (isar-23, job `jc54d8ce88c0c`) wrote to coordinate on the BL43 code. I replied:

- **Its intake change lands first.** The le Tacon fitting will read per-analyzer Q and other scan facts from what that job provides, not from the card. My design changes the fitting, results and display code; it doesn't touch the BL43 parser, calibration or the card.
- **One shared decision may come to you.** Your Mode 1 seeds and Mode 3 labels need a place where the GUI's human input is recorded and the engine reads it. Their design adds "ISAR-recorded settings edited in the GUI", which sounds like the same thing. I've asked whether it can hold per-spectrum seeds and labels, so we don't build two stores. Both designs are still unapproved, so I'm not building on theirs yet.

Question 4 is still with you. In short:
- **The analyzer grid:** is a03–09, a14–20, a25–31, a35–41 the physical 4×7 array, so neighbouring tiles would be neighbouring Q?
- **Who drives it:** will the le Tacon group run the GUI themselves at the beamtime, or will you?
- **The priority order:**
  1. open-ended peak count (with the quick benchmark, the S2 proof and the references)
  2. analyzer overview and inspector
  3. export
  4. labels
  5. click-to-seed
  6. tracking suggestions
  7. HTML report pages

  If the week runs short I cut from the bottom. Should tracking rank higher?

isar-23 replied, and two things came out of it.

- **Display code:** the other job reworks the Files panel and the facts section of the existing inspector. The new analyzer overview and spectrum inspector are separate widgets, so we don't collide. If I do have to touch the existing inspector widget, theirs lands first.
- **One storage decision for you, coming later:** the other job records values that people enter, one entry per scan and analyzer, and never overwrites or deletes them. isar-23 suggests your Mode 1 seeds and Mode 3 labels go into that same record as two more entry types, rather than a second store. I lean the same way, but it's your call, and both sessions are logging it as open. I'll put it to you properly once Question 4 is settled.

Question 4 is still with you: whether the analyzer numbering is the physical 4×7 array, who drives the GUI at the beamtime, and whether the priority order is right.

## User

Ok, let me recalibrate your timing. Go by your work speed, not mine. This will go faster than you think.

I am happy to pin mode 1 for later here, it's not an essential element. Let's focus on what matters most, and if we have more time we polish for them.

Q4: The grid seems reasonable. They will drive it, I am in support remotely. The order looks good.

## Assistant

Noted on timing. I'll plan to my own work pace.

**Decision: week scope and order** (2026-10-22)
- **Ruling:** "I am happy to pin mode 1 for later here, it's not an essential element. Let's focus on what matters most, and if we have more time we polish for them." Then: "The grid seems reasonable. They will drive it, I am in support remotely. The order looks good."
- **Ruled by:** operator
- **Constrains:** the build order is
  1. open-ended peak count, with the quick benchmark, the S2 proof and the references;
  2. the 7×4 analyzer overview and the inspector;
  3. the export;
  4. labels (Mode 3);
  5. tracking suggestions;
  6. HTML report pages.

  Mode 1 is deferred. Because the le Tacon group drives the GUI themselves, the screens have to explain themselves without you there.

**One decision I've settled myself, since storage is mine to settle:** labels go into the record isar-23's job is building, as one more entry type, provided their design is approved. I've told them.

The GUI will be run by outside users, so if you want the professional-UI design lens applied to it, name `/ui-professional` and I'll add it. Otherwise I'll design the screens without it.

---

## Question 5, the last before I write the design: should every spectrum be tried with a central peak?

**Why now:** it's the one physics default left open, and the le Tacon group won't know to change it.

**The situation:** ISAR offers two different "central" components, and the difference matters here.
- **Elastic:** a peak exactly the shape of the measured resolution, sitting at zero energy. It's offered in every spectrum whose window covers zero. In the scratch run, 53 of the 56 fits included it.
- **Quasi-elastic:** a broader Lorentzian centred at zero, convolved with the measured resolution. It's offered **only if the card declares its width**, and the width is fixed, not fitted. The le Tacon card declares none, so it was never tried. (I checked: it does use the measured BL43 resolution curve when one exists; an old code comment says otherwise.)

**The options:**
1. **Keep today's rule.** The elastic line is always offered; broad quasi-elastic only with a declared width. If the central region leaves structured residual, the fit is flagged, and the record says "quasi-elastic not tested" rather than "absent".
2. **Declare a width for this experiment** (your number, or the group's), so every spectrum at both temperatures is also tried with a broad central component of that width. It's cheap: one extra amplitude per model. But it's a conditional model, and it can't measure the width.
3. **Fit the quasi-elastic width freely.** It's the most physical, and the hardest. As the width goes to zero it becomes indistinguishable from the elastic line, and when broad it trades off against a soft low-energy phonon. Those are exactly the cases that push fits up to nested sampling. It needs its own prior and its own benchmark cases.

**My recommendation: option 1 for the beamtime, with option 2 available.** If you or the group have a defensible width, it goes into their experiment setup before handover, and the inspector and export show "quasi-elastic width fixed to …". Option 3 waits until the data show it's needed: if the 97 K spectra carry central residual that the elastic line can't absorb, it gets raised as a beamtime blocker rather than quietly guessed at. The design partner reached the same recommendation independently.

**Where I want the push:** you know CsV₃Sb₅ near its CDW transition far better than I do. Is the central intensity there resolution-limited (elastic), or would the group expect resolved broadening they'd want a width for?

Once this is answered I'll write the full design for a cold reader and run the two required checks: one reads the design alone, the other against this whole conversation. Then I'll present it for your `/approve`.

## User

Q5: Let's look at the data quickly here, I don't think we need the quasi-elastic included and option 1 would fit.

## Assistant

Option 1 it is. Writing the full design now, with the professional-UI lens applied to the GUI parts.

Design written and registered. Now running the two required checks on it: a cold read, and fidelity against this conversation.

**Decision: central region** (2026-10-22). Ruling: "I don't think we need the quasi-elastic included and option 1 would fit." Ruled by: operator. What it means: the elastic line is always tried; the broader quasi-elastic peak is only tried if its width is declared; leftover structure near zero energy is flagged, and the record says "not tested", never "absent".

The design is written and registered with seven review questions, including a GUI check through the professional-UI lens. Before you see it, two fresh reviewers are checking it: one reads only the design and notes every place it had to guess; the other compares it against this whole conversation for rulings it omits, bends or invents. I'll fix what they find and present it for your `/approve` once both are back.

Now labels, tracking, the G8 attribution, the dependency wording, and the "rejected alternatives" wording.

The design is ready for your `/approve`.

Design: [design.md](file:///C:/Users/AMM/AppData/Local/ACS/state/v1/working/j313eac923389/design.md)

## What it builds, in brief

Layer I for the le Tacon beamtime, in your priority order:
1. An open-ended peak count on the two-rung ladder, plus the fast benchmark subset, the S2 doublet proof, and the references pinned in the statistics primer.
2. The 7×4 analyzer overview and the inspector.
3. The export.
4. Labels.
5. Tracking suggestions.
6. HTML report pages.

Everything goes through the one fit path. The design also says what it lays down for the later hypothesis-map work, and what it leaves out: click-to-seed, "exactly N" forcing, parameter fixing (that's for PUFFIN), the free-width quasi-elastic peak, and per-analyzer Q (the intake job supplies it).

## What changed after the two design checks

One reviewer read the design cold and reported 27 places where it had to guess. The other checked it against this conversation and found 5 problems. I fixed all 32. The ones that change behaviour:
- **No early stop on the count.** I had written "stop one count after the evidence first declines". You never ruled that, and it could leave the higher-count answer untried. Now every count from 0 to the ceiling (6 by default) is tried on every spectrum.
- **Every spectrum shows exactly one state:** fitting, provisional (a quick result shown while the deeper check runs), settled, search limited, unresolved or held. Quality warnings, such as a structured residual, are shown alongside the state, never in place of it.
- **Tracking now allows the peak to move.** It looks within a window of a few resolution widths around the named peak, adjustable per peak. Before, it only matched peaks whose energies agreed within their error bars, which would have refused any real shift.
- **Labels survive refits honestly.** When a fit is replaced, a label moves over automatically only when exactly one new peak sits where the old one was. Otherwise it's marked "needs confirming" and you're asked.
- **The intake dependency is restated correctly.** The fitting work doesn't wait for the intake job. Only Q on the tiles, sorting by Q, and label storage depend on it, and that job's design is also still unapproved.

## One small question left open in the design

You approved "the best statistical test tells ISAR's fit from the truth at most 60% of the time". It's measured per test spectrum. What isn't settled is how it becomes pass/fail for the whole benchmark: must every resolvable case meet 60%, or a stated share of them, say 90%, to match the count criterion? Answering now or after approval are both fine; it doesn't change the build.

## Defaults I chose, all listed in the design's last section

- a mode ceiling of 6;
- a rung-2 margin of 10 in log-evidence;
- a tracking window of three resolution widths;
- benchmark draw counts (200 and 1000);
- CSV files for the exported spectra;
- labels stored in the intake job's record, if that design is approved.

---

Once you approve, I'll compact the transcript and design, hand planning to a planning coordinator, and build in slices from a job worktree. If this conversation is worth keeping in full, `/transcribe` will save it. I won't offer again.

## User

Let's not enforce a rigid pass rate; try to get a share passing, we can tune it more later. Setting a must-pass rate is a good way to sit tweaking a bad approach all night, so if we have trouble with it, don't overspend.

## Assistant

**Decision: A/B figure across the benchmark** (2026-10-22)
- **Question, as put:** must every resolvable case meet the 60% bound, or a stated share of them?
- **Ruling:** "Let's not enforce a rigid pass rate; try to get a share passing, we can tune it more later. Setting a must-pass rate is a good way to sit tweaking a bad approach all night, so if we have trouble with it, don't overspend."
- **Ruled by:** operator
- **Constrains:** the benchmark reports the share of cases where ISAR's fit can't be told from the truth, as a target, not a gate. If raising that share proves hard, the build reports it and moves on.

I've applied this to the A/B figure only. The count targets you set earlier (90% correct counts, at most 5% false extra peaks) still stand as ruled. If you meant the same no-gate treatment for those too, say so and I'll change it.

The design is updated and presented again, ready for your `/approve`:

Design: [design.md](file:///C:/Users/AMM/AppData/Local/ACS/state/v1/working/j313eac923389/design.md)

## User

Yes, still a no-gate treatment for all numerical pass/fail thresholds; shoot for it, but don't spend all night tweaking to get from 70% to 90%. It may take a lot of work, and if we build the foundations I can check in the morning.

## Assistant

**Decision: every numerical threshold is a target** (2026-10-22)
- **Ruling:** "Yes, still a no-gate treatment for all numerical pass/fail thresholds; shoot for it, but don't spend all night tweaking to get from 70% to 90%. It may take a lot of work, and if we build the foundations I can check in the morning."
- **Ruled by:** operator
- **Constrains:**
  - The 90% correct counts, the 5% false extra peaks and the 60% A/B figure are all targets to aim for and report, not gates.
  - The foundations come first: the two-rung ladder, the synthetic spectrum generator and the scoring.
  - Tuning stops when progress stalls, and the figures reached go to you in the morning report.

The design is updated and presented again:

Design: [design.md](file:///C:/Users/AMM/AppData/Local/ACS/state/v1/working/j313eac923389/design.md)

It's ready for your `/approve`.

## User

/approve

## Assistant

The design is approved. The ChatGPT review bridge and the Codex launcher are both up.

Next, the conversation and the design are each being condensed into short summaries for the agents that plan and build the work; both are running now. Once the conversation summary is accepted, a planning coordinator will turn the design into build slices. I'll report back when the plan is ready.

## User

/transcribe
