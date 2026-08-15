#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
FIXTURE=$(mktemp -d)
trap 'rm -rf "$FIXTURE"' EXIT
mkdir -p "$FIXTURE/movies/Film" "$FIXTURE/tv" "$FIXTURE/data" "$FIXTURE/bin"

cat > "$FIXTURE/movies/Film/Film.en.srt" <<'EOF'
1
00:00:01,000 --> 00:00:03,000
Hello there.
EOF

cat > "$FIXTURE/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
out=""; wants_status=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w) wants_status=1; shift 2 ;;
    *) shift ;;
  esac
done
if [ -z "$out" ]; then printf '{"data":[]}'; exit 0; fi
count_file="${MOCK_COUNT_FILE}"
count=$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 ))
printf '%s\n' "$count" > "$count_file"
if { [ "${MOCK_FAIL_AFTER:-0}" -gt 0 ] && [ "$count" -gt "$MOCK_FAIL_AFTER" ]; } || { [ "${MOCK_FAIL_AFTER:-0}" -eq 0 ] && [ "$count" -eq 1 ]; }; then
  printf '%s' '{"choices":[{"finish_reason":"stop","message":{"content":"2\nUntranslated."}}]}' > "$out"
else
  printf '%s' '{"choices":[{"finish_reason":"stop","message":{"content":"Hola."}}]}' > "$out"
fi
[ "$wants_status" -eq 1 ] && printf '200'
EOF
chmod +x "$FIXTURE/bin/curl"

env PATH="$FIXTURE/bin:$PATH" MOCK_COUNT_FILE="$FIXTURE/count" \
  MOVIE_DIR="$FIXTURE/movies" TV_DIR="$FIXTURE/tv" DATA_DIR="$FIXTURE/data" \
  LOG_FILE="$FIXTURE/data/translation.log" LIFETIME_STATS_FILE="$FIXTURE/data/stats.env" \
  TARGET_LANGUAGES=es-MX RUN_NOW=1 MAX_RUNTIME_MINUTES=0 MANAGE_LLAMA_CPP_CONTAINER=0 \
  CHUNK_RETRY_ATTEMPTS=3 CHUNK_RETRY_DELAY_SECONDS=0 FAILED_RETRY_HOURS=24 \
  bash "$ROOT/scripts/translate.sh" >/dev/null

output="$FIXTURE/movies/Film/Film.es-MX.srt"
[ "$(cat "$FIXTURE/count")" -eq 2 ]
[ -s "$output" ]
grep -qF '00:00:01,000 --> 00:00:03,000' "$output"
grep -qF 'Hola.' "$output"
! grep -qF 'WARNING:' "$output"
! find "$FIXTURE/movies" -name '*.failed' -print -quit | grep -q .
! find "$FIXTURE/movies" -name '*.checkpoint' -print -quit | grep -q .

mkdir -p "$FIXTURE/movies/Failure"
cat > "$FIXTURE/movies/Failure/Failure.en.srt" <<'EOF'
1
00:00:01,000 --> 00:00:03,000
Hello there.

2
00:00:04,000 --> 00:00:06,000
General Kenobi.
EOF
rm -f "$FIXTURE/count"
set +e
env PATH="$FIXTURE/bin:$PATH" MOCK_COUNT_FILE="$FIXTURE/count" MOCK_FAIL_AFTER=1 \
  MOVIE_DIR="$FIXTURE/movies" TV_DIR="$FIXTURE/tv" DATA_DIR="$FIXTURE/data" \
  LOG_FILE="$FIXTURE/data/translation.log" LIFETIME_STATS_FILE="$FIXTURE/data/stats.env" \
  TARGET_LANGUAGES=es-MX RUN_NOW=1 MAX_RUNTIME_MINUTES=0 MANAGE_LLAMA_CPP_CONTAINER=0 \
  CHUNK_SIZE=1 CHUNK_RETRY_ATTEMPTS=3 CHUNK_RETRY_DELAY_SECONDS=0 FAILED_RETRY_HOURS=24 \
  bash "$ROOT/scripts/translate.sh" >/dev/null 2>&1
failure_rc=$?
set -e
[ "$failure_rc" -eq 2 ]
[ "$(cat "$FIXTURE/count")" -eq 4 ]
[ -s "$FIXTURE/movies/Failure/Failure.es-MX.temp" ]
[ -s "$FIXTURE/movies/Failure/Failure.es-MX.temp.checkpoint" ] || [ -f "$FIXTURE/movies/Failure/Failure.es-MX.temp.checkpoint" ]
[ -f "$FIXTURE/movies/Failure/Failure.es-MX.failed" ]
diagnostic=$(find "$FIXTURE/data/translation-failures" -type f -name '*.json' -print -quit)
[ -n "$diagnostic" ]
[ "$(stat -c '%a' "$diagnostic")" = "600" ]

echo "translation retry tests passed"
