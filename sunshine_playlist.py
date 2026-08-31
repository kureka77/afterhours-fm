"""Track metadata from sunshine-live.de's own published playlist.

Why this module exists: the Sunshine Live streams send ICY metadata whose
StreamTitle is permanently the station's own name ("SUNSHINE LIVE - Techno"),
never a track — see streams.md. That leaves two ways to know what's playing,
and this is the better one:

  - Shazam (shazam_fallback.py): ~10s of audio + a fingerprint round trip,
    and only ever a guess.
  - This: one JSON GET against the same API the station's own playlist page
    (https://www.sunshine-live.de/programm/playlist) calls. It's the
    broadcaster's own data, it carries real cover art, and it costs a header
    read's worth of time rather than a ten-second capture.

So main.py tries this first and keeps Shazam as the last resort.

The endpoint was found by loading that page and watching its network calls;
it is undocumented, so everything below is what the API was observed to do,
not what it promises to do. It can change without notice — every failure path
here returns None so the caller just falls through to Shazam.

Channel ids come from the `<select>` on the playlist page (value=id). Only the
channels this app actually streams are listed in CHANNELS.

Deliberately NOT mapped: "Sunshine Live" (the main simulcast, channel id 3).
That channel's feed is a *show* schedule, not a track list — one entry per
hour with a ~8 second duration, carrying the programme and its host
(artist="Clapcast", title="Claptone"). Mapping it would have published show
names as though they were songs, which is the same class of bug this module
was written to fix. The main stream falls through to Shazam instead, which is
the only thing that can name an individual track inside a DJ mix.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

API_URL = "https://iris-sunshinelive.loverad.io/search.json"

# The API reports airtimes in Europe/Berlin and — confirmed by querying the
# same window with different offsets — *ignores the UTC offset it is sent*,
# matching only the naive wall-clock digits. Sending a correct "+00:00" UTC
# timestamp therefore silently returns tracks from two hours ago rather than
# an error, which is exactly the kind of wrong-but-plausible result that would
# have shipped unnoticed. Always build the window in Berlin local time.
STATION_TZ = ZoneInfo("Europe/Berlin")

# afterhours-fm station name -> sunshine-live channel id.
# Verified per channel by Shazam-ing the live stream and confirming the
# recognised track matched what this API reported for the same moment, rather
# than trusting the name similarity between "techno/mp3-192" and "TECHNO".
CHANNELS: dict[str, int] = {
    "Sunshine Live Techno": 9,
    "Sunshine Live House": 4,
}

# How far back to ask for. Only the newest entry is used; this just has to be
# comfortably longer than one track so the currently-playing one's *start* is
# inside the window (extended club mixes run well past the ~9 min observed).
# A wider window costs nothing — it's the same single request either way.
LOOKBACK = timedelta(minutes=30)
# Small forward slack: observed airtimes occasionally sit a few seconds ahead
# of the wall clock.
LOOKAHEAD = timedelta(minutes=2)
# Grace on top of a track's own duration before we stop calling it "current".
# Without this, a station that pauses publishing would leave the last track
# pinned as now-playing indefinitely. With it, we return None and the caller
# falls through to Shazam — a fallback is better than a stale answer.
STALE_GRACE = timedelta(minutes=2)


def _pick_cover(song: dict) -> str | None:
    """Largest available cover art. The API returns all size variants as keys
    that are present-but-empty when unset, so this can't just use .get()."""
    for key in ("cover_art_url_custom", "cover_art_url_xl",
                "cover_art_url_l", "cover_art_url_m"):
        url = (song.get(key) or "").strip()
        if url:
            return url
    return None


def _parse_entries(payload: dict) -> list[tuple[datetime, dict]]:
    """Flatten the API's deeply nested result into (airtime, song) pairs.

    The shape is result.entry[].song.entry[].artist.entry[] — three levels of
    the same {"found": "N", "entry": [...]} wrapper. Any level can be missing
    or empty, so every hop is defensive.
    """
    entries = []
    for item in (payload.get("result") or {}).get("entry") or []:
        songs = (item.get("song") or {}).get("entry") or []
        if not songs:
            continue
        song = songs[0]
        try:
            airtime = datetime.fromisoformat(item["airtime"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            duration = timedelta(seconds=int(item.get("duration") or 0))
        except (TypeError, ValueError):
            duration = timedelta(0)
        song = {**song, "_duration": duration}
        entries.append((airtime, song))
    return entries


async def fetch_now_playing(channel_id: int, timeout: float = 8.0) -> dict | None:
    """The track currently airing on one sunshine-live channel.

    Returns None for "nothing current" as well as for any failure — a missing
    answer here is normal (between tracks, or during a show the playlist
    doesn't itemise), and the caller treats both the same way: fall through to
    Shazam. Never raises into the request path.
    """
    now = datetime.now(timezone.utc).astimezone(STATION_TZ)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(API_URL, params={
                "station": channel_id,
                "start": (now - LOOKBACK).isoformat(),
                "end": (now + LOOKAHEAD).isoformat(),
            })
            resp.raise_for_status()
            payload = resp.json()
    except Exception:
        return None

    entries = _parse_entries(payload)
    if not entries:
        return None

    # Sorting rather than trusting the observed newest-first order: the API is
    # undocumented, and it also returns near-duplicate rows for the same track
    # (the same song logged twice a second apart), so "the latest airtime" is
    # the only stable way to pick one.
    airtime, song = max(entries, key=lambda pair: pair[0])

    if airtime > now + LOOKAHEAD:
        return None
    if now > airtime + song["_duration"] + STALE_GRACE:
        return None

    artists = (song.get("artist") or {}).get("entry") or []
    artist = (artists[0].get("name") if artists else "") or ""
    title = (song.get("title") or "").strip()
    if not title:
        return None

    return {
        "artist": artist.strip(),
        "title": title,
        "cover_url": _pick_cover(song),
        "started_at": airtime,
    }


async def fetch_for_station(station: str) -> dict | None:
    """fetch_now_playing() keyed by afterhours-fm station name. Returns None
    for any station without a mapped channel, so the caller can call it
    unconditionally."""
    channel_id = CHANNELS.get(station)
    if channel_id is None:
        return None
    return await fetch_now_playing(channel_id)
