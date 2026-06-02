"""Unit tests for the LLM provider abstraction layer (roadmap Phase 1.1).

All tests are offline and deterministic. Network adapters are exercised with
``httpx.MockTransport`` so no real LLM endpoint is contacted.
"""

from __future__ import annotations

import json

import httpx
import pytest

from packages.agent.llm.anthropic_adapter import AnthropicAdapter
from packages.agent.llm.base import extract_json, parse_into_schema
from packages.agent.llm.factory import build_llm
from packages.agent.llm.fake_adapter import FakeLLMAdapter
from packages.agent.llm.openai_adapter import OpenAICompatibleAdapter
from packages.agent.schemas import DiagnosisOutput, PlannedAction
from packages.common.errors import ValidationAppError
from packages.common.settings import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Factory                                                                      #
# --------------------------------------------------------------------------- #
class TestFactory:
    def test_fake_provider_returns_fake_adapter(self) -> None:
        llm = build_llm(_settings(llm_provider="fake"))
        assert isinstance(llm, FakeLLMAdapter)

    @pytest.mark.parametrize("provider", ["vllm", "openai", "deepseek"])
    def test_openai_compatible_providers(self, provider: str) -> None:
        llm = build_llm(_settings(llm_provider=provider, llm_api_key="k"))
        assert isinstance(llm, OpenAICompatibleAdapter)
        assert llm.provider == provider

    def test_anthropic_provider_returns_anthropic_adapter(self) -> None:
        llm = build_llm(_settings(llm_provider="anthropic", llm_api_key="sk-ant"))
        assert isinstance(llm, AnthropicAdapter)

    def test_unknown_provider_raises_validation_error(self) -> None:
        with pytest.raises(ValidationAppError):
            build_llm(_settings(llm_provider="not-a-provider"))

    def test_provider_name_is_case_insensitive(self) -> None:
        llm = build_llm(_settings(llm_provider="FAKE"))
        assert isinstance(llm, FakeLLMAdapter)

    def test_llm_api_key_is_secret_and_not_leaked(self) -> None:
        from pydantic import SecretStr

        settings = _settings(llm_provider="openai", llm_api_key="super-secret-key")
        assert isinstance(settings.llm_api_key, SecretStr)
        # The raw secret must not appear in str()/repr() (log/traceback safety).
        assert "super-secret-key" not in str(settings)
        assert "super-secret-key" not in repr(settings)
        assert settings.llm_api_key.get_secret_value() == "super-secret-key"

    def test_factory_unwraps_secret_api_key(self) -> None:
        llm = build_llm(_settings(llm_provider="openai", llm_api_key="k-unwrap"))
        assert llm.api_key == "k-unwrap"


# --------------------------------------------------------------------------- #
# Fake adapter — determinism must be preserved                                 #
# --------------------------------------------------------------------------- #
class TestFakeAdapter:
    def test_generate_json_is_deterministic(self) -> None:
        llm = FakeLLMAdapter()
        prompt = "Diagnose DatabaseConnectionExhaustion on checkout"
        first = llm.generate_json(prompt, DiagnosisOutput)
        second = llm.generate_json(prompt, DiagnosisOutput)
        assert isinstance(first, DiagnosisOutput)
        assert first.model_dump() == second.model_dump()
        assert "pool" in first.root_cause["summary"].lower()

    def test_generate_json_list_schema(self) -> None:
        llm = FakeLLMAdapter()
        actions = llm.generate_json("plan actions High5xxAfterDeploy", list[PlannedAction])
        assert isinstance(actions, list)
        assert all(isinstance(a, PlannedAction) for a in actions)

    def test_invoke_accepts_thinking_kwarg(self) -> None:
        llm = FakeLLMAdapter()
        raw = llm.invoke(
            [{"role": "user", "content": "RedisCacheAvalanche"}], thinking=True
        )
        assert json.loads(raw)["root_cause"]["summary"]

    def test_records_metadata(self) -> None:
        llm = FakeLLMAdapter()
        llm.invoke([{"role": "user", "content": "PodRestartLoop"}])
        assert llm.last_metadata["provider"] == "fake"
        assert llm.last_metadata["model"]


# --------------------------------------------------------------------------- #
# OpenAI-compatible adapter (vLLM / OpenAI / DeepSeek)                          #
# --------------------------------------------------------------------------- #
def _openai_client(content: str, *, finish: str = "stop") -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "model": payload["model"],
                "choices": [
                    {"message": {"role": "assistant", "content": content}, "finish_reason": finish}
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
        )

    return httpx.Client(
        base_url="http://vllm:8000/v1", transport=httpx.MockTransport(handler)
    )


class TestOpenAICompatibleAdapter:
    def test_invoke_returns_message_content(self) -> None:
        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=_openai_client('{"ok": true}'),
        )
        assert adapter.invoke([{"role": "user", "content": "hi"}]) == '{"ok": true}'

    def test_invoke_records_metadata(self) -> None:
        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            provider_name="vllm",
            client=_openai_client("hello"),
        )
        adapter.invoke([{"role": "user", "content": "hi"}])
        meta = adapter.last_metadata
        assert meta["provider"] == "vllm"
        assert meta["model"] == "qwen-7b"
        assert meta["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        assert meta["finish_reason"] == "stop"

    def test_generate_json_parses_model(self) -> None:
        diag = {
            "hypotheses": [],
            "root_cause": {"summary": "pool exhausted", "confidence": 0.9},
            "evidence_ids": ["evd_1"],
            "missing_evidence": [],
        }
        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=_openai_client(json.dumps(diag)),
        )
        out = adapter.generate_json("diagnose", DiagnosisOutput)
        assert isinstance(out, DiagnosisOutput)
        assert out.root_cause["summary"] == "pool exhausted"

    def test_generate_json_parses_list(self) -> None:
        actions = [
            {"type": "create_ticket", "target": "dev", "risk_hint": "L1"},
        ]
        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=_openai_client(json.dumps(actions)),
        )
        out = adapter.generate_json("plan", list[PlannedAction])
        assert isinstance(out, list)
        assert out[0].type == "create_ticket"

    def test_generate_json_strips_markdown_fence(self) -> None:
        fenced = "```json\n{\"summary\": \"x\"}\n```"
        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=_openai_client(fenced),
        )
        out = adapter.generate_json("diagnose", DiagnosisOutput)
        assert isinstance(out, DiagnosisOutput)

    def test_reasoning_params_added_when_enabled(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "model": "qwen-7b",
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        client = httpx.Client(
            base_url="http://vllm:8000/v1", transport=httpx.MockTransport(handler)
        )
        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            reasoning_enabled=True,
            reasoning_effort="high",
            client=client,
        )
        adapter.invoke([{"role": "user", "content": "hi"}], thinking=True)
        assert captured.get("reasoning_effort") == "high"


# --------------------------------------------------------------------------- #
# Anthropic adapter                                                            #
# --------------------------------------------------------------------------- #
def _anthropic_client(text: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "model": payload["model"],
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 9},
            },
        )

    return httpx.Client(
        base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
    )


class TestAnthropicAdapter:
    def test_invoke_returns_text_block(self) -> None:
        adapter = AnthropicAdapter(
            model="claude-sonnet-4-6", api_key="sk-ant", client=_anthropic_client("answer")
        )
        assert adapter.invoke([{"role": "user", "content": "hi"}]) == "answer"

    def test_invoke_records_metadata(self) -> None:
        adapter = AnthropicAdapter(
            model="claude-sonnet-4-6", api_key="sk-ant", client=_anthropic_client("answer")
        )
        adapter.invoke([{"role": "user", "content": "hi"}])
        meta = adapter.last_metadata
        assert meta["provider"] == "anthropic"
        assert meta["usage"] == {"prompt_tokens": 5, "completion_tokens": 9}

    def test_generate_json_parses_model(self) -> None:
        diag = {"hypotheses": [], "root_cause": {"summary": "deploy bug"}}
        adapter = AnthropicAdapter(
            model="claude-sonnet-4-6",
            api_key="sk-ant",
            client=_anthropic_client(json.dumps(diag)),
        )
        out = adapter.generate_json("diagnose", DiagnosisOutput)
        assert isinstance(out, DiagnosisOutput)
        assert out.root_cause["summary"] == "deploy bug"

    def test_system_message_is_split_out(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

        client = httpx.Client(
            base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
        )
        adapter = AnthropicAdapter(model="claude-sonnet-4-6", api_key="sk-ant", client=client)
        adapter.invoke(
            [
                {"role": "system", "content": "you are an SRE"},
                {"role": "user", "content": "hi"},
            ]
        )
        assert captured.get("system") == "you are an SRE"
        messages = captured.get("messages")
        assert isinstance(messages, list)
        assert all(m["role"] != "system" for m in messages)

    def test_missing_api_key_raises(self) -> None:
        with pytest.raises(ValidationAppError):
            AnthropicAdapter(model="claude-sonnet-4-6", api_key=None)


# --------------------------------------------------------------------------- #
# JSON parsing helpers                                                         #
# --------------------------------------------------------------------------- #
class TestJsonHelpers:
    def test_extract_json_plain(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_extract_json_markdown_fence(self) -> None:
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_extract_json_bare_fence(self) -> None:
        assert extract_json('```\n[1, 2]\n```') == [1, 2]

    def test_extract_json_from_surrounding_prose(self) -> None:
        assert extract_json('Here you go: {"a": 1}. Thanks!') == {"a": 1}

    def test_extract_json_raises_when_absent(self) -> None:
        with pytest.raises(ValueError):
            extract_json("no json at all")

    def test_parse_into_schema_passthrough_for_plain_type(self) -> None:
        assert parse_into_schema({"a": 1}, dict) == {"a": 1}
