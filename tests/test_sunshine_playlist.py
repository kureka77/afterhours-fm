"""Tests for sunshine_playlist — the broadcaster's own published playlist,
used for the Sunshine Live channels whose ICY metadata only ever announces
the station name (see streams.md and test_poll.py)."""
from datetime import datetime, timedelta

import httpx
import pytest

import sunshine_playlist
from sunshine_playlist import STATION_TZ


# ── Fixtures / helpers ─────────────────────────────────────────────────────────

def _song(title="Triplet King", artist="Anna Reusch", **cover):
    """One song node in the API's nested shape. The real payload always
    includes every cover_art_url_* key, empty when unset — hence the explicit
    empty strings rather than absent keys."""
    node = {
        "title": title,
        "artist": {"found": "1", "entry": [{"name": artist}]},
        "cover_art_url_custom": "",
        "cover_art_url_xl": "",
        "cover_art_url_l": "",
        "cover_art_url_m": "",
    }
    node.update(cover)
    return node


def _payload(*entries):
    """entries: (airtime datetime, duration seconds, song dict)"""
    return {"result": {
        "found": str(len(entries)),
        "entry": [
            {"airtime": at.isoformat(), "duration": str(dur),
             "song": {"found": "1", "entry": [song]}}
            for at, dur, song in entries
        ],
    }}


@pytest.fixture
def now():
    return datetime.now(STATION_TZ)


def _patch_get(monkeypatch, payload=None, exc=None, status=200):
    """Stub httpx.AsyncClient.get. The module builds its own client, so this
    patches the class rather than injecting one."""
    captured = {}

    class _Resp:
        status_code = status
        def raise_for_status(self):
            if status >= 400:
                raise httpx.HTTPStatusError("boom", request=None, response=None)
        def json(self):
            return payload

    class _Client:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            captured["url"] = url
            captured["params"] = params
            if exc:
                raise exc
            return _Resp()

    monkeypatch.setattr(sunshine_playlist.httpx, "AsyncClient", _Client)
    return captured


# ── Channel mapping ────────────────────────────────────────────────────────────

async def test_unmapped_station_returns_none_without_calling_api(monkeypatch):
    """fetch_for_station is called unconditionally by the route, so a station
    with no playlist feed must be a cheap no-op, not a wasted request."""
    called = _patch_get(monkeypatch, _payload())
    assert await sunshine_playlist.fetch_for_station("Antenne Bayern") is None
    assert called == {}


async def test_main_simulcast_is_deliberately_unmapped():
    """Channel 3 publishes a *show* schedule (hourly, ~8s entries carrying
    programme and host, e.g. artist="Clapcast" title="Claptone"), not tracks.
    Mapping it would publish show names as songs — the exact bug this module
    exists to fix. It falls through to Shazam instead."""
    assert "Sunshine Live" not in sunshine_playlist.CHANNELS
    assert sunshine_playlist.CHANNELS == {
        "Sunshine Live Techno": 9,
        "Sunshine Live House": 4,
    }


# ── Time window ────────────────────────────────────────────────────────────────

async def test_window_is_sent_in_berlin_local_time(monkeypatch):
    """The API ignores the UTC offset it is sent and matches only the naive
    wall-clock digits against Europe/Berlin. Sending a correct UTC timestamp
    therefore returns tracks hours stale rather than an error — so pin that
    the digits we send are Berlin's, not UTC's."""
    captured = _patch_get(monkeypatch, _payload())
    await sunshine_playlist.fetch_now_playing(9)

    berlin_now = datetime.now(STATION_TZ)
    start = datetime.fromisoformat(captured["params"]["start"])
    end = datetime.fromisoformat(captured["params"]["end"])

    assert start.utcoffset() == berlin_now.utcoffset()
    # Wall-clock digits must match Berlin's, which is what the API reads.
    assert abs((start.replace(tzinfo=None)
                - (berlin_now.replace(tzinfo=None) - sunshine_playlist.LOOKBACK))
               .total_seconds()) < 5
    assert abs((end.replace(tzinfo=None)
                - (berlin_now.replace(tzinfo=None) + sunshine_playlist.LOOKAHEAD))
               .total_seconds()) < 5
    assert captured["params"]["station"] == 9


# ── Picking the current track ──────────────────────────────────────────────────

async def test_returns_newest_entry(monkeypatch, now):
    _patch_get(monkeypatch, _payload(
        (now - timedelta(minutes=12), 300, _song("Older", "A")),
        (now - timedelta(minutes=2), 300, _song("Newest", "B")),
        (now - timedelta(minutes=7), 300, _song("Middle", "C")),
    ))
    result = await sunshine_playlist.fetch_now_playing(9)
    assert result["title"] == "Newest"
    assert result["artist"] == "B"


async def test_duplicate_rows_for_same_track_collapse(monkeypatch, now):
    """The real API logs the same track twice, a second apart — observed with
    'Clotur & Vault Records - Arkadia'. Taking the max airtime makes that a
    non-issue rather than two history entries."""
    _patch_get(monkeypatch, _payload(
        (now - timedelta(minutes=3), 348, _song("Arkadia", "Clotur")),
        (now - timedelta(minutes=3, seconds=-1), 348, _song("Arkadia", "Clotur")),
    ))
    result = await sunshine_playlist.fetch_now_playing(9)
    assert result["title"] == "Arkadia"


async def test_finished_track_is_not_reported_as_current(monkeypatch, now):
    """Past its duration plus the grace window — a station that stopped
    publishing must not leave the last track pinned forever. Returning None
    lets the caller fall through to Shazam."""
    _patch_get(monkeypatch, _payload(
        (now - timedelta(minutes=30), 60, _song("Long Over", "A")),
    ))
    assert await sunshine_playlist.fetch_now_playing(9) is None


async def test_track_still_current_within_grace(monkeypatch, now):
    started = now - timedelta(seconds=200)
    _patch_get(monkeypatch, _payload((started, 180, _song("Just Ended", "A"))))
    result = await sunshine_playlist.fetch_now_playing(9)
    assert result["title"] == "Just Ended"


async def test_show_schedule_shaped_entry_is_rejected(monkeypatch, now):
    """What channel 3 actually returns: an hourly programme entry with a ~8
    second duration. The staleness guard rejects it on its own merits, which
    is the second line of defence behind not mapping that channel at all."""
    _patch_get(monkeypatch, _payload(
        (now - timedelta(minutes=40), 8, _song("Claptone", "Clapcast")),
    ))
    assert await sunshine_playlist.fetch_now_playing(3) is None


async def test_far_future_airtime_is_rejected(monkeypatch, now):
    _patch_get(monkeypatch, _payload(
        (now + timedelta(minutes=45), 300, _song("Not Yet", "A")),
    ))
    assert await sunshine_playlist.fetch_now_playing(9) is None


# ── Cover art ──────────────────────────────────────────────────────────────────

async def test_prefers_largest_cover(monkeypatch, now):
    _patch_get(monkeypatch, _payload((now, 300, _song(
        cover_art_url_xl="https://img/xl.jpg",
        cover_art_url_l="https://img/l.jpg",
    ))))
    result = await sunshine_playlist.fetch_now_playing(9)
    assert result["cover_url"] == "https://img/xl.jpg"


async def test_skips_empty_cover_keys(monkeypatch, now):
    """Every cover key is present-but-empty when unset, so .get() alone would
    happily return "" and the frontend would render a broken image."""
    _patch_get(monkeypatch, _payload((now, 300, _song(cover_art_url_m="https://img/m.jpg"))))
    result = await sunshine_playlist.fetch_now_playing(9)
    assert result["cover_url"] == "https://img/m.jpg"


async def test_no_cover_at_all_is_none_not_empty_string(monkeypatch, now):
    _patch_get(monkeypatch, _payload((now, 300, _song())))
    result = await sunshine_playlist.fetch_now_playing(9)
    assert result["cover_url"] is None


# ── Failure paths: all must return None, never raise ───────────────────────────

async def test_network_error_returns_none(monkeypatch):
    _patch_get(monkeypatch, exc=httpx.ConnectError("no route"))
    assert await sunshine_playlist.fetch_now_playing(9) is None


async def test_http_error_returns_none(monkeypatch):
    _patch_get(monkeypatch, _payload(), status=503)
    assert await sunshine_playlist.fetch_now_playing(9) is None


async def test_empty_result_returns_none(monkeypatch):
    _patch_get(monkeypatch, {"result": {"found": "0", "entry": []}})
    assert await sunshine_playlist.fetch_now_playing(9) is None


@pytest.mark.parametrize("payload", [
    {},
    {"result": None},
    {"result": {"entry": None}},
    {"result": {"entry": [{"airtime": "not-a-date", "duration": "1", "song": {"entry": [{}]}}]}},
    {"result": {"entry": [{"duration": "1", "song": {"entry": [{"title": "T"}]}}]}},
    {"result": {"entry": [{"airtime": "2026-08-31T23:00:00+02:00", "song": {"entry": []}}]}},
])
async def test_malformed_payloads_return_none(monkeypatch, payload):
    """The endpoint is undocumented and can change shape without notice — a
    breaking change must degrade to Shazam, not 500 the now-playing route."""
    _patch_get(monkeypatch, payload)
    assert await sunshine_playlist.fetch_now_playing(9) is None


async def test_missing_duration_defaults_to_zero_not_crash(monkeypatch, now):
    """No duration means we can't tell when it ends; it stays current only for
    the grace window."""
    payload = _payload((now, 0, _song("No Duration", "A")))
    del payload["result"]["entry"][0]["duration"]
    _patch_get(monkeypatch, payload)
    result = await sunshine_playlist.fetch_now_playing(9)
    assert result["title"] == "No Duration"


async def test_missing_artist_yields_empty_string_not_none(monkeypatch, now):
    """main.py builds an "artist|title" cache key and writes artist to a DB
    column — it needs a string, not None."""
    song = _song()
    del song["artist"]
    _patch_get(monkeypatch, _payload((now, 300, song)))
    result = await sunshine_playlist.fetch_now_playing(9)
    assert result["artist"] == ""
    assert result["title"] == "Triplet King"


async def test_blank_title_returns_none(monkeypatch, now):
    _patch_get(monkeypatch, _payload((now, 300, _song(title="   "))))
    assert await sunshine_playlist.fetch_now_playing(9) is None
