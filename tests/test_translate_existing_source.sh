#!/usr/bin/env bash
# A video with no English .srt is translated from an English track inside it.
# Uses real ffmpeg and a real video, and a fake llama.cpp.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
command -v ffmpeg >/dev/null || { echo "ffmpeg not installed; skipping"; exit 0; }
FIXTURE=$(mktemp -d)
trap 'rm -rf "$FIXTURE"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
mkdir -p "$FIXTURE/movies" "$FIXTURE/tv" "$FIXTURE/data" "$FIXTURE/bin"

cat > "$FIXTURE/bin/curl" <<'EOF'
#!/usr/bin/env bash
out=""; wants_status=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w) wants_status=1; shift 2 ;;
    *) shift ;;
  esac
done
if [ -z "$out" ]; then printf '{"data":[]}'; exit 0; fi
printf '%s' '{"choices":[{"finish_reason":"stop","message":{"content":"Hola."}}]}' > "$out"
[ "$wants_status" -eq 1 ] && printf '200'
exit 0
EOF
chmod +x "$FIXTURE/bin/curl"

srt_cues() {
  local i
  for ((i = 1; i <= $1; i++)); do
    printf '%d\n00:00:%02d,000 --> 00:00:%02d,500\nLine number %d.\n\n' "$i" "$i" "$i" "$i"
  done
}

# make_video NAME CUES [TITLE]: a short video in its own folder with one English subtitle track.
make_video() {
  mkdir -p "$FIXTURE/movies/$1"
  srt_cues "$2" > "$FIXTURE/subs.srt"
  ffmpeg -nostdin -v error -y -f lavfi -i color=c=black:s=64x64:d=30 -i "$FIXTURE/subs.srt" \
    -map 0 -map 1 -c:v mpeg4 -c:s srt -metadata:s:s:0 language=eng \
    "$FIXTURE/movies/$1/$1.mkv"
}

run_translate() {
  rm -f "$FIXTURE/data/translation.log"
  set +e
  env -u PLEX_URL -u PLEX_TOKEN PATH="$FIXTURE/bin:$PATH" TRANSLATION_BATCH_CUES=1 \
    MOVIE_DIR="$FIXTURE/movies" TV_DIR="$FIXTURE/tv" DATA_DIR="$FIXTURE/data" \
    LOG_FILE="$FIXTURE/data/translation.log" LIFETIME_STATS_FILE="$FIXTURE/data/stats.env" \
    TARGET_LANGUAGES=es-MX RUN_NOW=1 MAX_RUNTIME_MINUTES=0 MANAGE_LLAMA_CPP_CONTAINER=0 \
    CHUNK_RETRY_ATTEMPTS=1 CHUNK_RETRY_DELAY_SECONDS=0 FAILED_RETRY_HOURS=24 \
    bash "$ROOT/scripts/translate.sh" >/dev/null 2>&1
  RC=$?
  set -e
}

# The only pending work is inside a video: the run must not stop at "fully translated".
make_video Film 25
run_translate
[ "$RC" -eq 0 ] || fail "translation exited $RC: $(tail -n 5 "$FIXTURE/data/translation.log")"
output="$FIXTURE/movies/Film/Film.es-MX.srt"
[ -s "$output" ] || fail "no translation beside the video"
[ "$(grep -c -- '-->' "$output")" -eq 25 ] || fail "expected 25 cues"
grep -qF '00:00:07,000 --> 00:00:07,500' "$output" || fail "timestamps did not carry over"
grep -qF 'Hola.' "$output" || fail "output is not the translation"
grep -qF 'using its embedded subtitle' "$FIXTURE/data/translation.log" || fail "source was not logged"
# Plex must not be handed a second English subtitle, and the working copy is gone.
[ -z "$(find "$FIXTURE/movies" -iname '*.en.srt' -print -quit)" ] || fail "an English .srt was added to the library"
[ -z "$(find "$FIXTURE/data/translation_sources" -type f -print -quit 2>/dev/null)" ] || fail "source copy left behind"

# Nothing more to do on a second run.
run_translate
[ "$RC" -eq 0 ] || fail "second run exited $RC"
grep -qF 'Library fully translated!' "$FIXTURE/data/translation.log" || fail "second run found more work"

# An upper-case sidecar is the .srt walk's job, so the video is not listed as well.
make_video Sidecar 25
srt_cues 25 > "$FIXTURE/movies/Sidecar/Sidecar.EN.srt"
run_translate
[ "$RC" -eq 0 ] || fail "sidecar run exited $RC"
grep -qF ' 1 source subs, 0 videos with a source inside' "$FIXTURE/data/translation.log" || fail "sidecar video counted twice"
grep -qF 'Translating to [es-MX]: Sidecar.EN.srt' "$FIXTURE/data/translation.log" || fail "sidecar was not used"
rm -f "$FIXTURE/movies/Sidecar/Sidecar.es-MX.srt" "$FIXTURE/movies/Sidecar/Sidecar.EN.srt"
rm -rf "$FIXTURE/movies/Sidecar"

# A track too short to be dialogue fails with a reason and produces nothing.
make_video Signs 3
run_translate
[ "$RC" -eq 2 ] || fail "short track: expected exit 2, got $RC"
grep -qF 'only 3 cues' "$FIXTURE/data/translation.log" || fail "the reason was not logged"
[ -f "$FIXTURE/movies/Signs/Signs.es-MX.failed" ] || fail "no failure marker"
[ ! -e "$FIXTURE/movies/Signs/Signs.es-MX.srt" ] || fail "translated a short track"

echo "translate existing-source tests passed"
