"""Deterministic diagnosis rules shared by production and test code.

The diagnosis and action maps provide a reliable fallback when the LLM is
unavailable.  Both the production :func:`~.nodes.diagnose._rules_diagnose` path
and the test :class:`~.fake_llm.FakeLLM` adapter draw from this single source
of truth so the two paths never diverge.
"""

from __future__ import annotations

from typing import Any

# Hypothesis tuple: (statement, confidence, rank_explanation)
_Hyp = tuple[str, float, str]


def _diag(
    summary: str, confidence: float, h1: _Hyp, h2: _Hyp, missing: list[str]
) -> dict[str, Any]:
    """Build a diagnosis dict with two hypotheses."""
    return {
        "hypotheses": [
            {
                "id": "h1",
                "statement": h1[0],
                "supporting_evidence_ids": [],
                "confidence": h1[1],
                "rank_explanation": h1[2],
            },
            {
                "id": "h2",
                "statement": h2[0],
                "supporting_evidence_ids": [],
                "confidence": h2[1],
                "rank_explanation": h2[2],
            },
        ],
        "root_cause": {"summary": summary, "confidence": confidence, "evidence_ids": []},
        "evidence_ids": [],
        "missing_evidence": missing,
    }


def _action(
    action_type: str,
    target: str,
    params: dict[str, Any],
    reason: str,
    risk_hint: str,
    rollback_plan: str = "",
) -> dict[str, Any]:
    return {
        "type": action_type,
        "target": target,
        "params": params,
        "reason": reason,
        "risk_hint": risk_hint,
        "rollback_plan": rollback_plan,
    }


_DIAGNOSIS_MAP: dict[str, dict[str, Any]] = {
    "DatabaseConnectionExhaustion": {
        "hypotheses": [
            {
                "id": "h1",
                "statement": "Connection pool saturated by slow queries",
                "supporting_evidence_ids": [],
                "confidence": 0.88,
                "rank_explanation": "Metrics show elevated active connections",
            },
            {
                "id": "h2",
                "statement": "Idle-in-transaction connections blocking pool",
                "supporting_evidence_ids": [],
                "confidence": 0.55,
                "rank_explanation": "DB diagnostics show idle-in-transaction state",
            },
        ],
        "root_cause": {
            "summary": (
                "DB connection pool exhausted near max connections by slow query "
                "accumulation"
            ),
            "confidence": 0.88,
            "evidence_ids": [],
        },
        "evidence_ids": [],
        "missing_evidence": ["slow query logs", "connection pool config"],
    },
    "High5xxAfterDeploy": {
        "hypotheses": [
            {
                "id": "h1",
                "statement": "Recent deployment introduced a code regression",
                "supporting_evidence_ids": [],
                "confidence": 0.83,
                "rank_explanation": "Deployment and 5xx spike are temporally correlated",
            },
            {
                "id": "h2",
                "statement": "Downstream dependency degraded after deploy",
                "supporting_evidence_ids": [],
                "confidence": 0.55,
                "rank_explanation": "Downstream error rates also elevated",
            },
        ],
        "root_cause": {
            "summary": "Deployment regression causing 5xx errors from a validation bug",
            "confidence": 0.83,
            "evidence_ids": [],
        },
        "evidence_ids": [],
        "missing_evidence": ["deployment diff", "error stack traces"],
    },
    "RedisCacheAvalanche": {
        "hypotheses": [
            {
                "id": "h1",
                "statement": "Cache miss storm due to key eviction or TTL expiry",
                "supporting_evidence_ids": [],
                "confidence": 0.80,
                "rank_explanation": "Cache hit rate collapsed; DB QPS surged",
            },
            {
                "id": "h2",
                "statement": "Redis instance degraded or network partition",
                "supporting_evidence_ids": [],
                "confidence": 0.60,
                "rank_explanation": "Redis connection errors in logs",
            },
        ],
        "root_cause": {
            "summary": "Synchronized TTL expiry flooded database causing cache avalanche",
            "confidence": 0.80,
            "evidence_ids": [],
        },
        "evidence_ids": [],
        "missing_evidence": ["TTL distribution", "hot key report"],
    },
    "PodRestartLoop": {
        "hypotheses": [
            {
                "id": "h1",
                "statement": "Memory leak causing OOMKilled and restart",
                "supporting_evidence_ids": [],
                "confidence": 0.88,
                "rank_explanation": "Memory metrics trending to limit",
            },
            {
                "id": "h2",
                "statement": "Startup probe timeout due to slow initialization",
                "supporting_evidence_ids": [],
                "confidence": 0.45,
                "rank_explanation": "Startup probe failing after deployment",
            },
        ],
        "root_cause": {
            "summary": "OOMKilled — memory limit too low after memory leak caused pod restart loop",
            "confidence": 0.88,
            "evidence_ids": [],
        },
        "evidence_ids": [],
        "missing_evidence": ["heap profile", "memory limit config"],
    },
    "CPUThrottling": _diag(
        "CPU throttling — resource limits too low for workload spike",
        0.82,
        ("CPU throttling under load", 0.82, "cpu_throttle_seconds increasing"),
        ("Noisy neighbor on same node", 0.45, "node-level CPU contention"),
        ["per-container CPU quota", "node CPU allocation"],
    ),
    "MemoryLeak": _diag(
        "Memory leak in application — heap growing without bound",
        0.85,
        ("Heap memory growing monotonically", 0.85, "memory_working_set increasing"),
        ("Memory fragmentation in allocator", 0.40, "no allocation spike in profile"),
        ["heap dump", "allocation profile"],
    ),
    "DiskFull": _diag(
        "Disk space exhausted on node or PVC",
        0.90,
        ("Disk usage at 100%", 0.90, "disk_avail_bytes trending to zero"),
        ("Log rotation stalled", 0.50, "log files consuming excessive space"),
        ["largest directories", "PVC capacity"],
    ),
    "CertificateExpiry": _diag(
        "TLS certificate expiring — connections failing validation",
        0.92,
        ("Certificate within expiry window", 0.92, "cert_expiry_days below threshold"),
        ("Intermediate CA expired", 0.35, "full chain validation failing"),
        ["certificate chain", "renewal automation status"],
    ),
    "DNSFailure": _diag(
        "DNS resolution failures — upstream names unreachable",
        0.78,
        ("DNS server unreachable or timeout", 0.78, "dns_error_rate elevated"),
        ("DNS cache poisoning", 0.30, "unexpected IPs in resolution logs"),
        ["DNS server health", "resolution latency distribution"],
    ),
    "MessageQueueLag": _diag(
        "Message queue consumer lag — backlog accumulating",
        0.84,
        ("Consumer throughput below produce rate", 0.84, "queue_lag metrics climbing"),
        ("Consumer group rebalancing", 0.55, "consumer group instability in logs"),
        ["consumer scaling config", "message size distribution"],
    ),
    "RateLimitTriggered": _diag(
        "Rate limit triggered — requests being throttled",
        0.86,
        ("Traffic exceeded configured limit", 0.86, "rate_limit_hits rising"),
        ("Downstream propagation of throttling", 0.50, "upstream services also throttled"),
        ["rate limit config", "traffic pattern analysis"],
    ),
    "SlowAPI": _diag(
        "API latency spike — requests timing out at edge",
        0.75,
        ("Upstream dependency slow", 0.75, "p99 latency spike in trace spans"),
        ("GC pause causing request latency", 0.40, "GC pause time in JVM metrics"),
        ["upstream SLO dashboard", "GC log"],
    ),
    "ErrorBudgetBurn": _diag(
        "SLO error budget burning faster than threshold",
        0.88,
        ("Sustained elevated error rate", 0.88, "slo_burn_rate exceeding threshold"),
        ("One-off incident inflating burn rate", 0.50, "single large spike"),
        ["error breakdown by endpoint", "long-term SLO trend"],
    ),
    "P0SiteOutage": _diag(
        "P0 multi-service outage — shared dependency failure",
        0.92,
        ("Shared dependency down", 0.92, "many services alerting together"),
        ("Network partition", 0.60, "cross-AZ errors correlated"),
        ["dependency health", "network topology"],
    ),
    "DownstreamTimeout": _diag(
        "Downstream timeouts — upstream callers retrying and stacking",
        0.79,
        ("Downstream service slow/unavailable", 0.79, "timeout errors to one dependency"),
        ("Retry storm amplifying load", 0.52, "retry budget exhausted in logs"),
        ["downstream SLOs", "retry policy"],
    ),
}

_ACTIONS_MAP: dict[str, list[dict[str, Any]]] = {
    "DatabaseConnectionExhaustion": [
        _action(
            "adjust_connection_pool",
            "database",
            {"max_connections": 200, "idle_timeout_seconds": 30},
            "Tune pool capacity and idle connection recycling",
            "L1",
            "Restore original pool settings",
        ),
        _action(
            "create_ticket",
            "db-team",
            {"priority": "P1"},
            "Investigate slow query accumulation",
            "L1",
        ),
    ],
    "High5xxAfterDeploy": [
        _action(
            "rollback_deployment",
            "checkout",
            {},
            "Rollback to last known good revision",
            "L3",
        ),
        _action(
            "create_ticket",
            "dev-team",
            {"priority": "P1"},
            "Investigate regression root cause",
            "L1",
        ),
    ],
    "RedisCacheAvalanche": [
        _action("scale_cache", "redis", {"replica_count": 3}, "Add read replicas", "L2"),
        _action(
            "enable_circuit_breaker",
            "checkout",
            {"timeout_ms": 200},
            "Prevent DB overload from cache fallback",
            "L3",
        ),
    ],
    "PodRestartLoop": [
        _action(
            "increase_memory_limit",
            "checkout",
            {"memory_limit": "2Gi"},
            "Increase container memory limit",
            "L2",
            "Reduce to original limit",
        ),
        _action(
            "rollback_deployment",
            "checkout",
            {},
            "Revert to known stable version",
            "L3",
        ),
    ],
    "CPUThrottling": [
        _action(
            "scale_deployment",
            "checkout",
            {"replicas": 4},
            "Scale deployment horizontally to reduce per-pod CPU pressure",
            "L2",
            "Scale back to the original replica count",
        ),
        _action("create_ticket", "platform-team", {"priority": "P2"}, "Right-size requests", "L1"),
    ],
    "MemoryLeak": [
        _action(
            "increase_memory_limit",
            "checkout",
            {"memory_limit": "4Gi"},
            "Bump memory limit to buy time",
            "L2",
            "Revert to original limit",
        ),
        _action("create_ticket", "dev-team", {"priority": "P1"}, "Diagnose memory leak", "L1"),
    ],
    "DiskFull": [
        _action("scale_disk", "checkout", {"pvc_size": "20Gi"}, "Expand PVC", "L2"),
        _action("rotate_logs", "checkout", {}, "Force log rotation", "L1"),
    ],
    "CertificateExpiry": [
        _action(
            "renew_certificate",
            "tls",
            {"domain": "*.example.com"},
            "Trigger automated renewal",
            "L1",
        ),
        _action("create_ticket", "security-team", {"priority": "P0"}, "Manual renewal", "L1"),
    ],
    "DNSFailure": [
        _action(
            "switch_dns_resolver",
            "checkout",
            {"nameserver": "8.8.8.8"},
            "Switch to backup resolver",
            "L3",
            "Restore primary resolver",
        ),
        _action("create_ticket", "netops", {"priority": "P1"}, "Investigate DNS", "L1"),
    ],
    "MessageQueueLag": [
        _action(
            "scale_consumers",
            "checkout",
            {"replicas": 4},
            "Increase consumer count",
            "L2",
            "Scale back to normal",
        ),
        _action("create_ticket", "data-team", {"priority": "P2"}, "Check partitioning", "L1"),
    ],
    "RateLimitTriggered": [
        _action(
            "raise_rate_limit",
            "checkout",
            {"max_qps": 2000},
            "Increase rate limit ceiling",
            "L3",
            "Restore original limit",
        ),
        _action("create_ticket", "platform-team", {"priority": "P2"}, "Review policy", "L1"),
    ],
    "SlowAPI": [
        _action(
            "enable_caching",
            "checkout",
            {"ttl_seconds": 60},
            "Enable response caching",
            "L2",
            "Disable cache",
        ),
        _action("create_ticket", "dev-team", {"priority": "P1"}, "Profile slow endpoints", "L1"),
    ],
    "ErrorBudgetBurn": [
        _action(
            "enable_rate_limit",
            "checkout",
            {"max_qps": 500},
            "Throttle traffic to protect SLO",
            "L3",
            "Remove rate limit",
        ),
        _action("create_ticket", "sre-team", {"priority": "P1"}, "Root-cause budget burn", "L1"),
    ],
    "P0SiteOutage": [
        _action(
            "failover",
            "primary",
            {"target": "secondary"},
            "Failover to secondary region",
            "L3",
            "Failback to primary",
        ),
        _action(
            "create_ticket",
            "incident-commander",
            {"priority": "P0"},
            "Declare major incident",
            "L1",
        ),
    ],
    "DownstreamTimeout": [
        _action(
            "enable_rate_limit",
            "checkout",
            {"max_qps": 400},
            "Throttle to stop retry storm",
            "L3",
            "Remove limit when downstream recovers",
        ),
        _action("create_ticket", "dev-team", {"priority": "P1"}, "Tune timeouts/retries", "L1"),
    ],
}
