# Security Policy

## Reporting a Vulnerability

Open a **private** GitHub Security Advisory:  
`github.com/FineChAInesium/PlexMind → Security → Report a vulnerability`

Do not open a public issue for security findings.

## Architecture — What PlexMind Does and Doesn't Expose

### What is never exposed
- `PLEX_TOKEN`, `TMDB_API_KEY`, `OMDB_API_KEY`, `PLEXMIND_API_KEY` — never returned by any endpoint
- `/health` returns only status booleans and the model name
- There is no `/config`, `/settings`, or `/env` endpoint — this was the critical flaw in the Huntarr incident
- Ollama and Whisper are on an internal Docker network, not published to the LAN

### Outbound requests
- **Plex** — via `plexapi` library using `X-Plex-Token` header, never in URLs
- **TMDB/OMDB** — API key in query param (TMDB v3 does not support header auth); httpx logging is suppressed at WARNING level to prevent key appearing in logs
- **Ollama** — internal Docker network only (`http://ollama:11434`)

### Authentication
- `PLEXMIND_API_KEY` is optional but strongly recommended
- If not set, a WARNING is logged at every startup
- When set, all non-health endpoints require `X-API-Key` header or `?api_key=` query param
- Key comparison uses `secrets.compare_digest` (timing-safe)
- The dashboard reads the key from localStorage and sends it automatically

### Rate limiting
- `POST /api/run-all` — 3 requests/hour per IP
- `GET /api/users/{id}/recommendations` — 20 requests/minute per IP
- `POST /webhook` — 30 requests/minute per IP

### Webhook defence-in-depth
- `POST /webhook` is additionally restricted to LAN IP ranges (RFC 1918 + loopback)
- Rejects any request from a non-private IP regardless of key

## Hardening Recommendations

```bash
# 1. Set an API key (generate once)
echo "PLEXMIND_API_KEY=$(openssl rand -hex 32)" >> .env

# 2. Restrict CORS to your LAN IP
echo "CORS_ORIGINS=http://192.168.x.x:8000" >> .env

# 3. Lock down the .env file
chmod 600 .env

# 4. Only expose PlexMind via Tailscale — do not port-forward to internet
```

## v2.0.1 Security Improvements

The following hardening was applied after an independent security audit:

| Fix | Detail |
|---|---|
| Timing-safe key comparison | `secrets.compare_digest` prevents timing oracle attacks on API key validation |
| Rate limiting | `/api/run-all` 3/hr, `/api/users/{id}/recommendations` 20/min, `/webhook` 30/min |
| Webhook LAN-only | RFC 1918 + loopback allowlist rejects any non-private source IP regardless of key |
| HTTP log suppression | `httpx` and `httpcore` loggers set to WARNING — TMDB `api_key` query param never appears in logs |
| Startup warning | Loud `WARNING` log at every start if `PLEXMIND_API_KEY` is not set |
| Input validation | `user_id` path params validated against `^[a-zA-Z0-9_@.\- ]{1,60}$` — rejects path traversal and injection attempts |

## Huntarr Comparison

PlexMind was designed with the Huntarr incident in mind:

| Huntarr flaw | PlexMind |
|---|---|
| `/api/settings` returned all *arr keys in cleartext | No config-read endpoint exists |
| All endpoints unauthenticated by default | Optional key; loud startup warning if unset |
| Ran as root in container | Runs as uid 1000 |
| No rate limiting | slowapi rate limits on GPU-heavy endpoints |
| Docker socket mounted | `/var/run/docker.sock` is not mounted — PlexMind cannot control other containers or escalate to host root |
