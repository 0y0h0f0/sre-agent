"""LLM profile configuration helpers.

Profiles are construction-time options for provider adapters. They do not
select providers, change provider allow rules, or alter redaction behavior.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from packages.common.errors import ValidationAppError
from packages.common.settings import Settings

DEFAULT_PROFILE = "default"
FAST_JSON_PROFILE = "fast_json"
DIAGNOSE_REASONING_PROFILE = "diagnose_reasoning"
REPORT_PROFILE = "report"

KNOWN_PROFILES = frozenset(
    {
        DEFAULT_PROFILE,
        FAST_JSON_PROFILE,
        DIAGNOSE_REASONING_PROFILE,
        REPORT_PROFILE,
    }
)


@dataclass(frozen=True)
class LLMProfile:
    name: str
    model: str
    max_tokens: int
    temperature: float
    reasoning_effort: str


def resolve_llm_profile(
    settings: Settings,
    profile: str | None = None,
    *,
    aliases: Sequence[str] | None = None,
) -> LLMProfile:
    """Resolve model/options for a profile without changing provider selection."""

    name = normalize_profile_name(profile)
    override_keys = _override_keys(name, aliases)
    model_overrides = _parse_assignment_map(
        settings.llm_node_model_overrides,
        setting_name="LLM_NODE_MODEL_OVERRIDES",
    )
    max_token_overrides = _parse_int_assignment_map(
        settings.llm_node_max_tokens,
        setting_name="LLM_NODE_MAX_TOKENS",
    )

    return LLMProfile(
        name=name,
        model=_resolve_model(settings, name, model_overrides, override_keys),
        max_tokens=_resolve_max_tokens(settings, name, max_token_overrides, override_keys),
        temperature=settings.llm_temperature,
        reasoning_effort=settings.llm_reasoning_effort,
    )


def normalize_profile_name(profile: str | None) -> str:
    if profile is None:
        return DEFAULT_PROFILE
    normalized = profile.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or DEFAULT_PROFILE


def _resolve_model(
    settings: Settings,
    profile: str,
    model_overrides: dict[str, str],
    override_keys: Sequence[str],
) -> str:
    for key in override_keys:
        override = model_overrides.get(key, "").strip()
        if override:
            return override
    dedicated = _dedicated_profile_model(settings, profile).strip()
    if dedicated:
        return dedicated
    return settings.llm_model


def _resolve_max_tokens(
    settings: Settings,
    profile: str,
    max_token_overrides: dict[str, int],
    override_keys: Sequence[str],
) -> int:
    for key in override_keys:
        override = max_token_overrides.get(key)
        if override:
            return override
    dedicated = _dedicated_profile_max_tokens(settings, profile)
    if dedicated > 0:
        return dedicated
    return settings.llm_max_tokens


def _dedicated_profile_model(settings: Settings, profile: str) -> str:
    if profile == FAST_JSON_PROFILE:
        return settings.llm_fast_json_model
    if profile == DIAGNOSE_REASONING_PROFILE:
        return settings.llm_diagnose_reasoning_model
    if profile == REPORT_PROFILE:
        return settings.llm_report_model
    return ""


def _dedicated_profile_max_tokens(settings: Settings, profile: str) -> int:
    if profile == FAST_JSON_PROFILE:
        return settings.llm_fast_json_max_tokens
    if profile == DIAGNOSE_REASONING_PROFILE:
        return settings.llm_diagnose_reasoning_max_tokens
    if profile == REPORT_PROFILE:
        return settings.llm_report_max_tokens
    return 0


def _override_keys(profile: str, aliases: Sequence[str] | None) -> list[str]:
    keys: list[str] = []
    for value in aliases or ():
        key = normalize_profile_name(value)
        if key != DEFAULT_PROFILE and key not in keys:
            keys.append(key)
    if profile not in keys:
        keys.append(profile)
    return keys


def _parse_int_assignment_map(raw: str, *, setting_name: str) -> dict[str, int]:
    parsed = _parse_assignment_map(raw, setting_name=setting_name)
    result: dict[str, int] = {}
    for key, value in parsed.items():
        try:
            token_count = int(value)
        except ValueError as exc:
            raise ValidationAppError(
                f"{setting_name} values must be positive integers",
                details={"setting": setting_name, "key": key},
            ) from exc
        if token_count <= 0:
            raise ValidationAppError(
                f"{setting_name} values must be positive integers",
                details={"setting": setting_name, "key": key},
            )
        result[key] = token_count
    return result


def _parse_assignment_map(raw: str, *, setting_name: str) -> dict[str, str]:
    value = raw.strip()
    if not value:
        return {}
    if value.startswith("{"):
        return _parse_json_assignment_map(value, setting_name=setting_name)
    return _parse_comma_assignment_map(value, setting_name=setting_name)


def _parse_json_assignment_map(raw: str, *, setting_name: str) -> dict[str, str]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationAppError(
            f"{setting_name} must be a JSON object or comma-separated key=value pairs",
            details={"setting": setting_name},
        ) from exc
    if not isinstance(parsed, dict):
        raise ValidationAppError(
            f"{setting_name} must be a JSON object",
            details={"setting": setting_name},
        )
    return _normalize_assignment_items(parsed.items(), setting_name=setting_name)


def _parse_comma_assignment_map(raw: str, *, setting_name: str) -> dict[str, str]:
    items: list[tuple[str, str]] = []
    for entry in raw.split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        separator = "=" if "=" in candidate else ":"
        if separator not in candidate:
            raise ValidationAppError(
                f"{setting_name} must use key=value pairs",
                details={"setting": setting_name},
            )
        key, value = candidate.split(separator, 1)
        items.append((key, value))
    return _normalize_assignment_items(items, setting_name=setting_name)


def _normalize_assignment_items(
    items: Any,
    *,
    setting_name: str,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_key, raw_value in items:
        key = normalize_profile_name(str(raw_key))
        value = str(raw_value).strip()
        if key == DEFAULT_PROFILE or not value:
            continue
        result[key] = value
    return result
