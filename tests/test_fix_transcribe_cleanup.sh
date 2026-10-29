#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT
# `! cmd` is exempt from `set -e`, so negative assertions must exit explicitly.
fail() { echo "FAIL: $*" >&2; tail -n 60 "$TEMP_ROOT"/*/data/logs/*.log >&2 2>/dev/null || true; exit 1; }
line_of() { awk -v needle="$2" 'index($0, needle) { print NR; exit }' "$1"; }

mkdir -p "$TEMP_ROOT/bin"
# No usable audio language tag, so long videos go through the AI language profiler.
cat > "$TEMP_ROOT/bin/ffprobe" <<'EOF'
#!/bin/sh
case "$*" in
  *stream_tags=language*) echo und ;;
  *format=duration*) echo "${MOCK_DURATION:-1000}" ;;
  *) echo 'aac,2,48000' ;;
esac
EOF
# "Silent" has no audio where the profiler samples it.
cat > "$TEMP_ROOT/bin/ffmpeg" <<'EOF'
#!/bin/sh
case "$*" in *Silent*/tmp/sample_*) silent=1 ;; *) silent="" ;; esac
out=""
while [ "$#" -gt 0 ]; do
  [ "$1" = "-y" ] && out="$2"
  shift
done
[ -n "$out" ] || exit 0
if [ -n "$silent" ]; then rm -f "$out"; exit 1; fi
printf 'fake audio\n' > "$out"
EOF
cat > "$TEMP_ROOT/bin/curl" <<'EOF'
#!/bin/sh
args="$*"; out=""; status=""; url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -w) status=1; shift ;;
    http*) url="$1" ;;
  esac
  shift
done
case "$url" in
  */containers/*)
    printf 'broker %s\n' "$url" >> "$MOCK_CURL_LOG"
    # A repeated dashboard Stop signals the whole job group, this request included.
    case "$url" in */stop*) kill -TERM $$ ;; esac
    printf '204'
    exit 0
    ;;
  *output=json*)
    printf 'profile %s\n' "$args" >> "$MOCK_CURL_LOG"
    printf '{"language": "ja", "text": "konnichiwa"}' > "$out"
    exit 0
    ;;
esac
# MOCK_UPLOAD_EXIT makes every audio upload fail the way curl does.
case "$args" in
  *audio_file=@*) [ -z "${MOCK_UPLOAD_EXIT:-}" ] || exit "$MOCK_UPLOAD_EXIT" ;;
esac
printf 'whisper %s\n' "$url" >> "$MOCK_CURL_LOG"
if [ -n "$out" ] && [ "$out" != /dev/null ]; then
  for i in 1 2 3 4 5 6; do
    printf '%d\n00:00:%02d,000 --> 00:00:%02d,000\nSpoken line number %d of the scene.\n\n' "$i" $((i * 2)) $((i * 2 + 1)) "$i"
  done > "$out"
fi
[ -z "$status" ] || printf '200'
# The first dashboard Stop arrives during the transcription upload.
if [ -n "${MOCK_TERM_PID_FILE:-}" ] && [ -n "$status" ] && [ ! -e "${MOCK_TERM_PID_FILE}.sent" ]; then
  case "$url" in
    *output=srt*) : > "${MOCK_TERM_PID_FILE}.sent"; kill -TERM "$(cat "$MOCK_TERM_PID_FILE")" ;;
  esac
fi
EOF
chmod +x "$TEMP_ROOT/bin/"*

# Usage: run_transcribe RUN_DIR [NAME=VALUE...]; sets rc.
run_transcribe() {
    local run="$1"
    shift
    rc=0
    env PATH="$TEMP_ROOT/bin:$PATH" MOCK_CURL_LOG="$run/curl.log" \
        MOVIE_DIR="$run/movies" TV_DIR="$run/tv" DATA_DIR="$run/data" \
        LOG_FILE="$run/data/transcription.log" LIFETIME_STATS_FILE="$run/data/stats.env" \
        WHISPER_API_URL=http://whisper.test/asr RUN_NOW=1 MAX_RUNTIME_MINUTES=5 \
        DOCKER_BROKER_URL=http://broker.test PLEXMIND_BROKER_TOKEN=test-broker-token \
        START_SIDECAR_CONTAINERS=1 STOP_SIDECAR_CONTAINERS=1 WHISPER_CONTAINER_NAME=whisper-asr-webservice \
        SUBTITLE_FILE_MODE=0644 "$@" \
        bash "$ROOT_DIR/scripts/transcribe.sh" >/dev/null 2>&1 || rc=$?
}

assert_whisper_stopped_before_report() {
    local log_file="$1/data/transcription.log" stop_line report_line
    stop_line=$(line_of "$log_file" "Whisper: stopped container whisper-asr-webservice.") || true
    report_line=$(line_of "$log_file" "REPORT: Dashboard written") || true
    [ -n "$stop_line" ] || fail "$2: the job-owned Whisper container was not stopped"
    [ -n "$report_line" ] || fail "$2: the end-of-session report was not written"
    [ "$stop_line" -lt "$report_line" ] || fail "$2: Whisper was stopped only after the library-wide report"
    grep -q '^broker .*/containers/whisper-asr-webservice/stop' "$1/curl.log" || fail "$2: no stop request reached the broker"
}

# --- Normal run: profiler short-circuit and cleanup order ---
RUN="$TEMP_ROOT/normal"
mkdir -p "$RUN/movies/Film" "$RUN/movies/Silent" "$RUN/tv" "$RUN/data"
touch "$RUN/movies/Film/Film.mkv" "$RUN/movies/Silent/Silent.mkv"
run_transcribe "$RUN"
[ "$rc" -eq 0 ] || fail "transcribe.sh exited $rc"

[ -f "$RUN/movies/Film/Film.ja.srt" ] || fail "the profiler's foreign verdict was not used for the transcript"
[ -f "$RUN/movies/Silent/Silent.en.srt" ] || fail "a video without profiler samples did not default to English"
profiles=$(grep -c '^profile ' "$RUN/curl.log") || true
# Film stops after its first (foreign) sample; Silent has no sample to upload.
[ "$profiles" = 1 ] || fail "expected 1 profiler request, got ${profiles:-0}: $(cat "$RUN/curl.log")"
grep -q -- '^profile .* --connect-timeout 30 --max-time 300 ' "$RUN/curl.log" || fail "profiler request has no curl timeouts"
assert_whisper_stopped_before_report "$RUN" "normal run"

# --- Stopped run: SIGTERM mid-upload, then a repeated Stop during cleanup ---
RUN="$TEMP_ROOT/stopped"
mkdir -p "$RUN/movies/Film" "$RUN/tv" "$RUN/data"
touch "$RUN/movies/Film/Film.mkv"
run_transcribe "$RUN" MOCK_DURATION=100 MOCK_TERM_PID_FILE="$RUN/data/plexmind_gpu.pid"
[ -e "$RUN/data/plexmind_gpu.pid.sent" ] || fail "the stop signal was never sent (no upload reached Whisper)"
[ "$rc" -eq 143 ] || fail "a stopped transcribe.sh should die by SIGTERM, exited $rc"
grep -qx 'LIFETIME_SCANNED=1' "$RUN/data/stats.env" || fail "lifetime stats were not written on stop"
assert_whisper_stopped_before_report "$RUN" "stopped run"

# --- Failure markers: success clears one, a subtitle settles one, and an
# exhausted one is skipped without quarantining the video again ---
RUN="$TEMP_ROOT/markers"
mkdir -p "$RUN/movies/Retry" "$RUN/movies/Done" "$RUN/movies/Spent" "$RUN/tv" "$RUN/data"
touch "$RUN/movies/Retry/Retry.mkv" "$RUN/movies/Done/Done.mkv" "$RUN/movies/Spent/Spent.mkv"
printf 'CURL_TIMEOUT\n1\n' > "$RUN/movies/Retry/Retry.ai.failed"
printf 'VALIDATION_FAILED\n1\n' > "$RUN/movies/Done/Done.ai.failed"
printf '1\n00:00:01,000 --> 00:00:02,000\nAlready here.\n' > "$RUN/movies/Done/Done.en.srt"
printf 'API_ERROR\n3\n' > "$RUN/movies/Spent/Spent.ai.failed"
run_transcribe "$RUN" MOCK_DURATION=100
# Spent's exhausted marker is a deferred failure.
[ "$rc" -eq 2 ] || fail "marker run exited $rc"
[ -f "$RUN/movies/Retry/Retry.en.srt" ] || fail "the retried video was not transcribed"
[ ! -e "$RUN/movies/Retry/Retry.ai.failed" ] || fail "a video that succeeded on retry kept its failure marker"
[ ! -e "$RUN/movies/Done/Done.ai.failed" ] || fail "a failure marker beside an existing subtitle was kept"
[ -f "$RUN/movies/Spent/Spent.ai.failed" ] || fail "an exhausted failure marker was removed"
if grep -qF 'Spent.mkv' "$RUN/data/quarantine/quarantine.log" 2>/dev/null; then
    fail "an exhausted video was quarantined again on a later run"
fi

# --- The failure that uses up the last retry quarantines the video, once ---
RUN="$TEMP_ROOT/last-retry"
mkdir -p "$RUN/movies/Last" "$RUN/tv" "$RUN/data"
touch "$RUN/movies/Last/Last.mkv"
printf 'CURL_TIMEOUT\n2\n' > "$RUN/movies/Last/Last.ai.failed"
run_transcribe "$RUN" MOCK_DURATION=100 MOCK_UPLOAD_EXIT=28
[ "$rc" -eq 2 ] || fail "failed-retry run exited $rc"
[ "$(sed -n 2p "$RUN/movies/Last/Last.ai.failed")" = 3 ] || fail "the last failed retry was not counted"
[ "$(grep -c '|CURL_TIMEOUT|.*Last\.mkv$' "$RUN/data/quarantine/quarantine.log" 2>/dev/null)" = 1 ] \
    || fail "a video whose last retry failed was not quarantined exactly once"

echo "transcribe cleanup tests passed"
