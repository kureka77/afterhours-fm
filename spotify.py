"""Spotify integration — search + save the currently-playing track to Liked
Songs.

Single-user by design: it authorizes once out-of-band and then holds a
long-lived refresh token in the environment (see .env.example), rather than
running an interactive OAuth flow per visitor. Written async on httpx to fit
FastAPI's request path — a blocking call here would stall the event loop for
the whole Spotify round trip.
"""
import base64
import os
import re

import config  # noqa: F401 — import for its side effect: loads .env

import httpx

TOKEN_URL  = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
# Feb 2026 API migration: PUT /me/tracks (ids=) was deprecated in favor of a
# generic library endpoint taking full Spotify URIs. Worth knowing if you hit
# this yourself — the old endpoint fails with a bare 403 and no deprecation
# notice, so it reads like a scope problem rather than a moved endpoint.
SAVE_URL   = "https://api.spotify.com/v1/me/library"


class SpotifyNotConfigured(Exception):
    """SPOTIFY_CLIENT_ID / SECRET / REFRESH_TOKEN missing from the environment."""


class SpotifyNoMatch(Exception):
    """Spotify's search returned nothing for this artist/title."""


def _creds() -> tuple[str, str, str]:
    client_id     = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    refresh_token = os.getenv("SPOTIFY_REFRESH_TOKEN")
    if not all([client_id, client_secret, refresh_token]):
        raise SpotifyNotConfigured(
            "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET / SPOTIFY_REFRESH_TOKEN missing from .env"
        )
    return client_id, client_secret, refresh_token


async def _get_access_token(client: httpx.AsyncClient) -> str:
    client_id, client_secret, refresh_token = _creds()
    auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = await client.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers={"Authorization": f"Basic {auth_header}"},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ── Matching ICY metadata to a Spotify track ────────────────────────────────────
# Broadcaster metadata is not a search query. It arrives ALL CAPS, uses "x" /
# "FEAT." / "&" as artist separators, and carries version suffixes the release
# may not use ("(Extended Mix)", "[Club Mix]", "(Radio Edit)"). Spotify's
# free-text search never says "no" to a loose query — it ranks *something* — so
# taking items[0] blind returned confidently wrong art. Measured against real
# station output: 6 of 8 matched results were a different song entirely
# (SUGARDADDY "Don't Look Any Further" -> Nelly "Batter Up"), and one was a
# karaoke backing version. The wrong cover was the visible half; the wrong
# spotify_url was persisted on the row, which outlives it.
_PARENTHETICAL = re.compile(r"[\(\[][^)\]]*[\)\]]")
_ARTIST_SEP    = re.compile(r"\s+(?:x|&|vs\.?|feat\.?|ft\.?|featuring|with|and)\s+|,\s*", re.I)
_NON_WORD      = re.compile(r"[^a-z0-9]+")
# A search for a track Spotify doesn't carry often lands on a soundalike whose
# artist literally advertises itself as a stand-in. None of these ever appear in
# a broadcaster's own artist field, so their presence on only one side is a
# reliable reject.
_IMPOSTOR = ("karaoke", "tribute", "made popular by", "backing track",
             "backing version", "originally performed", "in the style of")


def _norm(text: str) -> str:
    """Lowercase, punctuation-flattened form used for all comparisons."""
    return _NON_WORD.sub(" ", text.lower()).strip()


def _core_title(title: str) -> str:
    """Title with version suffixes removed — "Night Haze (Edit)" -> "night haze".

    Both sides get this treatment, so an ICY "(Extended Mix)" can still match a
    Spotify release that spells the same version " - Extended Mix" (Spotify puts
    it after a dash, not in brackets, which is why a raw string compare fails on
    tracks that are in fact the same recording).
    """
    return _norm(_PARENTHETICAL.sub(" ", title).split(" - ")[0])


def _artist_names(artist: str) -> list[str]:
    """Split a multi-artist ICY credit into individual normalized names."""
    return [n for n in (_norm(p) for p in _ARTIST_SEP.split(artist)) if n]


def _name_match(a: str, b: str) -> bool:
    """Do two names agree, allowing either to be the longer credit?

    Compared as *whole-token runs*, not raw substrings: the station may credit
    "Adriatique" where Spotify credits "Adriatique x GENESI", or the reverse.
    A plain `in` test would also call "&ME" (normalized to "me") a match for
    "Megamen", which is the kind of near-miss this whole matcher exists to
    reject.
    """
    ta, tb = _norm(a).split(), _norm(b).split()
    if not ta or not tb:
        return False
    outer, inner = (ta, tb) if len(ta) >= len(tb) else (tb, ta)
    span = len(inner)
    return any(outer[i:i + span] == inner for i in range(len(outer) - span + 1))


def _is_match(artist: str, title: str, candidate: dict) -> bool:
    """Does this Spotify result actually correspond to what the station played?

    Requires agreement on *both* artist and title. Artist alone is too weak
    (broadcasters credit remixers inconsistently) and title alone is far too
    weak — "Show Me Love" matched a different song of the same name by a
    different artist.
    """
    cand_artists = [_norm(a["name"]) for a in candidate.get("artists", [])]
    cand_title   = candidate.get("name", "")

    joined = " ".join(cand_artists)
    if any(word in joined for word in _IMPOSTOR) and not any(
        word in artist.lower() for word in _IMPOSTOR
    ):
        return False

    wanted = _artist_names(artist)
    if not wanted or not cand_artists:
        return False
    if not any(_name_match(w, c) for w in wanted for c in cand_artists):
        return False

    return _name_match(_core_title(title), _core_title(cand_title))


def _pick(artist: str, title: str, items: list[dict]) -> dict | None:
    for track in items:
        if _is_match(artist, title, track):
            images = track.get("album", {}).get("images", [])
            return {
                "id":             track["id"],
                "matched_artist": track["artists"][0]["name"],
                "matched_title":  track["name"],
                "spotify_url":    track["external_urls"]["spotify"],
                "cover_url":      images[0]["url"] if images else None,
            }
    return None


async def _query(client: httpx.AsyncClient, headers: dict, q: str) -> list[dict]:
    resp = await client.get(
        SEARCH_URL,
        # limit 5, not 1: the top hit for a messy ICY string is often junk while
        # the right release sits a place or two below it. Verification decides,
        # so asking for more costs nothing and rescues real matches.
        params={"q": q, "type": "track", "limit": 5},
        headers=headers,
    )
    resp.raise_for_status()
    return resp.json().get("tracks", {}).get("items", [])


async def _search(client: httpx.AsyncClient, headers: dict, artist: str, title: str) -> dict | None:
    """Best verified Spotify match for an artist/title, or None.

    Two queries, strictest first. The field-filtered form (artist:"…" track:"…")
    tells Spotify which half is which, so it stops ranking on accidental word
    overlap; it is also brittle when the broadcaster's spelling differs, which
    is what the free-text retry is for. Both results go through the same
    verification, so the fallback can't smuggle in a wrong track.
    """
    names = _artist_names(artist)
    attempts = []
    if names and title:
        # Quotes are the filter's own delimiter, so a title like 'Say "Yes"'
        # would close it early and make Spotify 400 the whole request.
        primary = _ARTIST_SEP.split(artist)[0].strip().replace('"', "")
        track   = _PARENTHETICAL.sub(" ", title).strip().replace('"', "")
        attempts.append(f'artist:"{primary}" track:"{track}"')
    attempts.append(f"{artist} {title}")

    for q in attempts:
        match = _pick(artist, title, await _query(client, headers, q))
        if match:
            return match
    return None


async def search_track(artist: str, title: str) -> dict | None:
    """Search only, no save — used to fetch cover art for whatever's showing
    in the now-playing panel, regardless of whether the user has clicked
    "Add to Spotify". Returns None on no match; raises SpotifyNotConfigured
    if credentials are missing (callers treat that as "no cover art
    available", same as a no-match — see main.py)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        access_token = await _get_access_token(client)
        return await _search(client, {"Authorization": f"Bearer {access_token}"}, artist, title)


async def save_current_track(artist: str, title: str) -> dict:
    """Search Spotify for artist/title and save the best match to Liked Songs.

    Returns {"matched_artist", "matched_title", "spotify_url", "cover_url"}.
    Raises SpotifyNotConfigured or SpotifyNoMatch — the route translates
    those into 503 / 404 respectively; any other httpx error propagates as
    a raw HTTPStatusError for the route to turn into a 502.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        access_token = await _get_access_token(client)
        headers = {"Authorization": f"Bearer {access_token}"}

        match = await _search(client, headers, artist, title)
        if not match:
            raise SpotifyNoMatch(f"No Spotify match for '{artist} - {title}'")

        uri = f"spotify:track:{match['id']}"
        resp = await client.put(SAVE_URL, params={"uris": uri}, headers=headers)
        resp.raise_for_status()

        return match
