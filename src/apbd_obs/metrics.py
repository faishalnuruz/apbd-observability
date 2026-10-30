"""Prometheus metrics endpoint + the tiny HTTP server behind it.

The contract results are re-exported in Prometheus text format so Grafana
can chart the quality score, freshness, and per-check status. Scoring and
rendering are pure functions; the server just wires them to HTTP.
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .scoring import CheckResult, level, portfolio_score, score_table


def render_metrics(
    results: list[CheckResult],
    warn_credit: float,
    error_level: float,
    warn_level: float,
    freshness: dict[str, float],
    catalog_tables: int,
    audit_counts: dict[str, int],
) -> str:
    """Pure: check results + counters -> Prometheus text exposition."""
    lines = [
        "# HELP apbd_quality_score Table quality score 0-100 (contract checks).",
        "# TYPE apbd_quality_score gauge",
    ]
    tables = sorted({r.table for r in results})
    for table in tables:
        score = score_table(results, table, warn_credit)
        lines.append(f'apbd_quality_score{{table="{table}"}} {score}')

    overall = portfolio_score(results, warn_credit)
    lvl = level(overall, error_level, warn_level)
    lines += [
        "# HELP apbd_quality_score_portfolio Portfolio-wide quality score 0-100.",
        "# TYPE apbd_quality_score_portfolio gauge",
        f"apbd_quality_score_portfolio {overall}",
        '# HELP apbd_quality_level Quality level as a number: 0=ok 1=warn 2=error.',
        "# TYPE apbd_quality_level gauge",
        f"apbd_quality_level { {'ok': 0, 'warn': 1, 'error': 2}[lvl] }",
    ]

    lines += [
        "# HELP apbd_check_status Latest status per contract check (1 pass, 0.5 warn, 0 fail).",
        "# TYPE apbd_check_status gauge",
    ]
    for r in results:
        val = 1.0 if r.status == "PASS" else 0.5 if r.status == "WARN" else 0.0
        lines.append(
            f'apbd_check_status{{table="{r.table}",check="{r.check_id}",'
            f'status="{r.status}"}} {val}'
        )

    lines += [
        "# HELP apbd_freshness_hours Hours since newest row per table.",
        "# TYPE apbd_freshness_hours gauge",
    ]
    for table, hours in sorted(freshness.items()):
        lines.append(f'apbd_freshness_hours{{table="{table}"}} {hours}')

    lines += [
        "# HELP apbd_catalog_tables Number of tables registered in OpenMetadata.",
        "# TYPE apbd_catalog_tables gauge",
        f"apbd_catalog_tables {catalog_tables}",
        "# HELP apbd_audit_events_total Gateway audit events by decision.",
        "# TYPE apbd_audit_events_total counter",
    ]
    for decision, count in sorted(audit_counts.items()):
        lines.append(f'apbd_audit_events_total{{decision="{decision}"}} {count}')

    lines += [
        "# HELP apbd_scrape_timestamp_seconds When this snapshot was rendered.",
        "# TYPE apbd_scrape_timestamp_seconds gauge",
        f"apbd_scrape_timestamp_seconds {time.time():.0f}",
    ]
    return "\n".join(lines) + "\n"


class MetricsState:
    """Mutable snapshot the HTTP handler serves; updated by the CLI runs."""

    def __init__(self) -> None:
        self.results: list[CheckResult] = []
        self.warn_credit = 0.5
        self.error_level = 60.0
        self.warn_level = 85.0
        self.freshness: dict[str, float] = {}
        self.catalog_tables = 0
        self.audit_counts: dict[str, int] = {}
        self.last_run: str = ""

    def snapshot(self) -> dict:
        return {
            "quality_score_portfolio": portfolio_score(self.results, self.warn_credit),
            "tables": {
                t: score_table(self.results, t, self.warn_credit)
                for t in sorted({r.table for r in self.results})
            },
            "checks": [
                {"table": r.table, "check": r.check_id, "status": r.status,
                 "observed": r.observed}
                for r in self.results
            ],
            "freshness_hours": self.freshness,
            "catalog_tables": self.catalog_tables,
            "audit_events": self.audit_counts,
            "last_run": self.last_run,
        }


def make_handler(state: MetricsState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
            pass  # keep the console clean; Prometheus polls every 15s

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib signature
            if self.path.startswith("/metrics"):
                text = render_metrics(
                    state.results, state.warn_credit, state.error_level,
                    state.warn_level, state.freshness, state.catalog_tables,
                    state.audit_counts,
                )
                self._send(200, text.encode(), "text/plain; version=0.0.4")
            elif self.path.startswith("/healthz"):
                self._send(200, b"ok\n", "text/plain")
            elif self.path.startswith("/api/score"):
                body = json.dumps(state.snapshot(), indent=2).encode()
                self._send(200, body, "application/json")
            else:
                self._send(404, b"not found\n", "text/plain")

    return Handler


def serve(state: MetricsState, host: str = "0.0.0.0", port: int = 8100) -> None:
    """Block and serve /metrics, /healthz, /api/score."""
    server = ThreadingHTTPServer((host, port), make_handler(state))
    server.serve_forever()
