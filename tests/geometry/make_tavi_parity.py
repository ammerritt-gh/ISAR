"""Write ``tavi_parity_010cec6d.json``: TAVI's own answers for ISAR's parity test.

Run by hand, once per carry-over, in an environment that runs TAVI (never by
the suite; the name has no ``test_`` prefix so pytest does not collect it)::

    micromamba run -n tavi-dev python tests/geometry/make_tavi_parity.py --tavi <TAVI checkout>

It refuses unless the checkout's HEAD is the vendored SHA and the vendored
sources (plus the instrument descriptors the stages are read from) are clean.
Every input is seeded and written into the fixture beside TAVI's result, so
``test_orientation.py`` recomputes each case with ``isar.geometry`` on exactly
these numbers. Cases (isar/geometry/VENDORED.md):

* ``stage_sets``: per TAVI instrument stage, both senses and four lattices, a
  seeded true U and reflections in and past travel: the signed mount vector,
  stt, lab Q, ``solve_stage`` readouts or refusal text, the legacy triple, and
  the U ``calculate_U_from_peaks`` fits from two and three stage-record and
  legacy peaks.
* ``locked``: ``lock_plane`` for a (1 0 0)/(0 1 0) mount and the locked solve
  of in-plane and out-of-plane reflections (or the refusals).
* ``refine``: ``refine_lattice_from_peaks`` per crystal system, and the sets
  it cannot decide with their messages; ``lattice_crystal_system`` per lattice.
* ``feasibility``: the per-point stage-solve excerpt of TAVI's
  ``calculate_stage_angles`` (``instruments/tas_runtime.py`` 920-934), re-run
  here with TAVI's own primitives rather than by calling that method, on
  PUMA's stage for an in-plane, a 10 deg and a 25 deg out-of-plane reflection.
"""
import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np

SHA = "010cec6d"
OUT = Path(__file__).with_name(f"tavi_parity_{SHA}.json")
SOURCES = ["tavi/orientation.py", "tavi/tas_geometry.py", "tavi/sample_mount.py",
           "tavi/reciprocal_space.py", "tavi/ub_matrix.py", "instruments/descriptor.py",
           "instruments/tas_runtime.py", "instruments/puma/plugin.py", "instruments/in8/plugin.py",
           "instruments/in12/plugin.py", "instruments/panda/plugin.py"]
INSTRUMENTS = ("puma", "in8", "in12", "panda")
LATTICES = {
    "cubic": (4.05, 4.05, 4.05, 90.0, 90.0, 90.0),
    "hexagonal": (3.21, 3.21, 5.21, 90.0, 90.0, 120.0),
    "monoclinic": (5.10, 6.30, 7.20, 90.0, 103.5, 90.0),
    "triclinic": (5.10, 6.30, 7.20, 85.0, 95.0, 100.0),
}
K = 2.662
HKL_POOL = [(h, k, l) for h in range(-3, 4) for k in range(-3, 4) for l in (-1, 0, 1)
            if (h, k, l) != (0, 0, 0)]
# (system, the lattice refined from, the lattice the peaks come from): TAVI's
# tests/test_orientation.py REFINE_CASES.
REFINE_CASES = {
    "cubic": ("cubic", (4.05, 4.05, 4.05, 90, 90, 90), (4.0905, 4.0905, 4.0905, 90, 90, 90)),
    "tetragonal": ("tetragonal", (4.0, 4.0, 6.0, 90, 90, 90), (4.04, 4.04, 5.94, 90, 90, 90)),
    "orthorhombic": ("orthorhombic", (4.0, 5.0, 6.0, 90, 90, 90),
                     (4.04, 4.95, 6.06, 90, 90, 90)),
    "hexagonal": ("hexagonal", (3.21, 3.21, 5.21, 90, 90, 120),
                  (3.2421, 3.2421, 5.1579, 90, 90, 120)),
    "trigonal-hexagonal-axes": ("trigonal", (3.21, 3.21, 5.21, 90, 90, 120),
                                (3.1779, 3.1779, 5.2621, 90, 90, 120)),
    "trigonal-rhombohedral-axes": ("trigonal", (5.0, 5.0, 5.0, 80, 80, 80),
                                   (5.05, 5.05, 5.05, 80.8, 80.8, 80.8)),
    "monoclinic-b": ("monoclinic", (5.1, 6.3, 7.2, 90, 103.5, 90),
                     (5.151, 6.237, 7.272, 90, 104.535, 90)),
    "monoclinic-a": ("monoclinic", (5.1, 6.3, 7.2, 98, 90, 90),
                     (5.049, 6.363, 7.128, 98.98, 90, 90)),
    "monoclinic-c": ("monoclinic", (5.1, 6.3, 7.2, 90, 90, 112),
                     (5.151, 6.363, 7.128, 90, 90, 110.88)),
    "triclinic": ("triclinic", (5.1, 6.3, 7.2, 85, 95, 100),
                  (5.151, 6.237, 7.272, 85.85, 94.05, 101.0)),
}
REFINE_HKLS = [(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0), (1, 0, 1), (0, 1, 1), (1, -1, 1)]
# Sets the refinement cannot decide (TAVI's refusal test, plus the hexagonal
# (h k 0) pair that leaves c undetermined): (system, start, hkls).
UNDECIDABLE = {
    "tetragonal-hk0": ("tetragonal", (4.0, 4.0, 6.0, 90, 90, 90),
                       [(1, 0, 0), (0, 1, 0), (1, 1, 0), (2, 1, 0)]),
    "hexagonal-hk0-pair": ("hexagonal", (3.21, 3.21, 5.21, 90, 90, 120), [(1, 0, 0), (1, 1, 0)]),
    "monoclinic-three": ("monoclinic", (5.1, 6.3, 7.2, 90, 103.5, 90),
                         [(1, 0, 0), (0, 1, 0), (0, 0, 1)]),
    "cubic-fields-not-cubic": ("cubic", (4.0, 4.0, 5.0, 90, 90, 90), [(1, 0, 0), (0, 0, 1)]),
    "trigonal-fields-neither": ("trigonal", (5.0, 5.0, 6.0, 80, 80, 80), [(1, 0, 0), (0, 0, 1)]),
}
# ISAR's PUMA profile works at kf = 2.6634 1/A (TAVI's PUMA parameters.json); elastic.
PUMA_K = 2.6634
FEASIBILITY_TILTS_DEG = (0.0, 10.0, 25.0)


def _check_checkout(tavi):
    def git(*args):
        return subprocess.run(["git", "-C", str(tavi), *args], capture_output=True,
                              check=True, text=True).stdout
    head = git("rev-parse", "HEAD").strip()
    if not head.startswith(SHA):
        raise SystemExit(f"TAVI HEAD is {head[:12]}, not {SHA}: check out {SHA} or "
                         "re-vendor first (isar/geometry/VENDORED.md)")
    dirty = git("status", "--porcelain", "--", *SOURCES).strip()
    if dirty:
        raise SystemExit(f"TAVI's vendored sources are modified:\n{dirty}")
    return head


def _rot(axis, deg):
    a = np.asarray(axis, dtype=float)
    a = a / np.linalg.norm(a)
    t = math.radians(deg)
    k = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + math.sin(t) * k + (1 - math.cos(t)) * (k @ k)


def _travel(value):
    return None if math.isinf(value) else float(value)


def _vec(v):
    return [float(x) for x in v]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tavi", required=True, help="TAVI checkout at " + SHA)
    tavi = Path(parser.parse_args().tavi).resolve()
    head = _check_checkout(tavi)
    sys.path.insert(0, str(tavi))

    import importlib

    from tavi.orientation import (StageUnreachable, legacy_triple, lock_plane, solve_stage,
                                  stage_record)
    from tavi.sample_mount import reciprocal_basis_tas
    from tavi.tas_geometry import _normalize_deg, lab_q_from_stt, stt_from_q_norm
    from tavi.ub_matrix import (ObservedPeak, UBMatrix, lattice_crystal_system,
                                refine_lattice_from_peaks)

    descriptors = {name: getattr(importlib.import_module(f"instruments.{name}.plugin"),
                                 f"{name}_descriptor")() for name in INSTRUMENTS}
    stages = {name: {"sense": int(d.geometry.sense_sample),
                     "axes": [[ax.name, _vec(ax.axis), _travel(ax.lower), _travel(ax.upper)]
                              for ax in d.goniometer]}
              for name, d in descriptors.items()}

    def fit(lattice, peaks):
        ub = UBMatrix(*lattice)
        ub.peaks = peaks
        ub.calculate_U_from_peaks()
        return ub.U.tolist()

    # --- stage solves and fits, per stage x sense x lattice -------------------------
    stage_sets = []
    for i, name in enumerate(INSTRUMENTS):
        gonio = descriptors[name].goniometer
        limited = not math.isinf(gonio[1].upper)
        for sense in (-1, 1):
            for j, (lattice_name, lattice) in enumerate(LATTICES.items()):
                rng = np.random.default_rng(1000 * i + 100 * (sense > 0) + j)
                u_true = _rot(rng.normal(size=3), rng.uniform(0.5, 10.0))
                B = reciprocal_basis_tas(*lattice)
                points, reached, refused = [], [], 0
                for index in rng.permutation(len(HKL_POOL)):
                    hkl = HKL_POOL[index]
                    q = u_true @ B @ np.array(hkl, dtype=float)
                    if not 0.5 < np.linalg.norm(q) < 4.8:
                        continue
                    stt = stt_from_q_norm(float(np.linalg.norm(q)), K, K, sense)
                    signed = -q if sense > 0 else q
                    q_lab = lab_q_from_stt(K, K, stt)
                    point = {"hkl": list(hkl), "q_mount": _vec(q), "stt": stt,
                             "q_lab": _vec(q_lab)}
                    try:
                        readouts = solve_stage(gonio, signed, q_lab)
                    except StageUnreachable as exc:
                        if refused >= 1:
                            continue
                        refused += 1
                        point["error"] = str(exc)
                    else:
                        if len(reached) >= 3:
                            continue
                        record = stage_record(gonio, readouts, ki=K, kf=K, sense=sense)
                        point["readouts"] = readouts
                        point["legacy"] = list(legacy_triple(record, stt, K, K, sense))
                        reached.append((hkl, readouts, stt, record, point["legacy"]))
                    points.append(point)
                    if len(reached) >= 3 and (refused or not limited):
                        break
                assert len(reached) == 3 and (refused or not limited), (name, sense, lattice_name)
                stage_peaks = [ObservedPeak(hkl=tuple(h), angles=(r["A3"], r["sgl"], s),
                                            ki=K, kf=K, stage=rec)
                               for h, r, s, rec, _ in reached]
                legacy_peaks = [ObservedPeak.from_dict({"hkl": list(h), "angles": leg,
                                                        "ki": K, "kf": K})
                                for h, _, _, _, leg in reached]
                stage_sets.append({
                    "stage": name, "sense": sense, "lattice": list(lattice), "k": K,
                    "u_true": u_true.tolist(), "points": points,
                    "fit": {"stage_2": fit(lattice, stage_peaks[:2]),
                            "stage_3": fit(lattice, stage_peaks),
                            "legacy_2": fit(lattice, legacy_peaks[:2]),
                            "legacy_3": fit(lattice, legacy_peaks)},
                })

    # --- locked mode -----------------------------------------------------------------
    locked = []
    B = reciprocal_basis_tas(*LATTICES["cubic"])
    for name in INSTRUMENTS:
        gonio = descriptors[name].goniometer
        for tilt in (3.0, 30.0):
            u = _rot((1, 0, 0), tilt) @ _rot((0, 0, 1), -2.0)
            ub = u @ B
            case = {"stage": name, "ub": ub.tolist(), "plane": [[1, 0, 0], [0, 1, 0]],
                    "points": []}
            try:
                lock = lock_plane(gonio, ub, (1, 0, 0), (0, 1, 0))
            except StageUnreachable as exc:
                case["lock_error"] = str(exc)
                locked.append(case)
                continue
            case["lock"] = lock
            for hkl in ((2, 0, 0), (1, 1, 0), (1, 0, 1)):
                q = ub @ np.array(hkl, dtype=float)
                stt = stt_from_q_norm(float(np.linalg.norm(q)), K, K, -1)
                q_lab = lab_q_from_stt(K, K, stt)
                point = {"hkl": list(hkl), "q_mount": _vec(q), "q_lab": _vec(q_lab)}
                try:
                    point["readouts"] = solve_stage(gonio, q, q_lab, locked=lock,
                                                    plane=((1, 0, 0), (0, 1, 0)))
                except StageUnreachable as exc:
                    point["error"] = str(exc)
                case["points"].append(point)
            locked.append(case)

    # --- lattice refinement by crystal system ---------------------------------------
    class Peak:
        """What refine_lattice_from_peaks reads: hkl, q_lab, is_valid."""
        is_valid = True

        def __init__(self, hkl, q):
            self.hkl, self.q_lab = tuple(hkl), q

    u_refine = _rot(np.random.default_rng(53).normal(size=3), 7.0)

    def refine_peaks(lattice, hkls):
        b = reciprocal_basis_tas(*lattice)
        return [Peak(h, u_refine @ b @ np.array(h, dtype=float)) for h in hkls]

    refine = []
    cases = {name: (system, start, refine_peaks(true, REFINE_HKLS))
             for name, (system, start, true) in REFINE_CASES.items()}
    cases.update({name: (system, start, refine_peaks(start, hkls))
                  for name, (system, start, hkls) in UNDECIDABLE.items()})
    for case, (system, start, peaks) in cases.items():
        entry = {"case": case, "system": system, "start": list(start),
                 "peaks": [{"hkl": list(p.hkl), "q": _vec(p.q_lab)} for p in peaks]}
        try:
            result = refine_lattice_from_peaks(peaks, start, system)
        except ValueError as exc:
            entry["error"] = str(exc)
        else:
            entry["lattice"] = list(result["lattice"])
            entry["rms_error"] = result["rms_error"]
            entry["crystal_system"] = result["crystal_system"]
        refine.append(entry)
    crystal_systems = {case: lattice_crystal_system(start)
                       for case, (_, start, _) in REFINE_CASES.items()}

    # --- tas_runtime.py 920-934, re-run with TAVI's primitives on PUMA's stage ---
    puma = descriptors["puma"]
    sense = int(puma.geometry.sense_sample)
    lattice = LATTICES["cubic"]
    B = reciprocal_basis_tas(*lattice)
    feasibility = []
    for tilt in FEASIBILITY_TILTS_DEG:
        hkl = (1.0, 0.0, math.tan(math.radians(tilt)))
        q_mount = B @ np.array(hkl)                     # U = identity
        stt = stt_from_q_norm(float(np.linalg.norm(q_mount)), PUMA_K, PUMA_K, sense)
        signed = -q_mount if sense > 0 else q_mount
        point = {"stage": "puma", "sense": sense, "lattice": list(lattice),
                 "u": np.eye(3).tolist(), "hkl": list(hkl), "tilt_deg": tilt,
                 "ki": PUMA_K, "kf": PUMA_K, "stt": stt}
        try:
            stage = solve_stage(puma.goniometer, signed, lab_q_from_stt(PUMA_K, PUMA_K, stt))
        except StageUnreachable as exc:
            point["error"] = str(exc)
        else:
            sth = stage["A3"]
            if sense > 0:
                sth = _normalize_deg(sth)
            point.update(sth=sth, sgl=stage["sgl"], sgu=stage["sgu"])
        feasibility.append(point)

    fixture = {"tavi_sha": head, "generator": "tests/geometry/make_tavi_parity.py",
               "stages": stages, "stage_sets": stage_sets, "locked": locked,
               "refine": refine, "crystal_systems": crystal_systems,
               "feasibility": feasibility}
    text = json.dumps(fixture, separators=(",", ":"))
    OUT.write_text(text + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({len(text)} bytes): {len(stage_sets)} stage sets, "
          f"{len(locked)} locked cases, {len(refine)} refinements, "
          f"{len(feasibility)} feasibility points")


if __name__ == "__main__":
    main()
