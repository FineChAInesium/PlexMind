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
