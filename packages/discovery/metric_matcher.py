"""MetricMatcher — core matching engine for Prometheus metric discovery.

M1 PR 1.3: Matches raw Prometheus metric names against SEMANTIC_PATTERNS
to produce MetricMapping results. Uses priority-ordered fallback strategy.
"""

from __future__ import annotations

from packages.discovery.models import (
    SEMANTIC_PATTERNS,
    DiscoveryCostControl,
    MetricCandidate,
    MetricMapping,
    SemanticType,
)
from packages.discovery.prom_discovery import PrometheusClient


class MetricMatcher:
    """Matches Prometheus metric names against semantic pattern templates."""

    def __init__(
        self,
        client: PrometheusClient,
        cost_control: DiscoveryCostControl | None = None,
    ) -> None:
        self._client = client
        self._cost = cost_control or DiscoveryCostControl()

    def match(self, metric_names: list[str]) -> dict[SemanticType, MetricMapping]:
        """Match all semantic types against the given metric name list."""
        results: dict[SemanticType, MetricMapping] = {}
        for stype, candidates in SEMANTIC_PATTERNS.items():
            results[stype] = self._match_single(stype, candidates, metric_names)
        return results

    def _match_single(
        self,
        semantic_type: SemanticType,
        candidates: list[MetricCandidate],
        metric_names: list[str],
    ) -> MetricMapping:
        candidates_tried = 0
        for candidate in sorted(candidates, key=lambda c: c.priority):
            candidates_tried += 1
            matched = [n for n in metric_names if candidate.matches(n)]
            if not matched:
                continue
            best_metric = sorted(matched, key=len)[0]

            try:
                label_ok = self._validate_labels(best_metric, candidate)
            except Exception:
                continue
            if not label_ok:
                continue

            if not self._validate_metadata(best_metric, candidate):
                continue

            return MetricMapping(
                semantic_type=semantic_type,
                metric_name=best_metric,
                promql_template=candidate.promql_template,
                status="available",
                confidence=self._calc_confidence(candidates_tried, candidate, True),
                evidence={"candidates_tried": candidates_tried},
            )

        return MetricMapping(
            semantic_type=semantic_type,
            status="unavailable",
            confidence=0.0,
            degraded_reason=f"No candidate matched (tried {candidates_tried})",
        )

    def _validate_labels(
        self, metric_name: str, candidate: MetricCandidate
    ) -> bool:
        if not candidate.required_any_labels:
            return True
        try:
            series = self._client.list_series(f'{{__name__="{metric_name}"}}')
        except Exception:
            return False
        if not series:
            return True
        all_labels: set[str] = set()
        for s in series:
            all_labels.update(s.keys())
        return any(lbl in all_labels for lbl in candidate.required_any_labels)

    def _validate_metadata(
        self, metric_name: str, candidate: MetricCandidate
    ) -> bool:
        try:
            meta = self._client.get_metadata(metric_name)
        except Exception:
            return True
        if not meta:
            return True
        mtype = meta.get("type", "")
        if candidate.semantic_type == "latency" and mtype and mtype != "histogram":
            return False
        if candidate.semantic_type in ("error_rate", "qps"):
            if mtype and mtype not in ("counter", "untyped"):
                return False
        if mtype == "gauge" and candidate.semantic_type in ("error_rate", "qps"):
            return False
        return True

    @staticmethod
    def _calc_confidence(
        tried: int, candidate: MetricCandidate, metadata_ok: bool
    ) -> float:
        base = 0.9 if candidate.priority <= 1 else 0.7
        penalty = (tried - 1) * 0.1
        conf = max(0.3, base - penalty)
        if metadata_ok:
            conf = min(1.0, conf + 0.05)
        return conf
