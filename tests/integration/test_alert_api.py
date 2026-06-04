from __future__ import annotations


def test_create_alert_enqueues_agent_run(
    client, fake_enqueue, fake_notification_enqueue, alert_payload
) -> None:
    response = client.post("/api/alerts", json=alert_payload, headers={"X-Request-Id": "req-test"})

    assert response.status_code == 202
    assert response.headers["X-Request-Id"] == "req-test"
    body = response.json()
    assert body["incident_id"].startswith("inc_")
    assert body["agent_run_id"].startswith("run_")
    assert body["celery_task_id"] == "task-1"
    assert body["status"] == "queued"
    assert body["deduplicated"] is False
    assert fake_enqueue.calls == [(body["incident_id"], body["agent_run_id"])]
    assert fake_notification_enqueue.calls == [
        ("new_incident", {"incident_id": body["incident_id"]})
    ]


def test_duplicate_fingerprint_returns_existing_incident(
    client, fake_enqueue, fake_notification_enqueue, alert_payload
) -> None:
    first = client.post("/api/alerts", json=alert_payload).json()
    second = client.post("/api/alerts", json=alert_payload).json()

    assert second["incident_id"] == first["incident_id"]
    assert second["agent_run_id"] == first["agent_run_id"]
    assert second["deduplicated"] is True
    assert len(fake_enqueue.calls) == 1
    assert len(fake_notification_enqueue.calls) == 1


def test_alertmanager_webhook_payload_normalizes_and_preserves_raw(client) -> None:
    payload = {
        "receiver": "sre",
        "status": "firing",
        "groupKey": "am-group-1",
        "commonLabels": {
            "alertname": "DatabaseConnectionExhaustion",
            "service": "checkout",
            "severity": "critical",
        },
        "commonAnnotations": {"summary": "database pool exhausted"},
        "alerts": [
            {
                "fingerprint": "am-fp-1",
                "startsAt": "2026-06-01T00:00:00Z",
                "labels": {"instance": "db-1"},
                "annotations": {"description": "too many connections"},
            }
        ],
    }

    created = client.post("/api/alerts", json=payload).json()
    detail = client.get(f"/api/incidents/{created['incident_id']}").json()

    assert detail["service"] == "checkout"
    assert detail["severity"] == "P1"
    assert detail["alert"]["source"] == "alertmanager"
    assert detail["alert"]["fingerprint"] == "am-fp-1"
    assert detail["alert"]["raw_payload"]["receiver"] == "sre"


def test_list_and_get_incident(client, alert_payload) -> None:
    created = client.post("/api/alerts", json=alert_payload).json()

    list_response = client.get("/api/incidents", params={"service": "checkout"})
    assert list_response.status_code == 200
    body = list_response.json()
    assert body["items"][0]["incident_id"] == created["incident_id"]
    assert body["total"] >= 1
    assert body["page"] == 1

    detail_response = client.get(f"/api/incidents/{created['incident_id']}")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["alert"]["fingerprint"] == alert_payload["fingerprint"]
    assert detail["alert"]["raw_payload"]["fingerprint"] == alert_payload["fingerprint"]
    assert detail["evidence"] == []
    assert detail["recommended_actions"] == []


def test_diagnose_rejects_active_run_unless_forced(client, fake_enqueue, alert_payload) -> None:
    created = client.post("/api/alerts", json=alert_payload).json()

    conflict = client.post(
        f"/api/incidents/{created['incident_id']}/diagnose", json={"force": False}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CONFLICT"

    forced = client.post(f"/api/incidents/{created['incident_id']}/diagnose", json={"force": True})
    assert forced.status_code == 202
    assert forced.json()["agent_run_id"] != created["agent_run_id"]
    assert len(fake_enqueue.calls) == 2


def test_incident_runs_and_agent_run_detail(client, alert_payload) -> None:
    created = client.post("/api/alerts", json=alert_payload).json()

    runs_response = client.get(f"/api/incidents/{created['incident_id']}/runs")
    assert runs_response.status_code == 200
    assert runs_response.json()[0]["agent_run_id"] == created["agent_run_id"]

    run_response = client.get(f"/api/agent-runs/{created['agent_run_id']}")
    assert run_response.status_code == 200
    assert run_response.json()["checkpoint_thread_id"] == created["agent_run_id"]


def test_validation_error_uses_standard_envelope(client, alert_payload) -> None:
    alert_payload.pop("service")
    response = client.post("/api/alerts", json=alert_payload, headers={"X-Request-Id": "req-bad"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["request_id"] == "req-bad"


def test_missing_incident_returns_standard_error(client) -> None:
    response = client.get("/api/incidents/inc_missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
