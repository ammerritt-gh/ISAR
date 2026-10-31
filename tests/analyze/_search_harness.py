"""The scripted pricer and the snapshot builder of the search tests (S3U2 D14). Every value is made up.

``Scripted(table)`` is a ``price_one`` for ``search.drive``: for a job it returns one basin, at the energies of
the job's first start, whose ledger mass (``ln_z`` including the labelling weight) is the table value of its
(count, labels in ascending start energy); unlisted labellings return ``default``. Options script the rest:
per-count outcomes without a price, a second crossed basin, a basin pinned at fixed energies, several variants
and a split (placed) pair. ``snapshot_for`` builds a real ``Snapshot`` on a generated spectrum, so ``_Spec``
works; ``run`` drives a real ``Search`` with a ``Scripted``; ``job_for`` makes the job the search would make
for one labelling (the incoming fit's values for the incoming labelling, else the neighbours' energies).
"""
from __future__ import annotations

from isar.analyze import fast_pass
from isar.analyze.coupled import rules
from isar.analyze.coupled.refit import open_search
from isar.analyze.coupled.search import drive
from isar.analyze.coupled.types import Priced, Snapshot, Start, TermInput
from isar.session.inter_spectra.graph import Term
from test_coupled_refit import S, held, record, spectrum

NOQE = (True, True, False)


class Scripted:
    def __init__(self, table=None, *, default=-120.0, peak=(20.0, 1.0), outcomes=None, crossed=None, at=None,
                 variants=None, split=()):
        """table {(k, labels): mass}; default: a mass, or {k: mass} with -120 elsewhere; outcomes {k or (k, labels):
        (status, reason)}; crossed {(k, labels): (crossed labels, mass, also)}: a basin at the same energies under
        the crossed labels (``also`` False: the only basin); at {(k, labels): energies} pins the basin (default:
        where the labelling's first start was, so every start of one labelling reaches one basin); variants
        {(k, labels): [(variant, mass), ...]}; split: labellings whose first two modes come back as a placed pair."""
        self.table, self.default, self.peak = dict(table or {}), default, peak
        self.outcomes, self.crossed, self.at = outcomes or {}, crossed or {}, at or {}
        self.variants, self.split = variants or {}, set(split)
        self.M, self.asked = None, []

    def value(self, k, labels):
        if (k, labels) in self.table:
            return self.table[(k, labels)]
        return self.default.get(k, -120.0) if isinstance(self.default, dict) else self.default

    def basin(self, k, labels, energies, mass, variant=NOQE, tag="scripted"):
        modes = tuple((b, e, 0.5, 10.0, False) for b, e in zip(labels, energies))
        label = f"{tag}{variant}"
        if (k, labels) in self.split:
            e = (energies[0] + energies[1]) / 2.0
            modes = ((labels[0], e, 0.5, 10.0, False), (labels[1], e, 0.5, 10.0, False)) + modes[2:]
            label += "|split"
        term = mass - rules.ln_w_label(k, sum(b is not None for b in labels), self.M)
        mode = (*variant, (), tuple(m[1] for m in modes))
        return (term, label, modes, (None, None), mode, mode)

    def __call__(self, job):
        self.asked.append(job)
        k, st = job.count, job.starts[0]
        order = sorted(range(k), key=lambda i: (st.modes[i][0], str(job.slots[i])))
        labels = tuple(job.slots[i] for i in order)
        # one basin per labelling: every start of it lands where the first one did (unless pinned by ``at``)
        energies = self.at.setdefault((k, labels), tuple(st.modes[i][0] for i in order))
        out = self.outcomes.get((k, labels)) or self.outcomes.get(k)
        if out is not None:
            return Priced(job.key, k, job.slots, None, out[0], None, out[1], len(job.starts), 0.001)
        basins = [self.basin(k, labels, energies, m, v) for v, m in self.variants.get((k, labels), [])]
        basins = basins or [self.basin(k, labels, energies, self.value(k, labels))]
        if (k, labels) in self.crossed:
            other, mass, also = self.crossed[(k, labels)]
            basins = (basins if also else []) + [self.basin(k, other, energies, mass, tag="crossed")]
        best = max(basins, key=lambda b: b[0])
        lnz = best[0] + rules.ln_w_label(k, sum(b is not None for b in labels), self.M)
        return Priced(job.key, k, job.slots, lnz, "ok", best[4], None, len(job.starts), 0.001, tuple(basins), (),
                      self.peak)


def run(snapshot, scripted):
    """(Assignment, prices, the Search) of one scripted search."""
    s = open_search(snapshot)
    scripted.M = s.M
    a, used = drive(s, scripted)
    return a, used, s


def snapshot_for(own, own_branches, neighbours=None, *, curvature=None, settings=S, aliases=None, rec=None,
                 kind="anchored"):
    """The spectrum y (200 K) with its incoming fit ``own`` [(omega0, Gamma, height)] named ``own_branches``;
    ``neighbours`` {unit: (modes, branches)} each joined to y by a T-change term (100 K); ``curvature``
    ((left modes, branches), (right modes, branches)) adds a curvature triple x-y-z."""
    terms = [TermInput(Term("T-change", (u, "y"), (100.0,), None, None), (held(u, m, b, T=100.0),))
             for u, (m, b) in (neighbours or {}).items()]
    if curvature is not None:
        (lm, lb), (rm, rb) = curvature
        terms.append(TermInput(Term("curvature", ("x", "y", "z"), (0.1, 0.1), 0.5, "row 1"),
                               (held("x", lm, lb, T=100.0), held("z", rm, rb, T=300.0))))
    prep = spectrum(own or [(8.0, 0.5, 10.0)], ceiling=3)
    return Snapshot(snapshot_id="b:ring1", stage_id="b:ring1", unit="y", fit_id="f00000000000a", context=prep,
                    own=held("y", own, own_branches, T=200.0), own_record=rec or record(own), terms=tuple(terms),
                    settings=settings, batch_kind=kind, aliases=aliases or {})


def job_for(snapshot, k, slots):
    """The job the search would make for one labelling: the incoming fit's values for the incoming labelling,
    else each matched slot at a neighbour's energy of its branch and each unmatched slot at an incoming mode's
    (or mid-window), heights left to the optimizer."""
    s = open_search(snapshot)
    s.attempted.clear()                     # its own seeds are not priced: the job is made as if fresh
    slots = tuple(slots)
    if (k, slots) == (s.n0, s.inc):
        start = Start("own", tuple((m.omega0, m.gamma, m.height) for m in s.spec.own.modes))
    else:
        spare = iter([(m.omega0, m.gamma) for m in s.spec.own.modes])
        modes = []
        for b in slots:
            md = next((m for h in s.neighbours.values() for m in h.modes
                       if b is not None and s.spec.canon(m.branch) == s.spec.canon(b)), None)
            e, g = (md.omega0, md.gamma) if md else next(spare, (0.5 * (s.spec.lo + s.spec.hi),
                                                                 fast_pass.LADDER_GAMMA))
            modes.append((e, g, None))
        start = Start("neighbour", tuple(modes))
    return s._jobs(k, slots, [start], "seed", "test")[0]


def ledger(snapshot, priced):
    """A fresh ``Search`` whose ledger has filed every basin of ``priced`` (real or scripted prices) as ``feed``
    files them: under the labelling of its fitted energies, a repeated basin once. ``s._mass(key)`` is then the mass
    the decision weighs."""
    s = open_search(snapshot)
    for p in priced:
        s.meta[p.key] = {"purpose": "seed", "source": "test", "new": ()}
        for rec in p.basins:
            s._file(rec, p, p)              # _file reads only the job's key
    return s
