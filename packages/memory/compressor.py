"""Rules-based context compression — deterministic, no LLM calls.

Compression rules:
- Logs (>20 entries): keep top 3 samples, top error types, top signature.
- Metrics: keep stats only (min/max/avg/p95), drop raw data points.
- Traces: keep top 5 slow spans + top 5 error spans + downstream list.
- Generic: keep first 3 items.
"""

from __future__ import annotations

import json
from typing import Any

from packages.memory.schemas import CompressedContext, ContextBudget
from packages.memory.token_counter import TokenCounter


class Compressor:
    """Deterministic, rules-based evidence compression.

    Compression must be repeatable for CI/evals and must preserve evidence
    lineage. If a future path wants LLM summaries, that belongs in the agent
    layer through an injected summarizer, not inside this package.
    """

    MAX_LOG_SAMPLES: int = 3
    MAX_TRACE_SPANS: int = 10
    REPORT_MAX_EVIDENCE_ITEMS: int = 12
    REPORT_MAX_ACTION_ITEMS: int = 10
    REPORT_MAX_ERROR_ITEMS: int = 5
    REPORT_SUMMARY_CHARS: int = 240

    def __init__(self, token_counter: TokenCounter | None = None) -> None:
        self.token_counter = token_counter or TokenCounter()

    def generate_compression_plan(
        self, evidence: list[dict[str, Any]], budget: ContextBudget
    ) -> list[CompressedContext]:
        """Return compression events without mutating the evidence list."""
        plans: list[CompressedContext] = []
        # Compress by evidence type so metrics/logs/traces retain their own
        # domain-specific summary shape and omitted IDs remain meaningful.
        grouped = self._group_by_type(evidence)
        for etype, items in grouped.items():
            before = self._estimate_tokens(items)
            if not self._needs_compression(items, before, budget):
                continue
            plan = self._compress_by_type(etype, items, before)
            plan.summary = f"[{etype}] {plan.summary}"
            plans.append(plan)
        return plans

    def compress_evidence(
        self, evidence: list[dict[str, Any]], budget: ContextBudget
    ) -> tuple[list[dict[str, Any]], CompressedContext]:
        """Compress evidence and return the new list plus aggregate metadata."""
        before = self._estimate_tokens(evidence)
        plans = self.generate_compression_plan(evidence, budget)
        if not plans:
            return evidence, CompressedContext(
                before_tokens=before, after_tokens=before, compression_ratio=1.0
            )

        compressed: list[dict[str, Any]] = []
        all_retained: list[str] = []
        all_omitted: list[str] = []
        all_notes: list[str] = []

        grouped = self._group_by_type(evidence)
        for etype, items in grouped.items():
            plan = next((p for p in plans if p.summary.startswith(f"[{etype}]")), None)
            if plan is None:
                # Groups below threshold pass through unchanged; they still
                # contribute to final after_tokens and summary.
                compressed.extend(items)
                continue
            comp_items = self._compress_by_type_to_items(etype, items)
            compressed.extend(comp_items)
            all_retained.extend(plan.retained_evidence_ids)
            all_omitted.extend(plan.omitted_evidence_ids)
            all_notes.extend(plan.risk_notes)

        after = self._estimate_tokens(compressed)
        return compressed, CompressedContext(
            summary=self._build_summary(compressed),
            retained_evidence_ids=all_retained,
            omitted_evidence_ids=all_omitted,
            before_tokens=before,
            after_tokens=after,
            compression_ratio=after / max(before, 1),
            risk_notes=all_notes,
        )

    def compress_report_inputs(
        self,
        *,
        evidence: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        errors: list[Any] | None = None,
    ) -> tuple[dict[str, Any], CompressedContext]:
        """Build compact report prompt input while preserving evidence lineage."""
        before_payload = {
            "evidence": evidence,
            "actions": actions,
            "errors": errors or [],
        }
        before = self.token_counter.count_tokens(json.dumps(before_payload, default=str))
        evidence_summaries = [
            self._report_evidence_summary(item)
            for item in evidence[: self.REPORT_MAX_EVIDENCE_ITEMS]
        ]
        retained_ids = [
            evidence_id
            for item in evidence_summaries
            if (evidence_id := item.get("evidence_id"))
        ]
        all_evidence_ids = [
            evidence_id
            for item in evidence
            if (evidence_id := item.get("evidence_id"))
        ]
        omitted_ids = [
            str(evidence_id)
            for evidence_id in all_evidence_ids
            if evidence_id not in retained_ids
        ]
        all_runbook_chunk_ids = _unique_strings(
            chunk_id
            for item in evidence
            for chunk_id in self._report_runbook_chunk_ids(item)
        )
        report_context: dict[str, Any] = {
            "evidence": evidence_summaries,
            "evidence_counts": _count_by_type(evidence),
            "retained_evidence_ids": retained_ids,
            "omitted_evidence_ids": omitted_ids,
            "all_evidence_ids": _unique_strings(str(eid) for eid in all_evidence_ids),
            "runbook_chunk_ids": all_runbook_chunk_ids,
            "actions": [
                self._report_action_summary(action)
                for action in actions[: self.REPORT_MAX_ACTION_ITEMS]
            ],
            "omitted_action_count": max(0, len(actions) - self.REPORT_MAX_ACTION_ITEMS),
            "errors": [
                self._report_error_summary(error)
                for error in (errors or [])[: self.REPORT_MAX_ERROR_ITEMS]
            ],
            "omitted_error_count": max(0, len(errors or []) - self.REPORT_MAX_ERROR_ITEMS),
        }
        after = self.token_counter.count_tokens(json.dumps(report_context, default=str))
        risk_notes: list[str] = []
        if omitted_ids:
            risk_notes.append(
                f"report evidence summaries omitted {len(omitted_ids)} evidence ids"
            )
        if len(actions) > self.REPORT_MAX_ACTION_ITEMS:
            omitted_actions = len(actions) - self.REPORT_MAX_ACTION_ITEMS
            risk_notes.append(
                f"report action trajectory omitted {omitted_actions} actions"
            )
        return report_context, CompressedContext(
            summary=(
                "compressed report inputs "
                f"({len(evidence_summaries)}/{len(evidence)} evidence summaries, "
                f"{min(len(actions), self.REPORT_MAX_ACTION_ITEMS)}/{len(actions)} actions)"
            ),
            retained_evidence_ids=[str(eid) for eid in retained_ids],
            omitted_evidence_ids=omitted_ids,
            before_tokens=before,
            after_tokens=after,
            compression_ratio=after / max(before, 1),
            risk_notes=risk_notes,
        )

    # -- internal -------------------------------------------------------

    def _needs_compression(
        self, items: list[dict[str, Any]], token_estimate: int, budget: ContextBudget
    ) -> bool:
        """Return whether an evidence group should be summarized."""
        etype = self._type_of(items)
        if etype == "log" and len(items) > 20:
            return True
        if token_estimate > int(budget.evidence * 0.8):
            return True
        return False

    def _compress_by_type(
        self, etype: str, items: list[dict[str, Any]], before: int
    ) -> CompressedContext:
        """Build one compression event for a single evidence type."""
        comp = self._compress_by_type_to_items(etype, items)
        after = self._estimate_tokens(comp)
        # retained/omitted IDs are the audit hook that lets reports explain
        # which raw evidence was summarized away.
        retained = [i.get("evidence_id", "") for i in comp if i.get("evidence_id")]
        omitted = [
            i.get("evidence_id", "")
            for i in items
            if i.get("evidence_id") and i.get("evidence_id") not in retained
        ]
        return CompressedContext(
            summary=self._build_summary(comp),
            retained_evidence_ids=retained,
            omitted_evidence_ids=omitted,
            before_tokens=before,
            after_tokens=after,
            compression_ratio=after / max(before, 1),
            risk_notes=self._risk_notes(etype, len(items), len(comp)),
        )

    def _compress_by_type_to_items(
        self, etype: str, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Apply the type-specific deterministic compression strategy."""
        if etype == "log":
            return self._compress_logs(items)
        if etype == "metric":
            return self._compress_metrics(items)
        if etype == "trace":
            return self._compress_traces(items)
        return self._compress_generic(items)

    def _compress_logs(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collapse many log samples into error counts, signature, and samples."""
        item = items[0] if items else {}
        data = self._payload(item)
        raw_samples = data.get("samples") or data.get("log_samples") or []
        samples = raw_samples[: self.MAX_LOG_SAMPLES] if isinstance(raw_samples, list) else []
        out = self._base_item(item, "log")
        out.update(
            {
                "top_error_type": data.get("top_error_type", data.get("top_error", "")),
                "top_stack_signature": data.get("top_stack_signature", ""),
                "line_count": data.get("line_count", data.get("total_lines", len(items))),
                "error_counts": data.get("error_counts", data.get("error_type_counts", {})),
                "samples": samples,
                "omitted_count": max(0, len(raw_samples) - self.MAX_LOG_SAMPLES)
                if isinstance(raw_samples, list)
                else 0,
            }
        )
        return [out]

    def _compress_metrics(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop raw metric points while preserving aggregate statistics."""
        result: list[dict[str, Any]] = []
        for item in items:
            data = self._payload(item)
            stats = data.get("stats", {}) if isinstance(data.get("stats"), dict) else {}
            out = self._base_item(item, "metric")
            out.update(
                {
                    "metric_type": item.get("metric_type") or data.get("metric_type", ""),
                    "service": item.get("service") or data.get("service", ""),
                    "stats": {
                        k: stats.get(k)
                        for k in ("min", "max", "avg", "p95", "first", "last", "change_ratio")
                        if k in stats
                    },
                }
            )
            result.append(out)
        return result

    def _compress_traces(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep representative slow/error spans and downstream services."""
        item = items[0] if items else {}
        data = self._payload(item)
        raw_slow = data.get("slow_spans") or []
        raw_errors = data.get("error_spans") or []
        slow = raw_slow[: self.MAX_TRACE_SPANS // 2] if isinstance(raw_slow, list) else []
        errors = raw_errors[: self.MAX_TRACE_SPANS // 2] if isinstance(raw_errors, list) else []
        downstream = data.get("downstream_services")
        if not isinstance(downstream, list):
            downstream = _downstream_from_spans(slow + errors)
        out = self._base_item(item, "trace")
        out.update(
            {
                "duration_p95_ms": data.get("duration_p95_ms"),
                "downstream_services": downstream,
                "slow_spans": slow,
                "error_spans": errors,
                "omitted_count": max(
                    0,
                    (len(raw_slow) if isinstance(raw_slow, list) else 0)
                    + (len(raw_errors) if isinstance(raw_errors, list) else 0)
                    - self.MAX_TRACE_SPANS,
                ),
            }
        )
        return [out]

    def _compress_generic(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fallback for unknown evidence types: keep a small deterministic prefix."""
        return items[:3]

    def _report_evidence_summary(self, item: dict[str, Any]) -> dict[str, Any]:
        """Return a report-safe evidence summary with no raw samples/payloads."""
        payload = self._payload(item)
        etype = str(item.get("type", payload.get("type", "")))
        summary = self._report_summary_text(etype, item, payload)
        result: dict[str, Any] = {
            "evidence_id": item.get("evidence_id", payload.get("evidence_id", "")),
            "type": etype,
            "source": item.get("source", payload.get("source", "")),
            "source_id": item.get("source_id", payload.get("source_id", "")),
            "source_path": _source_path(item),
            "summary": _limit_text(str(summary), self.REPORT_SUMMARY_CHARS),
            "status": item.get("status", payload.get("status", "")),
            "service": item.get("service", payload.get("service", "")),
            "timestamp": item.get("timestamp", payload.get("timestamp", "")),
            "runbook_chunk_ids": self._report_runbook_chunk_ids(item),
        }
        return {key: value for key, value in result.items() if value not in ("", [], None)}

    def _report_summary_text(
        self,
        etype: str,
        item: dict[str, Any],
        payload: dict[str, Any],
    ) -> str:
        if etype == "log":
            error_counts = payload.get("error_counts") or payload.get("error_type_counts") or {}
            line_count = payload.get("line_count") or payload.get("total_lines") or ""
            error_type_count = len(error_counts) if isinstance(error_counts, dict) else 0
            total_error_count = (
                sum(value for value in error_counts.values() if isinstance(value, int | float))
                if isinstance(error_counts, dict)
                else ""
            )
            return _limit_text(
                "log summary "
                f"line_count={line_count} "
                f"error_type_count={error_type_count} "
                f"total_error_count={total_error_count}",
                self.REPORT_SUMMARY_CHARS,
            )
        explicit = item.get("summary")
        if isinstance(explicit, str) and explicit:
            return _limit_text(explicit, self.REPORT_SUMMARY_CHARS)
        if etype == "metric":
            stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
            return _limit_text(
                f"metric summary type={payload.get('metric_type', '')} stats={stats}",
                self.REPORT_SUMMARY_CHARS,
            )
        if etype == "trace":
            return _limit_text(
                "trace summary "
                f"duration_p95_ms={payload.get('duration_p95_ms', '')} "
                f"downstream={payload.get('downstream_services', [])}",
                self.REPORT_SUMMARY_CHARS,
            )
        fallback = payload.get("summary") or payload.get("status") or ""
        return _limit_text(str(fallback), self.REPORT_SUMMARY_CHARS)

    def _report_action_summary(self, action: dict[str, Any]) -> dict[str, Any]:
        """Return a compact action trajectory entry for report prompting."""
        return {
            key: value
            for key, value in {
                "action_id": action.get("action_id", ""),
                "type": action.get("type", ""),
                "target": action.get("target", ""),
                "risk_level": action.get("risk_level", action.get("risk_hint", "")),
                "status": action.get("status", ""),
                "reason": _limit_text(str(action.get("reason", "")), self.REPORT_SUMMARY_CHARS),
            }.items()
            if value not in ("", None)
        }

    @staticmethod
    def _report_error_summary(error: Any) -> dict[str, Any]:
        if not isinstance(error, dict):
            return {"error_present": True}
        return {
            key: value
            for key, value in {
                "node": error.get("node", ""),
                "type": error.get("type", error.get("error_type", "")),
                "status": error.get("status", ""),
                "error_present": bool(error.get("error")),
            }.items()
            if value not in ("", None, False)
        } or {"error_present": True}

    @staticmethod
    def _report_runbook_chunk_ids(item: dict[str, Any]) -> list[str]:
        chunk_ids: list[str] = []
        for key in ("runbook_chunk_ids", "runbook_chunks"):
            value = item.get(key)
            if isinstance(value, list):
                chunk_ids.extend(str(v) for v in value if v)
        for source in (item, item.get("payload")):
            if isinstance(source, dict):
                chunk_id = source.get("chunk_id")
                if chunk_id:
                    chunk_ids.append(str(chunk_id))
                metadata = source.get("metadata")
                if isinstance(metadata, dict) and metadata.get("chunk_id"):
                    chunk_ids.append(str(metadata["chunk_id"]))
        return _unique_strings(chunk_ids)

    @staticmethod
    def _payload(item: dict[str, Any]) -> dict[str, Any]:
        """Return payload dict when present, otherwise the item itself."""
        payload = item.get("payload")
        return payload if isinstance(payload, dict) else item

    @staticmethod
    def _base_item(item: dict[str, Any] | None, etype: str) -> dict[str, Any]:
        """Create a compact item while preserving traceability fields."""
        if item is None:
            return {"type": etype, "source": "unknown"}
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        out: dict[str, Any] = {
            "type": etype,
            "source": item.get("source") or payload.get("source", "unknown"),  # type: ignore[union-attr]
        }
        for key in (
            "evidence_id", "source_id", "title",
            "summary", "status", "service", "timestamp",
        ):
            value = item.get(key, payload.get(key))  # type: ignore[union-attr]
            if value not in (None, ""):
                out[key] = value
        return out

    @staticmethod
    def _group_by_type(evidence: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """Group evidence by its public ``type`` field."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in evidence:
            etype = item.get("type", "unknown")
            grouped.setdefault(etype, []).append(item)
        return grouped

    @staticmethod
    def _type_of(items: list[dict[str, Any]]) -> str:
        return items[0].get("type", "unknown") if items else "unknown"

    def _estimate_tokens(self, items: list[dict[str, Any]]) -> int:
        """Estimate serialized token usage for a list of evidence items."""
        try:
            text = json.dumps(items, default=str)
        except (TypeError, ValueError):
            text = str(items)
        return self.token_counter.count_tokens(text)

    @staticmethod
    def _risk_notes(etype: str, before: int, after: int) -> list[str]:
        notes: list[str] = []
        if before > 20 and after <= 3:
            notes.append(f"{etype} evidence heavily compressed: {before} -> {after} items")
        if after == 0 and before > 0:
            notes.append(f"all {etype} evidence omitted")
        return notes

    @staticmethod
    def _build_summary(items: list[dict[str, Any]]) -> str:
        if not items:
            return "no evidence"
        types = {i.get("type", "?") for i in items}
        return f"compressed evidence ({len(items)} items, types: {', '.join(sorted(types))})"


def _downstream_from_spans(spans: list[dict[str, Any]]) -> list[str]:
    """Derive a stable downstream service list from span summaries."""
    services: list[str] = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        service = span.get("downstream_service")
        if isinstance(service, str) and service and service not in services:
            services.append(service)
    return services


def _source_path(evidence: dict[str, Any]) -> object:
    payload = evidence.get("payload")
    if isinstance(payload, dict):
        nested = payload.get("payload")
        if isinstance(nested, dict) and nested.get("source_path"):
            return nested.get("source_path")
        return payload.get("source_path", "")
    return evidence.get("source_path", "")


def _limit_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)] + "..."


def _count_by_type(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        etype = str(item.get("type", "unknown"))
        counts[etype] = counts.get(etype, 0) + 1
    return dict(sorted(counts.items()))


def _unique_strings(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value)
        if text and text not in result:
            result.append(text)
    return result
