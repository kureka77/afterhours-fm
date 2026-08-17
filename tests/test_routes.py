"""Tests for the API routes."""
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
import pytest
import main
import spotify
from models import PlayedTrack


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_track(title="Song", artist="Artist", offset_seconds=0):
    return PlayedTrack(
        title=title,
        artist=artist,
        started_at=datetime.now(timezone.utc) + timedelta(seconds=offset_seconds),
    )


EMPTY_READ = {"artist": "", "title": "", "format": None, "bitrate": None, "sample_rate": None}


@pytest.fixture(autouse=True)
def _register_test_stations(monkeypatch):
    """The route resolves a station name to a URL via main._STATION_URLS (the
    SSRF fix — clients no longer pass URLs). These tests use invented station
    names, so register them; monkeypatch restores the real registry after."""
    monkeypatch.setattr(main, "_STATION_URLS", {
        **main._STATION_URLS,
        "Test FM":    "http://x",
        "Station A":  "http://a",
        "Station B":  "http://b",
        "Ibiza Pura": "http://x",
    })


@pytest.fixture(autouse=True)
def _clear_now_playing_state():
    """Each test starts with clean in-memory now-playing state — this state
    is process-global (keyed by station name), so tests would otherwise leak
    into each other."""
    main._station_state.clear()
    main._meta_cache.clear()
    main._icecast_cache.clear()
    main._shazam_cache.clear()
    main._cover_art_cache.clear()
    yield


@pytest.fixture(autouse=True)
def _no_shazam_or_cover_art_by_default(monkeypatch):
    """Shazam and cover-art lookups do real network calls (or, for Shazam, a
    real ~10s audio capture) — default them to "nothing found" so route tests
    stay fast and hermetic. Tests that actually exercise the fallback/cover
    paths override these explicitly via _mock_shazam / _mock_cover_art."""
    _mock_shazam(monkeypatch, None)
    _mock_cover_art(monkeypatch, None)


def _mock_icy(monkeypatch, **overrides):
    """Stub out the actual network read — these are route tests, not tests
    of ICY parsing itself (see test_poll.py for that). Any field not
    overridden defaults to the "nothing to report" shape."""
    result = {**EMPTY_READ, **overrides}
    async def fake(url):
        return result
    monkeypatch.setattr(main, "_read_icy_now_playing_cached", fake)


def _mock_listeners(monkeypatch, value):
    async def fake(station):
        return value
    monkeypatch.setattr(main, "_read_icecast_listeners_cached", fake)


def _mock_shazam(monkeypatch, result):
    async def fake(station, url):
        return result
    monkeypatch.setattr(main, "_shazam_fallback_cached", fake)


def _mock_cover_art(monkeypatch, cover_url):
    async def fake(artist, title):
        return cover_url
    monkeypatch.setattr(main, "_cover_art_cached", fake)


# ── GET /api/now-playing ───────────────────────────────────────────────────────

async def test_now_playing_requires_station_param(client):
    r = await client.get("/api/now-playing")
    assert r.status_code == 422


async def test_now_playing_rejects_unknown_station(client):
    """The SSRF guard: only names in the registry resolve to a URL the server
    will open. An unknown name is a 404, not an outbound request."""
    r = await client.get("/api/now-playing", params={"station": "Totally Made Up"})
    assert r.status_code == 404


async def test_now_playing_ignores_client_supplied_url(client, monkeypatch):
    """A stray ?url= must not steer the server anywhere — it is not a parameter
    any more, so FastAPI drops it and the registry URL is used regardless."""
    seen = {}
    async def fake(url):
        seen["url"] = url
        return EMPTY_READ
    monkeypatch.setattr(main, "_read_icy_now_playing_cached", fake)
    _mock_listeners(monkeypatch, None)

    r = await client.get(
        "/api/now-playing",
        params={"station": "Test FM", "url": "http://169.254.169.254/latest/meta-data/"},
    )
    assert r.status_code == 200
    assert seen["url"] == "http://x"


# ── GET /api/stations ──────────────────────────────────────────────────────────

async def test_stations_returns_registry(client):
    r = await client.get("/api/stations")
    assert r.status_code == 200
    body = r.json()
    assert len(body) > 0
    assert all("name" in s and "url" in s for s in body)


async def test_now_playing_no_metadata(client, monkeypatch):
    _mock_icy(monkeypatch)
    _mock_listeners(monkeypatch, None)
    r = await client.get("/api/now-playing", params={"station": "Test FM"})
    assert r.status_code == 200
    body = r.json()
    assert body["current"] == {}
    assert body["history"] == []
    assert body["stream_info"] == {"format": None, "bitrate": None, "sample_rate": None, "listeners": None}


async def test_now_playing_with_track(client, monkeypatch):
    _mock_icy(monkeypatch, artist="DJ Test", title="Sunrise")
    _mock_listeners(monkeypatch, None)
    r = await client.get("/api/now-playing", params={"station": "Test FM"})
    assert r.status_code == 200
    assert r.json()["current"]["title"]  == "Sunrise"
    assert r.json()["current"]["artist"] == "DJ Test"


async def test_now_playing_includes_stream_info(client, monkeypatch):
    _mock_icy(monkeypatch, format="MP3", bitrate=128, sample_rate=44100)
    _mock_listeners(monkeypatch, 42)
    r = await client.get("/api/now-playing", params={"station": "Test FM"})
    assert r.json()["stream_info"] == {"format": "MP3", "bitrate": 128, "sample_rate": 44100, "listeners": 42}


async def test_now_playing_state_is_isolated_per_station(client, monkeypatch):
    calls = {"n": 0}
    async def fake(url):
        calls["n"] += 1
        return {**EMPTY_READ, "artist": "A", "title": f"Track {calls['n']}"}
    monkeypatch.setattr(main, "_read_icy_now_playing_cached", fake)
    _mock_listeners(monkeypatch, None)

    r1 = await client.get("/api/now-playing", params={"station": "Station A"})
    r2 = await client.get("/api/now-playing", params={"station": "Station B"})
    assert r1.json()["current"]["title"] != r2.json()["current"]["title"]


async def test_now_playing_repeated_track_does_not_duplicate_history(client, monkeypatch):
    _mock_icy(monkeypatch, artist="A", title="Same Track")
    _mock_listeners(monkeypatch, None)
    await client.get("/api/now-playing", params={"station": "Test FM"})
    r = await client.get("/api/now-playing", params={"station": "Test FM"})
    assert r.json()["current"]["title"] == "Same Track"
    assert r.json()["history"] == []  # never changed, so nothing pushed into history


async def test_now_playing_track_change_appends_to_history(client, monkeypatch):
    async def fake(url):
        fake.calls += 1
        return {**EMPTY_READ, "artist": "A", "title": f"Track {fake.calls}"}
    fake.calls = 0
    monkeypatch.setattr(main, "_read_icy_now_playing_cached", fake)
    _mock_listeners(monkeypatch, None)

    await client.get("/api/now-playing", params={"station": "Test FM"})
    r = await client.get("/api/now-playing", params={"station": "Test FM"})
    assert r.json()["current"]["title"] == "Track 2"
    assert len(r.json()["history"]) == 1
    assert r.json()["history"][0]["title"] == "Track 1"


async def test_now_playing_persists_to_db_with_station(client, monkeypatch, db_session):
    _mock_icy(monkeypatch, artist="DB Artist", title="DB Title")
    _mock_listeners(monkeypatch, None)
    await client.get("/api/now-playing", params={"station": "Test FM"})

    result = await db_session.execute(select(PlayedTrack).where(PlayedTrack.title == "DB Title"))
    row = result.scalar_one()
    assert row.artist  == "DB Artist"
    assert row.station == "Test FM"


async def test_now_playing_stores_naive_started_at(client, monkeypatch, db_session):
    """Regression test: started_at is a naive TIMESTAMP column. asyncpg (unlike
    sqlite) rejects a tz-aware datetime outright on insert — this caught a real
    bug on first deploy against Postgres. SQLite alone wouldn't catch it."""
    _mock_icy(monkeypatch, artist="TZ Artist", title="TZ Title")
    _mock_listeners(monkeypatch, None)
    await client.get("/api/now-playing", params={"station": "Test FM"})

    result = await db_session.execute(select(PlayedTrack).where(PlayedTrack.title == "TZ Title"))
    row = result.scalar_one()
    assert row.started_at.tzinfo is None


# ── Shazam fallback + cover art ─────────────────────────────────────────────────

async def test_shazam_not_called_when_icy_has_a_track(client, monkeypatch):
    """The fallback must only fire when ICY gives nothing — never run
    alongside a working ICY read (that would double the cost for no reason)."""
    _mock_icy(monkeypatch, artist="Real Artist", title="Real Title")
    _mock_listeners(monkeypatch, None)
    calls = {"n": 0}
    async def fake_shazam(station, url):
        calls["n"] += 1
        return {"artist": "Shazam Artist", "title": "Shazam Title", "cover_url": None}
    monkeypatch.setattr(main, "_shazam_fallback_cached", fake_shazam)

    r = await client.get("/api/now-playing", params={"station": "Test FM"})
    assert r.json()["current"]["title"] == "Real Title"
    assert calls["n"] == 0


async def test_shazam_used_when_icy_gives_nothing(client, monkeypatch):
    _mock_icy(monkeypatch)  # empty — e.g. Pure Ibiza Radio, Ibiza Pura, Deep Vibes
    _mock_listeners(monkeypatch, None)
    _mock_shazam(monkeypatch, {"artist": "D2ear", "title": "Pieona", "cover_url": "https://example.com/cover.jpg"})

    r = await client.get("/api/now-playing", params={"station": "Ibiza Pura"})
    body = r.json()
    assert body["current"]["artist"] == "D2ear"
    assert body["current"]["title"]  == "Pieona"
    assert body["current"]["source"] == "shazam"
    assert body["current"]["cover_url"] == "https://example.com/cover.jpg"


async def test_shazam_no_match_leaves_current_empty(client, monkeypatch):
    _mock_icy(monkeypatch)
    _mock_listeners(monkeypatch, None)
    _mock_shazam(monkeypatch, None)  # no match found

    r = await client.get("/api/now-playing", params={"station": "Test FM"})
    assert r.json()["current"] == {}


async def test_shazam_track_persists_to_db(client, monkeypatch, db_session):
    _mock_icy(monkeypatch)
    _mock_listeners(monkeypatch, None)
    _mock_shazam(monkeypatch, {"artist": "D2ear", "title": "Shazam Persisted", "cover_url": None})

    await client.get("/api/now-playing", params={"station": "Test FM"})

    result = await db_session.execute(select(PlayedTrack).where(PlayedTrack.title == "Shazam Persisted"))
    row = result.scalar_one()
    assert row.artist == "D2ear"
    assert row.station == "Test FM"


async def test_cover_art_fetched_for_icy_sourced_track(client, monkeypatch):
    """ICY tracks don't come with their own cover art — must fall through to
    the Spotify-search lookup, unlike Shazam tracks which bring their own."""
    _mock_icy(monkeypatch, artist="Duck Sauce", title="Barbra Streisand")
    _mock_listeners(monkeypatch, None)
    _mock_cover_art(monkeypatch, "https://example.com/duck-sauce.jpg")

    r = await client.get("/api/now-playing", params={"station": "Test FM"})
    assert r.json()["current"]["cover_url"] == "https://example.com/duck-sauce.jpg"
    assert r.json()["current"]["source"] == "icy"


async def test_shazam_cover_art_not_overridden_by_spotify_search(client, monkeypatch):
    """Shazam already returns cover art directly — _cover_art_cached (Spotify
    search) must not be consulted or allowed to clobber it."""
    _mock_icy(monkeypatch)
    _mock_listeners(monkeypatch, None)
    _mock_shazam(monkeypatch, {"artist": "A", "title": "B", "cover_url": "https://shazam.example/cover.jpg"})
    calls = {"n": 0}
    async def fake_cover_art(artist, title):
        calls["n"] += 1
        return "https://spotify.example/should-not-be-used.jpg"
    monkeypatch.setattr(main, "_cover_art_cached", fake_cover_art)

    r = await client.get("/api/now-playing", params={"station": "Test FM"})
    assert r.json()["current"]["cover_url"] == "https://shazam.example/cover.jpg"
    assert calls["n"] == 0


# ── GET /api/track-history ─────────────────────────────────────────────────────

async def test_track_history_empty(client):
    r = await client.get("/api/track-history")
    assert r.status_code == 200
    assert r.json() == []


async def test_track_history_returns_rows(client, db_session):
    tracks = [make_track(f"Track {i}", offset_seconds=i) for i in range(3)]
    for t in tracks:
        db_session.add(t)
    await db_session.commit()

    r = await client.get("/api/track-history")
    assert r.status_code == 200
    titles = [row["title"] for row in r.json()]
    assert "Track 0" in titles
    assert "Track 1" in titles
    assert "Track 2" in titles


async def test_track_history_limit_50(client, db_session):
    for i in range(60):
        db_session.add(make_track(f"Bulk {i}", offset_seconds=i))
    await db_session.commit()

    r = await client.get("/api/track-history")
    assert r.status_code == 200
    assert len(r.json()) <= 50


# ── POST /api/spotify/save ──────────────────────────────────────────────────────

async def test_spotify_save_success(client, monkeypatch):
    async def fake_save(artist, title):
        assert artist == "Energy 52" and title == "Café Del Mar"
        return {
            "matched_artist": "Energy 52",
            "matched_title": "Café Del Mar",
            "spotify_url": "https://open.spotify.com/track/abc123",
        }
    monkeypatch.setattr(spotify, "save_current_track", fake_save)

    r = await client.post("/api/spotify/save", json={"artist": "Energy 52", "title": "Café Del Mar"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["spotify_url"] == "https://open.spotify.com/track/abc123"


async def test_spotify_save_requires_artist_and_title(client):
    r = await client.post("/api/spotify/save", json={"artist": "A"})
    assert r.status_code == 422


async def test_spotify_save_not_configured_returns_503(client, monkeypatch):
    async def fake_save(artist, title):
        raise spotify.SpotifyNotConfigured("missing creds")
    monkeypatch.setattr(spotify, "save_current_track", fake_save)

    r = await client.post("/api/spotify/save", json={"artist": "A", "title": "B"})
    assert r.status_code == 503


async def test_spotify_save_no_match_returns_404(client, monkeypatch):
    async def fake_save(artist, title):
        raise spotify.SpotifyNoMatch("no match")
    monkeypatch.setattr(spotify, "save_current_track", fake_save)

    r = await client.post("/api/spotify/save", json={"artist": "A", "title": "B"})
    assert r.status_code == 404


async def test_spotify_save_upstream_error_returns_502(client, monkeypatch):
    import httpx
    async def fake_save(artist, title):
        request = httpx.Request("GET", "https://api.spotify.com/v1/search")
        response = httpx.Response(500, request=request)
        raise httpx.HTTPStatusError("boom", request=request, response=response)
    monkeypatch.setattr(spotify, "save_current_track", fake_save)

    r = await client.post("/api/spotify/save", json={"artist": "A", "title": "B"})
    assert r.status_code == 502
