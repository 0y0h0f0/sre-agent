"""Tests for PR 0.6: DisabledLLM adapter."""

from __future__ import annotations

from pydantic import BaseModel

from packages.agent.llm.disabled_adapter import DisabledLLMAdapter
from packages.agent.llm.factory import build_llm
from packages.common.settings import Settings


class _TestSchema(BaseModel):
    root_cause: str = ""


class TestDisabledLLMAdapter:
    def test_provider_is_disabled(self):
        """Adapter reports provider='disabled'."""
        adapter = DisabledLLMAdapter()
        assert adapter.provider == "disabled"

    def test_invoke_returns_string(self):
        """invoke() returns a string response without network calls."""
        adapter = DisabledLLMAdapter()
        result = adapter.invoke([
            {"role": "system", "content": "You are an SRE assistant."},
            {"role": "user", "content": "Diagnose: high latency"},
        ])
        assert isinstance(result, str)
        assert len(result) > 0

    def test_generate_json_returns_parsed(self):
        """generate_json() returns a parsed object without network calls."""
        adapter = DisabledLLMAdapter()
        result = adapter.generate_json(
            "Diagnose a CPU throttle alert",
            _TestSchema,
        )
        assert isinstance(result, _TestSchema)

    def test_metadata_records_fallback(self):
        """Metadata marks provider=disabled with zero-token usage."""
        adapter = DisabledLLMAdapter()
        adapter.invoke([{"role": "user", "content": "test"}])
        meta = adapter.get_last_metadata()
        assert meta.get("provider") == "disabled"
        assert meta.get("model") == "deterministic-fallback"
        assert meta.get("finish_reason") == "stop"
        usage = meta.get("usage", {})
        assert usage.get("prompt_tokens") == 0
        assert usage.get("completion_tokens") == 0

    def test_no_network_call(self):
        """Disabled adapter never makes network calls (zero-token usage metadata)."""
        adapter = DisabledLLMAdapter()
        adapter.generate_json("test", _TestSchema)
        meta = adapter.get_last_metadata()
        assert meta.get("provider") == "disabled"
        usage = meta.get("usage", {})
        assert usage.get("prompt_tokens") == 0
        assert usage.get("completion_tokens") == 0


class TestBuildLLMDisabled:
    def test_build_llm_disabled_returns_disabled_adapter(self):
        """build_llm with provider='disabled' returns DisabledLLMAdapter."""
        settings = Settings(
            _env_file=None,
            llm_provider="disabled",
        )
        adapter = build_llm(settings)
        assert isinstance(adapter, DisabledLLMAdapter)

    def test_production_env_without_llm_provider_uses_disabled(self):
        """APP_ENV=production without explicit LLM_PROVIDER defaults disabled."""
        # Delete LLM_PROVIDER from env, set APP_ENV=production.
        import os
        old = os.environ.pop("LLM_PROVIDER", None)
        try:
            os.environ["APP_ENV"] = "production"
            settings = Settings(_env_file=None)
            assert settings.llm_provider == "disabled"
            adapter = build_llm(settings)
            assert isinstance(adapter, DisabledLLMAdapter)
        finally:
            os.environ.pop("APP_ENV", None)
            if old is not None:
                os.environ["LLM_PROVIDER"] = old
