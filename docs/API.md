# API, command line and development

## API

Interactive docs: `http://<server>:8000/docs`

Scripts and other tools authenticate with `X-API-Key`. The key is
`PLEXMIND_API_KEY`, or the one PlexMind generated on first start:

```bash
docker exec plexmind cat /app/data/secrets.json
```

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | API, Plex and LLM readiness; `llm_state` is `ready`, `standby`, `starting` or `fault`. |
| `/health/live` | GET | Liveness only. |
| `/api/auth/status` | GET | Whether this browser is signed in, and whether Plex is connected. |
| `/api/auth/plex/start`, `/poll`, `/server` | POST | Sign in with Plex (used by the dashboard). |
| `/api/session` | POST/DELETE | Exchange the API key for a session cookie, or sign out. `DELETE ?everywhere=true` signs out every browser. |
| `/api/users` | GET | Plex users. |
| `/api/users/{id}/history` | GET | Watch history. |
| `/api/users/{id}/recommendations?force=true` | GET | Generate or fetch picks. |
| `/api/users/{id}/feedback` | GET/POST | Like, dislike or watched feedback. |
| `/api/users/{id}/sync` | POST/DELETE | Send picks to Plex, or remove them. |
| `/api/run-all` | POST | Start the all-user batch (3 per hour). |
| `/api/jobs/{job_id}/status`, `/events` | GET | Batch status, or a live SSE stream. |
| `/api/models` | GET/POST | Models available, and choose one. |
| `/api/models/trial` | GET/POST | Model Trial results, or start one. |
| `/api/model-advisor` | GET | Built-in model suggestions ranked for this GPU. |
| `/api/scheduler/status`, `/configure` | GET/POST | Next run, GPU readings; change the monthly schedule. |
| `/api/scripts/{job}/status`, `/log`, `/start`, `/stop` | GET/POST | Transcription, translation and maintenance jobs. |
| `/api/maintenance/report` | GET | Latest maintenance audit report. |
| `/webhook?webhook_secret=...` | POST | Plex webhook (LAN only). |

```bash
curl -H "X-API-Key: $KEY" http://192.168.1.10:8000/api/scheduler/status
curl -X POST -H "X-API-Key: $KEY" http://192.168.1.10:8000/api/run-all
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  --data '{"run_now":true,"max_runtime_minutes":60}' \
  http://192.168.1.10:8000/api/scripts/transcribe/start
```

## Command line

The dashboard runs the jobs on schedule; the scripts can also be run directly.

```bash
docker exec -e RUN_NOW=1 plexmind /app/transcribe.sh   # now, ignoring the window
docker exec -e RUN_NOW=1 plexmind /app/translate.sh
docker exec plexmind /app/stop-job.sh translate        # stops cleanly; resumes next run
docker exec plexmind /app/maintenance.sh audit         # also: dedup, pgs-cleanup, ordering, all
```

Logs are dated files in `/app/data/logs`, kept for `LOG_RETENTION_DAYS` (7).
When llama.cpp's reply to a translation batch cannot be used, the log says
`Batch of cues X-Y unusable (reason=...)`, the reply is saved under
`/app/data/translation-failures/`, and those lines are translated one at a time.

Maintenance never deletes: removed or replaced subtitles go to
`/app/data/quarantine/`. Empty it yourself once you are happy with the result.

## Development

```bash
cd plexmind
pip install -r requirements.txt
uvicorn app.main:app --reload          # API only, against your .env
npm install && npm run build:css       # after changing Tailwind classes in index.html
```

Tests: `pytest` from the repository root, with `plexmind` on `PYTHONPATH`.
