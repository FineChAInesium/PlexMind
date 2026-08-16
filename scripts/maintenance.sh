#!/bin/bash
# ==============================================================================
# maintenance.sh — Library Maintenance Utility
# Version: 0.8.20 — PlexMind release line
#
# Usage:
#   ./maintenance.sh audit       — Full library audit report
#   ./maintenance.sh report      — Dashboard from lifetime stats
#   ./maintenance.sh pgs-cleanup — Delete PGS subs where SRTs exist
#   ./maintenance.sh encoding    — Fix encoding on all SRT files
#   ./maintenance.sh dedup       — Remove duplicate subtitle files
#   ./maintenance.sh all         — Run everything
#
# Requires: lib.sh, python3
# ==============================================================================

set -uo pipefail

# --- CONFIGURATION ---
LOG_FILE="${LOG_FILE:-/app/data/maintenance.log}"
WHISPER_API_URL="${WHISPER_API_URL:-http://whisper:9000/asr}"

# --- LOAD SHARED LIBRARY ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh" || { echo "FATAL: Cannot load lib.sh"; exit 1; }

mkdir -p "$(dirname "$LOG_FILE")"
prepare_log_file

ALL_DIRS=("${MOVIE_DIR}" "${TV_DIR}")
validate_media_directories || exit 1
MODE="${1:-help}"

case "$MODE" in
    audit|report|help) ;;
    *) acquire_lock "/app/data/plexmind_media_mutation.lock" ;;
esac

case "$MODE" in
    audit)
        REPORT_FILE="${REPORT_DIR}/audit_$(date '+%Y-%m-%d_%H%M%S').txt"
        log "Running full library audit..."
        audit_library "$REPORT_FILE" "${ALL_DIRS[@]}" || { log "ERROR: Library audit failed."; exit 2; }
        if [ -s "${DATA_DIR:-/app/data}/gui-settings-audit.txt" ]; then
            printf '\n\n' >> "$REPORT_FILE"
            cat "${DATA_DIR:-/app/data}/gui-settings-audit.txt" >> "$REPORT_FILE"
        fi
        log "Audit complete. Report: ${REPORT_FILE}"
        cat "$REPORT_FILE"
        ;;

    report)
        log "Generating dashboard report..."
        generate_report || { log "ERROR: Report generation failed."; exit 2; }
        REPORT_FILE="${REPORT_DIR}/report_$(date '+%Y-%m-%d').md"
        [ -f "$REPORT_FILE" ] && cat "$REPORT_FILE"
        ;;

    pgs-cleanup)
        log "Scanning for PGS subtitle files to clean up..."
        cleanup_pgs "${ALL_DIRS[@]}" || { log "ERROR: PGS cleanup failed."; exit 2; }
        DELETED=${PGS_DELETED_COUNT:-0}
        log "PGS cleanup complete. Deleted: ${DELETED} files."
        ;;

    encoding)
        log "Scanning SRT files for encoding issues..."
        FIXED=0
        while IFS= read -r -d '' SRT; do
            if verify_encoding "$SRT"; then
                :
            else
                FIXED=$((FIXED+1))
            fi
        done < <(find "${ALL_DIRS[@]}" -type f -iname "*.srt" -print0 2>/dev/null)
        log "Encoding fix complete. Converted: ${FIXED} files."
        ;;

    dedup)
        log "Scanning for duplicate subtitle files..."
        deduplicate_subs "${MOVIE_DIR}" || { log "ERROR: Movie deduplication failed."; exit 2; }
        deduplicate_subs "${TV_DIR}" || { log "ERROR: TV deduplication failed."; exit 2; }
        log "Dedup complete."
        ;;

    all)
        log "========================================================="
        log "Full Library Maintenance"
        log "========================================================="

        log "--- Phase 1: Encoding ---"
        FIXED=0
        while IFS= read -r -d '' SRT; do
            verify_encoding "$SRT" || FIXED=$((FIXED+1))
        done < <(find "${ALL_DIRS[@]}" -type f -iname "*.srt" -print0 2>/dev/null)
        log "Encoding: fixed ${FIXED} files."

        log "--- Phase 2: Dedup ---"
        deduplicate_subs "${MOVIE_DIR}" || { log "ERROR: Movie deduplication failed."; exit 2; }
        deduplicate_subs "${TV_DIR}" || { log "ERROR: TV deduplication failed."; exit 2; }

        log "--- Phase 3: PGS Cleanup ---"
        cleanup_pgs "${ALL_DIRS[@]}" || { log "ERROR: PGS cleanup failed."; exit 2; }

        log "--- Phase 4: Audit ---"
        REPORT_FILE="${REPORT_DIR}/audit_$(date '+%Y-%m-%d_%H%M%S').txt"
        audit_library "$REPORT_FILE" "${ALL_DIRS[@]}" || { log "ERROR: Library audit failed."; exit 2; }
        if [ -s "${DATA_DIR:-/app/data}/gui-settings-audit.txt" ]; then
            printf '\n\n' >> "$REPORT_FILE"
            cat "${DATA_DIR:-/app/data}/gui-settings-audit.txt" >> "$REPORT_FILE"
        fi

        log "--- Phase 5: Dashboard ---"
        generate_report || { log "ERROR: Report generation failed."; exit 2; }

        log "========================================================="
        log "Maintenance complete."
        log "========================================================="
        cat "$REPORT_FILE"
        ;;

    help|*)
        echo "Usage: $0 {audit|report|pgs-cleanup|encoding|dedup|all}"
        echo ""
        echo "  audit       Full library health audit"
        echo "  report      Dashboard from lifetime stats"
        echo "  pgs-cleanup Delete PGS/image subs where SRTs exist"
        echo "  encoding    Fix non-UTF-8 SRT files"
        echo "  dedup       Remove duplicate subtitle files"
        echo "  all         Run all maintenance tasks"
        exit 1
        ;;
esac

exit 0
