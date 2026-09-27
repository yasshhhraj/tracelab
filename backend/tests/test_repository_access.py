"""Production requests cannot redirect an investigation to another checkout."""

import pytest

from app.config import settings

_PAYLOAD = {
    "jira_issue_id": "TEST-1",
    "repository": "/tmp/unapproved-repository",
    "symptom": "wrong result",
    "expected": "correct result",
    "actual": "wrong result",
    "affected_area": "service",
    "error_type": "deterministic",
}


@pytest.mark.asyncio
async def test_production_investigation_rejects_other_repository(client, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "target_repository", "/srv/tracelab/repos/target")

    response = await client.post("/api/investigations", json=_PAYLOAD)
    assert response.status_code == 403
    assert response.json() == {"detail": "Repository is not permitted"}


@pytest.mark.asyncio
async def test_production_jira_import_rejects_other_repository(client, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "target_repository", "/srv/tracelab/repos/target")

    response = await client.post(
        "/api/jira/import/TEST-1", json={"repository": "/tmp/unapproved-repository"}
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Repository is not permitted"}
