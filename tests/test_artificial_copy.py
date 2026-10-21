"""tools/artificial_copy.py: every number in the data rows is replaced, every other byte is kept, the
copy parses like the original, and a copy that does not is refused with nothing written.

No real data: SPEC (S30 supplementary, S30 raw) and numor (IN8) files come from the synthetic
generator (`isar.synth_format.run_builder` + the plugins' `emit_file`).
"""
from __future__ import annotations

import importlib.util
import json
import random
import re
import sys
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

import isar
from isar.parsers import ill_in8, numor_base, spec_base, spec_s30, spec_s30_raw
from isar.synth_format import run_builder

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("artificial_copy_under_test",
                                               REPO / "tools" / "artificial_copy.py")
ac = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ac                      # the tool's @dataclass looks its module up here
_spec.loader.exec_module(ac)

# kind -> (file name, how to emit it, corruptions of the in-memory copy the tool must refuse)
FORMATS = {
    "s30": ("syn.dat", lambda: spec_s30.emit_file(
        run_builder.build_s30_run("SYN", n_scans=3), spec_s30.card_template()),
        [(b"#C synthetic S30 run", b"#C synthetiX S30 run"), (b"#L HerixE", b"#L HerixX")]),
    "raw": ("syn.spec", lambda: spec_s30_raw.emit_file(
        run_builder.build_s30_raw_run("RAW", n_scans=1), spec_s30_raw.card_template()),
        [(b"#C synthetic RAW S30", b"#C synthetiX RAW S30"), (b"#Q ", b"#Q 9")]),
    "in8": ("055000", lambda: ill_in8.emit_file(
        run_builder.build_in8_run("IN8X", n_scans=2), ill_in8.card_template()),
        [(b"EXPNO: IN8X", b"EXPNO: IN8Y"), (b"  PNT  QH", b"  PNT  QX")]),
}


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    d = tmp_path_factory.mktemp("generated")
    out = {}
    for kind, (name, emit, _) in FORMATS.items():
        out[kind] = d / name
        out[kind].write_bytes(emit())
    return out


def _blocks(path: Path, kind: str):
    if kind == "in8":
        return numor_base.read_numor(path)
    mw = spec_s30_raw.card_template().policy.multiword_columns if kind == "raw" else None
    return spec_base.read_spec(path, multiword_columns=mw)[1]


def _make(src: Path, out: Path, scans: str | None = None) -> tuple[Path, dict]:
    target, truth = ac.make_copy(src, out, scans)
    return target, json.loads(truth.read_text(encoding="utf-8"))


def _floatlike(line: bytes) -> bool:
    try:
        [float(t) for t in line.split()]
    except ValueError:
        return False
    return bool(line.strip())


def _scans_of(copy: Path, kind: str):
    """The copy's scans, found without the tool: [label-line words, data rows] per scan, from the text."""
    out, state = [], "off"
    for ln in copy.read_bytes().splitlines():
        s = ln.strip()
        if kind == "in8":
            if len(s) >= 8 and set(s) == {ord("R")}:
                out.append([None, []])
                state = "off"
            elif s.startswith(b"DATA_:"):
                state = "label"
            elif state == "label" and s:
                out[-1][0] = s.decode().split()
                state = "rows"
            elif state == "rows":
                if _floatlike(ln):
                    out[-1][1].append(ln)
                elif s:
                    state = "off"
        elif s.startswith(b"#S "):
            out.append([None, []])
            state = "off"
        elif s.startswith(b"#L "):
            out[-1][0] = s[3:].decode().split()
            state = "rows"
        elif state == "rows" and s and not s.startswith(b"#"):
            out[-1][1].append(ln)
    return out


def _forced_indexes(scan: dict, labels: list) -> set:
    """Indexes of the columns this truth-file scan lists as forced (kept unchanged)."""
    return {j for j, lab in enumerate(labels) if lab in scan["forced_columns"]}


def test_import_root_is_this_checkout():
    assert Path(isar.__file__).resolve().parents[1] == REPO
    assert Path(ac.__file__).resolve().parents[1] == REPO


@pytest.mark.parametrize("kind", FORMATS)
def test_only_data_numbers_change_and_all_of_them(kind, sources, tmp_path):
    src = sources[kind]
    copy, truth = _make(src, tmp_path / "out", "99")
    a, b = src.read_bytes().splitlines(keepends=True), copy.read_bytes().splitlines(keepends=True)
    assert len(a) == len(b)
    changed = [(x, y) for x, y in zip(a, b) if x != y]
    for x, y in changed:                                  # same bytes around every number
        assert _floatlike(x) and _floatlike(y)
        assert re.sub(rb"\S+", b"#", x) == re.sub(rb"\S+", b"#", y)
        assert all(p != q and len(p) == len(q) for p, q in zip(x.split(), y.split()))
    old_blocks, new_blocks = _blocks(src, kind), _blocks(copy, kind)
    assert len(old_blocks) == len(new_blocks) == truth["number_of_scans"]
    # the changed lines are exactly the data rows (no header line changed) ...
    assert len(changed) == sum(len(blk.data) for blk in old_blocks)
    for o, n, scan in zip(old_blocks, new_blocks, truth["scans"]):
        assert o.data.shape == n.data.shape and o.column_labels == n.column_labels
        forced = _forced_indexes(scan, o.column_labels)   # only columns fixed by their length stay
        for j in range(o.data.shape[1]):                  # ... and every number in the others
            assert j in forced or np.all(o.data[:, j] != n.data[:, j])
        for j in range(o.data.shape[1]):                  # coarse character of each column kept
            d = np.diff(o.data[:, j])
            if np.all(d >= 0) or np.all(d <= 0):          # monotone or constant: same pattern
                assert np.array_equal(np.sign(d), np.sign(np.diff(n.data[:, j])))
            if np.all(o.data[:, j] >= 0) and np.all(o.data[:, j] == np.round(o.data[:, j])):
                assert np.all(n.data[:, j] >= 0) and np.all(n.data[:, j] == np.round(n.data[:, j]))


@pytest.mark.parametrize("kind", FORMATS)
def test_truth_file_is_what_the_tool_wrote(kind, sources, tmp_path):
    """Points, names and tokens come from the rows and label line in the copy's text, not the parser."""
    copy, truth = _make(sources[kind], tmp_path / "out", "99")
    scans = _scans_of(copy, kind)
    assert len(scans) == truth["number_of_scans"] == len(truth["scans"])
    for (label_words, rows), s in zip(scans, truth["scans"]):
        assert s["points"] == len(rows) > 0
        # raw S30 joins multi-word names with `_`: compare words, not the joining
        assert " ".join(s["column_names"]).replace("_", " ").split() == \
            " ".join(label_words).replace("_", " ").split()
        assert all(len(col["tokens"]) == s["points"] for col in s["data_columns"])
        # the tokens the tool says it wrote are the copy's own text, row by row
        assert [[col["tokens"][r] for col in s["data_columns"]] for r in range(s["points"])] == \
            [[t.decode() for t in ln.split()] for ln in rows]
    # header values are the file's text, not the parser's reading of it
    first = truth["scans"][0]["header_values"] if kind == "in8" else truth["file_header_values"]
    key, prefix = ("TITLE", b"TITLE:") if kind == "in8" else ("#C", b"#C ")
    line = next(ln for ln in copy.read_bytes().splitlines() if ln.startswith(prefix))
    assert first[key][0] == line[len(prefix):].decode().strip()


@pytest.mark.parametrize("kind", FORMATS)
def test_the_parsers_reading_of_the_copy_agrees_with_the_truth_file(kind, sources, tmp_path):
    copy, truth = _make(sources[kind], tmp_path / "out", "99")
    blocks = _blocks(copy, kind)
    assert [s["points"] for s in truth["scans"]] == [len(b.data) for b in blocks]
    assert [s["column_names"] for s in truth["scans"]] == [b.column_labels for b in blocks]


def test_a_truncated_final_row_is_refused_not_recorded_as_a_point(sources, tmp_path):
    """The parser drops a short final row, so only the tool's own row count can disagree with it."""
    src = tmp_path / "ragged.dat"
    lines = sources["s30"].read_bytes().splitlines(keepends=True)
    last = max(i for i, ln in enumerate(lines) if ln.strip() and not ln.startswith(b"#"))
    lines[last] = lines[last].rsplit(None, 1)[0] + b"\n"           # one token short
    src.write_bytes(b"".join(lines))
    with pytest.raises(ac.Refuse, match="data rows located"):
        ac.make_copy(src, tmp_path / "out", "99")
    assert not (tmp_path / "out").exists()


def test_spec_is_trimmed_to_the_chosen_scans(sources, tmp_path):
    src = sources["s30"]
    copy, truth = _make(src, tmp_path / "first2", "2")
    assert copy.read_bytes().count(b"\n#S ") == 2 and truth["number_of_scans"] == 2
    assert [s["scan_number"] for s in truth["scans"]] == [1, 2]
    copy, truth = _make(src, tmp_path / "third", "3,")
    assert [s["scan_number"] for s in truth["scans"]] == [3]
    assert copy.read_bytes().startswith(src.read_bytes().split(b"#S ")[0])   # file header kept
    with pytest.raises(ac.Refuse):
        ac.make_copy(src, tmp_path / "nope", "42,")


def test_crlf_survives(sources, tmp_path):
    src = tmp_path / "crlf.dat"
    src.write_bytes(sources["s30"].read_bytes().replace(b"\n", b"\r\n"))
    copy, _ = _make(src, tmp_path / "out", "99")
    data = copy.read_bytes()
    assert data.count(b"\r\n") == data.count(b"\n") == src.read_bytes().count(b"\n")


def test_same_seed_same_copy(sources, tmp_path):
    a, _ = _make(sources["in8"], tmp_path / "a")
    b, _ = _make(sources["in8"], tmp_path / "b")
    assert a.read_bytes() == b.read_bytes()


@pytest.mark.parametrize("kind", FORMATS)
def test_copy_that_does_not_parse_like_the_original_is_refused(
        kind, sources, tmp_path, monkeypatch, capsys):
    real_build = ac.build_copy
    for n, (old, new) in enumerate(FORMATS[kind][2]):
        def corrupt(*a, _old=old, _new=new):
            data, written = real_build(*a)
            assert _old in data
            return data.replace(_old, _new, 1), written
        monkeypatch.setattr(ac, "build_copy", corrupt)
        out = tmp_path / f"out{n}"
        rc = ac.main([str(sources[kind]), "--out", str(out)])
        assert rc == 1
        err = capsys.readouterr().err                     # differs, or the parser cannot read the copy
        assert "does not parse like the real file" in err or "could not read" in err
        assert not out.exists()                           # nothing written, not even the folder


def test_never_writes_into_real_data_or_over_an_existing_file(sources, tmp_path):
    src = sources["in8"]
    before = src.read_bytes()
    for out in (tmp_path / "real_data" / "x", tmp_path / "test_data", tmp_path / "Real_Data"):
        with pytest.raises(ac.Refuse):
            ac.make_copy(src, out)
        assert not out.exists()
    with pytest.raises(ac.Refuse):                        # the output would be the input itself
        ac.make_copy(src, src.parent)
    assert src.read_bytes() == before
    ac.make_copy(src, tmp_path / "once")
    with pytest.raises(ac.Refuse):
        ac.make_copy(src, tmp_path / "once")


def test_column_replacement_keeps_shape_and_character():
    rng = random.Random(3)
    cols = [
        [b"-12.5", b"-3.5", b"0.5", b"4.5", b"10.25", b"100.0"],        # ascending, widths vary
        [b"9.0", b"9.0", b"4.0", b"1.0"],                                # descending, equal neighbours
        [b"50", b"50", b"50"],                                           # constant integer
        [b"1.5e+02", b"0.0e+00", b"3.5e+01"],                            # free scientific
        [b"-1.0e+01", b"0.0e+00", b"2.5e-01", b"4.0e+00"],               # ascending through zero
        # a scanned axis where SPEC dropped the trailing zero of the first value: width 1, mixed decimals
        [b"-5"] + [b"-4.%d" % i for i in range(9, 0, -1)],
        [b"0", b"0.5", b"1", b"1.5", b"2", b"2.5", b"3", b"3.5", b"4", b"4.5"],
    ]
    for old in cols:
        new, forced = ac.replace_column(old, rng)
        assert forced == set()
        assert all(a != b and len(a) == len(b) for a, b in zip(old, new))
        ac.verify_column(old, new)


def test_a_counter_that_fills_its_width_is_forced_not_refused():
    rng = random.Random(5)
    tokens = lambda lo, hi: [str(i).encode() for i in range(lo, hi + 1)]
    old = tokens(0, 9)                                    # every one-digit value: fixed by its length
    new, forced = ac.replace_column(old, rng)
    assert new == old and forced == set(range(10))
    old = tokens(1, 99)                                   # one-digit part is replaceable, 10..99 is not
    new, forced = ac.replace_column(old, rng)
    assert forced == set(range(9, 99)) and new[9:] == old[9:]
    assert all(a != b for a, b in zip(old[:9], new[:9]))
    for hi in (40, 98):                                   # not saturated (98: one value short of full)
        new, forced = ac.replace_column(tokens(1, hi), rng)
        assert forced == set() and all(a != b for a, b in zip(tokens(1, hi), new))


def _counter_numor(src: Path, first: int, last: int) -> bytes:
    """The generated numor with its data rows rebuilt as a `first..last` point counter."""
    lines = src.read_bytes().decode().splitlines()
    i = lines.index("DATA_:") + 2
    rows = [f"{p}  2.1500  0.0500  0.0000  {-10 + 0.25 * k:.6f}  100000  50  60.0000  {5 + k % 7}"
            f"  291.000  290.000" for k, p in enumerate(range(first, last + 1))]
    return ("\n".join(lines[:i] + rows) + "\n").encode()


@pytest.mark.parametrize("first, last, forced_rows", [
    (0, 9, set(range(10))),                               # a counter that is one whole width
    (1, 99, set(range(9, 99))),                           # the two-digit width is full
    (0, 99, set(range(100))),
    (1, 120, set(range(9, 99))),                          # the three-digit part is replaceable
])
def test_a_point_counter_copies_and_is_reported_forced(first, last, forced_rows, sources, tmp_path):
    src = tmp_path / "counter" / "055001"
    src.parent.mkdir()
    src.write_bytes(_counter_numor(sources["in8"], first, last))
    copy, truth = _make(src, tmp_path / "out")
    (scan,) = truth["scans"]
    assert scan["forced_columns"] == ["PNT"]
    o, n = numor_base.read_numor(src)[0], numor_base.read_numor(copy)[0]
    assert o.column_labels == n.column_labels and len(o.data) == last - first + 1
    changed = o.data != n.data
    assert set(np.flatnonzero(~changed[:, 0])) == forced_rows   # the counter keeps only the forced rows
    assert changed[:, 1:].all()                           # every other data number changed


@pytest.mark.parametrize("old, bad", [
    ([b"1.0", b"2.0", b"3.0"], [b"5.0", b"4.0", b"9.0"]),            # monotone lost
    ([b"7", b"7", b"7"], [b"1", b"2", b"3"]),                        # constant lost
    ([b"4", b"9", b"2"], [b"5", b"1.5", b"3"]),                      # integer column lost
    ([b"1.0", b"2.0"], [b"1.0", b"3.0"]),                            # a value kept
    ([b"1.50", b"2.50"], [b"1.5", b"2.5"]),                          # shape changed
])
def test_a_bad_replacement_is_refused(old, bad):
    with pytest.raises(ac.Refuse):
        ac.verify_column(old, bad)


# ---------------------------------------------------------------------------
# BL43LXU tables and `name = value` files (the energy axis is a bin lattice)
# ---------------------------------------------------------------------------
def _bl43_member(tmp_path: Path) -> Path:
    """An analyzer file in the beamline's layout: bins on a 0.2 meV lattice through zero, a trailing comment."""
    energy = [-0.6 + 0.2 * k for k in range(12)]
    rows = ["#      meV      secs   npts          norm_i             y_i            ye_i"]
    rows += [f"{e:10.2f} {10.0 * (k % 3 + 1):9.1f} {k % 3 + 1:6d} {1.2e6 + 9e4 * k:15.5E} "
             f"{3.1e-4 + 1e-5 * k:15.5E} {2.2e-5 + 1e-6 * k:15.5E}" for k, e in enumerate(energy)]
    path = tmp_path / "in" / "Smp_100K_1_0p5_m2_run_a03"
    path.parent.mkdir()
    path.write_text("\n".join(rows + ["#Q=   18.815 /nm"]) + "\n", encoding="utf-8", newline="\n")
    return path


def test_a_table_copy_shifts_the_bin_lattice_and_keeps_every_other_byte(tmp_path):
    from isar.parsers import sp8_bl43
    src = _bl43_member(tmp_path)
    copy, truth = _make(src, tmp_path / "out")
    a, b = src.read_text(encoding="utf-8").splitlines(), copy.read_text(encoding="utf-8").splitlines()
    assert len(a) == len(b) and a[0] == b[0] and a[-1] == b[-1] == "#Q=   18.815 /nm"
    for x, y in zip(a[1:-1], b[1:-1]):
        assert len(x) == len(y) and re.sub(r"\S+", "#", x).split() == re.sub(r"\S+", "#", y).split()
        assert all(p != q for p, q in zip(x.split(), y.split()))     # every data number changed
    old, new = sp8_bl43.read_member(src), sp8_bl43.read_member(copy)
    steps = np.round(np.diff(new.energy), 4)
    assert np.all(steps == 0.2) and np.all(new.energy != old.energy)   # same step, moved along itself
    assert np.allclose(new.energy / 0.2, np.round(new.energy / 0.2))   # and still on the lattice through zero
    (scan,) = truth["scans"]
    assert scan["column_names"][0] == "meV" and scan["points"] == 12
    assert scan["data_columns"][0]["tokens"] == [ln.split()[0] for ln in b[1:-1]]


def test_a_shifted_progression_refuses_a_field_it_cannot_fit():
    toks = [b"-0.20", b"0.00", b"0.20", b"0.40"]
    new, forced = ac.replace_column(toks, random.Random(1), rooms=[8] * 4)
    assert forced == set() and all(a != b for a, b in zip(toks, new))
    assert [float(b) - float(a) for a, b in zip(new, new[1:])] == pytest.approx([0.2] * 3)
    with pytest.raises(ac.Refuse, match="inside its fields"):
        ac.replace_column(toks, random.Random(1), rooms=[1] * 4)      # no room for a shifted token
    new, _ = ac.replace_column(toks, random.Random(1), rooms=[8] * 4, free_phase=True)
    off_lattice = [(Decimal(t.decode()) / Decimal("0.2")) % 1 for t in new]
    assert all(r != 0 for r in off_lattice)                           # the offset moved by a fraction of a step
    # without `rooms` (SPEC, numor) a progression is an ordinary monotone column, as before
    assert ac.grid_step(toks) is not None
    new, _ = ac.replace_column(toks, random.Random(1))
    assert all(len(a) == len(b) for a, b in zip(toks, new))


def test_a_name_value_file_replaces_its_numbers_but_not_its_structure(tmp_path):
    from isar.parsers import sp8_bl43
    src = tmp_path / "Smp_100K_1_0p5_m2_run.var"
    src.write_text('\n'.join([
        ' file_name_prefix = "Smp_100K_1_0p5_m2_run"', ' silicon_order =    12', ' cal_factor =   1.0390',
        ' meV_bin_size =     0.20', ' nscans =     2', '', 'array ano[3]', 'array analeff[3]', '',
        'ano[1] = 3', 'analeff[1] =       1.00', '', 'ano[2] = 4', 'analeff[2] =       0.73', '',
        'ano[3] = 5', 'analeff[3] =       0.98', '']), encoding="utf-8", newline="\n")
    copy, truth = _make(src, tmp_path / "out")
    a, b = src.read_text(encoding="utf-8").splitlines(), copy.read_text(encoding="utf-8").splitlines()
    changed = {i for i, (x, y) in enumerate(zip(a, b)) if x != y}
    assert len(a) == len(b) and {a[i].split()[0] for i in changed} == {"cal_factor", "analeff[1]", "analeff[2]", "analeff[3]"}
    var = sp8_bl43.read_var(copy)
    assert var["arrays"]["ano"] == {1: 3.0, 2: 4.0, 3: 5.0}            # structural numbers kept
    assert var["scalars"]["silicon_order"] == 12.0 and var["scalars"]["meV_bin_size"] == 0.2
    cols = {c["name"]: c["tokens"] for c in truth["scans"][0]["data_columns"]}
    assert cols["analeff[]"] == [b[i].split()[-1] for i in sorted(changed) if b[i].startswith("analeff")]
    assert "cal_factor" in cols and "ano[]" not in cols
