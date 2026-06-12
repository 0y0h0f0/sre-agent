"""Deterministic Runbook Template Engine — Jinja2 rendering driven by discovery capability matrix.

Phase 0-8: no LLM, no web_search. Templates use discovery results to show/hide
diagnostic steps based on what observability signals are actually available.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, Template

from packages.discovery.models import CapabilityMatrix, DiscoveryResult, MetricMapping

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

INCIDENT_TYPE_TO_TEMPLATE: dict[str, str] = {
    "high_latency": "high_latency.md.j2",
    "high_error_rate": "high_error_rate.md.j2",
    "resource_saturation": "resource_saturation.md.j2",
    "dependency_failure": "dependency_failure.md.j2",
}

DEFAULT_TEMPLATE = "generic_incident.md.j2"


class RunbookTemplateContext:
    """Structured input for template rendering — built from discovery data.

    This separates the template's view of the world from the raw DiscoveryResult,
    so templates never reference internal model field names directly.
    """

    def __init__(
        self,
        *,
        service_name: str,
        incident_type: str,
        title: str,
        severity: str,
        owner: str,
        capability: CapabilityMatrix | None = None,
        metric_mappings: list[MetricMapping] | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> None:
        self.service_name = service_name
        self.incident_type = incident_type
        self.title = title
        self.severity = severity
        self.owner = owner
        self.today = date.today().isoformat()

        # Capability flags
        self.metrics_available = capability.metrics_available if capability else False
        self.logs_available = capability.logs_available if capability else False
        self.traces_available = capability.traces_available if capability else False
        self.k8s_accessible = capability.k8s_accessible if capability else False
        self.capability_gaps: list[str] = list(capability.capability_gaps) if capability else []

        # Metric mappings for PromQL snippets
        mmaps = metric_mappings or (capability.metric_mappings if capability else [])
        self.metric_by_type: dict[str, MetricMapping] = {m.semantic_type: m for m in mmaps}

        # Extra user-provided context (e.g. from incident cluster analysis)
        self.extra = extra_context or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_name": self.service_name,
            "incident_type": self.incident_type,
            "title": self.title,
            "severity": self.severity,
            "owner": self.owner,
            "today": self.today,
            "metrics_available": self.metrics_available,
            "logs_available": self.logs_available,
            "traces_available": self.traces_available,
            "k8s_accessible": self.k8s_accessible,
            "capability_gaps": self.capability_gaps,
            "metric_by_type": {
                k: v.model_dump() for k, v in self.metric_by_type.items()
            },
            "has_latency_metric": "latency" in self.metric_by_type,
            "has_error_rate_metric": "error_rate" in self.metric_by_type,
            "has_qps_metric": "qps" in self.metric_by_type,
            "has_cpu_metric": "cpu_throttle" in self.metric_by_type,
            "has_disk_metric": "disk_avail" in self.metric_by_type,
            **self.extra,
        }


class RunbookTemplateEngine:
    """Deterministic runbook template renderer.

    Uses Jinja2 templates driven by discovery capability matrix.
    No LLM, no web_search — purely deterministic.
    """

    def __init__(self, templates_dir: str | Path | None = None) -> None:
        loader = FileSystemLoader(str(templates_dir or _TEMPLATES_DIR))
        self._env = Environment(loader=loader, autoescape=False)

    def render(
        self,
        context: RunbookTemplateContext,
        *,
        template_name: str | None = None,
    ) -> str:
        """Render a runbook from the best-matching template.

        Args:
            context: Structured context built from discovery data.
            template_name: Optional explicit template override.
                If None, auto-selects based on incident_type.

        Returns:
            Rendered Markdown string (with YAML front matter).
        """
        if template_name is None:
            template_name = INCIDENT_TYPE_TO_TEMPLATE.get(
                context.incident_type, DEFAULT_TEMPLATE
            )

        try:
            template = self._env.get_template(template_name)
        except Exception:
            logger.warning(
                "Template '%s' not found, falling back to %s",
                template_name,
                DEFAULT_TEMPLATE,
            )
            template = self._env.get_template(DEFAULT_TEMPLATE)

        return template.render(context.to_dict())

    def render_for_service(
        self,
        *,
        discovery: DiscoveryResult,
        service_name: str,
        incident_type: str,
        title: str | None = None,
        severity: str = "P2",
        owner: str = "agent",
        extra_context: dict[str, Any] | None = None,
    ) -> str:
        """Convenience: render a runbook for a specific service from discovery data.

        Finds the matching CapabilityMatrix in the discovery result for the service.
        """
        capability = None
        for cm in discovery.capability_matrix:
            if cm.service_name == service_name:
                capability = cm
                break

        metric_mappings = list(discovery.metric_mappings)

        return self.render(
            RunbookTemplateContext(
                service_name=service_name,
                incident_type=incident_type,
                title=title or f"{incident_type.replace('_', ' ').title()} Runbook",
                severity=severity,
                owner=owner,
                capability=capability,
                metric_mappings=metric_mappings,
                extra_context=extra_context,
            )
        )

    @staticmethod
    def list_templates() -> list[str]:
        """List available template names in the templates directory."""
        if not _TEMPLATES_DIR.is_dir():
            return []
        return sorted(
            p.name for p in _TEMPLATES_DIR.iterdir()
            if p.suffix == ".j2" or p.name.endswith(".md.j2")
        )

    def get_template(self, name: str) -> Template:
        """Access a raw Jinja2 template by name."""
        return self._env.get_template(name)
