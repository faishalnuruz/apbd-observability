"""Lineage export + drift tests (pure)."""

from pathlib import Path

from apbd_obs.contract import load_contract
from apbd_obs.lineage import (
    LineageEdge,
    diff,
    expected_edges,
    mermaid,
    observed_edges_from_om,
    to_json,
)

ROOT = Path(__file__).resolve().parents[1]


def contract():
    return load_contract(ROOT / "config" / "contract.yaml")


def test_mermaid_contains_all_edges():
    text = mermaid(contract())
    assert "graph LR" in text
    assert "raw.transaksi --> staging.stg_transaksi" in text
    assert "staging.stg_transaksi --> marts.fct_realisasi" in text
    # The dashboard sits at the terminus of the chain.
    assert 'marts.fct_realisasi --> Rekap_Realisasi_APBD["Rekap Realisasi APBD"]' in text


def test_expected_edges_include_dashboard():
    edges = expected_edges(contract())
    assert LineageEdge("marts.fct_realisasi", "dashboard::Rekap Realisasi APBD") in edges
    assert LineageEdge("raw.transaksi", "staging.stg_transaksi") in edges


def test_to_json_is_valid_and_complete():
    import json

    doc = json.loads(to_json(contract()))
    assert doc["dashboard"] == "Rekap Realisasi APBD"
    kinds = {e["kind"] for e in doc["edges"]}
    assert kinds == {"table", "dashboard"}


def test_observed_edges_from_om_payload():
    payload = {
        "entity": "table::svc.db.marts.fct_realisasi",
        "upstreamEdges": [{"entity": "table::svc.db.staging.stg_transaksi"}],
        "downstreamEdges": [{"entity": "dashboard::metabase.Rekap Realisasi APBD"}],
    }
    edges = observed_edges_from_om(payload)
    assert LineageEdge("table::svc.db.staging.stg_transaksi",
                       "table::svc.db.marts.fct_realisasi") in edges
    assert LineageEdge("table::svc.db.marts.fct_realisasi",
                       "dashboard::metabase.Rekap Realisasi APBD") in edges


def test_diff_reports_both_directions():
    expected = {LineageEdge("a", "b"), LineageEdge("b", "c")}
    observed = {LineageEdge("a", "b"), LineageEdge("x", "y")}
    d = diff(expected, observed)
    assert d["missing_from_catalog"] == [LineageEdge("b", "c")]
    assert d["stale_in_contract"] == [LineageEdge("x", "y")]


def test_diff_clean_when_identical():
    edges = {LineageEdge("a", "b")}
    d = diff(edges, set(edges))
    assert d == {"missing_from_catalog": [], "stale_in_contract": []}
