This is a USER-owned document outlinining what ISAR should be able to *do*. Some of these are "in progress" or "wishlist" items some are mostly done, some need refinement; this is the *master objectives* list that shows what ISAR is aiming for. It should NEVER be AI-edited; if you find a discrepency, bring it up with the USER, do NOT edit it yourself.

# Auditing
[TRANSCRIPT] ISAR exists to serve scientific interest. Thus, every scientific claim that it makes, whether that's about data or the parameters of a model, a fit, anything, must be completely auditable, never deleted, and made consumable for outside scientists. ISAR's workings, by which I mean things like how it chooses which points to scan, how it chooses which models are true or false, how it actually goes and conducts a campaign, are only diagnostic audits, in that we need to know how they work to tune it and understand the mechanisms. But there is no situation where somebody will claim that there is a scientific mistake from how ISAR conducted a campaign, because as long as the measurements are valid, then those are true things, and we can back out exactly what happened at the machine. Fits can be done by ISAR or other methods and be valid either way, and having the fits auditable from ISAR is good because then they can be shared and proven that ISAR did it correctly, and we don't have to redo the work. But how ISAR decided which point to scan is not as important because a wrong answer here changes how an experiment was conducted, and this may be something the user doesn't like, but it's not something that an external reviewer would ever think comprises scientific misconduct or anything like this. If ISAR completely lost its logs, and we had no record of why it chose anything, only the scans, it would still be a perfectly valid experiment, and valid for any paper. It is still helpful for us to know how it makes its decisions for us to diagnose issues and understand that it's doing statistically correct things, but it does not necessarily keep this for external review or be perfectly compatible, perfectly auditable with that, only enough for our purposes and what others may want to know.

# Collecting Data
[TRANSCRIPT] ISAR needs a few different ways to collect data. The first way is to point it at a set folder or file or other directory from which it can collect data files there and perform any work, and these are fixed, so it's one-shot inputs and then work with the files. Or ISAR can be pointed at a particular file or directory for output files that are part of an ongoing experiment and watch that location for new incoming data and then bring that data into its provenance as well. So it's acting now as a watchdog and automatically works while an experiment is ongoing. And of course, ISAR can talk directly to TAVI and get simulated data from TAVI quickly that way. And it may be possible in the future for ISAR to link directly with an experiment and get the outputs automatically there without having to go through the intermediate data files. And it should still check in that case that the data files and its output match when it can, but it may get additional information or a nicer format that way.

# Data Reduction and Translation Catalogues
[TRANSCRIPT] ISAR is in its most basic mode, purely a reducer, in that it applies no fitting or analysis to data. It only translates data from the output of a particular machine into a format that the user wants. In this case, it is just acting as a parser and a collator, maybe. So what this can do, for example, is that we have a scientist who goes to multiple different machines and gets different file formats from them. What ISAR does is it is one system that you can feed any instrument's data file format into, and get back a parsed output in the format that you have set based on a catalog, and with options like collating scans together that are at the same point, either combining scan windows that are at different ranges, and also renormalizing for particular monitors. And this then is purely a data reduction system and leaves all analysis to the end user.

[TRANSCRIPT CONT] And so ISAR has to have translation catalogs that work for every instrument and knows the particular formats of the instrument with revisions based on date and schema changes, so that as output data formats change or we collect old data, anything, we can link it to a particular format from a particular instrument and effectively parse it then. And in this case, ISAR does no actual analysis work. It could do collation, so taking scans that are in the same location together and combining the data along with renormalization, whether that's with combined scans or also between scans so a user can directly compare between scans. And users can also select from different output translation logs to get data in a format that is effective for their needs, whether that's certain formats for analysis programs like Igor and Origin Labs or MATLAB, or their own custom work, file renaming with the parameters that they want in there, anything that they would like there that they are able to select from or write themselves that allows them to take input data in any format and get it into the right output format that they need to start analysis sooner.

# Data Analysis


# Sample Knowledge

## Sample lattice
The sample lattice is the fundamental aspect that maps from real space (atoms regularly spaced apart to form a crystal) to reciprocal lattice space (Brillouin zones). We map lattice paramters and the space group as the fundament parameters to form the metric for QE space: how are Bragg peaks spaced, what distances are HKL in, what angles and geometry do we work with, or what Bragg peaks are allowed versus forbidden. This is the "map" that allows us to understand QE space. We align on Bragg peaks and bound phenomena within Brillouin zones sized by space group and lattice parameters.

This means that understanding the lattice parameters and space group are critical to understanding the observed phenomena. An incorrect lattice parameter will scale the fundamental map incorrectly and put q targets off. An incorrect space group will lead to confusion as Bragg peaks go missing and acoustic phonons have the incorrect periodicity.

The sample lattice must be a first-class parameter tied to every scan, but it is *mutable*. If we have the wrong lattice constants and do a scan, those scans happened at some actual absolute machine setup, they just didn't map into QE space the way we thought they did. We stamp every scan with a "sample lattice parameter" ID, traced back to a recorded lattice parameter set. Then, we can always take any scan, trace what the lattice parameters used was, and transform it as necessary to reuse it in a different mapping; we apply the recorded change, always with clear provenance for tracebacks or modifications, and proceed.

Consider the case where we scan over H across a Bragg peak at (2,0,0) and measure the center at (2.1,0,0). We adjust the lattice paramter *a* by 5% up, rescan, and now the Bragg peak is found exactly at (2,0,0). Scan 1 is perfectly compatible with scan 2, after we note the modified lattice parameters; we can apply the change in lattice parameters to back out that the peak at (2.1,0,0) was at the (2,0,0) Bragg peak.

Users (or ISAR) may adjust commentary and metaparameters, e.g. we change temperature and need to adjust lattice parameters to compensate for the lattice expanding/shrinking, thus we branch into two different families of lattices that won't be remixed later.

## UB matrix
If the lattice parameters tell us what the sample *itself* is doing, the UB matrix tells us how our sample is orientated relative to the instrument configuration; in effect, we are fixing the relative orientation of the sample relative to the *instrument.* The *U* portion is the crystal orientation, and *B* is the reciprocal-lattice basis matrix.

The simplest way to think of the UB matrix is that if we can take two (non-collinear) points, these define a plane; two Bragg peaks then define a plane for us to orient the sample with. With a known lattice, we can get the full 3D orientation in the correct right-handed coordinate system. Thus, to define the UB matrix, we take at least two (possibly more) Bragg points, compare them against sample angles, and adjust to find the UB matrix. This math is known and straightforward.

The key idea in this section is that like the lattice paramters, the UB matrix can be stamped; and in fact, this is what will be recorded, because the full matrix *includes* the sample lattice parameters. Now, if a scan was done in some orientation with some UB matrix, we can convert any point of that scan into a different UB matrix-frame. We still explicitly convert into "orientation" and "sample lattice parameters," because e.g. the latter changes with temperature routinely (orientation CAN change with temperature if e.g. the mounting flexes as it cools! We cannot assume that orientation is always fixed).

## Space group
Finally, space group. The space group does not enter into the UB matrix at all. Instead, it mainly tells us which peaks are allowed or forbidden. Then, because Brillouin zones are centered by Bragg peaks, this also changes the size and shape of Brillouin zones; a simple cubic and a fcc lattice may have the same lattice parameters and both have a peak at (2,0,0), but because the (1,0,0) peak is forbidden in fcc the Brillouin zone is doubled in size. Space groups also place constraints on the allowed lattice parameters, e.g. simple cubic requires *a*=*b*=*c* and alpha=beta=gamma=90 degrees.

Space groups can also be degenerate, or users may use different orientations; for example, a tetragonal system can be rotated 45 degrees and scaled to make it equivalent to a orthorhombic system. We should be able to clearly match QE space between these two systems, changing orientation and space group as required for the need at hand; for example, if the material undergoes a tetragonal to orthorhombic transition, we can measure the lattice parameter splitting of the two twinned lattices, which is easier in orthorhombic than tetragonal, thus we switch before we actually undergo the transition.

Like the UB matrix, we stamp the space group for scans, thus we can always swap between different QE space mappings.

## Alignment and lattice corrections
Sample alignment is, bluntly, adjusting the UB matrix and the sample parameters to make the measurements match the ideal map. Our claim: we expect Bragg peaks at points X Y Z and not at A B C. We measure those Bragg peaks, find them at X' Y' Z' and adjust the UB matrix. We check A, if we observe a peak we have to update our space group, or not.

What we need to do is apply it to ISAR. Sample orientation and parameters are not items to be fixed by the user and left, but something that ISAR factors into its hypotheses as the *ground truth mapping of QE space.* This means that if ISAR measure a phonon at (2+/-h,0, 0) and gets different answers, its should be updating that there is a misalignment problem and that *it needs to correct this.* It can make the decision to check the Bragg peak, notice a misalignment, correct the UB matrix, reorient the older scans and then continue measuring.

As well, sample alignment is oftentimes a slightly tedious job for initializing an experiment. We ideally have some idea about the orientation of our sample when we place it in the mount, we try to get it roughly aligned with the beam, and we measure to fix the UB matrix. There are techniques to accomplish these, and we can transmit them to ISAR; an ideal case is we simply mount the sample and give some weak priors, then let ISAR take it from there to completely align the sample.

## Sample quality
Samples are oftentimes not ideal. Even a single crystal carries a fundamental mosaic, a spread as the atoms move slightly over long distances from their perfect orientation; this crystal mosaic broadens scans, as it's the crystal itself spread out. But there can be bigger issues; crystals can have very broad mosaics which heavily smears out signal, or can be made up of many smaller crystals that each have their own separate scattering. Thus, we have to consider how the sample *itself* can modify our expected scattering away from ideal.

Consider the case of two samples that are coaligned: checked and mounted to try and place their orientations together. If they are off (they are), we get two separate signals; two Bragg peaks where we expected one, slightly shifted, or two branches. Most often, this comes as a smearing of signal when it's broad (phonons) and split peaks when the signal is sharp (Bragg peaks). We can include this: we can record that we are measuring multiple crystals and fit each one separately, trying to deconvolve their smeared signal. There is a gradient that starts quantized (one perfect crystal, to two crystals, three, etc...) that smooths out as many crystals are coaligned (at 1000 crystals, their composite mosaic becomes like one crystal, nicely replicating a single crystal with broad mosaic). These are things that ISAR can handle.

## Sublattices
Crystal lattices aren't necessarily the only lattice of importance. Neutrons are sensitive to magnetic signal, and a magnetic lattice can coexist with the structural crystal lattice (I am not an expert on magnetic scattering, so this is something I will need to research and come back to).

Another good example is orthorhombic samples; the two possible orientations can coexist, and we measure two Bragg peaks naturally from the two different orientations (this is called twinning). So similarly to the two coaligned samples, we need to be able to disentangle two different orientations.

# Phenomena Hypotheses


# Automated Scans

## Pause/Failure States

When running an automated scan, the only reason to ever stop the campaign is from user choice, meeting a campaign goal, being overbudget or machine failure. If a user wants to intervene or sets a stopping point, this is fine. If we reach our goal (measure this branch to +/- 0.1 meV) we can stop this campaign and consider it complete. If a user sets a budget and we go over that budget (e.g. scan for 4 hours and alert me when finished), then we stick to that budget. If there is some machine failure, we don't keep scanning with garbage data (e.g. if the IXS beam is turned off, we don't keep scanning and getting nothing, we wait for the go-ahead when it's back up). This means that bad fits, low intensity, unknown phenomena, unexpected results, anything that isn't a real "stoppage" does NOT stop an automated campaign. ISAR should never have a stoppage that ends a campaign early outside of these scenarios. A bad fit is not a reason to stop, but it should inform new scans; this is the core of EIG. 

We have had campaigns stopped in the past for e.g. bad fits in previous iterations, so we need a review and cleanup pass to *remove all these extra stopping points.* We must maintain explicit, clearly delineated stopping points that are only activated under controlled, documented circumstances.

---

# Inbox — agent-writable

Candidate top-level objectives drafted by agents FOR the user, merged here from the former
`docs/DESIGN_GOAL_INBOX.md` (2026-08-20). Protocol unchanged: **agents may only APPEND** new
candidates to this section — after checking the item is not already in the body above or in this
Inbox. Agents never rewrite, reorder, or remove entries; the USER drains entries into the body
(in their own words) or deletes them, on their own. This section is an inbox, not a contract.

## Entries 2026-08-13 — from the SCENE-4 planning grill

### The hypothesis map and the QE map (→ its own top-level section, or "Data Analysis")

ISAR's chain of authority: the hypothesis map (our scientific model) → the scientific question
(what we care to challenge in that map) → the measured S(Q,ω)/QE space (what we actually
measured — ALL counts go into the map) → fits derived on top. **Counts are authoritative; fits
are not.** A fit can always be redone another way over the same region of QE space; a bad fit
never erases, hides, or blocks anything. Distinct from that: a *broken scan* (the counts
themselves untrustworthy — machine fault) is quarantined, the map emptied there, the failure
recorded, rescan attempted when possible — with an offramp so a persistently failing region
cannot trap a campaign.

### Hypothesis-driven at the core (→ "Phenomena Hypotheses")

The fundamental question is always: *what is our confidence in this model versus our data?*
Fitted quantities (a phonon energy) are instrumental to higher-level models — the real object
may be "the dispersion changes in this way at these temperatures," with individual energies only
in service of that. Models form a hierarchy, and completion of any measurement goal is a
statement about model confidence; a parameter tolerance (±0.1 meV) is the degenerate special
case, not the template. Example of the right shape: "the acoustic branch reaches the zone
centre" is answered by extrapolation under a declared prior (linear near ZC), with confidence
that grows as fits tighten or scans approach the fold — and with an honest ceiling: behaviour
*at* the ZC is untestable under the elastic line.

### Campaign regimes (→ "Automated Scans")

User intent spans a spectrum, and it is on the user to state it:
1. **Open discovery** — "scan this area, find phenomena, pin them down, identify them, fit to
   ISAR standard defaults." Unexpected in-domain findings are part of the goal.
2. **Strict targeted** — "map *this*; anything else only in service of that goal (background,
   calibration)." Possibly multiple targets with an order of importance.
3. **Guided** — region + expectations + bounded freedom; everything between the extremes.
Eventually ISAR needs a real campaign *writer* (a goal compiler with judgment), not just a
parser.

### Never set-and-forget (→ "Automated Scans")

Users can be prompted mid-campaign for permissions and campaign updates (widen the domain?
abandon a failing region? keep spending on an unresolvable question?). Prompts are non-blocking
with a declared default — silence chooses the default, the beam never idles waiting on a human —
and the consequences of answering or not answering are on the user. Hard blocking gates are
opt-in ("ask before ever leaving the region, and wait").

### ISAR is always measuring (→ "Automated Scans", extends Pause/Failure States)

Completing a campaign is not a reason to stop measuring. The ideal: enough campaigns queued to
run through without ending, or an open-ended finishing campaign that just collects useful data.
Only user intervention idles the beam; "stop when this campaign is done" is itself a user
choice.

### Freedom to leave the beaten path (→ "Automated Scans")

A campaign is a scientific objective, not a fence. By default ISAR may take confirmation scans
off the nominal path — the −E side, −k against +k, an equivalent point in another Brillouin
zone — when it needs corroboration; symmetry equivalence is what makes such scans usable as
evidence about the same phenomenon. Users *may* lock a campaign down with hard limits, but a
pre-computed safe zone is not required by default.

### The stale-model contract (→ "Automated Scans")

ISAR accepts that it usually does not have the perfect next scan ready. Model A chooses scan 2;
scan 1 lands; scan 2 starts while the update to model B (which chooses scan 3) computes. How
much staleness to accept — compute between every scan, run one behind, or batch scans on a very
fast machine — is an **operator** trade-off exposed as configuration, never fixed inside ISAR.
Every scan decision records which model state chose it.

### Even blind measurement is hypothesis-driven (→ "Automated Scans" or "Phenomena Hypotheses")

A first-look survey is not a uniform grid. ISAR knows what an acoustic phonon *is* (strong and
linear near the zone centre, weaker and less predictable at the boundary, hidden too close to
the Bragg peak — which is itself a modelled phenomenon to avoid), so it looks where the
hypothesis says the phenomenon is findable, and each fix feeds extrapolation to the next
placement. A truly prior-less sparse map is a legitimate *user choice*, not the default.

### Sample environment is an open parameter set (→ "Sample Knowledge")

Temperature, magnetic field, pressure — conditions are first-class, general, and never
hardcoded to one variable. Observations at different conditions (or from different samples) are
different thermodynamic scenes: they never blend by default, and inference across a condition
boundary happens only through a model that declares and prices that dependence (e.g. a
phase-transition hypothesis), not through ordinary spatial adjacency.

### Instrument cost model (→ "Automated Scans", future)

Real spectrometers pay real time to move (motor speeds, configuration changes). Scan selection
should eventually weigh information value against instrument-motion cost; until then, nearness
to the current configuration is a stand-in tiebreak.

## Entries 2026-08-15 — from the PR #33 cold-start ruling discussion

### The energy ceiling is soft when time remains (→ "Automated Scans" or "Pause/Failure States")

"Map the phonons" has a physical energy floor (E = 0, by detailed balance) but no natural
ceiling — the frozen program requires one anyway. When the campaign's declared goals are met
and beam time remains, ISAR should PROMPT (non-blocking, declared default) to extend the
energy ceiling toward the instrument limit and continue the optic hunt at the zone centre —
"more time? keep measuring up in energy at the ZC to find more optic phonons, up to some
limit of the machine." A widened ceiling is a consultation-channel re-freeze (new program
revision), never in-place mutation.

### A failed in-zone hunt recommends the next zone (→ "Automated Scans")

When the optic hunt at a zone centre finds nothing, the correct scientific move is often a
DIFFERENT Brillouin zone (structure-factor weighting) — "if we don't find them, we know we
need to look in a different BZ, and tell the user." The deferred symmetry-equivalent-coupling
feature (SCENE-5-adjacent) should be built toward exactly this sentence: the failed hunt
produces a concrete other-zone recommendation through the consultation channel, not a silent
exhaustion.

### Priors exist to be weakened (→ "Phenomena Hypotheses")

There are no hard rules for phenomena in ISAR — ever. A phenomenon class carries PRIORS
(the acoustic phonon: disperses to the zone centre, linearly nearby, at overwhelming
strength), and the point of priors is that they weaken: linearity decays away from the
fold, so hypothesis strength — predicted energies — is high near the zone centre and low
far away. Scan placement, window content (Stokes / anti-Stokes / elastic together when
they are cheap and mutually constraining; Stokes-first far out; anti-Stokes de-prioritized
when the Bose factor kills it at low T), and when to stop measuring all PRICE OUT of that
strength gradient against cost via EIG. Behaviour falls out of priors and prices; it is
never legislated per phenomenon.

## Entries 2026-08-18 — from the SCENE-4.3 scene-ownership discussion

### Three operating modes, GUI-canonical (→ its own top-level section, or the preamble)

ISAR has three user-facing operating modes, each a superset of the last, and the GUI is the
canonical way to access all of them (the console CLI is plumbing, not a user surface):
1. **Reduce-only** — ISAR as parser/reducer: machine formats in (files or streamed from the
   instrument), the user's preferred end-state format out; collation of scans at the same
   temperature and q-point across different energy ranges into one master scan; monitor
   correction. The user does all the analysis.
2. **In-flight analysis** — ISAR analyzes as data arrives: fit a DHO across a phonon scan
   automatically, and as new scans land at the same q-point, fit there too.
3. **Full campaign** — ISAR runs the instrument itself, under top-level instructions from
   the user.
Consequence for architecture: features should compose explicitly per mode rather than ride
as side effects of lower-level commands, because no user invokes those commands directly.

## Entries 2026-08-20 — from the documentation-rebuild grill

### The audience test (→ preamble, or "Auditing" as a sibling concern)

Distinct from external auditability (already ruled in the body's Auditing section): the OPERATOR
must be able to defend the methodology. "If I am placed before an audience after presenting on
ISAR, and they ask me questions, what can I explain about it? Why did we make the choices we
made?" It is acceptable that an AI built a detail and the exact history is forgotten — it can be
checked and understood — but the top-level points must be knowable. Canonical example: "when we
do a fit, we can do Poisson or Neyman or Baker–Cousins, and I have to understand why we chose
Baker–Cousins and what that meant for us, and that it is implemented and what it is doing within
ISAR." Documentation consequence: rationale for statistical-methodology choices is first-class
and must live where the operator will find it; rationale for steering choices is not ("the scan
is a scan").

### What ISAR is (→ preamble)

"It is a system for driving a campaign. It's a system for ensuring that you are obtaining
maximum information from your beam time — and it heavily relies on knowing how it works in
order to prove anything there."

### Usable by others, not self-certifying (→ "Operator experience" or preamble)

Other people should eventually use ISAR, and the how-to-use-it door is a standing check on the
product: "we are making it something that people can use, not just something that can run on
its own and prove to itself what it does in a circular logic."

### Fits are the human-readability layer, not the proof layer (→ "Data Analysis" or the hypothesis-map section)

2026-08-20, from the walkthrough: "Being able to fit e.g. a phonon dispersion or linewidth *is*
a very useful thing, but I would put this into a different, later step: making the data
human-readable. Pure intensity charts are not that convincing or easy to parse; being able to
have ISAR prove by intensity↔hypothesis and then later produce a detailed analysis for human
consumption is still very helpful." Refines the hypothesis-map hierarchy above: the map is
challenged by intensity; parametric fits are a downstream *presentation and analysis product*
for humans, not what the proof rests on.

### Building now; release discipline later, deliberately (→ "Future direction")

ISAR is unreleased and has no external users; justifying methodology changes to outsiders is
not yet required. When people start using it, releases, changelogs, and change-justification
become a deliberate, owned step — not something to backfill silently now.