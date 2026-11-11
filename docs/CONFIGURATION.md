# Configuration reference

PlexMind runs with no settings beyond your media folders: `setup.sh` or the
Unraid template sets those, sign in with Plex connects the server, and the
API key and webhook secret are generated on first start
(`data/secrets.json`). Everything below is optional. Put a line in `.env`
(Compose) or add a variable to the container (Unraid) only to change a default.

Values shown are the defaults.

## Plex and access

| Setting | Default | What it does |
|---|---|---|
| `PLEX_URL`, `PLEX_TOKEN` | from sign-in | Set both to skip sign in with Plex. A `PLEX_TOKEN` here always wins over the saved sign-in. |
| `PLEXMIND_API_KEY` | generated | Key for scripts and other tools (`X-API-Key` header). Generated into `data/secrets.json` when unset. |
| `PLEXMIND_WEBHOOK_SECRET` | generated | Secret in the Plex webhook URL. Generated into `data/secrets.json` when unset. |
| `PLEXMIND_SESSION_DAYS` | 30 | How long a dashboard login lasts. |
| `PLEXMIND_SECURE_COOKIE` | 0 | 1 when the dashboard is only reached over HTTPS. |
| `PLEXMIND_ADMIN_USERNAME` | from Plex | Display name for the server owner. |

## Settings explained

| Setting | What it does | Default |
|---|---|---|
| `CORS_ORIGINS` | Comma-separated browser origins allowed to call the API from another site. | blank (same-origin only) |
| `TMDB_API_KEY` | Optional metadata enrichment. | unset |
| `TVDB_API_KEY` | Optional TV metadata fallback. | unset |
| `OMDB_API_KEY` | Optional IMDb/OMDB enrichment. | unset |
| `LLAMA_CPP_URL` | llama.cpp base URL for recommendations. | `http://llama-cpp:8080` |
| `LLAMA_CPP_API_URL` | Optional explicit script endpoint; otherwise derived from `LLAMA_CPP_URL`. | derived |
| `MOVIES_HOST_PATH` / `TV_HOST_PATH` | Host directories bind-mounted into the containers. | platform-specific |
| `LLAMA_CPP_MODEL_ALIAS` | Recommendation and translation model alias. The model used until one is chosen on the dashboard. | `gemma-4-12b-it-Q5_K_M` in Compose |
| `LLAMA_CPP_HOST_PORT` | Host port for the bundled llama.cpp sidecar. | `11435` |
| `LLAMA_CPP_BIND_ADDRESS` | Host address for that port. llama.cpp has no API key and PlexMind uses the Docker network, so it stays on loopback unless a LAN client needs it. | `127.0.0.1` |
| `WHISPER_API_URL` | Whisper ASR endpoint used by scripts. | `http://whisper:9000/asr` |
| `WHISPER_IMAGE` | Whisper ASR Docker image. | Digest-pinned; see `.env.example` |
| `WHISPER_MODEL` | Whisper model: `tiny`, `base`, `small`, `medium`, `large`, `turbo`. | `turbo` |
| `WHISPER_DEVICE` | Whisper device. Use `cuda` or `cpu`. | `cuda` |
| `WHISPER_HOST_PORT` | Host port for the bundled Whisper sidecar. | `9001` |
| `WHISPER_BIND_ADDRESS` | Host address for that port. Set `0.0.0.0` if Bazarr or another LAN client uses Whisper. | `127.0.0.1` |
| `WHISPER_TMP_DIR` | Whisper `/tmp` scratch space: a host directory, or blank for the `whisper-tmp` Docker volume. | `whisper-tmp` volume |
| `TRANSLATION_BATCH_CUES` | Consecutive subtitle cues per llama.cpp translation request; `1` sends one cue per request. | `8` |
| `TRANSLATION_MAX_FALLBACK_PCT` | Fail a file only if more than this percentage of its cues kept their original line. | `10` |
| `CHUNK_RETRY_ATTEMPTS` | Attempts per cue before it keeps its original line (or, for llama.cpp errors, the file stops and resumes later). | `3` |
| `FAILED_RETRY_HOURS` | Hours before a failed file is tried again. | `24` |
| `TRANSCRIBE_FAILED_RETRY_DAYS` | Days before transcription retries a video that failed permanently (validation, no usable audio); `0` never retries. | `30` |
| `SUBTITLE_INVENTORY_TTL_HOURS` | Transcription and translation skip a video that already has a full subtitle in that language: a `.srt` beside it, a track inside the file, or any subtitle Plex lists for it (including ones Plex downloaded). Forced tracks don't count, and languages match on their base (any Spanish covers `es-MX`). The answer is cached per file for this many hours. | `24` |
| `THROTTLE_GPU_TEMP` / `THROTTLE_CPU_TEMP` / `THROTTLE_NVME_TEMP` / `THROTTLE_DISK_TEMP` / `THROTTLE_RAM_PCT` | Jobs hold while a sensor is at its limit (degrees C, or percent of memory used). Blank turns that sensor off; a sensor that can't be read is skipped. Translation checks between batches, recommendations between users, maintenance between phases, and transcription between files and upload chunks, so a Whisper upload in progress finishes first. Hard drive temperatures need `UNRAID_EMHTTP_DIR=/var/local/emhttp`. | blank |
| `THROTTLE_RESUME_MARGIN` / `THROTTLE_CHECK_SECONDS` | Jobs resume once every enabled sensor is this far below its limit; sensors are read at most this often. | `5` / `30` |
| `LLAMA_ON_DEMAND` | Start llama.cpp when needed and stop it when idle. | `0` |
| `LLAMA_IDLE_STOP_MINUTES` | Idle minutes before an on-demand llama.cpp is stopped. | `15` |
| `LLAMA_STARTUP_TIMEOUT_SECONDS` | How long to wait for an on-demand llama.cpp to load its model. | `180` |
| `LLAMA_CPP_PARALLEL` | llama.cpp request slots (`--parallel`). | `1` |
| `GPU_PROBE_CONTAINERS` | Containers the GPU-busy check runs `nvidia-smi` in, in order. | `llama-cpp` |
| `PLEXMIND_GPU_PROBE_CONTAINERS` | Extra containers the broker may run the fixed GPU probe in. | blank |
| `PGS_CLEANUP_ALLOW_UNKNOWN` | Let PGS cleanup act on image subtitles with no language tag. | `0` |
| `PGS_CLEANUP_ALLOW_GENERATED` | `1` lets PlexMind's own Whisper or LLM subtitles replace authored image subtitles in PGS cleanup; by default only other text subtitles (with at least 20 cues, or any forced track) replace them. | `0` |
| `OMDB_NOT_FOUND_TTL_SECONDS` | How long an OMDb "not found" is cached. Errors and rate limits are never cached. | `604800` (7 days) |
| `TARGET_LANGUAGES` | Comma-separated subtitle translation targets. | `zh,es-MX` |
| `MAX_RECOMMENDATIONS` | Picks per user. | `10` |
| `CANDIDATE_POOL_SIZE` | Prefiltered candidate count before LLM. | `40` |
| `MIN_HISTORY_ITEMS` | Minimum watch history before batch generation. | `3` |
| `REC_REFRESH_DAYS` | A user's picks are kept while they are newer than this and untouched; older ones are replaced even if none were watched. | `25` |
| `SUPPRESSION_DAYS` | Days before a shown recommendation can return. | `60` |
| `GPU_THRESHOLD_PCT` | Pause batch work at or above this GPU utilization. | `30` |
| `GPU_BACKOFF_MINUTES` | Wait time before checking a busy GPU again. | `30` |
| `GPU_BUSY_SAMPLES` | Consecutive GPU readings that must all be at or above the threshold before work backs off. | `3` |
| `GPU_SAMPLE_INTERVAL_SECONDS` | Seconds between those readings. | `5` |
| `SCRIPT_START_RATE_LIMIT` | Rate limit for script Start buttons. | `60/hour` |
| `WHISPER_CONTAINER_NAME` | Exact dedicated Whisper container to manage. | `whisper-asr-webservice` in Compose |
| `LLAMA_CPP_CONTAINER_NAME` | Exact llama.cpp container identity when lifecycle management is enabled. | `llama-cpp` |
| `MANAGE_LLAMA_CPP_CONTAINER` | Manage llama.cpp per translation job. Keep `0` for the shared recommendation service. | `0` |
| `START_SIDECAR_CONTAINERS` | Start AI sidecars before script jobs. | `1` |
| `STOP_SIDECAR_CONTAINERS` | Stop AI sidecars when script jobs exit. | `1` |
| `ALLOW_EMPTY_MEDIA_ROOTS` | Permit intentionally empty media roots. Leave disabled in production. | `0` |
| `LOG_RETENTION_DAYS` | Retain dated script logs under `/app/data/logs`. | `7` |
| `MAX_RUNTIME_MINUTES` | Optional per-run cap for scripts. `0` means no cap. | `0` |
| `TRANSCRIBE_START_HOUR` / `TRANSCRIBE_END_HOUR` | Window in which PlexMind starts transcription. A running translation is paused for it and resumes afterwards. | `5` / `12` |
| `TRANSLATE_START_HOUR` / `TRANSLATE_END_HOUR` | Window in which PlexMind starts translation. A run keeps going through the backlog after the window closes (set `MAX_RUNTIME_MINUTES` to cap it), pausing for transcription and the monthly recommendation batch. | `23` / `3` |
| `MONTHLY_RETRY_HOURS` / `MONTHLY_RETRY_LIMIT` | A monthly recommendation batch that was skipped or failed is retried hourly, up to this many times within this many hours. | `72` / `12` |
| `TZ` | Timezone used by the PlexMind script scheduler and logs. | `UTC` |

The dashboard's **Throttle** page sets the `THROTTLE_*` limits too; once saved there they replace the ones here, and running jobs pick them up.

## Every setting, with its default

```
# --- OPTIONAL: API KEYS ---

# TMDB API key — enriches recommendations with ratings/metadata
# Get one free at https://www.themoviedb.org/settings/api
TMDB_API_KEY=

# OMDB API key — adds IMDB/Rotten Tomatoes/Metascore data
# Get one free (1k/day) at https://www.omdbapi.com/apikey.aspx
OMDB_API_KEY=

# TVDB API key — TV show metadata (status, network, etc.)
# Free tier has limited data; leave blank to skip
TVDB_API_KEY=

# --- MEDIA PATHS ---

# Paths to your movie and TV library on the host machine
MOVIES_HOST_PATH=/path/to/Movies
TV_HOST_PATH=/path/to/TV Shows

# Optional persistent cache paths for bundled sidecars
LLAMA_CPP_MODELS_DIR=/mnt/cache/appdata/llama-cpp/models
# Host port for the bundled llama.cpp service.
LLAMA_CPP_HOST_PORT=11435
# Host address that port listens on. PlexMind reaches llama.cpp over the Docker
# network, and llama.cpp has no API key; set 0.0.0.0 only if a LAN client needs it.
LLAMA_CPP_BIND_ADDRESS=127.0.0.1
WHISPER_CACHE_DIR=./data/whisper-cache
WHISPER_MODEL_CACHE_DIR=/whisper-cache
# Whisper's /tmp scratch space. Blank uses the whisper-tmp Docker volume; set a
# dedicated host directory to keep it elsewhere (not the host's /tmp).
WHISPER_TMP_DIR=
WHISPER_MEM_LIMIT=12g
# Host port for the bundled Whisper service.
WHISPER_HOST_PORT=9001
# Host address that port listens on. PlexMind reaches Whisper over the Docker
# network; set 0.0.0.0 if Bazarr or another LAN client uses it.
WHISPER_BIND_ADDRESS=127.0.0.1

# --- AI MODELS ---

# Default model for recommendations + translation: a GGUF file name in LLAMA_CPP_MODELS_DIR,
# without .gguf. PlexMind is model-agnostic, and the dashboard's Model Advisor card can switch
# to any other GGUF in that folder; the choice there is remembered and overrides this.
LLAMA_CPP_MODEL_ALIAS=gemma-4-12b-it-Q5_K_M
LLAMA_CPP_MAX_TOKENS=768
CHUNK_RETRY_ATTEMPTS=3
CHUNK_RETRY_DELAY_SECONDS=2
FAILED_RETRY_HOURS=24
# Transcription retries a permanently failed video after this many days (0 = never).
TRANSCRIBE_FAILED_RETRY_DAYS=30
# Hours a video's embedded/Plex subtitle languages are cached before they are checked again.
SUBTITLE_INVENTORY_TTL_HOURS=24
# Hold jobs while hot or short of memory. A blank limit turns that sensor off.
# Temperatures in degrees C; RAM in percent used. Example: 80 / 90 / 70 / 50 / 90.
THROTTLE_GPU_TEMP=
THROTTLE_CPU_TEMP=
THROTTLE_NVME_TEMP=
# Needs UNRAID_EMHTTP_DIR=/var/local/emhttp (Unraid only).
THROTTLE_DISK_TEMP=
THROTTLE_RAM_PCT=
# Jobs resume once every enabled sensor is this far below its limit.
THROTTLE_RESUME_MARGIN=5
THROTTLE_CHECK_SECONDS=30
# A cue the model cannot translate keeps its English line; the whole file
# fails only if more than this percentage of its cues fall back.
TRANSLATION_MAX_FALLBACK_PCT=10
# Consecutive subtitle cues sent per llama.cpp request (JSON array, exact item
# count enforced); cues a batch does not deliver are retried one per request.
# 1 = one cue per request. A batch may generate up to N x 256 tokens, which must
# fit in the llama.cpp context (LLAMA_CPP_CTX_SIZE, 8192 in Compose).
TRANSLATION_BATCH_CUES=8

# Whisper model for transcription (tiny, base, small, medium, large, turbo)
WHISPER_MODEL=turbo

# Whisper Docker image (use cpu variant if no GPU)
# Digest-pinned image used for both CUDA and CPU modes.
WHISPER_IMAGE=onerahmet/openai-whisper-asr-webservice@sha256:3deb30cfd3d5614fd3885e6dfbef7dfcf95fe897300ecc703f7f3b3682b717ae

# Whisper device (cuda or cpu)
WHISPER_DEVICE=cuda

# --- RECOMMENDATIONS ---

# How many recommendations per user
MAX_RECOMMENDATIONS=10

# How many pre-filtered candidates to show the LLM
CANDIDATE_POOL_SIZE=40

# Minimum watch history items to generate recommendations
MIN_HISTORY_ITEMS=3
# Replace a user's picks once they are this many days old, even if none were watched
REC_REFRESH_DAYS=25

# Days before a recommendation can reappear
SUPPRESSION_DAYS=60

# Cache TTL in seconds
CACHE_TTL_SECONDS=3600

# GPU load threshold % — batch run pauses above this
GPU_THRESHOLD_PCT=30

# Minutes to wait before re-checking GPU utilization
GPU_BACKOFF_MINUTES=5

# The GPU only counts as busy when this many readings, this many seconds apart,
# are all at or above GPU_THRESHOLD_PCT (ignores momentary spikes, e.g.
# PlexMind's own llama.cpp requests).
GPU_BUSY_SAMPLES=3
GPU_SAMPLE_INTERVAL_SECONDS=5

# Minimum % watched to count as "watched" (0.0–1.0)
MIN_WATCH_PCT=0.70

# --- SCRIPT LIFECYCLE ---

# Container names used by PlexMind-managed script jobs.
# Transcription starts/stops Whisper; translation starts/stops llama.cpp.
WHISPER_CONTAINER_NAME=whisper-asr-webservice
LLAMA_CPP_CONTAINER_NAME=llama-cpp
MANAGE_LLAMA_CPP_CONTAINER=0
# On-demand llama.cpp: start it via the broker when recommendations or
# translation need it, and stop it after this many idle minutes to free VRAM.
LLAMA_ON_DEMAND=0
LLAMA_IDLE_STOP_MINUTES=15
LLAMA_STARTUP_TIMEOUT_SECONDS=180
# llama.cpp request slots; PlexMind sends one request at a time.
LLAMA_CPP_PARALLEL=1
# GPU-busy checks exec nvidia-smi in these containers, in order. With
# LLAMA_ON_DEMAND=1 add an always-running GPU container (e.g. Plex) here and in
# PLEXMIND_GPU_PROBE_CONTAINERS, or scheduled jobs defer while llama.cpp is stopped.
GPU_PROBE_CONTAINERS=llama-cpp
PLEXMIND_GPU_PROBE_CONTAINERS=
START_SIDECAR_CONTAINERS=1
STOP_SIDECAR_CONTAINERS=1
ALLOW_EMPTY_MEDIA_ROOTS=0

# --- SCRIPT LOGS ---

# Keep script logs for this many days under /app/data/logs
LOG_RETENTION_DAYS=7

# Optional per-run cap. Use 0 for no cap, or pass via docker exec/cron.
MAX_RUNTIME_MINUTES=0

# --- TRANSCRIPTION ---

# Extract compressed 16 kHz mono audio before uploading to Whisper.
TRANSCRIBE_AUDIO_EXT=mp3
TRANSCRIBE_AUDIO_CODEC=libmp3lame
TRANSCRIBE_AUDIO_BITRATE=64k
WHISPER_UPLOAD_SPLIT_MB=50
WHISPER_SEGMENT_SECONDS=600
SUBTITLE_MAX_LINE_CHARS=42
SUBTITLE_MAX_LINES=2
SUBTITLE_MAX_CPS=20
SUBTITLE_MIN_CUE_SECONDS=1.0
SUBTITLE_MAX_CUE_SECONDS=7.0
SUBTITLE_MIN_GAP_SECONDS=0.04

# Time window for transcription (24h format, local time)
TRANSCRIBE_START_HOUR=5
TRANSCRIBE_END_HOUR=12

# Comma-separated bilingual VIP titles (auto-detect + translate)
KNOWN_BILINGUAL_TITLES=90 Day Fiancé,Shogun,Shōgun,Squid Game

# Comma-separated English reality titles (skip language profiling)
KNOWN_ENGLISH_REALITY_TITLES=Summer House,Vanderpump Rules

# --- TRANSLATION ---

# Time window for translation (24h format, local time)
TRANSLATE_START_HOUR=23
TRANSLATE_END_HOUR=3
# A skipped or failed monthly recommendation batch is retried hourly.
MONTHLY_RETRY_HOURS=72
MONTHLY_RETRY_LIMIT=12

# Target languages (comma-separated ISO codes)
TARGET_LANGUAGES=zh,es-MX

# --- WATERMARK ---

# Set false to disable watermarking on newly generated subtitles.
ENABLE_WATERMARK=true

# Subtitle watermark text (SRT/ASS format), written exactly as given. Keep the
# single quotes so {\an8} reaches the subtitle with one backslash.
WATERMARK_TEXT='{\an8}<i>Brought to you by PlexMind</i>'
WATERMARK_SEARCH=PlexMind

# Final subtitle permissions. Optional UID/GID values change ownership when
# the scripts worker has permission; blank values preserve current ownership.
SUBTITLE_FILE_MODE=0644
SUBTITLE_UID=
SUBTITLE_GID=

# Replaced image subtitles are moved to DATA_DIR/quarantine/pgs; forced/SDH
# tracks only count as replaced by an SRT of the same kind.
# Unknown-language PGS tracks are preserved unless explicitly opted in.
PGS_CLEANUP_ALLOW_UNKNOWN=0
# 1 lets PlexMind's own Whisper/LLM subtitles replace (quarantine) authored image subtitles.
PGS_CLEANUP_ALLOW_GENERATED=0

# Start button rate limit for GUI-owned script jobs. Raise on trusted LANs if needed.
SCRIPT_START_RATE_LIMIT=60/hour

# Allowed CORS origins (comma-separated). Leave blank to disable cross-origin requests.
# Example for LAN access: http://192.168.1.10:8000,http://localhost:8000
CORS_ORIGINS=

# Set 1 when the dashboard is only reached over HTTPS (reverse proxy) to mark
# the session cookie Secure. Browsers will not send a Secure cookie over plain
# http://, so keep 0 for direct LAN access.
PLEXMIND_SECURE_COOKIE=0

# Set to true to disable the web dashboard (API-only mode)
PLEXMIND_NO_GUI=false

# --- GENERAL ---

# Timezone
TZ=UTC
```
