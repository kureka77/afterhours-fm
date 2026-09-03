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


# ── _drop_rating_column ───────────────────────────────────────────────────────
# The thumbs up/down feature was removed. The legacy table above still declares
# `rating`, which makes it the natural fixture for testing the drop.

async def _indexes(engine) -> set[str]:
    """Index names on played_tracks, whichever engine is under test."""
    async with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            result = await conn.exec_driver_sql("PRAGMA index_list(played_tracks)")
            return {row[1] for row in result}
        result = await conn.exec_driver_sql(
            "SELECT indexname FROM pg_indexes WHERE tablename='played_tracks'"
        )
        return {row[0] for row in result}


async def test_drops_rating_column_when_empty(legacy_engine):
    assert "rating" in await _columns(legacy_engine)

    await main._drop_rating_column(legacy_engine)

    assert "rating" not in await _columns(legacy_engine)


async def test_keeps_rating_column_when_it_holds_data(legacy_engine):
    """The guard refuses to drop a column that has values in it.

    The column was verified 100% empty before removal, so this should never
    fire in practice — but a migration that silently destroys data when its
    assumption turns out wrong is a bad trade for tidiness.
    """
    async with legacy_engine.begin() as conn:
        await conn.execute(
            _legacy_played_tracks.insert().values(
                title="Rated", artist="A", started_at=_NAIVE_NOW, rating=1
            )
        )

    await main._drop_rating_column(legacy_engine)

    assert "rating" in await _columns(legacy_engine)


async def test_drop_rating_is_idempotent(legacy_engine):
    """Runs on every startup — DROP COLUMN on an absent column would raise."""
    await main._drop_rating_column(legacy_engine)
    await main._drop_rating_column(legacy_engine)
    await main._drop_rating_column(legacy_engine)

    assert "rating" not in await _columns(legacy_engine)


async def test_drop_rating_no_op_on_current_schema(test_engine):
    """The path that runs on virtually every real startup, once migrated."""
    await main._drop_rating_column(test_engine)
    assert "rating" not in await _columns(test_engine)


# ── _ensure_indexes ───────────────────────────────────────────────────────────
# create_all() only emits indexes alongside a table it is creating, so an
# existing database never gets them from the model alone. The legacy fixture
# reproduces exactly that: a played_tracks with no indexes on it.

async def test_creates_both_indexes_when_missing(legacy_engine):
    before = await _indexes(legacy_engine)
    assert "ix_played_tracks_started_at" not in before
    assert "ix_played_tracks_artist_title" not in before

    await main._ensure_indexes(legacy_engine)

    after = await _indexes(legacy_engine)
    assert "ix_played_tracks_started_at" in after
    assert "ix_played_tracks_artist_title" in after


async def test_ensure_indexes_is_idempotent(legacy_engine):
    await main._ensure_indexes(legacy_engine)
    await main._ensure_indexes(legacy_engine)

    after = await _indexes(legacy_engine)
    assert "ix_played_tracks_started_at" in after


# ── _purge_ident_rows ────────────────────────────────────────────────────

async def _titles(engine) -> list:
    async with engine.begin() as conn:
        result = await conn.exec_driver_sql(
            "SELECT title FROM played_tracks ORDER BY id"
        )
        return [row[0] for row in result]


# These run against the current schema, not the legacy fixture: the purge
# references `station`, and in lifespan it always runs *after*
# _ensure_station_column, so a table without that column is a state that never
# reaches this guard.

async def test_purges_rows_with_no_artist(test_engine):
    """The 34 real rows this cleans up were show idents: 31x Blue Marlin's
    "Djs Blue Marlin Sessions", 2x a bare "-", one Deep Vibes show name."""
    await _insert_current(test_engine, "", "Djs Blue Marlin Sessions", "Blue Marlin")
    await _insert_current(test_engine, "Anyma", "Eternity", "Blue Marlin")

    await main._purge_ident_rows(test_engine)

    assert await _titles(test_engine) == ["Eternity"]


async def test_purges_whitespace_only_artist(test_engine):
    """trim() is standard SQL and behaves identically on both engines."""
    await _insert_current(test_engine, "   ", "Ident", "Blue Marlin")

    await main._purge_ident_rows(test_engine)

    assert await _titles(test_engine) == []


async def test_purge_is_idempotent_and_leaves_real_tracks(test_engine):
    await _insert_current(test_engine, "Anyma", "Eternity", "Blue Marlin")

    await main._purge_ident_rows(test_engine)
    await main._purge_ident_rows(test_engine)

    assert await _titles(test_engine) == ["Eternity"]


# ── _purge_ident_rows, rule 2: artist is a prefix of its own station ──────────
# These need the `station` column, so they run against the current schema
# rather than the legacy fixture.

async def _insert_current(engine, artist: str, title: str, station) -> None:
    """Insert through the real model's table so SQLAlchemy binds the datetime
    itself. Passing a datetime straight to exec_driver_sql goes through
    sqlite3's default adapter, deprecated since Python 3.12 — and artist/title
    here include an apostrophe ("Just Can't Get Enough"), so inlining them as
    SQL literals isn't an option either."""
    from models import PlayedTrack

    async with engine.begin() as conn:
        await conn.execute(
            PlayedTrack.__table__.insert().values(
                title=title, artist=artist, station=station, started_at=_NAIVE_NOW
            )
        )


async def _rows(engine) -> list:
    async with engine.begin() as conn:
        result = await conn.exec_driver_sql(
            "SELECT artist, title FROM played_tracks ORDER BY id"
        )
        return [(row[0], row[1]) for row in result]


async def test_purges_icy_name_split_rows(test_engine):
    """The real shape: artist "SUNSHINE LIVE" / title "Techno" on station
    "Sunshine Live Techno" — 7 such rows existed. They have a non-empty artist,
    so the artist-less rule misses them, yet they are idents, and they made the
    three Sunshine channels look mutually "similar" for an entirely fake
    reason."""
    await _insert_current(test_engine, "SUNSHINE LIVE", "Techno", "Sunshine Live Techno")
    await _insert_current(test_engine, "Anyma", "Eternity", "Sunshine Live Techno")

    await main._purge_ident_rows(test_engine)

    assert await _rows(test_engine) == [("Anyma", "Eternity")]


async def test_purges_station_tagline_logged_under_its_own_name(test_engine):
    """Milano Lounge logged its own tagline as a track by the same mechanism."""
    await _insert_current(test_engine, "Milano Lounge",
                          "Sophisticated Sounds from the Heart of Milan Italy",
                          "Milano Lounge")

    await main._purge_ident_rows(test_engine)

    assert await _rows(test_engine) == []


async def test_purge_keeps_a_real_track_on_a_similarly_named_station(test_engine):
    """The rule is a *prefix* match against the station the row played on, so a
    normal artist is never at risk — only one whose name literally begins the
    station's own name."""
    await _insert_current(test_engine, "Anyma", "Eternity", "Sunshine Live Techno")
    await _insert_current(test_engine, "Sunshine Anderson", "Heard It All Before", "Pure Ibiza Radio")

    await main._purge_ident_rows(test_engine)

    assert sorted(await _rows(test_engine)) == [
        ("Anyma", "Eternity"), ("Sunshine Anderson", "Heard It All Before")]


async def test_purge_ignores_short_artist_names(test_engine):
    """The 3-character floor: without it a one-letter artist would match every
    station whose name starts with that letter."""
    await _insert_current(test_engine, "M", "Pop Muzik", "Milano Lounge")

    await main._purge_ident_rows(test_engine)

    assert await _rows(test_engine) == [("M", "Pop Muzik")]


async def test_purge_does_not_treat_percent_in_artist_as_a_wildcard(test_engine):
    """Why the rule uses substr/length rather than LIKE: an artist containing %
    would otherwise become a wildcard pattern and delete unrelated rows."""
    await _insert_current(test_engine, "100%", "Just Can't Get Enough", "Milano Lounge")

    await main._purge_ident_rows(test_engine)

    assert await _rows(test_engine) == [("100%", "Just Can't Get Enough")]
