"""Data contract loading and validation (config/contract.yaml).

Pure module: no database, no network. The contract is the single source of
truth for what "good" means — checks, weights, alert levels, declared lineage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CHECK_TYPES = {"not_null", "unique", "freshness", "row_count", "invariant"}
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass(frozen=True)
class CheckSpec:
    """One executable quality check. Fields are used per ``type``."""

    type: str
    column: str | None = None
    expression: str | None = None
    min: int | None = None
    max: int | None = None
    warn_hours: float | None = None
    error_hours: float | None = None
    weight: float = 1.0

    @property
    def id(self) -> str:
        target = self.column or self.expression or "rows"
        return f"{self.type}:{target}"


@dataclass(frozen=True)
class TableSpec:
    name: str
    description: str
    owner: str
    checks: tuple[CheckSpec, ...]


@dataclass(frozen=True)
class DashboardSpec:
    service: str
    name: str
    description: str
    feeds: tuple[str, ...]


@dataclass(frozen=True)
class EdgeSpec:
    from_: str
    to: str


@dataclass(frozen=True)
class Contract:
    tables: tuple[TableSpec, ...]
    lineage: tuple[EdgeSpec, ...]
    dashboard: DashboardSpec
    warn_credit: float = 0.5
    error_level: float = 60.0
    warn_level: float = 85.0
    raw: dict = field(default_factory=dict, repr=False)


def _check_from_dict(entry: dict) -> CheckSpec:
    """Parse one flat check entry: {type: ..., column: ..., weight: ...}."""
    if "type" not in entry:
        raise ValueError(f"check entry {entry!r} needs a 'type' key")
    params = dict(entry)
    kind = params.pop("type")
    if kind not in CHECK_TYPES:
        raise ValueError(f"unknown check type {kind!r}; expected one of {sorted(CHECK_TYPES)}")
    weight = params.pop("weight", 1.0)
    try:
        check = CheckSpec(type=kind, weight=float(weight), **params)
    except TypeError as exc:
        raise ValueError(f"check entry {entry!r} has unknown fields: {exc}") from exc
    _validate_check(check)
    return check


def _validate_check(check: CheckSpec) -> None:
    if check.weight <= 0:
        raise ValueError(f"check {check.id} has non-positive weight {check.weight}")
    if check.type in {"not_null", "unique"}:
        if not check.column or not _IDENT.match(check.column):
            raise ValueError(f"check {check.id} needs a simple column identifier")
    elif check.type == "freshness":
        if not check.column or not _IDENT.match(check.column):
            raise ValueError(f"check {check.id} needs a simple column identifier")
        if check.warn_hours is None or check.error_hours is None:
            raise ValueError(f"check {check.id} needs warn_hours and error_hours")
        if check.warn_hours >= check.error_hours:
            raise ValueError(f"check {check.id}: warn_hours must be < error_hours")
    elif check.type == "row_count":
        if check.min is None or check.max is None or check.min > check.max:
            raise ValueError(f"check {check.id} needs min <= max")
    elif check.type == "invariant":
        if not check.expression:
            raise ValueError(f"check {check.id} needs an SQL boolean expression")


def load_contract(path: str | Path) -> Contract:
    doc = yaml.safe_load(Path(path).read_text())
    gates = doc.get("contract", {})
    tables = []
    for t in doc["tables"]:
        if not _IDENT.match(t["name"].split(".")[-1]):
            raise ValueError(f"table name {t['name']!r} is not a safe identifier")
        tables.append(
            TableSpec(
                name=t["name"],
                description=t.get("description", ""),
                owner=t["owner"],
                checks=tuple(_check_from_dict(c) for c in t["checks"]),
            )
        )
    lineage = tuple(
        EdgeSpec(from_=e["from"], to=e["to"]) for e in doc.get("lineage", [])
    )
    dash = doc["dashboard"]
    dashboard = DashboardSpec(
        service=dash["service"],
        name=dash["name"],
        description=dash.get("description", ""),
        feeds=tuple(dash.get("feeds", [])),
    )
    return Contract(
        tables=tuple(tables),
        lineage=lineage,
        dashboard=dashboard,
        warn_credit=float(gates.get("warn_credit", 0.5)),
        error_level=float(gates.get("error_level", 60)),
        warn_level=float(gates.get("warn_level", 85)),
        raw=doc,
    )
