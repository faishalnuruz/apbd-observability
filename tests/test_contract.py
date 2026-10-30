"""Contract loading and validation tests (pure, no stack)."""

from pathlib import Path

import pytest
import yaml

from apbd_obs.contract import CheckSpec, load_contract

ROOT = Path(__file__).resolve().parents[1]


def test_real_contract_loads():
    contract = load_contract(ROOT / "config" / "contract.yaml")
    names = [t.name for t in contract.tables]
    assert names == [
        "raw.transaksi",
        "staging.stg_transaksi",
        "marts.transaksi_bku",
        "marts.fct_realisasi",
        "marts.dim_skpd",
    ]
    assert contract.warn_credit == 0.5
    assert contract.error_level == 60
    assert contract.warn_level == 85
    assert contract.dashboard.name == "Rekap Realisasi APBD"


def test_real_contract_freshness_matches_prd():
    """The PRD says warn at 2h, error at 4h — the contract must say the same."""
    contract = load_contract(ROOT / "config" / "contract.yaml")
    fresh = [
        c for t in contract.tables for c in t.checks if c.type == "freshness"
    ]
    assert fresh, "no freshness checks in the contract"
    for c in fresh:
        assert c.warn_hours == 2
        assert c.error_hours == 4


def test_real_contract_covers_prd_quality_gates():
    """Every check kind named in the PRD's quality-gates example exists."""
    contract = load_contract(ROOT / "config" / "contract.yaml")
    kinds = {c.type for t in contract.tables for c in t.checks}
    assert {"not_null", "unique", "freshness", "row_count"} <= kinds
    # P2's invariant carried over: realisasi must never exceed pagu.
    invariants = [
        c for t in contract.tables for c in t.checks if c.type == "invariant"
    ]
    assert any("realisasi <= pagu" in c.expression for c in invariants)


def test_check_id_is_stable():
    c = CheckSpec(type="not_null", column="kode_skpd", weight=3)
    assert c.id == "not_null:kode_skpd"


def _write(tmp_path, doc):
    p = tmp_path / "contract.yaml"
    p.write_text(yaml.safe_dump(doc))
    return p


def _minimal(**overrides):
    doc = {
        "tables": [
            {
                "name": "marts.t",
                "description": "d",
                "owner": "o",
                "checks": [{"type": "row_count", "min": 1, "max": 2, "weight": 1}],
            }
        ],
        "lineage": [],
        "dashboard": {"service": "s", "name": "n", "feeds": []},
    }
    doc.update(overrides)
    return doc


def test_unknown_check_type_rejected(tmp_path):
    doc = _minimal()
    doc["tables"][0]["checks"] = [{"type": "shadow_count", "weight": 1}]
    with pytest.raises(ValueError, match="unknown check type"):
        load_contract(_write(tmp_path, doc))


def test_warn_hours_must_be_below_error_hours(tmp_path):
    doc = _minimal()
    doc["tables"][0]["checks"] = [
        {"type": "freshness", "column": "loaded_at", "warn_hours": 4, "error_hours": 2}
    ]
    with pytest.raises(ValueError, match="warn_hours must be <"):
        load_contract(_write(tmp_path, doc))


def test_row_count_needs_sane_bounds(tmp_path):
    doc = _minimal()
    doc["tables"][0]["checks"] = [{"type": "row_count", "min": 10, "max": 2}]
    with pytest.raises(ValueError, match="min <= max"):
        load_contract(_write(tmp_path, doc))


def test_column_identifier_is_sanitised(tmp_path):
    doc = _minimal()
    doc["tables"][0]["checks"] = [
        {"type": "not_null", "column": 'kode; DROP TABLE x', "weight": 1}
    ]
    with pytest.raises(ValueError, match="simple column identifier"):
        load_contract(_write(tmp_path, doc))


def test_check_entry_must_declare_type(tmp_path):
    doc = _minimal()
    doc["tables"][0]["checks"] = [{"column": "a", "weight": 1}]
    with pytest.raises(ValueError, match="needs a 'type' key"):
        load_contract(_write(tmp_path, doc))


def test_check_entry_rejects_unknown_fields(tmp_path):
    doc = _minimal()
    doc["tables"][0]["checks"] = [{"type": "row_count", "min": 1, "max": 2, "foo": 3}]
    with pytest.raises(ValueError, match="unknown fields"):
        load_contract(_write(tmp_path, doc))


def test_lineage_edges_parsed(tmp_path):
    doc = _minimal()
    doc["lineage"] = [{"from": "marts.a", "to": "raw.b"}]
    contract = load_contract(_write(tmp_path, doc))
    assert contract.lineage[0].from_ == "marts.a"
    assert contract.lineage[0].to == "raw.b"
