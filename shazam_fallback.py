"""Shazam-based audio recognition — a fallback for stations whose ICY
metadata is empty, static, or otherwise useless (e.g. Ibiza Pura, Deep
Vibes, Sunshine Live, Pure Ibiza Radio — see streams.md).

Uses vibra (github.com/BayernMuller/vibra), a C++ reimplementation of
Shazam's client-side audio fingerprinting that queries the same unofficial
endpoint Shazam's own apps use — chosen over the shazamio Python library
after shazamio's endpoint returned no matches in testing (a different
client fingerprint/request signature; vibra's confirmed working against a
real captured clip before this module was written).

This is NOT the default now-playing source — main.py only calls
recognize_stream() when the cheap, free ICY read comes back empty. A
recognition attempt costs a real ~10s audio capture plus a network round
trip, unlike a near-instant header read, so main.py also rate-limits how
often it retries per station (see SHAZAM_CACHE_TTL there).
"""
import asyncio
import os
import tempfile

import httpx
from vibra import Shazam, Vibra

CLIP_SECONDS = 10.0   # long enough for vibra to build a confident signature
CAPTURE_TIMEOUT = CLIP_SECONDS + 8.0


async def _capture_clip(url: str, seconds: float = CLIP_SECONDS) -> bytes | None:
    """Grab `seconds` worth of raw audio bytes from a live stream. No ICY
    metadata handling here — vibra doesn't need it, it fingerprints the
    audio itself, so this is a plain byte capture, simpler than
    _read_icy_now_playing's metaint bookkeeping in main.py."""
    try:
        async with httpx.AsyncClient(timeout=CAPTURE_TIMEOUT, follow_redirects=True) as client:
            async with client.stream("GET", url, headers={"User-Agent": "afterhours-fm/1.0"}) as resp:
                buf = bytearray()
                loop = asyncio.get_event_loop()
                deadline = loop.time() + seconds
                async for chunk in resp.aiter_bytes(chunk_size=8192):
                    buf += chunk
                    if loop.time() >= deadline:
                        break
                return bytes(buf) if buf else None
    except Exception:
        return None


def _recognize_sync(clip: bytes) -> dict | None:
    """Sync: write the clip to a temp file (vibra's file-based API detects
    format and shells out to ffmpeg internally for non-WAV input), generate
    a signature, query Shazam. Runs in a thread — see recognize_stream()."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
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
        return await asyncio.to_thread(_recognize_sync, clip)
    except Exception:
        return None
