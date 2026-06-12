"""Unit tests for _build_deps with effective config integration (PR 5.5)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _build_or_unavailable
# ---------------------------------------------------------------------------


def test_build_or_unavailable_returns_real_tool_when_url_present():
    """Returns the real tool when URL is provided."""
    from apps.worker.tasks import _build_or_unavailable

    mock_tool_cls = MagicMock(return_value="real_tool_instance")
    result = _build_or_unavailable(
        mock_tool_cls, "http://prom:9090", "metrics", extra_arg=42
    )
    assert result == "real_tool_instance"
    mock_tool_cls.assert_called_once_with(
        base_url="http://prom:9090", extra_arg=42
    )


def test_build_or_unavailable_returns_unavailable_tool_when_url_is_none():
    """Returns UnavailableTool when URL is None."""
    from apps.worker.tasks import _build_or_unavailable
    from packages.tools.unavailable import UnavailableTool

    result = _build_or_unavailable(MagicMock(), None, "metrics")
    assert isinstance(result, UnavailableTool)
    assert result.name == "metrics"
    assert "not configured" in result._reason


def test_build_or_unavailable_passes_url_when_not_none():
    """When url is not None, tool is constructed with base_url even if empty string."""
    from apps.worker.tasks import _build_or_unavailable

    mock_tool_cls = MagicMock(return_value="tool")
    result = _build_or_unavailable(mock_tool_cls, "", "logs")
    assert result == "tool"


# ---------------------------------------------------------------------------
# UnavailableTool
# ---------------------------------------------------------------------------


def test_unavailable_tool_name():
    """UnavailableTool has the correct name."""
    from packages.tools.unavailable import UnavailableTool

    tool = UnavailableTool("metrics", reason="Prometheus not configured")
    assert tool.name == "metrics"


def test_unavailable_tool_run_returns_degraded():
    """UnavailableTool.run() returns degraded status with reason."""
    from packages.tools.unavailable import UnavailableTool
    from pydantic import BaseModel

    class FakeQuery(BaseModel):
        query: str = "test"

    tool = UnavailableTool("metrics", reason="Prometheus not configured")
    result = tool.run(FakeQuery(query="test"))
    assert result.status == "degraded"
    assert "Prometheus not configured" in result.summary
    assert result.duration_ms == 0


def test_unavailable_tool_timeout_is_one_second():
    """UnavailableTool has a minimal timeout."""
    from packages.tools.unavailable import UnavailableTool

    tool = UnavailableTool("logs", reason="Loki unavailable")
    assert tool.timeout_seconds == 1.0


# ---------------------------------------------------------------------------
# _build_deps — demo path (backward compatible)
# ---------------------------------------------------------------------------


def _make_mock_effective_config(**overrides):
    """Build a mock EffectiveConfig with defaults suitable for tests."""
    cfg = MagicMock()
    cfg.prometheus = MagicMock(url="http://localhost:9090", source="default", degraded=False)
    cfg.loki = MagicMock(url="http://localhost:3100", source="default", degraded=False)
    cfg.jaeger = MagicMock(url="http://localhost:16686", source="default", degraded=False)
    cfg.alertmanager = MagicMock(url="http://localhost:9093", source="default", degraded=False)
    cfg.metrics_service_label = "service"
    cfg.logs_service_label = "service"
    cfg.source = "demo"
    cfg.warnings = []
    for key, val in overrides.items():
        setattr(cfg, key, val)
    return cfg


def _make_mock_settings(**overrides):
    """Build a mock Settings with defaults suitable for tests."""
    s = MagicMock()
    s.app_env = "local"
    s.tool_timeout_seconds = 5.0
    s.prometheus_url = "http://localhost:9090"
    s.loki_url = "http://localhost:3100"
    s.jaeger_url = "http://localhost:16686"
    s.alertmanager_url = "http://localhost:9093"
    s.metrics_service_label = "service"
    s.logs_service_label = "service"
    s.metrics_step_seconds = 15
    s.metrics_max_window_seconds = 3600
    s.metrics_max_shards = 10
    s.runbook_hybrid_search_enabled = False
    for key, val in overrides.items():
        setattr(s, key, val)
    return s


# Common tool patches needed for all _build_deps calls.
_BUILD_DEPS_PATCHES = [
    "apps.worker.tasks.MetricsTool",
    "apps.worker.tasks.LogsTool",
    "apps.worker.tasks.TraceTool",
    "apps.worker.tasks.GitChangeTool",
    "apps.worker.tasks.K8sDiagnosticsTool",
    "apps.worker.tasks.DbDiagnosticsTool",
    "apps.worker.tasks.RunbookSearchTool",
    "apps.worker.tasks.RunbookRetriever",
    "apps.worker.tasks.MemoryStore",
    "apps.worker.tasks.ContextBuilder",
    "apps.worker.tasks.build_llm",
    "apps.worker.tasks.build_trace_backend",
    "apps.worker.tasks.build_deployment_backend",
    "apps.worker.tasks.build_k8s_backend",
    "apps.worker.tasks.build_db_diagnostics_backend",
    "apps.worker.tasks.build_executor_backend",
    "apps.worker.tasks.RunbookChunkRepository",
    "apps.worker.tasks.ToolCallRepository",
]


def test_build_deps_demo_path_uses_effective_config():
    """Demo path uses EffectiveConfig.from_demo_sources()."""
    mock_db = MagicMock()
    mock_settings = _make_mock_settings()

    with patch(
        "packages.discovery.config_merge.EffectiveConfig.from_demo_sources",
        return_value=_make_mock_effective_config(),
    ):
        with _multi_patch(_BUILD_DEPS_PATCHES):
            from apps.worker.tasks import _build_deps
            result = _build_deps(mock_db, mock_settings, "run-1", "inc-1")

    assert result is not None
    assert result.effective_config is not None
    assert result.effective_config.source == "demo"


def test_build_deps_missing_prometheus_produces_unavailable_tool():
    """When prometheus URL is None, metrics_tool is UnavailableTool."""
    from packages.tools.unavailable import UnavailableTool

    mock_db = MagicMock()
    mock_settings = _make_mock_settings(app_env="production")

    eff_cfg = _make_mock_effective_config(
        source="operator",
        prometheus=MagicMock(url=None, source="default", degraded=True,
                             degraded_reason="No configured URL"),
        warnings=["prometheus: No configured URL"],
    )

    with patch(
        "packages.discovery.config_merge.EffectiveConfig.from_operator_sources",
        return_value=eff_cfg,
    ):
        with patch(
            "packages.db.repositories.effective_configs.EffectiveConfigRepository"
        ) as mock_ec_repo_cls:
            mock_ec_repo = MagicMock()
            mock_ec_repo.get_latest_published.return_value = None
            mock_ec_repo_cls.return_value = mock_ec_repo

            with _multi_patch(_BUILD_DEPS_PATCHES):
                from apps.worker.tasks import _build_deps
                result = _build_deps(mock_db, mock_settings, "run-2", "inc-2")

    assert result is not None
    assert isinstance(result.metrics_tool, UnavailableTool)
    assert result.metrics_tool.name == "metrics"
    assert result.effective_config is not None
    assert result.config_version_id is None


def test_build_deps_tracks_config_version_id():
    """config_version_id is set from the published EffectiveConfigVersion."""
    mock_db = MagicMock()
    mock_settings = _make_mock_settings(app_env="production")

    eff_cfg = _make_mock_effective_config(
        source="operator",
        prometheus=MagicMock(url="http://prom:9090", source="published"),
    )

    with patch(
        "packages.discovery.config_merge.EffectiveConfig.from_operator_sources",
        return_value=eff_cfg,
    ):
        with patch(
            "packages.db.repositories.effective_configs.EffectiveConfigRepository"
        ) as mock_ec_repo_cls:
            mock_ec_repo = MagicMock()
            mock_published = MagicMock()
            mock_published.version_id = "ecv_test_version_001"
            mock_published.config_snapshot = {"prometheus_url": "http://prom:9090"}
            mock_ec_repo.get_latest_published.return_value = mock_published
            mock_ec_repo_cls.return_value = mock_ec_repo

            with _multi_patch(_BUILD_DEPS_PATCHES):
                from apps.worker.tasks import _build_deps
                result = _build_deps(mock_db, mock_settings, "run-3", "inc-3")

    assert result is not None
    assert result.config_version_id == "ecv_test_version_001"
    assert result.effective_config is not None


def test_build_deps_production_no_published_config_does_not_crash():
    """Production path degrades gracefully when no published config exists."""
    mock_db = MagicMock()
    mock_settings = _make_mock_settings(app_env="production")

    eff_cfg = _make_mock_effective_config(
        source="operator",
        prometheus=MagicMock(url=None, source="default", degraded=True,
                             degraded_reason="No configured URL for prometheus"),
        loki=MagicMock(url=None, source="default", degraded=True,
                       degraded_reason="No configured URL for loki"),
    )

    with patch(
        "packages.discovery.config_merge.EffectiveConfig.from_operator_sources",
        return_value=eff_cfg,
    ):
        with patch(
            "packages.db.repositories.effective_configs.EffectiveConfigRepository"
        ) as mock_ec_repo_cls:
            mock_ec_repo = MagicMock()
            mock_ec_repo.get_latest_published.return_value = None
            mock_ec_repo_cls.return_value = mock_ec_repo

            with _multi_patch(_BUILD_DEPS_PATCHES):
                from apps.worker.tasks import _build_deps
                result = _build_deps(mock_db, mock_settings, "run-4", "inc-4")

    assert result is not None
    assert result.config_version_id is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _multi_patch:
    """Context manager that applies multiple patches at once."""

    def __init__(self, targets: list[str]):
        self._targets = targets
        self._patches: list[MagicMock] = []

    def __enter__(self):
        for t in self._targets:
            p = patch(t)
            p.start()
            self._patches.append(p)
        return self

    def __exit__(self, *args):
        for p in reversed(self._patches):
            p.__exit__(*args)
        self._patches.clear()
