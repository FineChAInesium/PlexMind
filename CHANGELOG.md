# Changelog

## v0.8.12 - 2026-04-14

- Use primary audio stream language metadata before falling back to Whisper profiling so Japanese audio is not forced through English transcription.
- Infer profiler language from Whisper JSON text when the API omits an explicit language field.
- Score transcription confidence with the detected language instead of always forcing English.

## v0.8.11 - 2026-04-14

- Serve the existing PlexMind icon at `/favicon.ico` so browser favicon requests no longer return 404.

## v0.8.10 - 2026-04-13

- Replace the Logs grid with a job dropdown so only the selected script log is loaded.
- Default the Logs page to a running job or the most recently updated log session.
- Cap dashboard and API log reads at 500 lines to avoid large log payloads.

## v0.8.9 - 2026-04-13

- Start the Whisper sidecar container before transcription and stop it when transcription exits or is terminated.
- Start the Ollama sidecar container before translation and stop it when translation exits or is terminated.
- Add Docker socket access for PlexMind-managed scripts so sidecar lifecycle control works from containerized runs.

## v0.8.8 - 2026-04-13

- Use the PlexMind logo asset in the dashboard sidebar brand mark.

## v0.8.7 - 2026-04-13

- Add the PlexMind Unraid icon asset used by the template.
- Add Unraid Docker labels for WebUI and icon discovery so existing containers can show the WebUI dropdown entry after recreation.

## v0.8.6 - 2026-04-13

- Replace the redundant Jobs sidebar page with a Logs page that tails PlexMind-managed script logs from transcription, translation, and maintenance jobs.
- Keep old `#/jobs` links routed to the new Logs page.

## v0.8.5 - 2026-04-13

- Fix the Unraid Docker template WebUI target so the Docker tab opens the PlexMind dashboard at `/` instead of the API docs path.
- Update the PlexMind template release metadata to the `v0.8.5` release line.

## v0.8.4 - 2026-04-13

- Add hash-routed dashboard pages so sidebar pages reload back to their current page instead of resetting to Dashboard.
- Add a Jobs sidebar page backed by `/api/scripts/jobs` for PlexMind-controlled script job status.
- Add job catalog metadata in the local PlexMind runner for titles, groups, descriptions, page targets, destructive flags, log presence, and running state.

## v0.8.3 - 2026-04-13

- Route generated Unraid schedule helper commands through the PlexMind `/api/scripts/{job}/start` API so scheduled script runs share PlexMind status, logs, limits, and stop controls.
- Fix transcription confidence parsing so timestamped log output from `score_confidence` cannot be captured as the numeric score.
- Align script headers and runtime log banners on the `v0.8.3` PlexMind release line.

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
