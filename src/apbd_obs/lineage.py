"""Lineage: declared graph -> Mermaid + JSON export, and drift both ways.

The contract declares what the lineage *should* be. OpenMetadata holds what
it *is* after catalog sync. ``diff`` compares the two in both directions and
returns what's missing from the catalog and what's stale in the contract —
the same idea as lakehouse-governance's drift check, but here across the
dashboard -> fact -> staging -> source chain.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .contract import Contract


@dataclass(frozen=True)
class LineageEdge:
    frm: str
    to: str


def mermaid(contract: Contract) -> str:
    """Render the declared graph (plus dashboard terminus) as Mermaid."""
    lines = ["graph LR"]
    for edge in contract.lineage:
        lines.append(f"    {edge.from_} --> {edge.to}")
    for feed in contract.dashboard.feeds:
        label = contract.dashboard.name.replace(" ", "_")
        lines.append(f'    {feed} --> {label}["{contract.dashboard.name}"]')
    return "\n".join(lines) + "\n"


def to_json(contract: Contract) -> str:
    edges = [
        {"from": e.from_, "to": e.to, "kind": "table"}
        for e in contract.lineage
    ]
    for feed in contract.dashboard.feeds:
        edges.append(
            {
                "from": feed,
                "to": contract.dashboard.name,
                "kind": "dashboard",
                "dashboard_service": contract.dashboard.service,
            }
        )
    return json.dumps(
        {"dashboard": contract.dashboard.name, "edges": edges},
        indent=2,
        ensure_ascii=False,
    ) + "\n"


def expected_edges(contract: Contract) -> set[LineageEdge]:
    edges = {LineageEdge(e.from_, e.to) for e in contract.lineage}
    for feed in contract.dashboard.feeds:
        edges.add(LineageEdge(feed, f"dashboard::{contract.dashboard.name}"))
    return edges


def observed_edges_from_om(lineage_json: dict) -> set[LineageEdge]:
    """Parse an OpenMetadata /v1/tables/lineage response into edge pairs."""
    edges: set[LineageEdge] = set()
    for node in lineage_json.get("downstreamEdges", []):
        edges.add(LineageEdge(lineage_json["entity"], node["entity"]))
    for node in lineage_json.get("upstreamEdges", []):
        edges.add(LineageEdge(node["entity"], lineage_json["entity"]))
    return edges


def diff(
    expected: set[LineageEdge], observed: set[LineageEdge]
) -> dict[str, list[LineageEdge]]:
    """Both directions: missing from catalog, and stale in contract."""
    return {
        "missing_from_catalog": sorted(expected - observed, key=lambda e: (e.frm, e.to)),
        "stale_in_contract": sorted(observed - expected, key=lambda e: (e.frm, e.to)),
    }
