#!/bin/bash
# ==============================================================================
# translate.sh — SRT Translation Backfill via Ollama LLM
# Version: 2.0 — Containerized (PlexMind Suite)
#
# Finds .en.srt files, translates to target languages using Ollama chat API.
# Chunks SRT into groups of N cues, sends each with previous context for
# coherent translation. Post-processes with timestamp normalization and
# encoding verification.
#
# Requires: lib.sh, curl, jq, python3
# ==============================================================================

set -u

# --- CONFIGURATION ---
OLLAMA_API_URL="${OLLAMA_API_URL:-http://ollama:11434/api/chat}"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3.5:9b}"
SOURCE_LANG="${SOURCE_LANG:-en}"
CHUNK_SIZE="${CHUNK_SIZE:-5}"
LOG_FILE="${LOG_FILE:-/app/data/translation.log}"
LIFETIME_STATS_FILE="${LIFETIME_STATS_FILE:-/app/data/translation_stats.env}"

# Whisper URL required by lib.sh but unused here
WHISPER_API_URL="${WHISPER_API_URL:-http://whisper:9000/asr}"

# Target languages (comma-separated env var → array)
IFS=',' read -ra TARGET_LANGUAGES <<< "${TARGET_LANGUAGES:-zh,es-MX}"

HEALTH_CHECK_INTERVAL="${HEALTH_CHECK_INTERVAL:-5}"
START_HOUR="${START_HOUR:-${TRANSLATE_START_HOUR:-23}}"
END_HOUR="${END_HOUR:-${TRANSLATE_END_HOUR:-3}}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-7}"
MAX_RUNTIME_MINUTES="${MAX_RUNTIME_MINUTES:-0}"

# --- LOAD SHARED LIBRARY ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh" || { echo "FATAL: Cannot load lib.sh"; exit 1; }

mkdir -p "$(dirname "$LOG_FILE")"
prepare_log_file
acquire_lock "/tmp/translation_backfill.lock"

TEMP_JSON_PAYLOAD="/tmp/ollama_payload.json"
TEMP_RESPONSE_FILE="/tmp/ollama_response.json"

export TOTAL_FILES_SCANNED=0 TRANSLATIONS_PROCESSED=0 SKIPPED_EXISTING=0 SKIPPED_FAILED=0
export SESSION_PROCESSING_SECONDS=0
FILES_SINCE_HEALTH_CHECK=0

# --- LIFETIME STATS ---
if [ -f "$LIFETIME_STATS_FILE" ]; then source "$LIFETIME_STATS_FILE"; fi
LIFETIME_SCANNED="${LIFETIME_SCANNED:-0}"
LIFETIME_PROCESSED="${LIFETIME_PROCESSED:-0}"
LIFETIME_SKIPPED_EXISTING="${LIFETIME_SKIPPED_EXISTING:-0}"
LIFETIME_SKIPPED_FAILED="${LIFETIME_SKIPPED_FAILED:-0}"
LIFETIME_PROCESSING_SECONDS="${LIFETIME_PROCESSING_SECONDS:-0}"

# --- SYSTEM PROMPTS ---
get_system_prompt() {
    local lang="$1"
    case "$lang" in
        "zh")    echo "你是一位專業的字幕翻譯員。我會提供「先前的上下文 (請勿翻譯)」以及「需要翻譯的目標」。請只翻譯「需要翻譯的目標」部分為繁體中文。保留原始的時間戳記。不要輸出任何標籤或 Markdown。只輸出翻譯後的 SRT 區塊。" ;;
        "es-MX") echo "Eres un traductor profesional. Te proporcionaré 'CONTEXTO PREVIO (NO TRADUCIR)' y 'OBJETIVO A TRADUCIR'. Traduce SOLO el 'OBJETIVO A TRADUCIR' al español de México. Conserva los marcadores de tiempo. NO devuelvas las etiquetas de instrucción ni Markdown. Devuelve solo los bloques SRT traducidos." ;;
        *)       echo "You are a professional subtitle translator. Translate ONLY the 'TARGET TO TRANSLATE' block to $lang. Keep timestamps intact. Output raw translated SRT blocks only." ;;
    esac
}

# --- PROGRESS BAR ---
draw_progress() {
    local current=$1 total=$2
    [ "${total:-0}" -le 0 ] && total=1
    local pct=$(( (current * 100) / total ))
    local filled=$(( (pct * 40) / 100 ))
    local empty=$(( 40 - filled ))
    printf "\r[%s%s] %d%% (%d/%d chunks)" \
        "$(printf "%${filled}s" | tr ' ' '#')" \
        "$(printf "%${empty}s" | tr ' ' '-')" \
        "$pct" "$current" "$total" >&2
    if (( current % 10 == 0 || current == total )); then
        echo "$(date '+%Y-%m-%d %H:%M:%S') - Progress: ${pct}% (${current}/${total})" >> "$LOG_FILE"
    fi
}

# --- CLEANUP TRAP ---
cleanup() {
    LIFETIME_SCANNED=$((LIFETIME_SCANNED + TOTAL_FILES_SCANNED))
    LIFETIME_PROCESSED=$((LIFETIME_PROCESSED + TRANSLATIONS_PROCESSED))
    LIFETIME_SKIPPED_EXISTING=$((LIFETIME_SKIPPED_EXISTING + SKIPPED_EXISTING))
    LIFETIME_SKIPPED_FAILED=$((LIFETIME_SKIPPED_FAILED + SKIPPED_FAILED))
    LIFETIME_PROCESSING_SECONDS=$((LIFETIME_PROCESSING_SECONDS + SESSION_PROCESSING_SECONDS))

    cat <<EOF > "$LIFETIME_STATS_FILE"
LIFETIME_SCANNED=$LIFETIME_SCANNED
LIFETIME_PROCESSED=$LIFETIME_PROCESSED
LIFETIME_SKIPPED_EXISTING=$LIFETIME_SKIPPED_EXISTING
LIFETIME_SKIPPED_FAILED=$LIFETIME_SKIPPED_FAILED
LIFETIME_PROCESSING_SECONDS=$LIFETIME_PROCESSING_SECONDS
EOF

    echo ""
    log "========================================================="
    log "Translation Session: Scanned:${TOTAL_FILES_SCANNED} Done:${TRANSLATIONS_PROCESSED} Skip-Exist:${SKIPPED_EXISTING} Skip-Fail:${SKIPPED_FAILED}"
    log "Lifetime Total: ${LIFETIME_PROCESSED}"
    log "========================================================="
    # Unload model from VRAM
    curl -s "${OLLAMA_API_URL%/chat}/generate" -d "{\"model\": \"${OLLAMA_MODEL}\", \"keep_alive\": 0}" >/dev/null
    rm -f "$TEMP_JSON_PAYLOAD" "$TEMP_RESPONSE_FILE" /tmp/translation_backfill.pid 2>/dev/null
}
trap cleanup EXIT

# --- OLLAMA HEALTH CHECK ---
health_check_ollama() {
    local STATUS
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "${OLLAMA_API_URL%/chat}/tags" 2>/dev/null)
    if [ "$STATUS" -eq 200 ]; then return 0; fi

    log "HEALTH CHECK: Ollama unresponsive (HTTP ${STATUS}). Waiting 60s..."
    sleep 60
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "${OLLAMA_API_URL%/chat}/tags" 2>/dev/null)
    if [ "$STATUS" -eq 200 ]; then
        log "HEALTH CHECK: Ollama recovered."; return 0
    fi
    log "HEALTH CHECK: Ollama still down."; return 1
}

# --- CALCULATE PENDING ---
calculate_pending_jobs() {
    log "Pre-scanning for pending translations..."
    local TEMP_TOTAL=0 TEMP_PENDING=0

    while IFS= read -r -d '' SUB_FILE; do
        TEMP_TOTAL=$((TEMP_TOTAL+1))
        local DIR_PATH BASENAME_NO_EXT
        DIR_PATH=$(dirname "$SUB_FILE")
        BASENAME_NO_EXT=$(basename "$SUB_FILE" | sed -E "s/\.${SOURCE_LANG}(\.hi|\.sdh)?\.srt$//I" | sed -E "s/(\.hi|\.sdh)\.${SOURCE_LANG}\.srt$//I")

        for TL in "${TARGET_LANGUAGES[@]}"; do
            [ -f "${DIR_PATH}/${BASENAME_NO_EXT}.${TL}.failed" ] && continue
            shopt -s nullglob nocaseglob
            local EX=( "${DIR_PATH}/${BASENAME_NO_EXT}"*.${TL}.srt "${DIR_PATH}/${BASENAME_NO_EXT}.${TL}"*.srt )
            shopt -u nullglob nocaseglob
            [ ${#EX[@]} -eq 0 ] && TEMP_PENDING=$((TEMP_PENDING+1))
        done
    done < <(find "${ALL_MEDIA_DIRS[@]}" -type f \( -iname "*.${SOURCE_LANG}.srt" -o -iname "*.${SOURCE_LANG}.sdh.srt" -o -iname "*.${SOURCE_LANG}.hi.srt" -o -iname "*.hi.${SOURCE_LANG}.srt" -o -iname "*.sdh.${SOURCE_LANG}.srt" \) -print0 2>/dev/null)

    log "LIBRARY: ${TEMP_TOTAL} source subs, ${TEMP_PENDING} pending translations"

    if [ "$LIFETIME_PROCESSED" -gt 0 ] && [ "$LIFETIME_PROCESSING_SECONDS" -gt 0 ]; then
        local AVG=$(( LIFETIME_PROCESSING_SECONDS / LIFETIME_PROCESSED ))
        local ETA=$(( TEMP_PENDING * AVG ))
        local D=$((ETA/86400)) H=$(((ETA%86400)/3600)) M=$(((ETA%3600)/60))
        local S=""; [ "$D" -gt 0 ] && S="${D}d "; S="${S}${H}h ${M}m"
        log "ETA: ${S} (Avg ${AVG}s/file)"
    fi

    [ "$TEMP_PENDING" -eq 0 ] && { log "Library fully translated!"; exit 0; }
}

# --- TRANSLATE CHUNK ---
translate_chunk() {
    local prev_chunk="$1" curr_chunk="$2" sys_prompt="$3"
    local user_message=""
    [ -n "$prev_chunk" ] && user_message="[PREVIOUS CONTEXT (DO NOT TRANSLATE)]\n${prev_chunk}\n"
    user_message+="[TARGET TO TRANSLATE]\n${curr_chunk}"

    jq -n --arg model "$OLLAMA_MODEL" --arg sys "$sys_prompt" --arg user_msg "$user_message" \
        '{model: $model, stream: false, think: false, options: {temperature: 0.1, num_predict: 2048}, messages: [{role: "system", content: $sys}, {role: "user", content: $user_msg}]}' \
        > "$TEMP_JSON_PAYLOAD"

    local HTTP_STATUS
    HTTP_STATUS=$(curl -s -w "%{http_code}" -o "$TEMP_RESPONSE_FILE" \
        --connect-timeout 30 --max-time 600 \
        -X POST -H "Content-Type: application/json" \
        -d @"$TEMP_JSON_PAYLOAD" "${OLLAMA_API_URL}")
    local CURL_EXIT=$?

    if [ $CURL_EXIT -ne 0 ]; then log "ERROR: curl exit $CURL_EXIT"; return 1; fi
    if [ "$HTTP_STATUS" != "200" ]; then log "ERROR: Ollama HTTP $HTTP_STATUS"; return 1; fi

    local TRANSLATED
    TRANSLATED=$(jq -r '.message.content' < "$TEMP_RESPONSE_FILE")
    TRANSLATED=$(echo "$TRANSLATED" | sed '/^```/d' | sed '/^\[TARGET TO TRANSLATE\]/d' | sed '/^\[PREVIOUS CONTEXT/d')

    if ! echo "$TRANSLATED" | grep -qF -- '-->'; then
        log "ERROR: Chunk has no SRT timestamps — hallucinated prose."
        return 1
    fi

    echo "$TRANSLATED"; echo ""
    return 0
}

# --- PROCESS SUBTITLE ---
process_subtitle() {
    local SOURCE_FILE="$1" TARGET_LANG="$2"
    local SYSTEM_PROMPT
    SYSTEM_PROMPT=$(get_system_prompt "$TARGET_LANG")

    local DIR_PATH BASENAME_NO_EXT
    DIR_PATH=$(dirname "$SOURCE_FILE")
    BASENAME_NO_EXT=$(basename "$SOURCE_FILE" | sed -E "s/\.${SOURCE_LANG}(\.hi|\.sdh)?\.srt$//I" | sed -E "s/(\.hi|\.sdh)\.${SOURCE_LANG}\.srt$//I")

    local FINAL_OUTPUT_FILE="${DIR_PATH}/${BASENAME_NO_EXT}.${TARGET_LANG}.srt"
    local FAILED_MARKER_FILE="${DIR_PATH}/${BASENAME_NO_EXT}.${TARGET_LANG}.failed"

    if [ -f "$FAILED_MARKER_FILE" ]; then SKIPPED_FAILED=$((SKIPPED_FAILED+1)); return; fi

    shopt -s nullglob nocaseglob
    local EX=( "${DIR_PATH}/${BASENAME_NO_EXT}"*.${TARGET_LANG}.srt "${DIR_PATH}/${BASENAME_NO_EXT}.${TARGET_LANG}"*.srt )
    shopt -u nullglob nocaseglob
    if [ ${#EX[@]} -gt 0 ]; then SKIPPED_EXISTING=$((SKIPPED_EXISTING+1)); return; fi

    # Health check
    FILES_SINCE_HEALTH_CHECK=$((FILES_SINCE_HEALTH_CHECK + 1))
    if [ $FILES_SINCE_HEALTH_CHECK -ge $HEALTH_CHECK_INTERVAL ]; then
        health_check_ollama || { log "FATAL: Ollama unrecoverable."; exit 1; }
        FILES_SINCE_HEALTH_CHECK=0
    fi

    log "--------------------------------------------------------"
    log "Translating to [${TARGET_LANG}]: $(basename "$SOURCE_FILE")"

    local TEMP_FINAL_FILE="${DIR_PATH}/${BASENAME_NO_EXT}.${TARGET_LANG}.temp"
    > "$TEMP_FINAL_FILE"
    local START_JOB_TIME=$(date +%s)

    # Structural chunk splitting via Python
    local total_blocks
    total_blocks=$(grep -c -- "-->" "$SOURCE_FILE")
    [ "${total_blocks:-0}" -eq 0 ] && total_blocks=1
    local total_chunks=$(( (total_blocks + CHUNK_SIZE - 1) / CHUNK_SIZE ))

    local chunk_success=true processed_chunks=0
    local CHUNK_DIR="/tmp/srt_chunks_$$"
    mkdir -p "$CHUNK_DIR"

    python3 - "$SOURCE_FILE" "$CHUNK_DIR" "$CHUNK_SIZE" <<'PYEOF'
import sys, re, os
srt_path, chunk_dir, chunk_size = sys.argv[1], sys.argv[2], int(sys.argv[3])
with open(srt_path, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()
content = content.replace('\r\n', '\n').replace('\r', '\n')
blocks = [b.strip() for b in re.split(r'\n{2,}', content.strip()) if ' --> ' in b]
for ci in range(0, len(blocks), chunk_size):
    chunk = '\n\n'.join(blocks[ci:ci+chunk_size]) + '\n\n'
    with open(os.path.join(chunk_dir, f'chunk_{ci//chunk_size:05d}.txt'), 'w', encoding='utf-8') as f:
        f.write(chunk)
PYEOF

    local chunk_files
    chunk_files=$(ls "$CHUNK_DIR"/chunk_*.txt 2>/dev/null | sort)
    if [ -z "$chunk_files" ]; then
        log "ERROR: Failed to split SRT."
        rm -rf "$CHUNK_DIR" "$TEMP_FINAL_FILE"
        touch "$FAILED_MARKER_FILE"
        return
    fi

    local previous_chunk=""
    draw_progress 0 "$total_chunks"
    for cf in $chunk_files; do
        local cc
        cc=$(cat "$cf")
        if ! translate_chunk "$previous_chunk" "$cc" "$SYSTEM_PROMPT" >> "$TEMP_FINAL_FILE"; then
            chunk_success=false; break
        fi
        ((processed_chunks++))
        draw_progress "$processed_chunks" "$total_chunks"
        previous_chunk="$cc"
    done
    rm -rf "$CHUNK_DIR"
    echo ""

    if [[ "$chunk_success" == true && -s "$TEMP_FINAL_FILE" ]]; then
        if grep -q -- "-->" "$TEMP_FINAL_FILE"; then
            verify_encoding "$TEMP_FINAL_FILE"
            normalize_timestamps "$TEMP_FINAL_FILE" >/dev/null

            mv "$TEMP_FINAL_FILE" "$FINAL_OUTPUT_FILE"
            log "SUCCESS: ${TARGET_LANG} translation complete."
            TRANSLATIONS_PROCESSED=$((TRANSLATIONS_PROCESSED+1))
            SESSION_PROCESSING_SECONDS=$(( SESSION_PROCESSING_SECONDS + $(date +%s) - START_JOB_TIME ))
        else
            log "ERROR: Invalid SRT."; rm -f "$TEMP_FINAL_FILE"; touch "$FAILED_MARKER_FILE"
        fi
    else
        log "ERROR: Chunk failure."; rm -f "$TEMP_FINAL_FILE"; touch "$FAILED_MARKER_FILE"
    fi
}

# ==============================================================================
# MAIN
# ==============================================================================
log "========================================================="
log "Translation Backfill v2.0 (containerized)"
log "Window: $(time_window_label) ($(time_window_hours)h); max runtime: ${MAX_RUNTIME_MINUTES:-0}m; retention: ${LOG_RETENTION_DAYS}d; RUN_NOW=${RUN_NOW}"
log "========================================================="
check_dependencies curl jq python3

# Wait for PlexMind to finish if it's holding the GPU
PLEXMIND_SENTINEL="/tmp/plexmind.running"
if [ -f "$PLEXMIND_SENTINEL" ]; then
    log "PlexMind is running — waiting before using Ollama..."
    while [ -f "$PLEXMIND_SENTINEL" ]; do
        sleep 30
        check_run_limits
    done
    log "PlexMind finished — proceeding."
fi

if ! curl -s --connect-timeout 5 "${OLLAMA_API_URL%/chat}/tags" >/dev/null; then
    log "ERROR: Ollama not responding."; exit 1
fi

ALL_MEDIA_DIRS=("${MOVIE_DIR}" "${TV_DIR}")
calculate_pending_jobs

while IFS= read -r -d '' SUB_FILE; do
    check_run_limits
    TOTAL_FILES_SCANNED=$((TOTAL_FILES_SCANNED+1))
    for LANG in "${TARGET_LANGUAGES[@]}"; do
        check_run_limits
        process_subtitle "$SUB_FILE" "$LANG"
    done
done < <(find "${ALL_MEDIA_DIRS[@]}" -type f \( -iname "*.${SOURCE_LANG}.srt" -o -iname "*.${SOURCE_LANG}.sdh.srt" -o -iname "*.${SOURCE_LANG}.hi.srt" -o -iname "*.hi.${SOURCE_LANG}.srt" -o -iname "*.sdh.${SOURCE_LANG}.srt" \) -print0 2>/dev/null)

# Fix timestamp ordering in all translated SRT files
if [ -f "${SCRIPT_DIR}/fix_srt_ordering.py" ]; then
    log "Running SRT timestamp ordering fix..."
    python3 "${SCRIPT_DIR}/fix_srt_ordering.py" 2>&1 | while IFS= read -r line; do log "$line"; done
    log "SRT ordering fix complete."
fi

exit 0
