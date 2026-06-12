"""FakeLLM — deterministic offline adapter for tests and demos."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, get_origin

from pydantic import BaseModel

from packages.agent.rules_fallback import (
    _ACTIONS_MAP,
    _DIAGNOSIS_MAP,
    _diag,
)
from packages.agent.schemas import DiagnosisOutput, PlannedAction

_EVIDENCE_ID_RE = re.compile(r"\b(?:evi|evd)_[A-Za-z0-9_-]+")
_PERSPECTIVE_RE = re.compile(r"\[perspective:(metrics|logs|traces|synthesizer)\]")

# Re-export for backward-compatible imports by tests
_Hyp = tuple[str, float, str]

# ---- Phase 2: perspective-specific specialist diagnoses ----
# Each entry is a partial DiagnosisOutput focusing on one evidence type.
# Entries not listed below are auto-generated from _DIAGNOSIS_MAP at import time.

_PERSPECTIVE_DIAGNOSIS_MAP: dict[tuple[str, str], dict[str, Any]] = {
    ("DatabaseConnectionExhaustion", "metrics"): _diag(
        "Elevated DB connection count and query latency", 0.80,
        ("Connection pool near capacity", 0.80, "active_connections > 80% max"),
        ("Query latency spike from slow queries", 0.65, "p99 query time 4x baseline"),
        ["log samples", "trace spans", "pool config"],
    ),
    ("DatabaseConnectionExhaustion", "logs"): _diag(
        "Connection timeout errors in application logs", 0.75,
        ("Connection acquisition timeout", 0.75, "timeout errors correlated with pool"),
        ("Idle-in-transaction connections blocking", 0.50, "idle-in-transaction observed"),
        ["pool metrics", "lock status", "trace spans"],
    ),
    ("DatabaseConnectionExhaustion", "traces"): _diag(
        "DB span dominates trace latency", 0.78,
        ("Slow queries identified in traces", 0.78, "DB span > 200ms in 90% of traces"),
        ("Connection acquisition wait visible", 0.55, "acquire_conn span elevated"),
        ["pool metrics", "slow query logs"],
    ),
    ("High5xxAfterDeploy", "metrics"): _diag(
        "5xx error rate spike post-deployment", 0.83,
        ("HTTP 5xx rate elevated", 0.83, "5xx rate > 5% starting at deploy time"),
        ("Latency increase on affected endpoints", 0.60, "p95 latency 3x baseline"),
        ["error logs", "stack traces", "deployment diff"],
    ),
    ("High5xxAfterDeploy", "logs"): _diag(
        "Validation errors and downstream call failures", 0.80,
        ("NullPointerException in request handler", 0.80, "NPE logged at rate 50/min"),
        ("Downstream API returning 400", 0.55, "downstream 400 errors in logs"),
        ["metrics", "trace details", "deployment diff"],
    ),
    ("High5xxAfterDeploy", "traces"): _diag(
        "Error spans concentrated in checkout handler", 0.82,
        ("checkout span failing with status=error", 0.82, "100% error in checkout span"),
        ("Upstream propagation of errors", 0.50, "errors propagate to edge gateway span"),
        ["metrics", "error logs"],
    ),
    ("RedisCacheAvalanche", "metrics"): _diag(
        "Cache hit rate collapsed, DB QPS surged", 0.80,
        ("Cache hit rate dropped to 12%", 0.80, "cache_hit_ratio trending to 0.12"),
        ("DB query rate 4x normal", 0.70, "db_qps spike correlates with cache miss"),
        ["TTL distribution", "hot key list", "cache logs"],
    ),
    ("RedisCacheAvalanche", "logs"): _diag(
        "Cache miss storms and DB timeout errors", 0.72,
        ("Cache miss logged at high rate", 0.72, "cache_miss log rate >> cache_hit"),
        ("DB timeout due to overload", 0.55, "db query timeout logged after cache miss"),
        ["metrics", "TTL config", "key patterns"],
    ),
    ("RedisCacheAvalanche", "traces"): _diag(
        "Cache fetch spans missing, DB spans dominate", 0.76,
        ("cache_get span empty, fallback to DB", 0.76, "cache_get returning empty"),
        ("DB span time increased", 0.60, "db_query span p99 > 500ms"),
        ["metrics", "cache config"],
    ),
    ("PodRestartLoop", "metrics"): _diag(
        "Memory usage at limit, pod restarts detected", 0.88,
        ("Memory usage near limit", 0.88, "container_memory_working_set_bytes near limit"),
        ("Restart count increasing", 0.82, "kube_pod_container_status_restarts climbing"),
        ["k8s events", "memory profile", "HPA history"],
    ),
    ("PodRestartLoop", "logs"): _diag(
        "OOMKilled and CrashLoopBackOff events", 0.85,
        ("OOMKilled logged by kubelet", 0.85, "OOMKilled event for container"),
        ("Startup probe failing", 0.40, "startup probe timeout in events"),
        ["k8s events", "memory metrics"],
    ),
    ("PodRestartLoop", "traces"): _diag(
        "Trace spans show increasing latency before restart", 0.65,
        ("GC pause time increasing before OOM", 0.65, "gc spans growing before restart"),
        ("Request latency chain from memory pressure", 0.50, "request spans show latency ramp"),
        ["memory metrics", "heap profile"],
    ),
}

# Fill remaining alert types with auto-generated perspective entries.
for _alert_name in _DIAGNOSIS_MAP:
    for _persp in ("metrics", "logs", "traces"):
        if (_alert_name, _persp) not in _PERSPECTIVE_DIAGNOSIS_MAP:
            full = _DIAGNOSIS_MAP[_alert_name]
            hyps = full.get("hypotheses", [])
            rc = full.get("root_cause", {})
            h1 = hyps[0] if len(hyps) > 0 else None
            h2 = hyps[1] if len(hyps) > 1 else None
            _PERSPECTIVE_DIAGNOSIS_MAP[(_alert_name, _persp)] = _diag(
                rc.get("summary", f"{_alert_name} — {_persp} perspective"),
                min(rc.get("confidence", 0.7) * 0.85, 0.95),
                (h1["statement"] if h1 else f"Primary cause from {_persp}",
                 h1["confidence"] * 0.9 if h1 else 0.6,
                 h1.get("rank_explanation", "") if h1 else ""),
                (h2["statement"] if h2 else f"Secondary cause from {_persp}",
                 h2["confidence"] * 0.8 if h2 else 0.4,
                 h2.get("rank_explanation", "") if h2 else ""),
                ["additional evidence"],
            )


class FakeLLM:
    """Deterministic fake LLM keyed by alert_name. No randomness, no network."""

    def invoke(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> str:
        alert_name = self._extract_alert_name(messages)
        content = str(messages)
        evidence_ids = self._extract_evidence_ids_from_text(content)
        perspective = self._extract_perspective(content)
        if "report" in content.lower():
            return json.dumps(self._report_dict(alert_name))
        if "rank" in content.lower():
            return self._ranked_json(alert_name, evidence_ids)
        if "plan" in content.lower() or "action" in content.lower():
            return json.dumps(_ACTIONS_MAP.get(alert_name, _ACTIONS_MAP["High5xxAfterDeploy"]))
        return json.dumps(self._diagnosis_dict(alert_name, evidence_ids, perspective))

    def generate_json(
        self, prompt: str, output_schema: type[BaseModel], *, thinking: bool = False, **kwargs: Any
    ) -> Any:
        alert_name = self._extract_alert_name_from_text(prompt)
        perspective = self._extract_perspective(prompt)
        origin = get_origin(output_schema)
        if origin is list:
            actions = _ACTIONS_MAP.get(alert_name, _ACTIONS_MAP["High5xxAfterDeploy"])
            return [PlannedAction(**a) for a in actions]
        if output_schema is DiagnosisOutput:
            evidence_ids = self._extract_evidence_ids_from_text(prompt)
            return DiagnosisOutput(**self._diagnosis_dict(alert_name, evidence_ids, perspective))
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
    def _diagnosis_dict(alert_name: str, evidence_ids: list[str] | None = None,
                        perspective: str | None = None) -> dict[str, Any]:
        if perspective and perspective != "synthesizer":
            key = (alert_name, perspective)
            fallback_key = ("High5xxAfterDeploy", perspective)
            data = deepcopy(_PERSPECTIVE_DIAGNOSIS_MAP.get(
                key,
                _PERSPECTIVE_DIAGNOSIS_MAP.get(
                    fallback_key,
                    _DIAGNOSIS_MAP.get(alert_name, _DIAGNOSIS_MAP["High5xxAfterDeploy"]),
                ),
            ))
        else:
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
    def _extract_perspective(text: str) -> str | None:
        """Extract perspective tag from a prompt, e.g. ``[perspective:metrics]``."""
        m = _PERSPECTIVE_RE.search(text)
        return m.group(1) if m else None

    @staticmethod
    def _extract_evidence_ids_from_text(text: str) -> list[str]:
        ids: list[str] = []
        for match in _EVIDENCE_ID_RE.findall(text):
            if match not in ids:
                ids.append(match)
        return ids
