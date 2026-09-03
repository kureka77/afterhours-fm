"""Shazam-based audio recognition — the fallback for stations whose ICY
metadata is empty, static, or otherwise useless (e.g. Ibiza Pura, Deep
Vibes, Sunshine Live, Pure Ibiza Radio — see streams.md), and the *only*
track source at all for the HLS stations, which have no ICY concept.

Uses vibra (github.com/BayernMuller/vibra), a C++ reimplementation of
Shazam's client-side audio fingerprinting that queries the same unofficial
endpoint Shazam's own apps use — chosen over the shazamio Python library
after shazamio's endpoint returned no matches in testing (a different
client fingerprint/request signature; vibra's confirmed working against a
real captured clip before this module was written).

This is NOT the default now-playing source — main.py only calls
recognize_stream() when the cheaper sources come back empty. A recognition
attempt costs a real ~10s audio capture plus a network round trip, unlike a
near-instant header read, so main.py also rate-limits how often it retries
per station (see SHAZAM_CACHE_TTL there).

Two capture strategies, picked by is_hls():

  - ICY/Icecast streams: the URL *is* the audio, so read bytes until we
    have enough.
  - HLS (.m3u8): the URL is a text playlist that has to be resolved to
    segment URLs, which are then fetched as ordinary files and
    concatenated. This is what gives m2o, m2o Dance and Dub Ninja a track
    at all — before it, isHls() short-circuited them to a static "Now
    playing live" placeholder.

Both feed the same fingerprinter. vibra shells out to ffmpeg internally,
which probes the container by content, so TS/AAC segments need no special
handling beyond being concatenated in order.
"""
import asyncio
import os
import tempfile
from urllib.parse import urljoin

import httpx
from vibra import Shazam, Vibra

CLIP_SECONDS = 10.0   # long enough for vibra to build a confident signature
CAPTURE_TIMEOUT = CLIP_SECONDS + 8.0

# Safety rails on the HLS path. A live media playlist holds a handful of
# segments; needing more than this means we misparsed it, and a capped
# download is better than an unbounded one on a request path.
MAX_SEGMENTS = 8
MAX_CLIP_BYTES = 16 * 1024 * 1024


def is_hls(url: str) -> bool:
    """Whether this stream is HLS rather than a raw ICY/Icecast stream.

    Same .m3u8 test the frontend's isHls() uses. Public because main.py
    needs it too: an ICY read against a playlist URL would parse .m3u8 text
    as audio and always come back empty.
    """
    return ".m3u8" in url.lower()


def _parse_playlist(text: str) -> tuple[list[str], list[tuple[str, float]]]:
    """Split an M3U8 into (variant URIs, [(segment URI, duration)]).

    A *master* playlist lists other playlists, each preceded by
    #EXT-X-STREAM-INF. A *media* playlist lists the actual audio segments,
    each preceded by #EXTINF:<seconds>,. Both use the same file extension,
    so which one we were handed is decided by the tags, not the URL — hence
    returning both and letting the caller branch.
    """
    variants: list[str] = []
    segments: list[tuple[str, float]] = []
    pending: float | None = None
    next_is_variant = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            # The URI is on the *following* line, not this one.
            next_is_variant = True
        elif line.startswith("#EXTINF"):
            # "#EXTINF:10.0,optional-title" — the duration is how we know how
            # many segments add up to CLIP_SECONDS without downloading first.
            try:
                pending = float(line.split(":", 1)[1].split(",")[0])
            except (IndexError, ValueError):
                pending = None
        elif line.startswith("#"):
            continue  # any other tag: not a URI, ignore
        elif next_is_variant:
            variants.append(line)
            next_is_variant = False
        else:
            # Fall back to 10s rather than skipping a segment with an
            # unparseable #EXTINF — a slightly wrong length just means we
            # capture a bit more or less audio, which vibra tolerates.
            segments.append((line, pending if pending is not None else 10.0))
            pending = None

    return variants, segments


async def _capture_hls_clip(
    url: str, seconds: float, client: httpx.AsyncClient
) -> bytes | None:
    """Concatenate enough segments off a live HLS playlist to fingerprint."""
    resp = await client.get(url)
    resp.raise_for_status()
    # Resolve relative URIs against the *final* URL, not the requested one:
    # these CDNs redirect, and a bare segment filename resolved against the
    # pre-redirect host 404s.
    base = str(resp.url)
    variants, segments = _parse_playlist(resp.text)

    if variants and not segments:
        # A master playlist — follow the first variant. These radio streams
        # publish a single audio rendition (m2o advertises exactly one), so
        # there's no bitrate ladder to choose from; and for fingerprinting
        # the lowest would do anyway.
        resp = await client.get(urljoin(base, variants[0]))
        resp.raise_for_status()
        base = str(resp.url)
        _, segments = _parse_playlist(resp.text)

    if not segments:
        return None

    # Take segments from the END of the window. A live media playlist lists
    # oldest first and only ever contains *complete* segments, so the last
    # one is both finished and closest to live. Starting from the front would
    # fingerprint audio up to a full window (~30s) old, which right after a
    # track change would confidently name the previous song.
    chosen: list[str] = []
    total = 0.0
    for uri, duration in reversed(segments):
        chosen.append(uri)
        total += duration
        if total >= seconds or len(chosen) >= MAX_SEGMENTS:
            break
    chosen.reverse()  # back to chronological order — the bytes get concatenated

    buf = bytearray()
    for uri in chosen:
        try:
            seg = await client.get(urljoin(base, uri))
            seg.raise_for_status()
        except Exception:
            # Keep a partial capture rather than losing the whole attempt.
            # These CDNs drop connections intermittently — m2o's returned
            # "Server disconnected without sending a response" twice during
            # testing — and a single segment already fingerprints reliably
            # (verified against all three HLS stations). Failing hard here
            # would blank the track for a whole SHAZAM_CACHE_TTL window over
            # a blip. If the *first* segment fails we still return None.
            break
        buf += seg.content
        if len(buf) >= MAX_CLIP_BYTES:
            break
    return bytes(buf) if buf else None


async def _capture_raw_clip(
    url: str, seconds: float, client: httpx.AsyncClient
) -> bytes | None:
    """Grab `seconds` worth of raw audio bytes from a live ICY stream. No ICY
    metadata handling here — vibra doesn't need it, it fingerprints the audio
    itself, so this is a plain byte capture, simpler than
    _read_icy_now_playing's metaint bookkeeping in main.py."""
    async with client.stream(
        "GET", url, headers={"User-Agent": "afterhours-fm/1.0"}
    ) as resp:
        buf = bytearray()
        loop = asyncio.get_event_loop()
        deadline = loop.time() + seconds
        async for chunk in resp.aiter_bytes(chunk_size=8192):
            buf += chunk
            if loop.time() >= deadline:
                break
        return bytes(buf) if buf else None


async def _capture_clip(url: str, seconds: float = CLIP_SECONDS) -> bytes | None:
    """Capture audio from a live stream by whichever route it supports.
    Returns None on any failure — this is a best-effort fallback."""
    try:
        async with httpx.AsyncClient(
            timeout=CAPTURE_TIMEOUT, follow_redirects=True
        ) as client:
            if is_hls(url):
                return await _capture_hls_clip(url, seconds, client)
            return await _capture_raw_clip(url, seconds, client)
    except Exception:
        return None


def _recognize_sync(clip: bytes, suffix: str = ".mp3") -> dict | None:
    """Sync: write the clip to a temp file (vibra's file-based API detects
    format and shells out to ffmpeg internally for non-WAV input), generate
    a signature, query Shazam. Runs in a thread — see recognize_stream().

    `suffix` is only a hint — ffmpeg probes the container by content, so a
    mislabelled file still works — but naming HLS captures .ts rather than
    .mp3 keeps the temp file honest about what's in it.
    """
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(clip)
        path = f.name
    try:
        signature = Vibra().get_signature(path)
        result = Shazam.recognize(signature)
    finally:
        os.unlink(path)

    track = result.get("track")
    if not track:
        return None
    images = track.get("images", {})
    return {
        "artist": track.get("subtitle", "") or "",
        "title": track.get("title", "") or "",
        "cover_url": images.get("coverarthq") or images.get("coverart"),
        "shazam_url": (track.get("share") or {}).get("href"),
    }


async def recognize_stream(url: str) -> dict | None:
    """Capture a clip from the live stream and identify it via Shazam
    (through vibra). Returns None on no match or any failure — this is a
    best-effort fallback and must never raise into the request path."""
    clip = await _capture_clip(url)
    if not clip:
        return None
    try:
        return await asyncio.to_thread(
            _recognize_sync, clip, ".ts" if is_hls(url) else ".mp3"
        )
    except Exception:
        return None
