#!/usr/bin/env bash
# Host-side GUI/settings audit. Writes a secret-free snapshot consumed by the
# normal maintenance audit report.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-${ROOT}/data/gui-settings-audit.txt}"
mkdir -p "$(dirname "$OUT")"

status() { printf '  [%s] %s\n' "$1" "$2"; }
bytes_for() { stat -f -c '%S %b %a' "$1" 2>/dev/null | awk '{printf "%.0f %.0f %.0f\n", $1*$2, $1*($2-$3), $1*$3}'; }
human_tb() { awk -v n="${1:-0}" 'BEGIN { printf "%.1f TB", n/1000000000000 }'; }

MEDIA_PATH="$(sed -n 's/^MOVIES_HOST_PATH=//p' "$ROOT/.env" | tail -n1)"
[ -n "$MEDIA_PATH" ] || MEDIA_PATH="/mnt/user/data/media/Movies"
APPDATA_PATH="$ROOT/data"
read -r MEDIA_TOTAL MEDIA_USED MEDIA_FREE <<< "$(bytes_for "$MEDIA_PATH")"
read -r DATA_TOTAL DATA_USED DATA_FREE <<< "$(bytes_for "$APPDATA_PATH")"

API_STORAGE="$(docker exec plexmind python -c "import os,urllib.request; r=urllib.request.Request('http://127.0.0.1:8000/api/storage',headers={'X-API-Key':os.getenv('PLEXMIND_API_KEY','')}); print(urllib.request.urlopen(r,timeout=3).read().decode())" 2>/dev/null || true)"
API_TOTAL="$(printf '%s' "$API_STORAGE" | jq -r '.total_bytes // 0' 2>/dev/null || echo 0)"

LIVE_MODEL="$(docker exec llama-cpp sh -c 'curl -fsS http://127.0.0.1:8080/v1/models' 2>/dev/null | jq -r '.data[0].id // empty' 2>/dev/null)"
ENV_MODEL="$(sed -n 's/^LLAMA_CPP_MODEL_ALIAS=//p' "$ROOT/.env" | tail -n1)"
RUNTIME_MODEL="$(docker inspect plexmind --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | sed -n 's/^LLAMA_CPP_MODEL=//p' | tail -n1)"

MAIN_MOUNTS="$(docker inspect plexmind --format '{{range .Mounts}}{{println .Destination}}{{end}}' 2>/dev/null || true)"
SCRIPTS_MOUNTS="$(docker inspect plexmind-scripts --format '{{range .Mounts}}{{println .Destination}}{{end}}' 2>/dev/null || true)"

SOURCE_MAIN_SHA="$(sha256sum "$ROOT/plexmind/app/main.py" 2>/dev/null | awk '{print $1}')"
RUNTIME_MAIN_SHA="$(docker exec plexmind sha256sum /app/app/main.py 2>/dev/null | awk '{print $1}')"
SOURCE_UI_SHA="$(sha256sum "$ROOT/plexmind/app/static/index.html" 2>/dev/null | awk '{print $1}')"
RUNTIME_UI_SHA="$(docker exec plexmind sha256sum /app/app/static/index.html 2>/dev/null | awk '{print $1}')"

{
    echo "## GUI AND SETTINGS AUDIT"
    echo "Generated: $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo ""
    echo "### Storage reporting"
    status INFO "Media filesystem: $(human_tb "$MEDIA_USED") used / $(human_tb "$MEDIA_TOTAL") total; $(human_tb "$MEDIA_FREE") free."
    status INFO "Appdata filesystem: $(human_tb "$DATA_USED") used / $(human_tb "$DATA_TOTAL") total; $(human_tb "$DATA_FREE") free."
    if [ "$API_TOTAL" -gt 0 ] && [ "$API_TOTAL" -lt $(( MEDIA_TOTAL * 9 / 10 )) ]; then
        status FAIL "GUI /api/storage reports $(human_tb "$API_TOTAL"), matching appdata/cache rather than the media filesystem."
    elif [ "$API_TOTAL" -gt 0 ]; then
        status PASS "GUI storage capacity is consistent with the media filesystem."
    else
        status FAIL "GUI /api/storage could not be queried."
    fi
    echo ""
    echo "### Runtime configuration drift"
    [ -n "$LIVE_MODEL" ] && status PASS "llama.cpp is reachable; live model alias: ${LIVE_MODEL}." || status FAIL "llama.cpp model inventory is unavailable."
    [ "$ENV_MODEL" = "$LIVE_MODEL" ] && status PASS ".env model alias matches llama.cpp." || status FAIL ".env model alias '${ENV_MODEL:-unset}' differs from live '${LIVE_MODEL:-unavailable}'."
    [ "$RUNTIME_MODEL" = "$ENV_MODEL" ] && status PASS "PlexMind runtime model label matches .env." || status FAIL "PlexMind runtime label '${RUNTIME_MODEL:-unset}' is stale versus .env '${ENV_MODEL:-unset}'; container recreation is required."
    [ "$SOURCE_MAIN_SHA" = "$RUNTIME_MAIN_SHA" ] && status PASS "Running API code matches the workspace." || status FAIL "Running API code differs from the workspace image."
    [ "$SOURCE_UI_SHA" = "$RUNTIME_UI_SHA" ] && status PASS "Running GUI matches the workspace." || status FAIL "Running GUI differs from the workspace image."
    echo ""
    echo "### Mount and path contracts"
    printf '%s\n' "$SCRIPTS_MOUNTS" | grep -qx '/media/movies' && status PASS "Scripts worker has the movies mount." || status FAIL "Scripts worker is missing /media/movies."
    printf '%s\n' "$SCRIPTS_MOUNTS" | grep -qx '/media/tv' && status PASS "Scripts worker has the TV mount." || status FAIL "Scripts worker is missing /media/tv."
    if [ "$API_TOTAL" -ge $(( MEDIA_TOTAL * 9 / 10 )) ]; then
        status PASS "PlexMind obtains media capacity through the least-privilege scripts API."
    elif printf '%s\n' "$MAIN_MOUNTS" | grep -qx '/media/movies'; then
        status PASS "PlexMind API has a media mount available for storage reporting."
    else
        status FAIL "PlexMind sets MOVIE_DIR/TV_DIR but mounts neither path; storage reporting can only see appdata."
    fi
    echo ""
    echo "### GUI security and input contracts"
    if grep -q "localStorage.setItem.*apiKey" "$ROOT/plexmind/app/static/index.html"; then
        status FAIL "GUI persists the raw API key in browser localStorage after creating an HttpOnly session cookie."
    else
        status PASS "GUI does not persist the raw API key in localStorage."
    fi
    if grep -q '@field_validator("target_languages")' "$ROOT/plexmind/app/main.py" && grep -q 'BCP-47 language tags' "$ROOT/scripts/control_server.py"; then
        status PASS "Translation target languages have server-side validation."
    else
        status FAIL "Translation target languages are accepted as an unrestricted string; allowlist/format validation is missing."
    fi
    if grep -q "This saves the cron line in this browser" "$ROOT/plexmind/app/static/index.html"; then
        status WARN "Schedule widgets store drafts in one browser and do not install or verify host cron entries."
    fi
    status INFO "Whisper model control is inventory-only and disabled; changing the displayed selection does not reconfigure the sidecar."
    echo ""
    echo "### Remaining operator notes"
    echo "  1. Cron widgets are command builders; install generated commands through Unraid to activate them."
    echo "  2. Whisper model changes remain an environment/container-recreation operation."
} > "$OUT"

chmod 600 "$OUT"
printf '%s\n' "$OUT"
