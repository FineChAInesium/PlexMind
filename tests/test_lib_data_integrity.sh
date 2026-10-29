#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT
# `! cmd` is exempt from `set -e`, so negative assertions must exit explicitly.
fail() { echo "FAIL: $*" >&2; exit 1; }

mkdir -p "$TEMP_ROOT/movies" "$TEMP_ROOT/tv" "$TEMP_ROOT/data"
MOVIE_DIR="$TEMP_ROOT/movies"
TV_DIR="$TEMP_ROOT/tv"
DATA_DIR="$TEMP_ROOT/data"
LOG_FILE=""
source "$ROOT_DIR/scripts/lib.sh"

srt_cues() {  # srt_cues N -> N numbered cues that pass validate_srt
    local n="$1" i
    for i in $(seq 1 "$n"); do printf '%d\n00:00:%02d,000 --> 00:00:%02d,500\nLine number %d is here.\n\n' "$i" "$i" "$i" "$i"; done
}

# BusyBox chmod/chown have no --reference; make any use of it fail here too.
chmod() { case "${1:-}" in --reference*) echo "chmod: unrecognized option: $1" >&2; return 1 ;; esac; command chmod "$@"; }
chown() { case "${1:-}" in --reference*) echo "chown: unrecognized option: $1" >&2; return 1 ;; esac; command chown "$@"; }

# --- Language tags from media metadata ---
[ "$(normalize_lang_code ENG)" = en ] || fail "eng was not normalized"
[ "$(normalize_lang_code pt-BR)" = pt ] || fail "region was not stripped"
[ "$(normalize_lang_code Hin)" = hi ] || fail "ISO 639-2 code was not mapped to Whisper's ISO 639-1 code"
[ -z "$(normalize_lang_code xyz)" ] || fail "three-letter code Whisper rejects was passed through"
for tag in "../../evil" "en/../x" "e" "engl" "en us" 'en$(id)' "und" ""; do
    [ -z "$(normalize_lang_code "$tag")" ] || fail "unsafe language tag accepted: $tag"
done

# --- Watermark: literal text, mode kept, temp file never left behind ---
watermark_default() { env -u WATERMARK_TEXT "$@" bash -c 'source "$1"; printf %s "$WATERMARK_TEXT"' _ "$ROOT_DIR/scripts/lib.sh"; }
[ "$(watermark_default)" = '{\an8}<i>Brought to you by PlexMind</i>' ] || fail "default watermark text is malformed: $(watermark_default)"
[ "$(watermark_default WATERMARK_TEXT='Custom mark')" = 'Custom mark' ] || fail "configured watermark text was altered: $(watermark_default WATERMARK_TEXT='Custom mark')"

mkdir -p "$TEMP_ROOT/movies/Film"
WM="$TEMP_ROOT/movies/Film/Film.en.srt"
srt_cues 6 > "$WM"
command chmod 640 "$WM"
ENABLE_WATERMARK=true
WATERMARK_TEXT='{\an8}<i>PlexMind test</i>'
WATERMARK_SEARCH=PlexMind
apply_watermark "$WM" > "$TEMP_ROOT/watermark.log"
[ "$(sed -n 3p "$WM")" = '{\an8}<i>PlexMind test</i>' ] || fail "watermark text was not written literally"
[ "$(sed -n 5p "$WM")" = 1 ] || fail "original cues did not follow the watermark"
[ "$(stat -c %a "$WM")" = 640 ] || fail "watermark did not preserve the subtitle mode"
[ ! -e "$WM.tmp" ] || fail "watermark left its temp file behind"
apply_watermark "$WM" >> "$TEMP_ROOT/watermark.log"
[ "$(grep -c PlexMind "$WM")" -eq 1 ] || fail "watermark was applied twice"

FAIL_WM="$TEMP_ROOT/movies/Film/Fail.en.srt"
srt_cues 6 > "$FAIL_WM"
cp "$FAIL_WM" "$TEMP_ROOT/fail-before.srt"
mv() { return 1; }
if apply_watermark "$FAIL_WM" >> "$TEMP_ROOT/watermark.log"; then
    unset -f mv
    fail "watermark reported success although the replace failed"
fi
unset -f mv
[ ! -e "$FAIL_WM.tmp" ] || fail "failed watermark left its temp file behind"
cmp -s "$FAIL_WM" "$TEMP_ROOT/fail-before.srt" || fail "failed watermark changed the original"

# --- Encoding repair: lossless only, original backed up ---
BOM="$TEMP_ROOT/movies/Film/Bom.en.srt"
{ printf '\357\273\277'; srt_cues 5; } > "$BOM"
if verify_encoding "$BOM" > "$TEMP_ROOT/encoding.log"; then fail "UTF-8 BOM was not stripped"; fi
[ "$(head -c 1 "$BOM")" = 1 ] || fail "UTF-8 BOM is still present"
[ -n "$(find "$DATA_DIR/quarantine/encoding-backups" -type f -name '*-Bom.en.srt' -print -quit)" ] || fail "BOM original was not backed up"

BAD="$TEMP_ROOT/movies/Film/Bad.en.srt"
{ printf '\357\273\277'; srt_cues 5; printf 'Broken \377 byte\n'; } > "$BAD"
cp "$BAD" "$TEMP_ROOT/bad-before.srt"
verify_encoding "$BAD" >> "$TEMP_ROOT/encoding.log" || fail "undecodable subtitle reported as converted"
cmp -s "$BAD" "$TEMP_ROOT/bad-before.srt" || fail "undecodable subtitle was rewritten"
grep -q 'Bad.en.srt does not decode cleanly' "$TEMP_ROOT/encoding.log" || fail "refused conversion was not logged"

if command -v file >/dev/null 2>&1; then
    LATIN="$TEMP_ROOT/movies/Film/Film.fr.srt"
    { srt_cues 5; printf '6\n00:00:06,000 --> 00:00:07,000\nCaf\351 cr\350me.\n'; } > "$LATIN"
    command chmod 640 "$LATIN"
    cp "$LATIN" "$TEMP_ROOT/latin-before.srt"
    if verify_encoding "$LATIN" >> "$TEMP_ROOT/encoding.log"; then fail "Latin-1 subtitle was not converted"; fi
    LC_ALL=C grep -q "$(printf 'Caf\303\251 cr\303\250me')" "$LATIN" || fail "converted subtitle is not UTF-8"
    [ "$(stat -c %a "$LATIN")" = 640 ] || fail "encoding repair did not preserve the subtitle mode"
    backup=$(find "$DATA_DIR/quarantine/encoding-backups" -type f -name '*-Film.fr.srt' -print -quit)
    [ -n "$backup" ] || fail "Latin-1 original was not backed up"
    cmp -s "$backup" "$TEMP_ROOT/latin-before.srt" || fail "backup does not hold the original bytes"
fi

# --- PGS cleanup: exact replacement only, quarantined not deleted ---
PGS="$TEMP_ROOT/movies/Pgs"
mkdir -p "$PGS"
srt_cues 5 > "$PGS/Pgs.en.forced.srt"
printf 'image\n' > "$PGS/Pgs.en.sup"
printf 'image\n' > "$PGS/Pgs.en.forced.sup"
cleanup_pgs "$PGS" > "$TEMP_ROOT/pgs.log"
[ -e "$PGS/Pgs.en.sup" ] || fail "full PGS track removed although only a forced SRT exists"
[ ! -e "$PGS/Pgs.en.forced.sup" ] || fail "forced PGS track kept despite a forced SRT"
[ -f "$DATA_DIR/quarantine/pgs/Pgs.en.forced.sup" ] || fail "replaced PGS track was not quarantined"
[ "$PGS_DELETED_COUNT" -eq 1 ] || fail "PGS cleanup count was $PGS_DELETED_COUNT"
grep -qF "Pgs.en.forced.sup -> $DATA_DIR/quarantine/pgs/Pgs.en.forced.sup" "$TEMP_ROOT/pgs.log" || fail "PGS cleanup did not log what it moved"

# --- Resume: only finished transcripts are promoted, the rest quarantined ---
RES="$TEMP_ROOT/movies/Resume"
mkdir -p "$RES"
srt_cues 6 > "$RES/Done.ai-fr.srt"
srt_cues 6 > "$RES/Region.ai-pt-BR.srt"
srt_cues 6 > "$RES/Cut.ai.srt"
srt_cues 6 > "$RES/Cut.translate_tmp.srt"
printf 'x\n' > "$RES/Cut.ai.srt.cleaned"
srt_cues 6 > "$RES/Dup.ai-en.srt"
srt_cues 6 > "$RES/Dup.en.srt"
srt_cues 2 > "$RES/Short.ai-en.srt"
srt_cues 6 > "$RES/Show.ai-Final.en.srt"
SUBTITLE_FILE_MODE=0644
resume_partial "$RES" > "$TEMP_ROOT/resume.log"
[ "$(tail -n 1 "$TEMP_ROOT/resume.log")" = 2 ] || fail "resume did not report exactly two recovered transcripts"
[ -f "$RES/Done.fr.srt" ] || fail "finished transcript was not promoted under its own language"
[ -f "$RES/Region.pt-BR.srt" ] || fail "region-tagged transcript was not promoted"
[ "$(stat -c %a "$RES/Done.fr.srt")" = 644 ] || fail "promoted transcript kept its private mode"
for name in Done.en.srt Cut.en.srt Cut.srt Short.en.srt; do
    [ ! -e "$RES/$name" ] || fail "partial was promoted as $name"
done
for name in Cut.ai.srt Cut.translate_tmp.srt Cut.ai.srt.cleaned Dup.ai-en.srt Short.ai-en.srt; do
    [ ! -e "$RES/$name" ] || fail "stale partial $name was left in the library"
    [ -f "$DATA_DIR/quarantine/partials/$name" ] || fail "stale partial $name was not quarantined"
done
[ -f "$RES/Dup.en.srt" ] || fail "existing subtitle was touched by resume"
[ -f "$RES/Show.ai-Final.en.srt" ] || fail "resume touched a subtitle it does not own"

srt_cues 6 > "$RES/Later.ai-de.srt"
SUBTITLE_UID=not-a-number
resume_partial "$RES" > "$TEMP_ROOT/resume-perms.log"
SUBTITLE_UID=""
[ -f "$RES/Later.ai-de.srt" ] || fail "finished transcript was lost when its permissions could not be applied"
[ ! -e "$RES/Later.de.srt" ] || fail "transcript was published without its final permissions"

echo "lib data integrity tests passed"
