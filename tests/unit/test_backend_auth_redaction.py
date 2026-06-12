"""Tests for PR 0.9: BackendAuthConfig redaction."""

from __future__ import annotations

from packages.common.backend_auth import (
    RuntimeBackendAuthConfig,
    resolve_secret_ref,
)


class TestRedaction:
    def test_redacted_does_not_include_token(self):
        runtime = RuntimeBackendAuthConfig(
            auth_type="bearer", token="super-secret-token"
        )
        redacted = runtime.redacted()
        assert redacted.has_token is True
        safe = redacted.to_safe_dict()
        assert "token" not in safe
        assert safe["has_token"] is True

    def test_redacted_does_not_include_password(self):
        runtime = RuntimeBackendAuthConfig(
            auth_type="basic", username="admin", password="secret-pw"
        )
        redacted = runtime.redacted()
        assert redacted.has_password is True
        safe = redacted.to_safe_dict()
        assert "password" not in safe

    def test_runtime_config_has_raw_secrets(self):
        runtime = RuntimeBackendAuthConfig(auth_type="bearer", token="abc123")
        assert runtime.token == "abc123"

    def test_to_safe_dict_excludes_secrets(self):
        runtime = RuntimeBackendAuthConfig(
            auth_type="mtls",
            cert_file="/path/to/cert.pem",
            key_file="/path/to/key.pem",
            ca_file="/path/to/ca.pem",
        )
        safe = runtime.redacted().to_safe_dict()
        assert "key_file" not in safe
        assert safe["auth_type"] == "mtls"
        assert safe["cert_ref"] == "/path/to/cert.pem"

    def test_default_auth_type_is_none(self):
        runtime = RuntimeBackendAuthConfig()
        assert runtime.auth_type == "none"
        assert runtime.redacted().has_token is False
        assert runtime.redacted().has_password is False


class TestSecretRef:
    def test_resolve_env_secret_ref(self, monkeypatch):
        monkeypatch.setenv("TEST_TOKEN", "my-test-token")
        result = resolve_secret_ref("env:TEST_TOKEN")
        assert result == "my-test-token"

    def test_resolve_missing_env_var(self):
        result = resolve_secret_ref("env:NONEXISTENT_VAR_12345")
        assert result is None

    def test_non_env_ref_returns_none(self):
        result = resolve_secret_ref("vault:secret/path")
        assert result is None
