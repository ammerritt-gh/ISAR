# DESIGN_GOALS inbox — APPEND-ONLY

> **Lifecycle:** Current
> **Provenance:** hand-written

Candidate top-level objectives for `DESIGN_GOALS.md`, drafted by agents FOR the user.
Protocol: **agents may only APPEND** new candidates here — after checking the item is not
already in `DESIGN_GOALS.md` or this inbox. Agents never rewrite, reorder, or remove entries;
the USER drains entries into `DESIGN_GOALS.md` (in their own words) or deletes them, on their
own. This file is an inbox, not a contract.

## Entries 2026-08-13 — from the SCENE-4 planning grill

## The hypothesis map and the QE map (→ its own top-level section, or "Data Analysis")

ISAR's chain of authority: the hypothesis map (our scientific model) → the scientific question
(what we care to challenge in that map) → the measured S(Q,ω)/QE space (what we actually
measured — ALL counts go into the map) → fits derived on top. **Counts are authoritative; fits
are not.** A fit can always be redone another way over the same region of QE space; a bad fit
never erases, hides, or blocks anything. Distinct from that: a *broken scan* (the counts
themselves untrustworthy — machine fault) is quarantined, the map emptied there, the failure
recorded, rescan attempted when possible — with an offramp so a persistently failing region
cannot trap a campaign.

## Hypothesis-driven at the core (→ "Phenomena Hypotheses")

The fundamental question is always: *what is our confidence in this model versus our data?*
Fitted quantities (a phonon energy) are instrumental to higher-level models — the real object
may be "the dispersion changes in this way at these temperatures," with individual energies only
in service of that. Models form a hierarchy, and completion of any measurement goal is a
statement about model confidence; a parameter tolerance (±0.1 meV) is the degenerate special
case, not the template. Example of the right shape: "the acoustic branch reaches the zone
centre" is answered by extrapolation under a declared prior (linear near ZC), with confidence
that grows as fits tighten or scans approach the fold — and with an honest ceiling: behaviour
*at* the ZC is untestable under the elastic line.

## Campaign regimes (→ "Automated Scans")

User intent spans a spectrum, and it is on the user to state it:
1. **Open discovery** — "scan this area, find phenomena, pin them down, identify them, fit to
   ISAR standard defaults." Unexpected in-domain findings are part of the goal.
2. **Strict targeted** — "map *this*; anything else only in service of that goal (background,
   calibration)." Possibly multiple targets with an order of importance.
3. **Guided** — region + expectations + bounded freedom; everything between the extremes.
Eventually ISAR needs a real campaign *writer* (a goal compiler with judgment), not just a
parser.

## Never set-and-forget (→ "Automated Scans")

Users can be prompted mid-campaign for permissions and campaign updates (widen the domain?
abandon a failing region? keep spending on an unresolvable question?). Prompts are non-blocking
with a declared default — silence chooses the default, the beam never idles waiting on a human —
and the consequences of answering or not answering are on the user. Hard blocking gates are
opt-in ("ask before ever leaving the region, and wait").

## ISAR is always measuring (→ "Automated Scans", extends Pause/Failure States)

Completing a campaign is not a reason to stop measuring. The ideal: enough campaigns queued to
run through without ending, or an open-ended finishing campaign that just collects useful data.
Only user intervention idles the beam; "stop when this campaign is done" is itself a user
choice.

## Freedom to leave the beaten path (→ "Automated Scans")

A campaign is a scientific objective, not a fence. By default ISAR may take confirmation scans
off the nominal path — the −E side, −k against +k, an equivalent point in another Brillouin
zone — when it needs corroboration; symmetry equivalence is what makes such scans usable as
evidence about the same phenomenon. Users *may* lock a campaign down with hard limits, but a
pre-computed safe zone is not required by default.

## The stale-model contract (→ "Automated Scans")

ISAR accepts that it usually does not have the perfect next scan ready. Model A chooses scan 2;
scan 1 lands; scan 2 starts while the update to model B (which chooses scan 3) computes. How
much staleness to accept — compute between every scan, run one behind, or batch scans on a very
fast machine — is an **operator** trade-off exposed as configuration, never fixed inside ISAR.
Every scan decision records which model state chose it.

## Even blind measurement is hypothesis-driven (→ "Automated Scans" or "Phenomena Hypotheses")

A first-look survey is not a uniform grid. ISAR knows what an acoustic phonon *is* (strong and
linear near the zone centre, weaker and less predictable at the boundary, hidden too close to
the Bragg peak — which is itself a modelled phenomenon to avoid), so it looks where the
hypothesis says the phenomenon is findable, and each fix feeds extrapolation to the next
placement. A truly prior-less sparse map is a legitimate *user choice*, not the default.

## Sample environment is an open parameter set (→ "Sample Knowledge")

Temperature, magnetic field, pressure — conditions are first-class, general, and never
hardcoded to one variable. Observations at different conditions (or from different samples) are
different thermodynamic scenes: they never blend by default, and inference across a condition
boundary happens only through a model that declares and prices that dependence (e.g. a
phase-transition hypothesis), not through ordinary spatial adjacency.

## Instrument cost model (→ "Automated Scans", future)

Real spectrometers pay real time to move (motor speeds, configuration changes). Scan selection
should eventually weigh information value against instrument-motion cost; until then, nearness
to the current configuration is a stand-in tiebreak.
