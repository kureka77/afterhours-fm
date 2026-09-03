"""Tests for _read_icy_now_playing() / _read_icy_now_playing_cached() —
the ICY in-band metadata reader that replaced the old AcoustID pipeline."""
import main

EMPTY = {"artist": "", "title": "", "format": None, "bitrate": None, "sample_rate": None}


# ── Fakes: minimal stand-ins for httpx's streaming API ──────────────────────

class _FakeStreamResponse:
    """Stands in for the object yielded by `async with client.stream(...) as resp`."""
    def __init__(self, headers: dict, chunks: list[bytes]):
        self.headers = headers
        self._chunks = chunks

    async def aiter_bytes(self, chunk_size=4096):
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


def _patch_client(monkeypatch, resp):
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kw: _FakeClient(resp))


def _icy_bytes(metaint: int, audio: bytes, meta_text: str | None) -> bytes:
    """Build a raw ICY byte stream: `metaint` bytes of audio, a length byte,
    then that many (x16, padded) bytes of metadata text — mirrors exactly
    what a real Shoutcast/Icecast encoder interleaves into the stream."""
    audio = audio.ljust(metaint, b"\x00")[:metaint]
    if meta_text is None:
        return audio + b"\x00"  # length byte 0 == "no title change since last block"
    payload = meta_text.encode()
    pad = (-len(payload)) % 16
    payload += b"\x00" * pad
    length_byte = bytes([len(payload) // 16])
    return audio + length_byte + payload


# ── _read_icy_now_playing: track title ──────────────────────────────────────

async def test_parses_artist_and_title(monkeypatch):
    raw = _icy_bytes(64, b"audio" * 10, "StreamTitle='Energy 52 - Café Del Mar';")
    _patch_client(monkeypatch, _FakeStreamResponse({"icy-metaint": "64"}, [raw]))
    result = await main._read_icy_now_playing("http://x")
    assert result["artist"] == "Energy 52"
    assert result["title"] == "Café Del Mar"


async def test_title_without_artist_separator_is_rejected(monkeypatch):
    """A StreamTitle with no " - " is a show or station ident, not a track.

    This used to be kept as artist="" / title=<whole string>, which is how 34
    junk rows reached the database — 31 of them Blue Marlin's "Djs Blue Marlin
    Sessions", the single most "played" row in the table. Returning nothing
    lets the fallbacks in now_playing() look for the actual song instead.
    """
    raw = _icy_bytes(64, b"audio" * 10, "StreamTitle='Djs Blue Marlin Sessions';")
    _patch_client(monkeypatch, _FakeStreamResponse({"icy-metaint": "64"}, [raw]))
    result = await main._read_icy_now_playing("http://x")
    assert result["artist"] == ""
    assert result["title"] == ""


async def test_title_with_separator_still_parses(monkeypatch):
    """The other side of that guard — a real track must be unaffected."""
    raw = _icy_bytes(64, b"audio" * 10, "StreamTitle='Anyma - Eternity';")
    _patch_client(monkeypatch, _FakeStreamResponse({"icy-metaint": "64"}, [raw]))
    result = await main._read_icy_now_playing("http://x")
    assert result["artist"] == "Anyma"
    assert result["title"] == "Eternity"


async def test_no_icy_metaint_header(monkeypatch):
    _patch_client(monkeypatch, _FakeStreamResponse({}, [b"audio" * 20]))
    result = await main._read_icy_now_playing("http://x")
    assert result["artist"] == "" and result["title"] == ""


async def test_zero_length_metadata_block_means_no_change(monkeypatch):
    raw = _icy_bytes(64, b"audio" * 10, None)
    _patch_client(monkeypatch, _FakeStreamResponse({"icy-metaint": "64"}, [raw]))
    result = await main._read_icy_now_playing("http://x")
    assert result["artist"] == "" and result["title"] == ""


async def test_json_blob_is_rejected(monkeypatch):
    # The real case this guards against — Pure Ibiza Radio's encoder:
    # StreamTitle='NOW ON AIR   {"autor":"Pure Artists","sesion":"Pure Sessions"}';
    raw = _icy_bytes(64, b"audio" * 10, 'StreamTitle=\'NOW ON AIR   {"autor":"Pure Artists"}\';')
    _patch_client(monkeypatch, _FakeStreamResponse({"icy-metaint": "64"}, [raw]))
    result = await main._read_icy_now_playing("http://x")
    assert result["artist"] == "" and result["title"] == ""


async def test_station_name_as_title_is_rejected(monkeypatch):
    """The real Sunshine Live case: icy-name and StreamTitle are both
    "SUNSHINE LIVE - Techno", permanently, on every one of its streams.

    Unfiltered this was worse than sending nothing at all — the " - " split
    produced artist="SUNSHINE LIVE" / title="Techno", which reads as a real
    track, and its non-empty title suppressed the playlist and Shazam
    fallbacks in now_playing() that do find the actual song."""
    raw = _icy_bytes(64, b"audio" * 10, "StreamTitle='SUNSHINE LIVE - Techno';")
    _patch_client(monkeypatch, _FakeStreamResponse(
        {"icy-metaint": "64", "icy-name": "SUNSHINE LIVE - Techno"}, [raw]))
    result = await main._read_icy_now_playing("http://x")
    assert result["artist"] == "" and result["title"] == ""


async def test_station_name_match_ignores_case_and_padding(monkeypatch):
    raw = _icy_bytes(64, b"audio" * 10, "StreamTitle='  sunshine live - techno  ';")
    _patch_client(monkeypatch, _FakeStreamResponse(
        {"icy-metaint": "64", "icy-name": "SUNSHINE LIVE - Techno"}, [raw]))
    result = await main._read_icy_now_playing("http://x")
    assert result["artist"] == "" and result["title"] == ""


async def test_real_track_still_passes_when_icy_name_present(monkeypatch):
    """The guard must only fire on an exact match. Every working station in
    stations.json sends an icy-name alongside real titles — checked against
    all of them, and Sunshine's three streams were the only ones where the
    two are equal."""
    raw = _icy_bytes(64, b"audio" * 10, "StreamTitle='Milk & Sugar - Higher';")
    _patch_client(monkeypatch, _FakeStreamResponse(
        {"icy-metaint": "64", "icy-name": "CHILLOUT ANTENNE"}, [raw]))
    result = await main._read_icy_now_playing("http://x")
    assert result["artist"] == "Milk & Sugar"
    assert result["title"] == "Higher"


async def test_title_equal_to_icy_name_only_rejected_when_header_present(monkeypatch):
    """No icy-name header means nothing to compare against — the title stands."""
    raw = _icy_bytes(64, b"audio" * 10, "StreamTitle='SUNSHINE LIVE - Techno';")
    _patch_client(monkeypatch, _FakeStreamResponse({"icy-metaint": "64"}, [raw]))
    result = await main._read_icy_now_playing("http://x")
    assert result["artist"] == "SUNSHINE LIVE"
    assert result["title"] == "Techno"


async def test_chunked_across_multiple_reads(monkeypatch):
    """Real network reads rarely land on a clean boundary — split into an
    awkward chunk size to exercise the buffering/target-tracking logic."""
    raw = _icy_bytes(64, b"audio" * 10, "StreamTitle='A - B';")
    chunks = [raw[i:i + 7] for i in range(0, len(raw), 7)]
    _patch_client(monkeypatch, _FakeStreamResponse({"icy-metaint": "64"}, chunks))
    result = await main._read_icy_now_playing("http://x")
    assert result["artist"] == "A" and result["title"] == "B"


async def test_stream_ends_before_metadata_arrives(monkeypatch):
    _patch_client(monkeypatch, _FakeStreamResponse({"icy-metaint": "64"}, [b"short"]))
    result = await main._read_icy_now_playing("http://x")
    assert result["artist"] == "" and result["title"] == ""


async def test_follows_redirects(monkeypatch):
    """Regression: several real stations 302 to a geo-nearest edge node (e.g.
    stream.rcs.revma.com -> nXX-eu.rcs.revma.com). httpx does not follow
    redirects by default (unlike curl -L / urllib) — this caught a real bug
    where every redirecting station silently read the 302 response itself
    (no icy-metaint at all) instead of the actual stream, on first deploy."""
    captured_kwargs = {}
    def fake_client(**kw):
        captured_kwargs.update(kw)
        return _FakeClient(_FakeStreamResponse({}, [b""]))
    monkeypatch.setattr(main.httpx, "AsyncClient", fake_client)

    await main._read_icy_now_playing("http://x")
    assert captured_kwargs.get("follow_redirects") is True


async def test_connection_error_returns_empty_not_raises(monkeypatch):
    class _BoomClient:
        def stream(self, *a, **kw):
            raise RuntimeError("connection refused")
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kw: _BoomClient())
    result = await main._read_icy_now_playing("http://x")
    assert result == EMPTY


# ── _read_icy_now_playing: stream format (from headers, independent of title) ─

async def test_format_bitrate_sample_rate_from_icy_headers(monkeypatch):
    raw = _icy_bytes(64, b"audio" * 10, None)  # no title change, format should still populate
    headers = {"icy-metaint": "64", "content-type": "audio/mpeg", "icy-br": "128", "icy-sr": "44100"}
    _patch_client(monkeypatch, _FakeStreamResponse(headers, [raw]))
    result = await main._read_icy_now_playing("http://x")
    assert result["format"] == "MP3"
    assert result["bitrate"] == 128
    assert result["sample_rate"] == 44100


async def test_format_falls_back_to_icy_audio_info(monkeypatch):
    """Some encoders (e.g. laut.fm) send a combined icy-audio-info string
    instead of separate icy-br/icy-sr headers."""
    raw = _icy_bytes(64, b"audio" * 10, None)
    headers = {
        "icy-metaint": "64",
        "content-type": "audio/aac",
        "icy-audio-info": "ice-channels=2;ice-samplerate=44100;ice-bitrate=128",
    }
    _patch_client(monkeypatch, _FakeStreamResponse(headers, [raw]))
    result = await main._read_icy_now_playing("http://x")
    assert result["format"] == "AAC"
    assert result["bitrate"] == 128
    assert result["sample_rate"] == 44100


async def test_format_present_even_with_no_metaint(monkeypatch):
    """format/bitrate come from headers available before metaint is even
    checked — a non-ICY server should still report them if present."""
    headers = {"content-type": "audio/mpeg", "icy-br": "192"}
    _patch_client(monkeypatch, _FakeStreamResponse(headers, [b"audio"]))
    result = await main._read_icy_now_playing("http://x")
    assert result["format"] == "MP3"
    assert result["bitrate"] == 192
    assert result["title"] == ""  # still no track — no metaint means no ICY metadata at all


async def test_unknown_content_type_falls_back_to_subtype_uppercased(monkeypatch):
    headers = {"content-type": "audio/ogg"}
    _patch_client(monkeypatch, _FakeStreamResponse(headers, [b"audio"]))
    result = await main._read_icy_now_playing("http://x")
    assert result["format"] == "OGG"


async def test_comma_joined_icy_br_does_not_crash(monkeypatch):
    """Regression: Pure Ibiza Radio's encoder sends 'icy-br: 256, 256' (some
    Icecast setups send the header twice; httpx merges duplicates with a
    comma). int('256, 256') raises ValueError — which, uncaught, blew up the
    *entire* read via the outer try/except and silently wiped out the track
    title too, not just bitrate. Caught by testing against the real stream."""
    headers = {"content-type": "audio/mpeg", "icy-br": "256, 256"}
    _patch_client(monkeypatch, _FakeStreamResponse(headers, [b"audio"]))
    result = await main._read_icy_now_playing("http://x")
    assert result["bitrate"] == 256
    assert result["format"] == "MP3"  # proves the crash didn't wipe out format too


async def test_audio_info_without_ice_prefix(monkeypatch):
    """Regression: Pure Ibiza Radio's icy-audio-info uses bare 'bitrate='/
    'samplerate=' (no 'ice-' prefix), unlike laut.fm's 'ice-bitrate='/
    'ice-samplerate='. Both must parse."""
    headers = {
        "content-type": "audio/mpeg",
        "icy-audio-info": "bitrate=256;samplerate=48000;channels=2",
    }
    _patch_client(monkeypatch, _FakeStreamResponse(headers, [b"audio"]))
    result = await main._read_icy_now_playing("http://x")
    assert result["bitrate"] == 256
    assert result["sample_rate"] == 48000


async def test_audio_info_header_name_without_y(monkeypatch):
    """Regression: the header *name* itself varies, not just its params —
    Pure Ibiza Radio sends 'ice-audio-info' (no y), laut.fm sends
    'icy-audio-info'. sample_rate silently stayed None for Pure Ibiza until
    this was caught by testing against the real stream."""
    headers = {
        "content-type": "audio/mpeg",
        "icy-br": "256, 256",  # the real duplicate-header quirk, both bugs at once
        "ice-audio-info": "bitrate=256;samplerate=48000;channels=2",
    }
    _patch_client(monkeypatch, _FakeStreamResponse(headers, [b"audio"]))
    result = await main._read_icy_now_playing("http://x")
    assert result["bitrate"] == 256       # from icy-br
    assert result["sample_rate"] == 48000  # from ice-audio-info fallback


# ── _read_icy_now_playing_cached ─────────────────────────────────────────────

async def test_cache_avoids_second_network_call(monkeypatch):
    calls = {"n": 0}
    async def fake_read(url, timeout=10.0):
        calls["n"] += 1
        return {**EMPTY, "artist": "A", "title": "B"}
    monkeypatch.setattr(main, "_read_icy_now_playing", fake_read)
    main._meta_cache.clear()

    r1 = await main._read_icy_now_playing_cached("http://x")
    r2 = await main._read_icy_now_playing_cached("http://x")
    assert r1 == r2
    assert calls["n"] == 1  # second call served from cache


async def test_cache_expires_after_ttl(monkeypatch):
    calls = {"n": 0}
    async def fake_read(url, timeout=10.0):
        calls["n"] += 1
        return {**EMPTY, "title": str(calls["n"])}
    monkeypatch.setattr(main, "_read_icy_now_playing", fake_read)
    monkeypatch.setattr(main, "META_CACHE_TTL", 0)  # expire immediately
    main._meta_cache.clear()

    await main._read_icy_now_playing_cached("http://x")
    await main._read_icy_now_playing_cached("http://x")
    assert calls["n"] == 2


async def test_cache_is_keyed_per_url(monkeypatch):
    async def fake_read(url, timeout=10.0):
        return {**EMPTY, "title": url}
    monkeypatch.setattr(main, "_read_icy_now_playing", fake_read)
    main._meta_cache.clear()

    r1 = await main._read_icy_now_playing_cached("http://station-a")
    r2 = await main._read_icy_now_playing_cached("http://station-b")
    assert r1["title"] == "http://station-a"
    assert r2["title"] == "http://station-b"


# ── _read_icecast_listeners_cached ───────────────────────────────────────────

async def test_listeners_unknown_station_returns_none():
    main._icecast_cache.clear()
    result = await main._read_icecast_listeners_cached("Some Random Station")
    assert result is None


async def test_listeners_matches_mount_in_source_list(monkeypatch):
    """Icecast returns icestats.source as a list when a port hosts multiple
    mounts — this is the real shape control.streaming-pro.com:8000 returns."""
    async def fake_get(self, url, **kw):
        class R:
            def json(self_):
                return {"icestats": {"source": [
                    {"listenurl": "http://control.streaming-pro.com:8000/AutoDj.mp3", "listeners": 1},
                    {"listenurl": "http://control.streaming-pro.com:8000/ibizaglobalclassics.mp3", "listeners": 42},
                ]}}
        return R()
    monkeypatch.setattr(main.httpx.AsyncClient, "get", fake_get)
    main._icecast_cache.clear()

    result = await main._read_icecast_listeners_cached("Ibiza Global Classics")
    assert result == 42


async def test_listeners_matches_single_source_dict(monkeypatch):
    """Icecast returns icestats.source as a single dict (not a list) when a
    port hosts exactly one mount — must be normalized, not just indexed."""
    async def fake_get(self, url, **kw):
        class R:
            def json(self_):
                return {"icestats": {"source": {"listenurl": "http://control.streaming-pro.com:8028/stream.mp3", "listeners": 161}}}
        return R()
    monkeypatch.setattr(main.httpx.AsyncClient, "get", fake_get)
    main._icecast_cache.clear()

    result = await main._read_icecast_listeners_cached("Pure Ibiza Radio")
    assert result == 161


async def test_listeners_request_failure_returns_none(monkeypatch):
    async def fake_get(self, url, **kw):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(main.httpx.AsyncClient, "get", fake_get)
    main._icecast_cache.clear()

    result = await main._read_icecast_listeners_cached("Pure Ibiza Radio")
    assert result is None
