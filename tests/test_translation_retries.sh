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
  jq -r '.messages[].content' "$payload" >> "$MOCK_PAYLOAD_LOG"
fi
if [ -n "${MOCK_REQUEST_LOG:-}" ] && [ -n "$payload" ]; then
  jq -c '{temperature, seed, max_tokens, thinking: .chat_template_kwargs.enable_thinking,
          min_items: .response_format.json_schema.schema.minItems,
          max_items: .response_format.json_schema.schema.maxItems}' "$payload" >> "$MOCK_REQUEST_LOG"
fi
reply() { printf '%s' "$2" > "$out"; [ "$wants_status" -eq 0 ] || printf '%s' "$1"; exit 0; }
if [ -n "${MOCK_HTTP_STATUS:-}" ] || { [ "${MOCK_HTTP_FAIL_AFTER:-0}" -gt 0 ] && [ "$count" -gt "$MOCK_HTTP_FAIL_AFTER" ]; }; then
  reply "${MOCK_HTTP_STATUS:-500}" '{"error":{"code":500,"message":"boom"}}'
fi
if [ "$count" -le "${MOCK_PEG_FIRST:-0}" ]; then
  reply 500 '{"error":{"code":500,"message":"The model produced output that does not match the expected peg-native format","type":"server_error"}}'
fi
if jq -e '.response_format' "$payload" >/dev/null; then
  # Batch request: the JSON array is the last line of the user message. Item
  # "..., N?" becomes "Hola, N." (or 你好，N。) so tests can see where it landed.
  sources=$(jq -r '.messages[-1].content' "$payload" | tail -n 1)
  reply 200 "$(jq -nc --argjson src "$sources" --arg zh "${MOCK_ZH:-0}" \
    --argjson echo "${MOCK_BATCH_ECHO_ITEM:-0}" --arg short "${MOCK_BATCH_SHORT:-0}" '
    [range($src | length) as $i | $src[$i] as $s | ($s | split(", ") | last | rtrimstr("?")) as $n
      | if $i + 1 == $echo then $s elif $zh == "1" then "你好，\($n)。" else "Hola, \($n)." end]
    | if $short == "1" then .[:-1] else . end
    | {choices: [{finish_reason: "stop", message: {content: tojson}}]}')"
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

# Per-cue cases pin TRANSLATION_BATCH_CUES=1; run_translate does too unless a case overrides it.
env PATH="$FIXTURE/bin:$PATH" MOCK_COUNT_FILE="$FIXTURE/count" TRANSLATION_BATCH_CUES=1 \
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
env PATH="$FIXTURE/bin:$PATH" MOCK_COUNT_FILE="$FIXTURE/count" MOCK_INVARIANT=1 TRANSLATION_BATCH_CUES=1 \
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
  env PATH="$FIXTURE/bin:$PATH" MOCK_COUNT_FILE="$FIXTURE/count" TRANSLATION_BATCH_CUES=1 "$@" \
    MOVIE_DIR="$dir" TV_DIR="$FIXTURE/tv" DATA_DIR="$FIXTURE/data" \
    LOG_FILE="$FIXTURE/data/translation.log" LIFETIME_STATS_FILE="$FIXTURE/data/stats.env" \
    RUN_NOW=1 MAX_RUNTIME_MINUTES=0 MANAGE_LLAMA_CPP_CONTAINER=0 \
    CHUNK_RETRY_ATTEMPTS=3 CHUNK_RETRY_DELAY_SECONDS=0 FAILED_RETRY_HOURS=24 \
    bash "$ROOT/scripts/translate.sh" >/dev/null 2>&1
  RC=$?
  set -e
}
srt_cues() {  # srt_cues N [FIRST] -> N numbered cues starting at FIRST (default 1)
  local n="$1" first="${2:-1}" i
  for i in $(seq "$first" $((first + n - 1))); do printf '%d\n00:00:%02d,000 --> 00:00:%02d,500\nWhere are you going with this, %d?\n\n' "$i" "$i" "$i" "$i"; done
}
# What the batch mock makes of srt_cues output; per-cue requests get "Hola." instead.
batch_translation() { sed 's/^Where are you going with this, \([0-9]*\)?$/Hola, \1./' "$1"; }

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
grep -qF '將以下英文字幕翻譯成繁體中文，只輸出譯文' "$FIXTURE/payloads" || fail "zh user message lacks the target-language instruction"
# The payload log holds system prompts too, so this also covers context lines.
if grep -q 'PlexMind' "$FIXTURE/payloads"; then fail "watermark text reached the model"; fi

# Batches: 10 cues in batches of 4 take 3 requests, and every item lands on
# its own cue with the source numbering and timestamps.
mkdir -p "$FIXTURE/movies/Batch"
srt_cues 10 > "$FIXTURE/movies/Batch/Batch.en.srt"
run_translate "$FIXTURE/movies/Batch" TARGET_LANGUAGES=es-MX TRANSLATION_BATCH_CUES=4 MOCK_REQUEST_LOG="$FIXTURE/batch-requests"
[ "$RC" -eq 0 ] || fail "batch run exited $RC"
[ "$(cat "$FIXTURE/count")" -eq 3 ] || fail "10 cues in batches of 4 took $(cat "$FIXTURE/count") requests"
batch_translation "$FIXTURE/movies/Batch/Batch.en.srt" > "$FIXTURE/expected"
diff -u "$FIXTURE/expected" "$FIXTURE/movies/Batch/Batch.es-MX.srt" >&2 || fail "batch output does not match the source cues"
jq -se '[.[].min_items] == [4, 4, 2] and [.[].max_items] == [4, 4, 2]
        and all(.[]; .thinking == false and .temperature == 0.1)' "$FIXTURE/batch-requests" >/dev/null \
  || fail "batch payloads do not pin the item count"
# The token budget follows the batch's text: ample for ~34-character cues, far
# below the old flat 256 tokens per cue that let a looping reply run for ~40 s.
jq -se 'all(.[]; .max_tokens > 40 * .max_items and .max_tokens < 128 * .max_items)' "$FIXTURE/batch-requests" >/dev/null \
  || fail "batch token budgets do not scale with the cues: $(jq -sc '[.[].max_tokens]' "$FIXTURE/batch-requests")"

# A reply with the wrong item count is redone one cue per request; after three
# unusable batches in a row the rest of the file is not batched.
mkdir -p "$FIXTURE/movies/Short"
srt_cues 12 > "$FIXTURE/movies/Short/Short.en.srt"
run_translate "$FIXTURE/movies/Short" TARGET_LANGUAGES=es-MX TRANSLATION_BATCH_CUES=3 MOCK_BATCH_SHORT=1
out="$FIXTURE/movies/Short/Short.es-MX.srt"
[ "$RC" -eq 0 ] || fail "short-batch run exited $RC"
# 3 batches + 9 per-cue requests, then 3 per-cue requests with batching off.
[ "$(cat "$FIXTURE/count")" -eq 15 ] || fail "short-batch run took $(cat "$FIXTURE/count") requests"
[ "$(grep -c -- '-->' "$out")" -eq 12 ] || fail "short-batch output lost cues"
[ "$(grep -c '^Hola\.$' "$out")" -eq 12 ] || fail "short-batch cues not translated one by one"
grep -qF 'unusable (reason=item_count_2_of_3, finish=stop)' "$FIXTURE/data/translation.log" || fail "wrong item count not logged"
grep -qF '3 batches in a row were unusable' "$FIXTURE/data/translation.log" || fail "batching not switched off"

# One rejected item is retried on its own; the rest of its batch is kept.
mkdir -p "$FIXTURE/movies/Item"
srt_cues 4 > "$FIXTURE/movies/Item/Item.en.srt"
run_translate "$FIXTURE/movies/Item" TARGET_LANGUAGES=es-MX TRANSLATION_BATCH_CUES=4 MOCK_BATCH_ECHO_ITEM=2
[ "$RC" -eq 0 ] || fail "rejected-item run exited $RC"
[ "$(cat "$FIXTURE/count")" -eq 2 ] || fail "rejected item was not retried alone"
batch_translation "$FIXTURE/movies/Item/Item.en.srt" | sed 's/^Hola, 2\.$/Hola./' > "$FIXTURE/expected"
diff -u "$FIXTURE/expected" "$FIXTURE/movies/Item/Item.es-MX.srt" >&2 || fail "rejected item not replaced by its per-cue translation"
grep -qF 'Batch of cues 1-4: 3/4 accepted; rejected cue(s) 2 (substantial_dialogue_unchanged)' "$FIXTURE/data/translation.log" \
  || fail "rejected item not logged"

# A watermark cue mid-file splits the batches and never reaches the model.
mkdir -p "$FIXTURE/movies/BatchMark"
{ srt_cues 2; printf '3\n00:00:03,000 --> 00:00:03,500\n{\\an8}<i>Brought to you by PlexMind</i>\n\n'; srt_cues 3 4; } \
  > "$FIXTURE/movies/BatchMark/BatchMark.en.srt"
run_translate "$FIXTURE/movies/BatchMark" TARGET_LANGUAGES=zh TRANSLATION_BATCH_CUES=4 MOCK_ZH=1 \
  MOCK_PAYLOAD_LOG="$FIXTURE/batch-payloads" MOCK_REQUEST_LOG="$FIXTURE/batch-mark-requests"
[ "$RC" -eq 0 ] || fail "watermark batch run exited $RC"
[ "$(cat "$FIXTURE/count")" -eq 2 ] || fail "watermark batch run took $(cat "$FIXTURE/count") requests"
jq -se '[.[].min_items] == [2, 3]' "$FIXTURE/batch-mark-requests" >/dev/null || fail "watermark cue was batched"
sed 's/^Where are you going with this, \([0-9]*\)?$/你好，\1。/' "$FIXTURE/movies/BatchMark/BatchMark.en.srt" > "$FIXTURE/expected"
diff -u "$FIXTURE/expected" "$FIXTURE/movies/BatchMark/BatchMark.zh.srt" >&2 || fail "watermark batch output does not match the source cues"
grep -qF '將以下英文字幕翻譯成繁體中文，只輸出譯文' "$FIXTURE/batch-payloads" || fail "zh batch message lacks the target-language instruction"
if grep -q 'PlexMind' "$FIXTURE/batch-payloads"; then fail "watermark text reached the model in a batch"; fi

# llama.cpp's "does not match the expected peg-native format" 500 is a bad
# sample: it is resampled, then the cue keeps its source line and the file
# still succeeds.
mkdir -p "$FIXTURE/movies/Peg"
srt_cues 10 > "$FIXTURE/movies/Peg/Peg.en.srt"
run_translate "$FIXTURE/movies/Peg" TARGET_LANGUAGES=es-MX MOCK_PEG_FIRST=3 MOCK_REQUEST_LOG="$FIXTURE/peg-requests"
out="$FIXTURE/movies/Peg/Peg.es-MX.srt"
[ "$RC" -eq 0 ] || fail "peg-native run exited $RC"
[ "$(cat "$FIXTURE/count")" -eq 12 ] || fail "peg-native run took $(cat "$FIXTURE/count") requests"
[ ! -e "$FIXTURE/movies/Peg/Peg.es-MX.failed" ] || fail "file failed for a peg-native error"
grep -qF 'Where are you going with this, 1?' "$out" || fail "unparseable cue not kept in the source language"
[ "$(grep -c '^Hola\.$' "$out")" -eq 9 ] || fail "cues after the peg-native error not translated"
grep -qF 'Cue 1 kept in the source language after 3 attempts (reason=unparseable_model_output)' "$FIXTURE/data/translation.log" \
  || fail "peg-native fallback not logged"
jq -se '[.[:3][] | .temperature] == [0.1, 0.3, 0.5] and [.[:3][] | .seed] == [null, 2, 3]' "$FIXTURE/peg-requests" >/dev/null \
  || fail "peg-native retries were not resampled"
# A 33-character cue gets 73 tokens, and each retry a larger share, still under LLAMA_CPP_MAX_TOKENS.
jq -se '[.[:3][] | .max_tokens] == [73, 146, 219]' "$FIXTURE/peg-requests" >/dev/null \
  || fail "per-cue token budgets do not grow per attempt: $(jq -sc '[.[:3][] | .max_tokens]' "$FIXTURE/peg-requests")"

# The same error on a batch sends its cues one per request, with no fallback.
mkdir -p "$FIXTURE/movies/PegBatch"
srt_cues 4 > "$FIXTURE/movies/PegBatch/PegBatch.en.srt"
run_translate "$FIXTURE/movies/PegBatch" TARGET_LANGUAGES=es-MX TRANSLATION_BATCH_CUES=4 MOCK_PEG_FIRST=1
[ "$RC" -eq 0 ] || fail "peg-native batch run exited $RC"
[ "$(cat "$FIXTURE/count")" -eq 5 ] || fail "peg-native batch run took $(cat "$FIXTURE/count") requests"
[ "$(grep -c '^Hola\.$' "$FIXTURE/movies/PegBatch/PegBatch.es-MX.srt")" -eq 4 ] || fail "peg-native batch cues not translated one by one"
grep -qF 'Batch of cues 1-4 unusable (reason=unparseable_model_output' "$FIXTURE/data/translation.log" || fail "peg-native batch not logged"

# A run that stops mid-batch on llama.cpp errors keeps no source filler and
# resumes without duplicating or skipping cues, even when a cue was appended
# just before a crash and never checkpointed.
mkdir -p "$FIXTURE/movies/Resume"
srt_cues 10 > "$FIXTURE/movies/Resume/Resume.en.srt"
temp="$FIXTURE/movies/Resume/Resume.es-MX.temp"
run_translate "$FIXTURE/movies/Resume" TARGET_LANGUAGES=es-MX TRANSLATION_BATCH_CUES=4 MOCK_HTTP_FAIL_AFTER=1
[ "$RC" -eq 2 ] || fail "interrupted run exited $RC"
# Batch 1 succeeds, batch 2 fails, then its first cue fails 3 times alone.
[ "$(cat "$FIXTURE/count")" -eq 5 ] || fail "interrupted run took $(cat "$FIXTURE/count") requests"
[ "$(grep -c -- '-->' "$temp")" -eq 4 ] || fail "partial output does not hold exactly the first batch"
if grep -qF 'Where are you going' "$temp"; then fail "source text used as filler for a failing batch"; fi
[ "$(cut -d' ' -f2 "$temp.checkpoint")" = "4" ] || fail "checkpoint does not record 4 cues"
printf '5\n00:00:05,000 --> 00:00:05,500\nHalf-written cue\n\n' >> "$temp"
rm -f "$FIXTURE/movies/Resume/Resume.es-MX.failed"
run_translate "$FIXTURE/movies/Resume" TARGET_LANGUAGES=es-MX TRANSLATION_BATCH_CUES=4
[ "$RC" -eq 0 ] || fail "resumed run exited $RC"
[ "$(cat "$FIXTURE/count")" -eq 2 ] || fail "resumed run took $(cat "$FIXTURE/count") requests"
grep -qF 'RESUME: Continuing at chunk 4/10.' "$FIXTURE/data/translation.log" || fail "run did not resume"
batch_translation "$FIXTURE/movies/Resume/Resume.en.srt" > "$FIXTURE/expected"
diff -u "$FIXTURE/expected" "$FIXTURE/movies/Resume/Resume.es-MX.srt" >&2 || fail "resumed output duplicates or skips cues"

# The batch size is validated like the other numeric settings.
for bad in 0 abc; do
  run_translate "$FIXTURE/movies/Batch" TRANSLATION_BATCH_CUES="$bad"
  [ "$RC" -eq 2 ] || fail "TRANSLATION_BATCH_CUES=$bad exited $RC"
  [ ! -e "$FIXTURE/count" ] || fail "TRANSLATION_BATCH_CUES=$bad still sent requests"
done

echo "translation retry tests passed"