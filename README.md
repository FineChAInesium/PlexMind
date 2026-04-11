# PlexMind Suite

<p align="center">
  <a href="https://finechinesium.github.io/PlexMind/"><img src="https://img.shields.io/badge/demo-live-violet?style=flat-square&logo=github" alt="Live Demo"></a>
  <img src="https://img.shields.io/badge/python-3.12-blue?style=flat-square&logo=python" alt="Python 3.12">
  <img src="https://img.shields.io/badge/fastapi-0.111+-green?style=flat-square&logo=fastapi" alt="FastAPI">
  <img src="https://img.shields.io/badge/ollama-local%20LLM-orange?style=flat-square" alt="Ollama">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey?style=flat-square" alt="MIT License">
  <img src="https://img.shields.io/badge/self--hosted-no%20cloud-blueviolet?style=flat-square" alt="Self-hosted">
</p>

**Your Plex library finally understands what you actually want to watch.**

Stop scrolling. PlexMind is a fully-local AI stack that generates eerily accurate movie/TV recommendations, backfills missing subtitles for your entire library, and auto-translates them into any language — all running on your own hardware with zero API costs.

No cloud. No subscriptions. No data leaving your server.

> **[Try the interactive demo →](https://finechinesium.github.io/PlexMind/)**  
> Runs in demo mode with mock data. Deploy locally to connect your actual Plex server.

## Why PlexMind?

Plex's built-in recommendations are generic. Third-party tools phone home. PlexMind runs a local LLM that actually *reads* your watch history, understands your taste, and picks from *your* library — not what's trending on Netflix.

**The difference:** Instead of "because you watched Sci-Fi," you get "Because you watched *Blade Runner 2049* and *Arrival*, you'll love *Annihilation* — it's in your library, unwatched, and has that same cerebral sci-fi vibe."

## What's Inside

| Component | What It Does | Why It Matters |
|---|---|---|
| **🧠 PlexMind** | FastAPI engine + Ollama LLM that analyzes your watch history and generates personalized picks | Actually understands taste, not just genres. Respects watchlist, avoids repeats, explains *why* |
| **🎙️ Transcribe** | Whisper ASR that scans your entire library and generates `.srt` subtitles for anything missing them | No more "no subtitles available." Works on foreign films, anime, obscure rips |
| **🌐 Translate** | Neural subtitle translation to Chinese, Spanish, French, or any language | Bilingual households? Auto-generates both tracks. Keeps timing perfect |
| **🔧 Maintenance** | Library audit, duplicate detection, PGS cleanup, encoding fixes | Finds the cruft Plex misses — duplicate movies, broken subs, wasted space |

## The Stack

- **Fully containerized** — one `docker compose up` and you're running
- **GPU-accelerated** — NVIDIA support with automatic VRAM detection and model selection
- **Privacy-first** — everything runs locally. Your watch history never leaves your network
- **Plex-native** — creates per-user playlists, respects managed users, syncs automatically

## Where Recommendations Appear

PlexMind syncs directly to Plex — no separate app needed:

- **Admin user:** Recommendations appear in your **Watchlist** (as a smart collection)
- **Managed users:** Get two separate playlists:
  - **"PlexMind Movies"** — Movie recommendations
  - **"PlexMind TV Pilot"** — TV show recommendations (first episodes only)

This keeps recommendations isolated per user and avoids cluttering the admin's library.

## Requirements

- Docker + Docker Compose
- Plex Media Server (local or remote)
- NVIDIA GPU recommended (8GB+ VRAM) — CPU fallback works but is slow
- NVIDIA Container Toolkit (for GPU passthrough)

## Quick Start

```bash
git clone https://github.com/FineChAInesium/PlexMind
cd PlexMind
cp .env.example .env

# Edit .env — set these four at minimum:
# PLEX_URL=http://192.168.1.10:32400
# PLEX_TOKEN=your_token_here
# MOVIES_DIR=/mnt/media/Movies
# TV_DIR=/mnt/media/TV

./setup.sh
```

`setup.sh` automatically:

- Detects your GPU and VRAM
- Pulls the optimal LLM (Qwen 9B for 12GB, Gemma 4B for 8GB, etc.)
- Starts PlexMind + Ollama
- Leaves Whisper stopped (GPU-heavy, start only when transcribing)

## Manual Control

```bash
# Start core services
docker compose up -d plexmind ollama

# Start Whisper only when needed (saves 4-6GB VRAM)
docker compose --profile whisper up -d whisper

# View logs
docker compose logs -f plexmind
```

## Configuration

All settings in [`.env.example`](.env.example). Key ones:

| Variable | What It Does | Example |
|---|---|---|
| `PLEX_URL` | Your Plex server (use LAN IP, not localhost) | `http://192.168.1.10:32400` |
| `PLEX_TOKEN` | Plex authentication token | `abc123...` |
| `MOVIES_DIR` / `TV_DIR` | Host paths to your media | `/mnt/user/media/Movies` |
| `OLLAMA_MODEL` | LLM for recommendations (auto-selected) | `qwen3.5:9b` |
| `WHISPER_MODEL` | Transcription accuracy vs speed | `turbo` (recommended) |
| `TARGET_LANGUAGES` | Auto-translate subtitles to these | `zh,es-MX,fr` |

## Web Dashboard

PlexMind ships with a built-in dashboard at `http://<your-server>:8000/`.

No separate install — served directly from the FastAPI container. **[Try the demo](https://finechinesium.github.io/PlexMind/)** to explore it before deploying.

**Dashboard tabs:**

| Tab | What It Shows |
|---|---|
| **Dashboard** | Live health cards (API, LLM ready state, GPU utilization %, Whisper), Plex user count, next scheduler run, disk usage — all polled from real API data every 30s |
| **Recommendations** | Per-user table with user type, playlist destination, and one-click Generate button. Refreshes against live `/api/users` |
| **Transcribe** | Settings reference, lifetime stats (223 processed, 8,565 hallucinations cleaned), and the exact `docker exec` command to run |
| **Translate** | Same pattern — model name, target languages, lifetime stats (37 translated, 151 skipped), docker exec command |
| **Maintenance** | Buttons for Audit Library, Find Duplicates, Clean PGS — each shows the correct `docker exec` command to run |
| **Settings** | Set `API_BASE_URL` and optional `API_KEY` (both persisted in localStorage). URL defaults to the origin that served the page so it works out of the box |

**GPU card** reads live `gpu_utilization_pct` from the scheduler status endpoint — shows "Busy" (amber) or "Available" (green) with threshold %.
**Storage widget** reads real disk usage from the data volume via `/api/storage` — updates every 60s.

## API Usage

PlexMind runs at `http://<your-server>:8000` — interactive docs at `/docs`.

| Endpoint | Purpose |
|---|---|
| `GET /health` | LLM ready state, model name |
| `GET /api/users` | List all Plex users (admin + managed) |
| `GET /api/users/{id}/recommendations` | Get cached picks for a user |
| `POST /api/users/{id}/recommendations?force=true` | Force-regenerate picks |
| `POST /api/users/{id}/feedback` | Like / dislike / watched — invalidates cache |
| `POST /api/run-all` | Trigger background recs + sync for all users |
| `GET /api/scheduler/status` | Next run time, GPU utilization %, busy flag |
| `GET /api/storage` | Disk usage for the data volume |
| `POST /webhook` | Plex webhook receiver — clears cache on `library.new` |

Example:

```bash
# Generate recommendations for a user
curl "http://192.168.1.10:8000/api/users/your_plex_username/recommendations?force=true"

# Trigger all users in background
curl -X POST http://192.168.1.10:8000/api/run-all
```

## Security

PlexMind is designed for trusted home networks. For extra hardening:

**Optional API key** — set `PLEXMIND_API_KEY` in your `.env` to require authentication on all mutation endpoints. The dashboard reads it from Settings and sends it as an `X-API-Key` header automatically.

```bash
# Generate a key
echo "PLEXMIND_API_KEY=$(openssl rand -hex 32)" >> .env
```

**Network isolation** — Ollama is bound to `127.0.0.1` in the compose file (no LAN exposure). Whisper is container-internal only. Only port `8000` is published.

**Non-root container** — PlexMind runs as uid 1000 inside the container.

See [`.env.example`](.env.example) for `PLEXMIND_API_KEY` and `CORS_ORIGINS` options.

## CLI Scripts

Run maintenance tasks directly:

```bash
# Transcribe everything missing subtitles (resumable)
docker exec plexmind-scripts /app/transcribe.sh

# Translate all English subs to target languages
docker exec plexmind-scripts /app/translate.sh

# Full library audit
docker exec plexmind-scripts /app/maintenance.sh all

# Find duplicates
docker exec plexmind-scripts /app/maintenance.sh dedup

# Add watermark to all SRTs
docker exec plexmind-scripts /app/watermark.sh
```

## Model Selection (Auto-Configured)

| Your GPU | Recommended Model | VRAM Used | Speed |
|---|---|---|---|
| RTX 4090 / 24GB+ | `gemma3:27b` | ~18GB | ~35 tok/s |
| RTX 3060 12GB / 4070 | `qwen3.5:9b` ⭐ | ~6.5GB | ~20 tok/s |
| RTX 3060 8GB / 4060 | `gemma3:4b` | ~4GB | ~30 tok/s |
| CPU only | `gemma3:4b` | RAM | ~3 tok/s |

`setup.sh` handles this automatically based on `nvidia-smi`.

## Transcription: Actually Smart

Not just "run Whisper on everything":

- **Language profiling** — samples audio at 5 points, auto-detects if it's actually English or foreign
- **Bilingual VIP** — Squid Game, Dark, etc. get both native language + English translation subs
- **Hallucination filtering** — removes Whisper's infamous repeated phrases and garbage output
- **Confidence scoring** — flags low-quality transcriptions for manual review
- **Resume support** — stops and picks up exactly where it left off
- **Time windows** — only runs during configured hours by default to avoid GPU contention

## Translation: Context-Aware

- **Chunked with memory** — passes previous subtitle chunk as context for coherent translations
- **Custom prompts** — Traditional Chinese uses different instructions than Mexican Spanish
- **SRT-safe** — preserves timing, fixes ordering issues, handles multi-line dialogue
- **Batch mode** — translate your entire library overnight

## Performance

Typical runtime on a 2,000-title library (RTX 3060 12GB), based on real lifetime stats:

- **Initial scan:** ~45 seconds (prefilters to top 100 candidates, enriches via TMDB)
- **Recommendations per user:** ~12–24 seconds (scales with watch history size)
- **Transcription:** ~3 minutes per video (Whisper turbo)
- **Translation:** ~26 minutes per subtitle file (Ollama chunk-based, scales with subtitle length)

No API rate limits. No monthly fees. Just your hardware.

## Architecture

```
Plex Server → PlexMind (FastAPI) → Ollama (LLM)
     ↓                                      ↓
Watch History                      Generates Picks
     ↓                                      ↓
TMDB/OMDB Enrichment ←─────────── Plex Playlist Sync
     ↓
Admin → Watchlist
Users → "PlexMind Movies" + "PlexMind TV Pilot" playlists
```

Everything is cached locally. Second run is instant.

## License

MIT — use it, fork it, break it, improve it.
