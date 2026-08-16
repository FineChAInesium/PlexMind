#!/bin/bash
# ==============================================================================
# translate.sh — SRT Translation Backfill via llama.cpp LLM
# Version: 0.8.20 — PlexMind release line
#
# Finds .en.srt files, translates to target languages using llama.cpp's OpenAI-compatible chat API.
# Translates one cue per request, then rebuilds the SRT envelope from the
# source cue. This prevents the model from merging cues or moving dialogue
# across timestamps.
#
# Requires: lib.sh, curl, jq, python3
# ==============================================================================

set -u

# --- CONFIGURATION ---
LLAMA_CPP_URL="${LLAMA_CPP_URL:-http://llama-cpp:8080}"
LLAMA_CPP_API_URL="${LLAMA_CPP_API_URL:-${LLAMA_CPP_URL%/}/v1/chat/completions}"
LLAMA_CPP_MODEL="${LLAMA_CPP_MODEL:-qwen3.5-9b-q5_k_m}"
LLAMA_CPP_MAX_TOKENS="${LLAMA_CPP_MAX_TOKENS:-768}"
CHUNK_RETRY_ATTEMPTS="${CHUNK_RETRY_ATTEMPTS:-3}"
CHUNK_RETRY_DELAY_SECONDS="${CHUNK_RETRY_DELAY_SECONDS:-2}"
FAILED_RETRY_HOURS="${FAILED_RETRY_HOURS:-24}"
case "$CHUNK_RETRY_ATTEMPTS:$CHUNK_RETRY_DELAY_SECONDS:$FAILED_RETRY_HOURS" in
    *[!0-9:]*|0:*) echo "FATAL: Translation retry settings must be non-negative integers and attempts must be at least 1." >&2; exit 2 ;;
esac
SOURCE_LANG="${SOURCE_LANG:-en}"
# Cue alignment is a correctness boundary, not a tuning knob. Multi-cue
# requests let small models merge or shift dialogue between timestamps.
CHUNK_SIZE=1
LOG_FILE="${LOG_FILE:-/app/data/translation.log}"
LIFETIME_STATS_FILE="${LIFETIME_STATS_FILE:-/app/data/translation_stats.env}"

# Whisper URL required by lib.sh but unused here
WHISPER_API_URL="${WHISPER_API_URL:-http://whisper:9000/asr}"

# Target languages (comma-separated env var → array)
IFS=',' read -ra TARGET_LANGUAGES <<< "${TARGET_LANGUAGES:-zh,es-MX}"

HEALTH_CHECK_INTERVAL="${HEALTH_CHECK_INTERVAL:-5}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-7}"
MAX_RUNTIME_MINUTES="${MAX_RUNTIME_MINUTES:-0}"

# --- LOAD SHARED LIBRARY ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh" || { echo "FATAL: Cannot load lib.sh"; exit 1; }

mkdir -p "$(dirname "$LOG_FILE")"
prepare_log_file
acquire_lock "/tmp/translation_backfill.lock"
acquire_lock "${DATA_DIR}/plexmind_media_mutation.lock"
acquire_lock "${DATA_DIR}/plexmind_gpu.lock"

TEMP_JSON_PAYLOAD="/tmp/llama_cpp_payload.json"
TEMP_RESPONSE_FILE="/tmp/llama_cpp_response.json"

export TOTAL_FILES_SCANNED=0 TRANSLATIONS_PROCESSED=0 SKIPPED_EXISTING=0 SKIPPED_FAILED=0 FAILED_THIS_RUN=0
export SESSION_PROCESSING_SECONDS=0
FILES_SINCE_HEALTH_CHECK=0

# --- LIFETIME STATS ---
if [ -f "$LIFETIME_STATS_FILE" ]; then load_numeric_stats "$LIFETIME_STATS_FILE"; fi
LIFETIME_SCANNED="${LIFETIME_SCANNED:-0}"
LIFETIME_PROCESSED="${LIFETIME_PROCESSED:-0}"
LIFETIME_SKIPPED_EXISTING="${LIFETIME_SKIPPED_EXISTING:-0}"
LIFETIME_SKIPPED_FAILED="${LIFETIME_SKIPPED_FAILED:-0}"
LIFETIME_PROCESSING_SECONDS="${LIFETIME_PROCESSING_SECONDS:-0}"

# --- SYSTEM PROMPTS ---
get_system_prompt() {
    local lang="$1"
    case "$lang" in
        "zh")    echo "你是專業字幕翻譯員。將一句英文字幕翻譯為精簡的繁體中文。只輸出翻譯後的對白；不要輸出編號、時間戳、標籤、解釋或 Markdown。" ;;
        "es-MX") echo "Eres traductor profesional de subtítulos. Traduce una sola entrada al español de México de forma breve. Devuelve solamente el diálogo traducido: sin números, marcas de tiempo, etiquetas, explicaciones ni Markdown." ;;
        *)       echo "Translate one subtitle cue to $lang. Output only the translated dialogue, without cue numbers, timestamps, labels, explanations, or Markdown." ;;
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
    LIFETIME_SKIPPED_FAILED=$((LIFETIME_SKIPPED_FAILED + SKIPPED_FAILED + FAILED_THIS_RUN))
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
    log "Translation Session: Scanned:${TOTAL_FILES_SCANNED} Done:${TRANSLATIONS_PROCESSED} Skip-Exist:${SKIPPED_EXISTING} New-Fail:${FAILED_THIS_RUN} Deferred-Fail:${SKIPPED_FAILED}"
    log "Lifetime Total: ${LIFETIME_PROCESSED}"
    log "========================================================="
    rm -f "$TEMP_JSON_PAYLOAD" "$TEMP_RESPONSE_FILE" /tmp/translation_backfill.pid 2>/dev/null
    if [ "${MANAGE_LLAMA_CPP_CONTAINER:-0}" = "1" ]; then
        stop_docker_container "llama.cpp" "${LLAMA_CPP_CONTAINER_NAME:-llama-cpp}"
    fi
}
trap cleanup EXIT

# --- LLAMA.CPP HEALTH CHECK ---
health_check_llama_cpp() {
    local STATUS
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "${LLAMA_CPP_API_URL%/v1/chat/completions}/v1/models" 2>/dev/null)
    if [ "$STATUS" -eq 200 ]; then return 0; fi

    log "HEALTH CHECK: llama.cpp unresponsive (HTTP ${STATUS}). Waiting 60s..."
    sleep 60
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "${LLAMA_CPP_API_URL%/v1/chat/completions}/v1/models" 2>/dev/null)
    if [ "$STATUS" -eq 200 ]; then
        log "HEALTH CHECK: llama.cpp recovered."; return 0
    fi
    log "HEALTH CHECK: llama.cpp still down."; return 1
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
            local marker="${DIR_PATH}/${BASENAME_NO_EXT}.${TL}.failed"
            if [ -f "$marker" ]; then
                local marker_age=$(( $(date +%s) - $(stat -c %Y "$marker" 2>/dev/null || echo 0) ))
                [ "$marker_age" -lt $((FAILED_RETRY_HOURS * 3600)) ] && continue
            fi
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
    local _prev_chunk="$1" curr_chunk="$2" sys_prompt="$3" diagnostic_key="$4" target_lang="$5"
    local cue_id cue_timestamp source_dialogue previous_dialogue=""
    cue_id=$(printf '%s\n' "$curr_chunk" | sed -n '1p')
    cue_timestamp=$(printf '%s\n' "$curr_chunk" | sed -n '2p')
    source_dialogue=$(printf '%s\n' "$curr_chunk" | sed '1,2d')
    [ -z "$_prev_chunk" ] || previous_dialogue=$(printf '%s\n' "$_prev_chunk" | sed '1,2d')
    local user_message="$source_dialogue"

    local attempt HTTP_STATUS CURL_EXIT finish_reason rejection_reason="unknown" translated_file="/tmp/llama_translated_$$.txt"
    for attempt in $(seq 1 "$CHUNK_RETRY_ATTEMPTS"); do
        rm -f "$TEMP_RESPONSE_FILE" "$translated_file"
        rejection_reason="unknown"
        local retry_instruction=""
        [ "$attempt" -gt 1 ] && retry_instruction=" Previous output was invalid or untranslated. Return only a non-empty translation of the supplied dialogue in the requested target language."
        local contextual_prompt="${sys_prompt}${retry_instruction}"
        if [ -n "$previous_dialogue" ]; then
            contextual_prompt+=$'\nThe previous source cue is context only. Do not translate or repeat it:\n'"${previous_dialogue}"
        fi
        jq -n --arg model "$LLAMA_CPP_MODEL" --arg sys "$contextual_prompt" --arg user_msg "$user_message" --argjson max_tokens "$LLAMA_CPP_MAX_TOKENS" \
            '{model: $model, stream: false, temperature: 0.1, max_tokens: $max_tokens, chat_template_kwargs: {enable_thinking: false}, messages: [{role: "system", content: $sys}, {role: "user", content: $user_msg}]}' \
            > "$TEMP_JSON_PAYLOAD"

        HTTP_STATUS=$(curl -s -w "%{http_code}" -o "$TEMP_RESPONSE_FILE" \
            --connect-timeout 30 --max-time 600 -X POST -H "Content-Type: application/json" \
            -d @"$TEMP_JSON_PAYLOAD" "${LLAMA_CPP_API_URL}")
        CURL_EXIT=$?
        if [ "$CURL_EXIT" -eq 0 ] && [ "$HTTP_STATUS" = "200" ]; then
            jq -r '.choices[0].message.content // empty' < "$TEMP_RESPONSE_FILE" \
                | sed '/^```/d; /^\[TARGET TO TRANSLATE\]/d; /^\[PREVIOUS CONTEXT/d' > "$translated_file"
            finish_reason=$(jq -r '.choices[0].finish_reason // "unknown"' < "$TEMP_RESPONSE_FILE")
            if rejection_reason=$(python3 - "$source_dialogue" "$translated_file" "$target_lang" <<'PYEOF'
import re, sys
source, output_path, lang = sys.argv[1], sys.argv[2], sys.argv[3]
output = open(output_path, encoding="utf-8", errors="strict").read().strip()
def reject(reason):
    print(reason)
    raise SystemExit(1)
if not output:
    reject("empty_output")
if "-->" in output or re.search(r"(?im)^\s*```|</?think>|\[(?:target|previous context)", output):
    reject("unexpected_formatting")
if re.search(r"(?m)^\s*\d+\s*$", output):
    reject("cue_number_in_output")
letters = re.findall(r"[A-Za-z]", source)
src_words = re.findall(r"[A-Za-z]+", source.casefold())
out_words = re.findall(r"[A-Za-z]+", output.casefold())
# Short cues frequently consist only of names, acronyms, sound words, or
# interjections that legitimately remain unchanged. Treat language heuristics
# as a failure only for substantial dialogue; envelope validity is enforced
# independently when the final SRT is assembled.
substantial = len(src_words) >= 4 and sum(map(len, src_words)) >= 12
if lang == "zh" and letters and substantial and not re.search(r"[\u3400-\u9fff]", output):
    reject("missing_target_script")
if substantial and src_words == out_words:
    reject("substantial_dialogue_unchanged")
print("ok")
PYEOF
            ); then
                printf '%s\n%s\n' "$cue_id" "$cue_timestamp"
                cat "$translated_file"
                printf '\n\n'
                rm -f "$translated_file"
                return 0
            fi
            log "WARNING: Translation validation rejected cue on attempt ${attempt}/${CHUNK_RETRY_ATTEMPTS} (reason=${rejection_reason:-unknown}, finish=${finish_reason})."
        else
            rejection_reason="request_failed_curl_${CURL_EXIT}_http_${HTTP_STATUS:-none}"
            log "WARNING: llama.cpp request failed on chunk attempt ${attempt}/${CHUNK_RETRY_ATTEMPTS} (curl=${CURL_EXIT}, HTTP=${HTTP_STATUS:-none})."
        fi
        [ "$attempt" -lt "$CHUNK_RETRY_ATTEMPTS" ] && sleep "$CHUNK_RETRY_DELAY_SECONDS"
    done

    local diagnostic_dir="${DATA_DIR:-/app/data}/translation-failures"
    mkdir -p "$diagnostic_dir"
    jq -n --arg source_file "${SOURCE_FILE:-unknown}" --arg target_language "$target_lang" \
        --arg cue_id "$cue_id" --arg cue_timestamp "$cue_timestamp" --arg source_dialogue "$source_dialogue" \
        --arg previous_context "$previous_dialogue" --arg rejection_reason "${rejection_reason:-unknown}" \
        --argjson response "$(jq -c . "$TEMP_RESPONSE_FILE" 2>/dev/null || printf '{}')" \
        '{source_file: $source_file, target_language: $target_language, cue_id: $cue_id,
          cue_timestamp: $cue_timestamp, source_dialogue: $source_dialogue,
          previous_context: $previous_context, rejection_reason: $rejection_reason,
          finish_reason: ($response.choices[0].finish_reason // null),
          content: ($response.choices[0].message.content // null), error: ($response.error // null)}' \
        > "${diagnostic_dir}/${diagnostic_key}.json" 2>/dev/null || true
    chmod 600 "${diagnostic_dir}/${diagnostic_key}.json" 2>/dev/null || true
    rm -f "$translated_file"
    log "ERROR: Cue translation failed validation after ${CHUNK_RETRY_ATTEMPTS} attempts (reason=${rejection_reason:-unknown}); diagnostic=${diagnostic_key}.json"
    return 1
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

    if [ -f "$FAILED_MARKER_FILE" ]; then
        local marker_age=$(( $(date +%s) - $(stat -c %Y "$FAILED_MARKER_FILE" 2>/dev/null || echo 0) ))
        if [ "$marker_age" -lt $((FAILED_RETRY_HOURS * 3600)) ]; then
            SKIPPED_FAILED=$((SKIPPED_FAILED+1)); return
        fi
        log "RETRY: Expired failure marker for $(basename "$SOURCE_FILE") [${TARGET_LANG}]."
        rm -f "$FAILED_MARKER_FILE"
    fi

    shopt -s nullglob nocaseglob
    local EX=( "${DIR_PATH}/${BASENAME_NO_EXT}"*.${TARGET_LANG}.srt "${DIR_PATH}/${BASENAME_NO_EXT}.${TARGET_LANG}"*.srt )
    shopt -u nullglob nocaseglob
    if [ ${#EX[@]} -gt 0 ]; then SKIPPED_EXISTING=$((SKIPPED_EXISTING+1)); return; fi

    # Health check
    FILES_SINCE_HEALTH_CHECK=$((FILES_SINCE_HEALTH_CHECK + 1))
    if [ "$FILES_SINCE_HEALTH_CHECK" -ge "$HEALTH_CHECK_INTERVAL" ]; then
        health_check_llama_cpp || { log "FATAL: llama.cpp unrecoverable."; exit 1; }
        FILES_SINCE_HEALTH_CHECK=0
    fi

    log "--------------------------------------------------------"
    log "Translating to [${TARGET_LANG}]: $(basename "$SOURCE_FILE")"

    local TEMP_FINAL_FILE="${DIR_PATH}/${BASENAME_NO_EXT}.${TARGET_LANG}.temp"
    local CHECKPOINT_FILE="${TEMP_FINAL_FILE}.checkpoint"
    local SOURCE_SIGNATURE
    SOURCE_SIGNATURE="cue-v3-context:$(stat -c '%s:%Y' "$SOURCE_FILE")"
    local START_JOB_TIME
    START_JOB_TIME=$(date +%s)

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

    local previous_chunk="" resume_chunk=0
    if [ -s "$TEMP_FINAL_FILE" ] && [ -f "$CHECKPOINT_FILE" ]; then
        local checkpoint_signature=""
        read -r checkpoint_signature resume_chunk < "$CHECKPOINT_FILE" || true
        if [ "$checkpoint_signature" != "$SOURCE_SIGNATURE" ] || ! [[ "$resume_chunk" =~ ^[0-9]+$ ]]; then
            resume_chunk=0; rm -f "$TEMP_FINAL_FILE" "$CHECKPOINT_FILE"
        else
            log "RESUME: Continuing at chunk ${resume_chunk}/${total_chunks}."
        fi
    fi
    [ "$resume_chunk" -eq 0 ] && true > "$TEMP_FINAL_FILE"
    draw_progress "$resume_chunk" "$total_chunks"
    local chunk_index=0
    for cf in $chunk_files; do
        local cc
        cc=$(cat "$cf")
        if [ "$chunk_index" -lt "$resume_chunk" ]; then
            previous_chunk="$cc"; chunk_index=$((chunk_index+1)); continue
        fi
        local diagnostic_key
        diagnostic_key=$(printf '%s-%s-%05d' "$(printf '%s' "${SOURCE_FILE}:${TARGET_LANG}" | sha256sum | cut -c1-16)" "$TARGET_LANG" "$chunk_index")
        if ! translate_chunk "$previous_chunk" "$cc" "$SYSTEM_PROMPT" "$diagnostic_key" "$TARGET_LANG" >> "$TEMP_FINAL_FILE"; then
            chunk_success=false; break
        fi
        processed_chunks=$((chunk_index+1))
        printf '%s %s\n' "$SOURCE_SIGNATURE" "$processed_chunks" > "$CHECKPOINT_FILE"
        draw_progress "$processed_chunks" "$total_chunks"
        previous_chunk="$cc"
        chunk_index=$((chunk_index+1))
    done
    rm -rf "$CHUNK_DIR"
    echo ""

    if [[ "$chunk_success" == true && -s "$TEMP_FINAL_FILE" ]]; then
        if python3 - "$SOURCE_FILE" "$TEMP_FINAL_FILE" <<'PYEOF'
import re, sys
pattern = re.compile(r"^\s*(\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2}:\d{2}:\d{2},\d{3})\s*$", re.M)
source = open(sys.argv[1], encoding="utf-8", errors="replace").read()
output = open(sys.argv[2], encoding="utf-8", errors="replace").read()
raise SystemExit(0 if pattern.findall(source) and pattern.findall(output) == pattern.findall(source) else 1)
PYEOF
        then
            verify_encoding "$TEMP_FINAL_FILE"
            normalize_timestamps "$TEMP_FINAL_FILE" >/dev/null

            mv "$TEMP_FINAL_FILE" "$FINAL_OUTPUT_FILE"
            finalize_subtitle_permissions "$FINAL_OUTPUT_FILE" || return
            rm -f "$CHECKPOINT_FILE" "$FAILED_MARKER_FILE"
            log "SUCCESS: ${TARGET_LANG} translation complete."
            TRANSLATIONS_PROCESSED=$((TRANSLATIONS_PROCESSED+1))
            SESSION_PROCESSING_SECONDS=$(( SESSION_PROCESSING_SECONDS + $(date +%s) - START_JOB_TIME ))
        else
            log "ERROR: Complete translation does not preserve every source timestamp; partial output retained."; FAILED_THIS_RUN=$((FAILED_THIS_RUN+1)); touch "$FAILED_MARKER_FILE"
        fi
    else
        log "ERROR: Chunk failure; partial output retained for resume."; FAILED_THIS_RUN=$((FAILED_THIS_RUN+1)); touch "$FAILED_MARKER_FILE"
    fi
}

# ==============================================================================
# MAIN
# ==============================================================================
log "========================================================="
log "Translation Backfill v0.8.20 (containerized)"
log "Schedule: launched by PlexMind; max runtime: ${MAX_RUNTIME_MINUTES:-0}m; retention: ${LOG_RETENTION_DAYS}d; RUN_NOW=${RUN_NOW}"
log "========================================================="
check_dependencies curl jq python3

# Wait for PlexMind to finish if it's holding the GPU
PLEXMIND_SENTINEL="/app/data/plexmind.running"
if [ -f "$PLEXMIND_SENTINEL" ]; then
    log "PlexMind is running — waiting before using llama.cpp..."
    while [ -f "$PLEXMIND_SENTINEL" ]; do
        sleep 30
        check_runtime
    done
    log "PlexMind finished — proceeding."
fi

ALL_MEDIA_DIRS=("${MOVIE_DIR}" "${TV_DIR}")
validate_media_directories || exit 1
if [ "${MANAGE_LLAMA_CPP_CONTAINER:-0}" = "1" ]; then
    start_docker_container "llama.cpp" "${LLAMA_CPP_CONTAINER_NAME:-llama-cpp}" || exit 1
else
    log "llama.cpp lifecycle is externally managed; waiting for the configured endpoint."
fi

LLAMA_READY=0
for LLAMA_ATTEMPT in $(seq 1 "${LLAMA_STARTUP_ATTEMPTS:-60}"); do
    if curl -sS --fail --connect-timeout 3 --max-time 5 \
        "${LLAMA_CPP_API_URL%/v1/chat/completions}/v1/models" >/dev/null 2>&1; then
        LLAMA_READY=1
        log "llama.cpp API ready after ${LLAMA_ATTEMPT} probe(s)."
        break
    fi
    sleep "${LLAMA_STARTUP_INTERVAL_SECONDS:-2}"
done
if [ "$LLAMA_READY" -ne 1 ]; then
    log "ERROR: llama.cpp did not become ready after ${LLAMA_STARTUP_ATTEMPTS:-60} probes."
    exit 1
fi
calculate_pending_jobs

while IFS= read -r -d '' SUB_FILE; do
    check_runtime
    TOTAL_FILES_SCANNED=$((TOTAL_FILES_SCANNED+1))
    for LANG in "${TARGET_LANGUAGES[@]}"; do
        check_runtime
        process_subtitle "$SUB_FILE" "$LANG"
    done
done < <(find "${ALL_MEDIA_DIRS[@]}" -type f \( -iname "*.${SOURCE_LANG}.srt" -o -iname "*.${SOURCE_LANG}.sdh.srt" -o -iname "*.${SOURCE_LANG}.hi.srt" -o -iname "*.hi.${SOURCE_LANG}.srt" -o -iname "*.sdh.${SOURCE_LANG}.srt" \) -print0 2>/dev/null)

# Fix timestamp ordering in all translated SRT files
if [ -f "${SCRIPT_DIR}/fix_srt_ordering.py" ]; then
    log "Running SRT timestamp ordering fix..."
    python3 "${SCRIPT_DIR}/fix_srt_ordering.py" 2>&1 | while IFS= read -r line; do log "$line"; done
    log "SRT ordering fix complete."
fi

if [ "$SKIPPED_FAILED" -gt 0 ] || [ "$FAILED_THIS_RUN" -gt 0 ]; then
    log "COMPLETED_WITH_ERRORS: ${FAILED_THIS_RUN} new failure(s), ${SKIPPED_FAILED} deferred failure(s)."
    exit 2
fi
exit 0
