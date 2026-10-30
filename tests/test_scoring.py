"""Scoring tests: the arithmetic the whole portfolio claim rests on."""

from apbd_obs.scoring import (
    FAIL,
    PASS,
    WARN,
    CheckResult,
    level,
    portfolio_score,
    score_table,
)


def r(table, status, weight=1.0, check_id="c:col", check_type="not_null"):
    return CheckResult(table, check_id, check_type, status, "obs", weight)


def test_all_pass_is_100():
    results = [r("a.t", PASS), r("a.t", PASS, weight=3)]
    assert score_table(results, "a.t") == 100.0


def test_all_fail_is_0():
    results = [r("a.t", FAIL), r("a.t", FAIL, weight=3)]
    assert score_table(results, "a.t") == 0.0


def test_warn_earns_half_credit():
    # 1 PASS (w=1) + 1 WARN (w=1): (1 + 0.5) / 2 = 75
    results = [r("a.t", PASS), r("a.t", WARN)]
    assert score_table(results, "a.t") == 75.0


def test_weights_matter():
    # FAIL on the heavy check hurts more: 3 PASS (w=1) + FAIL (w=3)
    # -> 3/6 = 50, while swapping weights gives 3/4 = 75.
    heavy_fail = [r("a.t", PASS), r("a.t", PASS), r("a.t", PASS), r("a.t", FAIL, weight=3)]
    assert score_table(heavy_fail, "a.t") == 50.0
    heavy_pass = [r("a.t", FAIL), r("a.t", PASS, weight=3)]
    assert score_table(heavy_pass, "a.t") == 75.0


def test_custom_warn_credit():
    results = [r("a.t", WARN)]
    assert score_table(results, "a.t", warn_credit=1.0) == 100.0
    assert score_table(results, "a.t", warn_credit=0.0) == 0.0


def test_table_without_checks_scores_100():
    assert score_table([r("a.t", FAIL)], "b.t") == 100.0


def test_portfolio_is_plain_mean():
    # a.t = 100, b.t = 0 -> 50
    results = [r("a.t", PASS), r("b.t", FAIL)]
    assert portfolio_score(results) == 50.0


def test_portfolio_empty_is_100():
    assert portfolio_score([]) == 100.0


def test_level_gates():
    assert level(59.9, 60, 85) == "error"
    assert level(60, 60, 85) == "warn"
    assert level(84.9, 60, 85) == "warn"
    assert level(85, 60, 85) == "ok"
    assert level(100, 60, 85) == "ok"


def test_check_result_credit_field():
    assert r("t", PASS).credit == 1.0
    assert r("t", WARN).credit == 0.5
    assert r("t", FAIL).credit == 0.0
