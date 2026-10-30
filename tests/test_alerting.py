"""Alerting tests: edge-triggered on the breach set, with recovery."""

from apbd_obs.alerting import breaching_tables, evaluate_alerts, load_state, save_state
from apbd_obs.scoring import FAIL, PASS, CheckResult


def results_for(*levels):
    """Build check results that yield the given per-table scores.

    A table with one weighted check: score = weight*credit/weight*100, so a
    single PASS => 100 and a single FAIL => 0.
    """
    out = []
    for table, status in levels:
        out.append(CheckResult(table, "row_count:rows", "row_count", status, "obs", 1))
    return out


def test_breaching_tables_picks_below_warn_gate():
    res = results_for(("marts.a", FAIL), ("marts.b", PASS))
    out = breaching_tables(res, warn_credit=0.5, error_level=60, warn_level=85)
    assert out == {"marts.a": "error"}
    # A WARN score (50) is below the warn gate but above error.
    warn_res = results_for(("marts.c", "WARN"))
    out = breaching_tables(warn_res, 0.5, 40, 85)
    assert out == {"marts.c": "warn"}


def test_first_run_alerts_and_persists_state(tmp_path):
    state = tmp_path / "state.json"
    out = tmp_path / "gen"
    res = results_for(("marts.a", FAIL))
    payloads = evaluate_alerts(res, 0.5, 60, 85, state_path=state, out_dir=out)
    assert len(payloads) == 1
    assert "ERROR" in payloads[0]["blocks"][0]["text"]["text"]
    assert load_state(state) == {"marts.a"}
    assert (out / "quality_alert_payload.json").exists()


def test_rerun_same_breach_does_not_re_alert(tmp_path):
    state = tmp_path / "state.json"
    out = tmp_path / "gen"
    res = results_for(("marts.a", FAIL))
    evaluate_alerts(res, 0.5, 60, 85, state_path=state, out_dir=out)
    second = evaluate_alerts(res, 0.5, 60, 85, state_path=state, out_dir=out)
    assert second == [], "a persistent breach must not re-alert every run"


def test_recovery_fires_all_clear(tmp_path):
    state = tmp_path / "state.json"
    out = tmp_path / "gen"
    bad = results_for(("marts.a", FAIL))
    good = results_for(("marts.a", PASS))
    evaluate_alerts(bad, 0.5, 60, 85, state_path=state, out_dir=out)
    payloads = evaluate_alerts(good, 0.5, 60, 85, state_path=state, out_dir=out)
    assert len(payloads) == 1
    assert "ALL-CLEAR" in payloads[0]["blocks"][0]["text"]["text"]
    assert load_state(state) == set()


def test_alert_payload_names_the_failing_checks(tmp_path):
    state = tmp_path / "state.json"
    out = tmp_path / "gen"
    res = [
        CheckResult("marts.a", "not_null:kode_skpd", "not_null", "FAIL", "12 NULL", 3),
        CheckResult("marts.a", "unique:id_bku", "unique", "FAIL", "5 ganda", 3),
    ]
    payloads = evaluate_alerts(res, 0.5, 60, 85, state_path=state, out_dir=out)
    text = str(payloads[0]["blocks"])
    assert "not_null:kode_skpd" in text
    assert "unique:id_bku" in text


def test_failing_checks_not_just_passes(tmp_path):
    state = tmp_path / "state.json"
    out = tmp_path / "gen"
    res = results_for(("marts.a", FAIL), ("marts.a", PASS), ("marts.a", PASS))
    payloads = evaluate_alerts(res, 0.5, 60, 85, state_path=state, out_dir=out)
    # score = 66.7 >= 60 -> warn-level (below 85) and only the FAIL listed.
    assert len(payloads) == 1
    assert "WARNING" in payloads[0]["blocks"][0]["text"]["text"]


def test_save_load_roundtrip(tmp_path):
    state = tmp_path / "state.json"
    save_state({"a", "b"}, state)
    assert load_state(state) == {"a", "b"}
