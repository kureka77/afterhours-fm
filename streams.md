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
  own station name forever, some send an empty string.
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
| Sunshine Live (live, techno, house) | German dance radio, 192 kbps MP3 |
| Antenne (Chillout, Dance XXL, Lounge, Bayern) | Bavarian network |
| Radio Panama | AAC+ |

### ICY present but empty or fake — Shazam fallback territory

| Station | What it actually sends |
|---|---|
| Pure Ibiza Radio | `icy-metaint` present, `StreamTitle` always empty. Occasionally a JSON blob: `StreamTitle='NOW ON AIR {"autor":...}'` — filtered by the `"{" in raw` check in `_read_icy_now_playing` |
| Deep Vibes | Bare IP host, no usable title |
| Sonica | Port has 4 mounts (`AutoDj.mp3`, `ibizaglobalclassics.mp3`, `livemain.mp3`, `radiojar`) and none clearly maps to Sonica — deliberately excluded from `ICECAST_STATUS` rather than guess a listener count |

### HLS — no ICY concept at all

`.m3u8` streams have no in-band metadata mechanism. These play via hls.js and report no
track; `isHls()` skips the now-playing call entirely for them.

- BBC Radio 4 FM
- m2o, m2o Dance
- Dub Ninja

### Tried and not included

| Stream | Why |
|---|---|
| `ibizapura.streaming-pro.com:8000/ibizapura` | ICY-empty. Was dropped before the Shazam fallback existed — worth re-adding now, since the fallback makes it as usable as Pure Ibiza Radio |
| `51.222.8.101:8000/stream` (salsa panama) | Bare-IP host, unreliable; superseded by Radio Panama's hostname-based stream |
| `mp3channels.webradio.antenne.de/chillout` | Older Antenne endpoint, superseded by `stream.antenne.de/chillout/stream/mp3` |

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
