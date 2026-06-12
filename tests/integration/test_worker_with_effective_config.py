"""Worker integration tests with EffectiveConfig — PR 5.5.

Tests the full EffectiveConfig → _build_deps → AgentDeps pipeline,
covering production and demo paths, missing backends, URL safety,
and manual config priority.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from apps.worker.tasks import _build_deps, _build_or_unavailable
from packages.agent.schemas import AgentDeps
from packages.tools.unavailable import UnavailableTool

# ---------------------------------------------------------------------------
# _build_or_unavailable unit tests
# ---------------------------------------------------------------------------


class TestBuildOrUnavailable:
    def test_returns_real_tool_when_url_present(self):
        from packages.tools.metrics import MetricsTool

        tool = _build_or_unavailable(
            MetricsTool,
            "http://prom:9090",
            "metrics",
            timeout_seconds=5.0,
        )
        assert isinstance(tool, MetricsTool)
        assert tool.base_url == "http://prom:9090"

    def test_returns_unavailable_tool_when_url_none(self):
        from packages.tools.metrics import MetricsTool

        tool = _build_or_unavailable(
            MetricsTool,
            None,
            "metrics",
            timeout_seconds=5.0,
        )
        assert isinstance(tool, UnavailableTool)
        assert tool.name == "metrics"

    def test_unavailable_tool_run_returns_degraded(self):
        from pydantic import BaseModel

        class _FakeQuery(BaseModel):
            pass

        tool = UnavailableTool("test_tool", reason="backend not configured")
        result = tool.run(_FakeQuery())
        assert result.status == "degraded"
        assert "backend not configured" in result.summary

    def test_unavailable_tool_timeout_is_short(self):
        tool = UnavailableTool("test_tool")
        assert tool.timeout_seconds == 1.0


# ---------------------------------------------------------------------------
# _build_deps integration tests
# ---------------------------------------------------------------------------


class TestBuildDepsIntegration:
    def test_demo_path_builds_agent_deps(self, db_session: Session):
        """Local/demo path should produce AgentDeps with working tools."""
        from packages.common.settings import Settings

        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            app_env="local",
            llm_provider="fake",
            embedding_provider="fake",
            prometheus_url="http://localhost:9090",
            loki_url="http://localhost:3100",
            jaeger_url="http://localhost:16686",
        )
        deps = _build_deps(db_session, settings, "run-1", "inc-1")
        assert isinstance(deps, AgentDeps)
        assert deps.metrics_tool is not None
        assert deps.logs_tool is not None

    def test_production_no_published_config_does_not_crash(
        self, db_session: Session
    ):
        """Production with no published config should still produce deps."""
        from packages.common.settings import Settings

        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            app_env="production",
            llm_provider="disabled",
            embedding_provider="fake",
        )
        deps = _build_deps(db_session, settings, "run-1", "inc-1")
        assert isinstance(deps, AgentDeps)
        # Metrics should be UnavailableTool since no URL configured
        assert isinstance(deps.metrics_tool, UnavailableTool)

    def test_config_version_id_in_run_state(
        self, db_session: Session
    ):
        """When published config exists, config_version_id is recorded."""
        from packages.common.ids import new_id
        from packages.common.settings import Settings
        from packages.common.time import utc_now
        from packages.db.models import EffectiveConfigVersion

        version = EffectiveConfigVersion(
            version_id=new_id("ecv_"),
            version_number=1,
            status="published",
            config_snapshot={
                "prometheus": {"url": "http://prod-prom:9090"},
            },
            published_at=utc_now(),
            published_by="test",
        )
        db_session.add(version)
        db_session.flush()

        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            app_env="production",
            llm_provider="disabled",
            embedding_provider="fake",
        )

        deps = _build_deps(db_session, settings, "run-1", "inc-1")
        assert isinstance(deps, AgentDeps)
        # Config version should be tracked
        assert deps.config_version_id == version.version_id
        assert deps.effective_config is not None

    def test_worker_does_not_use_unpublished_proposal(
        self, db_session: Session
    ):
        """Worker must only read published config — proposals are invisible."""
        from packages.common.ids import new_id
        from packages.common.settings import Settings
        from packages.db.models import DiscoveryProposal, DiscoveryRun

        status_run = DiscoveryRun(
            discovery_run_id=new_id("dr_"),
            source="scheduled",
            status="succeeded",
        )
        db_session.add(status_run)
        db_session.flush()

        proposal = DiscoveryProposal(
            proposal_id=new_id("proposal_"),
            discovery_run_id=status_run.discovery_run_id,
            status="pending_review",
            config_diff={"prometheus": {"url": "http://evil:9090"}},
        )
        db_session.add(proposal)
        db_session.flush()

        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            app_env="production",
            llm_provider="disabled",
            embedding_provider="fake",
        )

        deps = _build_deps(db_session, settings, "run-1", "inc-1")
        assert isinstance(deps, AgentDeps)
        # Unpublished proposal must NOT result in a working metrics tool
        assert isinstance(deps.metrics_tool, UnavailableTool)

    def test_effective_none_url_uses_unavailable_tool(
        self, db_session: Session
    ):
        """When effective config has None for a URL, worker gets UnavailableTool."""
        from packages.common.settings import Settings

        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            app_env="production",
            llm_provider="disabled",
            embedding_provider="fake",
            prometheus_url="",  # explicitly empty
        )
        deps = _build_deps(db_session, settings, "run-1", "inc-1")
        assert isinstance(deps.metrics_tool, UnavailableTool)

    def test_production_missing_prometheus_does_not_crash(
        self, db_session: Session
    ):
        """Worker must handle missing backend gracefully — no crash."""
        from packages.common.settings import Settings

        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            app_env="production",
            llm_provider="disabled",
            embedding_provider="fake",
        )
        deps = _build_deps(db_session, settings, "run-1", "inc-1")
        assert deps.metrics_tool is not None
        assert deps.logs_tool is not None


# ---------------------------------------------------------------------------
# Token safety tests
# ---------------------------------------------------------------------------


class TestTokenSafety:
    def test_token_not_in_agent_deps_repr(self, db_session: Session):
        """AgentDeps repr must not contain secrets or tokens."""
        from packages.common.settings import Settings

        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            app_env="local",
            llm_provider="fake",
            embedding_provider="fake",
        )
        deps = _build_deps(db_session, settings, "run-1", "inc-1")
        repr_str = repr(deps)
        assert "Bearer" not in repr_str
        assert "secret" not in repr_str.lower()


# ---------------------------------------------------------------------------
# Demo path backward compatibility
# ---------------------------------------------------------------------------


class TestDemoPathBackwardCompat:
    def test_demo_path_unchanged(self, db_session: Session):
        """Local/demo path must remain backward compatible."""
        from packages.common.settings import Settings

        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            app_env="local",
            llm_provider="fake",
            embedding_provider="fake",
            prometheus_url="http://localhost:9090",
            loki_url="http://localhost:3100",
        )
        deps = _build_deps(db_session, settings, "run-1", "inc-1")
        assert isinstance(deps, AgentDeps)
        # In demo path, tools should be real (not UnavailableTool)
        from packages.tools.metrics import MetricsTool

        assert isinstance(deps.metrics_tool, MetricsTool)


# ---------------------------------------------------------------------------
# Manual config priority test
# ---------------------------------------------------------------------------


class TestManualConfigPriority:
    def test_env_settings_used_in_demo_path(self, db_session: Session):
        """Demo path should use settings values directly."""
        from packages.common.settings import Settings

        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            app_env="local",
            llm_provider="fake",
            embedding_provider="fake",
            prometheus_url="http://custom-prom:9090",
        )
        deps = _build_deps(db_session, settings, "run-1", "inc-1")
        from packages.tools.metrics import MetricsTool

        assert isinstance(deps.metrics_tool, MetricsTool)
        assert deps.metrics_tool.base_url == "http://custom-prom:9090"
