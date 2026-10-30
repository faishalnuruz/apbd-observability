"""OpenMetadata catalog sync via the real platform's REST API.

Not a mock: this talks to the OpenMetadata server running in compose
(default http://localhost:8585/api, basic auth admin/admin). It registers:

  - a Postgres database service pointing at the data plane,
  - one table per contract entry, with description + owner + PII tags,
  - a Metabase-style dashboard service + the dashboard entity,
  - the declared lineage edges (dashboard -> fact -> staging -> raw).

Everything is idempotent: services/tables are looked up by FQN first and
created only when missing, so `catalog sync` is safe to re-run.
"""

from __future__ import annotations

import os
from base64 import b64encode
from dataclasses import dataclass
from urllib.parse import quote

import requests

from .contract import Contract

DEFAULT_OM = os.environ.get("OM_API", "http://localhost:8585/api")
DEFAULT_USER = os.environ.get("OM_USER", "admin@open-metadata.org")
DEFAULT_PASSWORD = os.environ.get("OM_PASSWORD", "admin")

# Contract table -> (pii columns, non-PII columns) for the search demo.
PII_COLUMNS = {
    "raw.transaksi": ["nik_pelapor"],
    "staging.stg_transaksi": ["nik_pelapor"],
    "marts.transaksi_bku": ["nik_pelapor"],
}


class CatalogError(RuntimeError):
    """OpenMetadata API returned an error."""


@dataclass
class CatalogSummary:
    service: str
    tables: list[str]
    dashboard: str
    lineage_edges: int
    created: list[str]


class OmClient:
    """Minimal OpenMetadata REST client (basic auth, Bearer JWT)."""

    def __init__(self, base_url: str = DEFAULT_OM, user: str = DEFAULT_USER,
                 password: str = DEFAULT_PASSWORD, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self._login(user, password)

    def _login(self, user: str, password: str) -> None:
        # OM 2.0 login protocol: the field is `email` (full address works for
        # the quickstart admin), and the password must be base64-encoded.
        resp = self.session.post(
            f"{self.base}/v1/users/login",
            json={"email": user, "password": b64encode(password.encode()).decode()},
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise CatalogError(f"login gagal: {resp.status_code} {resp.text[:300]}")
        token = resp.json().get("accessToken")
        if not token:
            raise CatalogError("respons login tidak berisi accessToken")
        self.session.headers["Authorization"] = f"Bearer {token}"

    def _req(self, method: str, path: str, **kwargs) -> dict | list | None:
        resp = self.session.request(method, f"{self.base}{path}", timeout=self.timeout, **kwargs)
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise CatalogError(f"{method} {path} -> {resp.status_code} {resp.text[:300]}")
        return resp.json() if resp.text else None

    def get(self, path: str, **kwargs):
        return self._req("GET", path, **kwargs)
    def post(self, path: str, payload: dict):
        return self._req("POST", path, json=payload)

    def put(self, path: str, payload: dict):
        return self._req("PUT", path, json=payload)

    def patch(self, path: str, ops: list[dict]):
        """JSON Patch — requires the json-patch content type per RFC 6902."""
        resp = self.session.request(
            "PATCH",
            f"{self.base}{path}",
            headers={"Content-Type": "application/json-patch+json"},
            json=ops,
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise CatalogError(f"PATCH {path} -> {resp.status_code} {resp.text[:300]}")
        return resp.json() if resp.text else None


def _fqn(*parts: str) -> str:
    return ".".join(parts)


def sync_catalog(
    contract: Contract,
    client: OmClient | None = None,
    service_name: str = "bpkd_postgres",
) -> CatalogSummary:
    """Register the whole contract in OpenMetadata. Idempotent."""
    client = client or OmClient()
    created: list[str] = []

    # 1. Database service -> the data-plane PostgreSQL.
    svc = client.get(f"/v1/services/databaseServices/name/{_fqn(service_name)}")
    if svc is None:
        client.post(
            "/v1/services/databaseServices",
            {
                "name": service_name,
                "serviceType": "Postgres",
                "connection": {
                    "config": {
                        # OM 2.0: the config discriminator is the service type
                        # ("Postgres"), not the 1.x "PostgresConnection".
                        "type": "Postgres",
                        "username": "bpkd",
                        "authType": {"password": "bpkd-2026"},
                        "hostPort": "postgres:5432",
                        "database": "bpkd",
                    }
                },
            },
        )
        created.append(f"service:{service_name}")
        svc = client.get(f"/v1/services/databaseServices/name/{_fqn(service_name)}")
    _ = svc  # service exists from here on

    # 2. Database + schema containers (raw, staging, marts).
    # OM 2.0 create requests take parent refs as FQN strings, not objects.
    db_fqn = _fqn(service_name, "bpkd")
    if client.get(f"/v1/databases/name/{db_fqn}") is None:
        client.post(
            "/v1/databases",
            {"name": "bpkd", "service": service_name},
        )
        created.append(f"database:{db_fqn}")

    schema_fqns: dict[str, str] = {}
    for schema in ("raw", "staging", "marts"):
        s_fqn = _fqn(db_fqn, schema)
        schema_fqns[schema] = s_fqn
        if client.get(f"/v1/databaseSchemas/name/{s_fqn}") is None:
            client.post(
                "/v1/databaseSchemas",
                {"name": schema, "database": db_fqn},
            )
            created.append(f"schema:{s_fqn}")

    # 3. Tables with description, owner, and PII tags on sensitive columns.
    tables: list[str] = []
    for table in contract.tables:
        schema, name = table.name.split(".", 1)
        t_fqn = _fqn(db_fqn, schema, name)
        tables.append(table.name)
        # `fields=owners,columns,tags`: tag labels are stripped from the
        # response unless explicitly requested, and the PATCHes below must
        # see current state to stay idempotent (no duplicate owners/tags).
        existing = client.get(
            f"/v1/tables/name/{t_fqn}",
            params={"fields": "owners,columns,tags"},
        )
        if existing is None:
            columns = []
            # Column list mirrors what the seeder actually creates.
            col_defs = _COLUMNS.get(table.name, [])
            for col_name, col_type in col_defs:
                columns.append(
                    {"name": col_name, "dataType": col_type, "dataTypeDisplay": col_type}
                )
            client.post(
                "/v1/tables",
                {
                    "name": name,
                    "displayName": table.name,
                    "description": table.description,
                    "columns": columns,
                    "databaseSchema": schema_fqns[schema],
                },
            )
            created.append(f"table:{table.name}")
            existing = client.get(f"/v1/tables/name/{t_fqn}")
        # Owners and PII tags are set via JSON Patch — OM 2.0 drops both when
        # passed at create time. PATCH is idempotent: only applied when missing.
        owner_id = _ensure_user(client, table.owner)
        owners = existing.get("owners") or []
        if not owners:
            client.patch(
                f"/v1/tables/{existing['id']}",
                [{"op": "add", "path": "/owners/0",
                  "value": {"id": owner_id, "type": "user"}}],
            )
        pii = set(PII_COLUMNS.get(table.name, []))
        for idx, col in enumerate(existing.get("columns", [])):
            if col["name"] in pii and not col.get("tags"):
                client.patch(
                    f"/v1/tables/{existing['id']}",
                    [{"op": "add", "path": f"/columns/{idx}/tags/0",
                      "value": {"tagFQN": "PII.Sensitive",
                                "source": "Classification"}}],
                )

    # 4. Dashboard service + dashboard entity (the lineage terminus).
    d_svc_fqn = _fqn(contract.dashboard.service)
    if client.get(f"/v1/services/dashboardServices/name/{d_svc_fqn}") is None:
        client.post(
            "/v1/services/dashboardServices",
            {
                "name": contract.dashboard.service,
                "serviceType": "Metabase",
                "connection": {
                    "config": {
                        "type": "Metabase",
                        "hostPort": "http://host.docker.internal:3300",
                        "username": "demo",
                        "password": "demo",
                    }
                },
            },
        )
        created.append(f"dashboardService:{d_svc_fqn}")
    _ = client.get(f"/v1/services/dashboardServices/name/{d_svc_fqn}")

    dash_fqn = _fqn(d_svc_fqn, contract.dashboard.name)
    if client.get(f"/v1/dashboards/name/{dash_fqn}") is None:
        client.post(
            "/v1/dashboards",
            {
                "name": contract.dashboard.name,
                "displayName": contract.dashboard.name,
                "description": contract.dashboard.description,
                "service": d_svc_fqn,
            },
        )
        created.append(f"dashboard:{dash_fqn}")

    # 5. Lineage: each contract edge, plus dashboard -> its feeding facts.
    edges = list(contract.lineage)
    for feed in contract.dashboard.feeds:
        edges.append(_edge(feed, f"dashboard::{dash_fqn}"))
    lineage_count = 0
    for edge in edges:
        _upsert_lineage(client, db_fqn, edge.from_, edge.to)
        lineage_count += 1

    return CatalogSummary(
        service=service_name,
        tables=sorted(tables),
        dashboard=contract.dashboard.name,
        lineage_edges=lineage_count,
        created=created,
    )


def _edge(from_: str, to: str):
    from dataclasses import dataclass as _dc

    @_dc(frozen=True)
    class _E:
        from_: str
        to: str

    return _E(from_=from_, to=to)


def _upsert_lineage(client: OmClient, db_fqn: str, from_: str, to: str) -> None:
    """`to` depends on `from_`: from_ is upstream of to.

    OM 2.0 moved edge creation to a URL-based endpoint:
    PUT /v1/lineage/{fromType}/name/{fromFQN}/{toType}/name/{toFQN}
    """
    from_type, from_fqn = "table", f"{db_fqn}.{from_}"
    if to.startswith("dashboard::"):
        to_type, to_fqn = "dashboard", to.removeprefix("dashboard::")
    else:
        to_type, to_fqn = "table", f"{db_fqn}.{to}"
    path = (
        f"/v1/lineage/{from_type}/name/{quote(from_fqn, safe='')}"
        f"/{to_type}/name/{quote(to_fqn, safe='')}"
    )
    client.put(path, {})


def _ensure_user(client: OmClient, username: str) -> str:
    """Look up (or create) a user to own catalog entries."""
    user = client.get(f"/v1/users/name/{username}")
    if user is None:
        user = client.post(
            "/v1/users",
            {
                "name": username,
                "displayName": username.replace("_", " ").title(),
                "email": f"{username}@bpkd.go.id",
                "isAdmin": False,
            },
        )
    return user["id"]


def search_catalog(client: OmClient, query: str) -> list[dict]:
    """Full-text search over the catalog — the PRD's 'searchable catalog'."""
    resp = client.get("/v1/search/query", params={"q": query, "index": "dataAsset"})
    hits = (resp or {}).get("hits", {}).get("hits", [])
    return [
        {
            "fqn": h.get("_source", {}).get("fullyQualifiedName"),
            "type": h.get("_source", {}).get("entityType"),
            "description": (h.get("_source", {}).get("description") or "")[:120],
        }
        for h in hits[:10]
    ]


def read_lineage(client: OmClient, table_fqn: str) -> dict:
    """Read the lineage graph around one table (OM 2.0: GET /v1/lineage)."""
    path = (
        f"/v1/lineage/table/name/{quote(table_fqn, safe='')}"
        "?upstreamDepth=3&downstreamDepth=3"
    )
    return client.get(path) or {}


def verify_catalog(
    contract: Contract,
    client: OmClient | None = None,
    service_name: str = "bpkd_postgres",
) -> dict:
    """Read back what the sync claims: tables, owners, PII tags, lineage.

    Every field in the result is what OpenMetadata actually returned, so a
    failed verify means the catalog is genuinely out of sync with the
    contract (missing table, dropped owner, untagged PII, lineage drift).
    """
    from .lineage import LineageEdge, diff, expected_edges

    client = client or OmClient()
    db_fqn = _fqn(service_name, "bpkd")
    tables: list[dict] = []
    for table in contract.tables:
        schema, name = table.name.split(".", 1)
        t_fqn = _fqn(db_fqn, schema, name)
        raw = client.get(
            f"/v1/tables/name/{t_fqn}", params={"fields": "owners,columns,tags"}
        )
        if raw is None:
            tables.append({"fqn": t_fqn, "present": False})
            continue
        pii = set(PII_COLUMNS.get(table.name, []))
        pii_tags = sorted(
            tag.get("tagFQN", "")
            for col in raw.get("columns", [])
            if col["name"] in pii
            for tag in (col.get("tags") or [])
        )
        tables.append(
            {
                "fqn": t_fqn,
                "present": True,
                "description": bool(raw.get("description")),
                "owners": [o.get("name") for o in raw.get("owners") or []],
                "expected_owner": table.owner,
                "pii_tags": pii_tags,
                "expected_pii": ["PII.Sensitive"] * len(pii),
            }
        )

    # Lineage read-back: every contract table's upstream graph, plus the
    # dashboard terminus on the feed tables' downstream side. Edges are
    # id-based; `nodes` carries the id -> entity map.
    observed: set[LineageEdge] = set()
    for table in contract.tables:
        schema, name = table.name.split(".", 1)
        t_fqn = _fqn(db_fqn, schema, name)
        graph = read_lineage(client, t_fqn)
        by_id = {n["id"]: n for n in graph.get("nodes", [])}
        if graph.get("entity"):
            by_id[graph["entity"]["id"]] = graph["entity"]

        for edge in graph.get("upstreamEdges", []):
            src = by_id.get(edge["fromEntity"], {})
            dst = by_id.get(edge["toEntity"], {})
            observed.add(LineageEdge(_node_name(src), _node_name(dst)))
    # The dashboard terminus: feed tables must list it downstream.
    for feed in contract.dashboard.feeds:
        schema, name = feed.split(".", 1)
        feed_graph = read_lineage(client, _fqn(db_fqn, schema, name))
        by_id = {n["id"]: n for n in feed_graph.get("nodes", [])}
        if feed_graph.get("entity"):
            by_id[feed_graph["entity"]["id"]] = feed_graph["entity"]
        for edge in feed_graph.get("downstreamEdges", []):
            dst = by_id.get(edge["toEntity"], {})
            if dst.get("type") == "dashboard":
                observed.add(LineageEdge(feed, f"dashboard::{dst['name']}"))

    drift = diff(expected_edges(contract), observed)
    return {
        "service": service_name,
        "tables": tables,
        "lineage_drift": {
            "missing_from_catalog": [
                {"from": e.frm, "to": e.to} for e in drift["missing_from_catalog"]
            ],
            "stale_in_contract": [
                {"from": e.frm, "to": e.to} for e in drift["stale_in_contract"]
            ],
        },
    }


def _node_name(node: dict) -> str:
    """OM lineage node -> contract-local name: 'schema.table' or 'dashboard::name'."""
    if node.get("type") == "dashboard":
        return f"dashboard::{node['name']}"
    fqn = node.get("fullyQualifiedName", "")
    return ".".join(fqn.split(".")[-2:])


# Column layouts, mirroring seed.py exactly (checked by an integration test).
_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "raw.transaksi": [
        ("id_bku", "BIGINT"), ("kode_skpd", "TEXT"), ("nik_pelapor", "TEXT"),
        ("uraian", "TEXT"), ("nilai", "NUMBER"), ("tanggal", "DATE"),
        ("loaded_at", "TIMESTAMP"),
    ],
    "staging.stg_transaksi": [
        ("id_bku", "BIGINT"), ("kode_skpd", "TEXT"), ("nik_pelapor", "TEXT"),
        ("uraian", "TEXT"), ("nilai", "NUMBER"), ("tanggal", "DATE"),
        ("loaded_at", "TIMESTAMP"),
    ],
    "marts.transaksi_bku": [
        ("id_bku", "BIGINT"), ("kode_skpd", "TEXT"), ("nik_pelapor", "TEXT"),
        ("uraian", "TEXT"), ("nilai", "NUMBER"), ("tanggal", "DATE"),
        ("loaded_at", "TIMESTAMP"),
    ],
    "marts.dim_skpd": [
        ("kode_skpd", "TEXT"), ("nama_skpd", "TEXT"), ("updated_at", "TIMESTAMP"),
    ],
    "marts.fct_realisasi": [
        ("kode_skpd", "TEXT"), ("pagu", "NUMBER"), ("realisasi", "NUMBER"),
        ("persen", "NUMBER"), ("updated_at", "TIMESTAMP"),
    ],
}
