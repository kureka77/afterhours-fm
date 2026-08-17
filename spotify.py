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


async def _search(client: httpx.AsyncClient, headers: dict, artist: str, title: str) -> dict | None:
    resp = await client.get(
        SEARCH_URL,
        params={"q": f"{artist} {title}", "type": "track", "limit": 1},
        headers=headers,
    )
    resp.raise_for_status()
    items = resp.json().get("tracks", {}).get("items", [])
    if not items:
        return None
    track = items[0]
    images = track.get("album", {}).get("images", [])
    return {
        "id":             track["id"],
        "matched_artist": track["artists"][0]["name"],
        "matched_title":  track["name"],
        "spotify_url":    track["external_urls"]["spotify"],
        "cover_url":      images[0]["url"] if images else None,
    }


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
