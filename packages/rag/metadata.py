"""Runbook Markdown front matter parsing."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

REQUIRED_FRONT_MATTER_FIELDS = ("service", "incident_type", "severity", "owner", "updated_at")
OPTIONAL_FRONT_MATTER_FIELDS = ("language",)
FRONT_MATTER_RE = re.compile(
    r"\A---[ \t]*\n(?P<front_matter>.*?)\n---[ \t]*\n?(?P<body>.*)\Z",
    re.S,
)
H1_RE = re.compile(r"^#\s+(?P<title>.+?)\s*$", re.M)


class RunbookMetadataError(ValueError):
    """Raised when a runbook document has invalid metadata."""


class RunbookMetadata(BaseModel):
    service: str = Field(min_length=1)
    incident_type: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    updated_at: date
    language: str = "en"
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("service", "incident_type", "severity", "owner")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "metadata field must not be blank"
            raise ValueError(msg)
        return stripped

    @field_validator("incident_type")
    @classmethod
    def _normalize_incident_type(cls, value: str) -> str:
        return value.strip().lower()

    def storage_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        extra = payload.pop("extra", {})
        payload.update(extra)
        return payload


class RunbookDocument(BaseModel):
    source_path: str
    document_id: str
    document_hash: str
    title: str
    metadata: RunbookMetadata
    body: str


def parse_runbook_markdown(text: str, *, source_path: str) -> RunbookDocument:
    match = FRONT_MATTER_RE.match(text)
    if match is None:
        raise RunbookMetadataError(f"{source_path}: runbook front matter is required")

    raw_metadata = _parse_simple_yaml(match.group("front_matter"), source_path=source_path)
    missing = [field for field in REQUIRED_FRONT_MATTER_FIELDS if field not in raw_metadata]
    if missing:
        raise RunbookMetadataError(
            f"{source_path}: missing runbook metadata fields: {', '.join(missing)}"
        )

    metadata = _metadata_from_mapping(raw_metadata, source_path=source_path)
    body = match.group("body").strip()
    title = _extract_title(body) or Path(source_path).stem.replace("-", " ").title()
    document_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    document_id = (
        "doc_" + hashlib.sha256(f"{source_path}:{document_hash}".encode()).hexdigest()[:16]
    )
    return RunbookDocument(
        source_path=source_path,
        document_id=document_id,
        document_hash=document_hash,
        title=title,
        metadata=metadata,
        body=body,
    )


def _parse_simple_yaml(text: str, *, source_path: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for line_number, line in enumerate(text.splitlines(), start=2):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise RunbookMetadataError(f"{source_path}:{line_number}: invalid front matter line")
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise RunbookMetadataError(f"{source_path}:{line_number}: empty metadata key")
        fields[key] = _strip_yaml_scalar(value)
    return fields


def _strip_yaml_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _metadata_from_mapping(raw: dict[str, Any], *, source_path: str) -> RunbookMetadata:
    known = {field: raw[field] for field in REQUIRED_FRONT_MATTER_FIELDS}
    for field in OPTIONAL_FRONT_MATTER_FIELDS:
        if field in raw:
            known[field] = raw[field]
    extra = {
        key: value
        for key, value in raw.items()
        if key not in REQUIRED_FRONT_MATTER_FIELDS and key not in OPTIONAL_FRONT_MATTER_FIELDS
    }
    try:
        return RunbookMetadata(**known, extra=extra)
    except ValueError as exc:
        raise RunbookMetadataError(f"{source_path}: invalid runbook metadata: {exc}") from exc


def _extract_title(body: str) -> str | None:
    match = H1_RE.search(body)
    if match is None:
        return None
    return match.group("title").strip()
