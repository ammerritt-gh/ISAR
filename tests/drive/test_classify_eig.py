import math

import pytest

from isar.drive.acquisition.classify_eig import (
    ESTIMATOR_VERSION, assignment_mi, belief_predict, information_per_cost, lambda_eff, opening_order,
    widen_factor,
)
from isar.schema.hypotheses import preset
from isar.session.branch_assignment import BranchBelief
import numpy as np


def test_componentwise_gh32_mi_is_deterministic_and_bounded_by_label_entropy():
    components = [{"mean": 1.0, "sigma": 0.2, "probability": 0.5},
                  {"mean": 5.0, "sigma": 0.2, "probability": 0.5}]
    first = assignment_mi(components, meas_sigma=0.2)
    assert first == assignment_mi(components, meas_sigma=0.2)
    assert 0.0 < first <= math.log(2.0) + 1e-10
    assert "gh32-component" in ESTIMATOR_VERSION


def test_classification_information_vanishes_when_labels_or_bands_are_settled():
    assert assignment_mi([{"mean": 1, "sigma": 1, "probability": 1}], meas_sigma=0.2) == 0.0
    close = [{"mean": 1, "sigma": 0.1, "probability": 0.5},
             {"mean": 1.2, "sigma": 0.1, "probability": 0.5}]
    assert assignment_mi(close, meas_sigma=0.2, differentiation_tolerance_meV=0.2) == 0.0


def test_information_is_cost_normalized_and_lambda_decays_with_confidence():
    assert information_per_cost(2.0, 1.0, classification_weight=0.5, cost=5.0) == 0.5
    assert lambda_eff(1.0, [0.5, 0.5]) > lambda_eff(1.0, [0.99, 0.01])


def test_opening_books_and_absent_signal_schedule():
    us = [0.0, 0.1, 0.2]
    pred = {0.0: (0.0, 1.0), 0.1: (0.6, 3.0), 0.2: (1.4, 2.0)}
    assert opening_order(us, {"name": "optic"}, resolution_fwhm=1.0, predictions=pred)[0] == 0.0
    assert opening_order(us, {"name": "acoustic"}, resolution_fwhm=1.0, predictions=pred)[0] == 0.2
    assert opening_order(us, None, resolution_fwhm=1.0, predictions=pred)[0] == 0.1
    assert [widen_factor(i) for i in range(5)] == [1.0, 1.5, 2.0, 2.5, 2.5]


def test_drive_belief_prediction_matches_session_with_and_without_prior():
    anchors = [{"u": 0.1, "omega0": 5.8, "weight": 0.8},
               {"u": 0.2, "omega0": 5.9, "weight": 1.0}]
    prior = preset("optic").to_dict()
    row = {"anchors": anchors, "parameter_prior": prior}
    session = BranchBelief("optic", np.array([0.1, 0.2]), np.array([5.8, 5.9]),
                           np.array([0.8, 1.0]), parameter_prior=prior)
    assert belief_predict(row, 0.15) == pytest.approx(session.predict(0.15))
    assert belief_predict(row, 0.15, data_only=True) == pytest.approx(
        session.predict_data_only(0.15))
