"""FakeLLM — deterministic offline adapter for tests and demos."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, get_origin

from pydantic import BaseModel

from packages.agent.schemas import DiagnosisOutput, PlannedAction

# Compact builders for the Phase 2.4 fault-catalog expansion. They produce the
# same dict shape as the explicit MVP entries below, just without the
# boilerplate (a hypothesis is (statement, confidence, rank_explanation)).
_Hyp = tuple[str, float, str]
_EVIDENCE_ID_RE = re.compile(r"\b(?:evi|evd)_[A-Za-z0-9_-]+")


def _diag(
    summary: str, confidence: float, h1: _Hyp, h2: _Hyp, missing: list[str]
) -> dict[str, Any]:
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
                "rank_explanation": "DB connections near max with elevated query latency",
            },
            {
                "id": "h2",
                "statement": "Connection leak in application code",
                "supporting_evidence_ids": [],
                "confidence": 0.60,
                "rank_explanation": "Connection count rising without traffic increase",
            },
        ],
        "root_cause": {
            "summary": "DB connection pool exhausted — max connections reached",
            "confidence": 0.88,
            "evidence_ids": [],
        },
        "evidence_ids": [],
        "missing_evidence": ["stack traces", "pool config", "schema changes"],
    },
    "High5xxAfterDeploy": {
        "hypotheses": [
            {
                "id": "h1",
                "statement": "Deployment regression causing 5xx errors",
                "supporting_evidence_ids": [],
                "confidence": 0.85,
                "rank_explanation": "5xx spike started within 3 min of deploy",
            },
            {
                "id": "h2",
                "statement": "Downstream dependency API incompatibility",
                "supporting_evidence_ids": [],
                "confidence": 0.55,
                "rank_explanation": "Error logs show downstream call failures",
            },
        ],
        "root_cause": {
            "summary": "Deploy introduced validation bug causing 5xx on checkout",
            "confidence": 0.85,
            "evidence_ids": [],
        },
        "evidence_ids": [],
        "missing_evidence": ["deployment diff", "smoke test results"],
    },
    "RedisCacheAvalanche": {
        "hypotheses": [
            {
                "id": "h1",
                "statement": "Synchronized TTL expiry flooded database",
                "supporting_evidence_ids": [],
                "confidence": 0.82,
                "rank_explanation": "Cache hit rate 12%, DB QPS 4x spike",
            },
            {
                "id": "h2",
                "statement": "Hot key thundering herd under traffic",
                "supporting_evidence_ids": [],
                "confidence": 0.70,
                "rank_explanation": "Single key miss pattern correlated with DB load",
            },
        ],
        "root_cause": {
            "summary": "Redis cache avalanche — synchronized TTL on hot keys",
            "confidence": 0.82,
            "evidence_ids": [],
        },
        "evidence_ids": [],
        "missing_evidence": ["TTL distribution", "hot key patterns"],
    },
    "PodRestartLoop": {
        "hypotheses": [
            {
                "id": "h1",
                "statement": "OOMKilled — memory limit too low",
                "supporting_evidence_ids": [],
                "confidence": 0.90,
                "rank_explanation": "OOMKilled events; memory at limit before restart",
            },
            {
                "id": "h2",
                "statement": "Startup probe failure after config change",
                "supporting_evidence_ids": [],
                "confidence": 0.45,
                "rank_explanation": "Restarts align with configmap update",
            },
        ],
        "root_cause": {
            "summary": "CrashLoopBackOff — OOMKilled, memory limit too low",
            "confidence": 0.90,
            "evidence_ids": [],
        },
        "evidence_ids": [],
        "missing_evidence": ["memory profile", "HPA events"],
    },
}

_ACTIONS_MAP: dict[str, list[dict[str, Any]]] = {
    "DatabaseConnectionExhaustion": [
        {
            "type": "adjust_connection_pool",
            "target": "checkout-db",
            "params": {"pool_size": 50},
            "reason": "Increase DB pool size",
            "risk_hint": "L1",
            "rollback_plan": "Revert pool to original size",
        },
        {
            "type": "create_ticket",
            "target": "dba-team",
            "params": {"priority": "P1"},
            "reason": "Investigate connection leak",
            "risk_hint": "L1",
            "rollback_plan": "",
        },
    ],
    "High5xxAfterDeploy": [
        {
            "type": "rollback_release",
            "target": "checkout",
            "params": {"from_version": "v2026.05.31-1", "to_version": "v2026.05.30-3"},
            "reason": "5xx spike correlated with deploy",
            "risk_hint": "L3",
            "rollback_plan": "Re-deploy after fix validated in staging",
        },
        {
            "type": "create_ticket",
            "target": "dev-team",
            "params": {"priority": "P1"},
            "reason": "Fix 5xx regression",
            "risk_hint": "L1",
            "rollback_plan": "",
        },
    ],
    "RedisCacheAvalanche": [
        {
            "type": "warmup_cache",
            "target": "redis-checkout",
            "params": {"keys": ["product:*", "price:*"], "ttl_stagger": True},
            "reason": "Rebuild cache with staggered TTL",
            "risk_hint": "L1",
            "rollback_plan": "Clear warmed keys if data incorrect",
        },
        {
            "type": "enable_rate_limit",
            "target": "checkout",
            "params": {"max_qps": 500},
            "reason": "Protect DB during rebuild",
            "risk_hint": "L3",
            "rollback_plan": "Remove limit when cache > 80%",
        },
    ],
    "PodRestartLoop": [
        {
            "type": "scale_deployment",
            "target": "checkout",
            "params": {"memory_limit": "512Mi"},
            "reason": "Increase memory limit",
            "risk_hint": "L2",
            "rollback_plan": "Revert to original limits",
        },
        {
            "type": "restart_pod",
            "target": "checkout",
            "params": {},
            "reason": "Restart with new limits",
            "risk_hint": "L2",
            "rollback_plan": "Old config in version control",
        },
    ],
}

# Phase 2.4: expand from 4 MVP faults to 15+. New entries use the compact
# builders. Risk levels stay within MVP scope (no L4; rollback/rate-limit = L3).
_DIAGNOSIS_MAP.update(
    {
        "CPUThrottling": _diag(
            "CPU throttled — container limit too low for load",
            0.84,
            ("CFS throttling from a tight CPU limit", 0.84, "throttled ratio high vs CPU request"),
            ("Noisy neighbor on the node", 0.40, "node CPU saturated across pods"),
            ["request/limit config", "HPA history"],
        ),
        "MemoryLeak": _diag(
            "Memory leak — working set grows until OOMKilled",
            0.86,
            ("Unbounded growth in app heap", 0.86, "memory_working_set rises monotonically"),
            ("Cache without eviction", 0.52, "growth correlates with cache size"),
            ["heap profile", "object retention graph"],
        ),
        "DiskFull": _diag(
            "Disk near full — log rotation or PVC undersized",
            0.83,
            ("Log files not rotating", 0.83, "node_filesystem_avail trending to zero"),
            ("PVC too small for retained data", 0.55, "growth steady with data volume"),
            ["log rotation config", "PVC size"],
        ),
        "CertificateExpiry": _diag(
            "TLS certificate expiring — renewal pipeline stalled",
            0.91,
            ("Cert expiry within threshold", 0.91, "tls_cert_expiry below N days"),
            ("ACME renewal job failing", 0.60, "no recent renewal events"),
            ["cert-manager logs", "renewal schedule"],
        ),
        "DNSFailure": _diag(
            "DNS resolution failures — CoreDNS dropping queries",
            0.80,
            ("CoreDNS overloaded/packet loss", 0.80, "dns error rate elevated"),
            ("Upstream resolver flapping", 0.50, "intermittent NXDOMAIN spikes"),
            ["CoreDNS metrics", "node conntrack table"],
        ),
        "MessageQueueLag": _diag(
            "Consumer lag building — throughput below ingest rate",
            0.82,
            ("Consumers under-provisioned", 0.82, "consumer lag growing steadily"),
            ("Poison message stalling partition", 0.48, "single partition lag spikes"),
            ["consumer group config", "partition lag breakdown"],
        ),
        "RateLimitTriggered": _diag(
            "Rate limiting firing — policy too aggressive or traffic surge",
            0.78,
            ("Limit threshold too low", 0.78, "rate_limit_hits high under normal QPS"),
            ("Legitimate traffic surge", 0.55, "hits track a real QPS spike"),
            ["rate limit policy", "client breakdown"],
        ),
        "SlowAPI": _diag(
            "P95 latency spike — downstream span is the bottleneck",
            0.81,
            ("Slow downstream dependency", 0.81, "trace p95 dominated by one span"),
            ("Lock contention in handler", 0.50, "latency rises with concurrency"),
            ["flame graph", "db lock waits"],
        ),
        "ErrorBudgetBurn": _diag(
            "SLO error budget burning fast — sustained elevated errors",
            0.85,
            ("High burn rate from 5xx", 0.85, "burn rate well above 1x"),
            ("Bad deploy eroding budget", 0.58, "burn started after release"),
            ["SLO config", "recent deploys"],
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
)

_ACTIONS_MAP.update(
    {
        "CPUThrottling": [
            _action(
                "scale_deployment",
                "checkout",
                {"cpu_limit": "1000m"},
                "Raise CPU limit",
                "L2",
                "Revert to original CPU limit",
            ),
            _action(
                "create_ticket", "platform-team", {"priority": "P2"}, "Right-size requests", "L1"
            ),
        ],
        "MemoryLeak": [
            _action(
                "restart_pod",
                "checkout",
                {},
                "Reclaim leaked memory",
                "L2",
                "No state lost; rolling restart",
            ),
            _action("create_ticket", "dev-team", {"priority": "P1"}, "Find and fix leak", "L1"),
        ],
        "DiskFull": [
            _action(
                "create_ticket",
                "platform-team",
                {"priority": "P1"},
                "Expand PVC / fix rotation",
                "L1",
            ),
            _action(
                "adjust_connection_pool",
                "log-shipper",
                {"retention_days": 3},
                "Shorten log retention",
                "L1",
                "Restore prior retention",
            ),
        ],
        "CertificateExpiry": [
            _action(
                "create_ticket", "security-team", {"priority": "P1"}, "Renew certificate", "L1"
            ),
        ],
        "DNSFailure": [
            _action(
                "scale_deployment",
                "coredns",
                {"replicas": 4},
                "Scale CoreDNS",
                "L2",
                "Revert replica count",
            ),
            _action(
                "create_ticket", "platform-team", {"priority": "P1"}, "Investigate DNS loss", "L1"
            ),
        ],
        "MessageQueueLag": [
            _action(
                "scale_deployment",
                "consumer",
                {"replicas": 6},
                "Add consumers to drain lag",
                "L2",
                "Scale back after lag clears",
            ),
            _action(
                "create_ticket", "data-team", {"priority": "P2"}, "Check poison messages", "L1"
            ),
        ],
        "RateLimitTriggered": [
            _action(
                "enable_rate_limit",
                "checkout",
                {"max_qps": 800},
                "Loosen limit to fit traffic",
                "L3",
                "Restore prior limit",
            ),
            _action("create_ticket", "api-team", {"priority": "P2"}, "Review limit policy", "L1"),
        ],
        "SlowAPI": [
            _action("create_ticket", "dev-team", {"priority": "P1"}, "Optimize slow span", "L1"),
        ],
        "ErrorBudgetBurn": [
            _action(
                "rollback_release",
                "checkout",
                {"from_version": "v2026.06.01-2", "to_version": "v2026.06.01-1"},
                "Stop budget burn from bad deploy",
                "L3",
                "Re-deploy after fix in staging",
            ),
            _action("create_ticket", "sre-team", {"priority": "P1"}, "Review SLO and burn", "L1"),
        ],
        "P0SiteOutage": [
            _action(
                "create_ticket",
                "incident-commander",
                {"priority": "P0"},
                "Page on-call, open bridge",
                "L1",
            ),
            _action(
                "enable_rate_limit",
                "edge",
                {"max_qps": 200},
                "Shed load to protect recovery",
                "L3",
                "Remove limit once services healthy",
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
)


class FakeLLM:
    """Deterministic fake LLM keyed by alert_name. No randomness, no network."""

    def invoke(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> str:
        alert_name = self._extract_alert_name(messages)
        content = str(messages)
        evidence_ids = self._extract_evidence_ids_from_text(content)
        if "report" in content.lower():
            return json.dumps(self._report_dict(alert_name))
        if "rank" in content.lower():
            return self._ranked_json(alert_name, evidence_ids)
        if "plan" in content.lower() or "action" in content.lower():
            return json.dumps(_ACTIONS_MAP.get(alert_name, _ACTIONS_MAP["High5xxAfterDeploy"]))
        return json.dumps(self._diagnosis_dict(alert_name, evidence_ids))

    def generate_json(
        self, prompt: str, output_schema: type[BaseModel], *, thinking: bool = False, **kwargs: Any
    ) -> Any:
        alert_name = self._extract_alert_name_from_text(prompt)
        origin = get_origin(output_schema)
        if origin is list:
            actions = _ACTIONS_MAP.get(alert_name, _ACTIONS_MAP["High5xxAfterDeploy"])
            return [PlannedAction(**a) for a in actions]
        if output_schema is DiagnosisOutput:
            evidence_ids = self._extract_evidence_ids_from_text(prompt)
            return DiagnosisOutput(**self._diagnosis_dict(alert_name, evidence_ids))
        return output_schema()

    def _report_dict(self, alert_name: str) -> dict[str, Any]:
        diag = _DIAGNOSIS_MAP.get(alert_name, _DIAGNOSIS_MAP["High5xxAfterDeploy"])
        rc = diag.get("root_cause", {}).get("summary", "unknown")
        return {
            "root_cause": rc,
            "impact": "Service affected for approximately 12 minutes",
            "timeline": [
                {"time": "T+0m", "event": "Alert fired"},
                {"time": "T+5m", "event": "Diagnosis complete"},
            ],
            "actions": _ACTIONS_MAP.get(alert_name, []),
            "follow_ups": ["Review monitoring thresholds", "Update runbook with findings"],
        }

    def _ranked_json(self, alert_name: str, evidence_ids: list[str] | None = None) -> str:
        data = self._diagnosis_dict(alert_name, evidence_ids or [])
        hyps = data.get("hypotheses", [])
        for i, h in enumerate(hyps):
            h["rank"] = i + 1
        return json.dumps(data)

    @staticmethod
    def _diagnosis_dict(alert_name: str, evidence_ids: list[str] | None = None) -> dict[str, Any]:
        data = deepcopy(_DIAGNOSIS_MAP.get(alert_name, _DIAGNOSIS_MAP["High5xxAfterDeploy"]))
        ids = list(evidence_ids or [])
        if ids:
            data["evidence_ids"] = ids
            data.setdefault("root_cause", {})["evidence_ids"] = ids
            for hypothesis in data.get("hypotheses", []) or []:
                if not hypothesis.get("supporting_evidence_ids"):
                    hypothesis["supporting_evidence_ids"] = ids
        return data

    @staticmethod
    def _extract_alert_name(messages: list[dict[str, Any]]) -> str:
        for msg in messages:
            name = FakeLLM._extract_alert_name_from_text(str(msg.get("content", "")))
            if name != "High5xxAfterDeploy":
                return name
        return "High5xxAfterDeploy"

    @staticmethod
    def _extract_alert_name_from_text(text: str) -> str:
        for name in _DIAGNOSIS_MAP:
            if name in text:
                return name
        return "High5xxAfterDeploy"

    @staticmethod
    def _extract_evidence_ids_from_text(text: str) -> list[str]:
        ids: list[str] = []
        for match in _EVIDENCE_ID_RE.findall(text):
            if match not in ids:
                ids.append(match)
        return ids
