"""Integration tests — need the compose stack running.

Run with:  python -m pytest -m integration
Everything here talks to real infrastructure: the seeded PostgreSQL data
plane, the OpenMetadata platform, Prometheus, and Grafana.
"""

from pathlib import Path

import pytest
import requests
import yaml

from apbd_obs import catalog, quality
from apbd_obs import seed as seedmod
from apbd_obs.contract import load_contract
from apbd_obs.gateway import AccessDenied, Gateway, load_policies
from apbd_obs.scoring import FAIL, PASS, WARN, portfolio_score, score_table

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "config" / "contract.yaml"
RBAC = ROOT / "config" / "rbac.yaml"
DSN = "host=localhost port=5433 dbname=bpkd user=bpkd password=bpkd-2026"

OM_API = "http://localhost:8585/api"
PROM = "http://localhost:9090"
GRAFANA = "http://localhost:3301"


def _stack_up() -> bool:
    try:
        import psycopg

        with psycopg.connect(DSN, connect_timeout=3):
            pass
        return requests.get(f"{OM_API}/v1/system/health", timeout=5).ok
    except Exception:
        return False


pytestmark = pytest.mark.integration
if not _stack_up():
    pytest.skip(
        "compose stack not reachable (docker compose up -d first)",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def contract():
    return load_contract(CONTRACT)


@pytest.fixture(scope="module")
def fresh_seed():
    """Re-seed right before checking so freshness offsets are exact."""
    import psycopg

    with psycopg.connect(DSN) as conn:
        return seedmod.seed(conn)


@pytest.fixture(scope="module")
def results(fresh_seed, contract):
    import psycopg

    with psycopg.connect(DSN) as conn:
        return quality.run_checks(conn, contract)


# ---------------------------------------------------------------- seed + checks


def test_seed_row_counts(fresh_seed):
    assert fresh_seed == {
        "raw.transaksi": 9417,
        "staging.stg_transaksi": 8845,
        "marts.transaksi_bku": 8845,
        "marts.dim_skpd": 5,
        "marts.fct_realisasi": 5,
    }


def test_seeded_defect_counts(fresh_seed):
    """The defects the README claims are really in the data plane."""
    import psycopg

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM raw.transaksi WHERE kode_skpd IS NULL")
            assert cur.fetchone()[0] == 12
            cur.execute(
                "SELECT COUNT(*) FROM (SELECT id_bku FROM raw.transaksi "
                "GROUP BY id_bku HAVING COUNT(*) > 1) d"
            )
            assert cur.fetchone()[0] == 5
            cur.execute(
                "SELECT COUNT(*) FROM marts.fct_realisasi WHERE realisasi > pagu"
            )
            assert cur.fetchone()[0] == 1


def test_checks_detect_every_planted_defect(results):
    by_id = {(r.table, r.check_id): r for r in results}
    # raw.transaksi: NULLs + duplicates fail; freshness + volume pass.
    assert by_id[("raw.transaksi", "not_null:kode_skpd")].status == FAIL
    assert "12 NULL" in by_id[("raw.transaksi", "not_null:kode_skpd")].observed
    assert by_id[("raw.transaksi", "unique:id_bku")].status == FAIL
    assert by_id[("raw.transaksi", "freshness:loaded_at")].status == PASS
    # staging: stale (4.5h > 4h) + volume drop (8845 < 9000) fail.
    assert by_id[("staging.stg_transaksi", "freshness:loaded_at")].status == FAIL
    assert by_id[("staging.stg_transaksi", "row_count:rows")].status == FAIL
    # fct: the P2 invariant + a freshness WARN between the PRD's 2h and 4h.
    assert by_id[("marts.fct_realisasi", "invariant:realisasi <= pagu")].status == FAIL
    assert by_id[("marts.fct_realisasi", "freshness:updated_at")].status == WARN
    # Healthy tables prove the scorer is not failing everything.
    for t in ("marts.transaksi_bku", "marts.dim_skpd"):
        for r in results:
            if r.table == t:
                assert r.status == PASS, r


def test_scores_match_the_documented_story(results, contract):
    scores = {
        t.name: score_table(results, t.name, contract.warn_credit)
        for t in contract.tables
    }
    assert scores == {
        "raw.transaksi": 40.0,
        "staging.stg_transaksi": 40.0,
        "marts.transaksi_bku": 100.0,
        "marts.fct_realisasi": 45.0,
        "marts.dim_skpd": 100.0,
    }
    assert portfolio_score(results, contract.warn_credit) == 65.0


# ---------------------------------------------------------------- gateway


def test_gateway_row_filter_and_masking(fresh_seed):
    import psycopg

    policies = load_policies(RBAC)
    audit = ROOT / "var" / "test_audit.jsonl"
    if audit.exists():
        audit.unlink()
    gw = Gateway(policies, audit)
    with psycopg.connect(DSN) as conn:
        # kepala sees all 5 SKPDs, unmasked.
        rows = gw.query(conn, "kepala_bpkd", "marts.fct_realisasi",
                        ["kode_skpd", "pagu", "realisasi", "persen"])
        assert len(rows) == 5
        # bidang_kesehatan sees exactly its own SKPD.
        rows = gw.query(conn, "bidang_kesehatan", "marts.fct_realisasi",
                        ["kode_skpd", "pagu", "realisasi", "persen"])
        assert {r[0] for r in rows} == {"1.05"}
        # auditor's NIP arrives masked, IT admin's does not.
        aud = gw.query(conn, "auditor", "marts.transaksi_bku",
                       ["id_bku", "nik_pelapor"], limit=3)
        assert all(str(r[1]).endswith("***") for r in aud)
        admin = gw.query(conn, "it_admin", "marts.transaksi_bku",
                         ["id_bku", "nik_pelapor"], limit=3)
        assert all(str(r[1]).startswith("3273") and "***" not in str(r[1])
                   for r in admin)
        # Fail closed.
        with pytest.raises(AccessDenied):
            gw.query(conn, "hacker", "marts.transaksi_bku", ["id_bku"])
        with pytest.raises(AccessDenied):
            gw.query(conn, "auditor", "raw.transaksi", ["id_bku"])

    decisions = [yaml.safe_load(line)["decision"]
                 for line in audit.read_text().splitlines()]
    assert "allowed" in decisions and "denied" in decisions


# ---------------------------------------------------------------- metrics


def test_metrics_endpoint_serves_prometheus_text(fresh_seed):
    import subprocess
    import time

    proc = subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "python"), "-m", "apbd_obs", "serve",
         "--port", "8199"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        text = ""
        for _ in range(20):
            time.sleep(0.5)
            try:
                text = requests.get("http://localhost:8199/metrics", timeout=2).text
                break
            except requests.ConnectionError:
                continue
        assert "apbd_quality_score_portfolio" in text
        assert "# TYPE apbd_quality_score gauge" in text
        assert requests.get("http://localhost:8199/healthz", timeout=2).text == "ok\n"
    finally:
        proc.terminate()


def test_prometheus_scrapes_the_service():
    targets = requests.get(f"{PROM}/api/v1/targets", timeout=5).json()
    health = {t["labels"].get("job"): t["health"]
              for t in targets["data"]["activeTargets"]}
    assert health.get("apbd-observability") in {"up", "unknown"}


def test_grafana_dashboard_provisioned():
    auth = ("admin", "obs-2026")
    dash = requests.get(f"{GRAFANA}/api/dashboards/uid/apbd-observability",
                        auth=auth, timeout=5)
    assert dash.status_code == 200
    assert dash.json()["dashboard"]["title"] == "APBD Data Quality & Governance"
    ds = requests.get(f"{GRAFANA}/api/datasources", auth=auth, timeout=5).json()
    assert any(d["type"] == "prometheus" for d in ds)


# ---------------------------------------------------------------- catalog


def test_catalog_has_all_contract_tables(contract):
    report = catalog.verify_catalog(contract)
    tables = {t["fqn"]: t for t in report["tables"]}
    assert all(t.get("present") for t in tables.values())
    for table in contract.tables:
        row = tables[f"bpkd_postgres.bpkd.{table.name}"]
        assert row["description"], f"{table.name} has no description"
        assert table.owner in row["owners"]
        expected_pii = len(catalog.PII_COLUMNS.get(table.name, []))
        assert len(row["pii_tags"]) == expected_pii
        assert all(tag == "PII.Sensitive" for tag in row["pii_tags"])


def test_catalog_lineage_matches_declaration(contract):
    report = catalog.verify_catalog(contract)
    assert report["lineage_drift"] == {
        "missing_from_catalog": [],
        "stale_in_contract": [],
    }


def test_catalog_search_finds_tables_and_dashboard():
    client = catalog.OmClient()
    hits = catalog.search_catalog(client, "realisasi")
    fqns = {h["fqn"] for h in hits}
    assert any("fct_realisasi" in f for f in fqns)
    assert any("Rekap Realisasi APBD" in f for f in fqns)


def test_dashboard_entity_registered(contract):
    client = catalog.OmClient()
    dash_fqn = f"{contract.dashboard.service}.{contract.dashboard.name}"
    resp = client.get(f"/v1/dashboards/name/{catalog.quote(dash_fqn, safe='')}")
    assert resp is not None
    assert resp.get("description")
