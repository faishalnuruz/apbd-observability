"""Contract checks executed against the PostgreSQL data plane.

Each check is one safe, read-only SQL statement. Freshness compares the
newest ``loaded_at``/``updated_at`` value to now(); WARN between the PRD's
2h and 4h thresholds, FAIL beyond.
"""

from __future__ import annotations

from datetime import datetime

import psycopg

from .contract import CheckSpec, Contract, TableSpec
from .scoring import FAIL, PASS, WARN, CheckResult


def _scalar(conn: psycopg.Connection, sql: str, params: tuple = ()) -> float:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def _run_check(conn: psycopg.Connection, table: str, check: CheckSpec) -> CheckResult:
    schema, name = table.split(".", 1)
    qualified = f'"{schema}"."{name}"'

    if check.type == "not_null":
        n = _scalar(
            conn,
            f"SELECT COUNT(*) FROM {qualified} WHERE {check.column} IS NULL",
        )
        status = PASS if n == 0 else FAIL
        return CheckResult(
            table, check.id, check.type, status, f"{int(n)} NULL",
            check.weight, f"{check.column} wajib terisi",
        )

    if check.type == "unique":
        n = _scalar(
            conn,
            f"SELECT COUNT(*) FROM (SELECT {check.column} FROM {qualified} "
            f"GROUP BY {check.column} HAVING COUNT(*) > 1) d",
        )
        status = PASS if n == 0 else FAIL
        return CheckResult(
            table, check.id, check.type, status, f"{int(n)} nilai ganda",
            check.weight, f"{check.column} harus unik",
        )

    if check.type == "freshness":
        hours = _scalar(
            conn,
            f"SELECT EXTRACT(EPOCH FROM (now() - MAX({check.column}))) / 3600 "
            f"FROM {qualified}",
        )
        if hours <= check.warn_hours:
            status = PASS
        elif hours <= check.error_hours:
            status = WARN
        else:
            status = FAIL
        return CheckResult(
            table, check.id, check.type, status, f"{hours:.1f} jam sejak update terakhir",
            check.weight,
            f"warn {check.warn_hours:g}j / error {check.error_hours:g}j",
        )

    if check.type == "row_count":
        n = _scalar(conn, f"SELECT COUNT(*) FROM {qualified}")
        status = PASS if check.min <= n <= check.max else FAIL
        return CheckResult(
            table, check.id, check.type, status, f"{int(n)} baris",
            check.weight, f"rentang {check.min}-{check.max}",
        )

    if check.type == "invariant":
        n = _scalar(
            conn,
            f"SELECT COUNT(*) FROM {qualified} WHERE NOT ({check.expression})",
        )
        status = PASS if n == 0 else FAIL
        return CheckResult(
            table, check.id, check.type, status, f"{int(n)} baris melanggar",
            check.weight, check.expression,
        )

    raise ValueError(f"unknown check type {check.type!r}")


def run_checks(conn: psycopg.Connection, contract: Contract) -> list[CheckResult]:
    """Run every check in the contract; results are deterministic per state."""
    results: list[CheckResult] = []
    for table_spec in contract.tables:
        for check in table_spec.checks:
            results.append(_run_check(conn, table_spec.name, check))
    return results


def freshness_hours(conn: psycopg.Connection, table: TableSpec) -> float | None:
    """Hours since the newest freshness column update — exported as a metric."""
    check = next((c for c in table.checks if c.type == "freshness"), None)
    if check is None:
        return None
    schema, name = table.name.split(".", 1)
    value = _scalar(
        conn,
        f'SELECT EXTRACT(EPOCH FROM (now() - MAX("{check.column}"))) / 3600 '
        f'FROM "{schema}"."{name}"',
    )
    return round(value, 2)


def last_check_time() -> str:
    """Timestamp of the most recent contract run, for the metrics endpoint."""
    return datetime.now().astimezone().isoformat(timespec="seconds")
