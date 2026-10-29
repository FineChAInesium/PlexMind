#!/bin/bash
# ==============================================================================
# translate.sh — SRT Translation Backfill via llama.cpp LLM
# Version: 0.8.20 — PlexMind release line
#
# Finds .en.srt files, translates to target languages using llama.cpp's OpenAI-compatible chat API.
# Sends TRANSLATION_BATCH_CUES consecutive cues per request as a JSON array and
# requires a JSON array of exactly that many translations back; any cue the
# batch does not deliver is retried one cue per request. The SRT envelope is
# always rebuilt from the source cue, so the model cannot merge cues or move
# dialogue across timestamps.
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
# A cue the model cannot translate keeps its source line; a file fails only
# when more than this percentage of its cues had to fall back.
TRANSLATION_MAX_FALLBACK_PCT="${TRANSLATION_MAX_FALLBACK_PCT:-10}"
# Consecutive cues per llama.cpp request; 1 sends one cue per request.
TRANSLATION_BATCH_CUES="${TRANSLATION_BATCH_CUES:-8}"
case "$CHUNK_RETRY_ATTEMPTS:$CHUNK_RETRY_DELAY_SECONDS:$FAILED_RETRY_HOURS:$TRANSLATION_MAX_FALLBACK_PCT" in
    *[!0-9:]*|0:*) echo "FATAL: Translation retry settings must be non-negative integers and attempts must be at least 1." >&2; exit 2 ;;
esac
case "$TRANSLATION_BATCH_CUES:$LLAMA_CPP_MAX_TOKENS" in
    *[!0-9:]*|*:*:*|0*|*:0*) echo "FATAL: TRANSLATION_BATCH_CUES and LLAMA_CPP_MAX_TOKENS must be positive integers." >&2; exit 2 ;;
esac
SOURCE_LANG="${SOURCE_LANG:-en}"
# Cue alignment is a correctness boundary, not a tuning knob: one chunk is
# one cue, and checkpoints and the SRT envelope are per cue. Batched requests
# return one JSON item per cue instead of free text, which small models would
# merge or shift between timestamps.
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
TRANSLATED_FILES=()

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

get_batch_system_prompt() {
    local lang="$1"
    case "$lang" in
        "zh")    echo "你是專業字幕翻譯員。輸入是一個 JSON 字串陣列，每個元素是一條英文字幕。將每個元素翻譯為精簡的繁體中文，只輸出一個 JSON 字串陣列：元素數量和順序與輸入完全相同，第 N 個元素只包含第 N 條字幕的譯文。保留換行和 <i> 等格式標籤；不要合併、拆分或省略字幕，不要輸出編號、時間戳、解釋或 Markdown。" ;;
        "es-MX") echo "Eres traductor profesional de subtítulos. La entrada es un arreglo JSON de cadenas; cada elemento es un subtítulo en inglés. Traduce cada elemento al español de México de forma breve y devuelve solamente un arreglo JSON de cadenas con la misma cantidad de elementos y en el mismo orden: el elemento N contiene solo la traducción del subtítulo N. Conserva los saltos de línea y las etiquetas de formato como <i>; no combines, dividas ni omitas subtítulos, y no agregues números, marcas de tiempo, explicaciones ni Markdown." ;;
        *)       echo "The input is a JSON array of strings, each one subtitle cue. Translate every element to $lang and output only a JSON array of strings with the same number of elements in the same order: element N holds only the translation of cue N. Keep line breaks and formatting tags such as <i>; do not merge, split, or drop cues, and do not add cue numbers, timestamps, explanations, or Markdown." ;;
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
    if [ "${LLAMA_ON_DEMAND:-0}" = "1" ]; then
        # Start the shared idle countdown; the recommendation worker stops
        # llama.cpp once nobody has used it for LLAMA_IDLE_STOP_MINUTES.
        touch "${DATA_DIR:-/app/data}/llama_last_used" 2>/dev/null || true
    elif [ "${MANAGE_LLAMA_CPP_CONTAINER:-0}" = "1" ]; then
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

# --- SOURCE NAMES ---
# Sets DIR_PATH and BASENAME_NO_EXT (the name without .en[.hi|.sdh].srt) for
# source subtitle $1 without forking: both library walks call it per file.
split_source_path() {
    local name="${1##*/}"
    DIR_PATH="${1%/*}"
    shopt -s nocasematch
    [[ "$name" =~ ^(.*)\."$SOURCE_LANG"(\.hi|\.sdh)?\.srt$ ]] && name="${BASH_REMATCH[1]}"
    shopt -u nocasematch
    BASENAME_NO_EXT="$name"
}

# --- CALCULATE PENDING ---
calculate_pending_jobs() {
    log "Pre-scanning for pending translations..."
    local TEMP_TOTAL=0 TEMP_PENDING=0

    while IFS= read -r -d '' SUB_FILE; do
        TEMP_TOTAL=$((TEMP_TOTAL+1))
        local DIR_PATH BASENAME_NO_EXT
        split_source_path "$SUB_FILE"

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

# --- MODEL OUTPUT ERRORS ---
# llama.cpp answers HTTP 500 when its chat-output parser rejects what the
# model generated ("does not match the expected peg-native format"). That is
# a bad sample, not an outage: resample it, then treat it like a rejected cue.
is_model_output_error() {
    local status="$1" response_file="$2" message
    [ "$status" = "500" ] || return 1
    message=$(jq -r '.error.message? // empty' < "$response_file" 2>/dev/null)
    case "$message" in
        *peg-native*|*"does not match the expected"*) return 0 ;;
    esac
    return 1
}

# --- TRANSLATE CHUNK ---
# Appends the translated cue to $6. It never writes subtitle text to stdout,
# so log() output (which echoes to stdout) cannot leak into the .srt.
# $7 is the cue's kind from `translation_cue.py classify`, if that ran.
translate_chunk() {
    local _prev_chunk="$1" curr_chunk="$2" sys_prompt="$3" diagnostic_key="$4" target_lang="$5" out_file="$6" cue_kind="${7:-}"
    local cue_id cue_timestamp source_dialogue previous_dialogue=""
    cue_id=$(printf '%s\n' "$curr_chunk" | sed -n '1p')
    cue_timestamp=$(printf '%s\n' "$curr_chunk" | sed -n '2p')
    source_dialogue=$(printf '%s\n' "$curr_chunk" | sed '1,2d')
    [ -z "$_prev_chunk" ] || previous_dialogue=$(printf '%s\n' "$_prev_chunk" | sed '1,2d')

    # The PlexMind watermark cue is copied verbatim, never translated. Only an
    # unclassified cue needs the helper to tell.
    if [ "$cue_kind" = "passthrough" ] || { [ -z "$cue_kind" ] \
        && python3 "${SCRIPT_DIR}/translation_cue.py" passthrough "$source_dialogue"; }; then
        printf '%s\n%s\n%s\n\n' "$cue_id" "$cue_timestamp" "$source_dialogue" >> "$out_file"
        return 0
    fi

    local user_message
    user_message=$(get_user_message "$target_lang" "$source_dialogue")

    local attempt HTTP_STATUS CURL_EXIT finish_reason rejection_reason="unknown" translated_file="/tmp/llama_translated_$$.txt"
    for attempt in $(seq 1 "$CHUNK_RETRY_ATTEMPTS"); do
        rm -f "$TEMP_RESPONSE_FILE" "$translated_file"
        rejection_reason="unknown"
        local retry_instruction="" temperature="0.1"
        if [ "$attempt" -gt 1 ]; then
            retry_instruction=" Previous output was invalid or untranslated. Return only a non-empty translation of the supplied dialogue in the requested target language."
            # Resample (new seed, warmer) instead of replaying a near-greedy reply.
            temperature="0.$(( attempt * 2 - 1 < 7 ? attempt * 2 - 1 : 7 ))"
        fi
        local contextual_prompt="${sys_prompt}${retry_instruction}"
        if [ -n "$previous_dialogue" ]; then
            contextual_prompt+=$'\nThe previous source cue is context only. Do not translate or repeat it:\n'"${previous_dialogue}"
        fi
        jq -n --arg model "$LLAMA_CPP_MODEL" --arg sys "$contextual_prompt" --arg user_msg "$user_message" --argjson max_tokens "$LLAMA_CPP_MAX_TOKENS" \
            --argjson temperature "$temperature" --argjson attempt "$attempt" \
            '{model: $model, stream: false, temperature: $temperature, max_tokens: $max_tokens, chat_template_kwargs: {enable_thinking: false}, messages: [{role: "system", content: $sys}, {role: "user", content: $user_msg}]}
             + (if $attempt > 1 then {seed: $attempt} else {} end)' \
            > "$TEMP_JSON_PAYLOAD"

        HTTP_STATUS=$(curl -s -w "%{http_code}" -o "$TEMP_RESPONSE_FILE" \
            --connect-timeout 30 --max-time 600 -X POST -H "Content-Type: application/json" \
            -d @"$TEMP_JSON_PAYLOAD" "${LLAMA_CPP_API_URL}")
        CURL_EXIT=$?
        if [ "$CURL_EXIT" -eq 0 ] && [ "$HTTP_STATUS" = "200" ]; then
            # Blank lines would end the SRT cue early, so they are dropped too.
            jq -r '.choices[0].message.content // empty' < "$TEMP_RESPONSE_FILE" \
                | sed '/^```/d; /^\[TARGET TO TRANSLATE\]/d; /^\[PREVIOUS CONTEXT/d; /^[[:space:]]*$/d' > "$translated_file"
            finish_reason=$(jq -r '.choices[0].finish_reason // "unknown"' < "$TEMP_RESPONSE_FILE")
            if rejection_reason=$(python3 "${SCRIPT_DIR}/translation_cue.py" validate "$source_dialogue" "$translated_file" "$target_lang"); then
                {
                    printf '%s\n%s\n' "$cue_id" "$cue_timestamp"
                    cat "$translated_file"
                    printf '\n'
                } >> "$out_file"
                rm -f "$translated_file"
                return 0
            fi
            log "WARNING: Translation validation rejected cue on attempt ${attempt}/${CHUNK_RETRY_ATTEMPTS} (reason=${rejection_reason:-unknown}, finish=${finish_reason})."
        elif [ "$CURL_EXIT" -eq 0 ] && is_model_output_error "$HTTP_STATUS" "$TEMP_RESPONSE_FILE"; then
            rejection_reason="unparseable_model_output"
            log "WARNING: llama.cpp could not parse the model output on attempt ${attempt}/${CHUNK_RETRY_ATTEMPTS} (HTTP ${HTTP_STATUS})."
        else
            rejection_reason="request_failed_curl_${CURL_EXIT}_http_${HTTP_STATUS:-none}"
            log "WARNING: llama.cpp request failed on chunk attempt ${attempt}/${CHUNK_RETRY_ATTEMPTS} (curl=${CURL_EXIT}, HTTP=${HTTP_STATUS:-none})."
            # Only a failed request may be an outage worth waiting out; a
            # rejected or unparseable sample is resampled at once.
            [ "$attempt" -lt "$CHUNK_RETRY_ATTEMPTS" ] && sleep "$CHUNK_RETRY_DELAY_SECONDS"
        fi
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
    case "$rejection_reason" in
        request_failed_*)
            # llama.cpp is unreachable or erroring: stop and resume later
            # rather than filling the file with untranslated lines.
            log "ERROR: Cue translation request failed after ${CHUNK_RETRY_ATTEMPTS} attempts (reason=${rejection_reason}); diagnostic=${diagnostic_key}.json"
            return 1
            ;;
    esac
    # One stubborn cue must not fail the whole file: keep the source line
    # for this cue. process_subtitle fails the file if too many cues fall back.
    printf '%s\n%s\n%s\n\n' "$cue_id" "$cue_timestamp" "$source_dialogue" >> "$out_file"
    CUE_FALLBACKS=$((CUE_FALLBACKS + 1))
    log "WARNING: Cue ${cue_id} kept in the source language after ${CHUNK_RETRY_ATTEMPTS} attempts (reason=${rejection_reason:-unknown}); diagnostic=${diagnostic_key}.json"
    return 0
}

# --- USER MESSAGES ---
# Small models sometimes echo a bare English line back unchanged; for Chinese,
# restate the target language next to the dialogue itself.
get_user_message() {
    local lang="$1" dialogue="$2"
    case "$lang" in
        "zh") printf '將以下英文字幕翻譯成繁體中文，只輸出譯文：\n%s' "$dialogue" ;;
        *)    printf '%s' "$dialogue" ;;
    esac
}

get_batch_user_message() {
    local lang="$1" count="$2" sources="$3"
    case "$lang" in
        "zh") printf '將以下英文字幕翻譯成繁體中文，只輸出譯文（JSON 字串陣列，共 %s 個元素）：\n%s' "$count" "$sources" ;;
        *)    printf '%s' "$sources" ;;
    esac
}

# --- TRANSLATE BATCH ---
# Sends consecutive cues (chunk files $5...) in one request and requires a
# JSON array of exactly as many translations back. Each accepted item is
# written to $1/<item index>.cue; process_subtitle sends every other cue
# through translate_chunk, so a failed batch behaves like per-cue requests.
# An unusable reply is saved as diagnostic $4.json.
# Returns 0 if every item was accepted, 1 if some were, 2 if none were usable.
translate_batch() {
    local batch_dir="$1" context="$2" target_lang="$3" diagnostic_key="$4"
    shift 4
    local count=$# sources sys_prompt user_message per_cue_tokens
    sources=$(python3 "${SCRIPT_DIR}/translation_cue.py" batch-sources "$@") || return 2
    sys_prompt=$(get_batch_system_prompt "$target_lang")
    if [ -n "$context" ]; then
        sys_prompt+=$'\nThe previous source cues are context only. Do not translate or repeat them:\n'"${context}"
    fi
    user_message=$(get_batch_user_message "$target_lang" "$count" "$sources")
    # A cue needs a few dozen tokens; capping each cue's share keeps a
    # runaway batch from generating for minutes.
    per_cue_tokens=$(( LLAMA_CPP_MAX_TOKENS < 256 ? LLAMA_CPP_MAX_TOKENS : 256 ))
    jq -n --arg model "$LLAMA_CPP_MODEL" --arg sys "$sys_prompt" --arg user_msg "$user_message" \
        --argjson max_tokens "$((per_cue_tokens * count))" --argjson count "$count" \
        '{model: $model, stream: false, temperature: 0.1, max_tokens: $max_tokens, chat_template_kwargs: {enable_thinking: false},
          response_format: {type: "json_schema", json_schema: {name: "subtitle_translations",
              schema: {type: "array", items: {type: "string"}, minItems: $count, maxItems: $count}}},
          messages: [{role: "system", content: $sys}, {role: "user", content: $user_msg}]}' \
        > "$TEMP_JSON_PAYLOAD"

    local HTTP_STATUS CURL_EXIT finish_reason="none" summary="" validate_status=3 cue_range
    rm -f "$TEMP_RESPONSE_FILE"
    HTTP_STATUS=$(curl -s -w "%{http_code}" -o "$TEMP_RESPONSE_FILE" \
        --connect-timeout 30 --max-time 600 -X POST -H "Content-Type: application/json" \
        -d @"$TEMP_JSON_PAYLOAD" "${LLAMA_CPP_API_URL}")
    CURL_EXIT=$?
    if [ "$CURL_EXIT" -eq 0 ] && [ "$HTTP_STATUS" = "200" ]; then
        jq -r '.choices[0].message.content // empty' < "$TEMP_RESPONSE_FILE" > "${batch_dir}/content.txt"
        finish_reason=$(jq -r '.choices[0].finish_reason // "unknown"' < "$TEMP_RESPONSE_FILE")
        summary=$(python3 "${SCRIPT_DIR}/translation_cue.py" batch-validate "${batch_dir}/content.txt" "$target_lang" "$batch_dir" "$@")
        validate_status=$?
        [ "$validate_status" -eq 0 ] && return 0
    elif [ "$CURL_EXIT" -eq 0 ] && is_model_output_error "$HTTP_STATUS" "$TEMP_RESPONSE_FILE"; then
        summary="unparseable_model_output"
    else
        # llama.cpp says why it refused the request, e.g. an unsupported json_schema.
        local error_message=""
        [ -s "$TEMP_RESPONSE_FILE" ] && error_message=$(jq -r '.error.message? // empty | split("\n") | join(" ") | .[:200]' < "$TEMP_RESPONSE_FILE" 2>/dev/null)
        summary="request_failed_curl_${CURL_EXIT}_http_${HTTP_STATUS:-none}${error_message:+: ${error_message}}"
    fi
    cue_range="$(sed -n '1p' "$1")-$(sed -n '1p' "${!#}")"
    if [ "$validate_status" -eq 1 ]; then
        log "WARNING: Batch of cues ${cue_range}: ${summary:-validation failed}; retrying those one cue per request."
        return 1
    fi
    local diagnostic_dir="${DATA_DIR:-/app/data}/translation-failures"
    mkdir -p "$diagnostic_dir"
    jq -n --arg source_file "${SOURCE_FILE:-unknown}" --arg target_language "$target_lang" --arg cue_range "$cue_range" \
        --arg rejection_reason "${summary:-invalid_batch_response}" \
        --argjson response "$(jq -c . "$TEMP_RESPONSE_FILE" 2>/dev/null || printf '{}')" \
        '{source_file: $source_file, target_language: $target_language, cue_range: $cue_range,
          rejection_reason: $rejection_reason, finish_reason: ($response.choices[0].finish_reason // null),
          content: ($response.choices[0].message.content // null | if type == "string" then .[:2000] else . end),
          error: ($response.error // null)}' \
        > "${diagnostic_dir}/${diagnostic_key}.json" 2>/dev/null || true
    chmod 600 "${diagnostic_dir}/${diagnostic_key}.json" 2>/dev/null || true
    log "WARNING: Batch of cues ${cue_range} unusable (reason=${summary:-invalid_batch_response}, finish=${finish_reason}); translating them one cue per request; diagnostic=${diagnostic_key}.json"
    return 2
}

# --- PROCESS SUBTITLE ---
process_subtitle() {
    local SOURCE_FILE="$1" TARGET_LANG="$2"
    local DIR_PATH BASENAME_NO_EXT
    split_source_path "$SOURCE_FILE"

    local FINAL_OUTPUT_FILE="${DIR_PATH}/${BASENAME_NO_EXT}.${TARGET_LANG}.srt"
    local FAILED_MARKER_FILE="${DIR_PATH}/${BASENAME_NO_EXT}.${TARGET_LANG}.failed"

    if [ -f "$FAILED_MARKER_FILE" ]; then
        local marker_age=$(( $(date +%s) - $(stat -c %Y "$FAILED_MARKER_FILE" 2>/dev/null || echo 0) ))
        if [ "$marker_age" -lt $((FAILED_RETRY_HOURS * 3600)) ]; then
            SKIPPED_FAILED=$((SKIPPED_FAILED+1)); return
        fi
        log "RETRY: Expired failure marker for ${SOURCE_FILE##*/} [${TARGET_LANG}]."
        rm -f "$FAILED_MARKER_FILE"
    fi

    shopt -s nullglob nocaseglob
    local EX=( "${DIR_PATH}/${BASENAME_NO_EXT}"*.${TARGET_LANG}.srt "${DIR_PATH}/${BASENAME_NO_EXT}.${TARGET_LANG}"*.srt )
    shopt -u nullglob nocaseglob
    if [ ${#EX[@]} -gt 0 ]; then SKIPPED_EXISTING=$((SKIPPED_EXISTING+1)); return; fi

    local SYSTEM_PROMPT
    SYSTEM_PROMPT=$(get_system_prompt "$TARGET_LANG")

    # Health check
    FILES_SINCE_HEALTH_CHECK=$((FILES_SINCE_HEALTH_CHECK + 1))
    if [ "$FILES_SINCE_HEALTH_CHECK" -ge "$HEALTH_CHECK_INTERVAL" ]; then
        health_check_llama_cpp || { log "FATAL: llama.cpp unrecoverable."; exit 1; }
        FILES_SINCE_HEALTH_CHECK=0
    fi

    log "--------------------------------------------------------"
    log "Translating to [${TARGET_LANG}]: ${SOURCE_FILE##*/}"

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
    CUE_FALLBACKS=0
    local CHUNK_DIR="/tmp/srt_chunks_$$"
    # A killed run can leave chunks behind under a reused PID.
    rm -rf "$CHUNK_DIR"
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

    local -a chunk_list cue_kinds
    mapfile -t chunk_list <<< "$chunk_files"
    # Watermark cues are copied verbatim: never batched or sent as context.
    mapfile -t cue_kinds < <(python3 "${SCRIPT_DIR}/translation_cue.py" classify "${chunk_list[@]}")
    if [ "${#cue_kinds[@]}" -ne "${#chunk_list[@]}" ]; then
        log "WARNING: Could not classify cues; translating one cue per request."
        cue_kinds=()
    fi

    # Checkpoint: signature, cues done, .temp size and fallbacks at that point.
    local previous_chunk="" resume_chunk=0
    if [ -s "$TEMP_FINAL_FILE" ] && [ -f "$CHECKPOINT_FILE" ]; then
        local checkpoint_signature="" checkpoint_bytes="" checkpoint_fallbacks="" temp_bytes
        read -r checkpoint_signature resume_chunk checkpoint_bytes checkpoint_fallbacks < "$CHECKPOINT_FILE" || true
        temp_bytes=$(stat -c %s "$TEMP_FINAL_FILE")
        if [ "$checkpoint_signature" != "$SOURCE_SIGNATURE" ] || ! [[ "$resume_chunk" =~ ^[0-9]+$ ]] \
            || [ "$resume_chunk" -gt "${#chunk_list[@]}" ] || ! [[ "$checkpoint_bytes" =~ ^[0-9]*$ ]] \
            || [ "$temp_bytes" -lt "${checkpoint_bytes:-0}" ]; then
            resume_chunk=0; rm -f "$TEMP_FINAL_FILE" "$CHECKPOINT_FILE"
        else
            # Bytes past the checkpoint are a cue appended just before a crash;
            # it is translated again, so drop them rather than duplicate it.
            if [ "$temp_bytes" -gt "${checkpoint_bytes:-$temp_bytes}" ]; then
                python3 -c 'import os, sys; os.truncate(sys.argv[1], int(sys.argv[2]))' "$TEMP_FINAL_FILE" "$checkpoint_bytes"
            fi
            [[ "$checkpoint_fallbacks" =~ ^[0-9]+$ ]] && CUE_FALLBACKS="$checkpoint_fallbacks"
            log "RESUME: Continuing at chunk ${resume_chunk}/${total_chunks}."
        fi
    fi
    [ "$resume_chunk" -eq 0 ] && true > "$TEMP_FINAL_FILE"
    draw_progress "$resume_chunk" "$total_chunks"
    local chunk_index="$resume_chunk" batch_failures=0 source_hash
    source_hash=$(printf '%s' "${SOURCE_FILE}:${TARGET_LANG}" | sha256sum | cut -c1-16)
    if [ "$chunk_index" -gt 0 ] && [ "${cue_kinds[chunk_index-1]:-}" != "passthrough" ]; then
        previous_chunk=$(cat "${chunk_list[chunk_index-1]}")
    fi
    while [ "$chunk_index" -lt "${#chunk_list[@]}" ]; do
        local batch_start="$chunk_index" batch_size=1 batch_dir=""
        # Three unusable batches in a row: this server cannot batch, so stop
        # paying a failed request per batch for the rest of the file.
        if [ "$batch_failures" -lt 3 ] && [ "${cue_kinds[batch_start]:-}" = "translate" ]; then
            while [ "$batch_size" -lt "$TRANSLATION_BATCH_CUES" ] \
                && [ "${cue_kinds[batch_start+batch_size]:-}" = "translate" ]; do
                batch_size=$((batch_size+1))
            done
        fi
        if [ "$batch_size" -gt 1 ]; then
            local context="" k
            for k in $((batch_start-2)) $((batch_start-1)); do
                if [ "$k" -ge 0 ] && [ "${cue_kinds[k]:-}" = "translate" ]; then
                    context+="$(sed '1,2d' "${chunk_list[k]}")"$'\n'
                fi
            done
            batch_dir="${CHUNK_DIR}/batch"
            rm -rf "$batch_dir"; mkdir -p "$batch_dir"
            local batch_key
            printf -v batch_key '%s-%s-batch-%05d' "$source_hash" "$TARGET_LANG" "$batch_start"
            translate_batch "$batch_dir" "${context%$'\n'}" "$TARGET_LANG" "$batch_key" "${chunk_list[@]:batch_start:batch_size}"
            if [ $? -eq 2 ]; then
                batch_failures=$((batch_failures+1))
                [ "$batch_failures" -eq 3 ] && log "WARNING: 3 batches in a row were unusable; translating the rest of this file one cue per request."
            else
                batch_failures=0
            fi
        fi
        while [ "$chunk_index" -lt $((batch_start + batch_size)) ]; do
            local cc batch_cue="${batch_dir}/$((chunk_index - batch_start)).cue"
            cc=$(cat "${chunk_list[chunk_index]}")
            # An empty .cue is a write the helper did not finish, not a translation.
            if [ -n "$batch_dir" ] && [ -s "$batch_cue" ]; then
                cat "$batch_cue" >> "$TEMP_FINAL_FILE"
            else
                local diagnostic_key
                diagnostic_key=$(printf '%s-%s-%05d' "$source_hash" "$TARGET_LANG" "$chunk_index")
                if ! translate_chunk "$previous_chunk" "$cc" "$SYSTEM_PROMPT" "$diagnostic_key" "$TARGET_LANG" "$TEMP_FINAL_FILE" "${cue_kinds[chunk_index]:-}"; then
                    chunk_success=false; break 2
                fi
                # Past the fallback limit the file is discarded anyway (below);
                # stop paying a full generation per remaining cue for it.
                [ $((CUE_FALLBACKS * 100)) -gt $((total_chunks * TRANSLATION_MAX_FALLBACK_PCT)) ] && break 2
            fi
            processed_chunks=$((chunk_index+1))
            printf '%s %s %s %s\n' "$SOURCE_SIGNATURE" "$processed_chunks" "$(stat -c %s "$TEMP_FINAL_FILE")" "$CUE_FALLBACKS" > "$CHECKPOINT_FILE"
            draw_progress "$processed_chunks" "$total_chunks"
            [ "${cue_kinds[chunk_index]:-}" = "passthrough" ] || previous_chunk="$cc"
            chunk_index=$((chunk_index+1))
        done
    done
    rm -rf "$CHUNK_DIR"
    echo ""

    if [[ "$chunk_success" == true ]] && [ $((CUE_FALLBACKS * 100)) -gt $((total_chunks * TRANSLATION_MAX_FALLBACK_PCT)) ]; then
        # Mostly untranslated: discard it so the retry starts fresh instead
        # of resuming into a finished-but-English file.
        log "ERROR: ${CUE_FALLBACKS}/${total_chunks} cues stayed untranslated (limit ${TRANSLATION_MAX_FALLBACK_PCT}%); discarding output."
        rm -f "$TEMP_FINAL_FILE" "$CHECKPOINT_FILE"
        FAILED_THIS_RUN=$((FAILED_THIS_RUN+1)); touch "$FAILED_MARKER_FILE"
        return
    fi

    if [[ "$chunk_success" == true && -s "$TEMP_FINAL_FILE" ]]; then
        if python3 - "$SOURCE_FILE" "$TEMP_FINAL_FILE" <<'PYEOF'
import re, sys
pattern = re.compile(r"^\s*(\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2}:\d{2}:\d{2},\d{3})\s*$", re.M)
source = open(sys.argv[1], encoding="utf-8", errors="replace").read()
output = open(sys.argv[2], encoding="utf-8", errors="replace").read()
raise SystemExit(0 if pattern.findall(source) and pattern.findall(output) == pattern.findall(source) else 1)
PYEOF
        then
            # Checked before anything rewrites the .temp: if the permissions
            # cannot be set, the .temp still matches its checkpoint, so a later
            # run resumes at its last cue and publishes it without llama.cpp.
            if ! finalize_subtitle_permissions "$TEMP_FINAL_FILE"; then
                log "ERROR: Finished translation kept unpublished as ${TEMP_FINAL_FILE##*/}."
                FAILED_THIS_RUN=$((FAILED_THIS_RUN+1)); return
            fi
            verify_encoding "$TEMP_FINAL_FILE"
            normalize_timestamps "$TEMP_FINAL_FILE" >/dev/null

            # Either repair may have replaced the file, so its final permissions
            # are applied again before it is published.
            if ! { finalize_subtitle_permissions "$TEMP_FINAL_FILE" && mv "$TEMP_FINAL_FILE" "$FINAL_OUTPUT_FILE"; }; then
                # A repaired .temp no longer matches its checkpoint.
                log "ERROR: Could not publish ${FINAL_OUTPUT_FILE##*/}; discarding output."
                rm -f "$TEMP_FINAL_FILE" "$CHECKPOINT_FILE"
                FAILED_THIS_RUN=$((FAILED_THIS_RUN+1)); touch "$FAILED_MARKER_FILE"
                return
            fi
            rm -f "$CHECKPOINT_FILE" "$FAILED_MARKER_FILE"
            TRANSLATED_FILES+=("$FINAL_OUTPUT_FILE")
            log "SUCCESS: ${TARGET_LANG} translation complete (${CUE_FALLBACKS} cue(s) kept in the source language)."
            TRANSLATIONS_PROCESSED=$((TRANSLATIONS_PROCESSED+1))
            SESSION_PROCESSING_SECONDS=$(( SESSION_PROCESSING_SECONDS + $(date +%s) - START_JOB_TIME ))
        else
            # Resuming a finished .temp would only fail this check again.
            log "ERROR: Complete translation does not preserve every source timestamp; discarding output."
            rm -f "$TEMP_FINAL_FILE" "$CHECKPOINT_FILE"
            FAILED_THIS_RUN=$((FAILED_THIS_RUN+1)); touch "$FAILED_MARKER_FILE"
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
PLEXMIND_SENTINEL="${DATA_DIR}/plexmind.running"
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
if [ "${LLAMA_ON_DEMAND:-0}" = "1" ]; then
    # Hold a shared lease for the whole run so the idle reaper never stops
    # llama.cpp mid-translation (it needs the exclusive lock to stop it).
    exec {LLAMA_LEASE_FD}>>"${DATA_DIR:-/app/data}/llama_lifecycle.lock"
    flock -s "$LLAMA_LEASE_FD"
    start_docker_container "llama.cpp" "${LLAMA_CPP_CONTAINER_NAME:-llama-cpp}" || exit 1
elif [ "${MANAGE_LLAMA_CPP_CONTAINER:-0}" = "1" ]; then
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

# Fix timestamp ordering in the SRT files this run wrote;
# `maintenance.sh ordering` sweeps the whole library.
if [ "${#TRANSLATED_FILES[@]}" -gt 0 ] && [ -f "${SCRIPT_DIR}/fix_srt_ordering.py" ]; then
    log "Running SRT timestamp ordering fix on ${#TRANSLATED_FILES[@]} new translation(s)..."
    # This run already holds the media mutation lock the fixer would take.
    PLEXMIND_MEDIA_LOCK_HELD=1 python3 "${SCRIPT_DIR}/fix_srt_ordering.py" "${TRANSLATED_FILES[@]}" 2>&1 | while IFS= read -r line; do log "$line"; done
    log "SRT ordering fix complete."
fi

if [ "$SKIPPED_FAILED" -gt 0 ] || [ "$FAILED_THIS_RUN" -gt 0 ]; then
    log "COMPLETED_WITH_ERRORS: ${FAILED_THIS_RUN} new failure(s), ${SKIPPED_FAILED} deferred failure(s)."
    exit 2
fi
exit 0
