"""Tests for _ensure_station_column() — the hand-rolled startup migration guard.

This is the one place in the codebase that deliberately runs *different SQL per
dialect*: SQLite has no information_schema, so it introspects with
`PRAGMA table_info`, while Postgres queries information_schema.columns. Both
branches used to be untested — the guard runs from the FastAPI lifespan, and
httpx's ASGITransport doesn't fire lifespan events, so nothing in the suite
ever called it.

These tests call it directly against the test engine, so whichever branch
matches TEST_DATABASE_URL gets exercised: SQLite by default, PostgreSQL in the
`test-backend-postgres` CI job.
"""
from datetime import datetime, timezone

import pytest_asyncio
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table

import main


# Pre-migration shape of played_tracks: everything the current model has except
# `station`. Declared through SQLAlchemy Core rather than raw DDL so it stays
# dialect-portable — an INTEGER PRIMARY KEY here becomes SERIAL on Postgres and
# a rowid alias on SQLite without this test needing to care.
_legacy_metadata = MetaData()
_legacy_played_tracks = Table(
    "played_tracks",
    _legacy_metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String, nullable=False),
    Column("artist", String, nullable=False),
    Column("album", String),
    Column("started_at", DateTime, nullable=False),
    Column("spotify_url", String),
    Column("apple_music_url", String),
    Column("deezer_url", String),
    Column("rating", Integer),
)

# Naive on purpose — started_at is a naive TIMESTAMP column and asyncpg rejects
# tz-aware values. Same rule as the app and the other test helpers.
_NAIVE_NOW = datetime.now(timezone.utc).replace(tzinfo=None)


@pytest_asyncio.fixture
async def legacy_engine(test_engine):
    """Replace played_tracks with its pre-migration shape (no `station`).

    Teardown drops it and lets Base.metadata rebuild the current schema. That
    restore is not optional: test_engine is function-scoped and calls
    create_all with checkfirst=True, so leaving a station-less table behind
    would make every later test silently run against the old schema.
    """
    from database import Base

    async with test_engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE IF EXISTS played_tracks")
        await conn.run_sync(_legacy_metadata.create_all)

    yield test_engine

    async with test_engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE IF EXISTS played_tracks")
        await conn.run_sync(Base.metadata.create_all)


async def _columns(engine) -> set[str]:
    """Introspect column names, whichever engine is under test."""
    async with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            result = await conn.exec_driver_sql("PRAGMA table_info(played_tracks)")
            return {row[1] for row in result}
        result = await conn.exec_driver_sql(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='played_tracks'"
        )
        return {row[0] for row in result}


async def _insert_legacy_row(engine, title: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            _legacy_played_tracks.insert().values(
                title=title, artist="Old Artist", started_at=_NAIVE_NOW
            )
        )


async def _stations(engine) -> list:
    async with engine.begin() as conn:
        result = await conn.exec_driver_sql(
            "SELECT station FROM played_tracks ORDER BY id"
        )
        return [row[0] for row in result]


# ── The migration itself ──────────────────────────────────────────────────────

async def test_adds_station_column_when_missing(legacy_engine):
    assert "station" not in await _columns(legacy_engine)

    await main._ensure_station_column(legacy_engine)

    assert "station" in await _columns(legacy_engine)


async def test_backfills_existing_rows_to_pure_ibiza(legacy_engine):
    """Rows predating multi-station support were all Pure Ibiza Radio."""
    await _insert_legacy_row(legacy_engine, "Ancient Track")

    await main._ensure_station_column(legacy_engine)

    assert await _stations(legacy_engine) == ["Pure Ibiza Radio"]


async def test_is_idempotent(legacy_engine):
    """Runs on every startup, so a second pass must be a no-op rather than an
    error — ALTER TABLE ADD COLUMN on an existing column would raise."""
    await _insert_legacy_row(legacy_engine, "Ancient Track")

    await main._ensure_station_column(legacy_engine)
    await main._ensure_station_column(legacy_engine)
    await main._ensure_station_column(legacy_engine)

    assert await _stations(legacy_engine) == ["Pure Ibiza Radio"]


async def test_backfill_does_not_clobber_real_station_values(legacy_engine):
    """The backfill is WHERE station IS NULL. A row written after the migration
    carries a real station name, and a later startup must leave it alone."""
    await _insert_legacy_row(legacy_engine, "Ancient Track")
    await main._ensure_station_column(legacy_engine)

    async with legacy_engine.begin() as conn:
        await conn.exec_driver_sql(
            "INSERT INTO played_tracks (title, artist, station, started_at) "
            "VALUES ('Modern Track', 'New Artist', 'Milano Lounge', "
            f"'{_NAIVE_NOW.isoformat(sep=' ')}')"
        )

    await main._ensure_station_column(legacy_engine)

    assert await _stations(legacy_engine) == ["Pure Ibiza Radio", "Milano Lounge"]


async def test_no_op_on_current_schema(test_engine):
    """Against an already-migrated table the guard must not touch data — this
    is the path that actually runs on virtually every real startup."""
    async with test_engine.begin() as conn:
        await conn.exec_driver_sql(
            "INSERT INTO played_tracks (title, artist, station, started_at) "
            "VALUES ('Untouched', 'Artist', 'Sonica', "
            f"'{_NAIVE_NOW.isoformat(sep=' ')}')"
        )

    await main._ensure_station_column(test_engine)

    assert await _stations(test_engine) == ["Sonica"]
