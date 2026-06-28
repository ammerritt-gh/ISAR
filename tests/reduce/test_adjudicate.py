"""adjudicate_records: dispatch flagged scans, parse/validate/repair, attach decisions + audit.

A FakeDispatcher drives every branch without a network: happy path, clean-scan skip, lenient parse
of fenced output, repair retry, and the hold-for-human fallback. The adjudicator is non-destructive —
it only writes ``record.adjudication`` and appends to ``card.state.corrections``.
"""
import json

import numpy as np

from isar.parsers import spec_s30
from isar.reduce import adjudicate
from isar.reduce.llm_client import DispatchResult
from isar.schema import axes, scan


class FakeDispatcher:
    def __init__(self, results):
        self._results = list(results)
        self.calls: list[dict] = []

    def dispatch(self, **kwargs) -> DispatchResult:
        self.calls.append(kwargs)
        return self._results.pop(0)


STRUCTURED_OK = {
    "decisions": [
        {"target": "point:ana1:3", "decision": "mask",
         "shape": "not_applicable", "reason": "cosmic ray", "confidence": 0.99},
    ],
    "scan_narrative": "masked one cosmic ray",
}


def _spec(det, intensity, *, point_flags=(), spectrum_flags=()):
    n = len(intensity)
    return scan.Spectrum(
        detector_id=det, q=[1.0, 0.0, 0.0], q_source="reported",
        intensity=np.asarray(intensity, float), variance=np.asarray(intensity, float),
        mask=np.ones(n, bool), point_flags=list(point_flags), spectrum_flags=list(spectrum_flags),
    )


def _record(spectra, *, scan_number=42, scan_type="measurement", scan_flags=()):
    n = len(next(iter(spectra.values())).intensity)
    energy = np.linspace(-2.0, 6.0, n)
    return scan.ScanRecord(
        experiment_id="X", scan_number=scan_number, scan_type=scan_type,
        scan_type_source="declared",
        scan_axes=[axes.energy_axis(energy), *axes.constant_q_axes(1.0, 0.0, 0.0, n)],
        spectra=spectra,
        normalization=scan.Normalization("time", np.full(n, 1.0), np.full(n, 1.0), 1.0),
        temperature=scan.Temperature(np.full(n, 2.0), {}, np.ones(n, bool)),
        environment=scan.Environment(), geometry=None,
        energy_calibration=scan.EnergyCalibration("energy", None, None, "needs_zero"),
        scan_flags=list(scan_flags), raw_header={},
        source=scan.Source("f", None, None, "p", "0", "2025", scan.SCHEMA_VERSION),
    )


def _flagged_record(scan_number=42):
    pf = scan.PointFlag(index=3, type="cosmic_candidate", reason="spike")
    return _record({"ana1": _spec("ana1", [40, 52, 60, 8000, 38, 35], point_flags=[pf])},
                   scan_number=scan_number)


def _clean_record(scan_number=99):
    return _record({"ana1": _spec("ana1", [1, 2, 3, 2, 1])}, scan_number=scan_number)


def test_adjudicate_attaches_decisions_and_appends_corrections():
    rec = _flagged_record()
    client = FakeDispatcher([DispatchResult(structured=STRUCTURED_OK, model_id="m1")])
    card = spec_s30.card_template()

    adjudicate.adjudicate_records([rec], card, client)

    assert rec.adjudication is not None
    assert rec.adjudication.decisions[0].decision == "mask"
    assert rec.adjudication.decisions[0].confidence == 0.99
    assert rec.adjudication.narrative == "masked one cosmic ray"
    assert rec.adjudication.model_id == "m1"
    corr = card.state.corrections
    assert len(corr) == 1
    assert corr[0]["source"] == "llm-adjudicate"
    assert corr[0]["target"] == "point:ana1:3"
    assert corr[0]["scan_number"] == rec.scan_number
    assert corr[0]["model_id"] == "m1"


def test_clean_scans_are_not_dispatched():
    flagged = _flagged_record(scan_number=1)
    clean = _clean_record(scan_number=2)
    client = FakeDispatcher([DispatchResult(structured=STRUCTURED_OK, model_id="m1")])

    adjudicate.adjudicate_records([clean, flagged], spec_s30.card_template(), client)

    assert len(client.calls) == 1            # only the flagged scan triggered a dispatch
    assert clean.adjudication is None
    assert flagged.adjudication is not None


def test_structured_null_falls_back_to_lenient_parse_of_output():
    rec = _flagged_record()
    fenced = "```json\n" + json.dumps(STRUCTURED_OK) + "\n```"
    client = FakeDispatcher([DispatchResult(structured=None, output=fenced, model_id="m1")])

    adjudicate.adjudicate_records([rec], spec_s30.card_template(), client)

    assert rec.adjudication.decisions[0].decision == "mask"


def test_nonconforming_then_repair_retry_succeeds():
    rec = _flagged_record()
    bad = DispatchResult(structured=None, output="not json at all", model_id="m1")
    good = DispatchResult(structured=STRUCTURED_OK, model_id="m1")
    client = FakeDispatcher([bad, good])

    adjudicate.adjudicate_records([rec], spec_s30.card_template(), client)

    assert len(client.calls) == 2            # one repair retry
    assert rec.adjudication.decisions[0].decision == "mask"


def test_unparseable_after_repair_holds_for_human():
    rec = _flagged_record()
    client = FakeDispatcher([
        DispatchResult(structured=None, output="garbage", model_id="m1"),
        DispatchResult(structured=None, output="still garbage", model_id="m1"),
    ])
    card = spec_s30.card_template()

    adjudicate.adjudicate_records([rec], card, client)

    assert len(rec.adjudication.decisions) == 1
    assert rec.adjudication.decisions[0].decision == "hold_for_human"
    assert rec.adjudication.decisions[0].target == "scan"
    assert len(card.state.corrections) == 1
    assert card.state.corrections[0]["decision"] == "hold_for_human"


def test_conforms_accepts_valid_and_rejects_bad_enum_and_extra_keys():
    s = adjudicate.DECISION_SCHEMA
    assert adjudicate._conforms({"decisions": [], "scan_narrative": "n"}, s)
    assert adjudicate._conforms(STRUCTURED_OK, s)
    assert not adjudicate._conforms({"scan_narrative": "n"}, s)       # missing 'decisions'
    bad_enum = {"decisions": [{"target": "scan", "decision": "banana",
                               "reason": "x", "confidence": 1}], "scan_narrative": "n"}
    assert not adjudicate._conforms(bad_enum, s)
    extra = {"decisions": [{"target": "scan", "decision": "keep", "reason": "x",
                            "confidence": 1, "extra": 9}], "scan_narrative": "n"}
    assert not adjudicate._conforms(extra, s)
