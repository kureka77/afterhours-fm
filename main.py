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
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import engine, Base, get_session, DATABASE_URL
import models  # noqa: F401 — registers ORM models
from models import PlayedTrack
import spotify
import shazam_fallback

# ── Config ────────────────────────────────────────────────────────────────────
META_CACHE_TTL = 15    # seconds — avoid re-hitting a station's origin on rapid re-polls
# Shazam fallback is expensive (a real ~10s audio capture + network round trip,
# vs a near-instant header read) — cache aggressively, including negative
# results, so a station stuck with no ICY metadata doesn't get hammered every
# poll. Deliberately >= the frontend's 20s poll interval so most polls hit cache.
SHAZAM_CACHE_TTL = 45  # seconds

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
    "Pure Ibiza Radio":      ("http://control.streaming-pro.com:8028/status-json.xsl", "stream.mp3"),
    "Ibiza Global Classics": ("https://control.streaming-pro.com:8000/status-json.xsl", "ibizaglobalclassics.mp3"),
    "Ibiza Global Radio":    ("https://control.streaming-pro.com:8024/status-json.xsl", "stream.aac"),
}

# ── In-memory state ──────────────────────────────────────────────────────────
# e.g. {"Pure Ibiza Radio": {"current": {...}, "history": deque([...])}}
_station_state: dict[str, dict] = {}
_meta_cache: dict[str, tuple[float, dict]] = {}          # stream url -> (fetched_at, result)
_icecast_cache: dict[str, tuple[float, dict | None]] = {}  # status url -> (fetched_at, parsed json)
_shazam_cache: dict[str, tuple[float, dict | None]] = {}   # station -> (fetched_at, result)
_cover_art_cache: dict[str, str | None] = {}               # "artist|title" -> cover_url (no TTL — a song's art doesn't change)


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
    if "{" in raw:
        # some broadcasters stuff a raw JSON blob in here instead of a plain title —
        # e.g. Pure Ibiza Radio sends StreamTitle='NOW ON AIR   {"autor":"...",...}'
        return result
    if " - " in raw:
        artist, title = raw.split(" - ", 1)
    else:
        artist, title = "", raw
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


# ── Shazam fallback (only when ICY gives nothing) ───────────────────────────────
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
async def _cover_art_cached(artist: str, title: str) -> str | None:
    """Best-effort cover art lookup via Spotify search — never raises; a
    missing cover just means the frontend shows no image, same as if this
    weren't called at all. Cached indefinitely per artist|title (a song's
    cover doesn't change), with a simple size cap so this can't grow forever
    over a long-running process."""
    key = f"{artist}|{title}"
    if key in _cover_art_cache:
        return _cover_art_cache[key]
    if len(_cover_art_cache) > 1000:
        _cover_art_cache.clear()
    try:
        match = await spotify.search_track(artist, title)
        cover = match.get("cover_url") if match else None
    except Exception:
        cover = None
    _cover_art_cache[key] = cover
    return cover


# ── Schema migration guard ──────────────────────────────────────────────────────
async def _ensure_station_column() -> None:
    """Add played_tracks.station if it's missing, and backfill old rows.

    No Alembic in this project (see README) — this is a hand-rolled, idempotent
    guard that runs on every startup. Safe to leave in place indefinitely; once
    the column exists it's a no-op read-then-skip. Old rows predate
    multi-station support and were always Pure Ibiza Radio, hence the backfill.
    """
    is_sqlite = DATABASE_URL.startswith("sqlite")
    async with engine.begin() as conn:
        if is_sqlite:
            result = await conn.exec_driver_sql("PRAGMA table_info(played_tracks)")
            existing = {row[1] for row in result}
        else:
            result = await conn.exec_driver_sql(
                "SELECT column_name FROM information_schema.columns WHERE table_name='played_tracks'"
            )
            existing = {row[0] for row in result}
        if "station" not in existing:
            await conn.exec_driver_sql("ALTER TABLE played_tracks ADD COLUMN station VARCHAR")
            await conn.exec_driver_sql(
                "UPDATE played_tracks SET station = 'Pure Ibiza Radio' WHERE station IS NULL"
            )


# ── App lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_station_column()
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

    state = _station_state.setdefault(station, {"current": {}, "history": deque(maxlen=10)})
    read      = await _read_icy_now_playing_cached(url)
    listeners = await _read_icecast_listeners_cached(station)

    artist, title, cover_url, source = read["artist"], read["title"], None, "icy"

    if not title:
        # ICY gave nothing usable — try Shazam (rate-limited, see SHAZAM_CACHE_TTL).
        # Deliberately only reached when the cheap path already failed; never
        # runs alongside a working ICY read.
        shazam = await _shazam_fallback_cached(station, url)
        if shazam and shazam.get("title"):
            artist, title, cover_url, source = shazam["artist"], shazam["title"], shazam.get("cover_url"), "shazam"

    if title:
        key     = f"{artist}|{title}"
        cur     = state["current"]
        cur_key = f"{cur.get('artist', '')}|{cur.get('title', '')}"
        if key != cur_key:
            # ICY-sourced tracks don't come with cover art — Shazam-sourced
            # ones already do (fetched above, straight from Shazam's own data).
            if not cover_url:
                cover_url = await _cover_art_cached(artist, title)

            started_at = datetime.now(timezone.utc)
            db_track = PlayedTrack(
                title=title,
                artist=artist,
                album=None,
                station=station,
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
