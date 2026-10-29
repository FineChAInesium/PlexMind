# Security Policy

## Reporting a Vulnerability

Open a private GitHub Security Advisory:

`github.com/FineChAInesium/PlexMind -> Security -> Report a vulnerability`

Do not open a public issue for security findings.

## Current Security Model

PlexMind is intended for trusted home-network or VPN access. It is not designed to be exposed directly to the public internet without an API key, HTTPS, and a reverse proxy you control.

### Secrets and configuration

PlexMind does not provide a config-read endpoint. There is no `/config`, `/settings`, or `/env` route that returns environment variables.

The following values are read from environment variables and are not returned by application endpoints:

- `PLEX_TOKEN`
- `PLEXMIND_API_KEY`
- `TMDB_API_KEY`
- `TVDB_API_KEY`
- `OMDB_API_KEY`

`/health` returns API status, LLM model name, and LLM readiness only.

### Authentication

`PLEXMIND_API_KEY` is required. Startup also requires distinct control, broker,
and webhook secrets.

Non-health endpoints require either:

- `X-API-Key: <key>` header, or
- a same-origin HttpOnly session cookie created by `POST /api/session`.

Key comparison uses `secrets.compare_digest`.

### Browser dashboard

The dashboard exchanges the API key for a persistent, same-origin HttpOnly session
cookie. It stores only the API base URL in `localStorage`; the raw key is cleared
from the form and is not persisted by browser JavaScript. Only one-way session-token
digests are saved under the persistent data directory. Sessions survive API restarts
and expire after 30 days by default (`PLEXMIND_SESSION_DAYS`). SSE uses the same cookie.

Security implications:

- Use HTTPS when accessing the dashboard through a reverse proxy, and set
  `PLEXMIND_SECURE_COOKIE=1` so the session cookie is marked `Secure`. Leave it
  at `0` for plain-HTTP LAN access, where browsers would drop a `Secure` cookie.
- Avoid sharing authenticated browser profiles with untrusted users.
- Usernames and streamed job details are HTML-escaped before rendering in the dashboard.

### CORS

`CORS_ORIGINS` defaults to empty, allowing same-origin dashboard use without
cross-origin API access. Add explicit origins only when required:

```bash
CORS_ORIGINS=https://plexmind.example.com
```

CORS is not an authentication boundary. Set `PLEXMIND_API_KEY`.

### Rate limiting

- `POST /api/run-all`: 3 requests/hour per IP
- `GET /api/users/{id}/recommendations`: 20 requests/minute per IP
- `POST /webhook`: 30 requests/minute per IP

### Webhook handling

`POST /webhook` is restricted to RFC 1918 LAN ranges and loopback. This is defense-in-depth only. If PlexMind is behind a reverse proxy, the application may see the proxy's private IP instead of the true client IP, so the LAN check can be bypassed by proxy topology.

Plex webhooks use the distinct `webhook_secret` query parameter configured by
`PLEXMIND_WEBHOOK_SECRET`; never place the general API key in a webhook URL.

PlexMind masks `webhook_secret` and other secret-looking query values (names
containing `secret`, `token`, `api_key`, `apikey`, `api-key` or `password`) as `***` in
uvicorn's access and error logs. The webhook URL itself still carries the
secret, so keep the endpoint LAN-only and keep proxy access logs private.

### Network exposure

In Docker Compose:

- PlexMind publishes port `8000`.
- llama.cpp publishes the configured `LLAMA_CPP_HOST_PORT` (default `11435`) on
  `LLAMA_CPP_BIND_ADDRESS` (default `127.0.0.1`). llama.cpp has no API key; PlexMind
  reaches it as `http://llama-cpp:8080` on the Docker network.
- Whisper publishes the configured `WHISPER_HOST_PORT` (default `9001`) on
  `WHISPER_BIND_ADDRESS` (default `127.0.0.1`); PlexMind uses `http://whisper:9000/asr`.
  Set `0.0.0.0` only if Bazarr or another LAN client needs the Whisper API.

The Unraid template expects an existing llama.cpp endpoint via `LLAMA_CPP_URL`, typically on the LAN.
If that endpoint is the Compose llama.cpp service, its host port must be bound to a LAN-reachable address.

### Containers and privileges

- The PlexMind API image runs as UID `1000`.
- The scripts image no longer installs Docker CLI or mounts `/var/run/docker.sock`.
- The scripts worker and Docker broker run with `no-new-privileges`. The scripts
  worker keeps its default capabilities because `SUBTITLE_UID`/`SUBTITLE_GID` need `chown`.
- Media folders are mounted into the scripts container so transcription, translation, and maintenance can write subtitle files.

### Destructive maintenance operations

The maintenance scripts can move or rewrite files inside mounted media folders:

- `pgs-cleanup` moves `.sup` files, and `.sub/.idx` pairs, to `DATA_DIR/quarantine/pgs`
  only when an SRT of the same title, language and kind replaces them: a forced or
  SDH track counts as replaced only by a forced or SDH SRT. Unknown-language image
  tracks are preserved unless explicitly opted in.
- `dedup` moves duplicate `.srt` files to `DATA_DIR/quarantine/dedup` after scoring cue count and size.
- `encoding` rewrites SRT files after conversion to UTF-8 and keeps the originals in
  `DATA_DIR/quarantine/encoding-backups`.
- Interrupted transcripts are moved to `DATA_DIR/quarantine/partials` instead of being published.

Quarantined files are never purged automatically. Run `maintenance.sh audit` first and keep backups if your media library does not have snapshot/backup coverage.

## Hardening Checklist

```bash
# Generate an API key
PLEXMIND_API_KEY=$(openssl rand -hex 32)

# Restrict browser origins behind a proxy
CORS_ORIGINS=https://plexmind.example.com

# Protect local secrets
chmod 600 .env

# Prefer VPN/Tailscale or a reverse proxy with HTTPS
# Do not raw port-forward :8000 to the internet.
```

Recommended reverse-proxy settings:

- force HTTPS
- avoid logging query strings on `/api/jobs/*/events` if using `?api_key=`
- pass WebSocket/SSE-friendly buffering settings for event streams
- require an API key even if the proxy has its own auth layer

## Audit Notes

| Area | Status |
|---|---|
| Config exposure | No config/env read endpoint. |
| API auth | Optional key, timing-safe comparison when set. |
| Heavy endpoints | Rate-limited. |
| Webhook | LAN check plus optional API key. API key required for proxied deployments. |
| Docker socket | Not mounted after the current audit. |
| Dashboard XSS | User and job-provided strings are escaped before dynamic rendering. |
| Secrets in logs | `httpx`/`httpcore` set to WARNING to reduce TMDB query-key log leakage; TMDB errors carry no URL; uvicorn logs mask secret query values. |
| File deletion | Limited to mounted media paths; maintenance modes move replaced or duplicate subtitles to `DATA_DIR/quarantine` instead of deleting them. |

## Huntarr Comparison

| Huntarr-style flaw | PlexMind status |
|---|---|
| `/api/settings` returned app keys | No equivalent endpoint exists. |
| Unauthenticated API by default | Closed: startup fails unless all scoped secrets are configured and distinct. |
| Container ran as root | API container runs as UID `1000`. |
| No rate limits | GPU-heavy and webhook endpoints are rate-limited. |
| Docker socket mounted | Removed from Compose scripts service. |
