"""Unit tests for packages/agent/ core — FakeLLM, guardrails, prompts."""

from __future__ import annotations

from packages.agent.fake_llm import FakeLLM
from packages.agent.guardrails.policy import classify_risk_level
from packages.agent.prompts import DIAGNOSIS_PROMPT_TEMPLATE, SYSTEM_PROMPT
from packages.agent.schemas import DiagnosisOutput, PlannedAction


class TestFakeLLM:
    def test_deterministic(self) -> None:
        llm = FakeLLM()
        a = llm.invoke([{"role": "user", "content": "DatabaseConnectionExhaustion diagnosis"}])
        b = llm.invoke([{"role": "user", "content": "DatabaseConnectionExhaustion diagnosis"}])
        assert a == b

    def test_diagnosis_all_alert_types(self) -> None:
        llm = FakeLLM()
        for alert in (
            "DatabaseConnectionExhaustion",
            "High5xxAfterDeploy",
            "RedisCacheAvalanche",
            "PodRestartLoop",
        ):
            output = llm.generate_json(f"diagnose {alert}", DiagnosisOutput)
            assert isinstance(output, DiagnosisOutput)
            assert len(output.hypotheses) >= 1

    def test_actions_all_alert_types(self) -> None:
        llm = FakeLLM()
        for alert in (
            "DatabaseConnectionExhaustion",
            "High5xxAfterDeploy",
            "RedisCacheAvalanche",
            "PodRestartLoop",
        ):
            actions = llm.generate_json(f"plan {alert}", list[PlannedAction])
            assert len(actions) >= 1
            assert actions[0].type

    def test_unknown_alert_fallback(self) -> None:
        llm = FakeLLM()
        output = llm.generate_json("diagnose UnknownAlert", DiagnosisOutput)
        assert isinstance(output, DiagnosisOutput)
        assert len(output.hypotheses) >= 1


class TestGuardrailPolicy:
    def test_l0_read_only(self) -> None:
        d = classify_risk_level({"type": "query_metrics", "target": "", "params": {}})
        assert d.risk_level == "L0"
        assert not d.requires_approval

    def test_l2_requires_approval(self) -> None:
        d = classify_risk_level({"type": "restart_pod", "target": "checkout", "params": {}})
        assert d.risk_level == "L2"
        assert d.requires_approval

    def test_l3_requires_approval(self) -> None:
        d = classify_risk_level({"type": "rollback_release", "target": "checkout", "params": {}})
        assert d.risk_level == "L3"
        assert d.requires_approval

    def test_l4_direct_reject(self) -> None:
        for atype in ("delete_data", "truncate_table", "flush_cache", "modify_database"):
            d = classify_risk_level({"type": atype, "target": "", "params": {}})
            assert d.risk_level == "L4"
            assert not d.allowed

    def test_forbidden_keywords_escalate(self) -> None:
        d = classify_risk_level({"type": "restart_pod", "target": "delete_all", "params": {}})
        assert d.risk_level == "L4"

    def test_unknown_defaults_l2(self) -> None:
        d = classify_risk_level({"type": "new_action", "target": "", "params": {}})
        assert d.risk_level == "L2"


class TestPrompts:
    def test_system_prompt_stable(self) -> None:
        assert "SRE Incident Response Agent" in SYSTEM_PROMPT

    def test_diagnosis_template_placeholders(self) -> None:
        for ph in ("{service_name}", "{alert_name}", "{evidence_block}"):
            assert ph in DIAGNOSIS_PROMPT_TEMPLATE
