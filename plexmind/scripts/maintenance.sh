#!/bin/bash
# ==============================================================================
# maintenance.sh — Library Maintenance Utility
# Version: 2.0 — Containerized (PlexMind Suite)
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

set -u

# --- CONFIGURATION ---
LOG_FILE="${LOG_FILE:-/app/data/maintenance.log}"
WHISPER_API_URL="${WHISPER_API_URL:-http://whisper:9000/asr}"

# --- LOAD SHARED LIBRARY ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh" || { echo "FATAL: Cannot load lib.sh"; exit 1; }

mkdir -p "$(dirname "$LOG_FILE")"
prepare_log_file

ALL_DIRS=("${MOVIE_DIR}" "${TV_DIR}")
MODE="${1:-help}"

case "$MODE" in
    audit)
        REPORT_FILE="${REPORT_DIR}/audit_$(date '+%Y-%m-%d_%H%M%S').txt"
        log "Running full library audit..."
        audit_library "$REPORT_FILE" "${ALL_DIRS[@]}"
        log "Audit complete. Report: ${REPORT_FILE}"
        cat "$REPORT_FILE"
        ;;

    report)
        log "Generating dashboard report..."
        generate_report
        REPORT_FILE="${REPORT_DIR}/report_$(date '+%Y-%m-%d').md"
        [ -f "$REPORT_FILE" ] && cat "$REPORT_FILE"
        ;;

    pgs-cleanup)
        log "Scanning for PGS subtitle files to clean up..."
        DELETED=$(cleanup_pgs "${ALL_DIRS[@]}")
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
        deduplicate_subs "${MOVIE_DIR}"
        deduplicate_subs "${TV_DIR}"
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
        deduplicate_subs "${MOVIE_DIR}"
        deduplicate_subs "${TV_DIR}"

        log "--- Phase 3: PGS Cleanup ---"
        cleanup_pgs "${ALL_DIRS[@]}" >/dev/null

        log "--- Phase 4: Audit ---"
        REPORT_FILE="${REPORT_DIR}/audit_$(date '+%Y-%m-%d_%H%M%S').txt"
        audit_library "$REPORT_FILE" "${ALL_DIRS[@]}"

        log "--- Phase 5: Dashboard ---"
        generate_report

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
