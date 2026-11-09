#!/usr/bin/env bash
# Batches stop at sentence ends and carry the finished lines before them.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
FIXTURE=$(mktemp -d)
trap 'rm -rf "$FIXTURE"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
mkdir -p "$FIXTURE/movies/Film" "$FIXTURE/tv" "$FIXTURE/data" "$FIXTURE/bin"

cat > "$FIXTURE/bin/curl" <<'EOF'
#!/usr/bin/env bash
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
if [ -n "$payload" ] && [ -f "$payload" ]; then
  jq -c '{size: .response_format.json_schema.schema.minItems, system: .messages[0].content}' "$payload" >> "$MOCK_LOG"
  sources=$(jq -r '.messages[-1].content' "$payload" | tail -n 1)
  jq -nc --argjson src "$sources" '$src | map("Hola " + (split(" ") | last)) | {choices: [{finish_reason: "stop", message: {content: tojson}}]}' > "$out"
fi
[ "$wants_status" -eq 1 ] && printf '200'
exit 0
EOF
chmod +x "$FIXTURE/bin/curl"

# Cues 6, 12 and 18 end sentences; the others run on with no pause.
{
  for i in $(seq 1 20); do
    tail=","; case "$i" in 6|12|18) tail="." ;; esac
    printf '%d\n00:00:%02d,000 --> 00:00:%02d,900\nLine number %d%s\n\n' "$i" "$i" "$i" "$i" "$tail"
  done
} > "$FIXTURE/movies/Film/Film.en.srt"

export MOCK_LOG="$FIXTURE/requests"
set +e
env -u PLEX_URL -u PLEX_TOKEN PATH="$FIXTURE/bin:$PATH" MOVIE_DIR="$FIXTURE/movies" TV_DIR="$FIXTURE/tv" DATA_DIR="$FIXTURE/data" \
  LOG_FILE="$FIXTURE/data/translation.log" LIFETIME_STATS_FILE="$FIXTURE/data/stats.env" \
  TARGET_LANGUAGES=es-MX TRANSLATION_BATCH_CUES=8 RUN_NOW=1 MAX_RUNTIME_MINUTES=0 MANAGE_LLAMA_CPP_CONTAINER=0 \
  CHUNK_RETRY_ATTEMPTS=1 CHUNK_RETRY_DELAY_SECONDS=0 FAILED_RETRY_HOURS=24 \
  bash "$ROOT/scripts/translate.sh" >/dev/null 2>&1
RC=$?
set -e
[ "$RC" -eq 0 ] || fail "translation exited $RC: $(tail -n 5 "$FIXTURE/data/translation.log")"

# The file's last cue can end a batch too, so the final batch runs to it.
[ "$(jq -r '.size' "$MOCK_LOG" | paste -sd' ')" = "6 6 8" ] || fail "batch sizes: $(jq -r '.size' "$MOCK_LOG" | paste -sd' ')"
[ "$(grep -c -- '-->' "$FIXTURE/movies/Film/Film.es-MX.srt")" -eq 20 ] || fail "expected 20 cues"
grep -qF 'QUEUE: 1 items in name order' "$FIXTURE/data/translation.log" || fail "queue was not logged"

second=$(sed -n '2p' "$MOCK_LOG" | jq -r '.system')
grep -qF 'English: Line number 6.' <<<"$second" || fail "the source line was not in the context"
grep -qF 'Translation: Hola 6.' <<<"$second" || fail "the finished translation was not in the context"
if sed -n '1p' "$MOCK_LOG" | jq -r '.system' | grep -qF 'English: '; then fail "the first batch has nothing before it"; fi

echo "translate batch context tests passed"
