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
    monkeypatch.setattr(sf, "_recognize_sync", lambda clip, suffix=".mp3": {"artist": "A", "title": "B", "cover_url": None, "shazam_url": None})

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


# ── HLS capture ──────────────────────────────────────────────────────────────
# The .m3u8 stations (m2o, m2o Dance, Dub Ninja) have no ICY metadata of any
# kind, so Shazam is their *only* track source — before this path existed the
# frontend short-circuited them to a static "Now playing live" placeholder.
# A playlist URL serves text, not audio, so it needs resolving to segments
# rather than reading bytes off the socket.

MASTER = "https://cdn.example/radio/master.m3u8"
MEDIA = "https://cdn.example/radio/play1.m3u8"


class _FakeResp:
    def __init__(self, text="", content=b"", url="", status=200):
        self.text, self.content, self.url, self.status_code = text, content, url, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeGetClient:
    """Stand-in for httpx.AsyncClient's .get(). `routes` maps URL -> response;
    `fail` names URLs that raise, which is how the CDN-drops-mid-capture case
    is exercised."""
    def __init__(self, routes, fail=frozenset()):
        self.routes, self.fail, self.requested = routes, set(fail), []

    async def get(self, url):
        self.requested.append(url)
        if url in self.fail:
            raise RuntimeError("Server disconnected without sending a response")
        if url not in self.routes:
            raise AssertionError(f"unexpected URL requested: {url}")
        return self.routes[url]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _media_playlist(*names, duration=10):
    body = "#EXTM3U\n#EXT-X-TARGETDURATION:11\n#EXT-X-MEDIA-SEQUENCE:1\n"
    for n in names:
        body += f"#EXT-X-PROGRAM-DATE-TIME:2026-09-03T16:42:14.110Z\n#EXTINF:{duration},\n{n}\n"
    return body


def _install(monkeypatch, client):
    monkeypatch.setattr(sf.httpx, "AsyncClient", lambda **kw: client)
    return client


def test_is_hls():
    assert sf.is_hls("https://cdn/x/master.m3u8")
    assert sf.is_hls("https://dub.ninja/stream/live.m3u8?sid=abc")  # query string
    assert sf.is_hls("HTTPS://CDN/MASTER.M3U8")                     # case-insensitive
    assert not sf.is_hls("https://control.streaming-pro.com:8028/stream.mp3")


# ── _parse_playlist ──────────────────────────────────────────────────────────

def test_parse_playlist_master_returns_variants_not_segments():
    """Master and media playlists share the .m3u8 extension — only the tags
    distinguish them, so the parser must report which it got."""
    text = ('#EXTM3U\n#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=122225,'
            'CODECS="mp4a.40.5"\nplay1.m3u8\n')
    variants, segments = sf._parse_playlist(text)
    assert variants == ["play1.m3u8"]
    assert segments == []


def test_parse_playlist_media_returns_segments_with_durations():
    variants, segments = sf._parse_playlist(_media_playlist("a.ts", "b.ts"))
    assert variants == []
    assert segments == [("a.ts", 10.0), ("b.ts", 10.0)]


def test_parse_playlist_unparseable_extinf_defaults_to_ten_seconds():
    """A bad duration must not drop the segment — a slightly wrong length just
    means marginally more or less audio, which vibra tolerates; a missing
    segment could mean no capture at all."""
    _, segments = sf._parse_playlist("#EXTM3U\n#EXTINF:notanumber,\nx.ts\n")
    assert segments == [("x.ts", 10.0)]


def test_parse_playlist_ignores_unknown_tags():
    text = ("#EXTM3U\n#EXT-X-DISCONTINUITY-SEQUENCE:0\n#EXT-X-VERSION:3\n"
            "#EXTINF:10,\nseg.ts\n")
    variants, segments = sf._parse_playlist(text)
    assert variants == [] and segments == [("seg.ts", 10.0)]


# ── _capture_hls_clip, through the public _capture_clip dispatch ─────────────

async def test_capture_follows_master_to_media_and_concatenates(monkeypatch):
    client = _install(monkeypatch, _FakeGetClient({
        MASTER: _FakeResp(text='#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nplay1.m3u8\n', url=MASTER),
        MEDIA: _FakeResp(text=_media_playlist("a.ts", "b.ts"), url=MEDIA),
        "https://cdn.example/radio/a.ts": _FakeResp(content=b"AAA", url=""),
        "https://cdn.example/radio/b.ts": _FakeResp(content=b"BBB", url=""),
    }))
    result = await sf._capture_clip(MASTER, seconds=25)
    assert result == b"AAABBB"          # chronological order, not reversed
    assert MEDIA in client.requested    # the master was actually followed


async def test_capture_takes_newest_segments_not_oldest(monkeypatch):
    """A live media playlist lists oldest first and holds only complete
    segments, so the last one is closest to live. Starting from the front
    would fingerprint audio a whole window old and, right after a track
    change, confidently name the previous song."""
    _install(monkeypatch, _FakeGetClient({
        MEDIA: _FakeResp(text=_media_playlist("s1.ts", "s2.ts", "s3.ts", "s4.ts"), url=MEDIA),
        "https://cdn.example/radio/s3.ts": _FakeResp(content=b"THREE", url=""),
        "https://cdn.example/radio/s4.ts": _FakeResp(content=b"FOUR", url=""),
    }))
    # 15s budget over 10s segments -> the last two, oldest-first once chosen.
    assert await sf._capture_clip(MEDIA, seconds=15) == b"THREEFOUR"


async def test_capture_resolves_relative_uris_against_redirected_url(monkeypatch):
    """Several of these CDNs redirect. Segment names are relative, so resolving
    them against the *requested* URL rather than the final one 404s."""
    requested = "https://cdn.example/radio/master.m3u8"
    final = "https://edge-eu.example/geo/radio/play1.m3u8"
    client = _install(monkeypatch, _FakeGetClient({
        requested: _FakeResp(text=_media_playlist("seg9.ts"), url=final),
        "https://edge-eu.example/geo/radio/seg9.ts": _FakeResp(content=b"OK", url=""),
    }))
    assert await sf._capture_clip(requested, seconds=5) == b"OK"
    assert "https://edge-eu.example/geo/radio/seg9.ts" in client.requested


async def test_capture_keeps_partial_clip_when_a_segment_fails(monkeypatch):
    """Observed live: m2o's CDN returns "Server disconnected without sending a
    response" intermittently. One segment already fingerprints reliably, so a
    blip must not blank the track for a whole SHAZAM_CACHE_TTL window."""
    _install(monkeypatch, _FakeGetClient(
        routes={
            MEDIA: _FakeResp(text=_media_playlist("s1.ts", "s2.ts", "s3.ts"), url=MEDIA),
            "https://cdn.example/radio/s1.ts": _FakeResp(content=b"FIRST", url=""),
        },
        fail={"https://cdn.example/radio/s2.ts"},
    ))
    assert await sf._capture_clip(MEDIA, seconds=25) == b"FIRST"


async def test_capture_returns_none_when_first_segment_fails(monkeypatch):
    """Partial is fine; empty is not — None lets the caller report no track
    rather than handing vibra zero bytes."""
    _install(monkeypatch, _FakeGetClient(
        routes={MEDIA: _FakeResp(text=_media_playlist("s1.ts"), url=MEDIA)},
        fail={"https://cdn.example/radio/s1.ts"},
    ))
    assert await sf._capture_clip(MEDIA, seconds=25) is None


async def test_capture_returns_none_for_empty_playlist(monkeypatch):
    _install(monkeypatch, _FakeGetClient({
        MEDIA: _FakeResp(text="#EXTM3U\n#EXT-X-ENDLIST\n", url=MEDIA),
    }))
    assert await sf._capture_clip(MEDIA, seconds=25) is None


async def test_capture_caps_segment_count(monkeypatch):
    """A misparsed playlist must not turn into an unbounded download on a
    request path — MAX_SEGMENTS is the backstop."""
    names = [f"s{i}.ts" for i in range(50)]
    routes = {MEDIA: _FakeResp(text=_media_playlist(*names, duration=1), url=MEDIA)}
    for n in names:
        routes[f"https://cdn.example/radio/{n}"] = _FakeResp(content=b"x", url="")
    client = _install(monkeypatch, _FakeGetClient(routes))
    result = await sf._capture_clip(MEDIA, seconds=9999)  # budget it can never fill
    assert len(result) == sf.MAX_SEGMENTS
    assert len(client.requested) == sf.MAX_SEGMENTS + 1  # + the playlist itself


async def test_capture_playlist_http_error_returns_none(monkeypatch):
    _install(monkeypatch, _FakeGetClient({MEDIA: _FakeResp(text="", url=MEDIA, status=503)}))
    assert await sf._capture_clip(MEDIA, seconds=25) is None


# ── suffix routing ───────────────────────────────────────────────────────────

async def test_recognize_stream_labels_hls_captures_ts(monkeypatch):
    """ffmpeg probes by content so a wrong suffix still works, but the temp
    file should be honest about what's in it."""
    seen = {}

    async def fake_capture(url, seconds=sf.CLIP_SECONDS):
        return b"clip"

    monkeypatch.setattr(sf, "_capture_clip", fake_capture)
    monkeypatch.setattr(sf, "_recognize_sync",
                        lambda clip, suffix=".mp3": seen.setdefault("suffix", suffix) and None)

    await sf.recognize_stream("https://cdn/x/master.m3u8")
    assert seen["suffix"] == ".ts"

    seen.clear()
    await sf.recognize_stream("http://icecast.example/stream.mp3")
    assert seen["suffix"] == ".mp3"
