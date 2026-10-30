# PRD mapping — Project 3 (P3) · honest status per element

How this repository maps onto Project 3 ("Data Governance & Observability Platform") of
`Prd-Architecture-Proyek-Portfolio.html`. Same rule as every project in the portfolio: **a cell
says "built" only when the code that builds it is in this repo and its evidence is checked in.**
Local equivalents are named as equivalents, never passed off as the production service.

## Governance objectives (the PRD box)

| PRD element | PRD target | Status | Evidence in this repo |
| --- | --- | --- | --- |
| Single Data Catalog | semua table searchable dengan owner & description | **built** | real OpenMetadata 2.0.7 (8-container compose); 5 tables + 1 dashboard, all with descriptions + owners (`docs/generated/catalog_verify.json`); `catalog-search realisasi` returns table, column, and dashboard via ES (`docs/generated/catalog_search.json`) |
| Data Quality Score | not_null, unique, freshness, volume anomaly → score 0-100 | **built** | 20 checks in `config/contract.yaml` → `apbd_obs/quality.py` + `scoring.py`; per-table 0-100, portfolio mean; committed run: **65.0/100 WARN**, per-table 40/40/100/45/100 (`docs/generated/quality_report.md`) |
| Lineage | Metabase dashboard → fact → staging → SIMDA MySQL | **built** | 5 edges in OpenMetadata (`raw → staging → {transaksi_bku, fct_realisasi}`, `dim_skpd → fct_realisasi`, `fct_realisasi → Rekap Realisasi APBD` dashboard); read back and diffed both directions by `catalog-verify` — zero drift; export to Mermaid + JSON (`docs/generated/lineage.mmd`, `lineage.json`) |
| RBAC | Bidang hanya lihat SKPD mereka, Kepala lihat semua, IT admin full | **built** | `config/rbac.yaml` (5 roles) enforced by `apbd_obs/gateway.py` — fail-closed, row filters, PII masking; proven from returned rows by the integration suite; every decision in `docs/generated/rbac_audit.jsonl` |

## Flowchart — governance pipeline

| PRD step | PRD description | Status | Evidence in this repo |
| --- | --- | --- | --- |
| Sources (Redshift/BQ + S3 + Airflow logs) | metadata ingestion untuk semua layer | **built (local analogue)** | PostgreSQL 16 data plane (5433) with `raw → staging → marts`, seeded by `apbd_obs/seed.py`; the lakehouse layers themselves live in P1 |
| OpenMetadata ingestion (cron Airflow) | DAG daily 03:00: metadata, lineage, profiler | **built (on-demand)** | `catalog-sync` over the OM 2.0 REST API — service, schemas, tables, owners, PII tags, dashboard, lineage edges; idempotent (second sync creates nothing). Scheduled/airflow-triggered profiling: designed-only |
| Data Quality Tests | not_null(kode_skpd), unique(id_bku), freshness {2h/4h}, row_count {9000..11000} | **built** | exactly these checks (plus the `realisasi <= pagu` invariant) in `config/contract.yaml`, all four check types in `apbd_obs/quality.py`; the committed run shows every one firing or passing as designed |
| Lineage Graph | column-level: dashboard.persen → fact.sisa → staging.realisasi | **built (table-level)** | table-level lineage with a dashboard terminus in OM; the Mermaid export is the graph deliverable. Column-level lineage: designed-only (needs profiler-level ingestion) |
| Access Control RBAC | finance_viewer per SKPD, auditor read-only, admin full, masking PII | **built** | 5 roles (`kepala_bpkd`, `bidang_kesehatan`, `bidang_pendidikan`, `auditor`, `it_admin`) with row filters + `left(nik,2)\|\|'***'` masking; audit trail records `masked_columns` per query |
| Grafana Prometheus Metrics | quality score, ingestion failures, freshness SLO breach | **built** | `python -m apbd_obs serve` exports `apbd_quality_score`, `apbd_quality_score_portfolio`, `apbd_check_status`, `apbd_freshness_hours`, `apbd_audit_events_total`; Prometheus scrape target `up`; Grafana dashboard provisioned from code |
| Metabase governance dashboard + Slack | quality score per table, open issues, Slack on fail | **built (dry-run)** | Grafana dashboard is the quality-score view (provisioned); Slack: payload written to `docs/generated/quality_alert_payload.json` (3 ERROR payloads), POSTs only with `SLACK_WEBHOOK_URL` set |

## Deliverables

| PRD deliverable | Status | Where |
| --- | --- | --- |
| Data Catalog searchable (OpenMetadata UI) | **built** | `docker compose up -d` → http://localhost:8585, search "realisasi" |
| Quality Score Dashboard (0-100 per table) | **built** | Grafana http://localhost:3301 — gauge + bar gauge + timeseries, provisioned from `grafana/dashboards/quality.json` |
| Lineage Graph export | **built** | `python -m apbd_obs lineage-export` → `docs/generated/lineage.mmd` (Mermaid) + `lineage.json` |
| RBAC matrix & audit logs | **built** | `config/rbac.yaml` (the matrix) + `docs/generated/rbac_audit.jsonl` (allowed & denied) |
| Slack alerts integration | **built (dry-run)** | edge-triggered payloads in `docs/generated/quality_alert_payload.json`; real POST only with webhook configured |

## Designed, not built (kept visible on purpose)

| PRD element | Why it is not built here | What stands in for it |
| --- | --- | --- |
| GKE / managed OpenMetadata | portfolio rule is no cloud accounts; a managed platform can't be verified from a laptop | official OM 2.0.7 quickstart compose, adapted — the real platform, locally |
| CloudWatch | managed observability service | Prometheus + Grafana, both open-source and locally verifiable |
| Airflow DAG scheduling (daily 03:00 cron, profiler/usage ingestion) | an orchestrator is P1's story; adding one here would duplicate it | `catalog-sync` / `check` invoked on demand (or by any scheduler — they're plain CLI commands with exit codes) |
| OpenMetadata's own metrics into Prometheus | OM 2.0 exposes Dropwizard as JSON on admin port 8586, not Prometheus text | documented in `prometheus/prometheus.yml`; the PRD's quality/freshness/audit metrics are exported by `apbd_obs serve` |
| Column-level lineage | needs profiler-level ingestion at column granularity | table-level lineage with the dashboard terminus, verified for drift |
| Production IAM/SSO, real user provisioning | out of scope for a local demo | OM's default local auth; gateway roles map to the PRD's job titles |
| Metabase app rendering the governance dashboard | Metabase is P1's dashboard layer; here the dashboard is a *catalog citizen* | registered as an OM dashboard entity at the lineage terminus; the quality-score dashboard is Grafana's |

## Test split

- **Unit (50)** — contract parsing, check execution against fixtures, scoring/gates,
  alert edge-triggering, RBAC policy + masking + audit, lineage diff + Mermaid export,
  catalog payload construction. Runs in CI on every push.
- **Integration (12)** — against the live stack: re-seed then exact defect counts (12 NULL,
  5 duplicates, 1 invariant breach), per-table + portfolio scores, the gateway matrix from
  returned rows (masking asserted in data), the Prometheus text endpoint, the Prometheus
  scrape target, the Grafana dashboard + datasource, and the full OpenMetadata read-back
  (tables, owners, PII tags, lineage drift, search, dashboard entity). Local only:
  `python -m pytest -m integration`.
