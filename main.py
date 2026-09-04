from contextlib import asynccontextmanager
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import json
import re
import time

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy import func, select

from database import engine, Base, get_session
import models  # noqa: F401 — registers ORM models
from models import PlayedTrack
import spotify
import shazam_fallback
import sunshine_playlist

# ── Config ────────────────────────────────────────────────────────────────────
META_CACHE_TTL = 15    # seconds — avoid re-hitting a station's origin on rapid re-polls
# Shazam fallback is expensive (a real ~10s audio capture + network round trip,
# vs a near-instant header read) — cache aggressively, including negative
# results, so a station stuck with no ICY metadata doesn't get hammered every
# poll. Deliberately >= the frontend's 20s poll interval so most polls hit cache.
SHAZAM_CACHE_TTL = 45  # seconds

# How long a track stays "current" without any source confirming it again.
#
# Safe to apply to every station because a working ICY stream repeats its
# StreamTitle on *every* read, not only when the track changes — verified
# directly against Antenne Bayern and Milano Lounge, three consecutive
# uncached reads each, all returning the same title. So a healthy station
# refreshes this on every poll and never expires; only a station that has
# genuinely stopped producing a title ages out.
#
# 180s is roughly four failed Shazam attempts (recognition is re-tried at most
# every SHAZAM_CACHE_TTL): long enough that a couple of misses mid-track — a DJ
# talking over an intro, a bad transition — don't blank a correct answer, short
# enough that a wrong or long-finished guess doesn't sit there indefinitely.
CURRENT_TRACK_TTL = 180  # seconds
# The station's own published playlist (sunshine_playlist.py) is a plain JSON
# GET, so it's cached like the ICY read rather than as aggressively as Shazam.
PLAYLIST_CACHE_TTL = 15  # seconds

# ── Station registry ─────────────────────────────────────────────────────────
# stations.json is the single source of truth for which streams this app knows
# about. The frontend fetches it via /api/stations rather than hardcoding its
# own copy, so there's exactly one list to keep current.
#
# It is also the security boundary. /api/now-playing takes a station *name*,
# never a URL: the server resolves the name to a URL from this file. An earlier
# version accepted `?url=` straight from the client and opened it server-side —
# a server-side request forgery (SSRF) hole, where an attacker passes something
# like http://169.254.169.254/ (cloud instance-metadata) or http://localhost:5432
# and makes the backend fetch internal endpoints it can reach but they can't.
# Resolving names server-side removes the attacker's control over the URL
# entirely, which is why the parameter was dropped rather than filtered.
STATIONS: list[dict] = json.loads((Path(__file__).parent / "stations.json").read_text())
_STATION_URLS: dict[str, str] = {s["name"]: s["url"] for s in STATIONS}

# station name -> (Icecast status-json.xsl URL, mount substring to match).
# Only stations where the mount could be confidently identified by inspecting
# control.streaming-pro.com's status-json.xsl directly and matching listenurl
# to the station's actual stream filename. Sonica deliberately excluded —
# its port has 4 mounts (AutoDj.mp3, ibizaglobalclassics.mp3, livemain.mp3,
# radiojar) and none match "ibizasonica" closely enough to be sure which one
# it actually is; better to show "—" than a confidently-wrong number.
ICECAST_STATUS: dict[str, tuple[str, str]] = {
    "Pure Ibiza Radio":      ("https://control.streaming-pro.com:8028/status-json.xsl", "stream.mp3"),
    "Ibiza Global Classics": ("https://control.streaming-pro.com:8000/status-json.xsl", "ibizaglobalclassics.mp3"),
    "Ibiza Global Radio":    ("https://control.streaming-pro.com:8024/status-json.xsl", "stream.aac"),
}

# ── In-memory state ──────────────────────────────────────────────────────────
# e.g. {"Pure Ibiza Radio": {"current": {...}, "history": deque([...])}}
_station_state: dict[str, dict] = {}
_meta_cache: dict[str, tuple[float, dict]] = {}          # stream url -> (fetched_at, result)
_icecast_cache: dict[str, tuple[float, dict | None]] = {}  # status url -> (fetched_at, parsed json)
_shazam_cache: dict[str, tuple[float, dict | None]] = {}   # station -> (fetched_at, result)
_playlist_cache: dict[str, tuple[float, dict | None]] = {} # station -> (fetched_at, result)
_spotify_cache: dict[str, str | None] = {}               # "artist|title" -> cover_url (no TTL — a song's art doesn't change)


# ── Stream format (from the ICY response's own headers) ────────────────────────
def _parse_stream_format(headers) -> dict:
    """Format/bitrate/sample-rate straight from the stream's own HTTP headers —
    available on any successful ICY connection, regardless of whether a track
    title is (title needs a broadcaster that bothers sending one; these are
    just properties of the encoder itself).

    Two real-world messes confirmed by hitting actual stations, not guessed:
    - icy-br is sometimes a plain number, sometimes a comma-joined duplicate
      like "256, 256" (some Icecast encoders send the header twice and httpx
      merges duplicates with a comma) — int() on that raises ValueError.
    - icy-audio-info's param names vary by encoder: laut.fm sends
      "ice-channels=2;ice-samplerate=44100;ice-bitrate=128" (ice- prefixed),
      Pure Ibiza Radio sends "bitrate=256;samplerate=48000;channels=2" (no
      prefix). Both need matching, or bitrate/sample_rate silently stay None
      for anything using the second style.
    An uncaught exception anywhere in here previously blew up the *entire*
    read (caught by the outer try/except in _read_icy_now_playing) and wiped
    out the track title too, not just the format fields — hence the
    conservative regex + _first_int rather than a bare int(header).
    """
    ctype = (headers.get("content-type") or "").lower()
    fmt_map = {"audio/mpeg": "MP3", "audio/aac": "AAC", "audio/aacp": "AAC+", "audio/ogg": "OGG"}
    fmt = fmt_map.get(ctype) or (ctype.split("/")[-1].upper() if "/" in ctype else None)

    # Header *name* itself is inconsistent too, not just its internal params —
    # laut.fm sends "icy-audio-info", Pure Ibiza Radio sends "ice-audio-info"
    # (no y). Confirmed against both real streams, not guessed.
    audio_info = headers.get("icy-audio-info") or headers.get("ice-audio-info") or ""
    br = _first_int(headers.get("icy-br")) or _regex_int(r"(?:ice-)?bitrate=(\d+)", audio_info)
    sr = _first_int(headers.get("icy-sr")) or _regex_int(r"(?:ice-)?samplerate=(\d+)", audio_info)

    return {"format": fmt, "bitrate": br, "sample_rate": sr}


def _regex_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def _first_int(value: str | None) -> int | None:
    if not value:
        return None
    m = re.search(r"\d+", value)
    return int(m.group()) if m else None


# ── ICY in-band metadata reader ─────────────────────────────────────────────────
async def _read_icy_now_playing(url: str, timeout: float = 10.0) -> dict:
    """Read live info from an ICY stream: track title (if the broadcaster sends
    one and it just changed) plus format/bitrate/sample_rate (from the stream's
    own headers).

    Always returns a dict, never None. "artist"/"title" are empty strings when
    there's no (new) track to report — that's a normal state (no title change
    since the last read, or this station never sends real titles at all), not
    a failure. format/bitrate/sample_rate are None only on a hard connection
    failure or a stream that genuinely doesn't send those headers.
    """
    result = {"artist": "", "title": "", "format": None, "bitrate": None, "sample_rate": None}
    try:
        # follow_redirects: several of these stations 302 to a geo-nearest edge
        # node (e.g. stream.rcs.revma.com -> nXX-eu.rcs.revma.com) — unlike
        # curl -L or urllib, httpx does NOT follow redirects by default. Without
        # this, we'd silently read the 302 response itself (no icy-metaint at
        # all) instead of the actual stream.
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream(
                "GET", url,
                headers={"Icy-MetaData": "1", "User-Agent": "afterhours-fm/1.0"},
            ) as resp:
                result.update(_parse_stream_format(resp.headers))

                # Kept to reject stations that broadcast their own name as the
                # "current track" forever — see the icy_name check below.
                icy_name = (resp.headers.get("icy-name") or "").strip()

                metaint = int(resp.headers.get("icy-metaint", 0))
                if not metaint:
                    return result   # e.g. non-ICY server — format info above still stands

                buf = bytearray()
                target = metaint + 1        # phase 1: audio bytes + the 1 length byte
                length = None
                async for chunk in resp.aiter_bytes(chunk_size=4096):
                    buf += chunk
                    if length is None and len(buf) >= target:
                        length = buf[metaint] * 16   # length byte * 16 = metadata block size
                        if length == 0:
                            return result   # no title change since the broadcaster's last block
                        target = metaint + 1 + length   # phase 2: + the metadata bytes
                    if length is not None and len(buf) >= target:
                        meta_bytes = bytes(buf[metaint + 1 : metaint + 1 + length])
                        break
                else:
                    return result   # stream ended before a full metadata block arrived
    except Exception:
        return result

    decoded = meta_bytes.decode("utf-8", errors="replace")
    m = re.search(r"StreamTitle='([^;]*)';", decoded)
    if not m or not m.group(1):
        return result
    raw = m.group(1).strip()
    if icy_name and raw.casefold() == icy_name.casefold():
        # The broadcaster is announcing itself, not a track. All three Sunshine
        # Live streams do this permanently: icy-name and StreamTitle are both
        # e.g. "SUNSHINE LIVE - Techno", forever. Left unfiltered it was worse
        # than sending nothing — the " - " split below turned it into
        # artist="SUNSHINE LIVE" / title="Techno", which looked like a real
        # track, got persisted as a PlayedTrack row, and (being a non-empty
        # title) suppressed the fallbacks in now_playing() that would have
        # found the actual song.
        #
        # Matching against this station's own icy-name rather than a hardcoded
        # list of bad titles: it needs no per-station upkeep, and it was
        # verified against every station in stations.json — the three Sunshine
        # streams are the only ones where the two headers are equal, so no
        # working station changes behaviour.
        return result
    if "{" in raw:
        # some broadcasters stuff a raw JSON blob in here instead of a plain title —
        # e.g. Pure Ibiza Radio sends StreamTitle='NOW ON AIR   {"autor":"...",...}'
        return result
    if " - " not in raw:
        # No "Artist - Title" separator: a show or station ident, not a track.
        # Verified against the 1,548 rows already logged — every artist-less row
        # was junk: 31x Blue Marlin's "Djs Blue Marlin Sessions", 2x a bare "-",
        # and one Deep Vibes show name. That Blue Marlin ident was the single
        # most "played" track in the database, which would have quietly skewed
        # both the play counts and the station-similarity scores below.
        #
        # Distinct from the icy_name check above: this catches a broadcaster
        # announcing a *show* rather than repeating its own station name, which
        # is why Blue Marlin slipped past that guard.
        return result
    artist, title = raw.split(" - ", 1)
    result["artist"], result["title"] = artist.strip(), title.strip()
    return result


async def _read_icy_now_playing_cached(url: str) -> dict:
    """Same as _read_icy_now_playing, but skips the network round-trip if we
    fetched this exact URL within the last META_CACHE_TTL seconds."""
    now = time.monotonic()
    cached = _meta_cache.get(url)
    if cached and now - cached[0] < META_CACHE_TTL:
        return cached[1]
    result = await _read_icy_now_playing(url)
    _meta_cache[url] = (now, result)
    return result


# ── Icecast listener counts (only for stations in ICECAST_STATUS) ──────────────
async def _read_icecast_listeners_cached(station: str) -> int | None:
    entry = ICECAST_STATUS.get(station)
    if not entry:
        return None
    status_url, mount = entry

    now = time.monotonic()
    cached = _icecast_cache.get(status_url)
    if cached and now - cached[0] < META_CACHE_TTL:
        data = cached[1]
    else:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(status_url)
                data = resp.json()
        except Exception:
            data = None
        _icecast_cache[status_url] = (now, data)

    if not data:
        return None
    # Icecast returns icestats.source as a dict when there's exactly one mount
    # on that port, or a list when there are several — normalize to a list.
    src = data.get("icestats", {}).get("source", [])
    if isinstance(src, dict):
        src = [src]
    for s in src:
        if mount in (s.get("listenurl") or ""):
            return s.get("listeners")
    return None


# ── Broadcaster playlist (only when ICY gives nothing) ─────────────────────────
async def _playlist_now_playing_cached(station: str) -> dict | None:
    """Rate-limited wrapper around sunshine_playlist.fetch_for_station().
    Returns None for stations that have no playlist feed, so the caller can
    call it unconditionally. Negative results are cached too — a station
    between tracks shouldn't be re-queried on every poll."""
    if station not in sunshine_playlist.CHANNELS:
        return None
    now = time.monotonic()
    cached = _playlist_cache.get(station)
    if cached and now - cached[0] < PLAYLIST_CACHE_TTL:
        return cached[1]
    result = await sunshine_playlist.fetch_for_station(station)
    _playlist_cache[station] = (now, result)
    return result


# ── Shazam fallback (only when ICY gives nothing) ───────────────────────────────
# Manual identify requests currently running, keyed by station. A recognition
# is a real ~10-15s audio capture, so without this a user tapping the button
# repeatedly would stack concurrent captures against the same stream.
_identify_in_flight: set[str] = set()


async def _shazam_fallback_cached(station: str, url: str) -> dict | None:
    """Rate-limited wrapper around shazam_fallback.recognize_stream() — see
    SHAZAM_CACHE_TTL. Caches negative results too, so a station that just
    doesn't match well isn't retried every single poll."""
    now = time.monotonic()
    cached = _shazam_cache.get(station)
    if cached and now - cached[0] < SHAZAM_CACHE_TTL:
        return cached[1]
    result = await shazam_fallback.recognize_stream(url)
    _shazam_cache[station] = (now, result)
    return result


# ── Cover art (Spotify search, for ICY-sourced tracks — Shazam brings its own) ──
async def _spotify_lookup_cached(artist: str, title: str) -> dict | None:
    """Best-effort Spotify search — never raises. Returns
    {"cover_url", "spotify_url"} or None.

    This was cover-art-only. It now also returns the track's Spotify URL so it
    can be persisted on the PlayedTrack row: that column has existed since the
    first schema and nothing ever wrote to it, so all 1,548 rows logged before
    this had spotify_url NULL *while this very lookup was being made and its
    URL thrown away*.

    Cached indefinitely per artist|title — neither a song's cover nor its
    Spotify URL changes — with a simple size cap so it can't grow forever over
    a long-running process.
    """
    key = f"{artist}|{title}"
    if key in _spotify_cache:
        return _spotify_cache[key]
    if len(_spotify_cache) > 1000:
        _spotify_cache.clear()
    try:
        match = await spotify.search_track(artist, title)
        result = {"cover_url": match.get("cover_url"),
                  "spotify_url": match.get("spotify_url")} if match else None
    except spotify.SpotifyNotConfigured:
        # A settled answer for the life of the process: credentials are read
        # from the environment at call time and won't appear mid-run. Cache it
        # so an unconfigured install doesn't retry on every new track.
        result = None
    except Exception:
        # Rate limit (429), timeout, upstream 5xx — the lookup never completed,
        # so there is no answer to remember. Returning without caching is the
        # whole point: this cache has no TTL, so storing a transient failure
        # blanked that track's cover *permanently*. Flipping quickly through
        # stations bursts enough searches to trip Spotify's rate limit, which
        # is exactly when a run of tracks would otherwise lose their art for
        # good. The next poll simply tries again.
        return None
    _spotify_cache[key] = result
    return result


# ── Schema migration guards ─────────────────────────────────────────────────────
async def _table_columns(conn, is_sqlite: bool) -> set[str]:
    """Column names currently on played_tracks.

    The one genuinely dialect-specific read in the codebase: SQLite has no
    information_schema, and Postgres has no PRAGMA. Shared by the guards below
    so that difference lives in exactly one place.
    """
    if is_sqlite:
        result = await conn.exec_driver_sql("PRAGMA table_info(played_tracks)")
        return {row[1] for row in result}
    result = await conn.exec_driver_sql(
        "SELECT column_name FROM information_schema.columns WHERE table_name='played_tracks'"
    )
    return {row[0] for row in result}


async def _ensure_station_column(db_engine: AsyncEngine | None = None) -> None:
    """Add played_tracks.station if it's missing, and backfill old rows.

    No Alembic in this project (see README) — this is a hand-rolled, idempotent
    guard that runs on every startup. Safe to leave in place indefinitely; once
    the column exists it's a no-op read-then-skip. Old rows predate
    multi-station support and were always Pure Ibiza Radio, hence the backfill.

    `db_engine` defaults to the app engine; the parameter exists so the test
    suite can point it at a test database. Without it this reached straight
    for the module-level engine and was effectively untestable, which left the
    SQLite/Postgres branch below unexercised — the one place in the codebase
    where the two engines need genuinely different SQL.

    The dialect is read off the engine rather than by string-matching
    DATABASE_URL: the engine is the thing actually being migrated, and asking
    it directly can't drift from whatever URL happens to be in the environment.
    """
    target = db_engine if db_engine is not None else engine
    is_sqlite = target.dialect.name == "sqlite"
    async with target.begin() as conn:
        existing = await _table_columns(conn, is_sqlite)
        if "station" not in existing:
            await conn.exec_driver_sql("ALTER TABLE played_tracks ADD COLUMN station VARCHAR")
            await conn.exec_driver_sql(
                "UPDATE played_tracks SET station = 'Pure Ibiza Radio' WHERE station IS NULL"
            )


async def _drop_rating_column(db_engine: AsyncEngine | None = None) -> None:
    """Drop played_tracks.rating — the thumbs up/down feature was removed.

    Idempotent like the guard above: once the column is gone this is a
    read-then-skip.

    It refuses to drop the column if *any* row has a non-NULL rating. The
    column was confirmed 100% empty before removal (1,548 rows, zero ratings),
    so that branch should never fire — but a migration that silently destroys
    data when its assumption turns out wrong is a bad trade for tidiness. An
    unused column costs nothing; deleted ratings are unrecoverable.

    SQLite has supported DROP COLUMN since 3.35 (2021); the app image ships
    3.46 and CI is newer still.
    """
    target = db_engine if db_engine is not None else engine
    is_sqlite = target.dialect.name == "sqlite"
    async with target.begin() as conn:
        if "rating" not in await _table_columns(conn, is_sqlite):
            return
        result = await conn.exec_driver_sql(
            "SELECT count(*) FROM played_tracks WHERE rating IS NOT NULL"
        )
        if list(result)[0][0]:
            return  # real ratings exist — keep the column rather than lose them
        await conn.exec_driver_sql("ALTER TABLE played_tracks DROP COLUMN rating")


async def _ensure_indexes(db_engine: AsyncEngine | None = None) -> None:
    """Create the read indexes if they're missing.

    Base.metadata.create_all() only emits indexes alongside a table it is
    creating, so declaring them on the model does nothing for a database that
    already exists — which is every deployed one. CREATE INDEX IF NOT EXISTS is
    supported by both SQLite and Postgres, making this the one migration here
    that needs no dialect branch.

    See models.py for why started_at is indexed ascending.
    """
    target = db_engine if db_engine is not None else engine
    async with target.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_played_tracks_started_at "
            "ON played_tracks (started_at)"
        )
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_played_tracks_artist_title "
            "ON played_tracks (artist, title)"
        )


async def _purge_ident_rows(db_engine: AsyncEngine | None = None) -> None:
    """Delete rows that are station/show idents rather than tracks.

    Both parser guards now reject these at the source, but rows persisted
    before they existed are still in the table, and they corrupt precisely the
    two features added alongside this — an ident is by definition the most
    repeated "track" on its station, so it tops any play count and creates
    fake artist overlap between stations.

    Two shapes, both verified against the real table before this was written:

    1. No artist at all (34 rows) — 31x Blue Marlin's "Djs Blue Marlin
       Sessions", 2x a bare "-", one Deep Vibes show name.
    2. The artist is a prefix of its own station's name (8 rows) — the
       icy-name split, e.g. artist "SUNSHINE LIVE" / title "Techno" on
       "Sunshine Live Techno", plus Milano Lounge's tagline logged under its
       own name. These have a non-empty artist, so rule 1 misses them.

    Rule 2 uses substr/length rather than LIKE deliberately: an artist
    containing % or _ would turn a LIKE pattern into a wildcard and could
    delete real rows. The 3-character floor guards the degenerate case of a
    one-letter artist matching every station starting with that letter.

    Idempotent, and narrow by construction: it removes only what the current
    parser would refuse to create.
    """
    target = db_engine if db_engine is not None else engine
    async with target.begin() as conn:
        await conn.exec_driver_sql(
            "DELETE FROM played_tracks "
            "WHERE artist IS NULL OR trim(artist) = '' "
            "   OR (station IS NOT NULL AND length(artist) >= 3 "
            "       AND upper(substr(station, 1, length(artist))) = upper(artist))"
        )


# ── App lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_station_column()
    await _drop_rating_column()
    await _ensure_indexes()
    await _purge_ident_rows()
    yield

app = FastAPI(lifespan=lifespan)

# ── API routes ────────────────────────────────────────────────────────────────
@app.get("/api/stations")
async def stations():
    """The station registry — the frontend builds its picker from this rather
    than carrying a second copy of the list."""
    return STATIONS


@app.get("/api/now-playing")
async def now_playing(
    station: str = Query(...),
    session: AsyncSession = Depends(get_session),
):
    """Live stream info for one station: track (if the broadcaster sent one
    and it's new, or Shazam identified one when ICY gave nothing), plus
    format/bitrate/sample-rate/listeners where available.

    Takes a station *name*, not a URL — see the STATIONS comment above for why
    (SSRF). Unknown names are rejected rather than fetched.
    """
    url = _STATION_URLS.get(station)
    if url is None:
        raise HTTPException(status_code=404, detail=f"Unknown station: {station}")

    state = _station_state.setdefault(
        station, {"current": {}, "history": deque(maxlen=10), "confirmed_at": 0.0}
    )
    if shazam_fallback.is_hls(url):
        # HLS has no ICY metadata concept at all: the URL serves a text
        # playlist rather than an audio stream with interleaved metadata, so an
        # ICY read here would parse .m3u8 markup as audio and always come back
        # empty — a wasted request per poll. Skip to the recognition chain.
        # format/bitrate stay None, which is why stations.json carries static
        # values for these stations.
        read = {"artist": "", "title": "", "format": None,
                "bitrate": None, "sample_rate": None}
    else:
        read = await _read_icy_now_playing_cached(url)
    listeners = await _read_icecast_listeners_cached(station)

    artist, title, cover_url, source = read["artist"], read["title"], None, "icy"

    if not title:
        # ICY gave nothing usable. Try the broadcaster's own published playlist
        # before Shazam: it's one JSON GET rather than a ~10s audio capture,
        # it's the station's own data rather than a fingerprint guess, and it
        # arrives with real cover art. Only a few stations publish one
        # (sunshine_playlist.CHANNELS); for everything else this is a no-op.
        playlist = await _playlist_now_playing_cached(station)
        if playlist and playlist.get("title"):
            artist, title, cover_url, source = (
                playlist["artist"], playlist["title"],
                playlist.get("cover_url"), "playlist",
            )

    if not title:
        # Still nothing — fall back to Shazam (rate-limited, see
        # SHAZAM_CACHE_TTL). Deliberately only reached when both cheaper paths
        # failed; never runs alongside a working ICY or playlist read.
        shazam = await _shazam_fallback_cached(station, url)
        if shazam and shazam.get("title"):
            artist, title, cover_url, source = shazam["artist"], shazam["title"], shazam.get("cover_url"), "shazam"

    if title:
        # Refreshed on every poll that produces a title, not only on a change —
        # that is what keeps a still-playing track alive past CURRENT_TRACK_TTL.
        state["confirmed_at"] = time.monotonic()
        key     = f"{artist}|{title}"
        cur     = state["current"]
        cur_key = f"{cur.get('artist', '')}|{cur.get('title', '')}"
        if key != cur_key:
            # ICY-sourced tracks don't come with cover art — Shazam-sourced
            # ones already do (fetched above, straight from Shazam's own data).
            # One Spotify lookup per *new* track, whatever the source. ICY
            # tracks need it for cover art; Shazam- and playlist-sourced tracks
            # already carry a cover but not a Spotify URL, and persisting that
            # is what makes the stored history clickable. Cached per
            # artist|title, so a repeat play costs nothing.
            match = await _spotify_lookup_cached(artist, title)
            if not cover_url and match:
                cover_url = match.get("cover_url")

            started_at = datetime.now(timezone.utc)
            db_track = PlayedTrack(
                title=title,
                artist=artist,
                album=None,
                station=station,
                spotify_url=match.get("spotify_url") if match else None,
                # started_at is a naive TIMESTAMP column (no tz stored) — asyncpg
                # rejects a tz-aware value outright ("can't subtract offset-naive
                # and offset-aware datetimes"), unlike sqlite which silently
                # accepts it. Strip tzinfo; the naive value is UTC by convention
                # throughout this app (matches the existing dev-DB rows).
                started_at=started_at.replace(tzinfo=None),
            )
            session.add(db_track)
            await session.commit()
            await session.refresh(db_track)

            if cur.get("title"):
                state["history"].appendleft({**cur})
            state["current"] = {
                "artist": artist,
                "title": title,
                "id": db_track.id,
                "started_at": started_at.isoformat(),
                "cover_url": cover_url,
                "source": source,
            }
    elif state["current"] and (
        time.monotonic() - state.get("confirmed_at", 0.0) > CURRENT_TRACK_TTL
    ):
        # Nothing has confirmed this track for CURRENT_TRACK_TTL, so stop
        # claiming it is playing. Without this, state["current"] was only ever
        # written and never cleared: the first track a station ever matched
        # stayed on screen indefinitely, which is why a Shazam-only station
        # like Radio Panama could sit on a single stale — and possibly wrong —
        # guess for hours. Clearing it lets the UI fall back to the honest
        # "Now playing live".
        #
        # It still goes to history: it did play, exactly as when a new track
        # displaces it.
        state["history"].appendleft({**state["current"]})
        state["current"] = {}

    return {
        "current": state["current"],
        "history": list(state["history"]),
        "stream_info": {
            "format": read["format"],
            "bitrate": read["bitrate"],
            "sample_rate": read["sample_rate"],
            "listeners": listeners,
        },
    }

@app.get("/api/track-history")
async def track_history(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(PlayedTrack).order_by(PlayedTrack.started_at.desc()).limit(50)
    )
    return result.scalars().all()


def _as_utc(value: datetime | None) -> str | None:
    """Serialise a stored started_at as an explicit UTC ISO timestamp.

    The column is naive TIMESTAMP holding UTC by convention. Serialising it
    bare gives "2026-08-21T14:03:00", which JavaScript's Date() parses as
    *local* time — the same naive/aware trap that shipped a bug against
    Postgres, pointed the other way. Attaching the offset costs nothing and
    removes the ambiguity at the boundary.
    """
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


@app.get("/api/track-stats")
async def track_stats(
    artist: str = Query(...),
    title: str = Query(...),
    session: AsyncSession = Depends(get_session),
):
    """Play history for one exact track — powers the "heard this before" badge.

    Served by ix_played_tracks_artist_title; before that index existed this was
    a full table scan on every track change.
    """
    match_track = (PlayedTrack.artist == artist, PlayedTrack.title == title)

    plays, first_at, last_at = (await session.execute(
        select(func.count(PlayedTrack.id),
               func.min(PlayedTrack.started_at),
               func.max(PlayedTrack.started_at)).where(*match_track)
    )).one()

    if not plays:
        return {"plays": 0, "first_heard": None, "last_heard": None,
                "first_station": None, "stations": []}

    stations = [
        {"station": st, "plays": c}
        for st, c in (await session.execute(
            select(PlayedTrack.station, func.count(PlayedTrack.id))
            .where(*match_track, PlayedTrack.station.is_not(None))
            .group_by(PlayedTrack.station)
            .order_by(func.count(PlayedTrack.id).desc())
        ))
    ]

    first_station = (await session.execute(
        select(PlayedTrack.station).where(*match_track)
        .order_by(PlayedTrack.started_at.asc()).limit(1)
    )).scalar_one_or_none()

    return {
        "plays": plays,
        "first_heard": _as_utc(first_at),
        "last_heard": _as_utc(last_at),
        "first_station": first_station,
        "stations": stations,
    }


@app.get("/api/similar-stations")
async def similar_stations(
    station: str = Query(...),
    limit: int = Query(4, ge=1, le=10),
    session: AsyncSession = Depends(get_session),
):
    """Stations whose played artists overlap this one's.

    Scored by Jaccard similarity on the distinct artist sets: |A n B| / |A u B|.
    Ranking by raw shared-artist count instead would just surface whichever
    station has the longest history — Pure Ibiza Radio alone holds ~60% of all
    rows and would top every list regardless of genre. Jaccard normalises by
    how much each station has been heard, so a small station that overlaps
    heavily still scores.

    The set maths runs in Python over a single query of distinct
    (station, artist) pairs rather than as a SQL self-join. At this size
    (~1.5k rows, 23 stations) that's a few thousand tuples, and it is far
    easier to follow than the equivalent CTE. If the table reaches six figures,
    push the intersection into SQL.
    """
    if station not in _STATION_URLS:
        raise HTTPException(status_code=404, detail=f"Unknown station: {station}")

    rows = await session.execute(
        select(PlayedTrack.station, PlayedTrack.artist)
        .where(PlayedTrack.station.is_not(None), PlayedTrack.artist != "")
        .distinct()
    )

    by_station: dict[str, set[str]] = {}
    display: dict[str, str] = {}
    for st, artist in rows:
        # Case-fold for matching so "ANYMA" and "Anyma" count as one artist —
        # broadcasters are wildly inconsistent about capitalisation — but keep
        # one display spelling for the UI.
        key = artist.casefold()
        by_station.setdefault(st, set()).add(key)
        display.setdefault(key, artist)

    mine = by_station.get(station, set())
    if not mine:
        return {"station": station, "similar": []}

    scored = []
    for other, theirs in by_station.items():
        # Only recommend stations still in the registry — the table also holds
        # rows for stations since removed from stations.json, and offering one
        # the picker can't select would be a dead end.
        if other == station or other not in _STATION_URLS or not theirs:
            continue
        shared = mine & theirs
        if not shared:
            continue
        scored.append({
            "station": other,
            "score": round(len(shared) / len(mine | theirs), 4),
            "shared_artists": len(shared),
            "examples": sorted(display[k] for k in shared)[:3],
        })

    scored.sort(key=lambda r: (r["score"], r["shared_artists"]), reverse=True)
    return {"station": station, "similar": scored[:limit]}


@app.post("/api/identify")
async def identify(station: str = Query(...)):
    """Force a fresh Shazam recognition of a station's live audio — what the
    "Name this track" button calls.

    Deliberately bypasses _shazam_cache. The automatic path caches *negative*
    results for SHAZAM_CACHE_TTL, and the user presses this button precisely
    because that came back empty; replaying the cached miss would make the
    button look broken.

    On success the result is written *into* that cache, so the next
    /api/now-playing poll picks it up through the normal path and persists it
    as a PlayedTrack. That keeps persistence in one place rather than
    duplicating it here.

    Takes a station name, not a URL — same SSRF reasoning as /api/now-playing.
    """
    url = _STATION_URLS.get(station)
    if url is None:
        raise HTTPException(status_code=404, detail=f"Unknown station: {station}")

    if station in _identify_in_flight:
        raise HTTPException(status_code=429, detail="Already listening to this station")

    _identify_in_flight.add(station)
    try:
        result = await shazam_fallback.recognize_stream(url)
    finally:
        # discard, not remove: the guard must come off even if recognition
        # raised, or the station would be permanently unidentifiable.
        _identify_in_flight.discard(station)

    if not result or not result.get("title"):
        # A no-match is a normal outcome (speech radio, a DJ talkover, a track
        # Shazam doesn't hold), not an error — 200 with an explicit null so the
        # frontend can say "no match" rather than "request failed".
        return {"match": None}

    _shazam_cache[station] = (time.monotonic(), result)
    return {"match": result}

class SpotifySaveIn(BaseModel):
    artist: str
    title: str

@app.post("/api/spotify/save")
async def spotify_save(payload: SpotifySaveIn):
    """Search Spotify for the given artist/title and save it to Liked Songs.
    The frontend calls this with whatever's currently shown in the now-playing
    panel — see spotify.py for the actual search+save logic.

    Single-user by design: it writes to whichever account the configured
    SPOTIFY_REFRESH_TOKEN belongs to, and has no auth of its own. That's fine
    on localhost or a private network, but it means anyone who can reach a
    publicly-exposed instance can add tracks to the owner's library — so don't
    expose this app to the internet without putting authentication in front
    of it. Flagged in the README's Security notes too.
    """
    try:
        result = await spotify.save_current_track(payload.artist, payload.title)
    except spotify.SpotifyNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except spotify.SpotifyNoMatch as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Spotify API error: {e.response.status_code}")
    return {"ok": True, **result}

# ── Static files (last — catches everything else) ─────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")
