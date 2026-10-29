#!/usr/bin/env bash
# translate.sh cost and recovery paths: a file past the fallback limit stops
# early, only failed requests back off, a refused batch is explained, an
# unencodable batch item is retranslated alone, a finished output that fails
# the timestamp check is discarded, and only this run's output goes to the
# ordering fixer.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
FIXTURE=$(mktemp -d)
trap 'rm -rf "$FIXTURE"' EXIT
mkdir -p "$FIXTURE/movies" "$FIXTURE/tv" "$FIXTURE/data" "$FIXTURE/bin"
LOG="$FIXTURE/data/translation.log"

# A copy of the scripts whose ordering fixer only records how it was called.
cp -R "$ROOT/scripts" "$FIXTURE/scripts"
cat > "$FIXTURE/scripts/fix_srt_ordering.py" <<'EOF'
import os
import sys

with open(os.environ["MOCK_ORDERING_LOG"], "a", encoding="utf-8") as log:
    print(os.getenv("PLEXMIND_MEDIA_LOCK_HELD", "unset"), *sys.argv[1:], file=log)
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
count=$(( $(cat "$MOCK_COUNT_FILE" 2>/dev/null || echo 0) + 1 ))
printf '%s\n' "$count" > "$MOCK_COUNT_FILE"
reply() { printf '%s' "$2" > "$out"; [ "$wants_status" -eq 0 ] || printf '%s' "$1"; exit 0; }
chat_reply() { jq -nc --arg c "$1" '{choices: [{finish_reason: "stop", message: {content: $c}}]}'; }
if [ -n "${MOCK_HTTP_STATUS:-}" ]; then
  reply "$MOCK_HTTP_STATUS" '{"error":{"code":503,"message":"Loading model","type":"unavailable_error"}}'
fi
if [ "$count" -le "${MOCK_PEG_FIRST:-0}" ]; then
  reply 500 '{"error":{"code":500,"message":"The model produced output that does not match the expected peg-native format","type":"server_error"}}'
fi
if jq -e '.response_format' "$payload" >/dev/null; then
  if [ -n "${MOCK_BATCH_HTTP_STATUS:-}" ]; then
    reply "$MOCK_BATCH_HTTP_STATUS" '{"error":{"code":400,"message":"JSON schema conversion failed:\nunsupported keyword","type":"invalid_request_error"}}'
  fi
  # The JSON array is the last line of the user message; "..., N?" becomes "Hola, N.".
  content=$(jq -r '.messages[-1].content' "$payload" | tail -n 1 \
    | jq -c '[.[] | split(", ") | last | rtrimstr("?") | "Hola, \(.)."]')
  if [ -n "${MOCK_BATCH_SURROGATE_ITEM:-}" ]; then
    # A lone surrogate escape is valid JSON text but cannot be written as UTF-8.
    content=$(printf '%s' "$content" | jq -c --argjson i "$MOCK_BATCH_SURROGATE_ITEM" '.[$i] = "SURROGATE"' \
      | sed 's/"SURROGATE"/"\\ud83c"/')
  fi
  reply 200 "$(chat_reply "$content")"
fi
if [ "$count" -le "${MOCK_FAIL_FIRST:-0}" ]; then
  # Echo the English line back untranslated.
  reply 200 "$(chat_reply "$(jq -r '.messages[-1].content' "$payload")")"
fi
reply 200 "$(chat_reply 'Hola.')"
EOF
chmod +x "$FIXTURE/bin/curl"

# `! cmd` is exempt from `set -e`, so negative assertions must exit explicitly.
fail() { echo "FAIL: $*" >&2; exit 1; }
run_translate() {  # run_translate DIR [VAR=value...]; the given settings win
  local dir="$1"; shift
  rm -f "$FIXTURE/count" "$FIXTURE/ordering-calls"
  if [ -e "$LOG" ]; then : > "$LOG"; fi
  set +e
  env PATH="$FIXTURE/bin:$PATH" MOCK_COUNT_FILE="$FIXTURE/count" MOCK_ORDERING_LOG="$FIXTURE/ordering-calls" \
    MOVIE_DIR="$dir" TV_DIR="$FIXTURE/tv" DATA_DIR="$FIXTURE/data" \
    LOG_FILE="$LOG" LIFETIME_STATS_FILE="$FIXTURE/data/stats.env" \
    TARGET_LANGUAGES=es-MX TRANSLATION_BATCH_CUES=1 RUN_NOW=1 MAX_RUNTIME_MINUTES=0 MANAGE_LLAMA_CPP_CONTAINER=0 \
    CHUNK_RETRY_ATTEMPTS=3 CHUNK_RETRY_DELAY_SECONDS=0 FAILED_RETRY_HOURS=24 "$@" \
    bash "$FIXTURE/scripts/translate.sh" >/dev/null 2>&1
  RC=$?
  set -e
}
srt_cues() {  # srt_cues N -> N numbered cues
  local n="$1" i
  for i in $(seq 1 "$n"); do printf '%d\n00:00:%02d,000 --> 00:00:%02d,500\nWhere are you going with this, %d?\n\n' "$i" "$i" "$i" "$i"; done
}
requests() { cat "$FIXTURE/count" 2>/dev/null || echo 0; }
no_ordering_run() { [ ! -e "$FIXTURE/ordering-calls" ] || fail "$1 run started the ordering fixer without a new translation"; }

# A file past TRANSLATION_MAX_FALLBACK_PCT is discarded anyway, so systematic
# peg-native 500s stop it at the second fallback (2 of 10 cues > 10%) rather
# than costing 3 requests for every cue.
mkdir -p "$FIXTURE/movies/Budget"
srt_cues 10 > "$FIXTURE/movies/Budget/Budget.en.srt"
run_translate "$FIXTURE/movies/Budget" MOCK_PEG_FIRST=1000
[ "$RC" -eq 2 ] || fail "fallback-limit run exited $RC"
[ "$(requests)" -eq 6 ] || fail "fallback-limit run took $(requests) requests instead of 6"
[ -f "$FIXTURE/movies/Budget/Budget.es-MX.failed" ] || fail "no failure marker past the fallback limit"
for ext in srt temp temp.checkpoint; do
  [ ! -e "$FIXTURE/movies/Budget/Budget.es-MX.$ext" ] || fail "Budget.es-MX.$ext kept past the fallback limit"
done
grep -qF '2/10 cues stayed untranslated (limit 10%); discarding output.' "$LOG" || fail "early stop not logged"
no_ordering_run fallback-limit

# Rejected and unparseable samples are resampled at once; only a failed
# request (llama.cpp loading or down) waits CHUNK_RETRY_DELAY_SECONDS.
mkdir -p "$FIXTURE/movies/NoWait"
srt_cues 1 > "$FIXTURE/movies/NoWait/NoWait.en.srt"
started=$SECONDS
run_translate "$FIXTURE/movies/NoWait" CHUNK_RETRY_DELAY_SECONDS=30 MOCK_PEG_FIRST=1 MOCK_FAIL_FIRST=2
elapsed=$((SECONDS - started))
[ "$RC" -eq 0 ] || fail "resample run exited $RC"
[ "$(requests)" -eq 3 ] || fail "resample run took $(requests) requests"
[ "$elapsed" -lt 30 ] || fail "a rejected or unparseable sample waited ${elapsed}s before its retry"
grep -qxF 'Hola.' "$FIXTURE/movies/NoWait/NoWait.es-MX.srt" || fail "resampled cue not translated"

mkdir -p "$FIXTURE/movies/Wait"
srt_cues 1 > "$FIXTURE/movies/Wait/Wait.en.srt"
started=$SECONDS
run_translate "$FIXTURE/movies/Wait" CHUNK_RETRY_DELAY_SECONDS=1 MOCK_HTTP_STATUS=503
elapsed=$((SECONDS - started))
[ "$RC" -eq 2 ] || fail "request-failure run exited $RC"
[ "$(requests)" -eq 3 ] || fail "request-failure run took $(requests) requests"
[ "$elapsed" -ge 2 ] || fail "failed requests were retried without waiting"
no_ordering_run request-failure

# llama.cpp's reason for refusing a batch reaches the log on one line, and the
# reply is kept as a private diagnostic.
mkdir -p "$FIXTURE/movies/Schema"
srt_cues 4 > "$FIXTURE/movies/Schema/Schema.en.srt"
rm -rf "$FIXTURE/data/translation-failures"
run_translate "$FIXTURE/movies/Schema" TRANSLATION_BATCH_CUES=4 MOCK_BATCH_HTTP_STATUS=400
[ "$RC" -eq 0 ] || fail "refused-batch run exited $RC"
[ "$(requests)" -eq 5 ] || fail "refused-batch run took $(requests) requests"
[ "$(grep -c '^Hola\.$' "$FIXTURE/movies/Schema/Schema.es-MX.srt")" -eq 4 ] || fail "refused batch not translated one cue per request"
grep -qF 'Batch of cues 1-4 unusable (reason=request_failed_curl_0_http_400: JSON schema conversion failed: unsupported keyword, finish=none)' "$LOG" \
  || fail "llama.cpp error message not logged on one line"
diagnostic=$(find "$FIXTURE/data/translation-failures" -type f -name '*-es-MX-batch-00000.json' -print -quit 2>/dev/null || true)
[ -n "$diagnostic" ] || fail "no diagnostic for the refused batch"
[ "$(stat -c '%a' "$diagnostic")" = "600" ] || fail "batch diagnostic is not private"
jq -e '.cue_range == "1-4" and (.rejection_reason | startswith("request_failed_curl_0_http_400: JSON schema"))
       and (.error.message | startswith("JSON schema conversion failed")) and .content == null' "$diagnostic" >/dev/null \
  || fail "batch diagnostic lacks the cue range or the llama.cpp error"

# A batch item that cannot be encoded as UTF-8 (a lone surrogate escape) is
# rejected on its own: it is retranslated alone and the items after it in the
# batch are still used.
mkdir -p "$FIXTURE/movies/Surrogate"
srt_cues 4 > "$FIXTURE/movies/Surrogate/Surrogate.en.srt"
run_translate "$FIXTURE/movies/Surrogate" TRANSLATION_BATCH_CUES=4 MOCK_BATCH_SURROGATE_ITEM=1
[ "$RC" -eq 0 ] || fail "unencodable-item run exited $RC"
[ "$(requests)" -eq 2 ] || fail "unencodable-item run took $(requests) requests"
sed 's/^Where are you going with this, \([134]\)?$/Hola, \1./; s/^Where are you going with this, 2?$/Hola./' \
  "$FIXTURE/movies/Surrogate/Surrogate.en.srt" > "$FIXTURE/expected"
diff -u "$FIXTURE/expected" "$FIXTURE/movies/Surrogate/Surrogate.es-MX.srt" >&2 || fail "unencodable batch item not retranslated on its own"
grep -qF 'Batch of cues 1-4: 3/4 accepted; rejected cue(s) 2 (invalid_unicode)' "$LOG" || fail "unencodable batch item not logged"

# A finished translation that fails the timestamp check is discarded, so the
# retry starts over instead of resuming into the same failure.
mkdir -p "$FIXTURE/movies/Mismatch"
printf '1\n00:00:01,000 --> 00:00:02,000 X1:100 X2:200\nWhere are you going with this, 1?\n\n' \
  > "$FIXTURE/movies/Mismatch/Mismatch.en.srt"
run_translate "$FIXTURE/movies/Mismatch"
[ "$RC" -eq 2 ] || fail "timestamp-mismatch run exited $RC"
[ -f "$FIXTURE/movies/Mismatch/Mismatch.es-MX.failed" ] || fail "no failure marker after a timestamp mismatch"
for ext in srt temp temp.checkpoint; do
  [ ! -e "$FIXTURE/movies/Mismatch/Mismatch.es-MX.$ext" ] || fail "Mismatch.es-MX.$ext kept after a timestamp mismatch"
done
grep -qF 'does not preserve every source timestamp; discarding output.' "$LOG" || fail "timestamp mismatch not logged"
no_ordering_run timestamp-mismatch

# The final permissions are applied before a translation is published. When
# they cannot be set nothing reaches the library, and the finished .temp is
# kept, so the next run publishes it without asking llama.cpp again.
mkdir -p "$FIXTURE/movies/Perms"
srt_cues 2 > "$FIXTURE/movies/Perms/Perms.en.srt"
run_translate "$FIXTURE/movies/Perms" SUBTITLE_UID=not-a-number
[ "$RC" -eq 2 ] || fail "unpublishable run exited $RC"
[ ! -e "$FIXTURE/movies/Perms/Perms.es-MX.srt" ] || fail "translation published without its final permissions"
[ -s "$FIXTURE/movies/Perms/Perms.es-MX.temp" ] || fail "finished translation was not kept for the next run"
[ ! -e "$FIXTURE/movies/Perms/Perms.es-MX.failed" ] || fail "a permission failure marked the translation as failed"
no_ordering_run unpublishable
run_translate "$FIXTURE/movies/Perms"
[ "$RC" -eq 0 ] || fail "publishing run exited $RC"
[ "$(requests)" -eq 0 ] || fail "publishing run sent $(requests) requests to llama.cpp"
[ "$(grep -c '^Hola\.$' "$FIXTURE/movies/Perms/Perms.es-MX.srt")" -eq 2 ] || fail "kept translation was not published"
for ext in temp temp.checkpoint; do
  [ ! -e "$FIXTURE/movies/Perms/Perms.es-MX.$ext" ] || fail "Perms.es-MX.$ext left after publishing"
done

# Source names: .hi.en.srt keeps its .hi, .EN.SDH.srt maps to the plain name.
mkdir -p "$FIXTURE/movies/Names"
srt_cues 1 > "$FIXTURE/movies/Names/Show.S01E01.hi.en.srt"
srt_cues 1 > "$FIXTURE/movies/Names/Movie.EN.SDH.srt"
run_translate "$FIXTURE/movies/Names"
[ "$RC" -eq 0 ] || fail "naming run exited $RC"
[ -s "$FIXTURE/movies/Names/Show.S01E01.hi.es-MX.srt" ] || fail ".hi.en.srt translation misnamed"
[ -s "$FIXTURE/movies/Names/Movie.es-MX.srt" ] || fail ".EN.SDH.srt translation misnamed"
[ "$(find "$FIXTURE/movies/Names" -name '*.es-MX.srt' | wc -l)" -eq 2 ] || fail "unexpected translations in Names"

# translate.sh hands the ordering fixer exactly the files this run wrote, in
# one call under the media lock it holds; a run with nothing new does not
# start it. (The fixer's path mode itself is covered by
# test_fix_fix_srt_ordering.py.)
mkdir -p "$FIXTURE/movies/Order"
srt_cues 2 > "$FIXTURE/movies/Order/Order.en.srt"
run_translate "$FIXTURE/movies/Order" TARGET_LANGUAGES=es-MX,fr
[ "$RC" -eq 0 ] || fail "ordering run exited $RC"
expected_call="1 $FIXTURE/movies/Order/Order.es-MX.srt $FIXTURE/movies/Order/Order.fr.srt"
[ "$(cat "$FIXTURE/ordering-calls" 2>/dev/null)" = "$expected_call" ] || fail "ordering fixer not run on exactly this run's translations"
run_translate "$FIXTURE/movies/Order" TARGET_LANGUAGES=es-MX,fr
[ "$RC" -eq 0 ] || fail "up-to-date run exited $RC"
no_ordering_run up-to-date

echo "translate fix tests passed"
