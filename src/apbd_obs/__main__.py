"""Command-line entry point: `python -m apbd_obs <command>`.

Commands
--------
seed                      (Re)create + load the data plane with planted defects
check                     Run the contract checks; print the score table
serve                     Serve /metrics for Prometheus (blocks)
catalog sync              Register service/tables/dashboard/lineage in OpenMetadata
catalog search QUERY      Full-text search over the real catalog
gateway query ROLE TABLE  Run one RBAC'd query (audited, masked)
gateway demo              The 4-role demo that produces the audit evidence
lineage export            Mermaid + JSON of the declared lineage
report                    One-shot: seed, check, alerts, evidence files
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg

from . import alerting, catalog, lineage, metrics, quality
from . import seed as seedmod
from .contract import load_contract
from .gateway import AccessDenied, Gateway, load_policies
from .scoring import level, portfolio_score, score_table

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "config" / "contract.yaml"
RBAC = ROOT / "config" / "rbac.yaml"
GENERATED = ROOT / "docs" / "generated"
VAR = ROOT / "var"


def _dsn() -> str:
    import os

    return os.environ.get(
        "OBS_PG_DSN",
        "host=localhost port=5433 dbname=bpkd user=bpkd password=bpkd-2026",
    )


def _connect() -> psycopg.Connection:
    return psycopg.connect(_dsn())


def cmd_seed(_args: argparse.Namespace) -> int:
    with _connect() as conn:
        counts = seedmod.seed(conn)
    print("data plane seeded:")
    for table, n in counts.items():
        print(f"  {table:26s} {n:>6} baris")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    contract = load_contract(CONTRACT)
    with _connect() as conn:
        results = quality.run_checks(conn, contract)
        freshness = {
            t.name: h
            for t in contract.tables
            if (h := quality.freshness_hours(conn, t)) is not None
        }

    print(f"{'tabel':26s} {'skor':>6s}  check gagal")
    for t in contract.tables:
        score = score_table(results, t.name, contract.warn_credit)
        failing = [
            r for r in results if r.table == t.name and r.status != "PASS"
        ]
        marks = ", ".join(f"{r.check_id}={r.status}" for r in failing) or "-"
        print(f"{t.name:26s} {score:6.1f}  {marks}")

    overall = portfolio_score(results, contract.warn_credit)
    lvl = level(overall, contract.error_level, contract.warn_level)
    print(f"\nportfolio: {overall}/100 ({lvl.upper()})")

    if args.evidence:
        _write_evidence(results, freshness, contract, overall)

    payloads = alerting.evaluate_alerts(
        results,
        contract.warn_credit,
        contract.error_level,
        contract.warn_level,
        state_path=VAR / "alert_state.json",
        out_dir=GENERATED,
    )
    if payloads:
        print(f"alert: {len(payloads)} payload ditulis "
              f"(kirim asli set SLACK_WEBHOOK_URL)")
    return 0


def _write_evidence(results, freshness, contract, overall) -> None:
    GENERATED.mkdir(parents=True, exist_ok=True)
    report = {
        "portfolio_score": overall,
        "level": level(overall, contract.error_level, contract.warn_level),
        "tables": {
            t.name: {
                "score": score_table(results, t.name, contract.warn_credit),
                "owner": t.owner,
                "checks": [
                    {
                        "check": r.check_id,
                        "status": r.status,
                        "observed": r.observed,
                        "message": r.message,
                    }
                    for r in results
                    if r.table == t.name
                ],
            }
            for t in contract.tables
        },
        "freshness_hours": freshness,
    }
    (GENERATED / "quality_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    )
    lines = [
        "# Laporan kualitas data (kontrak: config/contract.yaml)",
        "",
        f"Portfolio score: **{overall}/100** ({report['level'].upper()})",
        "",
        "| tabel | skor | check gagal |",
        "|---|---|---|",
    ]
    for t in contract.tables:
        failing = [
            r for r in results if r.table == t.name and r.status != "PASS"
        ]
        marks = "<br>".join(
            f"`{r.check_id}` **{r.status}** — {r.observed}" for r in failing
        ) or "semua PASS"
        score = score_table(results, t.name, contract.warn_credit)
        lines.append(f"| {t.name} | {score} | {marks} |")
    (GENERATED / "quality_report.md").write_text("\n".join(lines) + "\n")

    # Prometheus snapshot as committed evidence.
    state = metrics.MetricsState()
    state.results = results
    state.warn_credit = contract.warn_credit
    state.error_level = contract.error_level
    state.warn_level = contract.warn_level
    state.freshness = freshness
    state.catalog_tables = len(contract.tables)
    (GENERATED / "metrics.txt").write_text(
        metrics.render_metrics(
            results, contract.warn_credit, contract.error_level,
            contract.warn_level, freshness, len(contract.tables), {},
        )
    )


def cmd_serve(args: argparse.Namespace) -> int:
    state = metrics.MetricsState()
    contract = load_contract(CONTRACT)
    state.warn_credit = contract.warn_credit
    state.error_level = contract.error_level
    state.warn_level = contract.warn_level

    # One synchronous run at startup so /metrics is never empty, then let
    # Prometheus poll the snapshot (a re-run of `check` refreshes it).
    try:
        with _connect() as conn:
            state.results = quality.run_checks(conn, contract)
            state.freshness = {
                t.name: h
                for t in contract.tables
                if (h := quality.freshness_hours(conn, t)) is not None
            }
        state.last_run = quality.last_check_time()
    except psycopg.Error as exc:
        print(f"warn: cek awal gagal ({exc}); /metrics tetap naik", file=sys.stderr)

    print(f"metrics di http://0.0.0.0:{args.port}/metrics (Ctrl+C untuk berhenti)")
    metrics.serve(state, port=args.port)
    return 0


def cmd_catalog_sync(_args: argparse.Namespace) -> int:
    contract = load_contract(CONTRACT)
    summary = catalog.sync_catalog(contract)
    print(f"service: {summary.service}")
    print(f"tables : {len(summary.tables)}")
    print(f"dashboard: {summary.dashboard}")
    print(f"lineage edges: {summary.lineage_edges}")
    if summary.created:
        print("dibuat baru:")
        for c in summary.created:
            print(f"  + {c}")
    else:
        print("sudah sinkron (idempotent, tidak ada yang dibuat)")
    GENERATED.mkdir(parents=True, exist_ok=True)
    (GENERATED / "catalog_sync.json").write_text(
        json.dumps(
            {
                "service": summary.service,
                "tables": summary.tables,
                "dashboard": summary.dashboard,
                "lineage_edges": summary.lineage_edges,
                "created": summary.created,
            },
            indent=2,
        )
        + "\n"
    )
    return 0


def cmd_catalog_search(args: argparse.Namespace) -> int:
    client = catalog.OmClient()
    hits = catalog.search_catalog(client, args.query)
    print(f"{len(hits)} hasil untuk {args.query!r}:")
    for h in hits:
        print(f"  [{h['type']}] {h['fqn']}")
        if h["description"]:
            print(f"      {h['description']}")
    GENERATED.mkdir(parents=True, exist_ok=True)
    (GENERATED / "catalog_search.json").write_text(
        json.dumps({"query": args.query, "hits": hits}, indent=2, ensure_ascii=False)
        + "\n"
    )
    return 0


def cmd_catalog_verify(_args: argparse.Namespace) -> int:
    contract = load_contract(CONTRACT)
    report = catalog.verify_catalog(contract)
    ok = True
    for t in report["tables"]:
        if not t.get("present"):
            print(f"HILANG  {t['fqn']}")
            ok = False
            continue
        owner_ok = t["expected_owner"] in t["owners"]
        pii_ok = t["pii_tags"] == t["expected_pii"]
        ok = ok and owner_ok and pii_ok and t["description"]
        print(
            f"{'OK ' if (owner_ok and pii_ok and t['description']) else '!! '}"
            f"{t['fqn']} owner={t['owners']} pii={t['pii_tags']}"
        )
    drift = report["lineage_drift"]
    if drift["missing_from_catalog"] or drift["stale_in_contract"]:
        ok = False
        print(f"lineage drift: {json.dumps(drift)}")
    else:
        print("lineage: deklarasi == katalog (tidak ada drift)")
    GENERATED.mkdir(parents=True, exist_ok=True)
    (GENERATED / "catalog_verify.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    )
    return 0 if ok else 1


def cmd_gateway_query(args: argparse.Namespace) -> int:
    policies = load_policies(RBAC)
    gw = Gateway(policies, VAR / "audit.jsonl")
    columns = args.columns.split(",") if args.columns else _default_columns(args.table)
    try:
        with _connect() as conn:
            rows = gw.query(conn, args.role, args.table, columns, args.limit)
    except AccessDenied as exc:
        print(f"DITOLAK: {exc}")
        return 1
    print(f"{len(rows)} baris untuk role={args.role} table={args.table}")
    print(" | ".join(columns))
    for row in rows[:10]:
        print(" | ".join(str(v) for v in row))
    if len(rows) > 10:
        print(f"... ({len(rows) - 10} baris lagi)")
    return 0


def _default_columns(table: str) -> list[str]:
    if table.endswith("fct_realisasi"):
        return ["kode_skpd", "pagu", "realisasi", "persen"]
    return ["id_bku", "kode_skpd", "nik_pelapor", "nilai"]


def cmd_gateway_demo(_args: argparse.Namespace) -> int:
    """The evidence run: allowed and denied scenarios, all audited."""
    policies = load_policies(RBAC)
    audit_path = GENERATED / "rbac_audit.jsonl"
    if audit_path.exists():
        audit_path.unlink()
    gw = Gateway(policies, audit_path)

    mart_cols = ["kode_skpd", "pagu", "realisasi", "persen"]
    bku_cols = ["id_bku", "kode_skpd", "nik_pelapor", "nilai"]
    scenarios = [
        # (role, table, columns, expected)
        ("kepala_bpkd", "marts.fct_realisasi", mart_cols, "allowed"),
        ("bidang_kesehatan", "marts.fct_realisasi", mart_cols, "allowed"),
        ("bidang_kesehatan", "marts.transaksi_bku", bku_cols, "allowed"),
        ("auditor", "marts.transaksi_bku", bku_cols, "allowed"),
        ("it_admin", "raw.transaksi", bku_cols, "allowed"),
        # Negative cases: must be denied (fail closed).
        ("bidang_kesehatan", "raw.transaksi", bku_cols, "denied"),
        ("hacker", "marts.transaksi_bku", bku_cols, "denied"),
    ]

    failures = 0
    with _connect() as conn:
        for role, table, cols, expected in scenarios:
            try:
                rows = gw.query(conn, role, table, cols, limit=5)
                got, detail = "allowed", f"{len(rows)} baris"
                # A role with a mask must really have it applied, and a role
                # without one must never get it — otherwise the audit lies.
                if "nik_pelapor" in cols:
                    audited = gw.last_entry
                    masked = "nik_pelapor" in (audited.masked_columns if audited else [])
                    should_mask = role in ("auditor", "bidang_kesehatan", "bidang_pendidikan")
                    if masked != should_mask:
                        print(
                            f"!! mask salah untuk {role}: masked={masked} "
                            f"seharusnya {should_mask}"
                        )
                        failures += 1
            except AccessDenied as exc:
                got, detail = "denied", str(exc)[:60]
            ok = "OK " if got == expected else "!! "
            if got != expected:
                failures += 1
            print(f"{ok}{got:8s} {role:20s} {table:22s} -> {detail}")

    print(f"\naudit trail: {audit_path.relative_to(ROOT)}")
    if failures:
        print(f"GAGAL: {failures} skenario tidak sesuai harapan", file=sys.stderr)
        return 1
    return 0


def cmd_lineage_export(_args: argparse.Namespace) -> int:
    contract = load_contract(CONTRACT)
    GENERATED.mkdir(parents=True, exist_ok=True)
    (GENERATED / "lineage.mmd").write_text(lineage.mermaid(contract))
    (GENERATED / "lineage.json").write_text(lineage.to_json(contract))
    print((GENERATED / "lineage.mmd").read_text())
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """seed -> check(+evidence) -> gateway demo -> lineage export."""
    print("== seed ==")
    cmd_seed(args)
    print("\n== check ==")
    args.evidence = True
    rc = cmd_check(args)
    print("\n== gateway demo ==")
    cmd_gateway_demo(args)
    print("\n== lineage export ==")
    cmd_lineage_export(args)
    if not args.skip_catalog:
        print("\n== catalog sync ==")
        try:
            cmd_catalog_sync(args)
        except catalog.CatalogError as exc:
            print(f"catalog sync dilewati (OM belum siap?): {exc}")
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apbd_obs", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="seed the data plane").set_defaults(fn=cmd_seed)

    p_check = sub.add_parser("check", help="run contract checks")
    p_check.add_argument("--evidence", action="store_true",
                         help="write docs/generated evidence files")
    p_check.set_defaults(fn=cmd_check)

    p_serve = sub.add_parser("serve", help="serve /metrics for Prometheus")
    p_serve.add_argument("--port", type=int, default=8100)
    p_serve.set_defaults(fn=cmd_serve)

    sub.add_parser("catalog-sync", help="sync contract into OpenMetadata").set_defaults(
        fn=cmd_catalog_sync
    )

    p_search = sub.add_parser("catalog-search", help="full-text catalog search")
    p_search.add_argument("query")
    p_search.set_defaults(fn=cmd_catalog_search)

    sub.add_parser("catalog-verify", help="verify catalog matches the contract").set_defaults(
        fn=cmd_catalog_verify
    )

    p_gw = sub.add_parser("gateway", help="RBAC gateway")
    gw_sub = p_gw.add_subparsers(dest="gw_command", required=True)
    p_q = gw_sub.add_parser("query")
    p_q.add_argument("role")
    p_q.add_argument("table")
    p_q.add_argument("--columns", default=None)
    p_q.add_argument("--limit", type=int, default=100)
    p_q.set_defaults(fn=cmd_gateway_query)
    gw_sub.add_parser("demo").set_defaults(fn=cmd_gateway_demo)

    sub.add_parser("lineage-export", help="mermaid + json lineage").set_defaults(
        fn=cmd_lineage_export
    )

    p_report = sub.add_parser("report", help="full evidence run")
    p_report.add_argument("--skip-catalog", action="store_true")
    p_report.set_defaults(fn=cmd_report)

    args = parser.parse_args(argv)
    if not hasattr(args, "evidence"):
        args.evidence = False
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
