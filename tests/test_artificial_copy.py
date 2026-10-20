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


def _data_lines(copy: Path, kind: str):
    """The copy's data rows, found without the tool: what follows each label line."""
    out, state = [], "off"
    for ln in copy.read_bytes().splitlines():
        s = ln.strip()
        if kind == "in8":
            if s.startswith(b"DATA_:"):
                state = "label"
            elif state == "label" and s:
                state = "rows"
            elif state == "rows":
                if _floatlike(ln):
                    out.append(ln)
                elif s:
                    state = "off"
        elif s.startswith(b"#L "):
            state = "rows"
        elif s.startswith(b"#S "):
            state = "off"
        elif state == "rows" and s and not s.startswith(b"#"):
            out.append(ln)
    return out


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
    for o, n in zip(old_blocks, new_blocks):
        assert o.data.shape == n.data.shape and o.column_labels == n.column_labels
        assert np.all(o.data != n.data)                   # ... and every number in them
        for j in range(o.data.shape[1]):                  # coarse character of each column kept
            d = np.diff(o.data[:, j])
            if np.all(d >= 0) or np.all(d <= 0):          # monotone or constant: same pattern
                assert np.array_equal(np.sign(d), np.sign(np.diff(n.data[:, j])))
            if np.all(o.data[:, j] >= 0) and np.all(o.data[:, j] == np.round(o.data[:, j])):
                assert np.all(n.data[:, j] >= 0) and np.all(n.data[:, j] == np.round(n.data[:, j]))


@pytest.mark.parametrize("kind", FORMATS)
def test_truth_file_matches_the_copy(kind, sources, tmp_path):
    copy, truth = _make(sources[kind], tmp_path / "out", "99")
    blocks = _blocks(copy, kind)
    assert [s["points"] for s in truth["scans"]] == [len(b.data) for b in blocks]
    assert [s["column_names"] for s in truth["scans"]] == [b.column_labels for b in blocks]
    # the tokens the tool says it wrote are the copy's own text, scan by scan, column by column
    want = [[col["tokens"][r] for col in s["data_columns"]]
            for s in truth["scans"] for r in range(s["points"])]
    got = [[t.decode() for t in ln.split()] for ln in _data_lines(copy, kind)]
    assert got == want
    # header values are the file's text, not the parser's reading of it
    first = truth["scans"][0]["header_values"] if kind == "in8" else truth["file_header_values"]
    key, prefix = ("TITLE", b"TITLE:") if kind == "in8" else ("#C", b"#C ")
    line = next(ln for ln in copy.read_bytes().splitlines() if ln.startswith(prefix))
    assert first[key][0] == line[len(prefix):].decode().strip()


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
            data, written, starts = real_build(*a)
            assert _old in data
            return data.replace(_old, _new, 1), written, starts
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
    ]
    for old in cols:
        new = ac.replace_column(old, rng)
        assert all(a != b and len(a) == len(b) for a, b in zip(old, new))
        ac.verify_column(old, new)


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
