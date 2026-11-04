#!/bin/bash
# Audio-track choice, the "already has subtitles" check, retrying old failure
# markers, and the hallucination cleaner's timing.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

mkdir -p "$TEMP_ROOT/data" "$TEMP_ROOT/bin"
DATA_DIR="$TEMP_ROOT/data"
LOG_FILE="$TEMP_ROOT/lib.log"
source "$ROOT_DIR/scripts/lib.sh"

# --- The English track is chosen even when it is not first ---
# Fake ffprobe: prints the JSON in $FAKE_STREAMS for any audio probe.
cat > "$TEMP_ROOT/bin/ffprobe" <<'EOF'
#!/bin/bash
printf '%s' "$FAKE_STREAMS"
EOF
chmod +x "$TEMP_ROOT/bin/ffprobe"
PATH="$TEMP_ROOT/bin:$PATH"
export FAKE_STREAMS
stream() { printf '{"index":%s,"disposition":{"default":%s},"tags":{"language":"%s"}}' "$1" "$2" "$3"; }

FAKE_STREAMS="{\"streams\":[$(stream 1 1 jpn),$(stream 2 1 eng)]}"
[ "$(pick_audio_track video)" = "2 eng" ] || fail "English dub after a Japanese track: got '$(pick_audio_track video)'"
[ "$(pick_audio_track video foreign)" = "1 jpn" ] || fail "foreign pick: got '$(pick_audio_track video foreign)'"
FAKE_STREAMS="{\"streams\":[$(stream 1 0 und),$(stream 3 1 fra)]}"
[ "$(pick_audio_track video)" = "3 fra" ] || fail "no English track should use the default one: got '$(pick_audio_track video)'"
FAKE_STREAMS="{\"streams\":[$(stream 4 0 und)]}"
[ "$(pick_audio_track video)" = "4 -" ] || fail "an untagged only track: got '$(pick_audio_track video)'"
FAKE_STREAMS='not json'
[ -z "$(pick_audio_track video)" ] || fail "an unreadable probe should pick nothing"

# --- Only a full subtitle settles a video ---
V="$TEMP_ROOT/Movie"
has_full_subtitle "$V" && fail "no subtitle counted as one"
touch "$V.en.forced.srt" "$V.ai.srt" "$V.ai-en.srt" "$V.Part2.en.srt"
has_full_subtitle "$V" && fail "forced-only, in-progress or a sibling's subtitle counted as full"
touch "$V.EN.SDH.srt"
has_full_subtitle "$V" || fail "an SDH subtitle was not counted"
rm -f "$V".*.srt; touch "$V.srt"
has_full_subtitle "$V" || fail "an untagged Movie.srt was not counted"
rm -f "$V.srt"; touch "$V.es-MX.srt"
has_full_subtitle "$V" || fail "a regional language tag was not counted"

# --- A permanent failure is retried once its marker is old enough ---
MARKER="$TEMP_ROOT/Film.ai.failed"
printf '%s\n%s\n' "$FAIL_VALIDATION" 1 > "$MARKER"
failed_marker_retryable "$MARKER" && fail "a fresh VALIDATION_FAILED marker was retried"
python3 -c 'import os, sys, time; t = time.time() - 31 * 86400; os.utime(sys.argv[1], (t, t))' "$MARKER"
failed_marker_retryable "$MARKER" || fail "a 31-day-old VALIDATION_FAILED marker was not retried"
TRANSCRIBE_FAILED_RETRY_DAYS=0 failed_marker_retryable "$MARKER" && fail "TRANSCRIBE_FAILED_RETRY_DAYS=0 still retried"

# --- A repeated-line run keeps its first cue's timing ---
SRT="$TEMP_ROOT/loop.srt"
{
    for i in 1 2 3 4 5; do
        printf '%d\n00:00:%02d,000 --> 00:00:%02d,500\nThank you.\n\n' "$i" "$((i * 10))" "$((i * 10))"
    done
    printf '6\n00:01:00,000 --> 00:01:02,000\nGoodbye.\n'
} > "$SRT"
HALLUCINATION_REPEAT_THRESHOLD=3 clean_hallucinations "$SRT" >/dev/null
grep -qF '00:00:10,000 --> 00:00:10,500' "$SRT" || fail "the first cue's own timing was not kept: $(cat "$SRT")"
[ "$(grep -c 'Thank you' "$SRT")" -eq 1 ] || fail "repeats were not dropped"
grep -qF 'Goodbye.' "$SRT" || fail "the cue after the run was lost"

echo "transcribe selection tests passed"
