# Stream notes

A working log of what each station actually sends, found by connecting to them rather
than by reading documentation. Useful before adding a station: several advertise ICY
metadata and then send nothing usable.

The station list itself lives in `stations.json`.

## What "ICY metadata" means here

Icecast/SHOUTcast servers interleave a metadata block into the audio stream every
`icy-metaint` bytes when the client sends `Icy-MetaData: 1`. The block carries
`StreamTitle='Artist - Title'`. Three things vary in practice:

- **Whether the title is real.** Some stations send the current track, some send their
  own station name forever, some send an empty string. The station-name case is the
  nastiest of the three: it looks like data, so it needs an actual check to reject
  (see "Station name as the title" below) — an empty string at least fails obviously.
- **Header naming.** `icy-audio-info` vs `ice-audio-info` (no *y*), and internally
  `bitrate=`/`samplerate=` vs `ice-bitrate=`/`ice-samplerate=`.
- **Header duplication.** `icy-br` sometimes arrives twice and httpx merges it to
  `"256, 256"`, which a bare `int()` chokes on.

All three are handled in `_parse_stream_format()` in `main.py`.

## Categories

### Real per-track `StreamTitle`

These work with the cheap ICY path alone — no fingerprinting needed.

| Station | Notes |
|---|---|
| Ibiza Global Radio | Also exposes an Icecast mount for listener counts |
| Ibiza Global Classics | Same server, different port |
| Blue Marlin | radiojar-hosted |
| Ibiza 1 (Radio, Afrohouse, Anthems, Poolside, Tech House) | All on `stream.rcs.revma.com`. **302 to a geo-nearest edge node** — httpx needs `follow_redirects=True` or you read the redirect instead of the stream |
| Milano Lounge, Ondalatina | laut.fm — uses the `ice-` prefixed header variant |
| Antenne (Chillout, Dance XXL, Lounge, Bayern) | Bavarian network |
| Radio Panama | AAC+ |

### Station name as the title — the Sunshine Live case

All three Sunshine Live streams send `icy-metaint: 8192` and then a `StreamTitle`
that is permanently the station's own name, identical to their `icy-name` header:

| Stream | `icy-name` and `StreamTitle`, both |
|---|---|
| `live/mp3-192` | `SUNSHINE LIVE - Simulcast` |
| `techno/mp3-192/` | `SUNSHINE LIVE - Techno` |
| `house/mp3-192` | `SUNSHINE LIVE - House` |

This was worse than sending nothing. The `" - "` split in `_read_icy_now_playing`
turned it into artist `SUNSHINE LIVE` / title `Techno`, which reads as a real track,
got written to `played_tracks`, and — being a non-empty title — suppressed the
fallbacks in `now_playing()` that would have found the actual song.

`_read_icy_now_playing` now drops a `StreamTitle` equal to that stream's own
`icy-name`. Matching against the station's own header rather than a hardcoded list
of bad titles needs no per-station upkeep. Verified against every station in
`stations.json`: these three are the only ones where the two headers are equal, so
no working station changed behaviour.

Tracks for these come from `sunshine_playlist.py` instead — see below.

### ICY present but empty or fake — Shazam fallback territory

| Station | What it actually sends |
|---|---|
| Sunshine Live (main simulcast) | Station name only, and its playlist feed carries shows rather than tracks — Shazam is the only source of a track name |
| Pure Ibiza Radio | `icy-metaint` present, `StreamTitle` always empty. Occasionally a JSON blob: `StreamTitle='NOW ON AIR {"autor":...}'` — filtered by the `"{" in raw` check in `_read_icy_now_playing` |
| Deep Vibes | Bare IP host, no usable title |
| Sonica | Port has 4 mounts (`AutoDj.mp3`, `ibizaglobalclassics.mp3`, `livemain.mp3`, `radiojar`) and none clearly maps to Sonica — deliberately excluded from `ICECAST_STATUS` rather than guess a listener count |

### HLS — no ICY concept, but Shazam works via segment capture

`.m3u8` streams have no in-band metadata mechanism, and these carry no out-of-band tags
either — the playlists checked contain only `#EXTINF` and `#EXT-X-PROGRAM-DATE-TIME`, no
`#EXT-X-DATERANGE` or timed-ID3 title. So there is nothing to *read*; a track name can
only come from the audio.

They used to show a permanent "Now playing live" placeholder because `isHls()` skipped the
now-playing call outright. They now go straight to Shazam (`shazam_fallback` resolves the
playlist to segment URLs, concatenates the newest ones and fingerprints them), which is
their only possible source.

| Stream | Result |
|---|---|
| m2o | Real tracks. Verified live: `Quevedo & Elvis Crespo - LA GRACIOSA`, `Topic & A7S - Why Do You Lie to Me` |
| m2o Dance | Real tracks. Verified live: `Unit 2 - Sunshine (Kink Remix)`, `SolyMar & Megamen - All I Need`, `Panos Pissitelis & Junior Mi - Freaky` |
| Dub Ninja | Real tracks. Verified live: `Adam Ten & Volkoder - Got Me Crazy` |
| BBC Radio 4 FM | No match, and correctly so — it's speech radio. Falls back to the generic live placeholder |

Two things found the hard way here:

- **m2o publishes no per-track feed**, so there was no cheaper source to prefer over
  Shazam (unlike Sunshine Live). `m2o.it/playlist/` exists but renders no track list, and
  the site's "Ora in onda" widget shows the *show/DJ* (e.g. "Vittoria Hyde") — the same
  shape of trap as Sunshine's channel 3. Verified by loading the page and watching its
  network calls; no now-playing API is requested.
- **m2o's CDN drops connections intermittently** — `httpx.RemoteProtocolError: Server
  disconnected without sending a response`, hit twice in ~10 minutes of testing. A
  mid-capture segment failure therefore keeps the partial clip instead of losing the whole
  attempt; one 10s segment already recognises reliably (confirmed against all three music
  stations at both 1 and 2 segments).

### Tried and not included

| Stream | Why |
|---|---|
| `ibizapura.streaming-pro.com:8000/ibizapura` | ICY-empty. Was dropped before the Shazam fallback existed — worth re-adding now, since the fallback makes it as usable as Pure Ibiza Radio |
| `51.222.8.101:8000/stream` (salsa panama) | Bare-IP host, unreliable; superseded by Radio Panama's hostname-based stream |
| `mp3channels.webradio.antenne.de/chillout` | Older Antenne endpoint, superseded by `stream.antenne.de/chillout/stream/mp3` |

## Broadcaster playlist feeds

Some stations publish what they're playing on their own website. That beats both
ICY (which they may not populate) and Shazam (a guess from ~10s of audio), and it
usually carries cover art. `sunshine_playlist.py` implements this for Sunshine Live;
`now_playing()` tries ICY -> playlist -> Shazam.

**Endpoint:** `https://iris-sunshinelive.loverad.io/search.json?station=<id>&start=<t>&end=<t>`
— found by loading <https://www.sunshine-live.de/programm/playlist> and watching its
network calls. Undocumented, so treat everything here as observed behaviour, not a
contract.

Three things it does that will bite:

- **It ignores the UTC offset you send.** Only the naive wall-clock digits are
  matched, against Europe/Berlin. Sending a correct `+00:00` UTC timestamp returns
  tracks from two hours ago rather than an error — a wrong-but-plausible result that
  would ship unnoticed. Always build the window in Berlin local time.
- **It returns near-duplicate rows** for the same track, logged a second apart
  (seen on `Clotur & Vault Records - Arkadia`). Take the newest airtime rather than
  treating each row as a play.
- **Not every channel is a track list.** Channel 3 (the main SUNSHINE LIVE
  simulcast) returns the *show* schedule: one entry an hour, ~8s duration, carrying
  the programme and its host (`artist="Clapcast"`, `title="Claptone"`). Mapping it
  would publish show names as songs, so it is deliberately left out of
  `sunshine_playlist.CHANNELS`.

Channel ids come from the `<select>` on the playlist page (`value` is the id). The
two mapped here were confirmed by Shazam-ing the live stream and checking the
recognised track matched what the API reported for the same moment — not by assuming
`techno/mp3-192` is the channel named TECHNO:

| Station | Channel id |
|---|---|
| Sunshine Live Techno | 9 |
| Sunshine Live House | 4 |

Other ids seen on that page, if more channels are ever added: CLASSICS 6, HARDSTYLE
88, NATURE ONE 15, MELODIC BEATS 83, BOUNCE 101, SCHRANZ 98, EURODANCE 71, CHILLOUT
22, LOUNGE 10, HARDTECHNO 90, HARDCORE 91, DRUM & BASS 16, TRANCE 7, EDM 5,
AFRO HOUSE 95, IBIZA 35, MIX MISSION 34, WORKOUT 32, 90s 11, 2000s 52, 2010s 94.

## Icecast listener counts

Three stations expose a `status-json.xsl` endpoint whose mount could be matched to the
stream with confidence — see `ICECAST_STATUS` in `main.py`. One gotcha:
`icestats.source` is a **dict** when a port serves exactly one mount and a **list** when
it serves several, so it has to be normalised before matching on `listenurl`.

## Adding a station

Append to `stations.json`, then check it end to end:

```bash
curl -s -H "Icy-MetaData: 1" -D - -o /dev/null --max-time 5 "<stream-url>"
```

Look for `icy-metaint` in the response headers. If it's absent, the station will still
play and still report format/bitrate, but track identification falls to Shazam.
