"""File sets at ingest: the card's `[files]` table groups per-detector files into one scan.

A stub plugin defined here (no instrument dependency) reads a set as one scan with one spectrum per
member. Pins the contract unit p3's real reader builds on: grouping lives in ingest, a set missing
members still ingests and a re-ingest completes it through the existing upsert, sidecars are never
measurements, two sets on one identity are refused by name, and a card without `[files]` is untouched.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from isar.parsers import registry
from isar.schema import axes, scan
from isar.schema.card import Card, CardState, validate_policy_text
from isar.session import ingest
from isar.session.state import SessionState
from synthetic_ws import _POLICY_TOML

SET_RE = r"(?P<set>s\d+_\w+?)_(?P<analyzer>a\d+)"


def _record(scan_number: int, analyzers: list[str], label: str | None = None, n: int = 8):
    """A ScanRecord with today's fields only; counts are deterministic per analyzer so an
    unchanged member set hashes equal across ingests."""
    energy = np.linspace(-2.0, 6.0, n)
    spectra = {}
    for a in analyzers:
        counts = np.arange(n, dtype=float) + 3.0 * int(a[1:])
        spectra[a] = scan.Spectrum(
            detector_id=a, q=[1.0, 0.0, 0.0], q_source="reported", intensity=counts,
            variance=counts.copy(), mask=np.ones(n, bool), point_flags=[], spectrum_flags=[])
    rec = scan.ScanRecord(
        experiment_id="E", scan_number=scan_number, scan_type="measurement",
        scan_type_source="declared",
        scan_axes=[axes.energy_axis(energy), *axes.constant_q_axes(1.0, 0.0, 0.0, n)],
        spectra=spectra,
        normalization=scan.Normalization("time", np.full(n, 1.0), np.full(n, 1.0), 1.0),
        temperature=scan.Temperature(np.full(n, 5.0), {}, np.ones(n, bool)),
        environment=scan.Environment(), geometry=None,
        energy_calibration=scan.EnergyCalibration("energy", None, None, "absolute"),
        scan_flags=[], raw_header={},
        source=scan.Source("stub", None, None, "stub_sets", "0", "1", scan.SCHEMA_VERSION))
    if label is not None:   # read defensively by ingest; a plain attribute works before the field exists
        rec.facts = {"scan_label": {"value": label, "source": "filename"}}
    return rec


class _SetPlugin:
    """Parses a set as one scan. A member containing BAD is skipped (the plugin reports its own
    unreadable members); a member containing RAISE makes the whole set fail."""
    name = "stub_sets"
    version = "0"
    parse_set_calls: list
    parse_file_calls: list

    def __init__(self):
        self.parse_set_calls, self.parse_file_calls = [], []

    def parse_set(self, file_set, card):
        self.parse_set_calls.append(file_set)
        analyzers = []
        for path, groups in file_set.measurements:
            text = Path(path).read_text(encoding="utf-8")
            if "RAISE" in text:
                raise ValueError(f"cannot read {Path(path).name}")
            if "BAD" not in text:
                analyzers.append(groups["analyzer"])
        number = int(re.match(r"s(\d+)", file_set.key).group(1))
        return [_record(number, analyzers, label=file_set.key)]

    def parse_file(self, path, card):
        self.parse_file_calls.append(Path(path))
        raise AssertionError("a set member must not be parsed as a single file")

    def emit_file(self, run, card):  # pragma: no cover - Protocol filler
        raise NotImplementedError

    def card_template(self):  # pragma: no cover
        raise NotImplementedError

    def matches(self, path):  # pragma: no cover
        return False


class _FilePlugin(_SetPlugin):
    """Same card, but no parse_set: today's one-file-one-parse behaviour."""
    name = "stub_files"
    parse_set = None

    def parse_file(self, path, card):
        self.parse_file_calls.append(Path(path))
        m = re.search(r"a(\d+)$", Path(path).name)
        return [_record(int(m.group(1)), [f"a{m.group(1)}"])]


@pytest.fixture
def plugin():
    p = _SetPlugin()
    registry.REGISTRY_BY_NAME[p.name] = p
    yield p
    registry.REGISTRY_BY_NAME.pop(p.name, None)


def _card(plugin_name="stub_sets", files=True) -> Card:
    text = _POLICY_TOML.replace('plugin = "tavi"', f'plugin = "{plugin_name}"')
    text = text.replace('facility = "TAVI"', 'facility = "STUB"')
    if files:
        text += f"\n[files]\nmeasurement = ['{SET_RE}']\nsidecar = ['{{set}}.var']\nignore = ['*.keep']\n"
    res = validate_policy_text(text)
    assert res.is_valid, res.errors
    return Card(res.policy, CardState())


def _write(root: Path, name: str, text: str = "ok") -> Path:
    p = root / name
    p.write_text(text, encoding="utf-8")
    return p


def _members(root: Path, key: str, analyzers=("a03", "a04", "a05")) -> list[Path]:
    return [_write(root, f"{key}_{a}") for a in analyzers]


# --- grouping ----------------------------------------------------------------------------------
def test_set_of_three_members_is_one_scan_with_three_ledger_entries(tmp_path, plugin):
    _members(tmp_path, "s10_x")
    card, session = _card(), SessionState()

    report = ingest.ingest_batch(card, session, [tmp_path])

    assert len(plugin.parse_set_calls) == 1 and plugin.parse_file_calls == []
    assert report.added == ["E:10"] and not report.updated
    assert sorted(session.scans) == ["E:10:a03", "E:10:a04", "E:10:a05"]
    assert report.deduped == []                 # dedup is per set, never per member file
    assert report.refused == []
    meas = [f for f in report.files if f["class"] == "measurement"]
    assert len(meas) == 3 and all(f["scan_ids"] == ["E:10"] for f in meas)
    # the scan identity guard for a later set is stamped per entry
    prov = session.scans["E:10:a03"].provenance
    assert prov["scan_label"] == "s10_x" and prov["file_set"] == "s10_x"
    assert Path(prov["source_file"]).name == "s10_x_a03"


def test_two_sets_in_one_folder_become_two_scans(tmp_path, plugin):
    _members(tmp_path, "s10_x", ("a03", "a04"))
    _members(tmp_path, "s11_y", ("a03", "a04"))
    plan = ingest.discover([tmp_path], _card())
    assert [fs.key for fs in plan.file_sets] == ["s10_x", "s11_y"]
    assert [r.scan_number for r in ingest.parse_all(plan, _card())] == [10, 11]


def test_sidecar_and_ignored_files_are_never_measurements(tmp_path, plugin):
    members = _members(tmp_path, "s10_x", ("a03", "a04"))
    var = _write(tmp_path, "s10_x.var")
    kept = _write(tmp_path, "notes.keep")
    orphan = _write(tmp_path, "s99_z.var")           # sidecar of a set that has no members
    card = _card()

    plan = ingest.discover([tmp_path], card)

    assert plan.measurement_files == members
    assert plan.sidecar_files == [var] and plan.file_sets[0].sidecars == [var]
    assert plan.ignored_files == [kept]
    assert orphan in plan.unrecognized               # not a sidecar of anything found; detect() fails it
    report = ingest.ingest_batch(card, SessionState(), [tmp_path])
    assert var.name not in {Path(f["path"]).name for f in report.files if f["class"] == "measurement"}
    called = {p.name for fs in plugin.parse_set_calls for p, _g in fs.measurements}
    assert called == {"s10_x_a03", "s10_x_a04"}


# --- members missing, then arriving ------------------------------------------------------------
def test_set_missing_a_member_ingests_the_rest_and_reingest_completes_it(tmp_path, plugin):
    _members(tmp_path, "s10_x", ("a03", "a04"))
    card, session = _card(), SessionState()
    first = ingest.ingest_batch(card, session, [tmp_path])
    assert first.added == ["E:10"] and sorted(session.scans) == ["E:10:a03", "E:10:a04"]

    again = ingest.ingest_batch(card, session, [tmp_path])           # nothing arrived: a true no-op
    assert again.unchanged == ["E:10"] and again.dirty is False

    _write(tmp_path, "s10_x_a05")
    late = ingest.ingest_batch(card, session, [tmp_path])

    assert late.updated == ["E:10"] and late.added == []              # the scan is updated ...
    assert sorted(session.scans) == ["E:10:a03", "E:10:a04", "E:10:a05"]   # ... a05 is a new entry
    assert {e.status for e in session.scans.values()} == {"ingested"}
    assert late.dirty is True


def test_unreadable_member_does_not_abort_the_set_or_the_batch(tmp_path, plugin):
    _write(tmp_path, "s10_x_a03")
    _write(tmp_path, "s10_x_a04", "BAD")             # the plugin skips this member itself
    _members(tmp_path, "s11_y", ("a03", "a04"))
    _write(tmp_path, "s12_z_a03", "RAISE")           # a set that fails outright
    _write(tmp_path, "s12_z_a04")
    card, session = _card(), SessionState()

    report = ingest.ingest_batch(card, session, [tmp_path])

    assert report.added == ["E:10", "E:11"]
    assert sorted(session.scans) == ["E:10:a03", "E:11:a03", "E:11:a04"]
    failed = {Path(f["path"]).name: f["error"] for f in report.files if "error" in f}
    assert set(failed) == {"s12_z_a03", "s12_z_a04"} and "cannot read" in failed["s12_z_a03"]
    assert report.last_ingest["failed_file_count"] == 2


# --- identity collisions -----------------------------------------------------------------------
def test_two_sets_on_one_identity_are_refused_by_name_and_never_merged(tmp_path, plugin):
    first = _members(tmp_path, "s10_a", ("a03", "a04"))
    second = _members(tmp_path, "s10_b", ("a05",))           # same scan number 10, other set key
    card, session = _card(), SessionState()

    report = ingest.ingest_batch(card, session, [tmp_path])

    assert report.added == ["E:10"]
    assert sorted(session.scans) == ["E:10:a03", "E:10:a04"]  # s10_b's a05 never entered
    assert len(report.refused) == 1
    r = report.refused[0]
    assert r["scan"] == "E:10" and r["sets"] == ["s10_a", "s10_b"]
    assert "s10_a" in r["reason"] and "s10_b" in r["reason"]
    assert all(p.exists() for p in first + second)           # nothing moved or deleted
    err = next(f["error"] for f in report.files if f["path"].endswith("s10_b_a05"))
    assert err.startswith("refused:")
    assert session.scans["E:10:a03"].provenance["file_set"] == "s10_a"


def test_set_whose_label_differs_from_the_ledgers_is_refused(tmp_path, plugin):
    _members(tmp_path, "s10_a", ("a03", "a04"))
    card, session = _card(), SessionState()
    ingest.ingest_batch(card, session, [tmp_path])
    before = {k: (e.content_hash, dict(e.provenance)) for k, e in session.scans.items()}

    later = tmp_path / "later"
    later.mkdir()
    _members(later, "s10_b", ("a03", "a04", "a05"))
    report = ingest.ingest_batch(card, session, [later])

    assert report.refused and report.refused[0]["sets"] == ["s10_a", "s10_b"]
    assert report.added == [] and report.updated == []
    assert {k: (e.content_hash, dict(e.provenance)) for k, e in session.scans.items()} == before
    assert sorted(session.scans) == ["E:10:a03", "E:10:a04"]


# --- opt-in: nothing changes without [files] / parse_set ---------------------------------------
def test_card_without_files_table_never_forms_sets(tmp_path, plugin):
    _members(tmp_path, "s10_x")
    card = _card(files=False)

    plan = ingest.discover([tmp_path], card)

    assert plan.file_sets == [] and plan.sidecar_files == [] and plan.ignored_files == []
    assert plan.measurement_files == []              # unchanged path: detect() classifies by signature
    assert len(plan.unrecognized) == 3
    assert plugin.parse_set_calls == [] and plugin.parse_file_calls == []


def test_plugin_without_parse_set_keeps_one_file_one_parse(tmp_path):
    p = _FilePlugin()
    registry.REGISTRY_BY_NAME[p.name] = p
    try:
        _members(tmp_path, "s10_x")
        card, session = _card("stub_files"), SessionState()
        plan = ingest.discover([tmp_path], card)
        assert len(plan.file_sets) == 1               # grouped by discovery, but not used for parsing

        report = ingest.ingest_batch(card, session, [tmp_path])

        assert len(p.parse_file_calls) == 3           # one parse per file, as before
        assert report.added == ["E:3", "E:4", "E:5"]
    finally:
        registry.REGISTRY_BY_NAME.pop(p.name, None)


# --- card tables -------------------------------------------------------------------------------
def _validate(extra: str):
    return validate_policy_text(_POLICY_TOML + extra)


def test_files_facts_reader_tables_validate_without_unknown_table_warnings():
    res = _validate(f"\n[files]\nmeasurement = ['{SET_RE}']\nsidecar = ['{{set}}.var']\n"
                    "ignore = ['*.tmp']\n\n[facts]\nslit = '60x60'\n\n[reader]\nintensity_family = '_i'\n")
    assert res.is_valid and res.warnings == ()
    assert res.policy.files["measurement"] == [SET_RE]
    assert res.policy.facts == {"slit": "60x60"} and res.policy.reader == {"intensity_family": "_i"}


@pytest.mark.parametrize("files, needle", [
    ("measurement = ['(?P<set>x']", "does not compile"),
    ("measurement = ['(?P<set>.+)_a03']", "(?P<analyzer>"),
    ("measurement = ['(?P<analyzer>a\\d+)']", "(?P<set>"),
    (f"measurement = ['{SET_RE}']\nsidecar = ['no_placeholder.var']", "{set}"),
    (f"measurement = ['{SET_RE}']\nbogus = 1", "unknown key"),
    ("sidecar = ['{set}.var']", "measurement is required"),
    ("measurement = 'not-a-list'", "list of strings"),
])
def test_bad_files_table_is_a_validation_error(files, needle):
    res = _validate(f"\n[files]\n{files}\n")
    assert not res.is_valid
    assert any(needle in e for e in res.errors), res.errors
