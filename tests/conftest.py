import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    from database import Base
    import models  # noqa — registers ORM models with Base.metadata

    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def client(test_engine):
    """FastAPI test client backed by in-memory SQLite."""
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

    test_engine is session-scoped (one shared in-memory DB, for speed), so
    without this, rows a test writes — e.g. /api/now-playing now persists to
    the DB on a track change, which it didn't before — would leak into later
    tests that assert on row counts (empty-table checks, LIMIT 50, etc).
    """
    yield
    async with test_engine.begin() as conn:
        await conn.exec_driver_sql("DELETE FROM played_tracks")
