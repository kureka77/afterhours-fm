# Afterhours FM

**[Project page →](https://kureka77.github.io/afterhours-fm/)**

A self-hosted web radio player for 25 Ibiza/house and European dance stations, with
live track identification, album art, persistent play history, and one-click saving
to Spotify.

Track names come from each station's own broadcast metadata where it exists, and from
audio fingerprinting where it doesn't — no paid recognition API in either path.

![The Afterhours FM player: a full-bleed hero photo with the station name, the currently
playing track with album art and a "via Shazam" tag, a play/pause bar, and a grid of 25
station buttons.](docs/shot-player.jpg)

<p align="center"><em>A live Shazam match — the "via Shazam" tag marks a track identified by
fingerprinting rather than announced by the broadcaster.</em></p>

## How track identification works

Two sources, tried cheapest-first:

1. **ICY in-band metadata.** Icecast/SHOUTcast streams interleave a small text block
   into the audio every `icy-metaint` bytes, carrying `StreamTitle='Artist - Title'`.
   The backend opens the stream with `Icy-MetaData: 1`, reads exactly one metadata
   cycle, and closes. Near-instant, free, and it's the broadcaster's own announcement
   rather than a guess.
2. **Shazam fingerprinting**, only when step 1 returns nothing usable. Some stations
   send an empty `StreamTitle`, their own station name, or a JSON blob instead of a
   track. For those, the backend captures ~10s of live audio and identifies it via
   [vibra](https://github.com/BayernMuller/vibra), a C++ client for Shazam's
   fingerprint endpoint. A round trip costs 10–15s, so results are cached for 45s per
   station — including negative results, so a station that simply doesn't match well
   isn't retried on every poll.

Anything from source 2 is labelled **"via Shazam"** in the UI. A fingerprint match
from a 10-second sample is a guess; the broadcaster's own metadata isn't, and the
distinction is worth showing.

HLS stations (`.m3u8`) have no ICY metadata concept at all, so they play but report no
track.

## Features

- **25 stations** — Ibiza (Pure Ibiza, Blue Marlin, Ibiza Global, Sonica, Ibiza 1),
  German dance radio (Sunshine Live, Antenne), lounge/latin (Milano Lounge,
  Ondalatina), plus HLS reference stations (BBC Radio 4 FM, m2o)
- **Live stream stats** — format, bitrate and sample rate read from each stream's own
  response headers; listener counts for the three stations with a confirmed Icecast
  mount
- **Album art** — Shazam matches bring their own; ICY-sourced tracks get art via a
  Spotify search, cached indefinitely per track
- **Add to Spotify** — saves the current track to your Liked Songs in one click
- **Persistent history** — every identified track is written to PostgreSQL (prod) or
  SQLite (local), tagged with its station, and survives restarts
- **Keyboard control** — <kbd>Space</kbd> toggles play/pause

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn |
| Database | PostgreSQL 16 (prod) / SQLite (local), async SQLAlchemy |
| Reverse proxy | nginx — serves static files, proxies `/api/` |
| Recognition | ICY in-band metadata (httpx) → vibra/Shazam fallback |
| Frontend | Vanilla HTML/CSS/JS, no framework or build step |
| HLS playback | hls.js (CDN) |
| Tests | pytest + pytest-asyncio, Vitest |

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
        Shazam["Shazam endpoint<br/>via vibra"]
        SpotifyAPI["Spotify Web API"]
        CDN["jsDelivr CDN<br/>hls.js"]
    end

    Browser -->|"GET / and /static/*"| NGINX
    NGINX -->|"serve from disk"| Disk
    Browser -->|"GET /api/stations"| NGINX
    Browser -->|"GET /api/now-playing?station="| NGINX
    NGINX -->|"proxy"| FastAPI
    FastAPI <-->|"asyncpg"| PG
    FastAPI -->|"Icy-MetaData:1"| Stations
    FastAPI -.->|"only if ICY empty"| Shazam
    FastAPI -.->|"cover art · save track"| SpotifyAPI
    Browser -->|"MP3 / HLS audio"| Stations
    Browser -.->|"hls.js deferred"| CDN
```

Dashed arrows are conditional or deferred. There is no background polling task —
a stream read happens only when the browser asks, and is cached for 15s per station.

## Quick start

Requires Docker, or Python 3.12 with `ffmpeg` and `cmake` available (vibra compiles a
C++ extension at install time).

```bash
git clone https://github.com/kureka77/afterhours-fm.git
cd afterhours-fm
cp .env.example .env     # optional — only needed for the Spotify features
make dev                 # http://localhost:8000
```

Playback, track identification and history all work with no configuration. The Spotify
variables are optional and only affect the "Add to Spotify" button and cover art for
ICY-sourced tracks; without them, those degrade quietly rather than erroring.

**Prod stack** — PostgreSQL + nginx on port 80:

```bash
make prod                # requires POSTGRES_PASSWORD in .env
```

**Without Docker:**

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

## Configuration

All configuration is environment variables — see `.env.example`. Copy it to `.env`
and it is picked up automatically: `config.py` loads it for local runs, and
docker-compose passes it into the containers. Variables already set in the real
environment win over the file.

| Variable | Required | Purpose |
|---|---|---|
| `POSTGRES_PASSWORD` | prod only | Password for the bundled PostgreSQL container |
| `DATABASE_URL` | no | SQLAlchemy async URL. Defaults to `sqlite+aiosqlite:///./app.db`; set by docker-compose in prod |
| `SPOTIFY_CLIENT_ID` | no | From a Spotify app at [developer.spotify.com](https://developer.spotify.com/dashboard) |
| `SPOTIFY_CLIENT_SECRET` | no | As above |
| `SPOTIFY_REFRESH_TOKEN` | no | Obtained once via the authorization-code flow with the `user-library-modify` scope |

Shazam recognition needs no key — vibra talks to Shazam's endpoint directly.

## Security notes

This is a **single-user, self-hosted** app, and a few things follow from that. If you
put it on a public address, read this first.

- **`/api/spotify/save` has no authentication.** It writes to whichever account owns
  the configured refresh token. On localhost or a private network that's fine; exposed
  to the internet, anyone who finds it can add tracks to your library. Put
  authentication in front of the app before exposing it.
- **Stream URLs are resolved server-side.** `/api/now-playing` takes a station *name*
  and looks the URL up in `stations.json`. It deliberately does not accept a URL from
  the client — that would be an SSRF hole, letting a caller point the backend at
  `http://169.254.169.254/` (cloud instance metadata) or any other internal address the
  server can reach but they can't.
- **Secrets are environment-only.** Nothing reads a credential from source, and `.env`
  is git-ignored. `.env.example` documents the variables with no real values.
- **Station logos are hotlinked**, never re-hosted — see `CLAUDE.md` for the reasoning.
- **The CDN script is pinned with subresource integrity.** hls.js carries an `integrity`
  hash, so a compromised CDN cannot silently serve different JavaScript.
- **Two stations are still plain `http://`** — Ibiza Global Radio and Deep Vibes publish no
  HTTPS endpoint. Their stream and metadata therefore travel in clear text. Nothing
  sensitive moves over them, but it does mean the app can't be served from an HTTPS-only
  origin without those two breaking on mixed-content rules.

### Automated checks

Every push and pull request runs, alongside the tests:

| Check | Tool | What it covers |
|---|---|---|
| SAST | CodeQL (`security-and-quality`) | Python + JS, results in the Security tab, plus a weekly re-scan |
| Python SAST | Bandit | hardcoded credentials, injection, weak crypto |
| Python deps | pip-audit | published CVEs in `requirements*.txt` |
| JS deps | npm audit | published CVEs |
| Secrets | gitleaks | credentials anywhere in commit *history*, not just at the tip |
| Base image | Trivy | Debian CVEs in `python:3.14-slim`, which no other scanner here sees |
| Quality gate | SonarQube Cloud | bugs, smells, duplication and coverage, blocking on new code |
| Updates | Dependabot | weekly pip/npm, monthly actions + docker |

Every one of them runs on each pull request. The Trivy and SonarQube jobs are the
only two that can be red for reasons outside the diff, so both are scoped: Trivy
to fixable OS-package CVEs, SonarQube to *new* code.

### SonarQube

Analysis runs against [SonarQube Cloud](https://sonarcloud.io/dashboard?id=kureka77_afterhours-fm)
from `.github/workflows/sonar.yml`, and blocks on the quality gate rather than
uploading and passing regardless.

The job skips itself when `SONAR_TOKEN` is absent, so a fork never goes red for
want of a credential it cannot have. To point it at your own organisation, set
`sonar.organization` and `sonar.projectKey` in `sonar-project.properties` (Cloud
generates the key as `<org>_<repo>`), add `SONAR_TOKEN` as a repository secret,
and turn **Automatic Analysis off** for the project — Cloud refuses a CI scan
while its own scanning is enabled. Leave `SONAR_HOST_URL` unset for Cloud; set it
as a repository variable only for a self-hosted server.

Current state on `main`: **0 bugs, 0 vulnerabilities, 0 security hotspots**,
87.5% coverage, quality gate OK.

## Adding a station

Append an entry to `stations.json` — it's the single source of truth, read by the
backend as an allowlist and served to the frontend via `/api/stations`:

```json
{ "name": "My Station", "url": "https://example.com/stream.mp3", "format": "MP3" }
```

`format`, `bitrate`, `sampleRate` and `logo` are optional. Live values from the
stream's own headers overwrite the static ones on the first poll; they're mainly
useful for HLS stations, which never report live stats. Check `streams.md` first —
it's a log of every station tried, including ones that send fake or static metadata.

## Project structure

```
afterhours-fm/
├── main.py              # FastAPI app: routes, ICY reader, caching, schema guard
├── stations.json        # Station registry — single source of truth + SSRF allowlist
├── spotify.py           # Spotify search + save-to-Liked-Songs
├── shazam_fallback.py   # Shazam-via-vibra recognition (fallback only)
├── config.py            # Loads .env — imported by database.py and spotify.py
├── database.py          # Async SQLAlchemy engine and session factory
├── models.py            # PlayedTrack ORM model
├── nginx/nginx.conf     # Static file serving, gzip, cache headers, /api/ proxy
├── static/
│   ├── index.html       # Page shell + all client-side JS (inline)
│   ├── style.css        # All styles
│   ├── utils.js         # Pure helpers, covered by Vitest
│   ├── hero.{jpg,webp}  # Hero background
│   └── logo.{png,webp}  # Nav logo
├── tests/               # pytest (backend) + Vitest (frontend)
├── streams.md           # Working log of every station tried and what it sends
├── Dockerfile           # Multi-stage: base → dev or prod
├── docker-compose.yml   # dev, db, prod, nginx services
└── Makefile             # make help
```

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/stations` | The station registry the frontend builds its picker from |
| `GET` | `/api/now-playing?station=` | Current track + recent history + live stream stats for one station. 404 on an unknown station name. Persists to the DB on a track change |
| `GET` | `/api/track-history` | Last 50 tracks across all stations |
| `POST` | `/api/spotify/save` | `{"artist","title"}` → saves the best Spotify match to Liked Songs. 503 not configured, 404 no match, 502 upstream error |

## Development

```bash
make test            # both suites
make test-backend    # pytest
make test-frontend   # Vitest
make security        # npm audit
make db-shell        # psql into the running prod database
make help            # all targets
```

Backend tests use `pytest-asyncio` in auto mode, against in-memory SQLite by default.
Point them at a real PostgreSQL with:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/db pytest
```

CI runs the suite **both ways** — `Backend tests (pytest · SQLite)` and
`Backend tests (pytest · PostgreSQL)`. That's not redundancy. Prod runs Postgres while
dev runs SQLite, and the engines disagree: `started_at` is a naive `TIMESTAMP`, and
asyncpg rejects a timezone-aware datetime outright where SQLite silently accepts one.
That gap shipped a bug to production once, because every local check passed. Running
both engines is what closes the dev/prod parity gap rather than just documenting it.

Two things follow from testing against Postgres, both handled in `tests/conftest.py`:

- `test_engine` is **function-scoped**. An asyncpg connection is bound to the event
  loop that opened it, and pytest-asyncio gives each test a fresh loop — a
  session-scoped engine hands test two a pool from test one's dead loop. SQLite hides
  this because aiosqlite proxies to a worker thread.
- Row ids climb across the session rather than restarting at 1, since Postgres keeps
  the sequence running through a `DELETE`. Nothing asserts on id values.

There is no Alembic. `_ensure_station_column()` in `main.py` is a hand-rolled,
idempotent startup guard that adds the `station` column if missing and backfills old
rows. It takes an optional engine argument purely so the tests can point it at a test
database — it runs from the FastAPI lifespan, which httpx's `ASGITransport` never
fires, so it would otherwise be unreachable from the suite. It is also the only code
here that issues genuinely different SQL per dialect (`PRAGMA table_info` on SQLite,
`information_schema.columns` on Postgres), which is why
`tests/test_migration_guard.py` matters on both CI database jobs rather than one.

## License

MIT — see [LICENSE](LICENSE).

Station logos and audio streams belong to their respective broadcasters. This project
links to publicly-advertised stream endpoints; it does not host, rebroadcast, or
redistribute any audio.
