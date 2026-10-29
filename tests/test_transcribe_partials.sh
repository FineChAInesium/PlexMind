#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT
# `! cmd` is exempt from `set -e`, so negative assertions must exit explicitly.
fail() { echo "FAIL: $*" >&2; tail -n 40 "$TEMP_ROOT"/data/logs/*.log >&2 2>/dev/null || true; exit 1; }

MOVIES="$TEMP_ROOT/movies"
mkdir -p "$MOVIES/Film" "$MOVIES/Foreign" "$TEMP_ROOT/tv" "$TEMP_ROOT/data" "$TEMP_ROOT/bin"
touch "$MOVIES/Film/Film.mkv" "$MOVIES/Foreign/Foreign.mkv"
# Left behind by an interrupted run: it must not be published or block the video.
printf '1\n00:00:01,000 --> 00:00:02,000\nTruncated upload.\n' > "$MOVIES/Film/Film.ai.srt"

# Film carries a hostile audio language tag; Foreign a valid ISO 639-2 one.
cat > "$TEMP_ROOT/bin/ffprobe" <<'EOF'
#!/bin/sh
case "$*" in
  *stream_tags=language*) case "$*" in *Foreign*) echo fre ;; *) echo '../../evil' ;; esac ;;
  *format=duration*) echo 100 ;;
  *) echo 'aac,2,48000' ;;
esac
EOF
cat > "$TEMP_ROOT/bin/ffmpeg" <<'EOF'
#!/bin/sh
out=""
while [ "$#" -gt 0 ]; do
  [ "$1" = "-y" ] && out="$2"
  shift
done
[ -z "$out" ] || printf 'fake audio\n' > "$out"
EOF
cat > "$TEMP_ROOT/bin/curl" <<'EOF'
#!/bin/sh
out=""; status=""; url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -w) status=1; shift ;;
    http*) url="$1" ;;
  esac
  shift
done
printf '%s\n' "$url" >> "$MOCK_CURL_LOG"
if [ -n "$out" ] && [ "$out" != /dev/null ]; then
  for i in 1 2 3 4 5 6; do
    printf '%d\n00:00:%02d,000 --> 00:00:%02d,000\nSpoken line number %d of the scene.\n\n' "$i" $((i * 2)) $((i * 2 + 1)) "$i"
  done > "$out"
fi
[ -z "$status" ] || printf '200'
EOF
# BusyBox chmod/chown (the Alpine scripts image) have no --reference.
for tool in chmod chown; do
    printf '#!/bin/sh\ncase "$1" in --reference*) echo "%s: unrecognized option: $1" >&2; exit 1 ;; esac\nexec "%s" "$@"\n' \
        "$tool" "$(command -v "$tool")" > "$TEMP_ROOT/bin/$tool"
done
chmod +x "$TEMP_ROOT/bin/"*

rc=0
env PATH="$TEMP_ROOT/bin:$PATH" MOCK_CURL_LOG="$TEMP_ROOT/curl.log" \
    MOVIE_DIR="$MOVIES" TV_DIR="$TEMP_ROOT/tv" DATA_DIR="$TEMP_ROOT/data" \
    LOG_FILE="$TEMP_ROOT/data/transcription.log" LIFETIME_STATS_FILE="$TEMP_ROOT/data/stats.env" \
    WHISPER_API_URL=http://whisper.test/asr DOCKER_BROKER_URL= RUN_NOW=1 MAX_RUNTIME_MINUTES=5 \
    ENABLE_WATERMARK=true WATERMARK_TEXT='{\an8}<i>PlexMind test</i>' WATERMARK_SEARCH=PlexMind \
    SUBTITLE_FILE_MODE=0644 \
    bash "$ROOT_DIR/scripts/transcribe.sh" >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 0 ] || fail "transcribe.sh exited $rc"

[ -f "$TEMP_ROOT/data/quarantine/partials/Film/Film.ai.srt" ] || fail "stale partial was not quarantined"
EN="$MOVIES/Film/Film.en.srt"
[ -f "$EN" ] || fail "invalid audio tag did not fall back to an English transcript"
[ "$(stat -c %a "$EN")" = 644 ] || fail "transcript was published without the final subtitle mode"
[ "$(sed -n 3p "$EN")" = '{\an8}<i>PlexMind test</i>' ] || fail "transcript watermark is wrong"
grep -qF 'Spoken line number 6 of the scene.' "$EN" || fail "transcript lost cues"
[ -f "$MOVIES/Foreign/Foreign.fr.srt" ] || fail "valid audio tag was not used for the transcript name"
if [ -n "$(find "$MOVIES" \( -name '*.ai.srt*' -o -name '*.ai-*.srt' -o -name '*evil*' -o -name '*.tmp' \) -print -quit)" ]; then
    fail "partial, temp or tag-derived file left in the library: $(find "$MOVIES" -type f | sort)"
fi
grep -q 'language=fr' "$TEMP_ROOT/curl.log" || fail "Whisper was not asked for the tagged language"
if grep -q 'evil' "$TEMP_ROOT/curl.log"; then fail "untrusted audio tag reached the Whisper URL"; fi

echo "transcribe partial tests passed"
