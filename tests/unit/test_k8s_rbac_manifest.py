"""Tests for Kubernetes RBAC required by discovery."""

from __future__ import annotations

from pathlib import Path


def test_discovery_rbac_includes_ingresses_and_configmaps() -> None:
    manifest = Path("deploy/k8s/base/rbac.yaml").read_text(encoding="utf-8")

    assert "configmaps" in manifest
    assert 'apiGroups: ["networking.k8s.io"]' in manifest
    assert 'resources: ["ingresses"]' in manifest
    assert 'verbs: ["get", "list", "watch"]' in manifest
