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

mkdir -p "$FIXTURE/movies/Invariant"
cat > "$FIXTURE/movies/Invariant/Invariant.en.srt" <<'EOF'
1
00:00:01,000 --> 00:00:03,000
Frida Kahlo.

2
00:00:04,000 --> 00:00:06,000
No, no.
EOF

cat > "$FIXTURE/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
out=""; wants_status=0; payload=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w) wants_status=1; shift 2 ;;
    -d) payload="${2#@}"; shift 2 ;;
    *) shift ;;
  esac
done
if [ -z "$out" ]; then printf '{"data":[]}'; exit 0; fi
count_file="${MOCK_COUNT_FILE}"
count=$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 ))
printf '%s\n' "$count" > "$count_file"
if [ -n "${MOCK_PAYLOAD_LOG:-}" ] && [ -n "$payload" ]; then
  jq -r '.messages[-1].content' "$payload" >> "$MOCK_PAYLOAD_LOG"
fi
if [ -n "${MOCK_HTTP_STATUS:-}" ]; then
  printf '%s' '{"error":{"code":500,"message":"boom"}}' > "$out"
  [ "$wants_status" -eq 1 ] && printf '%s' "$MOCK_HTTP_STATUS"
  exit 0
fi
if [ "${MOCK_ZH:-0}" = 1 ]; then
  printf '%s' '{"choices":[{"finish_reason":"stop","message":{"content":"你好。"}}]}' > "$out"
elif [ "$count" -le "${MOCK_FAIL_FIRST:-0}" ]; then
  # Echo the English line back untranslated.
  jq -n --arg c "$(jq -r '.messages[-1].content' "$payload")" \
    '{choices:[{finish_reason:"stop",message:{content:$c}}]}' > "$out"
elif [ "${MOCK_INVARIANT:-0}" = 1 ]; then
  case "$count" in 1) content='Frida Kahlo.' ;; *) content='No, no.' ;; esac
  printf '%s' "{\"choices\":[{\"finish_reason\":\"stop\",\"message\":{\"content\":\"$content\"}}]}" > "$out"
elif { [ "${MOCK_FAIL_AFTER:-0}" -gt 0 ] && [ "$count" -gt "$MOCK_FAIL_AFTER" ]; } || { [ "${MOCK_FAIL_AFTER:-0}" -eq 0 ] && [ "$count" -eq 1 ]; }; then
  printf '%s' '{"choices":[{"finish_reason":"stop","message":{"content":"2\nUntranslated."}}]}' > "$out"
else
  printf '%s' '{"choices":[{"finish_reason":"stop","message":{"content":"Hola."}}]}' > "$out"
fi
[ "$wants_status" -eq 1 ] && printf '200'
EOF
chmod +x "$FIXTURE/bin/curl"

env PATH="$FIXTURE/bin:$PATH" MOCK_COUNT_FILE="$FIXTURE/count" \
  MOVIE_DIR="$FIXTURE/movies/Film" TV_DIR="$FIXTURE/tv" DATA_DIR="$FIXTURE/data" \
  LOG_FILE="$FIXTURE/data/translation.log" LIFETIME_STATS_FILE="$FIXTURE/data/stats.env" \
  TARGET_LANGUAGES=es-MX RUN_NOW=1 MAX_RUNTIME_MINUTES=0 MANAGE_LLAMA_CPP_CONTAINER=0 \
  CHUNK_RETRY_ATTEMPTS=3 CHUNK_RETRY_DELAY_SECONDS=0 FAILED_RETRY_HOURS=24 \
  bash "$ROOT/scripts/translate.sh" >/dev/null

output="$FIXTURE/movies/Film/Film.es-MX.srt"
[ "$(cat "$FIXTURE/count")" -eq 2 ]
[ -s "$output" ]
grep -qF '00:00:01,000 --> 00:00:03,000' "$output"
grep -qF 'Hola.' "$output"
# `! cmd` is exempt from `set -e`, so negative assertions must exit explicitly.
fail() { echo "FAIL: $*" >&2; exit 1; }
if grep -qE ' - (WARNING|ERROR|INFO):' "$output"; then fail "log line leaked into $output"; fi
if [ -n "$(find "$FIXTURE/movies" -name '*.failed' -print -quit)" ]; then fail "unexpected .failed marker"; fi
if [ -n "$(find "$FIXTURE/movies" -name '*.checkpoint' -print -quit)" ]; then fail "unexpected checkpoint"; fi

rm -f "$FIXTURE/count"
env PATH="$FIXTURE/bin:$PATH" MOCK_COUNT_FILE="$FIXTURE/count" MOCK_INVARIANT=1 \
  MOVIE_DIR="$FIXTURE/movies/Invariant" TV_DIR="$FIXTURE/tv" DATA_DIR="$FIXTURE/data" \
  LOG_FILE="$FIXTURE/data/invariant.log" LIFETIME_STATS_FILE="$FIXTURE/data/invariant-stats.env" \
  TARGET_LANGUAGES=es-MX RUN_NOW=1 MAX_RUNTIME_MINUTES=0 MANAGE_LLAMA_CPP_CONTAINER=0 \
  CHUNK_RETRY_ATTEMPTS=3 CHUNK_RETRY_DELAY_SECONDS=0 FAILED_RETRY_HOURS=24 \
  bash "$ROOT/scripts/translate.sh" >/dev/null
[ "$(cat "$FIXTURE/count")" -eq 2 ]
grep -qF 'Frida Kahlo.' "$FIXTURE/movies/Invariant/Invariant.es-MX.srt"
grep -qF 'No, no.' "$FIXTURE/movies/Invariant/Invariant.es-MX.srt"

run_translate() {
  local dir="$1"; shift
  rm -f "$FIXTURE/count"
  set +e
  env PATH="$FIXTURE/bin:$PATH" MOCK_COUNT_FILE="$FIXTURE/count" "$@" \
    MOVIE_DIR="$dir" TV_DIR="$FIXTURE/tv" DATA_DIR="$FIXTURE/data" \
    LOG_FILE="$FIXTURE/data/translation.log" LIFETIME_STATS_FILE="$FIXTURE/data/stats.env" \
    RUN_NOW=1 MAX_RUNTIME_MINUTES=0 MANAGE_LLAMA_CPP_CONTAINER=0 \
    CHUNK_RETRY_ATTEMPTS=3 CHUNK_RETRY_DELAY_SECONDS=0 FAILED_RETRY_HOURS=24 \
    bash "$ROOT/scripts/translate.sh" >/dev/null 2>&1
  RC=$?
  set -e
}
srt_cues() {  # srt_cues N TEXT... -> numbered cues 1..N with the given lines
  local n="$1" i
  for i in $(seq 1 "$n"); do printf '%d\n00:00:%02d,000 --> 00:00:%02d,500\nWhere are you going with this, %d?\n\n' "$i" "$i" "$i" "$i"; done
}

# One stubborn cue keeps its source line; the rest of the file is translated.
mkdir -p "$FIXTURE/movies/Fallback"
srt_cues 10 > "$FIXTURE/movies/Fallback/Fallback.en.srt"
run_translate "$FIXTURE/movies/Fallback" TARGET_LANGUAGES=es-MX MOCK_FAIL_FIRST=3
out="$FIXTURE/movies/Fallback/Fallback.es-MX.srt"
[ "$RC" -eq 0 ] || fail "fallback run exited $RC"
[ -s "$out" ] || fail "fallback output missing"
[ "$(grep -c -- '-->' "$out")" -eq 10 ] || fail "fallback output lost cues"
grep -qF 'Where are you going with this, 1?' "$out" || fail "stubborn cue not kept in source language"
[ "$(grep -c 'Hola.' "$out")" -eq 9 ] || fail "other cues not translated"
[ ! -e "$FIXTURE/movies/Fallback/Fallback.es-MX.failed" ] || fail "file failed for one stubborn cue"
grep -q 'Cue 1 kept in the source language' "$FIXTURE/data/translation.log" || fail "fallback not logged"

# Too many stubborn cues: the file fails and its output is discarded.
mkdir -p "$FIXTURE/movies/Failure"
srt_cues 2 > "$FIXTURE/movies/Failure/Failure.en.srt"
run_translate "$FIXTURE/movies/Failure" TARGET_LANGUAGES=es-MX MOCK_FAIL_AFTER=1
[ "$RC" -eq 2 ] || fail "too-many-fallbacks run exited $RC"
[ "$(cat "$FIXTURE/count")" -eq 4 ]
[ -f "$FIXTURE/movies/Failure/Failure.es-MX.failed" ] || fail "no failure marker"
[ ! -e "$FIXTURE/movies/Failure/Failure.es-MX.srt" ] || fail "mostly-English output was published"
[ ! -e "$FIXTURE/movies/Failure/Failure.es-MX.temp" ] || fail "mostly-English partial kept for resume"
diagnostic=$(find "$FIXTURE/data/translation-failures" -type f -name '*.json' -print -quit)
[ -n "$diagnostic" ]
[ "$(stat -c '%a' "$diagnostic")" = "600" ]
jq -e '.source_file and .target_language and .cue_id and .cue_timestamp and .source_dialogue and .rejection_reason' "$diagnostic" >/dev/null

# llama.cpp errors stop the file (no English filler) and keep partial output for resume.
mkdir -p "$FIXTURE/movies/Down"
srt_cues 2 > "$FIXTURE/movies/Down/Down.en.srt"
run_translate "$FIXTURE/movies/Down" TARGET_LANGUAGES=es-MX MOCK_HTTP_STATUS=500
[ "$RC" -eq 2 ] || fail "request-failure run exited $RC"
[ -f "$FIXTURE/movies/Down/Down.es-MX.failed" ] || fail "no failure marker after request errors"
[ ! -e "$FIXTURE/movies/Down/Down.es-MX.srt" ] || fail "output published despite request errors"
if grep -qF 'Where are you going' "$FIXTURE/movies/Down/Down.es-MX.temp" 2>/dev/null; then
  fail "source text used as filler when llama.cpp was failing"
fi
grep -q 'request failed after 3 attempts' "$FIXTURE/data/translation.log" || fail "request failure not logged"

# The watermark cue is copied verbatim and never sent to the model; Chinese
# requests restate the target language next to the dialogue.
mkdir -p "$FIXTURE/movies/Watermark"
{ printf '0\n00:00:00,000 --> 00:00:00,900\n{\\an8}<i>Brought to you by PlexMind</i>\n\n'; srt_cues 2; } > "$FIXTURE/movies/Watermark/Watermark.en.srt"
run_translate "$FIXTURE/movies/Watermark" TARGET_LANGUAGES=zh MOCK_ZH=1 MOCK_PAYLOAD_LOG="$FIXTURE/payloads"
out="$FIXTURE/movies/Watermark/Watermark.zh.srt"
[ "$RC" -eq 0 ] || fail "watermark run exited $RC"
[ "$(cat "$FIXTURE/count")" -eq 2 ] || fail "watermark cue was sent to the model"
grep -qF 'Brought to you by PlexMind' "$out" || fail "watermark cue not copied"
[ "$(grep -c '你好。' "$out")" -eq 2 ] || fail "dialogue cues not translated"
grep -q '繁體中文' "$FIXTURE/payloads" || fail "zh user message lacks the target-language instruction"
if grep -q 'PlexMind' "$FIXTURE/payloads"; then fail "watermark text reached the model"; fi

echo "translation retry tests passed"