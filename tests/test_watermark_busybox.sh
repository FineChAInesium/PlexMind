#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT
# `! cmd` is exempt from `set -e`, so negative assertions must exit explicitly.
fail() { echo "FAIL: $*" >&2; exit 1; }

mkdir -p "$TEMP_ROOT/movies/Film" "$TEMP_ROOT/tv" "$TEMP_ROOT/data" "$TEMP_ROOT/bin"
# BusyBox chmod/chown (the Alpine scripts image) have no --reference.
for tool in chmod chown; do
    printf '#!/bin/sh\ncase "$1" in --reference*) echo "%s: unrecognized option: $1" >&2; exit 1 ;; esac\nexec "%s" "$@"\n' \
        "$tool" "$(command -v "$tool")" > "$TEMP_ROOT/bin/$tool"
    chmod +x "$TEMP_ROOT/bin/$tool"
done

SUB="$TEMP_ROOT/movies/Film/Film.en.srt"
printf '1\n00:00:01,000 --> 00:00:03,000\nHello there.\n' > "$SUB"
chmod 640 "$SUB"
OTHER="$TEMP_ROOT/movies/Film/Other.en.srt"

run_watermark() {
    rc=0
    env PATH="$TEMP_ROOT/bin:$PATH" MOVIE_DIR="$TEMP_ROOT/movies" TV_DIR="$TEMP_ROOT/tv" \
        DATA_DIR="$TEMP_ROOT/data" LOG_FILE="$TEMP_ROOT/data/watermark.log" \
        WATERMARK_TEXT='{\an8}<i>Brought to you by PlexMind</i>' WATERMARK_SEARCH=PlexMind "$@" \
        bash "$ROOT_DIR/scripts/watermark.sh" >/dev/null 2>&1 || rc=$?
}

run_watermark
[ "$rc" -eq 0 ] || fail "watermark.sh exited $rc"
[ "$(sed -n 3p "$SUB")" = '{\an8}<i>Brought to you by PlexMind</i>' ] || fail "watermark text was not written literally"
[ "$(sed -n 5p "$SUB")" = 1 ] || fail "original cues did not follow the watermark"
[ "$(stat -c %a "$SUB")" = 640 ] || fail "watermark.sh did not preserve the subtitle mode"
[ -z "$(find "$TEMP_ROOT/movies" -name '*.tmp' -print -quit)" ] || fail "watermark.sh left a temp file behind"

run_watermark
[ "$rc" -eq 0 ] || fail "second watermark.sh run exited $rc"
[ "$(grep -c PlexMind "$SUB")" -eq 1 ] || fail "a second run stacked another watermark"

# Text that dupe detection cannot find would be prepended again on every run.
printf '1\n00:00:01,000 --> 00:00:02,000\nSecond file.\n' > "$OTHER"
cp "$OTHER" "$TEMP_ROOT/other-before.srt"
run_watermark WATERMARK_TEXT='Subtitles by Bob'
[ "$rc" -eq 2 ] || fail "WATERMARK_TEXT without WATERMARK_SEARCH was accepted (exit $rc)"
cmp -s "$OTHER" "$TEMP_ROOT/other-before.srt" || fail "misconfigured run modified a subtitle"

echo "watermark busybox tests passed"
