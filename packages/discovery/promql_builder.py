"""PromQL Builder — parameterized PromQL from semantic type and labels.

M1 PR 1.5: Five template generators (latency, error_rate, qps, cpu_throttle,
disk_avail), each producing PromQL from injectable service_label, service_name,
and metric_name.
"""

from __future__ import annotations

from collections.abc import Callable

from packages.discovery.models import SemanticType


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_latency_promql(
    metric_name: str,
    service_label: str,
    service_name: str,
    quantile: float = 0.99,
) -> str:
    """histogram_quantile(q, sum(rate(metric[5m])) by (le, service_label))"""
    escaped = _escape_label_value(service_name)
    return (
        f"histogram_quantile({quantile}, "
        f'sum(rate({metric_name}{{{service_label}="{escaped}"}}[5m])) '
        f"by (le, {service_label}))"
    )


def build_error_rate_promql(
    metric_name: str,
    service_label: str,
    service_name: str,
) -> str:
    """Error rate with 5xx filter and clamp_min.

    sum(rate(metric{status=~"5.."}[5m])) / clamp_min(sum(rate(metric[5m])), 1)
    """
    escaped = _escape_label_value(service_name)
    sel = f'{service_label}="{escaped}"'
    return (
        f'sum(rate({metric_name}{{{sel},status=~"5.."}}[5m]))'
        f" / clamp_min(sum(rate({metric_name}{{{sel}}}[5m])), 1)"
    )


def build_qps_promql(
    metric_name: str,
    service_label: str,
    service_name: str,
) -> str:
    """QPS: sum(rate(metric[5m])) by (service_label)"""
    escaped = _escape_label_value(service_name)
    return (
        f"sum(rate({metric_name}{{{service_label}=\"{escaped}\"}}[5m]))"
        f" by ({service_label})"
    )


def build_cpu_throttle_promql(
    metric_name: str,
    service_label: str,
    service_name: str,
) -> str:
    """CPU throttle: rate(cfs_throttled[5m]) / rate(cfs_periods[5m])"""
    escaped = _escape_label_value(service_name)
    sel = f'{service_label}="{escaped}"'
    return (
        f"sum(rate({metric_name}{{{sel}}}[5m])) by ({service_label})"
        f" / clamp_min(sum(rate(container_cpu_cfs_periods_total"
        f"{{{sel}}}[5m])) by ({service_label}), 1)"
    )


def build_disk_avail_promql(
    metric_name: str,
    service_label: str,
    service_name: str,
) -> str:
    """Disk avail: min(metric) / min(node_filesystem_size_bytes)"""
    escaped = _escape_label_value(service_name)
    sel = f'{service_label}="{escaped}"'
    return (
        f"min({metric_name}{{{sel}}}) by ({service_label})"
        f" / clamp_min(min(node_filesystem_size_bytes"
        f"{{{sel}}}) by ({service_label}), 1)"
    )


PROMQL_BUILDERS: dict[SemanticType, Callable[..., str]] = {
    "latency": build_latency_promql,
    "error_rate": build_error_rate_promql,
    "qps": build_qps_promql,
    "cpu_throttle": build_cpu_throttle_promql,
    "disk_avail": build_disk_avail_promql,
}


def build_promql(
    semantic_type: SemanticType,
    metric_name: str,
    service_label: str,
    service_name: str,
    **kwargs: float,
) -> str:
    """Build a PromQL query string for a given semantic type."""
    builder = PROMQL_BUILDERS[semantic_type]
    return builder(
        metric_name=metric_name,
        service_label=service_label,
        service_name=service_name,
        **kwargs,
    )
