from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATASET_ROOT = Path(__file__).resolve().parent


@dataclass(slots=True)
class EvalCase:
    case_id: str
    incident_type: str
    alert: dict[str, Any]
    fixtures: dict[str, Any]
    expected: dict[str, Any]
    source_path: str


def load_suite_cases(suite: str) -> list[EvalCase]:
    if suite not in {"smoke", "full"}:
        msg = f"unknown suite: {suite}"
        raise ValueError(msg)

    if suite == "smoke":
        paths = sorted((DATASET_ROOT / suite).glob("*.json"))
        return [_load_case_file(path) for path in paths]

    manifest = DATASET_ROOT / suite / "cases.json"
    if manifest.exists():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            msg = f"{manifest.as_posix()} must contain a JSON array"
            raise ValueError(msg)
        return [_load_case_payload(case, manifest) for case in payload]

    paths = sorted((DATASET_ROOT / suite).glob("*.json"))
    return [_load_case_file(path) for path in paths]


def suite_dataset_version(suite: str) -> str:
    return f"{suite}-v1"


def _load_case_file(path: Path) -> EvalCase:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return _load_case_payload(payload, path)


def _load_case_payload(payload: dict[str, Any], source: Path) -> EvalCase:
    return EvalCase(
        case_id=str(payload["case_id"]),
        incident_type=str(payload["incident_type"]),
        alert=dict(payload["alert"]),
        fixtures=dict(payload["fixtures"]),
        expected=dict(payload["expected"]),
        source_path=source.as_posix(),
    )
