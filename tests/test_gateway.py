"""Gateway policy tests: masking, row filters, fail-closed denials (pure)."""

from pathlib import Path

import pytest

from apbd_obs.gateway import (
    AccessDenied,
    RolePolicy,
    load_policies,
    plan_query,
)

ROOT = Path(__file__).resolve().parents[1]

KEPALA = RolePolicy("kepala_bpkd", "", ("marts.fct",), None, ())
BIDANG = RolePolicy(
    "bidang_kesehatan", "", ("marts.fct", "marts.bku"),
    {"kode_skpd": ["1.05"]}, ("nik_pelapor",)
)


def test_masked_column_becomes_expression():
    plan = plan_query(BIDANG, "marts.bku", ["id_bku", "kode_skpd", "nik_pelapor"])
    assert "left(nik_pelapor, 2) || '***' AS nik_pelapor" in plan.sql
    assert plan.masked_columns == ["nik_pelapor"]


def test_unmasked_column_stays_bare():
    plan = plan_query(KEPALA, "marts.fct", ["kode_skpd", "pagu"])
    assert "left(" not in plan.sql
    assert plan.masked_columns == []


def test_row_filter_becomes_parameterised_any():
    plan = plan_query(BIDANG, "marts.fct", ["kode_skpd", "pagu"])
    assert "kode_skpd = ANY(%s)" in plan.sql
    assert plan.params == [["1.05"]]
    # The value must never be interpolated into the SQL string.
    assert "'1.05'" not in plan.sql


def test_row_filter_column_must_be_selected():
    with pytest.raises(AccessDenied, match="tidak ikut dipilih"):
        plan_query(BIDANG, "marts.fct", ["pagu"])


def test_table_outside_policy_denied():
    with pytest.raises(AccessDenied, match="tidak punya akses"):
        plan_query(KEPALA, "raw.transaksi", ["id_bku"])


def test_column_injection_denied():
    with pytest.raises(AccessDenied, match="kolom tidak aman"):
        plan_query(KEPALA, "marts.fct", ["pagu; DROP TABLE fct"])


def test_limit_bounds_enforced():
    with pytest.raises(AccessDenied, match="limit"):
        plan_query(KEPALA, "marts.fct", ["pagu"], limit=0)
    with pytest.raises(AccessDenied, match="limit"):
        plan_query(KEPALA, "marts.fct", ["pagu"], limit=10_001)


def test_order_and_limit_applied():
    plan = plan_query(KEPALA, "marts.fct", ["kode_skpd"], limit=7)
    assert plan.sql.endswith("ORDER BY 1 LIMIT 7")


def test_load_real_policies():
    policies = load_policies(ROOT / "config" / "rbac.yaml")
    assert set(policies) == {
        "kepala_bpkd", "bidang_kesehatan", "bidang_pendidikan", "auditor", "it_admin",
    }
    # Bidang sees one SKPD only; auditor sees all but loses the NIK.
    assert policies["bidang_kesehatan"].row_filter == {"kode_skpd": ["1.05"]}
    assert policies["auditor"].row_filter is None
    assert "nik_pelapor" in policies["auditor"].mask_columns
    # Only IT admin may touch the raw layer.
    assert "raw.transaksi" in policies["it_admin"].tables
    for role in ("kepala_bpkd", "bidang_kesehatan", "bidang_pendidikan", "auditor"):
        assert "raw.transaksi" not in policies[role].tables
