"""Unit tests for the LLM provider abstraction layer (roadmap Phase 1.1).

All tests are offline and deterministic. Network adapters are exercised with
``httpx.MockTransport`` so no real LLM endpoint is contacted.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from packages.agent.llm.anthropic_adapter import AnthropicAdapter
from packages.agent.llm.base import extract_json, parse_into_schema
from packages.agent.llm.disabled_adapter import DisabledLLMAdapter
from packages.agent.llm.factory import build_llm
from packages.agent.llm.fake_adapter import FakeLLMAdapter
from packages.agent.llm.openai_adapter import OpenAICompatibleAdapter
from packages.agent.llm.profiles import resolve_llm_profile
from packages.agent.llm.redacting_adapter import RedactingLLMAdapter
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

    def test_vllm_provider_returns_openai_compatible_adapter(self) -> None:
        llm = build_llm(
            _settings(
                llm_provider="vllm",
                llm_api_key="k",
            )
        )
        assert isinstance(llm, OpenAICompatibleAdapter)
        assert llm.provider == "vllm"

    def test_default_profile_preserves_global_model_options(self) -> None:
        llm = build_llm(
            _settings(
                llm_provider="vllm",
                llm_model="qwen-7b",
                llm_max_tokens=768,
                llm_temperature=0.2,
            )
        )

        assert isinstance(llm, OpenAICompatibleAdapter)
        assert llm.model == "qwen-7b"
        assert llm.max_tokens == 768
        assert llm.temperature == 0.2

    def test_profile_model_and_token_overrides_are_applied(self) -> None:
        llm = build_llm(
            _settings(
                llm_provider="vllm",
                llm_model="qwen-base",
                llm_max_tokens=512,
                llm_fast_json_model="qwen-fast",
                llm_fast_json_max_tokens=128,
            ),
            profile="fast_json",
        )

        assert isinstance(llm, OpenAICompatibleAdapter)
        assert llm.model == "qwen-fast"
        assert llm.max_tokens == 128

    def test_node_override_maps_take_precedence_over_profile_defaults(self) -> None:
        profile = resolve_llm_profile(
            _settings(
                llm_model="qwen-base",
                llm_max_tokens=512,
                llm_report_model="qwen-report",
                llm_report_max_tokens=1024,
                llm_node_model_overrides='{"report": "qwen-report-hot"}',
                llm_node_max_tokens="report=640,diagnose_reasoning=1536",
            ),
            profile="report",
        )

        assert profile.model == "qwen-report-hot"
        assert profile.max_tokens == 640

    def test_profile_override_does_not_enable_cloud_provider(self) -> None:
        with pytest.raises(ValidationAppError, match="external LLM provider"):
            build_llm(
                _settings(
                    llm_provider="openai",
                    llm_api_key="k",
                    llm_report_model="gpt-report",
                ),
                profile="report",
            )

    def test_profile_override_preserves_cloud_redaction_wrapper(self) -> None:
        llm = build_llm(
            _settings(
                llm_provider="openai",
                llm_api_key="k",
                llm_external_provider_allowed=True,
                llm_report_model="gpt-report",
                llm_report_max_tokens=900,
            ),
            profile="report",
        )

        assert isinstance(llm, RedactingLLMAdapter)
        assert isinstance(llm.delegate, OpenAICompatibleAdapter)
        assert llm.delegate.model == "gpt-report"
        assert llm.delegate.max_tokens == 900

    @pytest.mark.parametrize("provider", ["fake", "disabled"])
    def test_profiles_do_not_change_offline_provider_paths(self, provider: str) -> None:
        llm = build_llm(
            _settings(
                llm_provider=provider,
                llm_fast_json_model="external-fast-model",
                llm_fast_json_max_tokens=64,
            ),
            profile="fast_json",
        )

        assert isinstance(llm, (FakeLLMAdapter, DisabledLLMAdapter))

    @pytest.mark.parametrize("provider", ["openai", "deepseek"])
    def test_external_openai_compatible_providers_are_redacted(
        self, provider: str
    ) -> None:
        llm = build_llm(
            _settings(
                llm_provider=provider,
                llm_api_key="k",
                llm_external_provider_allowed=True,
            )
        )
        assert isinstance(llm, RedactingLLMAdapter)
        assert isinstance(llm.delegate, OpenAICompatibleAdapter)
        assert llm.delegate.provider == provider

    def test_anthropic_provider_returns_anthropic_adapter(self) -> None:
        llm = build_llm(
            _settings(
                llm_provider="anthropic",
                llm_api_key="sk-ant",
                llm_external_provider_allowed=True,
            )
        )
        assert isinstance(llm, RedactingLLMAdapter)
        assert isinstance(llm.delegate, AnthropicAdapter)

    @pytest.mark.parametrize("provider", ["openai", "deepseek", "anthropic"])
    def test_external_cloud_provider_requires_explicit_allow(self, provider: str) -> None:
        with pytest.raises(ValidationAppError, match="external LLM provider"):
            build_llm(_settings(llm_provider=provider, llm_api_key="k"))

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
        llm = build_llm(
            _settings(
                llm_provider="openai",
                llm_api_key="k-unwrap",
                llm_external_provider_allowed=True,
            )
        )
        assert isinstance(llm, RedactingLLMAdapter)
        assert isinstance(llm.delegate, OpenAICompatibleAdapter)
        assert llm.delegate.api_key == "k-unwrap"


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
# Redacting adapter — external cloud egress boundary                           #
# --------------------------------------------------------------------------- #
class _RecordingLLM:
    def __init__(self) -> None:
        self.last_prompt = ""
        self.last_messages: list[dict[str, Any]] = []
        self.last_metadata = {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }

    def invoke(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> str:
        self.last_messages = messages
        return '{"ok": true}'

    def invoke_with_metadata(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> tuple[str, dict[str, Any]]:
        result = self.invoke(messages, thinking=thinking, **kwargs)
        return result, dict(self.last_metadata)

    def generate_json(
        self, prompt: str, output_schema: Any, *, thinking: bool = False, **kwargs: Any
    ) -> dict[str, bool]:
        self.last_prompt = prompt
        return {"ok": True}

    def generate_json_with_metadata(
        self, prompt: str, output_schema: Any, *, thinking: bool = False, **kwargs: Any
    ) -> tuple[dict[str, bool], dict[str, Any]]:
        result = self.generate_json(prompt, output_schema, thinking=thinking, **kwargs)
        return result, dict(self.last_metadata)


class TestRedactingLLMAdapter:
    def test_generate_json_redacts_prompt_before_delegate(self) -> None:
        delegate = _RecordingLLM()
        adapter = RedactingLLMAdapter(delegate)

        out = adapter.generate_json(
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789 "
            "password=topsecret",
            dict,
        )

        assert out == {"ok": True}
        assert "abcdefghijklmnopqrstuvwxyz0123456789" not in delegate.last_prompt
        assert "topsecret" not in delegate.last_prompt
        assert adapter.last_metadata["provider"] == "deepseek"
        assert adapter.last_metadata["redaction_applied"] is True
        assert adapter.last_metadata["redaction_count"] >= 2
        assert "bearer_token" in adapter.last_metadata["redaction_types"]

    def test_generate_json_with_metadata_returns_call_local_redaction_metadata(self) -> None:
        delegate = _RecordingLLM()
        adapter = RedactingLLMAdapter(delegate)

        out, meta = adapter.generate_json_with_metadata(
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
            dict,
        )

        assert out == {"ok": True}
        assert meta["provider"] == "deepseek"
        assert meta["redaction_applied"] is True
        assert meta["redaction_count"] >= 1
        assert adapter.last_metadata == {}

    def test_invoke_redacts_nested_messages_without_mutating_original(self) -> None:
        delegate = _RecordingLLM()
        adapter = RedactingLLMAdapter(delegate)
        messages = [
            {
                "role": "user",
                "content": "call http://10.1.2.3/status with api_key=abc123",
                "metadata": {"note": "namespace=payments"},
            }
        ]

        adapter.invoke(messages)

        sent = json.dumps(delegate.last_messages)
        assert "10.1.2.3" not in sent
        assert "abc123" not in sent
        assert "namespace=payments" not in sent
        assert "10.1.2.3" in str(messages)
        assert adapter.last_metadata["redaction_count"] >= 3


# --------------------------------------------------------------------------- #
# OpenAI-compatible adapter (vLLM / OpenAI / DeepSeek)                          #
# --------------------------------------------------------------------------- #
def _openai_client(
    content: str,
    *,
    finish: str = "stop",
    usage: dict[str, Any] | None = None,
    response_extra: dict[str, Any] | None = None,
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        body = {
            "id": "cmpl-1",
            "model": payload["model"],
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish,
                }
            ],
            "usage": usage
            if usage is not None
            else {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        }
        if response_extra:
            body.update(response_extra)
        return httpx.Response(
            200,
            json=body,
        )

    return httpx.Client(
        base_url="http://vllm:8000/v1", transport=httpx.MockTransport(handler)
    )


class _MetricSpy:
    def __init__(self) -> None:
        self.inc_values: list[float] = []
        self.observe_values: list[float] = []
        self.label_calls: list[dict[str, object]] = []

    def labels(self, **kwargs: object) -> _MetricSpy:
        self.label_calls.append(kwargs)
        return self

    def inc(self, value: float = 1) -> None:
        self.inc_values.append(value)

    def observe(self, value: float) -> None:
        self.observe_values.append(value)


class TestOpenAICompatibleAdapter:
    def test_generate_json_with_metadata_returns_call_local_metadata(self) -> None:
        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=_openai_client(
                '{"ok": true}',
                usage={"prompt_tokens": 4, "completion_tokens": 2},
            ),
        )

        out, meta = adapter.generate_json_with_metadata("prompt", dict)

        assert out == {"ok": True}
        assert meta["provider"] == "vllm"
        assert meta["model"] == "qwen-7b"
        assert meta["usage"]["prompt_tokens"] == 4
        assert adapter.last_metadata == {}

    def test_invoke_returns_message_content(self) -> None:
        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=_openai_client('{"ok": true}'),
        )
        assert adapter.invoke([{"role": "user", "content": "hi"}]) == '{"ok": true}'

    def test_invoke_uses_per_call_profile_options(self) -> None:
        payloads: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            payloads.append(payload)
            return httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "model": payload["model"],
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-base",
            max_tokens=512,
            reasoning_effort="medium",
            client=httpx.Client(
                base_url="http://vllm:8000/v1",
                transport=httpx.MockTransport(handler),
            ),
        )

        adapter.invoke(
            [{"role": "user", "content": "hi"}],
            thinking=True,
            model="qwen-fast",
            max_tokens=96,
            reasoning_effort="high",
        )

        assert payloads[0]["model"] == "qwen-fast"
        assert payloads[0]["max_tokens"] == 96
        assert payloads[0]["reasoning_effort"] == "high"
        assert adapter.last_metadata["model"] == "qwen-fast"

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
        assert meta["provider_cache_status"] == "unknown"
        assert isinstance(meta["duration_ms"], int)
        assert meta["duration_ms"] >= 0

    def test_invoke_records_cached_prompt_tokens_as_provider_hit(self) -> None:
        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=_openai_client(
                "hello",
                usage={
                    "prompt_tokens": 40,
                    "completion_tokens": 5,
                    "total_tokens": 45,
                    "prompt_tokens_details": {"cached_tokens": 32},
                },
            ),
        )

        adapter.invoke([{"role": "user", "content": "hi"}])

        meta = adapter.last_metadata
        assert meta["provider_cache_status"] == "hit"
        assert meta["usage"]["cached_prompt_tokens"] == 32

    def test_invoke_records_explicit_zero_cached_tokens_as_provider_miss(self) -> None:
        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=_openai_client(
                "hello",
                usage={
                    "prompt_tokens": 40,
                    "completion_tokens": 5,
                    "total_tokens": 45,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
            ),
        )

        adapter.invoke([{"role": "user", "content": "hi"}])

        meta = adapter.last_metadata
        assert meta["provider_cache_status"] == "miss"
        assert meta["usage"]["cached_prompt_tokens"] == 0

    def test_invoke_records_unknown_cache_without_explicit_usage_detail(self) -> None:
        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=_openai_client("hello", finish="cache_hit"),
        )

        adapter.invoke([{"role": "user", "content": "hi"}])

        meta = adapter.last_metadata
        assert meta["finish_reason"] == "cache_hit"
        assert meta["provider_cache_status"] == "unknown"
        assert "cached_prompt_tokens" not in meta["usage"]

    def test_unknown_provider_cache_status_does_not_record_miss_metric(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from packages.common import metrics as agent_metrics

        hit_metric = _MetricSpy()
        miss_metric = _MetricSpy()
        status_metric = _MetricSpy()
        monkeypatch.setattr(agent_metrics, "llm_cache_hit_total", hit_metric)
        monkeypatch.setattr(agent_metrics, "llm_cache_miss_total", miss_metric)
        monkeypatch.setattr(agent_metrics, "llm_provider_cache_status_total", status_metric)

        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=_openai_client("hello", finish="cache_hit"),
        )

        adapter.invoke([{"role": "user", "content": "hi"}])

        assert hit_metric.inc_values == []
        assert miss_metric.inc_values == []
        assert status_metric.inc_values == [1]
        assert status_metric.label_calls == [
            {"model": "qwen_7b", "provider": "vllm", "status": "unknown"}
        ]

    @pytest.mark.parametrize(
        ("cached_tokens", "expected_hits", "expected_misses"),
        [(9, [1], []), (0, [], [1])],
    )
    def test_explicit_provider_cache_status_records_cache_metric(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cached_tokens: int,
        expected_hits: list[float],
        expected_misses: list[float],
    ) -> None:
        from packages.common import metrics as agent_metrics

        hit_metric = _MetricSpy()
        miss_metric = _MetricSpy()
        monkeypatch.setattr(agent_metrics, "llm_cache_hit_total", hit_metric)
        monkeypatch.setattr(agent_metrics, "llm_cache_miss_total", miss_metric)

        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=_openai_client(
                "hello",
                usage={
                    "prompt_tokens": 40,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": cached_tokens},
                },
            ),
        )

        adapter.invoke([{"role": "user", "content": "hi"}])

        assert hit_metric.inc_values == expected_hits
        assert miss_metric.inc_values == expected_misses

    def test_llm_runtime_metrics_are_emitted_once_with_safe_labels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from packages.common import metrics as agent_metrics

        prompt_metric = _MetricSpy()
        completion_metric = _MetricSpy()
        cached_metric = _MetricSpy()
        duration_metric = _MetricSpy()
        status_metric = _MetricSpy()
        hit_metric = _MetricSpy()
        miss_metric = _MetricSpy()
        monkeypatch.setattr(agent_metrics, "llm_prompt_tokens_total", prompt_metric)
        monkeypatch.setattr(agent_metrics, "llm_completion_tokens_total", completion_metric)
        monkeypatch.setattr(agent_metrics, "llm_cached_prompt_tokens_total", cached_metric)
        monkeypatch.setattr(agent_metrics, "llm_call_duration_seconds", duration_metric)
        monkeypatch.setattr(agent_metrics, "llm_provider_cache_status_total", status_metric)
        monkeypatch.setattr(agent_metrics, "llm_cache_hit_total", hit_metric)
        monkeypatch.setattr(agent_metrics, "llm_cache_miss_total", miss_metric)

        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            provider_name="vllm-prod",
            client=_openai_client(
                "hello",
                usage={
                    "prompt_tokens": 40,
                    "completion_tokens": 5,
                    "total_tokens": 45,
                    "prompt_tokens_details": {"cached_tokens": 32},
                },
            ),
        )

        adapter.invoke([{"role": "user", "content": "prompt-secret"}])

        assert prompt_metric.inc_values == [40]
        assert completion_metric.inc_values == [5]
        assert cached_metric.inc_values == [32]
        assert len(duration_metric.observe_values) == 1
        assert status_metric.inc_values == [1]
        assert hit_metric.inc_values == [1]
        assert miss_metric.inc_values == []
        assert prompt_metric.label_calls == [{"model": "qwen_7b", "provider": "vllm_prod"}]
        assert completion_metric.label_calls == [
            {"model": "qwen_7b", "provider": "vllm_prod"}
        ]
        assert cached_metric.label_calls == [{"model": "qwen_7b", "provider": "vllm_prod"}]
        assert duration_metric.label_calls == [
            {"model": "qwen_7b", "provider": "vllm_prod"}
        ]
        assert status_metric.label_calls == [
            {"model": "qwen_7b", "provider": "vllm_prod", "status": "hit"}
        ]
        assert "prompt-secret" not in json.dumps(
            prompt_metric.label_calls
            + completion_metric.label_calls
            + cached_metric.label_calls
            + duration_metric.label_calls
            + status_metric.label_calls
            + hit_metric.label_calls
            + miss_metric.label_calls
        )

    def test_llm_usage_metric_helper_sanitizes_malformed_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from packages.common import metrics as agent_metrics

        prompt_metric = _MetricSpy()
        completion_metric = _MetricSpy()
        cached_metric = _MetricSpy()
        duration_metric = _MetricSpy()
        status_metric = _MetricSpy()
        hit_metric = _MetricSpy()
        miss_metric = _MetricSpy()
        monkeypatch.setattr(agent_metrics, "llm_prompt_tokens_total", prompt_metric)
        monkeypatch.setattr(agent_metrics, "llm_completion_tokens_total", completion_metric)
        monkeypatch.setattr(agent_metrics, "llm_cached_prompt_tokens_total", cached_metric)
        monkeypatch.setattr(agent_metrics, "llm_call_duration_seconds", duration_metric)
        monkeypatch.setattr(agent_metrics, "llm_provider_cache_status_total", status_metric)
        monkeypatch.setattr(agent_metrics, "llm_cache_hit_total", hit_metric)
        monkeypatch.setattr(agent_metrics, "llm_cache_miss_total", miss_metric)

        agent_metrics.AgentMetricsCollector.record_llm_usage(
            model="qwen-7b",
            provider="vllm",
            prompt_tokens=-5,
            completion_tokens=float("inf"),
            cached_prompt_tokens=-2,
            duration_seconds=float("nan"),
            provider_cache_status=["hit"],  # type: ignore[arg-type]
        )

        assert prompt_metric.inc_values == [0]
        assert completion_metric.inc_values == [0]
        assert cached_metric.inc_values == []
        assert duration_metric.observe_values == [0.0]
        assert status_metric.label_calls == [
            {"model": "qwen_7b", "provider": "vllm", "status": "unknown"}
        ]
        assert status_metric.inc_values == [1]
        assert hit_metric.inc_values == []
        assert miss_metric.inc_values == []

    @pytest.mark.parametrize(
        ("cache_hit", "expected_status", "expected_hits", "expected_misses"),
        [(True, "hit", [1], []), (False, "miss", [], [1])],
    )
    def test_llm_usage_metric_helper_preserves_legacy_cache_hit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cache_hit: bool,
        expected_status: str,
        expected_hits: list[float],
        expected_misses: list[float],
    ) -> None:
        from packages.common import metrics as agent_metrics

        status_metric = _MetricSpy()
        hit_metric = _MetricSpy()
        miss_metric = _MetricSpy()
        monkeypatch.setattr(agent_metrics, "llm_provider_cache_status_total", status_metric)
        monkeypatch.setattr(agent_metrics, "llm_cache_hit_total", hit_metric)
        monkeypatch.setattr(agent_metrics, "llm_cache_miss_total", miss_metric)

        agent_metrics.AgentMetricsCollector.record_llm_usage(
            model="qwen-7b",
            provider="vllm",
            prompt_tokens=1,
            completion_tokens=1,
            duration_seconds=0.1,
            cache_hit=cache_hit,
        )

        assert status_metric.label_calls == [
            {"model": "qwen_7b", "provider": "vllm", "status": expected_status}
        ]
        assert status_metric.inc_values == [1]
        assert hit_metric.inc_values == expected_hits
        assert miss_metric.inc_values == expected_misses

    def test_diagnosis_completed_does_not_duplicate_llm_token_metrics(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from packages.common import metrics as agent_metrics

        prompt_metric = _MetricSpy()
        completion_metric = _MetricSpy()
        diagnosis_metric = _MetricSpy()
        duration_metric = _MetricSpy()
        monkeypatch.setattr(agent_metrics, "llm_prompt_tokens_total", prompt_metric)
        monkeypatch.setattr(agent_metrics, "llm_completion_tokens_total", completion_metric)
        monkeypatch.setattr(agent_metrics, "diagnosis_total", diagnosis_metric)
        monkeypatch.setattr(agent_metrics, "diagnosis_duration_seconds", duration_metric)

        agent_metrics.AgentMetricsCollector.record_diagnosis_completed(
            status="succeeded",
            duration_seconds=1.5,
            model="qwen-7b",
            provider="vllm",
            prompt_tokens=99,
            completion_tokens=7,
        )

        assert diagnosis_metric.inc_values == [1]
        assert duration_metric.observe_values == [1.5]
        assert prompt_metric.inc_values == []
        assert completion_metric.inc_values == []

    def test_invoke_records_reasoning_tokens_and_service_tier(self) -> None:
        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=_openai_client(
                "hello",
                usage={
                    "prompt_tokens": 40,
                    "completion_tokens": 11,
                    "total_tokens": 51,
                    "completion_tokens_details": {"reasoning_tokens": 6},
                },
                response_extra={"service_tier": "default"},
            ),
        )

        adapter.invoke([{"role": "user", "content": "hi"}])

        meta = adapter.last_metadata
        assert meta["usage"]["reasoning_tokens"] == 6
        assert meta["service_tier"] == "default"

    def test_invoke_drops_negative_usage_metadata(self) -> None:
        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=_openai_client(
                "hello",
                usage={
                    "prompt_tokens": -40,
                    "completion_tokens": -11,
                    "total_tokens": -51,
                    "prompt_tokens_details": {"cached_tokens": -6},
                    "completion_tokens_details": {"reasoning_tokens": -2},
                },
            ),
        )

        adapter.invoke([{"role": "user", "content": "hi"}])

        assert "usage" not in adapter.last_metadata
        assert adapter.last_metadata["provider_cache_status"] == "unknown"

    def test_invoke_drops_non_finite_usage_metadata(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            body = (
                "{"
                '"id":"cmpl-1",'
                f'"model":"{payload["model"]}",'
                '"choices":[{"message":{"role":"assistant","content":"hello"},'
                '"finish_reason":"stop"}],'
                '"usage":{'
                '"prompt_tokens":Infinity,'
                '"completion_tokens":NaN,'
                '"total_tokens":Infinity,'
                '"prompt_tokens_details":{"cached_tokens":Infinity},'
                '"completion_tokens_details":{"reasoning_tokens":NaN}'
                "}"
                "}"
            )
            return httpx.Response(200, content=body.encode("utf-8"))

        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=httpx.Client(
                base_url="http://vllm:8000/v1", transport=httpx.MockTransport(handler)
            ),
        )

        adapter.invoke([{"role": "user", "content": "hi"}])

        assert "usage" not in adapter.last_metadata
        assert adapter.last_metadata["provider_cache_status"] == "unknown"

    def test_invoke_does_not_record_raw_reasoning_content(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "model": payload["model"],
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "answer",
                                "reasoning_content": "raw private chain of thought",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 40,
                        "completion_tokens": 11,
                        "completion_tokens_details": {"reasoning_tokens": 6},
                    },
                },
            )

        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=httpx.Client(
                base_url="http://vllm:8000/v1", transport=httpx.MockTransport(handler)
            ),
        )

        adapter.invoke([{"role": "user", "content": "hi"}])

        serialized = json.dumps(adapter.last_metadata)
        assert "reasoning_summary" not in adapter.last_metadata
        assert "raw private chain of thought" not in serialized
        assert adapter.last_metadata["usage"]["reasoning_tokens"] == 6

    def test_invoke_omits_non_string_service_tier_metadata(self) -> None:
        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=_openai_client(
                "hello",
                response_extra={
                    "service_tier": {"unexpected": "raw response content"},
                },
            ),
        )

        adapter.invoke([{"role": "user", "content": "hi"}])

        serialized = json.dumps(adapter.last_metadata)
        assert "service_tier" not in adapter.last_metadata
        assert "raw response content" not in serialized

    def test_invoke_omits_malformed_model_and_finish_reason_metadata(self) -> None:
        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=_openai_client(
                "hello",
                response_extra={
                    "model": {"unexpected": "raw model content"},
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "hello"},
                            "finish_reason": {"unexpected": "raw finish content"},
                        }
                    ],
                },
            ),
        )

        adapter.invoke([{"role": "user", "content": "hi"}])

        serialized = json.dumps(adapter.last_metadata)
        assert adapter.last_metadata["model"] == "qwen-7b"
        assert adapter.last_metadata["finish_reason"] == ""
        assert "raw model content" not in serialized
        assert "raw finish content" not in serialized

    def test_invoke_metadata_does_not_store_prompt_or_completion_text(self) -> None:
        adapter = OpenAICompatibleAdapter(
            base_url="http://vllm:8000/v1",
            model="qwen-7b",
            client=_openai_client("completion-secret"),
        )

        adapter.invoke([{"role": "user", "content": "prompt-secret"}])

        serialized = json.dumps(adapter.last_metadata)
        assert "prompt-secret" not in serialized
        assert "completion-secret" not in serialized

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

    def test_global_reasoning_flag_does_not_override_node_flag(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "model": "qwen-7b",
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
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

        adapter.invoke([{"role": "user", "content": "hi"}], thinking=False)

        assert "reasoning_effort" not in captured

    def test_deepseek_standard_node_explicitly_disables_thinking(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "model": "deepseek-v4-flash",
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                },
            )

        client = httpx.Client(
            base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
        )
        adapter = OpenAICompatibleAdapter(
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
            provider_name="deepseek",
            reasoning_enabled=True,
            client=client,
        )

        adapter.invoke([{"role": "user", "content": "hi"}], thinking=False)

        assert captured["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in captured

    def test_deepseek_reasoning_node_enables_thinking(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "model": "deepseek-v4-pro",
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                },
            )

        client = httpx.Client(
            base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
        )
        adapter = OpenAICompatibleAdapter(
            base_url="https://api.deepseek.com",
            model="deepseek-v4-pro",
            provider_name="deepseek",
            reasoning_effort="medium",
            client=client,
        )

        adapter.invoke([{"role": "user", "content": "hi"}], thinking=True)

        assert captured["thinking"] == {"type": "enabled"}
        assert captured["reasoning_effort"] == "high"


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

    def test_global_reasoning_flag_does_not_override_node_flag(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                },
            )

        client = httpx.Client(
            base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
        )
        adapter = AnthropicAdapter(
            model="claude-sonnet-4-6",
            api_key="sk-ant",
            reasoning_enabled=True,
            client=client,
        )

        adapter.invoke([{"role": "user", "content": "hi"}], thinking=False)

        assert "thinking" not in captured
        assert "output_config" not in captured
        assert captured["temperature"] == 0.1

    def test_thinking_payload_omits_temperature_and_records_summary(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "model": "claude-sonnet-4-6",
                    "content": [
                        {"type": "thinking", "thinking": "checked evidence ids"},
                        {"type": "text", "text": "answer"},
                    ],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                },
            )

        client = httpx.Client(
            base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
        )
        adapter = AnthropicAdapter(
            model="claude-sonnet-4-6",
            api_key="sk-ant",
            reasoning_effort="high",
            client=client,
        )

        assert adapter.invoke([{"role": "user", "content": "hi"}], thinking=True) == "answer"

        assert captured["thinking"] == {"type": "adaptive"}
        assert captured["output_config"] == {"effort": "high"}
        assert "temperature" not in captured
        assert adapter.last_metadata["reasoning_summary"] == "checked evidence ids"

    def test_invoke_uses_per_call_profile_options(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "model": captured["model"],
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

        client = httpx.Client(
            base_url="https://api.anthropic.com",
            transport=httpx.MockTransport(handler),
        )
        adapter = AnthropicAdapter(
            model="claude-sonnet-4-6",
            api_key="sk-ant",
            max_tokens=512,
            reasoning_effort="medium",
            client=client,
        )

        adapter.invoke(
            [{"role": "user", "content": "hi"}],
            thinking=True,
            model="claude-haiku-fast",
            max_tokens=128,
            reasoning_effort="high",
        )

        assert captured["model"] == "claude-haiku-fast"
        assert captured["max_tokens"] == 128
        assert captured["output_config"] == {"effort": "high"}
        assert adapter.last_metadata["model"] == "claude-haiku-fast"


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
