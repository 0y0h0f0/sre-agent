"""BM25 / PostgreSQL tsvector hybrid search utilities."""

from __future__ import annotations

import re

WORD_RE = re.compile(r"[a-z0-9_]+")


def build_tsquery(query_text: str) -> str:
    """Convert a user query to a PostgreSQL tsquery string.

    Terms are stemmed, the last term is prefix-matched (:*), and terms
    are joined with & for AND semantics.
    """
    terms = [term.lower() for term in WORD_RE.findall(query_text) if len(term) > 1]
    if not terms:
        return query_text.strip().replace(" ", " & ") or "''"

    # Prefix-match the last term, exact-match earlier ones
    ts_terms: list[str] = []
    for index, term in enumerate(terms):
        if index == len(terms) - 1:
            ts_terms.append(f"{term}:*")
        else:
            ts_terms.append(term)
    return " & ".join(ts_terms)


def adaptive_alpha(query: str, titles: list[str]) -> float:
    """Return alpha (BM25 weight) adapted to query characteristics.

    If any normalized query term appears in any chunk title, alpha is
    raised (favor BM25 for keyword-heavy queries).  Otherwise alpha is
    lowered (favor vector for natural-language queries).

    Returns a float in [0, 1].
    """
    from packages.common.settings import get_settings

    settings = get_settings()
    query_terms = set(WORD_RE.findall(query.lower()))
    if not query_terms:
        return settings.runbook_hybrid_alpha_nl

    title_terms: set[str] = set()
    for title in titles:
        title_terms.update(WORD_RE.findall(title.lower()))

    if query_terms & title_terms:
        return settings.runbook_hybrid_alpha_keyword
    return settings.runbook_hybrid_alpha_nl


def normalize_bm25(raw_score: float) -> float:
    """Clamp and normalize a BM25 raw score to [0, 1]."""
    return min(1.0, max(0.0, raw_score))
