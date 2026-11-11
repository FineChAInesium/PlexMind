# Architecture

One container runs four processes under `supervisor.py`, which restarts any
that exit. Only the API listens beyond the container; the other three talk to
each other on 127.0.0.1 with tokens generated at each start.

```text
Browser dashboard
  -> FastAPI app (:8000, uid 1000)
      -> Plex API: users, history, Watchlist, playlists
      -> plex.tv: Sign in with Plex
      -> durable recommendation queue and live progress (SSE)
      -> scripts controller for media jobs (127.0.0.1:9010)
      -> Docker broker for sidecar state and GPU readings (127.0.0.1:9020)

Recommendation worker (uid 1000)
  -> claims queued jobs from /app/data
  -> llama.cpp for recommendations
  -> Plex API: one sync per user

Scripts controller (root)
  -> transcription, translation and maintenance scripts on /media/movies and /media/tv
  -> one shared lock so only one GPU job runs at a time

Docker broker (root)
  -> the only process that uses the Docker socket
  -> only start, stop and inspect of the named llama.cpp and Whisper containers,
     plus a fixed nvidia-smi query

AI sidecars
  -> llama.cpp (:8080 on the Docker network, 127.0.0.1:11435 on the host)
  -> Whisper ASR (:9000 on the Docker network, 127.0.0.1:9001 on the host)

Data
  -> /app/data: queue, history, feedback, logs, quarantine,
     plex_auth.json (Plex sign-in), secrets.json (generated keys, root only)
```

## Where picks go in Plex

| Account | Destination |
|---|---|
| Server owner | Plex Watchlist |
| Everyone else | `PlexMind Movies` and `PlexMind TV Pilots` playlists |

TV picks are pilot episodes, so people can sample a show without a whole season
landing in their lists. A user's picks are kept while they are under
`REC_REFRESH_DAYS` (25) old and untouched; older ones are replaced.

## Scheduling

- Recommendations: monthly, on the 1st at 03:00 UTC (editable on the dashboard).
- Transcription: daily window 05:00-12:00. A running translation pauses for it.
- Translation: nightly window 23:00-03:00. A run keeps going through its backlog
  after the window closes unless `MAX_RUNTIME_MINUTES` caps it.

Every job holds while a throttle sensor (GPU, CPU, NVMe or disk temperature, RAM)
is at its limit; set limits on the dashboard's **Throttle** page.
