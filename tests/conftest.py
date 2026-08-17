import os

import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

# In-memory SQLite by default: fast, no setup, fine for the ~59 tests here.
#
# Override with TEST_DATABASE_URL to run the same suite against a real
# PostgreSQL, which CI does in the `test-backend-postgres` job. This exists
# because the two engines genuinely disagree: asyncpg rejects a tz-aware
# datetime for a naive TIMESTAMP column outright, while SQLite silently
# accepts it. That gap shipped a bug to production once — every local check
# passed because dev and tests were both SQLite, and it only surfaced on a
# real Postgres deploy. Running the suite against both closes the
# dev/prod parity gap that allowed it.
TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")


@pytest_asyncio.fixture
async def test_engine():
    """Function-scoped on purpose, not for isolation — for the event loop.

    pytest-asyncio gives each test its own event loop. An asyncpg connection
    is bound to the loop that opened it, so a session-scoped engine hands the
    second test a pool belonging to the first test's dead loop and every query
    dies with "attached to a different loop". aiosqlite hides this (it proxies
    calls to a worker thread), which is why this only breaks against Postgres.

    Building the engine per test keeps it on the right loop.
    `create_all` defaults to checkfirst=True, so it is a cheap no-op once the
    table exists.
    """
    from database import Base
    import models  # noqa — registers ORM models with Base.metadata

    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def client(test_engine):
    """FastAPI test client backed by the test database (see TEST_DB_URL)."""
    from main import app
    import database

    TestSession = async_sessionmaker(test_engine, expire_on_commit=False)

    async def override_get_session():
        async with TestSession() as session:
            yield session

    app.dependency_overrides[database.get_session] = override_get_session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def db_session(test_engine):
    """Direct DB session for seeding test data."""
    TestSession = async_sessionmaker(test_engine, expire_on_commit=False)
    async with TestSession() as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def _clean_played_tracks(test_engine):
    """Reset played_tracks after every test.

    test_engine is session-scoped (one shared database, for speed), so
    without this, rows a test writes — e.g. /api/now-playing now persists to
    the DB on a track change, which it didn't before — would leak into later
    tests that assert on row counts (empty-table checks, LIMIT 50, etc).

    Postgres keeps the sequence behind played_tracks.id running across a
    DELETE, so ids climb through the session rather than restarting at 1 the
    way they do on a fresh in-memory SQLite. Nothing asserts on specific id
    values, so this is deliberate — RESTART IDENTITY would hide a test that
    wrongly depends on them.
    """
    yield
    async with test_engine.begin() as conn:
        await conn.exec_driver_sql("DELETE FROM played_tracks")
