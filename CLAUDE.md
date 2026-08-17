# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the server

**With Docker (preferred):**

```bash
make dev    # hot-reload, SQLite, port 8000
make prod   # PostgreSQL + nginx, port 80, detached
make down   # stop all containers
```

Or directly:

```bash
docker compose up dev                        # dev
docker compose up db prod nginx --build -d   # prod
```

**Locally:**

```bash
source venv/bin/activate
uvicorn main:app --reload --port 8000
```

Config is environment-only, loaded via `python-dotenv` — copy `.env.example` to `.env`.
Nothing is required to play audio or identify tracks: stream info reads each station's
own ICY metadata, and Shazam recognition needs no API key. The Spotify variables are
optional and only affect the save button and cover art.

## File structure

```
afterhours-fm/
├── main.py               # FastAPI app — all API routes, ICY reader, caches, migration guard
├── stations.json         # Station registry: single source of truth AND the SSRF allowlist
├── database.py           # Async SQLAlchemy engine, session factory, Base
├── models.py             # ORM model: PlayedTrack (rating column is vestigial — see below)
├── spotify.py            # Spotify search + save-to-Liked-Songs
├── shazam_fallback.py    # Shazam-via-vibra recognition — fallback only
├── requirements.txt      # Production Python dependencies
├── requirements-dev.txt  # Dev/test-only dependencies (pytest etc.)
├── Dockerfile            # Multi-stage build: base → dev or prod target
├── docker-compose.yml    # dev, db (PostgreSQL), prod, and nginx services
├── Makefile              # Common tasks — see `make help`
├── package.json          # Vitest (JS test runner), npm audit
├── pytest.ini            # asyncio_mode = auto, testpaths = tests
├── .env.example          # Documents every env var, no real values
├── nginx/
│   └── nginx.conf        # Serves /static/ from disk; proxies /api/ to FastAPI
├── tests/
│   ├── conftest.py
│   ├── test_routes.py
│   ├── test_poll.py
│   ├── test_shazam_fallback.py
│   └── js/utils.test.js
├── .dockerignore         # Excludes venv, node_modules, .git, .env, app.db, etc.
├── streams.md            # Working notes on every station tried — what worked and why
├── .env                  # Secrets (git-ignored)
├── app.db                # SQLite database — local dev only (git-ignored)
└── static/
    ├── index.html        # Single-page HTML shell + all client-side JS (inline)
    ├── style.css         # All CSS — variables, layout, components, responsive
    ├── utils.js          # Pure helpers mirrored from index.html, covered by Vitest
    ├── hero.jpg / .webp  # Hero background (WebP 493 KB vs 811 KB JPEG)
    └── logo.png / .webp  # Nav logo (WebP 72×72, 2 KB)
```

## Architecture

```mermaid
graph TD
    Browser(["Browser"])

    subgraph prod["Docker — prod stack"]
        NGINX["nginx · :80"]
        Disk["static/ on disk<br/>WebP · CSS · JS"]
        FastAPI["FastAPI · :8000"]
        PG[("PostgreSQL<br/>afterhours_fm")]
    end

    subgraph ext["External services"]
        Stations["Radio stations<br/>(ICY/Icecast streams)"]
        CDN["jsDelivr CDN<br/>hls.js @1.5.13"]
        Fonts["Google Fonts"]
    end

    Browser -->|"GET / and /static/*"| NGINX
    NGINX -->|"serve from disk"| Disk
    Disk -->|"HTML · CSS · WebP"| Browser
    Browser -->|"GET /api/stations"| NGINX
    Browser -->|"GET /api/now-playing?station="| NGINX
    NGINX -->|"proxy"| FastAPI
    FastAPI <-->|"asyncpg"| PG
    FastAPI -->|"Icy-MetaData:1 — read one metadata cycle"| Stations
    Stations -->|"StreamTitle='Artist - Title'"| FastAPI
    Browser -->|"MP3 / HLS audio"| Stations
    Browser -.->|"hls.js deferred"| CDN
    Browser -.->|"web fonts"| Fonts
```

**Station registry:** `stations.json` is the single source of truth for which streams
exist. The backend loads it at import into `STATIONS` / `_STATION_URLS`; the frontend
fetches it via `/api/stations` instead of carrying its own copy.

This is also a security boundary, not just a tidiness choice. `/api/now-playing` takes
a station **name** and resolves the URL server-side. An earlier version accepted
`?url=` straight from the client and opened it — a server-side request forgery (SSRF)
hole, where a caller passes `http://169.254.169.254/` (cloud instance metadata) or
`http://localhost:5432` and makes the backend fetch internal endpoints on their behalf.
The parameter was removed rather than filtered, because removing the attacker's
control over the URL entirely is what actually closes it. Two regression tests pin
this: `test_now_playing_rejects_unknown_station` and
`test_now_playing_ignores_client_supplied_url`.

**Backend:** `main.py` — single-file FastAPI app with:
- `lifespan` context manager creates DB tables, then runs `_ensure_station_column()` — an idempotent hand-rolled migration guard (no Alembic in this project) that adds `played_tracks.station` if missing and backfills old rows to `'Pure Ibiza Radio'`. Takes an optional engine argument so tests can drive it (the lifespan itself never fires under `ASGITransport`), and reads the dialect off the engine rather than string-matching `DATABASE_URL`. It's the only code issuing different SQL per dialect — `PRAGMA table_info` vs `information_schema.columns` — so `tests/test_migration_guard.py` runs on both CI database jobs. No background task — everything else is on-demand, driven by requests.
- `_read_icy_now_playing(url)` — opens the stream with `Icy-MetaData: 1`, reads one audio block + its length-prefixed metadata block, parses `StreamTitle='Artist - Title'`. Pure `httpx`, no external API, no audio download/transcode. Always returns a dict, never `None` — empty `artist`/`title` just means no (new) track right now, not an error. **Must pass `follow_redirects=True`** — several stations (e.g. `stream.rcs.revma.com`) 302 to a geo-nearest edge node, and httpx doesn't follow redirects by default (unlike `curl -L`/`urllib`); this bit us once already on first deploy.
- `_parse_stream_format(headers)` — format/bitrate/sample_rate from the same response's headers, independent of whether a title is present. Three real broadcaster inconsistencies found by testing against live streams, not guessed — see the docstring: `icy-br` can be a comma-joined duplicate (`"256, 256"`, breaks bare `int()`); `icy-audio-info`'s internal params are sometimes `ice-bitrate=`/`ice-samplerate=` (laut.fm), sometimes bare `bitrate=`/`samplerate=` (Pure Ibiza); and the *header name itself* varies too — `icy-audio-info` vs `ice-audio-info` (no y). Any one of these uncaught would silently wipe the whole read via the outer try/except, not just the format fields.
- `_read_icy_now_playing_cached(url)` — same, with a 15s in-memory per-URL cache (`META_CACHE_TTL`) so rapid re-polls don't hammer a station's origin
- `ICECAST_STATUS` + `_read_icecast_listeners_cached(station)` — listener counts for the 3 stations with a confirmed Icecast mount match (Pure Ibiza Radio, Ibiza Global Classics, Ibiza Global Radio — all on `control.streaming-pro.com`, different ports). `icestats.source` is a dict when a port has one mount, a list when it has several — normalize before matching by `listenurl`. Sonica deliberately excluded: its port has 4 mounts and none match its stream filename closely enough to be sure which is actually it — better `—` than confidently wrong.
- `/api/stations` — returns the registry as-is.
- `/api/now-playing?station=` — resolves the station name to a URL (404 if unknown), reads its live stream info (cached), returns `{current, history, stream_info}` keyed by `station` in `_station_state` (in-memory, per-station `deque(maxlen=10)`). On a track change, persists a `PlayedTrack` row via the FastAPI-injected session (not a raw `SessionLocal()` — that would bypass the test suite's DB override; see `tests/conftest.py`).
- `/api/track-history` — last 50 played tracks from the DB (any station), persists across restarts
- `/api/spotify/save` — `POST {"artist","title"}`, calls `spotify.save_current_track()`: refresh-token grant -> search -> `PUT /me/library`. 503 not-configured, 404 no-match, 502 upstream error. **No auth of its own** — it writes to whichever account owns the configured refresh token, so the app must not be exposed publicly without authentication in front of it. `PlayedTrack.rating` still exists (old data) but nothing writes to it.
- Shazam fallback flow inside `now_playing()`: if `read["title"]` is empty, call `_shazam_fallback_cached(station, url)` -> `shazam_fallback.recognize_stream(url)` (capture ~10s of audio, fingerprint via vibra, query Shazam). Only reached when ICY genuinely gave nothing — never runs alongside a working ICY read. `SHAZAM_CACHE_TTL = 45` (longer than ICY's 15s — a real recognition costs a ~10-15s round trip, unlike a near-instant header read) caches negative results too, so a station that just doesn't match well isn't retried every poll.
- Cover art: Shazam-sourced tracks already carry `cover_url` from Shazam's own response — no extra call. ICY-sourced tracks don't, so `_cover_art_cached(artist, title)` does a Spotify search (via `spotify.search_track()`, read-only, no save) and caches the result indefinitely per `artist|title` (a song's cover doesn't change; simple 1000-entry size cap so this can't grow forever over a long-running process).
- `/static/*` serves everything in `static/`; `/` returns `static/index.html`

**Database:** `database.py` + `models.py` — async SQLAlchemy.
- `PlayedTrack` stores: title, artist, album, station, started_at, spotify_url, apple_music_url, deezer_url, rating
- **Prod:** PostgreSQL 16 (`postgresql+asyncpg://`), injected via `DATABASE_URL` in docker-compose
- **Local/dev:** SQLite (`sqlite+aiosqlite:///./app.db`), the default when `DATABASE_URL` is not set
- `started_at` is a **naive** `TIMESTAMP` column (no tz stored) — always strip tzinfo before inserting (`datetime.now(timezone.utc).replace(tzinfo=None)`). asyncpg rejects a tz-aware value outright ("can't subtract offset-naive and offset-aware datetimes"); SQLite silently accepts it, which is exactly how this shipped broken against Postgres the first time and wasn't caught until a real deploy.

**Reverse proxy (prod only):** `nginx/nginx.conf`
- gzip enabled for CSS, JS, and JSON responses
- Images (`*.webp`, `*.jpg`, `*.png`): 30-day cache + `immutable` — browsers skip network entirely on repeat visits
- CSS/JS: 1-day cache without `immutable` — updates propagate within a day
- `/api/` proxied to `prod:8000`
- `/` falls back to `index.html`

**Frontend:** `static/index.html` — all JS is inline. (`static/utils.js` holds pure
helper functions mirrored from that inline script; it exists so `tests/js/utils.test.js`
can exercise them in isolation. Editing `utils.js` does not change the running page.)
- `STATIONS` starts empty and is filled by `loadStations()` from `/api/stations` at boot; `buildStationButtons()` renders the picker once it arrives. A fetch failure shows a `.stations-error` message rather than an empty picker that looks like a CSS bug.
- `isHls(station)` (`url.includes(".m3u8")`) gates both playback routing (hls.js vs plain `<audio>`) and whether `/api/now-playing` gets called at all — HLS has no ICY metadata concept
- Hero background uses CSS `image-set()` (WebP first, JPEG fallback); `<link rel="preload">` in `<head>` tells the browser to fetch it before CSS is parsed
- Nav logo uses `<picture>` with a WebP `<source>` and PNG fallback
- `fetchNowPlaying()` polls `/api/now-playing?station=` every 20s for whichever station is active, updates the hero + info panel, and updates `statFormat`/`statBitrate`/`statSampleRate`/`listenerStat`/`navListeners` from `data.stream_info` — for *any* station. Static per-station `format`/`bitrate`/`sampleRate` in `stations.json` are just the initial fallback (shown at selection time, before the first poll lands) — mainly relevant for HLS stations, which never get live `stream_info` back.
- "Add to Spotify" button (`spotifySaveBtn`): `data-state` attribute drives its look — `idle` (disabled until a real title exists) -> `saving` -> `done` (click re-opens `data-spotifyUrl`) or `error` (auto-resets after 4s). Resets on both station change (`resetSpotifySave()`) and track change (new `artist|title` key in `fetchNowPlaying()`), so a stale "Added ✓" doesn't linger onto the next song.
- `#heroTrackInfo` wraps `#heroCover` (album art, hidden if `c.cover_url` is falsy) + `#heroArtist`/`#heroTitle`/`#heroSource`. `heroSource` shows "via Shazam" only when `c.source === "shazam"` — a Shazam-fallback guess from a ~10s audio sample is worth flagging as such, vs. the broadcaster's own ICY announcement.

## Key environment variables

| Variable | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | PostgreSQL password. Required for prod stack (`db` and `prod` services both read it). |
| `DATABASE_URL` | SQLAlchemy async DB URL. Not set locally — defaults to `sqlite+aiosqlite:///./app.db`. Injected by docker-compose for prod: `postgresql+asyncpg://afterhours_fm:<password>@db:5432/afterhours_fm`. |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` / `SPOTIFY_REFRESH_TOKEN` | Spotify app credentials for `/api/spotify/save` and cover-art search. Register an app at developer.spotify.com and obtain a refresh token once via the authorization-code flow with the `user-library-modify` scope. Without these, `spotify.save_current_track()`/`search_track()` raise `SpotifyNotConfigured` and the features degrade quietly. |

No env var needed for Shazam recognition — vibra queries Shazam's endpoint directly, no API key involved (it's an unofficial/reverse-engineered client, same category as the shazamio Python library that was tried and rejected first — its endpoint returned no matches in testing; vibra's did).

## Testing

```bash
make test            # pytest + Vitest
make test-backend    # ./venv/bin/pytest
make test-frontend   # npm test (Vitest, runs from project root)
make security        # npm audit — exits 1 if vulnerabilities found
```

Backend tests use `pytest-asyncio` (auto mode) and `pytest-mock`. Frontend tests use Vitest v4.

Because the route now resolves station names against the real registry, route tests
register their fake stations through the `_register_test_stations` fixture in
`tests/test_routes.py` — `monkeypatch` restores the real `_STATION_URLS` afterwards.

## Stream notes

- **Pure Ibiza Radio:** `http://control.streaming-pro.com:8028/stream.mp3` — Icecast 2.4, 256 kbps MP3, 48 kHz stereo. `icy-metaint` is present but `StreamTitle` is always empty — this is exactly the station the Shazam fallback exists for, not a bug in `_read_icy_now_playing`.
- **Icecast status endpoints:** wired up server-side, not per-station in the frontend — see `ICECAST_STATUS` in `main.py` (3 stations, all on `control.streaming-pro.com` at different ports).
- **`streams.md`** has the full working log of every station tried: which have real per-track `StreamTitle`, which send static/fake data (station name, or a broadcaster-specific JSON blob instead of a title — e.g. Pure Ibiza's own `StreamTitle='NOW ON AIR {"autor":...}'`, filtered out via the `"{" in raw` check in `_read_icy_now_playing`), and which are HLS-only with no ICY concept at all (BBC Radio 4 FM, m2o, m2o Dance, Dub Ninja).
- **Ibiza Pura** (`ibizapura.streaming-pro.com:8000/ibizapura`, listed in `streams.md`'s "Old Stations") is another ICY-empty station but was **never added** to `stations.json` — worth adding if it comes up again, since the Shazam fallback now makes it just as usable as Pure Ibiza Radio.

## Station logos

`stations.json` entries carry an optional `logo` field — a **hotlinked** URL (never downloaded/stored in the repo) shown in the hero title in place of styled text. Sourced from [radio-browser.info](https://www.radio-browser.info/)'s crowd-sourced `favicon` field, which is how most FOSS radio-player apps solve this — some resolve to a station's own official domain (Blue Marlin, Ibiza Global Classics, Antenne Bayern), others to a third-party aggregator's hosted copy (same risk tier as any radio-aggregator app). Copyright reasoning: nominative use (identifying the station, not implying endorsement) in a personal, non-commercial app is low-risk; hotlinking rather than storing a copy keeps it that way. Don't add a `logo` by downloading/re-hosting an image found via a Google Images search or similar — ask first, same as any other file download.

**3 of the 10 checked resolve to dead links** (confirmed via curl, not guessed) — `onerror` on the `<img>` in `selectStation()` catches this and swaps in the plain styled-text span, so nothing breaks, they just silently don't show a logo:
- Sonica — `ibizasonica.com/favicon.ico` 301s to the homepage, not an actual icon file
- Pure Ibiza Radio — the `radio.es`-hosted image 404s (moved/removed since radio-browser.info indexed it)
- Sunshine Live (all 3 sub-brands share this logo) — the Wikimedia Commons thumbnail URL 400s (likely regenerated with a different hash)

If re-checking coverage later, re-run the radio-browser.info search per station name and re-verify each result with a real HTTP request before trusting it — the database is crowd-sourced and drifts.

**Adding a new station:** append an entry to `stations.json`. Both the backend allowlist and the frontend picker read from it, so there's nothing else to update. Set `format`/`bitrate` if known; otherwise the stats panel shows `—` until the first live poll. Check `streams.md` first in case the station's already been tried and found to send fake/static metadata.

## Style guide

The palette lives in `:root` at the top of `static/style.css` — that file is the only
source for it. (A `Style_Guide.txt` describing an unrelated "Radio Calico" brand used to
sit in the repo; its mint/forest-green palette never matched this app's CSS and it was
removed rather than published as if it applied here.)

| CSS variable | Hex | Role |
|---|---|---|
| `--azure` | `#0B7BB5` | Section headings, stream link borders |
| `--turquoise` | `#2BBFB0` | Active states, live indicator, links |
| `--sand` | `#E8D5A3` | Track artist label in hero |
| `--deep` | `#0A2840` | Nav, footer, player bar backgrounds |
| `--charcoal` | `#1C1C1E` | Body text |
