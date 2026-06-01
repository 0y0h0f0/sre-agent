from __future__ import annotations


def test_runbook_search_response_fields_are_stable(client, tmp_path) -> None:
    base = tmp_path / "runbooks"
    base.mkdir()
    (base / "pod.md").write_text(
        """---
service: checkout
incident_type: pod_restart_loop
severity: P1
owner: payment-team
updated_at: 2026-05-31
---
# Pod Restart Loop

## Detection
CrashLoopBackOff, OOMKilled, and readiness failures identify pod restart loops.
""",
        encoding="utf-8",
    )
    client.post("/api/runbooks/ingest", json={"path": str(base)})

    response = client.get(
        "/api/runbooks/search",
        params={"q": "pod restart loop oomkilled", "top_k": 1},
    )

    assert response.status_code == 200
    item = response.json()[0]
    assert set(item) == {"chunk_id", "source_path", "title", "excerpt", "score", "metadata"}
    assert set(item["metadata"]).issuperset(
        {"service", "incident_type", "severity", "owner", "updated_at", "chunk_index"}
    )
