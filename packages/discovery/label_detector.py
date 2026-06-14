"""Service label detector with cross-validation between K8s and metrics.

M2 PR 2.2: Detects the best service label key across K8s resource labels
and metrics labels, with cross-validation to increase confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_CANDIDATE_LABEL_KEYS = [
    "app",
    "app.kubernetes.io/name",
    "service",
    "job",
    "component",
    "deployment",
    "k8s-app",
    "name",
]


@dataclass
class LabelConvention:
    """Detected service label convention."""
    service_label_key: str | None = None
    confidence: float = 0.0
    coverage: float = 0.0
    alternatives: list[str] = field(default_factory=list)
    evidence: dict[str, float] = field(default_factory=dict)
    requires_review: bool = True


def detect_k8s_service_label(
    k8s_labels: list[dict[str, str]],
    metrics_service_label: str | None = None,
    metrics_label_coverage: float = 0.0,
) -> LabelConvention:
    """Detect the best service label key from K8s resource labels.

    Cross-validates with metrics label detection to increase confidence.
    """
    if not k8s_labels:
        return LabelConvention(requires_review=True)

    scores: dict[str, float] = {}
    for key in _CANDIDATE_LABEL_KEYS:
        count = sum(1 for lbls in k8s_labels if key in lbls)
        scores[key] = count / len(k8s_labels)

    best_key = max(scores, key=scores.get)  # type: ignore[arg-type]
    best_coverage = scores[best_key]
    if best_coverage == 0.0:
        return LabelConvention(
            service_label_key=None,
            confidence=0.0,
            coverage=0.0,
            evidence=scores,
            requires_review=True,
        )

    alternatives = sorted(
        [k for k in scores if k != best_key and scores[k] > 0.1],
        key=lambda k: -scores[k],
    )

    cross_validated = (
        metrics_service_label is not None
        and best_key == metrics_service_label
        and metrics_label_coverage >= 0.7
    )

    if cross_validated:
        confidence = min(0.95, (best_coverage + metrics_label_coverage) / 2 + 0.1)
        requires_review = False
    elif best_coverage >= 0.8:
        confidence = 0.7
        requires_review = False
    else:
        confidence = best_coverage
        requires_review = True

    return LabelConvention(
        service_label_key=best_key,
        confidence=confidence,
        coverage=best_coverage,
        alternatives=alternatives[:3],
        evidence=scores,
        requires_review=requires_review,
    )
