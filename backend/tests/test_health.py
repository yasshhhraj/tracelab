import pytest


@pytest.mark.asyncio
async def test_health_returns_ok(client):
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert "environment" in body


@pytest.mark.asyncio
async def test_health_version_matches_config(client):
    from app.config import settings

    response = await client.get("/health")
    assert response.json()["version"] == settings.version
