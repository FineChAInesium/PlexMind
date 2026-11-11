# Upgrading

[CHANGELOG.md](../CHANGELOG.md) lists every change. Notes for bigger steps:

## To 1.0

- Nothing to do. Your `PLEX_TOKEN`, `PLEXMIND_API_KEY` and webhook secret keep
  working and win over the new defaults. You can now sign in to the dashboard
  with Plex as the server owner.
- **Unraid:** if you installed from the template before v0.9.15, edit the
  container once and set Repository to `ghcr.io/finechainesium/plexmind:latest`,
  so Unraid offers updates.

## To v0.9.0: one container

v0.9.0 replaced the four PlexMind containers (`plexmind`,
`plexmind-recommendation-worker`, `plexmind-scripts`, `plexmind-docker-broker`)
with one `plexmind` container.

- **Compose:** `docker compose up -d --remove-orphans`.
- **Unraid or hand-made containers:** remove the four old containers, then create
  `plexmind` from the template with the old scripts worker's media mounts, the
  Docker socket and `--stop-timeout=40`. Keep the same `/app/data` folder.
- `docker exec plexmind-scripts ...` becomes `docker exec plexmind ...`.

## To v0.8.22

- llama.cpp and Whisper host ports bind to `127.0.0.1`. If Bazarr or another LAN
  client uses them, set `LLAMA_CPP_BIND_ADDRESS` / `WHISPER_BIND_ADDRESS` to `0.0.0.0`.
- Translation sends 8 cues per request (`TRANSLATION_BATCH_CUES`); `1` restores
  one per request.
- Rotate `PLEXMIND_WEBHOOK_SECRET` and the TMDB key if old container logs could
  have been read: earlier versions logged both.

## Version numbers

The project briefly published `v2.1.0` and `v2.1.1` while it was still being
hardened. Those numbers overstated its maturity, so the line was reset to
`v0.7.0` / `v0.7.1` and continued from there to 1.0. That was a renumbering, not a
code rollback.
