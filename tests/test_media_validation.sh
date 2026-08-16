#!/bin/bash
set -eu

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT

mkdir -p "$TEMP_ROOT/movies" "$TEMP_ROOT/tv"
touch "$TEMP_ROOT/movies/example.mkv"

MOVIE_DIR="$TEMP_ROOT/movies"
TV_DIR="$TEMP_ROOT/tv"
LOG_FILE=""
source "$ROOT_DIR/scripts/lib.sh"
validate_media_directories

cat > "$TEMP_ROOT/arrow-dialogue.srt" <<'EOF'
1
00:00:01,000 --> 00:00:03,000
Workflow: input --> output

2
00:00:04,000 --> 00:00:06,000
Second cue

3
00:00:07,000 --> 00:00:09,000
Third cue

4
00:00:10,000 --> 00:00:12,000
Fourth cue

5
00:00:13,000 --> 00:00:15,000
Fifth cue
EOF
validate_srt "$TEMP_ROOT/arrow-dialogue.srt"

chmod 600 "$TEMP_ROOT/arrow-dialogue.srt"
SUBTITLE_FILE_MODE=0644
finalize_subtitle_permissions "$TEMP_ROOT/arrow-dialogue.srt"
[ "$(stat -c %a "$TEMP_ROOT/arrow-dialogue.srt")" = "644" ] || {
    echo "final subtitle mode was not normalized to 0644" >&2
    exit 1
}
chmod 640 "$TEMP_ROOT/arrow-dialogue.srt"
ENABLE_WATERMARK=true WATERMARK_TEXT="PlexMind test" WATERMARK_SEARCH="PlexMind"
apply_watermark "$TEMP_ROOT/arrow-dialogue.srt"
[ "$(stat -c %a "$TEMP_ROOT/arrow-dialogue.srt")" = "640" ] || {
    echo "watermark replacement did not preserve subtitle mode" >&2
    exit 1
}

ASCII_MTIME=$(stat -c %Y "$TEMP_ROOT/arrow-dialogue.srt")
verify_encoding "$TEMP_ROOT/arrow-dialogue.srt"
[ "$(stat -c %Y "$TEMP_ROOT/arrow-dialogue.srt")" = "$ASCII_MTIME" ] || {
    echo "ASCII subtitle was unnecessarily rewritten" >&2
    exit 1
}

sed 's/00:00:15,000/00:00:12,000/' "$TEMP_ROOT/arrow-dialogue.srt" > "$TEMP_ROOT/non-positive.srt"
if validate_srt "$TEMP_ROOT/non-positive.srt"; then
    echo "non-positive timestamp was incorrectly accepted" >&2
    exit 1
fi

cp "$TEMP_ROOT/arrow-dialogue.srt" "$TEMP_ROOT/movies/cleanup.en.srt"
printf 'fixture\n' > "$TEMP_ROOT/movies/cleanup.en.sup"
cleanup_pgs "$TEMP_ROOT/movies" > "$TEMP_ROOT/pgs-cleanup.log"
[ "${PGS_DELETED_COUNT:-0}" -eq 1 ] || {
    echo "PGS cleanup did not expose an exact numeric deletion count" >&2
    exit 1
}
[ ! -e "$TEMP_ROOT/movies/cleanup.en.sup" ] || {
    echo "PGS cleanup did not remove the fixture" >&2
    exit 1
}

printf 'fixture\n' > "$TEMP_ROOT/movies/cleanup.es.sup"
cleanup_pgs "$TEMP_ROOT/movies" > "$TEMP_ROOT/pgs-language.log"
[ -e "$TEMP_ROOT/movies/cleanup.es.sup" ] || {
    echo "PGS cleanup removed a language without a matching text subtitle" >&2
    exit 1
}

printf 'fixture\n' > "$TEMP_ROOT/movies/cleanup.sup"
cleanup_pgs "$TEMP_ROOT/movies" > "$TEMP_ROOT/pgs-unknown.log"
[ -e "$TEMP_ROOT/movies/cleanup.sup" ] || {
    echo "PGS cleanup removed an unknown-language track without opt-in" >&2
    exit 1
}

cat > "$TEMP_ROOT/stats.env" <<EOF
LIFETIME_SCANNED=12
LIFETIME_PROCESSED=3
LIFETIME_PROCESSING_SECONDS=99
MALICIOUS=\$(touch "$TEMP_ROOT/stats-executed")
EOF
LIFETIME_SCANNED=0 LIFETIME_PROCESSED=0 LIFETIME_PROCESSING_SECONDS=0
load_numeric_stats "$TEMP_ROOT/stats.env"
[ "$LIFETIME_SCANNED" -eq 12 ] && [ "$LIFETIME_PROCESSED" -eq 3 ] || {
    echo "numeric stats were not loaded" >&2
    exit 1
}
[ ! -e "$TEMP_ROOT/stats-executed" ] || {
    echo "stats parser executed untrusted shell content" >&2
    exit 1
}

MOVIE_DIR="$TEMP_ROOT/missing"
if validate_media_directories; then
    echo "missing media root was incorrectly accepted" >&2
    exit 1
fi

MOVIE_DIR="$TEMP_ROOT/movies"
TV_DIR="$TEMP_ROOT/movies"
if validate_media_directories; then
    echo "identical media roots were incorrectly accepted" >&2
    exit 1
fi

echo "media validation tests passed"
