"""Tests for Kubernetes RBAC required by discovery."""

from __future__ import annotations

from pathlib import Path


def test_base_configmap_keeps_safe_defaults() -> None:
    manifest = Path("deploy/k8s/base/configmap.yaml").read_text(encoding="utf-8")

    assert 'LLM_PROVIDER: "fake"' in manifest
    assert 'K8S_BACKEND: "fixture"' in manifest
    assert 'EXECUTOR_BACKEND: "fixture"' in manifest
    assert 'M9_EXTENSIONS_ENABLED: "false"' in manifest
    assert 'LLM_EXTERNAL_PROVIDER_ALLOWED: "false"' in manifest
    assert 'API_KEY_AUTH_ENABLED: "true"' in manifest
    assert 'CORS_ALLOW_ORIGINS: "*"' not in manifest


def test_k8s_secret_example_documents_smtp_delivery_settings() -> None:
    example = Path("deploy/k8s/base/.env.secret.example").read_text(encoding="utf-8")

    assert "SMTP_HOST 为空时系统会把邮件记录为 skipped" in example
    assert "# SMTP_HOST=smtp.example.com" in example
    assert "# SMTP_FROM=sre-agent@example.com" in example
    assert "# SRE_EMAIL_LIST=sre@example.com,oncall@example.com" in example
    assert "# WEB_BASE_URL=https://sre-agent.example.com" in example


def test_k8s_readme_points_secret_configuration_at_env_secret() -> None:
    readme = Path("deploy/k8s/README.md").read_text(encoding="utf-8")

    assert "Kustomize 通过 `secretGenerator` 读取 `base/.env.secret`" in readme
    assert "`base/secret.yaml` 只是字段参考" in readme
    assert "cp deploy/k8s/base/.env.secret.example deploy/k8s/base/.env.secret" in readme
    assert "SMTP_HOST=smtp.example.com" in readme


def test_discovery_rbac_includes_ingresses_and_configmaps() -> None:
    manifest = Path("deploy/k8s/base/rbac.yaml").read_text(encoding="utf-8")

    assert "configmaps" in manifest
    assert "statefulsets" in manifest
    assert 'apiGroups: ["networking.k8s.io"]' in manifest
    assert 'resources: ["ingresses"]' in manifest
    assert 'verbs: ["get", "list", "watch"]' in manifest


def test_base_rbac_is_namespace_scoped_and_read_only() -> None:
    manifest = Path("deploy/k8s/base/rbac.yaml").read_text(encoding="utf-8")

    assert "kind: Role\n" in manifest
    assert "kind: RoleBinding\n" in manifest
    assert "kind: ClusterRole" not in manifest
    assert "kind: ClusterRoleBinding" not in manifest
    assert '"patch"' not in manifest
    assert '"create"' not in manifest
    assert "deployments/rollback" not in manifest
