"""Tests for shazam_fallback.py — the Shazam-via-vibra recognition fallback,
used only when a station's ICY metadata gives nothing (see main.py's
_shazam_fallback_cached)."""
from unittest.mock import MagicMock, patch

import shazam_fallback as sf


# ── _capture_clip ────────────────────────────────────────────────────────────

class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def aiter_bytes(self, chunk_size=8192):
        for c in self._chunks:
            yield c


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp
    async def __aenter__(self):
        return self._resp
    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp
    def stream(self, method, url, headers=None):
        return _FakeStreamCtx(self._resp)
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


async def test_capture_clip_collects_bytes(monkeypatch):
    chunks = [b"a" * 100, b"b" * 100, b"c" * 100]
    monkeypatch.setattr(sf.httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeStreamResponse(chunks)))
    result = await sf._capture_clip("http://x", seconds=100)  # long budget — collect everything
    assert result == b"a" * 100 + b"b" * 100 + b"c" * 100


async def test_capture_clip_stops_at_time_budget(monkeypatch):
    """seconds=0 means the very first chunk already exceeds the deadline —
    should return just that first chunk, not hang around for more."""
    chunks = [b"a" * 100, b"b" * 100, b"c" * 100]
    monkeypatch.setattr(sf.httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeStreamResponse(chunks)))
    result = await sf._capture_clip("http://x", seconds=0)
    assert result == b"a" * 100


async def test_capture_clip_connection_error_returns_none(monkeypatch):
    class _BoomClient:
        def stream(self, *a, **kw):
            raise RuntimeError("connection refused")
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(sf.httpx, "AsyncClient", lambda **kw: _BoomClient())
    result = await sf._capture_clip("http://x")
    assert result is None


async def test_capture_clip_empty_stream_returns_none(monkeypatch):
    monkeypatch.setattr(sf.httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeStreamResponse([])))
    result = await sf._capture_clip("http://x")
    assert result is None


# ── _recognize_sync ───────────────────────────────────────────────────────────

def test_recognize_sync_parses_match():
    fake_signature = object()
    fake_vibra_instance = MagicMock()
    fake_vibra_instance.get_signature.return_value = fake_signature

    fake_result = {
        "track": {
            "title": "Café Del Mar",
            "subtitle": "Energy 52",
            "images": {"coverarthq": "https://example.com/hq.jpg", "coverart": "https://example.com/lq.jpg"},
            "share": {"href": "https://www.shazam.com/track/123/cafe-del-mar"},
        }
    }

    with patch("shazam_fallback.Vibra", return_value=fake_vibra_instance), \
         patch("shazam_fallback.Shazam") as mock_shazam:
        mock_shazam.recognize.return_value = fake_result
        result = sf._recognize_sync(b"fake audio bytes")

    assert result == {
        "artist": "Energy 52",
        "title": "Café Del Mar",
        "cover_url": "https://example.com/hq.jpg",
        "shazam_url": "https://www.shazam.com/track/123/cafe-del-mar",
    }


def test_recognize_sync_falls_back_to_lq_cover():
    fake_vibra_instance = MagicMock()
    fake_result = {"track": {"title": "T", "subtitle": "A", "images": {"coverart": "https://example.com/lq.jpg"}}}

    with patch("shazam_fallback.Vibra", return_value=fake_vibra_instance), \
         patch("shazam_fallback.Shazam") as mock_shazam:
        mock_shazam.recognize.return_value = fake_result
        result = sf._recognize_sync(b"audio")

    assert result["cover_url"] == "https://example.com/lq.jpg"


def test_recognize_sync_no_match_returns_none():
    fake_vibra_instance = MagicMock()
    with patch("shazam_fallback.Vibra", return_value=fake_vibra_instance), \
         patch("shazam_fallback.Shazam") as mock_shazam:
        mock_shazam.recognize.return_value = {"matches": []}  # no "track" key — genuine no-match shape
        result = sf._recognize_sync(b"audio")
    assert result is None


# ── recognize_stream (integration of capture + recognize) ───────────────────

async def test_recognize_stream_no_clip_returns_none(monkeypatch):
    async def fake_capture(url, seconds=sf.CLIP_SECONDS):
        return None
    monkeypatch.setattr(sf, "_capture_clip", fake_capture)
    result = await sf.recognize_stream("http://x")
    assert result is None


async def test_recognize_stream_happy_path(monkeypatch):
    async def fake_capture(url, seconds=sf.CLIP_SECONDS):
        return b"fake clip bytes"
    monkeypatch.setattr(sf, "_capture_clip", fake_capture)
    monkeypatch.setattr(sf, "_recognize_sync", lambda clip: {"artist": "A", "title": "B", "cover_url": None, "shazam_url": None})

    result = await sf.recognize_stream("http://x")
    assert result == {"artist": "A", "title": "B", "cover_url": None, "shazam_url": None}


async def test_recognize_stream_swallows_recognition_errors(monkeypatch):
    """A best-effort fallback must never raise into the request path — a
    vibra/Shazam-side exception (network error, decode failure, etc.) should
    come back as None, same as a clean no-match."""
    async def fake_capture(url, seconds=sf.CLIP_SECONDS):
        return b"fake clip bytes"
    def fake_recognize_sync(clip):
        raise RuntimeError("ffmpeg exploded")
    monkeypatch.setattr(sf, "_capture_clip", fake_capture)
    monkeypatch.setattr(sf, "_recognize_sync", fake_recognize_sync)

    result = await sf.recognize_stream("http://x")
    assert result is None
