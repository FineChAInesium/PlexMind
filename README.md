# PlexMind Suite

**Local AI recommendations, subtitle backfill, translation, and media-library cleanup for Plex.**

[![Live Demo](https://img.shields.io/badge/demo-interactive-blue?style=for-the-badge)](https://finechainesium.github.io/PlexMind/)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-dashboard-green.svg)](https://fastapi.tiangolo.com/)
[![Ollama](https://img.shields.io/badge/Ollama-local_LLM-orange.svg)](https://ollama.com/)
[![Docker](https://img.shields.io/badge/Docker-Unraid_ready-2496ED.svg)](https://www.docker.com/)

PlexMind turns your Plex history into usable, explainable recommendations without sending your viewing data to a cloud recommender. It runs a local FastAPI app, talks to Ollama for taste-aware picks, syncs those picks back into Plex, and gives you a dashboard for the parts you actually need day to day: health, users, schedules, GPU load, storage, logs, and batch progress.

The suite also includes Whisper transcription, Ollama-powered subtitle translation, and maintenance scripts for subtitle audits, duplicate cleanup, encoding repair, and PGS cleanup.

**Try the demo:** https://finechainesium.github.io/PlexMind/

The demo uses mock data in the browser. A real install connects to your Plex server, Ollama, and media folders.

---

## What It Does

| Area | What PlexMind Handles |
|---|---|
| Recommendations | Reads Plex watch history, asks a local LLM for taste-aware picks, and explains why each title fits. |
| Plex sync | Admin recommendations go to Watchlist; managed users get `PlexMind Movies` and `PlexMind TV Pilots` playlists. |
| Batch runs | `/api/run-all` runs in the background and streams live SSE progress to the dashboard. |
| Scheduling | Monthly recommendation runs, editable from the dashboard. |
| GPU awareness | Checks NVIDIA, Intel Arc, then AMD utilization before heavy batch work. |
| Subtitles | Whisper ASR creates missing SRTs; Ollama translates existing SRTs into target languages. |
| Maintenance | Audits the library, removes duplicate subtitles, fixes SRT encoding, and deletes PGS/image subs when SRTs exist. |
| Dashboard | FastAPI serves the UI directly with compiled Tailwind CSS. No CDN Tailwind runtime. |

## Dashboard

![Dashboard](docs/images/dashboard.png)

Open `http://<server>:8000` after install.

The dashboard includes:

- API and LLM health cards
- GPU vendor/utilization and busy threshold
- disk usage for the data volume
- next recommendation run and editable schedule
- user table with per-user generation
- live all-user batch progress with per-user status
- transcribe/translate log download buttons
- maintenance command helpers
- API URL and API key settings stored in browser localStorage

The production UI is built from `plexmind/app/static/css/input.css` into `plexmind/app/static/css/styles.css` with Tailwind CLI:

```bash
cd plexmind
npm install
npm run build:css
```

## Where Picks Show Up in Plex

| Plex Account | Destination |
|---|---|
| Server admin | Plex Watchlist |
| Managed users | `PlexMind Movies` playlist |
| Managed users | `PlexMind TV Pilots` playlist |

TV recommendations are pilot episodes, so users can sample a show without adding a full season.

## Quick Start: Unraid

Community Applications submission is still pending. Manual template install:

```text
https://raw.githubusercontent.com/FineChAInesium/PlexMind/main/templates/PlexMind.xml
```

1. Open Community Applications.
2. Click the template URL/folder option.
3. Paste the template URL above.
4. Set `PLEX_URL`, `PLEX_TOKEN`, and `OLLAMA_URL`.
5. Set `PLEXMIND_API_KEY` before exposing the dashboard outside a trusted LAN.
6. Start the container and open `http://[unraid-ip]:8000`.

For NVIDIA GPU access, the template includes `--gpus all`. Intel/AMD utilization fallback works if the relevant CLI tools are available inside the container.

## Quick Start: Docker Compose

```bash
git clone https://github.com/FineChAInesium/PlexMind.git
cd PlexMind
cp .env.example .env
```

Edit `.env`:

```bash
PLEX_URL=http://192.168.1.10:32400
PLEX_TOKEN=your_plex_token
PLEXMIND_API_KEY=$(openssl rand -hex 32)
MOVIES_DIR=/mnt/media/Movies
TV_DIR=/mnt/media/TV
```

Start it:

```bash
./setup.sh
```

Or use Compose directly:

```bash
docker compose up -d --build
```

Open `http://localhost:8000`.

## Avoiding Rebuilds During Development

For production, rebuild the image when app code changes:

```bash
docker compose build plexmind
docker compose up -d plexmind
```

For local iteration, bind-mount the source and run uvicorn with reload:

```yaml
services:
  plexmind:
    volumes:
      - ./plexmind/app:/app/app
      - ./data:/app/data
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

That makes Python, HTML, and compiled CSS changes visible without rebuilding. You still need `npm run build:css` after Tailwind class changes unless you run `npm run watch:css`.

## API Reference

Interactive docs: `http://<server>:8000/docs`

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | API and LLM readiness. |
| `/api/users` | GET | Plex users visible to the configured token. |
| `/api/users/{id}/history` | GET | Deduplicated watch history. |
| `/api/users/{id}/recommendations?force=true` | GET | Generate or fetch recommendations. |
| `/api/users/{id}/feedback` | GET/POST | Read or record like/dislike/watched feedback. |
| `/api/users/{id}/sync` | POST/DELETE | Sync or remove PlexMind Plex destinations. |
| `/api/run-all` | POST | Start all-user batch job. |
| `/api/jobs/{job_id}/status` | GET | Poll batch status. |
| `/api/jobs/{job_id}/events` | GET | SSE stream for live batch progress. |
| `/api/scheduler/status` | GET | Next run, cron values, GPU vendor/utilization, busy threshold. |
| `/api/scheduler/configure` | POST | Update monthly recommendation schedule. |
| `/api/storage` | GET | Disk usage for the data volume. |
| `/api/trending` | GET | TMDB trending data when configured. |
| `/webhook` | POST | Plex webhook handler for cache invalidation. |

Example:

```bash
curl -H "X-API-Key: $PLEXMIND_API_KEY" \
  http://192.168.1.10:8000/api/scheduler/status

curl -X POST -H "X-API-Key: $PLEXMIND_API_KEY" \
  http://192.168.1.10:8000/api/run-all
```

## Configuration

| Variable | Description | Default |
|---|---|---|
| `PLEX_URL` | Plex server URL. Use a LAN address from inside Docker. | `http://host.docker.internal:32400` |
| `PLEX_TOKEN` | Plex admin token. | required |
| `PLEXMIND_API_KEY` | Protects non-health endpoints. Strongly recommended. | unset |
| `CORS_ORIGINS` | Comma-separated browser origins. Use your HTTPS origin behind a proxy. | `*` |
| `TMDB_API_KEY` | Optional metadata enrichment. | unset |
| `TVDB_API_KEY` | Optional TV metadata fallback. | unset |
| `OMDB_API_KEY` | Optional IMDb/OMDB enrichment. | unset |
| `OLLAMA_URL` | Ollama API URL. | `http://ollama:11434` |
| `OLLAMA_MODEL` | Recommendation/translation model. | `gemma3:12b` in Compose |
| `MAX_RECOMMENDATIONS` | Picks per user. | `10` |
| `CANDIDATE_POOL_SIZE` | Prefiltered candidate count before LLM. | `40` |
| `MIN_HISTORY_ITEMS` | Minimum watch history before batch generation. | `3` |
| `SUPPRESSION_DAYS` | Days before a shown recommendation can return. | `60` |
| `GPU_THRESHOLD_PCT` | Pause batch work at or above this utilization. | `30` |
| `GPU_BACKOFF_MINUTES` | Wait time before checking a busy GPU again. | `30` |
| `PLEXMIND_NO_GUI` | Disable dashboard and serve API only. | `false` |
| `WHISPER_MODEL` | Whisper ASR model for scripts. | `turbo` |
| `TARGET_LANGUAGES` | Comma-separated subtitle translation targets. | `zh,es-MX` |

## Security Notes

PlexMind is designed for trusted home networks, but it still has real write access to Plex destinations and subtitle folders.

Minimum hardening:

```bash
PLEXMIND_API_KEY=$(openssl rand -hex 32)
CORS_ORIGINS=https://plexmind.example.com
chmod 600 .env
```

Important details:

- No endpoint returns `.env`, Plex tokens, API keys, or app settings.
- API key comparison uses `secrets.compare_digest`.
- `/api/run-all`, recommendation generation, and webhooks are rate-limited.
- `/webhook` rejects non-LAN clients, but reverse proxies can make internet traffic appear local. Use `PLEXMIND_API_KEY` if proxied.
- The scripts container no longer mounts `/var/run/docker.sock`; it does not need Docker host control for transcription or maintenance.
- Subtitle maintenance modes can delete `.sup`, `.sub/.idx`, and duplicate `.srt` files from mounted media folders. Run audits first and keep backups if your media library is not disposable.
- The dashboard stores its API key in browser localStorage. Use HTTPS when accessing it through a reverse proxy.

See [SECURITY.md](SECURITY.md) for the full security model and current audit notes.

## CLI Helpers

```bash
# Transcribe missing subtitles during the configured window, default 05:00-12:00 local
docker exec plexmind-scripts /app/transcribe.sh

# Run transcription immediately, bypassing the window, with a 60-minute cap
docker exec -e RUN_NOW=1 -e MAX_RUNTIME_MINUTES=60 plexmind-scripts /app/transcribe.sh

# Stop an active transcription run
docker exec plexmind-scripts /app/stop-job.sh transcribe

# Translate SRTs to TARGET_LANGUAGES during the configured window, default 23:00-03:00 local
docker exec plexmind-scripts /app/translate.sh

# Run translation immediately, bypassing the window, with a 60-minute cap
docker exec -e RUN_NOW=1 -e MAX_RUNTIME_MINUTES=60 plexmind-scripts /app/translate.sh

# Stop an active translation run
docker exec plexmind-scripts /app/stop-job.sh translate

# Maintenance
docker exec plexmind-scripts /app/maintenance.sh audit
docker exec plexmind-scripts /app/maintenance.sh dedup
docker exec plexmind-scripts /app/maintenance.sh pgs-cleanup
docker exec plexmind-scripts /app/maintenance.sh all
```

Script logs are written as dated files under `/app/data/logs` and retained for `LOG_RETENTION_DAYS`, default `7`. The plain `/app/data/transcription.log`, `/app/data/translation.log`, and maintenance log paths point at the current day for compatibility.

The dashboard schedule cards are cron helpers. They show the suggested cron line, time window, max runtime, and runtime estimate; add the command to Unraid or your host crontab to execute it. `MAX_RUNTIME_MINUTES` stops the script cleanly between files; `RUN_NOW=1` bypasses the start/end window for manual runs.

## Performance Snapshot

Observed on a roughly 2,000-title library with an RTX 3060 12GB:

| Task | Typical Time |
|---|---|
| Candidate scan | about 45 seconds |
| Recommendation generation | about 12-24 seconds per user |
| Whisper turbo transcription | about 3 minutes per video |
| Ollama subtitle translation | about 26 minutes per subtitle |

## Model Guide

| Hardware | Suggested model |
|---|---|
| 24GB+ VRAM | `gemma3:27b` |
| 12GB VRAM | `qwen3.5:9b` |
| 8GB VRAM | `gemma3:4b` |
| CPU-only | `gemma3:4b`, with patience |

## Architecture

```text
Browser dashboard
    -> FastAPI :8000
        -> Plex API
        -> Ollama for recommendations
        -> TMDB/TVDB/OMDB metadata when keys are configured
        -> scheduler + GPU utilization checks

Scripts container
    -> mounted media folders
    -> Whisper ASR
    -> Ollama translation
    -> subtitle audit/dedup/cleanup
```

Runtime state lives under `data/`. Secrets belong in `.env`. Neither should be committed.

## Development

```bash
cd plexmind
pip install -r requirements.txt
uvicorn app.main:app --reload

npm install
npm run build:css
```

## Credits

Built and maintained by [@FineChAInesium](https://github.com/FineChAInesium), with AI-assisted iteration.

## License

MIT.
