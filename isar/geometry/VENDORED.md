# isar/geometry — what is vendored, from where, and how to carry it over

> **Status:** live
> **Authority:** canonical for the provenance of `isar/geometry/` and its carry-over procedure

`isar/geometry` is a Qt-free, numpy-and-stdlib copy of TAVI's crystal geometry plus a stateless
port of TAS_MCP's feasibility layer. ISAR never imports `tavi`; it carries a copy, and this file
is where the next carry-over starts.

- **TAVI revision vendored:** `010cec6d` (TAVI `main`, carried over 2026-10-01; first vendored
  2026-07-11).
- **Parity fixture:** `tests/geometry/tavi_parity_010cec6d.json`, TAVI's own answers on seeded
  inputs, checked by `tests/geometry/test_orientation.py::test_parity_with_tavi_010cec6d`.
  Regenerate it by hand, never from the suite, in an environment that runs TAVI:

  ```
  micromamba run -n tavi-dev python tests/geometry/make_tavi_parity.py --tavi <TAVI checkout>
  ```

  The generator refuses unless the checkout's HEAD is the vendored SHA and the vendored sources
  and instrument descriptors are clean.

## Modules

Every module carries a provenance header; the adaptations below are the complete list, one line
each, and are re-applied on every carry-over.

| ISAR file | Source | Adaptations |
|---|---|---|
| `orientation.py` | TAVI `tavi/orientation.py` | `tavi.tas_geometry` import made relative. |
| `tas_geometry.py` | TAVI `tavi/tas_geometry.py` | `stt_from_q_norm`, `solve_sample_angles` and `solve_instrument_angles` refuse a `sense_sample` other than -1/+1 (TAVI's `stt_from_q_norm` multiplies by any value). |
| `sample_mount.py` | TAVI `tavi/sample_mount.py` | `reciprocal_basis_tas` refuses non-finite or non-positive a, b, c. |
| `reciprocal_space.py` | TAVI `tavi/reciprocal_space.py` | A shared `_cell_volume` guard makes both HKL->Q and Q->HKL refuse a degenerate or invalid cell (NaN-safe `not (V > 0)`); TAVI checks only Q->HKL. |
| `ub_matrix.py` | TAVI `tavi/ub_matrix.py` | `tavi.*` imports made relative, keeping only the names the module still uses; the Training section (`generate_training_exercise`, `encode_training`/`decode_training`, `grade_alignment`, the obfuscation key and its `base64`/`struct` imports) dropped — ISAR grades no exercise. ISAR's own constrained `refine_lattice_from_peaks` (uniform-scale fallback, `method` key) stands in place of TAVI's block from `_G_STAR` to `refine_lattice_from_peaks`. |
| `instruments.py` | TAS_MCP `tas_mcp/instruments/puma.py` | Flattened into one module; `bragg_two_theta_deg` refuses a `which` other than `"mono"`/`"analyzer"`; `tas_goniometer` from TAVI `instruments/descriptor.py`, over `StageAxis` (no corrections or zero errors). |
| `feasibility.py` | TAS_MCP `tas_mcp/feasibility.py` | Vendored primitives replace the tavi shim; stateless (per-call `SampleState` and explicit fixed-k/instrument); `accessible_range` grid and contiguous-run hardening. |
| `convert.py` | TAS_MCP `tas_mcp/convert.py` | Vendored primitives replace the tavi shim; stateless; `bragg_two_theta` tolerates float overshoot at the Ewald boundary. |
| `plane.py` | TAS_MCP `tas_mcp/plane.py` | Vendored primitives replace the tavi shim; stateless. Its mounting rule (first vector along x, plane normal along +y) is TAS_MCP's, not TAVI's `u_from_plane`. |
| `sample_state.py` | TAS_MCP `tas_mcp/session.py` | Only the `SampleState` value object kept; `ub()` rebuilds B when the lattice changes, keeping U. |
| `units.py` | TAS_MCP `tas_mcp/units.py` | Direct energy input refuses negative energies. |
| `epochs.py` | ISAR's own | — |
| `__init__.py` | ISAR's own | — |

## The sample stages

TAVI's stage per instrument at `010cec6d`: turntable `A3` about y (free), lower arc `sgl` about
x, upper arc `sgu` about z (`tas_goniometer`). `Sense.LEFT` is +1, `Sense.RIGHT` -1. ISAR has a
feasibility profile only for PUMA; the other three stages are test data in
`tests/geometry/test_orientation.py`.

| Instrument | Arc travel | Sample sense | TAVI source |
|---|---|---|---|
| PUMA | ±20° | -1 | `instruments/puma/plugin.py:248` (sense), `:313` (`tas_goniometer(20.0)`) |
| IN8 | undocumented (unlimited) | +1 | `instruments/in8/plugin.py:159`, `:252` (`tas_goniometer()`) |
| IN12 | ±20° | +1 | `instruments/in12/plugin.py:261`, `:375` (`tas_goniometer(20.0)`) |
| PANDA | ±15° | +1 | `instruments/panda/plugin.py:192`, `:307` (`tas_goniometer(15.0)`) |

PUMA's ±20° is TAVI's own choice (the MLZ page's standard goniometer; ±15° amagnetic is the
alternative); a change in TAVI is a one-line change in `instruments.py`.

## Carrying over a newer TAVI

1. Diff each TAVI source in the table against the recorded SHA
   (`git -C <TAVI> diff 010cec6d <new> -- tavi/orientation.py tavi/tas_geometry.py ...`) and
   read what changed.
2. Copy each changed TAVI file over its ISAR file, then re-apply that row's adaptations and the
   provenance header (new date and SHA).
3. Check the copy: a diff of each ISAR file against the new TAVI file shows only the header and
   the adaptations listed here.
4. Update the SHA in this file, the generator's `SHA` and the fixture name, then rerun the
   generator against the new checkout.
5. Run `tests/geometry`, `tests/test_qt_free_core.py` and `tests/docs/test_doc_drift.py`, then
   the not-slow lane.
