#!/bin/bash
# ==============================================================================
# maintenance.sh — Library Maintenance Utility
# Version: 0.8.20 — PlexMind release line
#
# Usage:
#   ./maintenance.sh audit       — Full library audit report
#   ./maintenance.sh report      — Dashboard from lifetime stats
#   ./maintenance.sh pgs-cleanup — Quarantine PGS subs that matching SRTs replace
#   ./maintenance.sh encoding    — Fix encoding on all SRT files (originals backed up)
#   ./maintenance.sh ordering    — Fix timestamp order in every *.es-MX/*.zh translation
#                                  (run once after upgrading; translate.sh now checks
#                                  only the files it writes)
#   ./maintenance.sh dedup       — Quarantine duplicate subtitle files
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

# A single `srt_encoding.py check` classifies every SRT (the huge --limit turns
# off the audit's sampling), so verify_encoding, one python3 start per file,
# runs only for the few that are not plain UTF-8. Sets FIXED; fails when the
# check did not finish.
repair_library_encoding() {
    local ENC SRT CHECKED=""
    FIXED=0
    while IFS='|' read -r -d '' ENC SRT; do
        if [ "$ENC" = "checked" ]; then
            CHECKED="$SRT"
            continue
        fi
        verify_encoding "$SRT" || FIXED=$((FIXED+1))
    done < <(find "${ALL_DIRS[@]}" -type f -iname "*.srt" -print0 2>/dev/null \
        | python3 "$(script_helper srt_encoding.py)" check --limit 2147483647)
    [ -n "$CHECKED" ]
}

# Without paths fix_srt_ordering.py sweeps every script translation; this job
# already holds the media mutation lock the fixer would take. Fails when a
# file could not be checked or fixed.
fix_library_ordering() {
    PLEXMIND_MEDIA_LOCK_HELD=1 python3 "$(script_helper fix_srt_ordering.py)" 2>&1 | while IFS= read -r line; do log "$line"; done
    return "${PIPESTATUS[0]}"
}

ALL_DIRS=("${MOVIE_DIR}" "${TV_DIR}")
validate_media_directories || exit 1
MODE="${1:-help}"
EXIT_CODE=0

case "$MODE" in
    audit|report|help) ;;
    *) acquire_lock "${DATA_DIR}/plexmind_media_mutation.lock" ;;
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
        log "PGS cleanup complete. Quarantined: ${PGS_DELETED_COUNT:-0} files."
        ;;

    encoding)
        log "Scanning SRT files for encoding issues..."
        repair_library_encoding || { log "ERROR: Encoding scan failed."; exit 2; }
        log "Encoding fix complete. Converted: ${FIXED} files."
        ;;

    ordering)
        log "Checking timestamp order in script translations..."
        fix_library_ordering || { log "ERROR: Some translations could not be checked or fixed; see above."; exit 2; }
        log "Ordering fix complete."
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
        repair_library_encoding || { log "ERROR: Encoding scan failed."; exit 2; }
        log "Encoding: fixed ${FIXED} files."

        # After the encoding repair, which converts what the fixer refuses to
        # read. A file it still cannot fix must not stop the other phases.
        log "--- Phase 2: Ordering ---"
        fix_library_ordering || { log "WARNING: Some translations could not be checked or fixed; see above."; EXIT_CODE=2; }

        log "--- Phase 3: Dedup ---"
        deduplicate_subs "${MOVIE_DIR}" || { log "ERROR: Movie deduplication failed."; exit 2; }
        deduplicate_subs "${TV_DIR}" || { log "ERROR: TV deduplication failed."; exit 2; }

        log "--- Phase 4: PGS Cleanup ---"
        cleanup_pgs "${ALL_DIRS[@]}" || { log "ERROR: PGS cleanup failed."; exit 2; }

        log "--- Phase 5: Audit ---"
        REPORT_FILE="${REPORT_DIR}/audit_$(date '+%Y-%m-%d_%H%M%S').txt"
        audit_library "$REPORT_FILE" "${ALL_DIRS[@]}" || { log "ERROR: Library audit failed."; exit 2; }
        if [ -s "${DATA_DIR:-/app/data}/gui-settings-audit.txt" ]; then
            printf '\n\n' >> "$REPORT_FILE"
            cat "${DATA_DIR:-/app/data}/gui-settings-audit.txt" >> "$REPORT_FILE"
        fi

        log "--- Phase 6: Dashboard ---"
        generate_report || { log "ERROR: Report generation failed."; exit 2; }

        log "========================================================="
        log "Maintenance complete."
        log "========================================================="
        cat "$REPORT_FILE"
        ;;

    help|*)
        echo "Usage: $0 {audit|report|pgs-cleanup|encoding|ordering|dedup|all}"
        echo ""
        echo "  audit       Full library health audit"
        echo "  report      Dashboard from lifetime stats"
        echo "  pgs-cleanup Quarantine PGS/image subs that matching SRTs replace"
        echo "  encoding    Fix non-UTF-8 SRT files"
        echo "  ordering    Fix timestamp order in every es-MX/zh translation (run once after upgrading)"
        echo "  dedup       Quarantine duplicate subtitle files"
        echo "  all         Run all maintenance tasks"
        exit 1
        ;;
esac

exit "$EXIT_CODE"
