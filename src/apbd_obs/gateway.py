"""RBAC query gateway: the only door into the warehouse, with an audit trail.

Every query passes through ``authorize()``: unknown role or table is denied
(fail closed), allowed queries get their row filter and column masking
applied, and the decision — allowed or denied — is recorded as an
``AuditEntry``. Enforcing in one choke point is what makes the audit log
complete; scattered WHERE clauses would never be.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psycopg
import yaml

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class AccessDenied(Exception):
    """Raised when a role may not run a query; message says why."""


@dataclass(frozen=True)
class RolePolicy:
    name: str
    description: str
    tables: tuple[str, ...]
    row_filter: dict[str, list[str]] | None
    mask_columns: tuple[str, ...]


@dataclass
class AuditEntry:
    ts: float
    role: str
    table: str
    decision: str  # allowed | denied
    reason: str
    masked_columns: list[str] = field(default_factory=list)
    rows_returned: int | None = None
    duration_ms: float | None = None


def load_policies(path: str | Path) -> dict[str, RolePolicy]:
    doc = yaml.safe_load(Path(path).read_text())
    policies = {}
    for name, cfg in doc["roles"].items():
        policies[name] = RolePolicy(
            name=name,
            description=cfg.get("description", ""),
            tables=tuple(cfg.get("tables", [])),
            row_filter=cfg.get("row_filter"),
            mask_columns=tuple(cfg.get("mask_columns", [])),
        )
    return policies


@dataclass(frozen=True)
class QueryPlan:
    sql: str
    params: list
    masked_columns: list[str]


def plan_query(policy: RolePolicy, table: str, columns: list[str], limit: int = 100) -> QueryPlan:
    """Pure query planner: policy + table + columns -> parameterised SQL."""
    if table not in policy.tables:
        raise AccessDenied(f"role {policy.name!r} tidak punya akses ke {table!r}")
    if not columns or not all(_IDENT.match(c) for c in columns):
        raise AccessDenied(f"kolom tidak aman: {columns!r}")
    if not 0 < limit <= 10_000:
        raise AccessDenied(f"limit di luar rentang: {limit}")

    select_parts = []
    masked = []
    for col in columns:
        if col in policy.mask_columns:
            select_parts.append(f"left({col}, 2) || '***' AS {col}")
            masked.append(col)
        else:
            select_parts.append(col)

    where = ""
    params: list = []
    if policy.row_filter:
        clauses = []
        for col, values in policy.row_filter.items():
            if col not in columns:
                raise AccessDenied(f"row filter kolom {col!r} tidak ikut dipilih")
            clauses.append(f"{col} = ANY(%s)")
            params.append(values)
        where = " WHERE " + " AND ".join(clauses)

    schema, name = table.split(".", 1)
    sql = (
        f'SELECT {", ".join(select_parts)} FROM "{schema}"."{name}"'
        f"{where} ORDER BY 1 LIMIT {int(limit)}"
    )
    return QueryPlan(sql=sql, params=params, masked_columns=masked)


class Gateway:
    """Executes planned queries and appends every decision to the audit log."""

    def __init__(self, policies: dict[str, RolePolicy], audit_path: str | Path):
        self.policies = policies
        self.audit_path = Path(audit_path)
        self.last_entry: AuditEntry | None = None

    def audit(self, entry: AuditEntry) -> None:
        self.last_entry = entry
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a") as fh:
            fh.write(json.dumps(asdict(entry)) + "\n")

    def query(
        self,
        conn: psycopg.Connection,
        role: str,
        table: str,
        columns: list[str],
        limit: int = 100,
    ) -> list[tuple]:
        started = time.perf_counter()
        policy = self.policies.get(role)
        if policy is None:
            entry = AuditEntry(time.time(), role, table, "denied", "role tidak dikenal")
            self.audit(entry)
            raise AccessDenied(f"role {role!r} tidak dikenal")

        try:
            plan = plan_query(policy, table, columns, limit)
        except AccessDenied as exc:
            self.audit(AuditEntry(time.time(), role, table, "denied", str(exc)))
            raise

        with conn.cursor() as cur:
            cur.execute(plan.sql, plan.params)
            rows = cur.fetchall()
        duration = (time.perf_counter() - started) * 1000
        self.audit(
            AuditEntry(
                ts=time.time(),
                role=role,
                table=table,
                decision="allowed",
                reason="sesuai kebijakan",
                masked_columns=plan.masked_columns,
                rows_returned=len(rows),
                duration_ms=round(duration, 1),
            )
        )
        return rows
