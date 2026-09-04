"""Tests for the Spotify match verifier in spotify.py.

Every case here is a real string logged by a station, paired with the real
result Spotify's search returned for it. Before verification existed the route
took `items[0]` blind, and 6 of 8 matched results were a different song — the
wrong cover was the visible half, the wrong spotify_url was persisted on the
row and outlived it.
"""
import pytest

import spotify


def _track(artists, title, track_id="id1"):
    """One item in Spotify's search response, trimmed to the fields read."""
    return {
        "id": track_id,
        "name": title,
        "artists": [{"name": a} for a in artists],
        "album": {"images": [{"url": "https://i.scdn.co/image/cover.jpg"}]},
        "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"},
    }


# ── Normalizing broadcaster strings ────────────────────────────────────────────

def test_core_title_strips_both_version_spellings():
    """The station writes "(Edit)", Spotify writes " - Edit" — same recording."""
    assert spotify._core_title("Night Haze (Edit)") == "night haze"
    assert spotify._core_title("Night Haze - Edit") == "night haze"
    assert spotify._core_title("Soul Brother [Superlover Remix]") == "soul brother"


def test_artist_names_splits_every_separator_a_station_uses():
    assert spotify._artist_names("JOEL CORRY x DAVID GUETTA x BRYSON TILLER") == [
        "joel corry", "david guetta", "bryson tiller"]
    assert spotify._artist_names("JOEZI FEAT. COCO & PAPE DIOUF") == [
        "joezi", "coco", "pape diouf"]


def test_name_match_compares_token_runs_not_substrings():
    """"&ME" normalizes to "me", which is a substring of "Megamen" but not a
    token of it. Raw `in` would call that a match."""
    assert spotify._name_match("Adriatique", "Adriatique x GENESI")
    assert spotify._name_match("Robin S.", "Robin S")
    assert not spotify._name_match("&ME", "Megamen")


# ── Rejecting the wrong track ──────────────────────────────────────────────────

@pytest.mark.parametrize("artist, title, cand_artists, cand_title", [
    # Real top hits from before verification, all confidently wrong.
    ("SUGARDADDY", "Don't Look Any Further", ["Nelly"], "Batter Up"),
    ("Robin S.", "Show Me Love", ["Francis Mercier"], "Show Me Love (Devotion)"),
    ("ARODES x MOJAVE GREY", "Beyond", ["OsmosisJones"], "Worldwide"),
    ("Nikonn", "Sunday (Original Mix)", ["Melba Moore"], "My Heart Belongs To You"),
])
def test_rejects_a_different_song(artist, title, cand_artists, cand_title):
    assert not spotify._is_match(artist, title, _track(cand_artists, cand_title))


def test_rejects_a_karaoke_impostor():
    """Right title, and the artist field even names the real act — as the thing
    it is imitating. Title-only agreement would have let this through."""
    candidate = _track(
        ["Party Tyme Karaoke"],
        "What Would You Do? (made popular by Joel Corry x David Guetta) [backing version]")
    assert not spotify._is_match(
        "JOEL CORRY x DAVID GUETTA x BRYSON TILLER", "What Would You Do", candidate)


@pytest.mark.parametrize("artist, title, cand_artists, cand_title", [
    ("ADRIATIQUE x GENESI", "Closer", ["Adriatique", "GENESI"], "Closer"),
    ("Traumer", "Night Haze (Edit)", ["Traumer"], "Night Haze - Edit"),
    ("Robin S.", "Show Me Love", ["Robin S"], "Show Me Love"),
    # The station credits the collective, Spotify credits the members.
    ("KEINEMUSIK", "Thandaza", ["&ME", "Rampa", "Keinemusik"], "Thandaza"),
])
def test_accepts_the_same_recording_credited_differently(artist, title, cand_artists, cand_title):
    assert spotify._is_match(artist, title, _track(cand_artists, cand_title))


# ── Query strategy ─────────────────────────────────────────────────────────────

async def test_search_looks_past_the_top_hit(monkeypatch):
    """limit is 5, not 1: the right release often sits below junk. Verification
    decides which one, so the extra candidates cost nothing."""
    items = [_track(["Nelly"], "Batter Up", "wrong"),
             _track(["Sugardaddy"], "Don't Look Any Further", "right")]
    monkeypatch.setattr(spotify, "_query", _stub_query({None: items}))

    match = await spotify._search(None, {}, "SUGARDADDY", "Don't Look Any Further")
    assert match["spotify_url"].endswith("right")
    assert match["cover_url"] == "https://i.scdn.co/image/cover.jpg"


async def test_search_falls_back_to_free_text(monkeypatch):
    """The field-filtered query is precise but brittle when the broadcaster's
    spelling differs — it returned nothing at all for several real cases."""
    seen = []

    async def fake_query(client, headers, q):
        seen.append(q)
        return [] if q.startswith("artist:") else [_track(["Joezi"], "7 Seconds")]

    monkeypatch.setattr(spotify, "_query", fake_query)
    match = await spotify._search(None, {}, "JOEZI FEAT. COCO & PAPE DIOUF", "7 Seconds")

    assert match["matched_title"] == "7 Seconds"
    assert seen[0].startswith('artist:"JOEZI"')
    assert seen[1] == "JOEZI FEAT. COCO & PAPE DIOUF 7 Seconds"


async def test_search_returns_none_rather_than_a_wrong_track(monkeypatch):
    """Nothing Spotify offered for this string was the song. Showing no cover
    is the correct outcome — a wrong one is worse than none."""
    monkeypatch.setattr(spotify, "_query", _stub_query(
        {None: [_track(["Kings Of Tomorrow"], "Finally")]}))

    assert await spotify._search(None, {}, "CASSIUS", "1999 Remix (Radio Edit)") is None


def _stub_query(by_query):
    async def fake_query(client, headers, q):
        return by_query.get(q, by_query.get(None, []))
    return fake_query
