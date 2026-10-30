"""Prometheus exposition rendering tests (pure)."""

from apbd_obs.metrics import render_metrics
from apbd_obs.scoring import FAIL, PASS, WARN, CheckResult


def results():
    return [
        CheckResult("marts.fct", "row_count:rows", "row_count", PASS, "5 baris", 3),
        CheckResult("marts.fct", "not_null:kode_skpd", "not_null", FAIL, "1 NULL", 2),
        CheckResult("marts.dim", "unique:kode_skpd", "unique", PASS, "0 ganda", 1),
        CheckResult("stg.t", "freshness:loaded_at", "freshness", WARN, "3.0 jam", 1),
    ]


def render(**overrides):
    kwargs = dict(
        results=results(),
        warn_credit=0.5,
        error_level=60,
        warn_level=85,
        freshness={"marts.fct": 2.6, "stg.t": 4.5},
        catalog_tables=5,
        audit_counts={"allowed": 5, "denied": 2},
    )
    kwargs.update(overrides)
    return render_metrics(**kwargs)


def test_table_scores_present():
    text = render()
    # marts.fct: 3 PASS + 2 FAIL -> (3)/(5) = 60.0 ; marts.dim = 100 ; stg.t WARN = 50
    assert 'apbd_quality_score{table="marts.fct"} 60.0' in text
    assert 'apbd_quality_score{table="marts.dim"} 100.0' in text
    assert 'apbd_quality_score{table="stg.t"} 50.0' in text


def test_portfolio_and_level():
    # scores: fct 60, dim 100, stg.t 50 -> mean 70 -> warn (60 <= 70 < 85)
    text = render()
    assert "apbd_quality_score_portfolio 70.0" in text
    assert "apbd_quality_level 1" in text


def test_level_encoding():
    assert "apbd_quality_level 2" in render(error_level=95)  # everything errors
    assert "apbd_quality_level 0" in render(warn_level=10)   # everything ok


def test_check_status_values():
    text = render()
    fail = 'apbd_check_status{table="marts.fct",check="not_null:kode_skpd",status="FAIL"} 0.0'
    warn = 'apbd_check_status{table="stg.t",check="freshness:loaded_at",status="WARN"} 0.5'
    passed = 'apbd_check_status{table="marts.dim",check="unique:kode_skpd",status="PASS"} 1.0'
    assert fail in text
    assert warn in text
    assert passed in text


def test_freshness_and_counters():
    text = render()
    assert 'apbd_freshness_hours{table="stg.t"} 4.5' in text
    assert 'apbd_audit_events_total{decision="denied"} 2' in text
    assert "apbd_catalog_tables 5" in text


def test_every_metric_family_declares_type():
    text = render()
    for family in (
        "apbd_quality_score", "apbd_quality_score_portfolio", "apbd_quality_level",
        "apbd_check_status", "apbd_freshness_hours", "apbd_catalog_tables",
        "apbd_audit_events_total",
    ):
        assert f"# TYPE {family} gauge" in text or f"# TYPE {family} counter" in text


def test_output_ends_with_newline():
    assert render().endswith("\n")
