# APBD Observability & Governance (P3)
[![CI](https://github.com/faishalnuruz/apbd-observability/actions/workflows/ci.yml/badge.svg)](https://github.com/faishalnuruz/apbd-observability/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Project 3 of the BPKD portfolio PRD, built for real: a **real OpenMetadata 2.0 platform**
(catalogued, owned, PII-tagged, lineage-graphed, full-text searchable), a **data-contract
quality gate** that scores every table 0–100, a **fail-closed RBAC gateway** with row filters,
column masking and a full audit trail, and **Prometheus + Grafana** scraping the whole thing —
all over a PostgreSQL data plane seeded with *deliberate, documented defects* so the gates have
something real to catch.

Where `sikt-mini-lakehouse` (P1) answers *"is the data trustworthy?"* and
`apbd-realtime-alerts` (P2) answers *"how fast does the system notice?"*, this repo answers
*"who is accountable for it, and what does the platform prove?"* — and it proves it against a
catalog that actually runs, not a diagram of one.

The measured verdict on the seeded state: **portfolio score 65.0/100 (WARN)**, with every
planted defect caught and attributed to its owner in OpenMetadata
(`docs/generated/quality_report.md`, `docs/generated/catalog_verify.json`).

## What actually runs

| Layer | Component | Real mechanism |
| --- | --- | --- |
| Platform | **OpenMetadata 2.0.7** (8 containers: MySQL metadata store, Elasticsearch, server, migrate, ingestion, plus ours) | official quickstart compose, adapted: `om-*` naming, ES heap 512 MB |
| Data plane | PostgreSQL 16 (`raw` → `staging` → `marts`) | seeded deterministic defects — 12 NULL `kode_skpd`, 5 duplicate `id_bku`, a 4.5-hour-stale staging load, one `realisasi > pagu` invariant breach |
| Contract | 20 executable checks in `config/contract.yaml` | `not_null`, `unique`, `row_count`, `freshness` (PRD's 2 h warn / 4 h error), `invariant` (`realisasi <= pagu`) — PASS = full weight, WARN = half, FAIL = 0 |
| Scoring | per-table 0–100, portfolio = mean | gates at 60 (ERROR) and 85 (WARNING), edge-triggered alerts |
| Access | RBAC gateway in `apbd_obs.gateway` | 5 roles, row filters, `left(nik,2)||'***'` masking, **fail closed**, every decision (allowed *and* denied) written to an append-only JSONL audit trail |
| Catalog sync | `apbd_obs.catalog` — pure REST against OM 2.0 | database service, schemas, tables with descriptions + owners + `PII.Sensitive` tags, Metabase-style dashboard entity, 5 lineage edges (URL-based `PUT /v1/lineage/...`, idempotent — a second sync creates nothing) |
| Lineage | contract declaration ↔ catalog read-back | `catalog-verify` diffs both directions; today: **zero drift** |
| Metrics | `python -m apbd_obs serve` | Prometheus text: `apbd_quality_score{table=…}`, `apbd_quality_score_portfolio`, `apbd_check_status`, `apbd_freshness_hours`, `apbd_audit_events_total{decision=…}`, `apbd_catalog_tables` |
| Dashboards | Grafana 11 (provisioned as code) | gauge (portfolio score), bar gauge (per table), freshness timeseries with the PRD's 2 h/4 h thresholds, check-status timeseries, failing-checks table |

Ports are disjoint from every other portfolio stack: OM MySQL 3308, ES 9200, server 8585/8586,
ingestion 8081, data plane **5433**, Prometheus 9090, Grafana 3301, metrics 8100.

## Quickstart

```bash
docker compose up -d                     # OpenMetadata + PostgreSQL + Prometheus + Grafana (~2 min)
python -m venv .venv && .venv/bin/pip install -e '.[dev]'

.venv/bin/python -m apbd_obs seed         # deterministic data plane + defects
.venv/bin/python -m apbd_obs check        # run the contract, score every table
.venv/bin/python -m apbd_obs gateway demo # 7 scenarios: row filters, masking, denials
.venv/bin/python -m apbd_obs catalog-sync # register service/tables/dashboard/lineage in OM
.venv/bin/python -m apbd_obs catalog-verify   # read it all back; fails on drift
.venv/bin/python -m apbd_obs serve        # :8100/metrics — Prometheus scrapes it
```

Open http://localhost:8585 (OpenMetadata, `admin@open-metadata.org` / `admin`), search
"realisasi" and watch the table, its column, and the dashboard surface with owners and
descriptions; http://localhost:3301 (Grafana, `admin` / `obs-2026`) for the dashboard.

## The seeded defects — and what caught them

The data plane is *honestly bad on purpose* (`apbd_obs.seed` documents every row it corrupts),
so the report is a demonstration of detection, not a vanity metric:

| table | score | planted defect | caught by |
| --- | --- | --- | --- |
| `raw.transaksi` | 40.0 | 12 NULL `kode_skpd`, 5 duplicate `id_bku` | `not_null`, `unique` — **FAIL** |
| `staging.stg_transaksi` | 40.0 | 4.5-hour-stale load, volume 8845 (< 9000) | `freshness` (PRD > 4 h), `row_count` — **FAIL** |
| `marts.transaksi_bku` | 100.0 | *(healthy control)* | all PASS |
| `marts.fct_realisasi` | 45.0 | 1 row `realisasi > pagu`; 2.6-hour-stale | `invariant`, `freshness` — **FAIL + WARN** |
| `marts.dim_skpd` | 100.0 | *(healthy control)* | all PASS |

**Portfolio: 65.0/100 (WARN)** — between the ERROR gate (60) and WARNING gate (85). The two
healthy tables prove the scorer is not failing everything; the alert payloads
(`docs/generated/quality_alert_payload.json`) are edge-triggered — three ERROR payloads fired
once for the three degraded tables, and re-running the check while they stay broken does not
re-spam.

## The catalog is the platform, not a mock

`catalog-sync` talks to a live OpenMetadata 2.0 server over REST and creates *governed* assets,
not bare tables:

- **Owners**: `bidang_data` owns `raw`/`staging`; `bidang_perencanaan` owns the marts — as real
  OM users, attached via JSON Patch (OM 2.0 silently drops `owners` at create time; this and
  every other 2.0 API quirk is handled in `catalog.py`).
- **PII**: `nik_pelapor` carries the `PII.Sensitive` classification tag on all three tables that
  have the column.
- **Lineage**: 5 edges — `raw → staging → {transaksi_bku, fct_realisasi}`, `dim_skpd →
  fct_realisasi`, and `fct_realisasi → Rekap Realisasi APBD` (the dashboard terminus,
  registered as a Metabase-style dashboard entity).
- **Search**: `catalog-search realisasi` returns the table, its column, *and* the dashboard,
  with descriptions, via OM's Elasticsearch index (`docs/generated/catalog_search.json`).
- **Verify, don't trust**: `catalog-verify` re-reads every table (owners, tags, descriptions)
  and the full lineage graph back out of OpenMetadata and diffs it against
  `config/contract.yaml`. Both drift directions fail the command; today: **no drift**
  (`docs/generated/catalog_verify.json`).

```bash
.venv/bin/python -m apbd_obs catalog-search realisasi
#   [tableColumn] …marts.fct_realisasi.realisasi
#   [table]       …marts.fct_realisasi      "Realisasi vs pagu per SKPD — fact …"
#   [dashboard]   metabase_bpkd.Rekap Realisasi APBD  "titik hilir seluruh lineage"
```

## RBAC that fails closed, and proves it

Every query goes through `apbd_obs.gateway`; unknown role or table is denied, and the audit
trail records both outcomes. From the committed run (`docs/generated/rbac_audit.jsonl`):

```
allowed  kepala_bpkd      fct_realisasi  5 baris        (all SKPDs, unmasked)
allowed  bidang_kesehatan fct_realisasi  1 baris        (row filter: kode_skpd = 1.05)
allowed  auditor          transaksi_bku  5 baris        masked: ["nik_pelapor"] → "32***"
allowed  it_admin         raw.transaksi  5 baris        (only role reaching raw; full NIK)
denied   bidang_kesehatan raw.transaksi  "tidak punya akses"
denied   hacker           transaksi_bku  "role tidak dikenal"
```

The integration suite asserts the mask from the *returned rows* (`32***` for the auditor, full
`3273…` for IT admin), so masking is verified where it matters — at the data, not just in the
audit line.

## Honest limits

| PRD element | Status |
| --- | --- |
| OpenMetadata data catalog | **built & running** — real 2.0.7 platform, 5 tables + dashboard + 5 lineage edges, synced and verified idempotently |
| Data contract quality gates | built, running — 20 checks, per-table + portfolio scoring, PRD's 2 h/4 h freshness thresholds |
| RBAC + masking + audit | built, tested — fail-closed, JSONL audit trail committed as evidence |
| Prometheus + Grafana | built — provisioned dashboard + datasource; the metrics target is `up` in Prometheus |
| Slack alerts | dry-run: payload written (`docs/generated/quality_alert_payload.json`); the POST happens only when `SLACK_WEBHOOK_URL` is set |
| Metabase dashboard | registered as an OM dashboard **entity** (lineage terminus); the actual Metabase app is P1's — here the dashboard is a catalog citizen, not a rendered page |
| OM's own internal metrics | designed only — OM 2.0 exposes Dropwizard JSON on admin port 8586, not Prometheus text; bridging it is future work (noted in `prometheus/prometheus.yml`) |
| Production IAM/SSO, real user provisioning | designed only — OM runs on its default local auth; gateway roles map to the PRD's job titles |

Full per-requirement mapping: [`docs/prd-mapping.md`](docs/prd-mapping.md). Architecture detail,
including the OM 2.0 API quirks discovered the hard way:
[`docs/architecture.md`](docs/architecture.md).

## Tests

```bash
python -m pytest -m "not integration"   # 50 unit tests — contract parsing, scoring, masking, lineage diff; what CI runs
python -m pytest -m integration          # 12 integration tests — against the live stack (~15 s)
```

The integration suite owns its setup: it re-seeds the data plane so freshness offsets are exact,
then checks row counts, defect counts, per-table scores, the gateway matrix (from returned
rows), the metrics endpoint, the Prometheus target, the Grafana dashboard, and the full
OpenMetadata catalog + lineage read-back. CI runs the unit layer because the compose stack is a
local resource; the split is stated in the workflow itself.

## Related

- [`sikt-mini-lakehouse`](https://github.com/faishalnuruz/sikt-mini-lakehouse) — the P1
  flagship: same SIMDA-shaped source, lakehouse + dbt + the Metabase dashboard this repo
  catalogues.
- [`apbd-realtime-alerts`](https://github.com/faishalnuruz/apbd-realtime-alerts) — the P2
  sibling: the same defects caught *in motion*, with measured p95 2.483 ms latency.
- [`lakehouse-governance`](https://github.com/faishalnuruz/lakehouse-governance) — the
  governance-focused repo: Unity Catalog-flavoured policies, this repo's OpenMetadata counterpart.

MIT © Faishal Nuruz Zuhri
