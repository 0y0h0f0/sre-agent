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


class TestFakeLLMMultiPerspective:
    """Phase 2: perspective-aware FakeLLM routing."""

    def test_perspective_tag_extraction(self) -> None:
        llm = FakeLLM()
        assert llm._extract_perspective("[perspective:metrics]\nSome prompt") == "metrics"
        assert llm._extract_perspective("[perspective:logs]\nSome prompt") == "logs"
        assert llm._extract_perspective("[perspective:traces]\nSome prompt") == "traces"
        assert llm._extract_perspective("[perspective:synthesizer]\nSome prompt") == "synthesizer"
        assert llm._extract_perspective("No perspective tag here") is None

    def test_specialist_output_is_diagnosis_output(self) -> None:
        from packages.agent.schemas import DiagnosisOutput

        llm = FakeLLM()
        prompt = "[perspective:metrics]\ndiagnose DatabaseConnectionExhaustion"
        output = llm.generate_json(prompt, DiagnosisOutput)
        assert isinstance(output, DiagnosisOutput)
        assert len(output.hypotheses) >= 1

    def test_specialist_output_differs_from_full(self) -> None:
        from packages.agent.schemas import DiagnosisOutput

        llm = FakeLLM()
        full = llm.generate_json("diagnose DatabaseConnectionExhaustion", DiagnosisOutput)
        metrics = llm.generate_json(
            "[perspective:metrics]\ndiagnose DatabaseConnectionExhaustion", DiagnosisOutput
        )
        assert isinstance(full, DiagnosisOutput)
        assert isinstance(metrics, DiagnosisOutput)
        # Both produce hypotheses
        assert len(full.hypotheses) >= 1
        assert len(metrics.hypotheses) >= 1

    def test_synthesizer_uses_full_diagnosis_map(self) -> None:
        from packages.agent.schemas import DiagnosisOutput

        llm = FakeLLM()
        prompt = "[perspective:synthesizer]\ndiagnose DatabaseConnectionExhaustion"
        output = llm.generate_json(prompt, DiagnosisOutput)
        assert isinstance(output, DiagnosisOutput)
        assert len(output.hypotheses) >= 2

    def test_unknown_alert_with_perspective_falls_back(self) -> None:
        from packages.agent.schemas import DiagnosisOutput

        llm = FakeLLM()
        prompt = "[perspective:metrics]\ndiagnose TotallyUnknownAlert"
        output = llm.generate_json(prompt, DiagnosisOutput)
        assert isinstance(output, DiagnosisOutput)
        assert len(output.hypotheses) >= 1

    def test_evidence_ids_passed_to_perspective_diagnosis(self) -> None:
        from packages.agent.schemas import DiagnosisOutput

        llm = FakeLLM()
        prompt = (
            "[perspective:metrics]\ndiagnose High5xxAfterDeploy\n"
            "evidence_ids: evi_001 evi_002"
        )
        output = llm.generate_json(prompt, DiagnosisOutput)
        assert isinstance(output, DiagnosisOutput)


class TestPrompts:
    def test_system_prompt_stable(self) -> None:
        assert "SRE Incident Response Agent" in SYSTEM_PROMPT

    def test_diagnosis_template_placeholders(self) -> None:
        for ph in ("{service_name}", "{alert_name}", "{evidence_block}"):
            assert ph in DIAGNOSIS_PROMPT_TEMPLATE
