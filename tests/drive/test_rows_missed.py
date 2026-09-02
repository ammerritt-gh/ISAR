"""``_rows_missed``: which per-scan dispositions count as a missed energy window."""
from isar.drive import loop as loop_mod


def test_unmodeled_row_is_not_a_miss():
    rows = [{"disposition": "unmodeled"}]
    assert loop_mod._rows_missed(rows) is False


def test_only_unusable_rows_is_a_miss():
    rows = [{"disposition": "abstain"}, {"disposition": "no_signal"}]
    assert loop_mod._rows_missed(rows) is True


def test_empty_rows_is_a_miss():
    assert loop_mod._rows_missed([]) is True
