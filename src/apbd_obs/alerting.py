"""Slack-style alerts for quality-level changes (Block Kit, dry-run by default).

Edge-triggered on the *set* of tables currently in `error`: an alert fires
when a table enters the set, and a separate all-clear payload fires when it
leaves. Without that, a cron re-run would re-alert the same breach forever.

Real delivery needs SLACK_WEBHOOK_URL; without it the payload is written to
docs/generated/ so the committed evidence shows exactly what would be sent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import requests

from .scoring import CheckResult, level, score_table

_STATE_PATH = Path("var/alert_state.json")


def breaching_tables(
    results: list[CheckResult],
    warn_credit: float,
    error_level: float,
    warn_level: float,
) -> dict[str, str]:
    """table -> level ('warn'|'error') for tables below the warn gate."""
    out: dict[str, str] = {}
    for table in sorted({r.table for r in results}):
        score = score_table(results, table, warn_credit)
        lvl = level(score, error_level, warn_level)
        if lvl != "ok":
            out[table] = lvl
    return out


def _payload(table: str, lvl: str, score: float, failing: list[CheckResult]) -> dict:
    color = "#e01e5a" if lvl == "error" else "#ecb22e"
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{'ERROR' if lvl == 'error' else 'WARNING'}: kualitas {table} turun",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Skor:*\n{score}/100"},
                {"type": "mrkdwn", "text": f"*Level:*\n{lvl.upper()}"},
            ],
        },
    ]
    if failing:
        lines = "\n".join(f"• `{r.check_id}` — {r.status}: {r.observed}" for r in failing)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": lines}})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "#data-quality-alerts · apbd-observability · "
                    "kontrak: config/contract.yaml",
                }
            ],
        }
    )
    return {"channel": "#data-quality-alerts", "color": color, "blocks": blocks}


def _all_clear_payload(table: str, score: float) -> dict:
    return {
        "channel": "#data-quality-alerts",
        "color": "#2eb67d",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"ALL-CLEAR: {table} pulih"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Skor kembali {score}/100. Tidak ada check gagal.",
                },
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "#data-quality-alerts · apbd-observability"}
                ],
            },
        ],
    }


def load_state(path: Path = _STATE_PATH) -> set[str]:
    if path.exists():
        return set(json.loads(path.read_text()).get("breaching", []))
    return set()


def save_state(breaching: set[str], path: Path = _STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"breaching": sorted(breaching)}, indent=2) + "\n")


def evaluate_alerts(
    results: list[CheckResult],
    warn_credit: float,
    error_level: float,
    warn_level: float,
    state_path: Path = _STATE_PATH,
    out_dir: Path = Path("docs/generated"),
) -> list[dict]:
    """Compute new breaches/recoveries vs persisted state; emit payloads.

    Returns the payloads that were produced (and sends them if
    SLACK_WEBHOOK_URL is set). Persists the new state.
    """
    current = breaching_tables(results, warn_credit, error_level, warn_level)
    previous = load_state(state_path)

    entered = set(current) - previous
    recovered = previous - set(current)

    payloads: list[dict] = []
    for table in sorted(entered):
        lvl = current[table]
        score = score_table(results, table, warn_credit)
        failing = [r for r in results if r.table == table and r.status != "PASS"]
        payloads.append(_payload(table, lvl, score, failing))
    for table in sorted(recovered):
        score = score_table(results, table, warn_credit)
        payloads.append(_all_clear_payload(table, score))

    if payloads:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "quality_alert_payload.json").write_text(
            json.dumps(payloads, indent=2, ensure_ascii=False) + "\n"
        )
        webhook = os.environ.get("SLACK_WEBHOOK_URL")
        if webhook:
            for p in payloads:
                requests.post(webhook, json=p, timeout=10)

    save_state(set(current), state_path)
    return payloads
