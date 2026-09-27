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


@pytest.mark.asyncio
async def test_ready_requires_database(client, db_engine, monkeypatch):
    from app.routers import health

    monkeypatch.setattr(health, "engine", db_engine)
    response = await client.get("/ready")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_ready_returns_503_when_database_is_unavailable(client, monkeypatch):
    from app.routers import health

    class UnavailableEngine:
        def connect(self):
            raise ConnectionError("database offline")

    monkeypatch.setattr(health, "engine", UnavailableEngine())
    response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
