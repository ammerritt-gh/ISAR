# AP1 acoustic branch-mapping deadlock

> **Lifecycle:** Historical evidence
> **Provenance:** hand-written
> **Verified against code revision:** `d0ea1df` (2026-07-25)
> **Superseded by:** the current contract named in [`campaigns/README.md`](README.md). A dated record of what happened, never a statement of current behaviour (DECISIONS D3).

**Date:** 2026-07-20
**Role:** historical campaign evidence; the live contract is O25 and the steering documentation.

## What happened

AP1 completed its survey, then repeatedly acquired H=2.30. The pooled fit contained two real modes at
approximately 2.709 and 6.848 meV. Their target identities were almost evenly split (acoustic posterior
about 0.481, optic about 0.473), so withholding authoritative target `omega0` was correct. The failure
was downstream: the stopping reducer treated that null delivery as no measurement, and the GUI skipped
the fit before reading its candidates. H=2.30 therefore appeared unmeasured to both acquisition and the
operator even though its spectrum had been fitted successfully.

The nominal 12-production-batch setting did not change acquisition strategy. Increasing dwell was then
repeatedly clamped to the live 100,000,000-neutron-per-point instrument limit, so further same-q scans
could not execute the requested exposure increase. With no bounded ambiguity terminal, the global
iteration limit was the only brake.

## O24 attempted resolution

O24 specified a split between pooled evidence, branch identity, and target delivery. The AP1-like
pooled master was intended to mark H=2.30 measured and retain both modes for steering and display
while target `omega0` remains null.
Candidates render as open markers without certifying whiskers or a dispersion line. Named acoustic maps
now use connected path tracking from the nearest predicted-resolvable anchor, rather than independent
lowest-energy selection at every q.

The production count was intended to be a same-q strategy ceiling. Reaching it was meant to force
an untried adjacent differentiation probe; only configured bridge/revisit attempts may return to
the ambiguous frontier.
Two repeated live-limit clamps (or the existing no-progress interval diagnostic) establish exposure
saturation. A saturated frontier with no remaining connected classification route terminates
automatically as `incomplete`, preserving the candidates and reporting `branch_ambiguity` and
`frontier_blocked`.

## Regression coverage

The O24 deterministic fixture reproduces the two-candidate/null-delivery state and checks that it is
measured, visible, non-certifying, and excluded from prior admission. Additional tests cover acoustic
anchor selection and contiguous tracking, the strategy ceiling, live-limit saturation, remaining-probe
behavior, and the final incomplete outcome. Legacy frozen objective envelopes retain their selector;
new named-branch goals receive path tracking by default.

## Second replay: connected anchor repeated (2026-07-20)

The next temporary campaign confirmed that objective compilation was no longer the problem. The named
acoustic map compiled to `tracked_branch` and deliberately skipped a full survey: it began at H=2.15,
the nearest predicted-resolvable acoustic anchor. Persisted scans, pooled members, the plot, and the log
all identify H=2.15. The master browser alone displayed H=2.1 because its shared q formatter used two
significant digits.

Acquisition then repeated that anchor for one quicklook and twelve production attempts. This was not a
statistics problem. The final pooled master retained two precise candidates near 1.411 and 6.420 meV;
the acoustic assignment probability was approximately 0.862 with assignment entropy approximately
0.484, and the conservative diagnostic half-width was about 0.019 meV against a 0.5 meV tolerance.
Identity was ambiguous, but the spectral evidence was already adequate. The connected-anchor exception
nevertheless routed every unconfirmed anchor to more same-q exposure and did not consult the general
evidence-readiness rule until the strategy ceiling was exhausted.

The intended repaired frontier has explicit evidence states. Missing or imprecise candidates may receive
more same-q exposure. Precise ambiguous candidates immediately select one of the two unvisited adjacent
points by expected classification information, with path order breaking ties. The whole path tracker is
rerun after that probe; the anchor can be revisited only after a neighbor has produced fitted
candidate/track evidence and within the bridge allowance. A visited-only or submitted-only quicklook
does not unlock the revisit. The transition is logged with source and probe q, candidate precision,
target probability, entropy, and reason. The production ceiling remains a safety limit, never a quota.

The GUI now formats reciprocal coordinates with six significant digits and suppresses trailing zeroes,
so H=2.15 remains H=2.15 in both master and member rows while H=2.10 is compactly shown as H=2.1.

## AP2 falsification of O24 (2026-07-20)

AP2 used an objective tolerance of **0.5 meV**. Its actual proposal sequence was:

1. H=2.15 quicklook;
2. H=2.10 quicklook;
3. H=2.05 quicklook;
4. H=2.10 production, repeated **thirteen times**;
5. H=2.00 only after those repeats.

Thus AP2 did buy three initial path points, but it still collapsed onto the middle point and used
production submissions as repeated identity work. The sequence directly falsifies O24's Implemented
claim: the production ceiling did not force informative breadth, and unchanged branch evidence could
still emit an equivalent same-q proposal.

The investigation also exposed three coupled identity errors. First, the off-centre acquisition seed
could drift from its persisted H=2.15 purpose toward a locally selected candidate/frontier point.
Second, dispersion transitions were being interpreted relative to that acquisition anchor rather than
the physical reduced-q origin declared by the branch period. For an acoustic hypothesis, it is the
physical zone centre—not whichever point was easiest to resolve first—that supplies the soft
expectation that energy approaches zero. Third, target/rival handling was not consistently role-aware:
logic written around the acoustic/optic ordering could suppress or promote the wrong role when the
declared target changed. The approximately 5.98 meV feature at H=2.00 is therefore retained as unordered,
optic-like evidence; it is not acoustic delivery merely because it was locally available.

O25 supersedes only O24's false implementation status, not its intended evidence/identity/delivery
separation. Phase 0 now persists acquisition seed and physical origin separately, reruns joint identity
only after authoritative pooled-master changes, and requires newer fitted neighboring evidence before
an equivalent same-q identity revisit. Otherwise the controller must broaden by branch-information
value or report `branch_ambiguity` / `frontier_blocked` when no feasible route remains. This repair is
**in progress** pending deterministic AP1/AP2 replay verification; the Smarter Steering A → B → C
sub-phases remain Planned.
