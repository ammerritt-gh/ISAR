# Schema Landscape — Inelastic Scattering Data Formats, Scan Types & Metadata

**Status:** Research reference. Informs the Phase 2 schema (`docs/archive/PHASE_2_DESIGN.md` §3, §5, §6).
**Note:** two claims here were later **corrected by real files in `test_data/`** — BL43LXU writes SPEC (not
an undocumented custom format), and IN8 ships `.nxs` NeXus twins beside each numor. Corrections are marked
inline with “**[test_data]**”. See `archive/PHASE_2_DESIGN.md` §6 for the consolidated, corrected instrument table.
**Purpose:** Survey scan types, per-scan metadata, sample environment, and file formats across IXS and
TAS facilities so the ISAR normalized schema has its extension points in the right places — i.e. so the
schema does **not** need a rewrite to support scan types and instruments beyond the current
constant-Q-vs-energy focus.

---

## TL;DR

- **Adopt a "generalized scan axis" model.** Represent every scan as *N* measured points × *M* named,
  typed scan columns (any combination of energy, H/K/L, |Q|, an angle, or a sample-environment
  variable) plus a measured-signal block (counts + error) and a per-point environment/log table.
  NeXus `NXtas` and ORNL SPICE's "everything-is-a-motor" model both validate this. Constant-Q-vs-energy
  is just the special case *M*=1, axis=energy.
- **Build five extension points from the start:** (1) a list-of-scan-axes instead of a hardcoded energy
  axis; (2) a typed-but-open sample-environment/log container (key→series, with units, setpoint, sensor
  id, validity mask); (3) a normalization block carrying **both** monitor counts and counting time plus
  the counting mode; (4) a multi-spectrum signal block (one scan → many spectra, as on HERIX's nine
  analyzers); (5) a per-facility, per-version parser registry that detects format and dispatches.
- **Don't over-build:** full N-dimensional MD/event data (Mantid MDWorkspace / Horace SQW), live event
  streaming, and a universal 3-D instrument-geometry model are **not** needed for during-beamtime triage
  of step-scanned 1-D spectra. Keep signal/axes as arrays and keep provenance so they *could* be added,
  but defer them.

---

## 1. A community-blessed scan abstraction already exists

The NeXus `NXtas` application definition stores every quantity that can vary during a scan as a parallel
array of length `nP` (number of points): `qh[nP]`, `qk[nP]`, `ql[nP]`, monochromator `ei[nP]`, analyser
`ef[nP]`, rotation/polar angles `[nP]`, and detector `data[nP]`. NXtas is explicitly "for the trademark
scan of the TAS, the Q-E scan," and defers alignment scans to the generic `NXscan`; its `probe` field
accepts `neutron | x-ray`, so **IXS reuses it** (there is no separate `NXinelastic`/`NXixs`).

ORNL's SPICE control system (HB-1/HB-3) generalizes further: all changeable quantities — physical
motors, pseudomotors, sample-environment quantities, flipper voltages — are **all treated as motors**,
and its scan structure "allows any combination of drives and counts to be converted into a properly
formatted scan."

The 2002 NeXus inelastic proposal (Beckmann et al., from PANDA/PUMA at FRM-II) states the rule
explicitly: store all parameters that might change during a scan in arrays; one NXdata group's
scan-variable array may hold ki values while another's holds temperature; it is the data-processing
software's job to track which is which.

**Takeaway:** "list of scanned columns + measured signal" is the single most important borrowable idea.
Keep the scanned variable as one element of a list rather than a privileged `energy` field.

---

## 2. The XYE layout is the lowest common denominator — but X must be a named, typed axis

- Mantid's `MatrixWorkspace` is fundamentally X (axis, e.g. energy or H), Y (counts/signal), E (error)
  per spectrum.
- ESRF ID28's reduction tool `addIXS` outputs an xye file where x is energy transfer, y is intensity
  normalized to the monitor ("ione"), e is the √ error.
- SPEC files (APS Sector 30, legacy ID28) are column ASCII where the first column is always the primary
  scanned item and trailing columns are monitor/counter.

So an XYE-with-named-X core is correct — but X must be a named, typed axis
(`energy | H | K | L | |Q| | angle | temperature | field`), never implicitly energy.

---

## 3. Normalization must carry both monitor AND time, plus the counting mode

TAS/IXS instruments count in one of two modes: **fixed-monitor** (count until a beam monitor reaches a
preset) or **fixed-time** (count a preset number of seconds). SPEC stores both columns; which one is the
fixed preset flips with the mode. The monitor — a low-efficiency beam monitor sampling incident flux —
is the physically correct flux normalizer.

**Combine-before-divide (error-safe merging).** Per Mantid's MDNorm guidance: do not normalize by
monitor and then add experiments; instead add raw counts together, add monitors together, and only then
divide. Errors are Poisson (σ = √N on raw counts) propagated through the division.

ISAR's normalization block must store: `counting_mode` (monitor|time), `monitor[nP]`, `time[nP]`, the
preset value, and a `monitor_correction` flag (thermal-TAS monitors have a known λ/2 contamination that
is routinely subtracted).

---

## 4. The energy axis is absolute on a fixed-kf TAS but derived/calibrated on IXS

- **TAS:** energy transfer is computed deterministically from ki and kf; fixed-kf (or fixed-ki)
  operation makes the absolute energy axis known from configuration (e.g. HB-3 at fixed Ef = 14.7 meV;
  IN8 at fixed kf = 2.662 Å⁻¹). The schema must record geometry: fixed-ki vs fixed-kf, the fixed k/E,
  mono and analyzer crystal+reflection (PG(002), Si(111), Cu, Heusler for polarized), collimations,
  filter.
- **meV-IXS:** energy is scanned by varying the **monochromator temperature** (thermal expansion of the
  Si backscattering crystal, ≈17 mK/meV near RT at 21.7 keV, calibrated against a known phonon line).
  The file may store **temperature as the abscissa, not energy**, and the **energy zero must be
  calibrated from the elastic line**. ID28's addIXS converts monochromator temperature to energy via the
  Si reflection order.

**Note for ISAR:** facilities differ on *what the abscissa column already is*. APS Sector 30 exposes a
converted energy column (`HerixE`); ESRF ID28 exposes raw monochromator temperature. So `raw_abscissa_kind`
is genuinely per-card (`energy` for S30; `temperature` for ID28), and the schema needs a
"raw abscissa → calibrated energy" transform with a fitted elastic-line zero offset as first-class
metadata — not a hardcoded energy column. (The actual zero *fitting* is a later phase; the schema only
has to carry the fields.)

---

## 5. NeXus provides a typed-but-extensible sample-environment model — borrow its structure

- `NXsample` carries `temperature` (may be a scanned variable), `magnetic_field`, `electric_field`,
  plus pressure/stress/strain fields, each an array whose length equals the number of values in that
  field.
- `NXenvironment` holds richer external-stimulus setups (applied pressure, gas phase/pressure,
  electric/magnetic/mechanical fields, temperature) as `NXsensor` groups.
- `NXsensor` has an enumerated-but-open `measurement` type (temperature | pH | magnetic_field |
  electric_field | current | conductivity | resistance | voltage | pressure | flow | stress | strain |
  shear | surface_pressure, **or a custom value**), plus `value` (setpoint/average), derivatives, and
  `run_control` (is acquisition synchronised to this quantity).
- For time-series logging NeXus uses `NXlog` (value + time arrays).
- **Mantid** implements the pragmatic version as **sample logs**: arbitrary key→(time-series) values on
  the workspace `Run` object (`getLogData()`, `addProperty('sample.pressure', …)`), and natively flags
  the disconnected/unreliable-sensor case via invalid-data masks on logs.

**Recommended container (hybrid):** promote the two common variables (temperature, magnetic_field) to
typed fields with sensor/setpoint substructure, and back everything else with an **open `logs`
dictionary** keyed by name — each entry `{values[nP] or time-series, units, setpoint, sensor_id,
role(scanned|monitored|constant), valid_mask}`. Custom electric field, uniaxial/hydrostatic pressure,
strain, laser illumination, gas loading all drop into `logs` with no schema change, and any of them can
be promoted to a scanned axis by reference from `scan_axes`.

---

## 6. Native file formats per instrument — and they drift

- **APS Sector 30 HERIX (IXS):** SPEC ASCII; instrument logic lives in SPEC `.mac` macros (the APS
  temperature macro carries edits dated 2004–2012). HERIX has **nine analyzers, ~0.95° (2 nm⁻¹) apart**,
  ΔE/E ≈ 6×10⁻⁸ at 23.724 keV, Qmax = 72 nm⁻¹ (Said et al., *J. Synchrotron Rad.* 27, 827–835, 2020) —
  one scan yields nine spectra at nine Q simultaneously. Sample environment includes a Razorbill
  uniaxial-strain cell (±6 µm at RT, capacitance-measured), static fields, and high pressure, 5–1000 K.
- **ESRF ID28 (IXS):** Native raw format **ASCII SPEC**; nine analyzer signals are separate per-detector
  columns; addIXS emits one file per analyzer (`<spec>_<scan>_A<det>.dat`). Energy stored as
  monochromator temperature; monitor "ione"; each analyzer needs its own temperature offset (different
  thermistor contact resistances). Per-analyzer resolution differs (~3 meV avg, e.g. 2.96(7) meV for
  analyzer 2). ESRF-wide BLISS writes NeXus/HDF5, but **no public doc confirms ID28's IXS spectrometer
  has migrated** — assume SPEC ASCII for current and legacy data.
- **SPring-8 BL43LXU (IXS):** ~~**Custom in-house acquisition software**; on-disk format not publicly
  documented~~ — **[test_data] corrected:** the real file `specixs_22Nov05` (Nov 2022) is **plain SPEC**
  (`#F/#E/#D/#O/#S/#G/#P/#Q` blocks), so BL43 is handled by the shared SPEC base reader with a per-card
  column map like S30/ID28, not a bespoke parser. Large analyzer array (described as 6×4 → now 28; a 2024
  experiment used 7×4 = 28 analyzers, 28 momentum transfers per scan), resolution ~0.75–6 meV.
- **ILL IN8 (TAS):** ILL "numor" format (NUMber Of Run; 6-digit zero-padded filenames), historically
  ASCII/TAPDAT-derived. **[test_data]:** the real files confirm the classic numor ASCII (header blocks
  `INSTR/EXPNO/COMND/POSQE/PARAM/VARIA/ZEROS`, then a `DATA_:` column table `PNT A3 M1 M2 TIME CNTS …`),
  **and each numor has a `.nxs` NeXus twin** — so IN8 already exhibits the format-versioning case (two
  on-disk formats for the same scan). Tools: LAMP, Steffens' MATLAB TAS routines, Rplot. Newer ILL
  instruments emit NeXus/HDF5 (IN5; D17 from Jan 2023). Format still drifts (Rplot: "adapted to new D3
  Numor format"). IN8 was rebuilt (new "ThermES" secondary spectrometer) and runs as single-detector or
  in IMPS multi-analyzer mode (nine (Q,ω) points via a nine-blade Ge(111) analyzer + PSD).
- **HFIR HB-3 (TAS):** ORNL **SPICE** format — column ASCII with a structured header, LabVIEW-written
  (also HB-1). UB-matrix formalism generalized for inelastic scans; all environment quantities as
  motors. Modern reader/reducer is **TAVI** (Triple Axis data Visualization Toolkit,
  `github.com/neutrons/TAVI`, active through 2026), working with SPICE data and an HDF5 representation.

---

## 7. The standards landscape, assessed for borrowability

- **NeXus** (`NXtas`, `NXspe`, `NXsample`, `NXenvironment`, `NXsensor`, `NXlog`, `NXdata`, generic
  `NXscan`): the richest, most directly mappable model. `NXtas` is the canonical Q-E scan; `NXspe` is for
  reduced direct-geometry S(Q,E). **No `NXinelastic`/`NXixs`** — IXS reuses `NXtas` with `probe="x-ray"`.
  Recommended **vocabulary target** for ISAR's schema even if ISAR keeps an in-memory Python object.
- **Mantid:** `MatrixWorkspace` (XYE + spectra), `MDWorkspace` (N-dim event/binned for mapping), sample
  logs (arbitrary metadata), the `Run` object, versioned instrument-definition files (IDF, XML). Gold
  standard for the signal+logs+history triad and for backward compatibility.
- **TAS analysis ecosystem:** Takin (open-source; Cooper-Nathans/Popovici resolution; convolution
  fitting), ResLib/ResLibCal (MATLAB), Python ports NeutronPy etc. — resolution/convolution engines
  ISAR's later DHO-convolution phase can call rather than reimplement; they consume the (Q,E) point +
  instrument config the schema must already carry.
- **DAVE/MSlice** (NIST): mature INS reduction/visualization but TOF-centric and IDL-based — less
  relevant as a schema source.
- **SciCat / PaN (ExPaNDS, PaNOSC, PaNET ontology):** SciCat is a metadata *catalogue* for FAIR
  findability, not a scan data model. Relevant only if ISAR later pushes metadata to a facility
  catalogue; PaNET offers controlled technique vocabulary.

---

## 8. Parser/schema versioning — Mantid's pattern transfers directly

Mantid resolves which instrument geometry to apply by a **valid-from date** embedded in versioned IDF
files; if a file embeds its own definition that is used, otherwise it matches by date — which requires
the data file to record when it was collected. NeXus achieves forward/backward compatibility via its
self-describing hierarchy, a `NeXus_version` attribute, and additive application definitions (new
optional fields don't break old readers).

**Transferable pattern for ISAR:** detect `(facility, instrument, format_version)` from file
content/headers/date, dispatch to a parser registered on that tuple, and stamp every normalized scan with
the parser version and source format for provenance.

---

## 9. Forward-compatibility — the principal axes of variation

1. **Dimensionality of the scan** (1 → N scanned axes) — handled by the `scan_axes` list.
2. **Identity of the scanned variable** (energy | Q-component | angle | environment) — axis `kind`.
3. **Number of spectra per scan point** (1 → many) — HERIX 9, ID28 9, BL43LXU 28, IN8-IMPS 9. Handled by
   the `n_spectra` dimension + per-spectrum coords. **Build now** (the named instruments already need it).
4. **Polarization/spin channels** (polarized INS, Heusler optics, XYZ/spherical polarimetry as on HB-1) —
   model as a `channel` coord `{spin_in, spin_out, flipper_state}`. Reserve now (length-1); populate later.
5. **Energy-axis provenance** (absolute TAS vs elastic-calibrated IXS) — a flag/calibration block.
6. **Environment-ramp independent variable** (T or B ramp) — covered by `kind=environment`.
7. **Time/pump-probe** (delay as an axis) — another axis kind + a `delay` log; defer implementation.
8. **Multi-sample / sample-array** — an optional `sample_id` coord; defer.

---

## 10. Recommendations

**Build into v1 (the named instruments already require these):**
1. **Generalized `scan_axes` list** (named, typed, role-tagged) instead of a hardcoded energy column.
2. **Multi-spectrum signal block** (`value[nP, n_spectra]`, `error`, per-spectrum coords).
3. **Normalization block with both monitor and time + `counting_mode`**, combine-before-divide
   semantics, Poisson √N error propagation from raw counts.
4. **Hybrid environment container**: typed temperature + magnetic_field (multi-sensor, setpoint,
   validity mask) plus an open `logs` dict for arbitrary parameters.
5. **IXS energy-calibration block** (raw_abscissa_kind, T→E transform, elastic-line zero offset,
   resolution FWHM) and a fixed-ki/kf geometry block for TAS.
6. **Parser registry keyed on (facility, instrument, format_version)** with content/date-based detection
   and per-scan provenance stamping (source format, parser version, schema version).
7. **Map field names onto NeXus vocabulary** (NXtas/NXsample/NXsensor terms) even in the in-memory
   object, so a future NeXus export or SciCat push is mechanical.

**Defer (design so they slot in; don't implement):**
- Polarization/spin-channel dimension — reserve the coordinate, leave it length-1.
- Pump-probe delay axis and multi-sample id — additional axis kinds / coordinates.
- Full N-dimensional MD/event representation (Mantid MDWorkspace, Horace SQW) and live event streaming.
- A full constructive-solid-geometry instrument model (Mantid IDF) — ISAR needs scalar geometry
  parameters (fixed k, crystals, collimations) for resolution, not 3-D detector geometry.

**Triggers that would change these:** routine mesh/map scans → promote multi-axis handling, consider a
thin MD layer; polarized HB-1 data → activate the spin-channel dimension; a facility format migration
(ID28 → BLISS/HDF5, ORNL TAS → TAVI HDF5) → add a parser keyed to the new tuple without touching the
schema.

---

## 11. Caveats

- ~~**BL43LXU's native file format is not publicly documented.**~~ **[test_data] corrected:** BL43LXU
  writes **SPEC** (`specixs_*`), handled by the shared SPEC base reader; the only per-facility unknown is
  the column/`#V` mapping, resolved by inspecting a real file. Still worth confirming with staff if a new
  beamtime changes the acquisition setup.
- **ID28's migration to BLISS/HDF5 is unconfirmed in public sources.** Assume SPEC ASCII; design the
  registry so an HDF5 ID28 variant can be added.
- **Format drift is real and continuous** (ILL numor versioned changes; APS SPEC macros 2004–2012; IN8
  rebuilt to ThermES). Several-year-old data may predate current conventions — the per-version parser
  registry is essential, not optional.
- **`NXtas` is a thin application definition** (standardizes the Q-E scan arrays, not the full
  environment/polarization breadth); treat it as a vocabulary anchor, supplement with
  NXsample/NXenvironment/NXsensor/NXlog.
- **Monitor normalization carries instrument-specific corrections** (thermal-TAS λ/2 contamination is
  routinely subtracted); store a `monitor_correction` flag rather than assume the monitor column is clean.
- **Resolution must be stored per spectrum, not per scan**: for multi-analyzer IXS each analyzer has a
  slightly different resolution (ID28: 2.96(7) meV for analyzer 2 vs ~3 meV average), which matters for
  the later DHO-convolution fitting phase.
