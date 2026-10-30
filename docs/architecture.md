# Architecture — APBD Observability & Governance (P3)

Eight containers plus one Python service, all local, no cloud account. This document explains
what talks to what, and the OpenMetadata 2.0 API behaviour that `catalog.py` had to learn the
hard way.

```
┌──────────────────────────── docker compose (obs-*) ────────────────────────────┐
│                                                                                │
│  om-mysql (3308) ◄── om-server (8585/8586) ◄── om-elasticsearch (9200)         │
│        ▲                    ▲                        ▲                        │
│        │              om-migrate (one-shot)           │                        │
│        │              om-ingestion (8081)             │                        │
│                                                                                │
│  postgres (5433): raw.transaksi → staging.stg_transaksi → marts.*             │
│                                                                                │
│  prometheus (9090) ──scrapes──► host.docker.internal:8100 (apbd_obs serve)     │
│  grafana (3301) ──queries──► prometheus                                         │
└────────────────────────────────────────────────────────────────────────────────┘

host: python -m apbd_obs {seed, check, gateway, catalog-sync, catalog-verify,
                          catalog-search, lineage-export, serve, report}
      │                                        │
      ├── psycopg ──► postgres (5433)          └── requests/REST ──► om-server (8585)
```

## Why the data plane looks like this

`raw.transaksi` (9 417 rows) → `staging.stg_transaksi` (8 845) → `marts.transaksi_bku` +
`marts.fct_realisasi`, with `marts.dim_skpd` as the dimension. The seeder (`apbd_obs/seed.py`)
is deterministic: same rows, same defects, every run. The defects are *documented production
failure modes*, not random corruption:

| defect | table | failure mode it represents |
| --- | --- | --- |
| 12 NULL `kode_skpd` | raw | feeder dropped rows with a missing SKPD code |
| 5 duplicate `id_bku` | raw | double-load from the SIMDA export |
| 4.5-hour-stale `loaded_at` | staging | the nightly job silently stopped |
| 8845 rows (min 9000) | staging | volume drop from the same stall |
| 1 row `realisasi > pagu` | fct | the invariant breach the PRD names explicitly |

Two marts are left healthy on purpose: a scorer that fails everything teaches nothing. The
score report must show contrast — 100 and 40 in the same run.

## Contract → score → gate

`config/contract.yaml` is the executable data contract. Each check has a weight; PASS earns
full weight, WARN half, FAIL zero (`apbd_obs/scoring.py`). Per-table score = earned/total ×
100; portfolio = mean over tables. Gates at 60 (ERROR) and 85 (WARNING) drive the alert
payloads — edge-triggered against `var/alert_state.json`, so a persistently broken table alerts
once, not every run.

Freshness thresholds come straight from the PRD: warn at 2 h, error at 4 h. Because freshness is
time-relative, the integration suite re-seeds right before checking — otherwise yesterday's seed
would score differently than today's.

## RBAC gateway

`apbd_obs/gateway.py` is the only path from a role to rows. `config/rbac.yaml` declares 5 roles:
which tables each may touch, an optional row filter (`kode_skpd: ["1.05"]`), and which columns
to mask. The rules:

- **fail closed** — unknown role, unknown table, or a request for an undeclared column is
  denied; there is no default-allow.
- **masking at the row** — `left(nik_pelapor, 2) || '***'`, applied to the returned rows, so
  the integration suite can assert `32***` in the data itself.
- **audit everything** — every decision, allowed or denied, appends a JSONL line (role, table,
  decision, reason, masked columns, row count, duration) to `docs/generated/rbac_audit.jsonl`.

## OpenMetadata 2.0: what the sync had to learn

`catalog.py` syncs the contract into a live OM 2.0.7 server over plain REST (`requests`, no SDK
dependency). The 2.0 API differs from the 1.x docs in ways that fail *silently* if you get them
wrong — each of these cost a debugging cycle:

1. **Login** is `POST /v1/users/login` with `{"email": "admin@open-metadata.org",
   "password": "<base64 of admin>"}` — password is base64-encoded, not plain.
2. **Connection configs are typed enums**: `"type": "Postgres"` on the connection object, not
   the 1.x `PostgresConnection` discriminator, and `"type": "Metabase"` for the dashboard
   service.
3. **Create requests take FQN strings**, not `{id, type}` references, for parent links
   (`databaseSchema: "bpkd_postgres.bpkd.raw"`).
4. **`owners` and column `tags` are dropped at create time.** The create returns 201 and the
   fields simply aren't there. They must be set with `PATCH` and
   `Content-Type: application/json-patch+json` (RFC 6902) — plain `application/json` on a PATCH
   is accepted and ignored.
5. **Relationship fields are stripped from GET responses unless requested**:
   `?fields=owners,columns,tags`. This one is doubly nasty — a sync that reads state without
   `fields=` will see empty owners/tags and re-patch them on every run (duplicating owners at
   `/owners/0`). `catalog.py` fetches with all three fields precisely to stay idempotent.
6. **Lineage moved** to `PUT /v1/lineage/{fromType}/name/{fromFQN}/{toType}/name/{toFQN}`
   (upstream → downstream; delete by same shape), replacing the 1.x `/v1/tables/lineage`
   entityLink payload. Edge objects in the read-back are **id-based**; the `nodes` array is the
   id → entity map.
7. **The list endpoint** (`GET /v1/tables?...`) hides column tags that the single-entity GET
   with `fields=tags` returns — verification reads each table individually for this reason.

`catalog-verify` closes the loop: it reads every table (owners, tags, descriptions) and the
whole lineage graph back out of OM, maps it to contract-local names
(`_node_name`: `schema.table`, `dashboard::name`), and diffs both directions against
`lineage.expected_edges()`. Any drift — a table deleted in the UI, an owner changed, an edge
added by hand — fails the command with the exact edge pair.

## Metrics path

`python -m apbd_obs serve` runs the contract on startup and serves Prometheus text on
`/metrics` (plus `/healthz`). Prometheus scrapes it via `host.docker.internal:8100`; Grafana
provisions its datasource and dashboard from `grafana/provisioning/` and
`grafana/dashboards/quality.json` (gauge, bar gauge, freshness timeseries with the 2 h/4 h
thresholds, check-status timeseries, failing-checks table).

OpenMetadata's own metrics are *not* scraped: OM 2.0 exposes Dropwizard as JSON on the admin
port (8586/metrics), not Prometheus text format. Bridging that (a dropwizard exporter or a small
converter in the serve loop) is documented as designed-only in `prometheus/prometheus.yml`
rather than shipping a permanently-down scrape target.

## Evidence discipline

`docs/generated/` is committed and regenerated in a fixed order: seed → check (which writes
`quality_report.{md,json}`, `metrics.txt`, `quality_alert_payload.json`) → gateway demo
(`rbac_audit.jsonl`) → lineage export → catalog sync → catalog search → catalog verify. Every
number quoted in the README comes from those files; nothing in the README is hand-typed from
memory.
