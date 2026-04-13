# Changelog

## v0.8.2 - 2026-04-13

- Protect the one-time playlist migration endpoint with the configured PlexMind API key.
- Render dashboard toast messages as text content to avoid HTML injection from dynamic error strings.
- Serialize JSON persistence updates with a shared lock and atomic temp-file replacement.
- Raise the GUI script-start rate-limit default from `5/hour` to configurable `SCRIPT_START_RATE_LIMIT`, default `60/hour`, to avoid normal Unraid retries hitting HTTP 429.
- Fall back to the Docker bridge gateway for Whisper when legacy bridge DNS cannot resolve `whisper` or `whisper-asr-webservice`.

## v0.8.1 - 2026-04-13

- Align the dashboard, FastAPI metadata, package metadata, and script runtime banners on the `v0.8.1` release line.
- Replace the old standalone script `2.0` labels with PlexMind release-line versions so script logs and docs no longer imply a separate production-ready major version.

## v0.8.0 - 2026-04-12

- Add a live Whisper ASR health probe to `/health` using the configured `WHISPER_API_URL`.
- Replace the static Whisper dashboard placeholder with real Ready/Offline/Not checked state.
- Show an explicit pending-restart state when the dashboard is newer than the running API process.

## v0.7.1 - 2026-04-12

- Make script job log polling resilient to transient API/proxy disconnects by backing off and resuming instead of appending repeated `Failed to fetch` lines every 3 seconds.
- Keep already-running transcribe/translate jobs attached to the existing log poller without raw 409 JSON or duplicate polling intervals.
- Fix maintenance already-running handling so maintenance jobs show a toast instead of referencing the transcribe/translate log element.

## v0.7.0 - 2026-04-12

- Run transcription, translation, and maintenance jobs directly from the PlexMind API container by default, with the optional scripts sidecar still supported via `PLEXMIND_SCRIPT_MODE=sidecar`.
- Package `/app/scripts` plus `curl`, `jq`, and `ffmpeg` into the API image, and mount Movies/TV paths into the API service for GUI-launched script jobs.
- Fix recommendation generation in the dashboard so long runs do not trip a 60-second timeout and incorrectly flip the UI into demo mode.
- Persist generated recommendation history and load the Recent Recommendations section from real generated results instead of mock/demo data.
- Add maintenance job execution through the GUI with confirmation prompts for destructive dedupe, PGS cleanup, and all-maintenance runs.
- Update the live container to `mode=local` script execution with `/app/scripts`, `/media/movies`, `/media/tv`, `curl`, `jq`, and `ffmpeg` available.
- Harden the default browser surface by disabling wildcard CORS unless `CORS_ORIGINS` is explicitly configured.
- Fall back from run-all SSE to job-status polling when the browser/proxy drops the job stream.
- Treat already-running transcribe/translate jobs as attach-to-log states in the dashboard instead of showing raw 409 JSON.
- Stop duplicating script log lines on future local/sidecar script starts by letting scripts write `LOG_FILE` once and sending stderr to the log.
