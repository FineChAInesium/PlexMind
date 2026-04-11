# PlexMind Suite

**Your Plex library finally understands what you actually want to watch.**

Stop scrolling. PlexMind is a fully-local AI stack that generates eerily accurate movie/TV recommendations, backfills missing subtitles for your entire library, and auto-translates them into any language — all running on your own hardware with zero API costs.

No cloud. No subscriptions. No data leaving your server.

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

## API Usage

PlexMind runs at `http://localhost:8000`

| Endpoint | Purpose |
|---|---|
| `GET /health` | Check LLM status, GPU, Plex connectivity |
| `POST /recommend/{username}` | Generate picks for a user (respects their watch history) |
| `POST /run-batch` | Update all users (runs nightly via cron) |
| `GET /users` | List all Plex managed users |
| `POST /feedback` | Thumbs up/down — trains future recommendations |

Example:

```bash
curl -X POST http://localhost:8000/recommend/your_plex_username
```

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
