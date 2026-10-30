"""Scoring math: check results -> 0-100 quality score. Pure, heavily tested.

PASS earns full weight, WARN half (configurable), FAIL nothing. A table's
score is the credit earned over the credit possible; the portfolio score is
the plain mean of table scores.
"""

from __future__ import annotations

from dataclasses import dataclass

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass(frozen=True)
class CheckResult:
    table: str
    check_id: str
    check_type: str
    status: str  # PASS | WARN | FAIL
    observed: str  # human-readable observed value ("12 baris", "5.1 jam")
    weight: float
    message: str = ""

    @property
    def credit(self) -> float:
        if self.status == PASS:
            return 1.0
        if self.status == WARN:
            return 0.5
        return 0.0


def score_table(results: list[CheckResult], table: str, warn_credit: float = 0.5) -> float:
    """0-100 score for one table. A table with no checks scores 100."""
    mine = [r for r in results if r.table == table]
    if not mine:
        return 100.0
    earned = sum(
        r.weight * (1.0 if r.status == PASS else warn_credit if r.status == WARN else 0.0)
        for r in mine
    )
    possible = sum(r.weight for r in mine)
    return round(100.0 * earned / possible, 1)


def portfolio_score(results: list[CheckResult], warn_credit: float = 0.5) -> float:
    """Plain mean over the distinct tables present in ``results``."""
    tables = sorted({r.table for r in results})
    if not tables:
        return 100.0
    scores = [score_table(results, t, warn_credit) for t in tables]
    return round(sum(scores) / len(scores), 1)


def level(score: float, error_level: float, warn_level: float) -> str:
    """ok | warn | error for a score against the contract's gates."""
    if score < error_level:
        return "error"
    if score < warn_level:
        return "warn"
    return "ok"
