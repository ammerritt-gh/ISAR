"""A mode not observed in its own spectrum supports no prediction (S1U4, C-not-observed).

Prevents: a phantom mode (a noise peak or background wiggle inherited from the solo fit) carrying its branch into
the spectra around it, so the branch is predicted and added where nothing observes it. Matching is unchanged: the
phantom still takes part in linking and keeps its name.
"""
from isar.analyze.coupled import linking as lk
from isar.analyze.coupled.settings import InterSettings
from isar.analyze.coupled.types import Held, HeldMode
from isar.session.inter_spectra.graph import Link

SET = InterSettings(link_slope_meV_per_invA=8.0, width_tolerance_ln=0.7)
ROW = (0.1, 0.0, 0.0)


def unit(name, omega0, status="determined_by_own_data"):
    """One spectrum with one mode (Gamma 0.2, area 10), built as test_coupled_linking.py builds its spectra."""
    rows = [(omega0, 0.2, 10.0)]
    mode = HeldMode(branch=f"fast:{name}#0", omega0=omega0, gamma=0.2, height=1.0, guard=None,
                    guard_source="unavailable", status=status, regime="underdamped", pinned=False)
    held = Held(unit=name, fit="fast", inputs="h-" + name, window=(1.0, 12.0), modes=(mode,), dormant=(), T=100.0)
    return lk.LinkInput(held=held, fwhm=0.4, areas=(10.0,), sigma_areas=(None,), sigma_gammas=(None,),
                        at_cap=(False,), stamp=lk.fit_stamp("fast", "h-" + name, rows), scale=1.0)


def row(middle_status):
    """Three spectra along q: the outer two observe the branch at 8 meV, the middle one has the given status."""
    units = {"a01": unit("a01", 8.0), "a02": unit("a02", 8.1, middle_status), "a03": unit("a03", 8.2)}
    links = [Link("q", "a01", "a02", 0.1, ROW, "lattice"), Link("q", "a02", "a03", 0.1, ROW, "lattice")]
    return lk.link(units, links, SET)


def test_a_not_observed_middle_mode_keeps_its_name_and_predicts_nothing_outward():
    r = row("not_observed_here")
    b = r.labels["a02"][0]
    assert b is not None
    assert r.labels["a01"] == (b,) and r.labels["a03"] == (b,)          # matching is unchanged: it still links
    assert r.predictions["a01"] == () and r.predictions["a03"] == ()    # the middle mode carries no prediction


def test_the_same_row_predicts_the_branch_outward_when_the_middle_mode_is_observed():
    r = row("determined_by_own_data")
    b = r.labels["a02"][0]
    assert [p.branch for p in r.predictions["a01"]] == [b]
    assert [p.branch for p in r.predictions["a03"]] == [b]
