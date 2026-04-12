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

`PLEXMIND_API_KEY` is optional for LAN-only installs, but strongly recommended.

When set, non-health endpoints require either:

- `X-API-Key: <key>` header, or
- `?api_key=<key>` query param for clients that cannot set headers, such as Plex webhooks and browser `EventSource`.

Key comparison uses `secrets.compare_digest`.

If `PLEXMIND_API_KEY` is not set, PlexMind logs a startup warning and all non-health endpoints are open to clients that can reach the service.

### Browser dashboard

The dashboard stores the API key in browser `localStorage` and sends it as `X-API-Key` for normal requests. The SSE job stream uses `?api_key=` because native `EventSource` cannot set custom headers.

Security implications:

- Use HTTPS when accessing the dashboard through a reverse proxy.
- Avoid sharing browser profiles with untrusted users.
- Be aware that query strings can appear in reverse-proxy access logs unless logging is configured carefully.
- Usernames and streamed job details are HTML-escaped before rendering in the dashboard.

### CORS

`CORS_ORIGINS` defaults to `*` for easy LAN setup. For a proxied or internet-reachable deployment, set it to your dashboard origin:

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

If you expose PlexMind through a proxy, set `PLEXMIND_API_KEY` and include `?api_key=<key>` in the Plex webhook URL.

### Network exposure

In Docker Compose:

- PlexMind publishes port `8000`.
- Ollama publishes `127.0.0.1:11434` only.
- Whisper is exposed only on the internal Compose network unless started with a different override.

The Unraid template expects an existing Ollama endpoint via `OLLAMA_URL`, typically on the LAN.

### Containers and privileges

- The PlexMind API image runs as UID `1000`.
- The scripts image no longer installs Docker CLI or mounts `/var/run/docker.sock`.
- Media folders are mounted into the scripts container so transcription, translation, and maintenance can write subtitle files.

### Destructive maintenance operations

The maintenance scripts can delete files inside mounted media folders:

- `pgs-cleanup` deletes `.sup` files, and `.sub/.idx` pairs, when matching SRTs exist.
- `dedup` removes duplicate `.srt` files after scoring cue count and size.
- `encoding` rewrites SRT files after conversion to UTF-8.

Run `maintenance.sh audit` first and keep backups if your media library does not have snapshot/backup coverage.

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
| Secrets in logs | `httpx`/`httpcore` set to WARNING to reduce TMDB query-key log leakage. |
| File deletion | Limited to mounted media paths, but maintenance modes intentionally delete subtitle files. |

## Huntarr Comparison

| Huntarr-style flaw | PlexMind status |
|---|---|
| `/api/settings` returned app keys | No equivalent endpoint exists. |
| Unauthenticated API by default | Still optional for LAN convenience; startup warning logs loudly. Set `PLEXMIND_API_KEY`. |
| Container ran as root | API container runs as UID `1000`. |
| No rate limits | GPU-heavy and webhook endpoints are rate-limited. |
| Docker socket mounted | Removed from Compose scripts service. |
