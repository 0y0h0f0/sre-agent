"""EffectiveConfig — merged runtime configuration with priority chain.

Priority: env > active override > profile > published EffectiveConfigVersion > safe default.

Manual config always wins over discovery. Discovery only fills gaps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packages.common.settings import Settings


@dataclass
class BackendConfig:
    """Resolved config for a single observability backend."""

    url: str | None = None
    auth_type: str = "none"
    source: str = "default"  # env | override | profile | published | default
    degraded: bool = False
    degraded_reason: str | None = None


@dataclass
class EffectiveConfig:
    """Merged effective runtime configuration.

    This is what the worker uses to construct AgentDeps with live backend URLs,
    auth configs, and service label mappings.
    """

    prometheus: BackendConfig = field(default_factory=BackendConfig)
    loki: BackendConfig = field(default_factory=BackendConfig)
    jaeger: BackendConfig = field(default_factory=BackendConfig)
    alertmanager: BackendConfig = field(default_factory=BackendConfig)
    metrics_service_label: str = "service"
    logs_service_label: str = "service"
    source: str = "demo"  # demo | operator
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_demo_sources(cls, settings: Settings) -> EffectiveConfig:
        """Build config for local demo/CI using settings defaults."""
        return cls(
            prometheus=BackendConfig(
                url=settings.prometheus_url, source="default"
            ),
            loki=BackendConfig(url=settings.loki_url, source="default"),
            jaeger=BackendConfig(url=settings.jaeger_url, source="default"),
            alertmanager=BackendConfig(
                url=settings.alertmanager_url, source="default"
            ),
            metrics_service_label=settings.metrics_service_label,
            logs_service_label=settings.logs_service_label,
            source="demo",
        )

    @classmethod
    def from_operator_sources(
        cls,
        settings: Settings,
        *,
        profile_overrides: dict[str, str] | None = None,
        active_overrides: list[dict[str, Any]] | None = None,
        published_config: dict[str, Any] | None = None,
    ) -> EffectiveConfig:
        """Build config for production using full priority chain.

        Priority: env > active override > profile > published > safe default.
        """
        warnings: list[str] = []
        profile = profile_overrides or {}

        def _resolve(
            backend_key: str,
            settings_attr: str,
            default_url: str | None = None,
        ) -> BackendConfig:
            # 1. env (from Settings — highest priority)
            env_url: str | None = getattr(settings, settings_attr, None)
            if env_url and env_url != default_url:
                return BackendConfig(url=env_url, source="env")

            # 2. active overrides (pre-filtered: not expired, not revoked)
            if active_overrides:
                for ov in active_overrides:
                    if ov.get("backend_type") == backend_key:
                        return BackendConfig(
                            url=ov.get("url"),
                            auth_type=ov.get("auth_type", "none"),
                            source="override",
                        )

            # 3. profile
            if backend_key in profile:
                return BackendConfig(
                    url=profile[backend_key], source="profile"
                )

            # 4. published config
            if published_config:
                pub_url = published_config.get(f"{backend_key}_url")
                if pub_url:
                    return BackendConfig(url=pub_url, source="published")

            # 5. safe default — production does NOT fall back to localhost
            if settings.app_env == "production":
                return BackendConfig(
                    url=None,
                    source="default",
                    degraded=True,
                    degraded_reason=(
                        f"No configured URL for {backend_key} — "
                        "production does not fall back to localhost"
                    ),
                )

            # Local can use localhost defaults.
            if default_url:
                return BackendConfig(url=default_url, source="default")
            return BackendConfig(url=None, source="default")

        def _resolve_with_warning(
            backend_key: str, settings_attr: str, default_url: str | None
        ) -> BackendConfig:
            cfg = _resolve(backend_key, settings_attr, default_url)
            if cfg.degraded:
                warnings.append(f"{backend_key}: {cfg.degraded_reason}")
            return cfg

        return cls(
            prometheus=_resolve_with_warning(
                "prometheus", "prometheus_url", "http://localhost:9090"
            ),
            loki=_resolve_with_warning(
                "loki", "loki_url", "http://localhost:3100"
            ),
            jaeger=_resolve_with_warning(
                "jaeger", "jaeger_url", "http://localhost:16686"
            ),
            alertmanager=_resolve_with_warning(
                "alertmanager",
                "alertmanager_url",
                "http://localhost:9093",
            ),
            metrics_service_label=settings.metrics_service_label,
            logs_service_label=settings.logs_service_label,
            source="operator",
            warnings=warnings,
        )

    def has_unresolved_required_sources(self) -> bool:
        """Check if any required backend is missing a URL."""
        backends = [
            self.prometheus,
            self.loki,
            self.jaeger,
            self.alertmanager,
        ]
        return any(cfg.url is None for cfg in backends)

    def unresolved_sources(self) -> list[str]:
        """Return list of backend names missing URLs."""
        _map = {
            "prometheus": self.prometheus,
            "loki": self.loki,
            "jaeger": self.jaeger,
            "alertmanager": self.alertmanager,
        }
        return [name for name, cfg in _map.items() if cfg.url is None]
