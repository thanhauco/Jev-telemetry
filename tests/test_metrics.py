from jev_telemetry.eval import best_threshold_for_precision, binary_metrics, brier, ece, reliability


def test_binary_metrics():
    m = binary_metrics([True, True, False, False], [0.9, 0.4, 0.6, 0.1])
    assert (m.tp, m.fp, m.fn, m.tn) == (1, 1, 1, 1)
    assert m.precision == 0.5 and m.recall == 0.5


def test_perfect_calibration_has_zero_ece():
    y = [True] * 3 + [False] * 7
    p = [0.3] * 10
    assert ece(y, p) == 0.0
    assert reliability(y, p) == [{"bin": "0.3-0.4", "n": 10, "mean_p": 0.3, "observed": 0.3}]


def test_overconfident_has_high_ece():
    assert ece([True, False] * 50, [0.99] * 100) > 0.45


def test_brier_and_threshold():
    assert brier([True, False], [1.0, 0.0]) == 0.0
    y = [True, True, False, True, False]
    p = [0.95, 0.9, 0.85, 0.6, 0.2]
    assert best_threshold_for_precision(y, p, 1.0) == 0.9
