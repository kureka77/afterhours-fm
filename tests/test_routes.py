"""Tests for the API routes."""
import asyncio
import time
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
import pytest
import main
import spotify
from models import PlayedTrack


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_track(title="Song", artist="Artist", offset_seconds=0):
    # .replace(tzinfo=None) mirrors what the route does at main.py:344, and is
    # not optional: started_at is a naive TIMESTAMP column, and asyncpg refuses
    # a tz-aware value outright while SQLite quietly stores it. Seeding rows
    # tz-aware here passed for months on SQLite and only failed once the suite
    # was pointed at real Postgres — the same parity gap that shipped the
    # original production bug, reproduced in the test helper.
    started_at = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return PlayedTrack(
        title=title,
        artist=artist,
        started_at=started_at.replace(tzinfo=None),
    )


EMPTY_READ = {"artist": "", "title": "", "format": None, "bitrate": None, "sample_rate": None}

# Captured before any fixture patches it: the autouse stub below replaces this
# attribute on `main`, so the two wrapper tests at the end of this file need a
# handle on the genuine implementation to exercise it.
_REAL_PLAYLIST_CACHED = main._playlist_now_playing_cached


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
        # HLS: the URL serves a playlist, not audio. Shazam is its only track
        # source — see the HLS section at the end of this file.
        "HLS FM":     "http://cdn/radio/master.m3u8",
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
    main._spotify_cache.clear()
    main._playlist_cache.clear()
    main._identify_in_flight.clear()
    yield


@pytest.fixture(autouse=True)
def _no_shazam_or_cover_art_by_default(monkeypatch):
    """Shazam and cover-art lookups do real network calls (or, for Shazam, a
    real ~10s audio capture) — default them to "nothing found" so route tests
    stay fast and hermetic. Tests that actually exercise the fallback/cover
    paths override these explicitly via _mock_shazam / _mock_cover_art."""
    _mock_shazam(monkeypatch, None)
    _mock_cover_art(monkeypatch, None)
    _mock_playlist(monkeypatch, None)


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


def _mock_cover_art(monkeypatch, cover_url, spotify_url=None):
    """Stub the Spotify lookup. It returns cover art *and* the track URL now —
    the URL is persisted on the row, so tests that assert on what's stored need
    to be able to set it."""
    async def fake(artist, title):
        if cover_url is None and spotify_url is None:
            return None
        return {"cover_url": cover_url, "spotify_url": spotify_url}
    monkeypatch.setattr(main, "_spotify_lookup_cached", fake)


def _mock_playlist(monkeypatch, result):
    async def fake(station):
        return result
    monkeypatch.setattr(main, "_playlist_now_playing_cached", fake)


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
    """Shazam returns its own cover art, which must win.

    The Spotify lookup *is* still made — it's the only source of the
    spotify_url that gets persisted on the row — but its cover must not clobber
    the one Shazam already supplied.
    """
    _mock_icy(monkeypatch)
    _mock_listeners(monkeypatch, None)
    _mock_shazam(monkeypatch, {"artist": "A", "title": "B", "cover_url": "https://shazam.example/cover.jpg"})
    calls = {"n": 0}
    async def fake_lookup(artist, title):
        calls["n"] += 1
        return {"cover_url": "https://spotify.example/should-not-be-used.jpg",
                "spotify_url": "https://open.spotify.com/track/abc"}
    monkeypatch.setattr(main, "_spotify_lookup_cached", fake_lookup)

    r = await client.get("/api/now-playing", params={"station": "Test FM"})
    assert r.json()["current"]["cover_url"] == "https://shazam.example/cover.jpg"
    assert calls["n"] == 1


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


# ── Broadcaster playlist fallback (Sunshine Live) ──────────────────────────────

async def test_playlist_not_called_when_icy_has_a_track(client, monkeypatch):
    """Same rule as Shazam: the cheap ICY read wins, and nothing else runs."""
    _mock_icy(monkeypatch, artist="Real Artist", title="Real Title")
    _mock_listeners(monkeypatch, None)
    calls = {"n": 0}
    async def fake_playlist(station):
        calls["n"] += 1
        return {"artist": "P", "title": "T", "cover_url": None}
    monkeypatch.setattr(main, "_playlist_now_playing_cached", fake_playlist)

    r = await client.get("/api/now-playing", params={"station": "Test FM"})
    assert r.json()["current"]["title"] == "Real Title"
    assert calls["n"] == 0


async def test_playlist_used_when_icy_gives_nothing(client, monkeypatch):
    """The Sunshine Live case: ICY only ever announced the station name, so
    _read_icy_now_playing now returns empty and the playlist supplies the
    track — including cover art, which ICY-sourced tracks never carry."""
    _mock_icy(monkeypatch)
    _mock_listeners(monkeypatch, None)
    _mock_playlist(monkeypatch, {
        "artist": "Anna Reusch", "title": "Triplet King",
        "cover_url": "https://img.example/xl.jpg",
    })

    body = (await client.get("/api/now-playing", params={"station": "Test FM"})).json()
    assert body["current"]["artist"] == "Anna Reusch"
    assert body["current"]["title"] == "Triplet King"
    assert body["current"]["source"] == "playlist"
    assert body["current"]["cover_url"] == "https://img.example/xl.jpg"


async def test_playlist_takes_precedence_over_shazam(client, monkeypatch):
    """Ordering matters: the playlist is the broadcaster's own data and one
    JSON GET, Shazam is a fingerprint guess costing a ~10s audio capture. If
    the playlist answers, Shazam must not run at all."""
    _mock_icy(monkeypatch)
    _mock_listeners(monkeypatch, None)
    _mock_playlist(monkeypatch, {"artist": "P", "title": "Playlist Track", "cover_url": None})
    calls = {"n": 0}
    async def fake_shazam(station, url):
        calls["n"] += 1
        return {"artist": "S", "title": "Shazam Track", "cover_url": None}
    monkeypatch.setattr(main, "_shazam_fallback_cached", fake_shazam)

    body = (await client.get("/api/now-playing", params={"station": "Test FM"})).json()
    assert body["current"]["title"] == "Playlist Track"
    assert calls["n"] == 0


async def test_shazam_still_runs_when_playlist_has_nothing(client, monkeypatch):
    """Between tracks, or on the main simulcast where the feed carries shows
    rather than songs, the playlist returns None — Shazam is still the last
    resort, not skipped."""
    _mock_icy(monkeypatch)
    _mock_listeners(monkeypatch, None)
    _mock_playlist(monkeypatch, None)
    _mock_shazam(monkeypatch, {"artist": "S", "title": "Shazam Track", "cover_url": None})

    body = (await client.get("/api/now-playing", params={"station": "Test FM"})).json()
    assert body["current"]["title"] == "Shazam Track"
    assert body["current"]["source"] == "shazam"


async def test_playlist_cover_art_not_overridden_by_spotify_search(client, monkeypatch):
    """The playlist brings its own cover art, so the Spotify lookup that
    exists for bare ICY tracks must not run and overwrite it."""
    _mock_icy(monkeypatch)
    _mock_listeners(monkeypatch, None)
    _mock_playlist(monkeypatch, {
        "artist": "A", "title": "B", "cover_url": "https://playlist.example/cover.jpg",
    })
    calls = {"n": 0}
    async def fake_lookup(artist, title):
        calls["n"] += 1
        return {"cover_url": "https://spotify.example/cover.jpg",
                "spotify_url": "https://open.spotify.com/track/xyz"}
    monkeypatch.setattr(main, "_spotify_lookup_cached", fake_lookup)

    body = (await client.get("/api/now-playing", params={"station": "Test FM"})).json()
    # Playlist cover wins; the lookup still runs, for the spotify_url.
    assert body["current"]["cover_url"] == "https://playlist.example/cover.jpg"
    assert calls["n"] == 1


async def test_playlist_track_persists_to_db(client, monkeypatch, db_session):
    _mock_icy(monkeypatch)
    _mock_listeners(monkeypatch, None)
    _mock_playlist(monkeypatch, {"artist": "Prospa", "title": "This Rhythm", "cover_url": None})

    await client.get("/api/now-playing", params={"station": "Test FM"})

    rows = (await db_session.execute(select(PlayedTrack))).scalars().all()
    assert [(r.artist, r.title, r.station) for r in rows] == [
        ("Prospa", "This Rhythm", "Test FM")
    ]


async def test_playlist_wrapper_skips_stations_without_a_feed(monkeypatch):
    """_playlist_now_playing_cached short-circuits on stations not in
    sunshine_playlist.CHANNELS, so the ~20 stations with no feed never pay for
    a request. Tests the real wrapper directly — the route-level tests above
    stub it out, so nothing else covers this branch."""
    calls = {"n": 0}
    async def fake_fetch(station):
        calls["n"] += 1
        return {"artist": "A", "title": "B", "cover_url": None}
    monkeypatch.setattr(main.sunshine_playlist, "fetch_for_station", fake_fetch)

    assert await _REAL_PLAYLIST_CACHED("Antenne Bayern") is None
    assert calls["n"] == 0


async def test_playlist_wrapper_caches_including_negative_results(monkeypatch):
    """A station between tracks must not be re-queried on every 20s poll."""
    calls = {"n": 0}
    async def fake_fetch(station):
        calls["n"] += 1
        return None
    monkeypatch.setattr(main.sunshine_playlist, "fetch_for_station", fake_fetch)

    assert await _REAL_PLAYLIST_CACHED("Sunshine Live Techno") is None
    assert await _REAL_PLAYLIST_CACHED("Sunshine Live Techno") is None
    assert calls["n"] == 1


# ── HLS stations ───────────────────────────────────────────────────────────────
# m2o, m2o Dance and Dub Ninja serve .m3u8 playlists rather than an audio
# stream with interleaved metadata. There is no ICY concept to read, so the
# route must not spend a request trying, and Shazam is their only track source.

async def test_hls_station_does_not_attempt_an_icy_read(client, monkeypatch):
    """An ICY read against a playlist URL would parse .m3u8 markup as audio and
    always come back empty — one wasted request per poll, every poll."""
    calls = {"n": 0}

    async def fake_icy(url):
        calls["n"] += 1
        return dict(EMPTY_READ)

    monkeypatch.setattr(main, "_read_icy_now_playing_cached", fake_icy)
    _mock_shazam(monkeypatch, {"artist": "A", "title": "T", "cover_url": None})

    r = await client.get("/api/now-playing", params={"station": "HLS FM"})
    assert r.status_code == 200
    assert calls["n"] == 0


async def test_non_hls_station_still_reads_icy(client, monkeypatch):
    """The other side of the branch above — the ICY path must be untouched."""
    calls = {"n": 0}

    async def fake_icy(url):
        calls["n"] += 1
        return {**EMPTY_READ, "artist": "Real", "title": "Track"}

    monkeypatch.setattr(main, "_read_icy_now_playing_cached", fake_icy)

    r = await client.get("/api/now-playing", params={"station": "Test FM"})
    assert calls["n"] == 1
    assert r.json()["current"]["title"] == "Track"


async def test_hls_station_gets_its_track_from_shazam(client, monkeypatch):
    _mock_shazam(monkeypatch, {
        "artist": "Unit 2", "title": "Sunshine (Kink Remix)",
        "cover_url": "https://img/cover.jpg",
    })
    r = await client.get("/api/now-playing", params={"station": "HLS FM"})
    current = r.json()["current"]
    assert current["title"] == "Sunshine (Kink Remix)"
    assert current["artist"] == "Unit 2"
    assert current["source"] == "shazam"
    # Shazam supplies its own cover art, so no Spotify lookup is needed.
    assert current["cover_url"] == "https://img/cover.jpg"


async def test_hls_station_reports_empty_stream_info(client, monkeypatch):
    """No ICY read means no format/bitrate headers. These must come back None
    rather than stale or invented values — the frontend falls back to the
    static per-station values in stations.json when they do."""
    _mock_shazam(monkeypatch, {"artist": "A", "title": "T", "cover_url": None})
    r = await client.get("/api/now-playing", params={"station": "HLS FM"})
    info = r.json()["stream_info"]
    assert info["format"] is None
    assert info["bitrate"] is None
    assert info["sample_rate"] is None


async def test_hls_station_with_no_shazam_match_returns_no_track(client, monkeypatch):
    """Speech radio (BBC Radio 4) legitimately never matches. That's an empty
    result, not an error — the frontend shows its generic live placeholder."""
    _mock_shazam(monkeypatch, None)
    r = await client.get("/api/now-playing", params={"station": "HLS FM"})
    assert r.status_code == 200
    assert r.json()["current"] == {}


# ── GET /api/track-stats ("heard this before") ─────────────────────────────────

def _played(artist, title, station, offset_seconds=0):
    """A PlayedTrack with a station, for the history-derived features."""
    started = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return PlayedTrack(title=title, artist=artist, station=station,
                       started_at=started.replace(tzinfo=None))


async def _seed(db_session, rows):
    for r in rows:
        db_session.add(r)
    await db_session.commit()


async def test_track_stats_unknown_track_returns_zero(client):
    r = await client.get("/api/track-stats", params={"artist": "Nobody", "title": "Nothing"})
    assert r.status_code == 200
    assert r.json() == {"plays": 0, "first_heard": None, "last_heard": None,
                        "first_station": None, "stations": []}


async def test_track_stats_counts_plays_across_stations(client, db_session):
    await _seed(db_session, [
        _played("Anyma", "Eternity", "Station A", -300),
        _played("Anyma", "Eternity", "Station B", -200),
        _played("Anyma", "Eternity", "Station A", -100),
    ])
    body = (await client.get("/api/track-stats",
                             params={"artist": "Anyma", "title": "Eternity"})).json()
    assert body["plays"] == 3
    assert {s["station"]: s["plays"] for s in body["stations"]} == {"Station A": 2, "Station B": 1}


async def test_track_stats_first_station_is_the_earliest_play(client, db_session):
    """"First heard on X" must follow started_at, not insertion order."""
    await _seed(db_session, [
        _played("Anyma", "Eternity", "Station B", -100),   # inserted first, played later
        _played("Anyma", "Eternity", "Station A", -900),   # the actual first play
    ])
    body = (await client.get("/api/track-stats",
                             params={"artist": "Anyma", "title": "Eternity"})).json()
    assert body["first_station"] == "Station A"


async def test_track_stats_timestamps_carry_an_explicit_utc_offset(client, db_session):
    """started_at is a naive TIMESTAMP holding UTC. Serialised bare it reads as
    "2026-08-21T14:03:00", which JavaScript's Date() parses as *local* time —
    the same naive/aware trap that shipped a bug against Postgres, pointed the
    other way. The offset must be explicit at the API boundary."""
    await _seed(db_session, [_played("Anyma", "Eternity", "Station A")])
    body = (await client.get("/api/track-stats",
                             params={"artist": "Anyma", "title": "Eternity"})).json()
    assert body["first_heard"].endswith("+00:00")
    assert body["last_heard"].endswith("+00:00")


async def test_track_stats_matches_exactly_not_by_prefix(client, db_session):
    await _seed(db_session, [
        _played("Anyma", "Eternity", "Station A"),
        _played("Anyma", "Eternity (Remix)", "Station A"),
        _played("Anyma & Someone", "Eternity", "Station A"),
    ])
    body = (await client.get("/api/track-stats",
                             params={"artist": "Anyma", "title": "Eternity"})).json()
    assert body["plays"] == 1


# ── GET /api/similar-stations ──────────────────────────────────────────────────

async def test_similar_stations_rejects_unknown_station(client):
    r = await client.get("/api/similar-stations", params={"station": "Totally Made Up"})
    assert r.status_code == 404


async def test_similar_stations_empty_without_history(client):
    body = (await client.get("/api/similar-stations", params={"station": "Test FM"})).json()
    assert body == {"station": "Test FM", "similar": []}


async def test_similar_stations_ranks_by_jaccard_not_shared_count(client, db_session):
    """The whole reason for Jaccard rather than a raw shared-artist count.

    Station A shares *more* artists in absolute terms but has a huge catalogue,
    so the overlap is a small fraction of it. Station B is a near-perfect match.
    Ranking by count would put A first, which is how "the station with the most
    history wins every list" happens — in the real data Pure Ibiza Radio holds
    ~60% of all rows.
    """
    rows = [_played("Anyma", "T1", "Test FM"),
            _played("Argy", "T2", "Test FM"),
            _played("Bedouin", "T3", "Test FM")]
    # Station A: shares all 3, but 40 artists in total -> 3/40 = 0.075
    rows += [_played(a, f"x{i}", "Station A") for i, a in enumerate(["Anyma", "Argy", "Bedouin"])]
    rows += [_played(f"Filler {i}", f"f{i}", "Station A") for i in range(37)]
    # Station B: shares 2, and has only those 2 -> 2/3 = 0.667
    rows += [_played("Anyma", "y1", "Station B"), _played("Argy", "y2", "Station B")]
    await _seed(db_session, rows)

    similar = (await client.get("/api/similar-stations",
                                params={"station": "Test FM"})).json()["similar"]
    assert [s["station"] for s in similar] == ["Station B", "Station A"]
    assert similar[0]["shared_artists"] == 2   # fewer shared...
    assert similar[1]["shared_artists"] == 3   # ...but ranked higher
    assert similar[0]["score"] > similar[1]["score"]


async def test_similar_stations_excludes_itself(client, db_session):
    await _seed(db_session, [
        _played("Anyma", "T1", "Test FM"),
        _played("Anyma", "T2", "Station A"),
    ])
    similar = (await client.get("/api/similar-stations",
                                params={"station": "Test FM"})).json()["similar"]
    assert [s["station"] for s in similar] == ["Station A"]


async def test_similar_stations_excludes_stations_not_in_the_registry(client, db_session):
    """The table also holds rows for stations since removed from stations.json.
    Recommending one the picker can't select would be a dead end."""
    await _seed(db_session, [
        _played("Anyma", "T1", "Test FM"),
        _played("Anyma", "T2", "Retired Station"),
    ])
    similar = (await client.get("/api/similar-stations",
                                params={"station": "Test FM"})).json()["similar"]
    assert similar == []


async def test_similar_stations_matches_artists_case_insensitively(client, db_session):
    """Broadcasters are wildly inconsistent about capitalisation — the real
    data has both "ANYMA" and "Anyma" styles across stations."""
    await _seed(db_session, [
        _played("Anyma", "T1", "Test FM"),
        _played("ANYMA", "T2", "Station A"),
    ])
    similar = (await client.get("/api/similar-stations",
                                params={"station": "Test FM"})).json()["similar"]
    assert len(similar) == 1
    assert similar[0]["shared_artists"] == 1


async def test_similar_stations_returns_example_artists(client, db_session):
    await _seed(db_session, [
        _played("Anyma", "T1", "Test FM"), _played("Argy", "T2", "Test FM"),
        _played("Anyma", "T3", "Station A"), _played("Argy", "T4", "Station A"),
    ])
    similar = (await client.get("/api/similar-stations",
                                params={"station": "Test FM"})).json()["similar"]
    assert similar[0]["examples"] == ["Anyma", "Argy"]


async def test_similar_stations_respects_limit(client, db_session):
    rows = [_played("Anyma", "T1", "Test FM")]
    rows += [_played("Anyma", f"t{i}", s) for i, s in enumerate(["Station A", "Station B", "Ibiza Pura"])]
    await _seed(db_session, rows)

    similar = (await client.get("/api/similar-stations",
                                params={"station": "Test FM", "limit": 2})).json()["similar"]
    assert len(similar) == 2


# ── spotify_url persistence ────────────────────────────────────────────────────

async def test_spotify_url_is_persisted_on_the_row(client, monkeypatch, db_session):
    """The column existed from the first schema and nothing ever wrote to it —
    all 1,548 rows logged before this had it NULL while this exact lookup was
    being made and its URL discarded."""
    _mock_icy(monkeypatch, artist="Anyma", title="Eternity")
    _mock_listeners(monkeypatch, None)
    _mock_cover_art(monkeypatch, "https://img/cover.jpg",
                    spotify_url="https://open.spotify.com/track/abc123")

    await client.get("/api/now-playing", params={"station": "Test FM"})

    row = (await db_session.execute(select(PlayedTrack))).scalars().one()
    assert row.spotify_url == "https://open.spotify.com/track/abc123"


async def test_no_spotify_match_leaves_url_null(client, monkeypatch, db_session):
    _mock_icy(monkeypatch, artist="Anyma", title="Eternity")
    _mock_listeners(monkeypatch, None)
    _mock_cover_art(monkeypatch, None)

    await client.get("/api/now-playing", params={"station": "Test FM"})

    row = (await db_session.execute(select(PlayedTrack))).scalars().one()
    assert row.spotify_url is None


# ── POST /api/identify (the "Name this track" button) ──────────────────────────
# Replaces a link to shazam.com's homepage, where the user had to click Shazam's
# mic button and let it listen through the *device microphone* — so it heard the
# room, not the stream. This runs the recognition the server already does.

def _mock_recognize(monkeypatch, result, calls=None):
    async def fake(url):
        if calls is not None:
            calls["n"] += 1
            calls["url"] = url
        return result
    monkeypatch.setattr(main.shazam_fallback, "recognize_stream", fake)


async def test_identify_rejects_unknown_station(client):
    """Same SSRF reasoning as /api/now-playing — a name, never a URL."""
    r = await client.post("/api/identify", params={"station": "Totally Made Up"})
    assert r.status_code == 404


async def test_identify_returns_the_match(client, monkeypatch):
    _mock_recognize(monkeypatch, {"artist": "Anyma", "title": "Eternity",
                                  "cover_url": "https://img/c.jpg",
                                  "shazam_url": "https://www.shazam.com/track/123"})
    body = (await client.post("/api/identify", params={"station": "Test FM"})).json()
    assert body["match"]["artist"] == "Anyma"
    assert body["match"]["title"] == "Eternity"
    # The deep link is the point: the old button could only reach shazam.com's
    # front page, never the identified track.
    assert body["match"]["shazam_url"] == "https://www.shazam.com/track/123"


async def test_identify_bypasses_the_negative_cache(client, monkeypatch):
    """The whole reason this endpoint exists rather than reusing
    _shazam_fallback_cached.

    The automatic path caches *misses* for SHAZAM_CACHE_TTL, and the user
    presses this button precisely because the automatic path came back empty.
    Replaying the cached miss would make the button look broken.
    """
    main._shazam_cache["Test FM"] = (time.monotonic(), None)   # a fresh cached miss
    calls = {"n": 0}
    _mock_recognize(monkeypatch, {"artist": "A", "title": "B", "cover_url": None,
                                  "shazam_url": None}, calls)

    body = (await client.post("/api/identify", params={"station": "Test FM"})).json()

    assert calls["n"] == 1          # actually re-listened
    assert body["match"]["title"] == "B"


async def test_identify_primes_the_cache_for_the_next_poll(client, monkeypatch):
    """On success the result goes into _shazam_cache, so the next
    /api/now-playing picks it up through the normal path and persists it —
    rather than this endpoint carrying a second copy of the persistence logic."""
    _mock_recognize(monkeypatch, {"artist": "Anyma", "title": "Eternity",
                                  "cover_url": None, "shazam_url": None})

    await client.post("/api/identify", params={"station": "Test FM"})

    cached = main._shazam_cache.get("Test FM")
    assert cached is not None and cached[1]["title"] == "Eternity"


async def test_identify_no_match_is_200_not_an_error(client, monkeypatch):
    """A miss is a normal outcome — speech radio, a DJ talking over the intro,
    a track Shazam doesn't hold. The frontend needs to say "no match", not
    "request failed"."""
    _mock_recognize(monkeypatch, None)
    r = await client.post("/api/identify", params={"station": "Test FM"})
    assert r.status_code == 200
    assert r.json() == {"match": None}


async def test_identify_blank_title_counts_as_no_match(client, monkeypatch):
    _mock_recognize(monkeypatch, {"artist": "A", "title": "", "cover_url": None})
    body = (await client.post("/api/identify", params={"station": "Test FM"})).json()
    assert body == {"match": None}


async def test_identify_does_not_cache_a_miss(client, monkeypatch):
    """A no-match must not poison the cache — otherwise pressing the button
    would suppress the automatic fallback for the next SHAZAM_CACHE_TTL."""
    _mock_recognize(monkeypatch, None)
    await client.post("/api/identify", params={"station": "Test FM"})
    assert "Test FM" not in main._shazam_cache


async def test_identify_rejects_a_concurrent_request_for_the_same_station(client, monkeypatch):
    """A recognition is a real ~10-15s capture. Without the guard, tapping the
    button repeatedly stacks concurrent captures against the same stream."""
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(url):
        started.set()
        await release.wait()
        return {"artist": "A", "title": "B", "cover_url": None, "shazam_url": None}

    monkeypatch.setattr(main.shazam_fallback, "recognize_stream", slow)

    first = asyncio.create_task(client.post("/api/identify", params={"station": "Test FM"}))
    await started.wait()
    second = await client.post("/api/identify", params={"station": "Test FM"})
    release.set()

    assert second.status_code == 429
    assert (await first).status_code == 200


async def test_identify_allows_a_different_station_concurrently(client, monkeypatch):
    """The guard is per station, not global — two stations can be identified at
    once."""
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(url):
        started.set()
        await release.wait()
        return {"artist": "A", "title": "B", "cover_url": None, "shazam_url": None}

    monkeypatch.setattr(main.shazam_fallback, "recognize_stream", slow)

    first = asyncio.create_task(client.post("/api/identify", params={"station": "Station A"}))
    await started.wait()
    release.set()
    second = await client.post("/api/identify", params={"station": "Station B"})

    assert second.status_code == 200
    assert (await first).status_code == 200


async def test_identify_releases_the_guard_when_recognition_raises(client, monkeypatch):
    """recognize_stream is documented never to raise, but if it ever did, a
    leaked flag would make that station permanently unidentifiable."""
    async def boom(url):
        raise RuntimeError("vibra exploded")

    monkeypatch.setattr(main.shazam_fallback, "recognize_stream", boom)

    with pytest.raises(RuntimeError):
        await client.post("/api/identify", params={"station": "Test FM"})

    assert "Test FM" not in main._identify_in_flight


async def test_identify_works_for_hls_stations(client, monkeypatch):
    """HLS stations have no broadcaster metadata at all, so the manual button
    matters most there."""
    calls = {"n": 0}
    _mock_recognize(monkeypatch, {"artist": "A", "title": "B", "cover_url": None,
                                  "shazam_url": None}, calls)

    body = (await client.post("/api/identify", params={"station": "HLS FM"})).json()

    assert body["match"]["title"] == "B"
    assert calls["url"].endswith(".m3u8")


# ── Stale current-track expiry ─────────────────────────────────────────────────
# state["current"] used to be written and never cleared, so the first track a
# station ever matched stayed on screen indefinitely — a Shazam-only station
# like Radio Panama could sit on one stale, possibly wrong, guess for hours.

async def _poll(client, station="Test FM"):
    return (await client.get("/api/now-playing", params={"station": station})).json()


def _freeze(monkeypatch, at):
    monkeypatch.setattr(main.time, "monotonic", lambda: at)


async def test_current_track_expires_when_nothing_confirms_it(client, monkeypatch):
    _mock_icy(monkeypatch, artist="Anyma", title="Eternity")
    _mock_listeners(monkeypatch, None)
    assert (await _poll(client))["current"]["title"] == "Eternity"

    # The station goes quiet and more than the TTL passes.
    _mock_icy(monkeypatch)
    _freeze(monkeypatch, time.monotonic() + main.CURRENT_TRACK_TTL + 1)

    assert (await _poll(client))["current"] == {}


async def test_current_track_survives_a_brief_gap(client, monkeypatch):
    """A couple of failed recognitions mid-track — a DJ talking over an intro,
    a bad transition — must not blank a correct answer."""
    _mock_icy(monkeypatch, artist="Anyma", title="Eternity")
    _mock_listeners(monkeypatch, None)
    await _poll(client)

    _mock_icy(monkeypatch)
    _freeze(monkeypatch, time.monotonic() + main.CURRENT_TRACK_TTL - 10)

    assert (await _poll(client))["current"]["title"] == "Eternity"


async def test_a_confirming_poll_refreshes_the_expiry(client, monkeypatch):
    """Why confirmed_at updates on every poll that yields a title, not only on
    a *change*: a working ICY station repeats its StreamTitle on every read, so
    a long track must keep being renewed rather than ageing out mid-play."""
    _mock_icy(monkeypatch, artist="Anyma", title="Eternity")
    _mock_listeners(monkeypatch, None)
    t0 = time.monotonic()
    await _poll(client)

    # Nearly the whole TTL passes, then the same track is reported again.
    _freeze(monkeypatch, t0 + main.CURRENT_TRACK_TTL - 5)
    await _poll(client)

    # Another near-TTL passes with silence. Without the refresh above, the
    # total elapsed (~2x TTL) would have expired it.
    _mock_icy(monkeypatch)
    _freeze(monkeypatch, t0 + 2 * (main.CURRENT_TRACK_TTL - 5))

    assert (await _poll(client))["current"]["title"] == "Eternity"


async def test_expired_track_is_moved_to_history(client, monkeypatch):
    """It did play — same as when a new track displaces it."""
    _mock_icy(monkeypatch, artist="Anyma", title="Eternity")
    _mock_listeners(monkeypatch, None)
    await _poll(client)

    _mock_icy(monkeypatch)
    _freeze(monkeypatch, time.monotonic() + main.CURRENT_TRACK_TTL + 1)
    body = await _poll(client)

    assert body["current"] == {}
    assert [h["title"] for h in body["history"]] == ["Eternity"]


async def test_expiry_does_not_fire_when_there_is_no_current_track(client, monkeypatch):
    """A station that has never matched anything must not push an empty dict
    into history on every poll."""
    _mock_icy(monkeypatch)
    _mock_listeners(monkeypatch, None)
    _freeze(monkeypatch, time.monotonic() + main.CURRENT_TRACK_TTL * 10)

    body = await _poll(client)
    assert body["current"] == {}
    assert body["history"] == []


async def test_a_working_icy_station_never_expires(client, monkeypatch):
    """The safety property that makes a blanket TTL acceptable: a healthy
    station reports its title on every read, so it re-confirms continuously.
    Verified against the real streams — Antenne Bayern and Milano Lounge both
    return the same title on three consecutive uncached reads."""
    _mock_icy(monkeypatch, artist="Anyma", title="Eternity")
    _mock_listeners(monkeypatch, None)
    t = time.monotonic()
    for _ in range(6):
        t += main.CURRENT_TRACK_TTL - 1
        _freeze(monkeypatch, t)
        body = await _poll(client)

    assert body["current"]["title"] == "Eternity"
