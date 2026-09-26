from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.base import Base
from app.main import app

# ── HTTP client fixture (existing, kept as-is) ────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# ── In-memory DB fixtures (new for CP-06) ─────────────────────────────────────


@pytest_asyncio.fixture
async def db_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Spin up a fresh in-memory SQLite engine with all tables created."""
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Yield a single AsyncSession backed by the in-memory engine."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker:
    """Return an async_sessionmaker bound to the in-memory engine."""
    return async_sessionmaker(db_engine, expire_on_commit=False)


# ── HTTP client wired to the in-memory DB (for API integration tests) ─────────


@pytest_asyncio.fixture
async def client_with_db(db_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    """
    AsyncClient whose FastAPI dependency `get_session` is overridden to use
    the in-memory test database.
    """
    from app.db.session import get_session

    AsyncTestSession = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with AsyncTestSession() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
