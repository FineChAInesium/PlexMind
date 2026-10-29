#!/bin/bash
# ==============================================================================
# watermark.sh — SRT Mass Watermark Injector
# Version: 0.8.21 — PlexMind release line
#
# Recursively scans for .srt files and safely prepends a 5-second
# watermark block to the beginning of the file.
#
# Requires: lib.sh
# ==============================================================================

set -uo pipefail

# --- CONFIGURATION ---
LOG_FILE="${LOG_FILE:-/app/data/watermark_injector.log}"
WHISPER_API_URL="${WHISPER_API_URL:-http://whisper:9000/asr}"

# --- LOAD SHARED LIBRARY ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh" || { echo "FATAL: Cannot load lib.sh"; exit 1; }

mkdir -p "$(dirname "$LOG_FILE")"
prepare_log_file

# --- GLOBAL COUNTERS ---
export FILES_SCANNED=0
export WATERMARKS_ADDED=0
export ALREADY_WATERMARKED=0

log "========================================================="
log "Starting Mass SRT Watermark Injection"
log "========================================================="

case "$WATERMARK_TEXT" in
    *"$WATERMARK_SEARCH"*) ;;
    *) log "ERROR: WATERMARK_TEXT must contain WATERMARK_SEARCH, or every run would add another watermark."; exit 2 ;;
esac

ALL_MEDIA_DIRS=("${MOVIE_DIR}" "${TV_DIR}")
validate_media_directories || exit 1
acquire_lock "${DATA_DIR}/plexmind_media_mutation.lock"
trap '[ -z "${WATERMARK_TMP_FILE:-}" ] || rm -f "$WATERMARK_TMP_FILE"' EXIT

for DIR in "${ALL_MEDIA_DIRS[@]}"; do
    if [ ! -d "$DIR" ]; then
        log "WARNING: Directory not found: $DIR"
        continue
    fi

    log "Scanning directory: $DIR"

    while IFS= read -r -d $'\0' SUB_FILE; do
        FILES_SCANNED=$((FILES_SCANNED + 1))

        # Not `head | grep -q`: once grep matches, head can die of SIGPIPE and
        # pipefail would report the file as unmarked, stacking another cue.
        case "$(head -n 15 "$SUB_FILE")" in
            *"$WATERMARK_SEARCH"*) ALREADY_WATERMARKED=$((ALREADY_WATERMARKED + 1)) ;;
            *)
                # Writes WATERMARK_TEXT literally (like transcribe.sh), keeps the
                # file's mode/owner and never leaves its temp file behind.
                if ! prepend_watermark "$SUB_FILE"; then
                    log "ERROR: Failed to watermark ${SUB_FILE}."
                    exit 2
                fi

                log "INJECTED: $(basename "$SUB_FILE")"
                WATERMARKS_ADDED=$((WATERMARKS_ADDED + 1))
                ;;
        esac

    done < <(find "$DIR" -type f -name "*.srt" -print0 2>/dev/null)
done

log "========================================================="
log "Injection Session Finished!"
log "Total SRT Files Scanned: ${FILES_SCANNED}"
log "New Watermarks Added: ${WATERMARKS_ADDED}"
log "Skipped (Already Watermarked): ${ALREADY_WATERMARKED}"
log "========================================================="
