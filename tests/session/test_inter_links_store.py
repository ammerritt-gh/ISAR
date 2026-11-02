"""The link store: a saved mode-linking result reads back equal, is stored once per content, and takes its branch
names from the store's own sequence, so a save never refuses on names (C-link-store, amendment A4). A link continues
the newest record of each scan it covers, merged (P3), so a name that lives only in an older record is never renumbered.

Prevents: one result stored as two records, two results' display names colliding after a re-link, a payload
read back as a different result, a link line the inter-spectra log reader counts as damage, and a series re-link
renumbering the branches its scans carried in older records.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from isar.analyze.coupled import linking as lk
from isar.analyze.coupled.settings import InterSettings
from isar.analyze.coupled.types import Held, HeldMode
from isar.session import writer
from isar.session.inter_spectra import linking, records
from isar.session.inter_spectra.graph import Link

UNIT, OTHER = "exp1:12:a03", "exp1:13:a03"
SERIES_T = {"exp1:12": 100.0, "exp1:13": 200.0, "exp1:14": 300.0}   # each scan's temperature


def _result(**changes) -> lk.LinkResult:
    """An empty linking result with the given fields replaced: the store reads only its content."""
    return replace(lk.link({}, (), InterSettings()), **changes)


def _named(name_start: int, next_name: int, *branches: lk.Branch) -> lk.LinkResult:
    return _result(branches=branches, name_start=name_start, next_name=next_name,
                   made_from={UNIT: ("fast", "h1")}, stamps={UNIT: "s1"})


def _spectrum(unit: str, T: float) -> lk.LinkInput:
    """A spectrum with modes at 4 and 8 meV (Gamma 0.2, area 10): the core reads these, never a workspace."""
    rows = [(4.0, 0.2, 10.0), (8.0, 0.2, 10.0)]
    modes = tuple(HeldMode(branch=f"fast:{unit}#{i}", omega0=o, gamma=g, height=1.0, guard=None,
                           guard_source="unavailable", status="determined_by_own_data", regime="underdamped",
                           pinned=False) for i, (o, g, _) in enumerate(rows))
    held = Held(unit=unit, fit="fast", inputs="h-" + unit, window=(1.0, 12.0), modes=modes, dormant=(), T=T)
    return lk.LinkInput(held=held, fwhm=0.4, areas=(10.0, 10.0), sigma_areas=(None, None),
                        sigma_gammas=(None, None), at_cap=(False, False), stamp=lk.fit_stamp("fast", "h-" + unit, rows))


def _step(root, scans: list[str]) -> lk.LinkResult:
    """One link of ``scans`` as the session runs it: previous_for, link, save, then the stored result read back."""
    prev, nxt = linking.previous_for(root, scans)
    inputs = {f"{s}:{d}": _spectrum(f"{s}:{d}", SERIES_T[s]) for s in scans for d in ("a03", "a04")}
    links = [Link("q", f"{s}:a03", f"{s}:a04", 0.1, (0.1, 0.0, 0.0), "lattice") for s in scans]
    links += [Link("T", f"{a}:{d}", f"{b}:{d}", 100.0, None, None) for a, b in zip(scans, scans[1:]) for d in ("a03", "a04")]
    result = lk.link(inputs, links, InterSettings(), prev, next_name=nxt)
    writer.save_links(root, result, scope=scans, batch=None, stage=None)
    return records.read_link_payload(root, records.read_links(root)[-1])


def test_a_saved_result_reads_back_equal_and_its_payload_starts_the_file(tmp_path):
    result = _result(made_from={UNIT: ("fast", "h1")}, stamps={UNIT: "s1"})
    link = writer.save_links(tmp_path, result, scope=["exp1:12"], batch=None, stage=None)
    (line,) = records.read_links(tmp_path)
    assert line.link == link and line.scope == ("exp1:12",)
    assert line.payload == (f"{link}.jsonl", 0, line.payload[2])
    assert records.read_link_payload(tmp_path, line) == result


def test_the_same_result_for_the_same_scope_is_stored_once(tmp_path):
    result = _result(made_from={UNIT: ("fast", "h1")})
    first = writer.save_links(tmp_path, result, scope=["exp1:12"], batch=None, stage=None)
    again = writer.save_links(tmp_path, result, scope=["exp1:12"], batch=None, stage=None)
    assert again == first
    assert len(records.read_links(tmp_path)) == 1


def test_a_relink_of_unchanged_fits_is_stored_once_whatever_its_name_start(tmp_path):
    branch = lk.Branch(id="link:exp1:12:a03#0", name="B1", members=((UNIT, 0), (OTHER, 0)))
    first = writer.save_links(tmp_path, _named(1, 2, branch), scope=["exp1:12"], batch=None, stage=None)
    again = writer.save_links(tmp_path, _named(2, 2, branch), scope=["exp1:12"], batch=None, stage=None)
    assert again == first and len(records.read_links(tmp_path)) == 1


def test_a_changed_result_is_a_new_record(tmp_path):
    result = _result(made_from={UNIT: ("fast", "h1")}, stamps={UNIT: "s1"})
    first = writer.save_links(tmp_path, result, scope=["exp1:12"], batch=None, stage=None)
    second = writer.save_links(tmp_path, replace(result, stamps={UNIT: "s2"}), scope=["exp1:12"], batch=None,
                               stage=None)
    assert second != first
    assert [line.link for line in records.read_links(tmp_path)] == [first, second]


def test_new_branches_continue_the_stores_names_and_keep_their_ids(tmp_path):
    writer.save_links(tmp_path, _result(next_name=4), scope=["exp1:12"], batch=None, stage=None)
    branch = lk.Branch(id="link:exp1:12:a03#0", name="B1", members=((UNIT, 0), (OTHER, 0)))
    link = writer.save_links(tmp_path, _named(1, 2, branch), scope=["exp1:13"], batch=None, stage=None)
    stored = records.read_link_payload(tmp_path, records.read_links(tmp_path)[-1])
    assert stored.branches == (replace(branch, name="B4"),)
    assert (stored.name_start, stored.next_name) == (4, 5)
    assert records.read_links(tmp_path)[-1].link == link and records.read_links(tmp_path)[-1].next_name == 5


def test_a_result_naming_no_new_branch_is_stored_as_it_is(tmp_path):
    writer.save_links(tmp_path, _result(next_name=4), scope=["exp1:12"], batch=None, stage=None)
    writer.save_links(tmp_path, _named(1, 1), scope=["exp1:13"], batch=None, stage=None)
    assert records.read_links(tmp_path)[-1].next_name == 1


def test_a_link_line_is_not_damage_to_the_inter_log_and_a_torn_one_is_skipped(tmp_path):
    writer.save_links(tmp_path, _result(), scope=["exp1:12"], batch=None, stage=None)
    assert records.read_log(tmp_path).damaged == ()
    with open(tmp_path / records.LOG, "ab") as f:
        f.write(b'{"schema":"isar.inter-log/1","kind":"link","link":"l0')
    assert len(records.read_links(tmp_path)) == 1
    assert [d.line for d in records.read_log(tmp_path).damaged] == [2]


def test_the_batch_save_target_saves_in_order_and_refuses_a_bad_payload_alone(tmp_path):
    good = {"schema": records.SCHEMA_LINK, "unit": "link:l1:link", "pass": "link", "scope": ["exp1:12"],
            "result": lk.to_json(_result(made_from={UNIT: ("fast", "h1")})), "batch": None, "stage": None}
    bad = {**good, "scope": []}
    first, refused, again = writer.save_link_payloads(tmp_path, [good, bad, good])
    assert first["saved"] and first["new"] and refused == "a link covers no scan"
    assert again == {"saved": True, "link": first["link"], "new": False}


def test_an_empty_scope_is_refused_and_nothing_is_written(tmp_path):
    with pytest.raises(writer.PayloadError):
        writer.save_links(tmp_path, _result(), scope=[], batch=None, stage=None)
    assert records.read_links(tmp_path) == ()


def test_previous_for_takes_the_newest_record_of_each_scan(tmp_path):
    # A series link keeps the series record's branches; a scan's stamps come from its newest record (the merge).
    series, one = _named(1, 4), replace(_named(1, 4), stamps={UNIT: "s2"})
    writer.save_links(tmp_path, series, scope=["exp1:12", "exp1:13"], batch=None, stage=None)
    writer.save_links(tmp_path, one, scope=["exp1:12"], batch=None, stage=None)
    assert linking.previous_for(tmp_path, ["exp1:12", "exp1:13"]) == (one, 4)
    assert linking.previous_for(tmp_path, ["exp1:12"]) == (one, 4)
    assert linking.previous_for(tmp_path, ["exp1:12", "exp1:14"]) == (one, 4)
    assert linking.previous_for(tmp_path, ["exp1:14"]) == (None, 4)


def test_a_series_relink_keeps_every_name_after_one_scan_and_series_records(tmp_path):
    # The series takes its names from the 12 record it extends, not from the 14 record it overlaps last.
    assert [b.name for b in _step(tmp_path, ["exp1:12"]).branches] == ["B1", "B2"]
    assert [b.name for b in _step(tmp_path, ["exp1:12", "exp1:13"]).branches] == ["B1", "B2"]
    alone = _step(tmp_path, ["exp1:14"])
    assert [b.name for b in alone.branches] == ["B3", "B4"] and alone.next_name == 5
    series = _step(tmp_path, ["exp1:12", "exp1:13", "exp1:14"])
    assert [b.name for b in series.branches] == ["B1", "B2"]
    assert [len(b.members) for b in series.branches] == [6, 6]
    assert series.next_name == 5


def test_previous_for_merges_the_newest_record_of_each_scan(tmp_path):
    x = lk.Branch("link:exp1:12:a03#0", "B1", ((UNIT, 0), (OTHER, 0)))
    series = replace(_named(1, 2, x), made_from={UNIT: ("fast", "h1"), OTHER: ("fast", "h2")},
                     stamps={UNIT: "s1", OTHER: "s2"})
    y = lk.Branch("link:exp1:12:a03#1", "B2", ((UNIT, 1),))
    newer = replace(_named(2, 3, replace(x, members=((UNIT, 0),)), y), made_from={UNIT: ("fast", "h1b")},
                    stamps={UNIT: "s1b"})
    writer.save_links(tmp_path, series, scope=["exp1:12", "exp1:13"], batch=None, stage=None)
    writer.save_links(tmp_path, newer, scope=["exp1:12"], batch=None, stage=None)
    merged, nxt = linking.previous_for(tmp_path, ["exp1:12", "exp1:13"])
    assert nxt == 3
    assert merged.branches == (replace(x, members=((UNIT, 0), (OTHER, 0))), y)
    assert (merged.made_from, merged.stamps) == ({UNIT: ("fast", "h1b"), OTHER: ("fast", "h2")},
                                                 {UNIT: "s1b", OTHER: "s2"})
    assert linking.previous_for(tmp_path, ["exp1:13"]) == (series, 3)
    # an unreadable newest record is skipped; the older record that covers its scans is used as stored
    (tmp_path / records.LINK_DIR / records.read_links(tmp_path)[-1].payload[0]).unlink()
    assert linking.previous_for(tmp_path, ["exp1:12", "exp1:13"]) == (series, 3)
