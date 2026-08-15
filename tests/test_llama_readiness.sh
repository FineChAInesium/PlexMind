#!/bin/bash
set -eu

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT
mkdir -p "$TEMP_ROOT/movies" "$TEMP_ROOT/tv" "$TEMP_ROOT/data"
touch "$TEMP_ROOT/movies/example.mkv"

curl() {
    local count=0
    [ -f "$CURL_COUNT_FILE" ] && count=$(<"$CURL_COUNT_FILE")
    count=$((count + 1))
    printf '%s' "$count" > "$CURL_COUNT_FILE"
    [ "$count" -ge 3 ]
}
export -f curl

export CURL_COUNT_FILE="$TEMP_ROOT/curl-count"
export MOVIE_DIR="$TEMP_ROOT/movies"
export TV_DIR="$TEMP_ROOT/tv"
export LOG_FILE="$TEMP_ROOT/data/translation.log"
export LIFETIME_STATS_FILE="$TEMP_ROOT/data/translation_stats.env"
export REPORT_DIR="$TEMP_ROOT/data/reports"
export MANAGE_LLAMA_CPP_CONTAINER=0
export LLAMA_STARTUP_ATTEMPTS=3
export LLAMA_STARTUP_INTERVAL_SECONDS=0
export TARGET_LANGUAGES=zh

bash "$ROOT_DIR/scripts/translate.sh"
[ "$(<"$CURL_COUNT_FILE")" -eq 3 ]
grep -q 'llama.cpp API ready after 3 probe(s)' "$TEMP_ROOT/data/logs/translation-"*.log

echo "llama readiness tests passed"
