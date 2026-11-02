"""The link store: a saved mode-linking result reads back equal, is stored once per content, and takes its branch
names from the store's own sequence, so a save never refuses on names (C-link-store, amendment A4).

Prevents: one result stored as two records, two results' display names colliding after a re-link, a payload
read back as a different result, and a link line the inter-spectra log reader counts as damage.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from isar.analyze.coupled import linking as lk
from isar.analyze.coupled.settings import InterSettings
from isar.session import writer
from isar.session.inter_spectra import linking, records

UNIT, OTHER = "exp1:12:a03", "exp1:13:a03"


def _result(**changes) -> lk.LinkResult:
    """An empty linking result with the given fields replaced: the store reads only its content."""
    return replace(lk.link({}, (), InterSettings()), **changes)


def _named(name_start: int, next_name: int, *branches: lk.Branch) -> lk.LinkResult:
    return _result(branches=branches, name_start=name_start, next_name=next_name,
                   made_from={UNIT: ("fast", "h1")}, stamps={UNIT: "s1"})


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


def test_previous_for_continues_the_newest_record_covering_every_scan(tmp_path):
    # A series link must continue the series record, not a newer one-scan link that drops other temperatures' branches.
    series, one = _named(1, 4), replace(_named(1, 4), stamps={UNIT: "s2"})
    writer.save_links(tmp_path, series, scope=["exp1:12", "exp1:13"], batch=None, stage=None)
    writer.save_links(tmp_path, one, scope=["exp1:12"], batch=None, stage=None)
    assert linking.previous_for(tmp_path, ["exp1:12", "exp1:13"]) == (series, 4)
    assert linking.previous_for(tmp_path, ["exp1:12"]) == (one, 4)
    assert linking.previous_for(tmp_path, ["exp1:12", "exp1:14"]) == (one, 4)
    assert linking.previous_for(tmp_path, ["exp1:14"]) == (None, 4)
