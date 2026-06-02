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
    """Deterministic, rules-based evidence compression."""

    MAX_LOG_SAMPLES: int = 3
    MAX_TRACE_SPANS: int = 10

    def __init__(self, token_counter: TokenCounter | None = None) -> None:
        self.token_counter = token_counter or TokenCounter()

    def generate_compression_plan(
        self, evidence: list[dict[str, Any]], budget: ContextBudget
    ) -> list[CompressedContext]:
        plans: list[CompressedContext] = []
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

    # -- internal -------------------------------------------------------

    def _needs_compression(
        self, items: list[dict[str, Any]], token_estimate: int, budget: ContextBudget
    ) -> bool:
        etype = self._type_of(items)
        if etype == "log" and len(items) > 20:
            return True
        if token_estimate > int(budget.evidence * 0.8):
            return True
        return False

    def _compress_by_type(
        self, etype: str, items: list[dict[str, Any]], before: int
    ) -> CompressedContext:
        comp = self._compress_by_type_to_items(etype, items)
        after = self._estimate_tokens(comp)
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
        if etype == "log":
            return self._compress_logs(items)
        if etype == "metric":
            return self._compress_metrics(items)
        if etype == "trace":
            return self._compress_traces(items)
        return self._compress_generic(items)

    def _compress_logs(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        return items[:3]

    @staticmethod
    def _payload(item: dict[str, Any]) -> dict[str, Any]:
        payload = item.get("payload")
        return payload if isinstance(payload, dict) else item

    @staticmethod
    def _base_item(item: dict[str, Any], etype: str) -> dict[str, Any]:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        out: dict[str, Any] = {
            "type": etype,
            "source": item.get("source") or payload.get("source", "unknown"),
        }
        for key in ("evidence_id", "source_id", "title", "summary", "status", "service", "timestamp"):
            value = item.get(key, payload.get(key))
            if value not in (None, ""):
                out[key] = value
        return out

    @staticmethod
    def _group_by_type(evidence: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in evidence:
            etype = item.get("type", "unknown")
            grouped.setdefault(etype, []).append(item)
        return grouped

    @staticmethod
    def _type_of(items: list[dict[str, Any]]) -> str:
        return items[0].get("type", "unknown") if items else "unknown"

    def _estimate_tokens(self, items: list[dict[str, Any]]) -> int:
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
    services: list[str] = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        service = span.get("downstream_service")
        if isinstance(service, str) and service and service not in services:
            services.append(service)
    return services
