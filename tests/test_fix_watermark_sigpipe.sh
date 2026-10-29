#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT
# `! cmd` is exempt from `set -e`, so negative assertions must exit explicitly.
fail() { echo "FAIL: $*" >&2; exit 1; }

mkdir -p "$TEMP_ROOT/movies/Film" "$TEMP_ROOT/symbols/Film" "$TEMP_ROOT/tv" "$TEMP_ROOT/data"

run_watermark() {
    rc=0
    env MOVIE_DIR="$TEMP_ROOT/movies" TV_DIR="$TEMP_ROOT/tv" \
        DATA_DIR="$TEMP_ROOT/data" LOG_FILE="$TEMP_ROOT/data/watermark.log" \
        WATERMARK_TEXT='{\an8}<i>Brought to you by PlexMind</i>' WATERMARK_SEARCH=PlexMind "$@" \
        bash "$ROOT_DIR/scripts/watermark.sh" > "$TEMP_ROOT/run.out" 2>&1 || rc=$?
}

# A CR-only SRT has no newline after the watermark cue, so `head -n 15` emits
# the whole 1 MiB file. With `head | grep -q`, head always outlived grep's
# match, died of SIGPIPE and pipefail stacked another watermark on every run.
SUB="$TEMP_ROOT/movies/Film/Film.en.srt"
{ printf '1\r00:00:01,000 --> 00:00:03,000\r'; printf '%*s' 1048576 '' | tr ' ' a; printf '\r\r'; } > "$SUB"

run_watermark
[ "$rc" -eq 0 ] || fail "watermark.sh exited $rc"
[ "$(grep -c PlexMind "$SUB")" -eq 1 ] || fail "first run did not add exactly one watermark"
grep -qF 'New Watermarks Added: 1' "$TEMP_ROOT/run.out" || fail "first run did not count the injected watermark"

for run in 2 3 4; do
    run_watermark
    [ "$rc" -eq 0 ] || fail "watermark.sh run $run exited $rc"
    [ "$(grep -c PlexMind "$SUB")" -eq 1 ] || fail "run $run stacked another watermark on a large subtitle"
    grep -qF 'Skipped (Already Watermarked): 1' "$TEMP_ROOT/run.out" || fail "run $run did not count the subtitle as already watermarked"
done

# WATERMARK_SEARCH is literal text, not a glob or a grep option. "-M" in the
# dialogue matches the glob -[PM]* but not the literal string.
SYM="$TEMP_ROOT/symbols/Film/Film.en.srt"
printf '1\n00:00:01,000 --> 00:00:03,000\n-Mom?\n-Pardon?\n' > "$SYM"
for run in 1 2; do
    run_watermark MOVIE_DIR="$TEMP_ROOT/symbols" WATERMARK_TEXT='Subs by -[PM]*' WATERMARK_SEARCH='-[PM]*'
    [ "$rc" -eq 0 ] || fail "watermark.sh run $run exited $rc for a symbol search string"
    [ "$(grep -cF -- '-[PM]*' "$SYM")" -eq 1 ] || fail "run $run did not leave exactly one watermark for a symbol search string"
done
[ "$(sed -n 3p "$SYM")" = 'Subs by -[PM]*' ] || fail "watermark text was not written literally"

echo "watermark sigpipe tests passed"
