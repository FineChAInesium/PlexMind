#!/usr/bin/env bash
# ==============================================================================
# PlexMind Scripts Container Entrypoint
# Keeps the container alive and runs transcription/translation on schedule.
# ==============================================================================
set -u

LOG_DIR="/app/data/logs"
mkdir -p "$LOG_DIR"

echo "$(date) - PlexMind Scripts container started."
echo "  Whisper API:  ${WHISPER_API_URL:-not set}"
echo "  Ollama API:   ${OLLAMA_API_URL:-not set}"
echo "  Ollama Model: ${OLLAMA_MODEL:-not set}"
echo "  Movies:       ${MOVIES_DIR:-/media/movies}"
echo "  TV Shows:     ${TV_DIR:-/media/tv}"
echo "  Languages:    ${TARGET_LANGUAGES:-zh,es-MX}"
echo ""
echo "Run scripts manually:"
echo "  docker exec plexmind-scripts /app/transcribe.sh"
echo "  docker exec plexmind-scripts /app/translate.sh"
echo "  docker exec plexmind-scripts /app/maintenance.sh all"
echo "  docker exec plexmind-scripts /app/stop-job.sh transcribe"
echo "  docker exec plexmind-scripts /app/stop-job.sh translate"
echo "  API: http://plexmind-scripts:9010/health"
echo ""

# Serve script controls and keep the container alive
exec python3 /app/control_server.py
