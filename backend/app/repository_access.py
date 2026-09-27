"""Limit production investigations to the configured local checkout."""

from pathlib import Path

from fastapi import HTTPException

from app.config import settings


def permitted_repository(repository: str) -> str:
    if settings.environment != "production":
        return repository
    if not settings.target_repository:
        raise HTTPException(status_code=503, detail="Target repository is not configured")

    configured = Path(settings.target_repository).resolve()
    requested = Path(repository).resolve()
    if requested != configured:
        raise HTTPException(status_code=403, detail="Repository is not permitted")
    return str(configured)
