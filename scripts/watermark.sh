#!/bin/bash
# ==============================================================================
# watermark.sh — SRT Mass Watermark Injector
# Version: 0.8.20 — PlexMind release line
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

ALL_MEDIA_DIRS=("${MOVIE_DIR}" "${TV_DIR}")
validate_media_directories || exit 1
acquire_lock "${DATA_DIR}/plexmind_media_mutation.lock"

for DIR in "${ALL_MEDIA_DIRS[@]}"; do
    if [ ! -d "$DIR" ]; then
        log "WARNING: Directory not found: $DIR"
        continue
    fi

    log "Scanning directory: $DIR"

    while IFS= read -r -d $'\0' SUB_FILE; do
        FILES_SCANNED=$((FILES_SCANNED + 1))

        if ! head -n 15 "$SUB_FILE" | grep -qF "$WATERMARK_SEARCH"; then
            WATERMARK_BLOCK="0\n00:00:00,000 --> 00:00:05,000\n${WATERMARK_TEXT}\n\n"

            if ! { printf '%b' "$WATERMARK_BLOCK"; cat "$SUB_FILE"; } > "${SUB_FILE}.tmp"; then
                rm -f "${SUB_FILE}.tmp"
                log "ERROR: Failed to prepare watermark for ${SUB_FILE}."
                exit 2
            fi
            if ! grep -qF "$WATERMARK_SEARCH" "${SUB_FILE}.tmp" || ! grep -qF -- '-->' "${SUB_FILE}.tmp"; then
                rm -f "${SUB_FILE}.tmp"
                log "ERROR: Refusing invalid watermarked output for ${SUB_FILE}."
                exit 2
            fi
            chmod --reference="$SUB_FILE" "${SUB_FILE}.tmp" || { rm -f "${SUB_FILE}.tmp"; log "ERROR: Could not preserve mode for ${SUB_FILE}."; exit 2; }
            chown --reference="$SUB_FILE" "${SUB_FILE}.tmp" 2>/dev/null || true
            mv -f "${SUB_FILE}.tmp" "$SUB_FILE" || { rm -f "${SUB_FILE}.tmp"; log "ERROR: Atomic watermark replacement failed for ${SUB_FILE}."; exit 2; }

            log "INJECTED: $(basename "$SUB_FILE")"
            WATERMARKS_ADDED=$((WATERMARKS_ADDED + 1))
        else
            ALREADY_WATERMARKED=$((ALREADY_WATERMARKED + 1))
        fi

    done < <(find "$DIR" -type f -name "*.srt" -print0 2>/dev/null)
done

log "========================================================="
log "Injection Session Finished!"
log "Total SRT Files Scanned: ${FILES_SCANNED}"
log "New Watermarks Added: ${WATERMARKS_ADDED}"
log "Skipped (Already Watermarked): ${ALREADY_WATERMARKED}"
log "========================================================="
