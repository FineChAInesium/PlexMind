#!/bin/bash
# The watermark cue goes after a UTF-8 byte-order mark, and a UTF-16 or other
# non-UTF-8 subtitle is left untouched instead of being spliced.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

mkdir -p "$TEMP_ROOT/data"
DATA_DIR="$TEMP_ROOT/data"
LOG_FILE="$TEMP_ROOT/lib.log"
source "$ROOT_DIR/scripts/lib.sh"
WATERMARK_TEXT='{\an8}<i>PlexMind test</i>'

CUE='1
00:00:01,000 --> 00:00:02,000
Hello.
'

# Plain UTF-8: the cue comes first.
printf '%s' "$CUE" > "$TEMP_ROOT/plain.srt"
prepend_watermark "$TEMP_ROOT/plain.srt" || fail "plain UTF-8 file was not watermarked"
[ "$(head -n 1 "$TEMP_ROOT/plain.srt")" = 0 ] || fail "watermark cue is not first"

# UTF-8 with a BOM: the BOM stays the first bytes, then the cue, then the rest.
printf '\xef\xbb\xbf%s' "$CUE" > "$TEMP_ROOT/bom.srt"
prepend_watermark "$TEMP_ROOT/bom.srt" || fail "UTF-8 BOM file was not watermarked"
python3 - "$TEMP_ROOT/bom.srt" <<'PYEOF' || fail "BOM file was not watermarked cleanly"
import sys
data = open(sys.argv[1], "rb").read()
assert data.startswith(b"\xef\xbb\xbf0\n00:00:00,000 --> 00:00:05,000\n"), data[:60]
assert data.count(b"\xef\xbb\xbf") == 1, "a BOM was left in the middle of the file"
assert data.endswith(b"1\n00:00:01,000 --> 00:00:02,000\nHello.\n")
PYEOF

# UTF-16: left byte-for-byte as it was, reported as skipped (3).
python3 -c 'import sys; open(sys.argv[1], "wb").write(sys.argv[2].encode("utf-16"))' "$TEMP_ROOT/utf16.srt" "$CUE"
before=$(md5sum < "$TEMP_ROOT/utf16.srt")
rc=0
prepend_watermark "$TEMP_ROOT/utf16.srt" || rc=$?
[ "$rc" -eq 3 ] || fail "UTF-16 file returned $rc, expected 3 (skipped)"
[ "$(md5sum < "$TEMP_ROOT/utf16.srt")" = "$before" ] || fail "UTF-16 file was modified"
[ ! -e "$TEMP_ROOT/utf16.srt.tmp" ] || fail "temp file left behind"

# cp1252 (not UTF-8): also left alone.
printf '1\n00:00:01,000 --> 00:00:02,000\nCaf\xe9.\n' > "$TEMP_ROOT/cp1252.srt"
rc=0
prepend_watermark "$TEMP_ROOT/cp1252.srt" || rc=$?
[ "$rc" -eq 3 ] || fail "cp1252 file returned $rc, expected 3 (skipped)"

echo "watermark encoding tests passed"
