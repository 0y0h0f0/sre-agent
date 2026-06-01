"""Runbook result reranking."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from packages.common.time import utc_now

WORD_RE = re.compile(r"[a-z0-9_]+")


def rerank_score(
    *,
    query: str,
    metadata: dict[str, Any],
    title: str,
    vector_score: float,
    service: str | None,
    incident_type: str | None,
) -> float:
    service_match = 1.0 if service and metadata.get("service") == service else 0.0
    incident_type_match = (
        1.0 if incident_type and metadata.get("incident_type") == incident_type else 0.0
    )
    title_keyword_match = _title_keyword_match(query, title)
    freshness_score = _freshness_score(metadata.get("updated_at"))
    score = (
        _clamp01(vector_score) * 0.65
        + service_match * 0.15
        + incident_type_match * 0.10
        + title_keyword_match * 0.05
        + freshness_score * 0.05
    )
    return round(score, 6)


def _title_keyword_match(query: str, title: str) -> float:
    query_terms = set(WORD_RE.findall(query.lower()))
    if not query_terms:
        return 0.0
    title_terms = set(WORD_RE.findall(title.lower()))
    if not title_terms:
        return 0.0
    return len(query_terms & title_terms) / len(query_terms)


def _freshness_score(value: object) -> float:
    if not value:
        return 0.0
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        return 0.0
    age_days = max(0, (utc_now().date() - parsed).days)
    if age_days <= 30:
        return 1.0
    if age_days <= 180:
        return 0.8
    if age_days <= 365:
        return 0.6
    return 0.4


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))
