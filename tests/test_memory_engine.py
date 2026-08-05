"""
Engine 10 — Organizational Memory & KPI System.
`build_kpi()` is pure/synchronous; semantic search (OpenAI embeddings) is
not exercised here.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from engines.memory_system import MemoryEngine


def _record(agency, outcome, score, days_ago, lessons=None):
    return SimpleNamespace(
        agency=agency,
        outcome=outcome,
        score=score,
        created_at=datetime.utcnow() - timedelta(days=days_ago),
        lessons_learned=lessons or [],
    )


def test_build_kpi_empty_records_does_not_divide_by_zero():
    dashboard = MemoryEngine().build_kpi("org-1", [])
    assert dashboard.total_proposals == 0
    assert dashboard.success_rate == 0.0
    assert dashboard.avg_score == 0.0


def test_build_kpi_computes_success_rate_and_avg_score():
    records = [
        _record("NSF", "funded", 85.0, days_ago=10),
        _record("NSF", "declined", 55.0, days_ago=5),
        _record("NIH", "funded", 90.0, days_ago=1),
    ]
    dashboard = MemoryEngine().build_kpi("org-1", records)

    assert dashboard.total_proposals == 3
    assert dashboard.funded == 2
    assert dashboard.success_rate == round(2 / 3 * 100, 1)
    assert dashboard.avg_score == round((85.0 + 55.0 + 90.0) / 3, 2)
    assert dashboard.proposals_by_agency == {"NSF": 2, "NIH": 1}


def test_build_kpi_score_trend_is_sorted_oldest_first():
    records = [
        _record("NSF", "funded", 80.0, days_ago=1),
        _record("NSF", "funded", 70.0, days_ago=20),
    ]
    dashboard = MemoryEngine().build_kpi("org-1", records)

    assert [entry["score"] for entry in dashboard.score_trend] == [70.0, 80.0]


def test_build_kpi_top_weaknesses_ranked_by_frequency():
    records = [
        _record("NSF", "declined", 40.0, 1, lessons=["weak budget", "unclear timeline"]),
        _record("NSF", "declined", 45.0, 2, lessons=["weak budget"]),
        _record("NIH", "funded", 90.0, 3, lessons=["unclear timeline"]),
    ]
    dashboard = MemoryEngine().build_kpi("org-1", records)

    assert dashboard.top_weaknesses[0] == "weak budget"  # appears twice, most frequent
