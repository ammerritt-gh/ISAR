# AP3 global branch-evidence failure

**Status:** historical evidence; Phase 0 repair implemented in focused tests, live replay pending.

AP3 began near the H=2 zone centre, then jumped to H=3 and worked backward. The connected rising
mode was initially only an acoustic candidate, became labelled optic at high q, and retained that
character after it joined the low-q measurements. The frozen `zone_period=2.0` already makes H=3 a
zone boundary for this path, so the immediate failures were trajectory-local character assignment
and acquisition value that ignored uncertainty across unmeasured links—not an H=3/Γ fold error.

The repair recomputes character over all authoritative pooled trajectory evidence, applies soft
acoustic/optic physics across the trajectory, and discounts classification information across
unmeasured gaps without forbidding distant probes. The unrelated finite-energy feature at H=2 is
not acoustic delivery. When the connected low-q trajectory supports a zero intercept, H=2 is instead
recorded as an `inferred_unresolved` structural endpoint and excluded from fitted delivery, prior
admission, and the production-precision denominator.

Automatic Bravais/space-group derivation of zone centres and boundaries is deferred; no geometry
schema change belongs to this correctness repair.
