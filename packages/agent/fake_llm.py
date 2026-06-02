"""FakeLLM — deterministic offline adapter for tests and demos."""

from __future__ import annotations

import json
from typing import Any, get_origin

from pydantic import BaseModel

from packages.agent.schemas import DiagnosisOutput, PlannedAction

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


class FakeLLM:
    """Deterministic fake LLM keyed by alert_name. No randomness, no network."""

    def invoke(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> str:
        alert_name = self._extract_alert_name(messages)
        content = str(messages)
        if "report" in content.lower():
            return json.dumps(self._report_dict(alert_name))
        if "rank" in content.lower():
            return self._ranked_json(alert_name)
        if "plan" in content.lower() or "action" in content.lower():
            return json.dumps(_ACTIONS_MAP.get(alert_name, _ACTIONS_MAP["High5xxAfterDeploy"]))
        return json.dumps(_DIAGNOSIS_MAP.get(alert_name, _DIAGNOSIS_MAP["High5xxAfterDeploy"]))

    def generate_json(
        self, prompt: str, output_schema: type[BaseModel], *, thinking: bool = False, **kwargs: Any
    ) -> Any:
        alert_name = self._extract_alert_name_from_text(prompt)
        origin = get_origin(output_schema)
        if origin is list:
            actions = _ACTIONS_MAP.get(alert_name, _ACTIONS_MAP["High5xxAfterDeploy"])
            return [PlannedAction(**a) for a in actions]
        if output_schema is DiagnosisOutput:
            data = _DIAGNOSIS_MAP.get(alert_name, _DIAGNOSIS_MAP["High5xxAfterDeploy"])
            return DiagnosisOutput(**data)
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

    def _ranked_json(self, alert_name: str) -> str:
        data = dict(_DIAGNOSIS_MAP.get(alert_name, _DIAGNOSIS_MAP["High5xxAfterDeploy"]))
        hyps = data.get("hypotheses", [])
        for i, h in enumerate(hyps):
            h["rank"] = i + 1
        return json.dumps(data)

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
