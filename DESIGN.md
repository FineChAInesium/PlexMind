# PlexMind Suite — Design Document

> **Living document.** Updated as the system evolves.  
> Last reviewed: 2026-05-25 | Version: v0.8.18

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [Component Inventory](#3-component-inventory)
4. [Data Model & Storage](#4-data-model--storage)
5. [External Integrations](#5-external-integrations)
6. [Recommendation Engine](#6-recommendation-engine)
7. [Subtitle Pipeline](#7-subtitle-pipeline)
8. [Security Posture](#8-security-posture)
9. [Live System State](#9-live-system-state)
10. [Known Bugs](#10-known-bugs)
11. [Recommendations & Improvements](#11-recommendations--improvements)
12. [Future Features Backlog](#12-future-features-backlog)
13. [Changelog](#13-changelog)

---

## 1. System Overview

PlexMind Suite is a self-hosted AI control plane for Plex. It runs entirely on local hardware (Unraid + NVIDIA GPU) and provides:

| Capability | What it does |
|---|---|
| **Taste-aware recommendations** | Generates personalized picks per Plex user using local llama.cpp LLM |
| **Subtitle backfill** | Whisper ASR transcription for any video missing subtitles |
| **Subtitle translation** | llama.cpp-driven SRT translation (current targets: zh, es-MX) |
| **Library hygiene** | Dedup, PGS cleanup, encoding repair, audit reports |
| **Dashboard** | FastAPI web UI for job management, progress, and monitoring |

**Stack:** Python 3.12 / FastAPI / APScheduler / Bash / Tailwind CSS (vanilla JS)  
**Deployment:** Docker Compose (5 services) on Unraid  
**LLM:** llama.cpp OpenAI-compatible server running qwen3-4b-q4_k_m (local, no cloud)  
**ASR:** Whisper (onerahmet webservice, GPU-accelerated)

---

## 2. Architecture

```
Browser Dashboard (Tailwind CSS / Vanilla JS)
        ↓ HTTP + SSE
FastAPI App  :8000  [plexmind container]
  ├─ Plex API client        — watch history, users, playlists, webhooks
  ├─ Recommendation engine  — genre fingerprint + LLM ranking
  ├─ APScheduler            — monthly batch cron
  ├─ Script runner          — subprocess launcher (local mode) / HTTP proxy (sidecar mode)
  ├─ TMDB / TVDB / OMDB     — metadata enrichment
  └─ SSE event bus          — real-time progress to dashboard

AI Sidecars  [start on-demand, stopped when idle]
  ├─ llama.cpp    :11435   — OpenAI-compatible LLM for recs + translation
  └─ Whisper ASR  :9001    — speech-to-text

Storage  [persistent volumes]
  ├─ /app/data/            — JSON state, logs, reports, stats
  ├─ /media/movies         — mounted movie library
  └─ /media/tv             — mounted TV library

Infrastructure
  ├─ /var/run/docker.sock  — container lifecycle for sidecars
  └─ nginx-proxy-manager   — TLS termination / LAN reverse proxy
```

### Service Map (Docker)

| Container | Image | Port | Status |
|---|---|---|---|
| `plexmind` | plexmind:latest | 8000 | Up |
| `plexmind-scripts` | plexmind-scripts:latest | — | Up (idle) |
| `llama-cpp` | ghcr.io/ggml-org/llama.cpp:server-cuda | 11435 | Up (GPU-backed) |
| `whisper-asr-webservice` | onerahmet/openai-whisper-asr-webservice:latest-gpu | 9001 | Exited when idle |
| `ffmpeg-nvidia` | jrottenberg/ffmpeg:4.2-nvidia | — | Up (sidecar) |

---

## 3. Component Inventory

### Python — plexmind/app/

| Module | Lines | Role |
|---|---|---|
| `main.py` | ~800 | FastAPI routes, SSE streaming, webhook receiver |
| `recommender.py` | ~300 | 9-feature recommendation scoring + LLM ranking |
| `plex_client.py` | ~283 | Plex API: users, history dedup, on-deck exclusion |
| `plex_sync.py` | ~300 | Watchlist sync (admin) + playlist sync (managed users) |
| `scheduler.py` | ~491 | APScheduler cron, GPU utilization polling, batch runner |
| `llm_client.py` | — | llama.cpp chat, JSON repair, fence stripping, Qwen no_think prompting, retry |
| `tmdb_client.py` | — | Genre, keywords, cast, posters, trending |
| `tvdb_client.py` | — | TV status, networks |
| `imdb_client.py` | — | IMDb ratings via OMDB |
| `cache.py` | ~150 | TTL rec cache, shown-rec suppression, feedback persistence |
| `script_runner.py` | ~300 | Job lifecycle: start, stop, status, log tail |

### Shell Scripts — scripts/

| Script | Lines | Role |
|---|---|---|
| `transcribe.sh` | 533 | Full Whisper ASR pipeline (language profiling → extract → transcribe → post-process) |
| `translate.sh` | 337 | Chunk-based llama.cpp SRT translation with context windows |
| `maintenance.sh` | 121 | Audit, PGS cleanup, dedup, encoding repair, reports |
| `lib.sh` | 800+ | Shared utilities: logging, locking, Docker socket API, SRT validation, hallucination cleaning |
| `control_server.py` | ~200 | Optional Flask sidecar for script job HTTP API |
| `fix_srt_ordering.py` | ~120 | SRT timestamp repair |
| `watermark.sh` | ~80 | ASS-format watermark injection |

### API Endpoints (key ones)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Full health: API + LLM + Whisper |
| GET | `/api/users` | Plex user list |
| GET | `/api/users/{id}/recommendations` | Generate or return cached recs |
| POST | `/api/run-all` | Trigger all-user batch (rate: 3/hr) |
| GET | `/api/jobs/{id}/events` | SSE stream for batch progress |
| POST | `/api/users/{id}/sync` | Push recs to Plex watchlist/playlist |
| POST | `/api/scripts/{job}/start` | Start transcribe/translate/maintenance |
| GET | `/api/scripts/{job}/log` | Current session log tail |
| POST | `/webhook` | Plex event receiver (invalidates cache on library.new) |
| GET | `/api/trending` | TMDB weekly trending |
| GET | `/api/storage` | Disk usage |

---

## 4. Data Model & Storage

No database. All state is in-memory or JSON files under `/app/data/`.

### In-Memory

| Key | Type | TTL |
|---|---|---|
| `_cache[user_id]` | Recommendation list | CACHE_TTL_SECONDS (default 3600s) |
| `_jobs[job_id]` | Batch job state for SSE | Cleared on next batch |
| `PROCS[job_name]` | Subprocess handles | Lifetime of process |

### JSON Files

| File | Contents |
|---|---|
| `feedback.json` | `{user_id: [{title, rating, note, ts}]}` |
| `shown_recs.json` | `{user_id: {title_lower: unix_ts}}` — suppression tracking |
| `recommendation_history.json` | Lifetime recommendation log for dashboard history view |
| `watchlist_track.json` | Items PlexMind added to watchlist (for cleanup) |
| `plex_users_cache.json` | Cached Plex user list |
| `lifetime_stats.env` | Shell-sourced transcription counters |
| `translation_stats.env` | Shell-sourced translation counters |

### Log Files

Dated logs with 7-day retention (LOG_RETENTION_DAYS):
- `logs/transcription-YYYY-MM-DD.log`
- `logs/translation-YYYY-MM-DD.log`
- `logs/maintenance-YYYY-MM-DD.log`

Active symlinks: `transcription.log`, `translation.log`, `maintenance.log`

---

## 5. External Integrations

| Service | Purpose | Required |
|---|---|---|
| Plex Media Server (`plexapi`) | Library metadata, watch history, users, playlists | Yes |
| llama.cpp | LLM for recs + SRT translation through OpenAI-compatible chat API | Yes |
| Whisper ASR Webservice | Speech-to-text for subtitle generation | For transcription |
| TMDB | Genres, keywords, posters, trending | Optional (strongly recommended) |
| TVDB | TV show status, networks | Optional |
| OMDB / IMDb | IMDb ratings, Metascore | Optional |
| Docker socket | Start/stop Whisper and llama.cpp sidecars; probe GPU utilization through GPU-backed containers | For sidecar lifecycle and GPU status fallback |

**Current config:**
- Model: `qwen3-4b-q4_k_m`; generation defaults are capped for 8192-token context
- TVDB + OMDB keys: **not set** — metadata enrichment is partial
- Plex token in `.env` (plaintext — keep `chmod 600`)

---

## 6. Recommendation Engine

### 9 Scoring Features (recommender.py)

| # | Feature | Detail |
|---|---|---|
| 1 | **Recency-weighted genre fingerprint** | Last 90d = 3×, 90–180d = 2×, older = 1× |
| 2 | **Partial-watch exclusion** | Items < MIN_WATCH_PCT (70%) excluded |
| 3 | **In-progress exclusion** | Plex on-deck items excluded from candidates |
| 4 | **Feedback penalties** | Genres from disliked items reduce candidate scores |
| 5 | **Original-language awareness** | Dominant watch language gets +6% boost per candidate |
| 6 | **"Because you watched X" reasoning** | LLM includes specific similar titles in output reason |
| 7 | **Trending boost** | TMDB weekly trending candidates get +8% |
| 8 | **Re-recommendation suppression** | Shown items invisible for SUPPRESSION_DAYS (60d default) |
| 9 | **Candidate pre-filter** | Top CANDIDATE_POOL_SIZE (40) by composite score sent to LLM |

### Flow

```
Watch history → genre fingerprint → score all unwatched items
→ TMDB/TVDB/OMDB enrich (concurrent) → apply boosts/penalties
→ top 40 candidates → llama.cpp chat → parse + repair JSON
→ check suppression → cache 1hr → return
```

### Gaps (not yet implemented)

- TMDB cast/director data is fetched but not used in scoring
- Feedback penalties are binary — not graduated by dislike intensity
- Single llama.cpp model — no fallback or A/B test path
- Webhook only invalidates on `library.new`; `media.rate` events ignored

---

## 7. Subtitle Pipeline

### Transcription Flow (transcribe.sh)

```
Video file
  → Language detection: metadata → 5-point audio sampling → Whisper detect
  → Bilingual VIP / reality-TV bypass check
  → ffmpeg audio extraction (language-specific track or primary)
  → POST to Whisper ASR API (initial_prompt="English" for EN)
  → Post-processing: hallucination clean → timestamp normalize → SRT validate
  → Watermark inject → confidence score → quarantine if <30%
  → Bilingual VIP: non-Latin script detected → rename → llama.cpp translate pass
  → Update lifetime stats
```

### Translation Flow (translate.sh)

```
.en.srt file
  → Split into CHUNK_SIZE=5 cue chunks
  → Per chunk: build prompt with previous chunk as context
  → llama.cpp chat → strip markdown → validate timestamps
  → fix_srt_ordering.py
  → Unload model from VRAM on exit
```

### Maintenance Modes (maintenance.sh)

| Mode | Action |
|---|---|
| `audit` | Subtitle coverage report per library |
| `pgs-cleanup` | Delete `.sup`, `.sub/.idx` image subtitle files |
| `dedup` | Remove duplicate `.srt` files by cue count + size |
| `encoding` | Convert non-UTF-8 SRTs |
| `report` | Generate dashboard markdown from lifetime stats |
| `all` | All of the above in sequence |

### Live Stats (as of 2026-05-07)

| Metric | Value |
|---|---|
| Videos scanned (lifetime) | 46,251 |
| English transcribed | 1,170 |
| Bilingual processed | 17 |
| Hallucinations cleaned | 54,696 |
| Skipped (existing subs) | 44,932 |
| Skipped (failures) | 56 |
| Translations scanned | 517 |
| SRTs translated | 73 |

---

## 8. Security Posture

### Strengths

- `PLEXMIND_API_KEY` uses `secrets.compare_digest()` (timing-safe)
- Rate limiting on heavy endpoints (`slowapi`): 3/hr batch, 20–30/min per IP
- Plex webhook checks for LAN IP origin
- API container runs as UID 1000 (non-root)
- No `/config` or `/env` endpoint exposes secrets
- CORS configurable via `CORS_ORIGINS`

### Weaknesses

- **API key is optional by default** — all endpoints are open if `PLEXMIND_API_KEY` is unset
- Plex token and TMDB key are plaintext in `.env` on disk
- `PLEXMIND_API_KEY` can be passed as query param (`?api_key=...`) — appears in proxy access logs
- Dashboard stores API key in browser `localStorage` (XSS risk if ever internet-exposed)
- Webhook LAN check is bypassable via reverse proxy
- Destructive maintenance ops (`pgs-cleanup`, `dedup`) delete files without per-file confirmation

### Recommendations

- Set `PLEXMIND_API_KEY` — the startup warning is loud but easy to dismiss
- Ensure `.env` is `chmod 600`
- Use HTTPS + nginx-proxy-manager for any access outside LAN
- Set `CORS_ORIGINS` to your proxy origin
- Always run `audit` before `pgs-cleanup` or `dedup`

---

## 9. Live System State

*Snapshot as of 2026-05-25*

| Item | State |
|---|---|
| LLM model | qwen3-4b-q4_k_m via llama.cpp OpenAI-compatible API |
| LLM health | `/health` returns `llm_ready: true` |
| LLM endpoint | `http://192.168.2.10:11435` externally, `http://llama-cpp:8080` in Docker network |
| GPU status | NVIDIA detected through Docker-socket fallback against `llama-cpp`; `/api/scheduler/status` returns `gpu_vendor: nvidia` |
| GPU utilization | 0% at verification time, threshold 30% |
| Recommendations | Live `GET /api/users/admin/recommendations?force=true` returns HTTP 200 with recommendation JSON |
| Translation | Script status returns HTTP 200; direct `/no_think` llama.cpp SRT smoke returns valid translated SRT |
| TVDB key | Not set |
| OMDB key | Not set |
| PLEXMIND_API_KEY | Set in `.env` and required by protected endpoints |
| Whisper | Not ready at verification time: `http://192.168.2.10:9001/asr` connection failed |
| Transcription window | 05:00-12:00 |
| Translation window | 23:00-03:00 |

### Current Fix State

The previous port 8000 UI and API failures were caused by stale Ollama/qwen3.5 references, oversized llama.cpp prompts, and Qwen reasoning output leaking into translation chunks. The live app now serves llama.cpp/qwen3-4b labels, caps recommendation prompt inputs, lowers max generation tokens to fit the 8192-token model context, preserves `/no_think` for every translation chunk, and reports NVIDIA GPU utilization even when the PlexMind app image does not include `nvidia-smi`. The 2026-06-07 release also fixes the Whisper large-audio crash path by extracting compressed 16 kHz mono MP3, segmenting uploads over 50 MB, adding a 12 GB Whisper sidecar memory limit, and moving bundled sidecar host ports to `11435` for llama.cpp and `9001` for Whisper so PlexMind does not contend with services using host `8080` or `9000`.

### Current Live Verification

The promoted 2026-06-07 containers expose PlexMind on host `8000`, llama.cpp on host `11435` mapped to container `8080`, and Whisper on host `9001` mapped to container `9000`. `bin/verify-live.sh` passed against `http://127.0.0.1:8000`, including `/health`, LLM readiness, GPU detection, translation script availability, recommendation smoke, and dashboard stale-label checks.

---

## 10. Known Bugs

### High Priority

| # | Location | Description |
|---|---|---|
| B1 | `transcribe.sh:449` | Bilingual VIP second-pass audio file may be deleted before translate pass if first extraction fails silently — results in missing one of the two language SRTs |
| B2 | Whisper HTTP 500 (live) | Fixed 2026-06-07: transcription now uploads compressed 16 kHz mono MP3 and segments payloads above 50 MB before calling Whisper; the sidecar also has a 12 GB memory cap. |

### Medium Priority

| # | Location | Description |
|---|---|---|
| B3 | dashboard GPU card | Detection source and probe errors are not surfaced in the UI; failures currently collapse to generic unavailable text |
| B4 | `recommender.py:105–127` | TMDB/TVDB/OMDB enrichment runs concurrently per candidate but has no circuit-breaker — API throttle on one service stalls the whole batch |
| B5 | `cache.py:35–52` | `_save_json_atomic()` fails silently if `/app/data` is read-only — no error logging to stderr |
| B6 | `llm_client.py:59–76` | JSON truncation repair assumes array; open objects like `{"title":..., "reason":...}` may be silently discarded |
| B7 | `plex_client.py:177–186` | Managed user token failure is silent — no fallback |

### Low Priority

| # | Location | Description |
|---|---|---|
| B8 | `main.py:526` | SSE keepalive fires every 30s — can cause buffering on slow proxy connections |
| B9 | `plex_client.py:192` | Watch history `maxresults=500` hardcoded — libraries >500 watched items are silently truncated |
| B10 | `recommender.py:85–98` | Full library scan on every rec generation — no caching of library structure between calls |

---

## 11. Recommendations & Improvements

Prioritized from highest-impact to polish.

---

### R1 — Fix Whisper OOM on Large Audio *(Completed 2026-06-07)*

Episodes that previously produced 80-90 MB uploads now go through compressed 16 kHz mono MP3 extraction (`TRANSCRIBE_AUDIO_CODEC=libmp3lame`, `TRANSCRIBE_AUDIO_BITRATE=64k`). If a payload still exceeds `WHISPER_UPLOAD_SPLIT_MB` (default 50 MB), `transcribe.sh` segments it into `WHISPER_SEGMENT_SECONDS` chunks (default 600 seconds), uploads each chunk, and stitches the SRT timestamps back together. Compose also gives the Whisper sidecar `WHISPER_MEM_LIMIT=12g`.

---

### R2 — Set PLEXMIND_API_KEY *(High / Security)*

The dashboard is currently protected in the live `.env`, but the application default remains open if `PLEXMIND_API_KEY` is omitted. Given Docker socket access, every deployment should set a strong key.

Keep set in `.env`:
```
PLEXMIND_API_KEY=<random 32-char hex>
```

---

### R3 — Cache Library Structure Between Rec Calls *(Medium / Performance)*

`recommender.py` performs a full Plex library scan on every `GET /api/users/{id}/recommendations` request, even for cached responses. The library scan should be memoized with invalidation on `library.new` webhook (which already fires cache clears — extend it to invalidate the library cache too).

**Expected impact:** Faster rec generation for subsequent users in a batch run. No disk I/O change.

---

### R4 — Add TVDB + OMDB API Keys *(Medium / Recommendation Quality)*

Currently unset in `.env`. TVDB enriches TV show metadata (network, status: ended/continuing), which meaningfully improves rec quality for TV shows — e.g., avoiding recommending a cancelled show at season 2 when you've already watched season 1.

OMDB adds IMDb/Rotten Tomatoes/Metascore data. The recommendation prompt already has slots for these fields — they just come back empty.

Both keys are free tier on their respective sites.

---

### R5 — Use TMDB Cast/Director Data in Scoring *(Medium / Recommendation Quality)*

`tmdb_client.py` fetches cast and director data but `recommender.py` never uses it. Adding director affinity (recurring director boost) and top-3-cast overlap scoring would significantly improve recs for users with auteur-heavy watch histories.

**Suggested implementation:** In the genre fingerprint builder, also accumulate director and top cast member counters (same recency weighting). In the candidate scoring function, add a `director_score` and `cast_score` alongside genre score.

---

### R6 — Graduate Feedback Penalties *(Low / Recommendation Quality)*

Currently a dislike is binary — all genres from a disliked title get a penalty regardless of how strongly the user disliked it. The feedback API already accepts a `rating` field. Use it: a 1-star dislike should apply a larger genre penalty than a 2-star one. This prevents over-penalization from mild dislikes.

---

### R7 — Paginate Watch History Beyond 500 Items *(Low / Correctness)*

`plex_client.py:192` caps at `maxresults=500`. Users with large libraries (years of watch history) silently lose older items. This biases the genre fingerprint toward recent watches — which may actually be desirable, but it should be a conscious decision, not an accidental truncation.

Change to paginate or increase the cap to 1000–2000 with a warning log if the limit is hit.

---

### R8 — Emit media.rate Webhook Events as Feedback *(Low / Automation)*

Plex fires `media.rate` events when a user rates something. `main.py:726–728` receives the event but takes no action. Automatically recording a Plex rating as PlexMind feedback would close the loop without requiring users to interact with the PlexMind dashboard.

---

### R9 — Add Structured Error Logging in Scripts *(Low / Observability)*

Many shell functions in `lib.sh` and `transcribe.sh` fail silently or with a generic `ERROR:` prefix. Adding error codes (e.g., `WHISPER_TIMEOUT`, `FFMPEG_EXTRACT_FAILED`, `API_OOM`) to the quarantine reason field and lifetime stats would make it much easier to diagnose recurring failures like B2.


---

### R10 - Add LLM Context Budgeting Telemetry *(High / Reliability)*

The 2026-05-25 recommendation failure was a llama.cpp HTTP 400 caused by a 9,411-token request exceeding the 8,192-token context. The current caps fix the immediate issue, but the app should log estimated prompt size, candidate count, history count, feedback count, and requested `max_tokens` for every LLM call. Add a warning when the calculated budget approaches 80% of model context.

### R11 - Add a Translation Smoke Test Job *(High / Reliability)*

Qwen can return reasoning-only output unless `/no_think` is preserved in every chunk. Add a lightweight `/api/scripts/translate/smoke` endpoint or script mode that sends a two-cue SRT and validates that the response contains timestamps and non-empty content. Run it after deployment and before long translation windows.

### R12 - Harden GPU Detection *(Medium / Operations)*

The app image does not include `nvidia-smi`, while the GPU-backed `llama-cpp` container does. The scheduler now falls back to Docker-socket exec against `LLAMA_CPP_CONTAINER_NAME`. Keep this behavior, but add visible diagnostics to the dashboard: detection source (`local`, `docker:llama-cpp`, or `none`) and the probe error if all methods fail.

### R13 - Add Build/Deploy Parity Checks *(Medium / Operations)*

The live fix required direct `docker cp` because compose tooling was unavailable. Add a small `bin/verify-live.sh` that checks container code version, `/health`, `/api/scheduler/status`, translation status, and a recommendation smoke. This prevents source and running container drift.

### R14 - Store GitHub Deployment State *(Low / Release Hygiene)*

Local `main` contains the llama.cpp fix set, but remote push was blocked by missing GitHub credentials. Track the deployed commit SHA in the dashboard or `/health` response so it is clear whether the running container, local repo, and GitHub remote agree.


### R15 - Add Top-Level Degraded-Service Status Strip *(Medium / UI)*

The dashboard has useful cards, but degraded subsystems are still too scattered. Add a compact status strip at the top showing API, LLM, GPU, Whisper, translation, and recommendations. Each item should show `ok`, `busy`, `degraded`, or `down`, with click-through to the relevant log or settings panel.

### R16 - Add Job Run History *(Medium / Observability)*

Persist a small job-run ledger for transcription, translation, maintenance, and recommendations: start time, duration, status, files scanned, processed, skipped, failed, exit code, and log path. The current live status is good, but debugging previous runs still requires reading raw logs.

### R17 - Add Recommendation Control Panel *(Medium / Product)*

Expose practical user controls without requiring env edits: movie/show ratio, new-vs-classic bias, language preference, genre include/exclude, and stronger `more like this` / `less like this` feedback. These controls should become structured inputs to candidate scoring before the LLM prompt, not only prompt prose.

### R18 - Add Model Settings and Last LLM Error Panel *(Medium / Operations)*

Show active llama.cpp URL, model alias, context limit, max tokens, GPU layers, candidate/history caps, and last LLM error. Keep editing these settings env-backed initially, but make the active runtime state visible in the UI.

### R19 - Explain Recommendation Evidence *(Low / Trust)*

The LLM reason text is useful but not auditable. Add a structured explanation drawer per recommendation showing matched genres, cast/director overlap, trend boost, feedback penalty, language match, and whether it was suppressed or reintroduced after the suppression window.


---

## 12. Future Features Backlog

These are worth considering but not blocking anything current.

| Feature | Value | Complexity |
|---|---|---|
| Translation batch resume | High — currently fails mid-batch with no recovery | Medium |
| Cast/director affinity scoring (R5 above) | High — improves recs meaningfully | Low |
| Batch transcription audio size pre-check (R1 above) | High — active bug fix | Low |
| User-configurable language preferences | Medium — separate from watch history language | Medium |
| Multiple llama.cpp model support (A/B or fallback) | Low — only one model in practice | High |
| Subtitle hard-subs (MKV remux) | Low — niche use case | High |
| Audit log for job runs | Medium — traceability | Low |
| Top-level degraded-service status strip | Medium — faster triage | Low |
| Recommendation preference controls | High — better user fit | Medium |
| Model settings and last-error panel | Medium — faster LLM debugging | Low |
| Structured recommendation evidence drawer | Medium — improves trust | Medium |
| Confidence threshold alerting (notify dashboard if quarantine > N files) | Medium — currently silent | Low |

---

## 13. Changelog

| Date | Change |
|---|---|
| 2026-06-07 | Fixed Whisper large-audio OOM/crash mitigation, moved PlexMind sidecar host ports to avoid conflicts, rebuilt/promoted live containers, and verified the suite with `bin/verify-live.sh`. |
| 2026-05-25 | Migrated design state to llama.cpp/qwen3-4b, documented live translation and recommendation fixes, added GPU detection fallback, and added R10-R19 hardening/UI recommendations. |
| 2026-05-07 | **All recommendations actioned.** See details below. |
| 2026-05-07 | Initial design doc created. Reviewed codebase at v0.8.17. Documented live state, bugs B1–B10, recommendations R1–R9. |

### 2026-06-07 - Changes Applied

**Bug fixes:**
- Large Whisper uploads now use compressed 16 kHz mono MP3 instead of uncompressed WAV.
- Uploads over 50 MB are split into 10-minute chunks and stitched back into one SRT.
- Whisper sidecar memory is capped at 12 GB to reduce crash risk during large ASR jobs.
- PlexMind sidecar host ports moved to llama.cpp `11435` and Whisper `9001`; internal Docker service ports remain `8080` and `9000`.
- Remaining install/template/setup drift from Ollama/qwen3.5 was corrected to llama.cpp/qwen3-4b defaults.

**Verified live:**
- Rebuilt `plexmind:latest` and `plexmind-scripts:latest`.
- Recreated `llama-cpp`, `whisper-asr-webservice`, `plexmind`, and `plexmind-scripts` with the new port map and env.
- `bin/verify-live.sh` passed: health, LLM readiness, GPU detection, translation status, recommendation smoke, and dashboard label checks.

**Release state:**
- Local release commit: `Fix Whisper upload stability and sidecar ports`.
- Local HTTPS `git push` is unavailable on this host because GitHub credentials are not configured; GitHub upload is performed through the GitHub connector.

### 2026-05-25 - Changes Applied

**Bug fixes:**
- Port 8000 dashboard no longer presents Ollama/qwen3.5 as the active LLM; live labels and health defaults now show llama.cpp and `qwen3-4b-q4_k_m`.
- Recommendations no longer overflow llama.cpp context. Prompt inputs are capped with `MAX_HISTORY_PROMPT_ITEMS`, `MAX_CANDIDATE_PROMPT_ITEMS`, and `MAX_FEEDBACK_PROMPT_ITEMS`; `LLAMA_CPP_MAX_TOKENS` defaults to 768.
- Translation chunks preserve `/no_think` even when previous-context text is included, preventing Qwen reasoning-only responses from producing empty subtitle output.
- GPU status on the dashboard now falls back to probing the GPU-backed `llama-cpp` container through the Docker socket when the `plexmind` app container lacks `nvidia-smi`.

**Verified live:**
- `/health` returns `llm_ready: true` for `qwen3-4b-q4_k_m`.
- `/api/scheduler/status` returns `gpu_vendor: nvidia` and a utilization percentage.
- `/api/scripts/translate/status` returns HTTP 200.
- `GET /api/users/admin/recommendations?force=true` returns HTTP 200 with recommendation JSON.

**Release state:**
- Local release commit: `Fix llama.cpp recommendation and translation engines` in the local `main` branch.
- GitHub push is blocked in the current environment by missing HTTPS credentials and missing SSH public-key access.


### 2026-05-07 — Changes Applied

**Bug fixes:**
- **B2 (root cause fix)** — `transcribe.sh`: `unk` language code was not mapped to empty in `normalize_lang_code`, causing Whisper API calls with `language=unk` → HTTP 500. Fixed: added `unk` to the `und|unknown|none → ""` case. Real Housewives of London (and any video with unknown language metadata) will now fall through to the AI language profiler correctly.
- **B3** — `scheduler.py`: Multi-GPU NVIDIA systems now average utilization across all GPUs instead of only reading the first GPU's value.
- **B5** — `cache.py`: `_save_json_atomic` now logs failures to stderr instead of silently swallowing exceptions.
- **B8** — `main.py`: SSE keepalive reduced from 30s to 10s to reduce proxy buffering on slow connections.
- **B9** — `plex_client.py`: Watch history limit raised from hardcoded 500 to configurable `HISTORY_LIMIT` (default 2000 via env var).

**Recommendations implemented:**
- **R2** — `PLEXMIND_API_KEY` set in container env. Dashboard and all API endpoints are now protected.
- **R3** — `recommender.py`: Full Plex library scan is now cached at module level (5-minute TTL). `clear_library_cache()` added and called on `library.new` webhook. Large libraries with multiple users no longer re-scan Plex on every rec generation.
- **R5** — `recommender.py`: Director and cast affinity scoring added to `_build_fingerprint` (returns 5 counters instead of 3) and `_score_candidate` (director boost up to 8%, cast boost up to 6%). Candidates from the same director/cast as heavily-watched titles now score meaningfully higher.
- **R6** — `recommender.py`: `_disliked_genres` now returns a Counter (genre → dislike frequency) instead of a flat set. Penalty is graduated: `0.1 × min(count, 3)` per genre overlap — a genre disliked across 3+ titles gets 3× the penalty of a genre disliked once. Binary penalty replaced.
- **R7** — `plex_client.py`: Watch history now fetches up to 2000 items (configurable via `HISTORY_LIMIT`). Libraries with >500 watch entries no longer silently truncate the history used for recommendations.
- **R8** — `main.py`: Plex `media.rate` webhook events are now wired as feedback. Ratings ≥ 7/10 → `like`, < 7/10 → `dislike`, stored in feedback.json with note `"Plex rating N/10"`. Cache invalidated on each rating event.

**Not actioned (requires user input):**
- **R4** — TVDB and OMDB API keys are free to obtain but require account registration. Set `TVDB_API_KEY` and `OMDB_API_KEY` in Unraid's Docker Manager for the `plexmind` container once obtained.

**Both images rebuilt and containers recreated** (`plexmind:latest`, `plexmind-scripts:latest`).

---

*To update this doc: edit `/mnt/cache/appdata/plexmind-suite/DESIGN.md` and add an entry to the changelog.*
