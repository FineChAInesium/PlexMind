#!/bin/bash
# ==============================================================================
# transcribe.sh — Library Transcription Backfill
# Version: 0.8.14 — PlexMind release line
#
# Scans Movies and TV directories, transcribes via Whisper ASR API.
# Features: language profiling, bilingual VIP handling, hallucination
# cleaning, validation, watermarking, confidence scoring, quarantine,
# retry/resume, lifetime stats, time-window enforcement.
#
# Requires: lib.sh, ffmpeg, ffprobe, curl, python3
# ==============================================================================

set -u

# --- CONFIGURATION ---
WHISPER_API_URL="${WHISPER_API_URL:-http://whisper:9000/asr}"
OUTPUT_FORMAT="${OUTPUT_FORMAT:-srt}"
TRANSCRIPTION_LANGUAGE="${TRANSCRIPTION_LANGUAGE:-en}"
LOG_FILE="${LOG_FILE:-/app/data/transcription.log}"
LIFETIME_STATS_FILE="${LIFETIME_STATS_FILE:-/app/data/lifetime_stats.env}"
MAX_FILE_SIZE_MB="${MAX_FILE_SIZE_MB:-54000}"
INITIAL_PROMPT="${INITIAL_PROMPT:-Hello! Welcome to the show. Dr. Smith, Mr. Jones... fuck, shit, damn, okay, alright.}"
CONFIDENCE_THRESHOLD="${CONFIDENCE_THRESHOLD:-30}"
HEALTH_CHECK_INTERVAL="${HEALTH_CHECK_INTERVAL:-10}"
START_HOUR="${START_HOUR:-${TRANSCRIBE_START_HOUR:-5}}"
END_HOUR="${END_HOUR:-${TRANSCRIBE_END_HOUR:-12}}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-7}"
MAX_RUNTIME_MINUTES="${MAX_RUNTIME_MINUTES:-0}"

# Bilingual / reality VIP lists (comma-separated env vars → arrays)
IFS=',' read -ra KNOWN_BILINGUAL_TITLES <<< "${KNOWN_BILINGUAL_TITLES:-90 Day Fiancé,Shogun,Shōgun,Squid Game}"
IFS=',' read -ra KNOWN_ENGLISH_REALITY_TITLES <<< "${KNOWN_ENGLISH_REALITY_TITLES:-Summer House,Vanderpump Rules}"

normalize_lang_code() {
    local LANG
    LANG=$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]' | cut -d'-' -f1)
    case "$LANG" in
        eng|english) echo "en" ;;
        jpn|jp|japanese) echo "ja" ;;
        kor|korean) echo "ko" ;;
        zho|chi|cmn|yue|chinese) echo "zh" ;;
        por|portuguese) echo "pt" ;;
        spa|spanish) echo "es" ;;
        fre|fra|french) echo "fr" ;;
        ger|deu|german) echo "de" ;;
        ita|italian) echo "it" ;;
        rus|russian) echo "ru" ;;
        und|unknown|none) echo "" ;;
        *) echo "$LANG" ;;
    esac
}

# --- LOAD SHARED LIBRARY ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh" || { echo "FATAL: Cannot load lib.sh"; exit 1; }

mkdir -p "$(dirname "$LOG_FILE")"
prepare_log_file
acquire_lock "/tmp/transcription_backfill.lock"

TEMP_AUDIO_FILE="/tmp/transcribe_temp_audio.wav"

export TOTAL_SCANNED=0 ENGLISH_PROCESSED=0 VIP_PROCESSED=0 FOREIGN_PROCESSED=0
export SKIPPED_EXISTING=0 SKIPPED_FAILED=0 SKIPPED_SIZE=0 HALLUCINATIONS_CLEANED=0
export SESSION_PROCESSING_SECONDS=0
FILES_SINCE_HEALTH_CHECK=0

# --- LIFETIME STATS ---
if [ -f "$LIFETIME_STATS_FILE" ]; then
    source "$LIFETIME_STATS_FILE"
fi
LIFETIME_SCANNED="${LIFETIME_SCANNED:-0}"
LIFETIME_ENGLISH_PROCESSED="${LIFETIME_ENGLISH_PROCESSED:-0}"
LIFETIME_BILINGUAL_PROCESSED="${LIFETIME_BILINGUAL_PROCESSED:-0}"
LIFETIME_FOREIGN_PROCESSED="${LIFETIME_FOREIGN_PROCESSED:-0}"
LIFETIME_SKIPPED_EXISTING="${LIFETIME_SKIPPED_EXISTING:-0}"
LIFETIME_SKIPPED_FAILED="${LIFETIME_SKIPPED_FAILED:-0}"
LIFETIME_SKIPPED_SIZE="${LIFETIME_SKIPPED_SIZE:-0}"
LIFETIME_HALLUCINATIONS_CLEANED="${LIFETIME_HALLUCINATIONS_CLEANED:-0}"
LIFETIME_PROCESSING_SECONDS="${LIFETIME_PROCESSING_SECONDS:-0}"

# --- CLEANUP TRAP ---
cleanup() {
    LIFETIME_SCANNED=$((LIFETIME_SCANNED + TOTAL_SCANNED))
    LIFETIME_ENGLISH_PROCESSED=$((LIFETIME_ENGLISH_PROCESSED + ENGLISH_PROCESSED))
    LIFETIME_BILINGUAL_PROCESSED=$((LIFETIME_BILINGUAL_PROCESSED + VIP_PROCESSED))
    LIFETIME_FOREIGN_PROCESSED=$((LIFETIME_FOREIGN_PROCESSED + FOREIGN_PROCESSED))
    LIFETIME_SKIPPED_EXISTING=$((LIFETIME_SKIPPED_EXISTING + SKIPPED_EXISTING))
    LIFETIME_SKIPPED_FAILED=$((LIFETIME_SKIPPED_FAILED + SKIPPED_FAILED))
    LIFETIME_SKIPPED_SIZE=$((LIFETIME_SKIPPED_SIZE + SKIPPED_SIZE))
    LIFETIME_HALLUCINATIONS_CLEANED=$((LIFETIME_HALLUCINATIONS_CLEANED + HALLUCINATIONS_CLEANED))
    LIFETIME_PROCESSING_SECONDS=$((LIFETIME_PROCESSING_SECONDS + SESSION_PROCESSING_SECONDS))

    cat <<EOF > "$LIFETIME_STATS_FILE"
LIFETIME_SCANNED=$LIFETIME_SCANNED
LIFETIME_ENGLISH_PROCESSED=$LIFETIME_ENGLISH_PROCESSED
LIFETIME_BILINGUAL_PROCESSED=$LIFETIME_BILINGUAL_PROCESSED
LIFETIME_FOREIGN_PROCESSED=$LIFETIME_FOREIGN_PROCESSED
LIFETIME_SKIPPED_EXISTING=$LIFETIME_SKIPPED_EXISTING
LIFETIME_SKIPPED_FAILED=$LIFETIME_SKIPPED_FAILED
LIFETIME_SKIPPED_SIZE=$LIFETIME_SKIPPED_SIZE
LIFETIME_HALLUCINATIONS_CLEANED=$LIFETIME_HALLUCINATIONS_CLEANED
LIFETIME_PROCESSING_SECONDS=$LIFETIME_PROCESSING_SECONDS
EOF

    log "========================================================="
    log "Session Finished! Scanned:${TOTAL_SCANNED} EN:${ENGLISH_PROCESSED} FGN:${FOREIGN_PROCESSED} VIP:${VIP_PROCESSED}"
    log "Hallucinations:${HALLUCINATIONS_CLEANED} Skip-Exist:${SKIPPED_EXISTING} Skip-Fail:${SKIPPED_FAILED} Skip-Size:${SKIPPED_SIZE}"
    log "========================================================="

    log "Running end-of-session tasks..."
    cleanup_pgs "${MOVIE_DIR}" "${TV_DIR}" >/dev/null
    generate_report

    rm -f "$TEMP_AUDIO_FILE" /tmp/transcription_backfill.pid 2>/dev/null
    stop_docker_container "Whisper" "${WHISPER_CONTAINER_NAME:-}" whisper-asr-webservice plexmind-whisper whisper
}
trap cleanup EXIT

# --- CALCULATE PENDING JOBS ---
calculate_pending_jobs() {
    log "Pre-scanning for pending jobs..."
    local TEMP_TOTAL=0 TEMP_PENDING=0

    while IFS= read -r -d '' VIDEO_FILE; do
        TEMP_TOTAL=$((TEMP_TOTAL+1))
        local VBASE DIR_PATH BASENAME_NO_EXT
        VBASE=$(basename "$VIDEO_FILE")
        DIR_PATH=$(dirname "$VIDEO_FILE")
        BASENAME_NO_EXT="${VBASE%.*}"

        [ -f "${DIR_PATH}/${BASENAME_NO_EXT}.ai.failed" ] && continue

        shopt -s nullglob nocaseglob
        local EXISTING_SUBS=( "${DIR_PATH}/${BASENAME_NO_EXT}".*.srt )
        shopt -u nullglob nocaseglob
        [ ${#EXISTING_SUBS[@]} -eq 0 ] && TEMP_PENDING=$((TEMP_PENDING+1))
    done < <(find "${ALL_MEDIA_DIRS[@]}" -type f \( -iname "*.mkv" -o -iname "*.mp4" -o -iname "*.avi" -o -iname "*.mov" -o -iname "*.wmv" -o -iname "*.m4v" \) -print0 2>/dev/null)

    log "LIBRARY: ${TEMP_TOTAL} videos, ${TEMP_PENDING} pending"

    local TOTAL_LP=$(( LIFETIME_ENGLISH_PROCESSED + LIFETIME_BILINGUAL_PROCESSED + LIFETIME_FOREIGN_PROCESSED ))
    if [ "$TOTAL_LP" -gt 0 ] && [ "$LIFETIME_PROCESSING_SECONDS" -gt 0 ]; then
        local AVG=$(( LIFETIME_PROCESSING_SECONDS / TOTAL_LP ))
        local ETA=$(( TEMP_PENDING * AVG ))
        local D=$((ETA/86400)) H=$(((ETA%86400)/3600)) M=$(((ETA%3600)/60))
        local S=""; [ "$D" -gt 0 ] && S="${D}d "; S="${S}${H}h ${M}m"
        log "ETA: ${S} (Avg ${AVG}s/video)"
    fi

    [ "$TEMP_PENDING" -eq 0 ] && { log "Library fully transcribed!"; exit 0; }
}

# --- PROCESS VIDEO ---
process_video() {
    local VIDEO_FILE="$1"
    [ ! -f "$VIDEO_FILE" ] && return

    local BASENAME_WITH_EXT DIR_PATH BASENAME_NO_EXT LANG_TAG
    BASENAME_WITH_EXT=$(basename "$VIDEO_FILE")
    DIR_PATH=$(dirname "$VIDEO_FILE")
    BASENAME_NO_EXT="${BASENAME_WITH_EXT%.*}"
    LANG_TAG="${TRANSCRIPTION_LANGUAGE}"

    local FINAL_OUTPUT_FILE="${DIR_PATH}/${BASENAME_NO_EXT}.${LANG_TAG}.srt"
    local TEMP_OUTPUT_FILE="${DIR_PATH}/${BASENAME_NO_EXT}.ai.srt"
    local FAILED_MARKER_FILE="${DIR_PATH}/${BASENAME_NO_EXT}.ai.failed"

    # --- RETRY CHECK ---
    if [ -f "$FAILED_MARKER_FILE" ]; then
        if ! retry_failed "$FAILED_MARKER_FILE"; then
            local RC
            RC=$(sed -n '2p' "$FAILED_MARKER_FILE" 2>/dev/null | tr -d '[:space:]')
            [ "${RC:-0}" -ge "$MAX_RETRIES_SOFT" ] && quarantine_video "$VIDEO_FILE" "$(head -n1 "$FAILED_MARKER_FILE")"
            SKIPPED_FAILED=$((SKIPPED_FAILED+1))
            return
        fi
    fi

    # --- EXISTING SUBS CHECK ---
    shopt -s nullglob nocaseglob
    local EXISTING_SUBS=( "${DIR_PATH}/${BASENAME_NO_EXT}".*.srt )
    shopt -u nullglob nocaseglob
    if [ ${#EXISTING_SUBS[@]} -gt 0 ]; then SKIPPED_EXISTING=$((SKIPPED_EXISTING+1)); return; fi

    # --- HEALTH CHECK ---
    FILES_SINCE_HEALTH_CHECK=$((FILES_SINCE_HEALTH_CHECK + 1))
    if [ $FILES_SINCE_HEALTH_CHECK -ge $HEALTH_CHECK_INTERVAL ]; then
        health_check_api || { log "FATAL: API unrecoverable."; exit 1; }
        FILES_SINCE_HEALTH_CHECK=0
    fi

    log "--------------------------------------------------------"
    log "Processing: ${BASENAME_WITH_EXT}"
    local START_JOB_TIME PROCESSING_MODE DETECTED_FOREIGN_LANG
    START_JOB_TIME=$(date +%s)
    PROCESSING_MODE="ENGLISH"
    DETECTED_FOREIGN_LANG=""

    # --- VIP CHECK ---
    local IS_BIL=false IS_REAL=false
    for t in "${KNOWN_BILINGUAL_TITLES[@]}"; do
        [[ "$BASENAME_WITH_EXT" == *"$t"* ]] && { IS_BIL=true; break; }
    done
    if [ "$IS_BIL" = false ]; then
        for t in "${KNOWN_ENGLISH_REALITY_TITLES[@]}"; do
            [[ "$BASENAME_WITH_EXT" == *"$t"* ]] && { IS_REAL=true; break; }
        done
    fi

    if [ "$IS_BIL" = true ]; then
        log "Step 1/5: Bilingual VIP!"; PROCESSING_MODE="BILINGUAL_VIP"
    elif [ "$IS_REAL" = true ]; then
        log "Step 1/5: English Reality VIP!"
    else
        # --- LANGUAGE PROFILER ---
        local PRIMARY_AUDIO_LANG
        PRIMARY_AUDIO_LANG=$(ffprobe -v error -select_streams a:0 -show_entries stream_tags=language \
            -of default=noprint_wrappers=1:nokey=1 "${VIDEO_FILE}" | head -n1)
        PRIMARY_AUDIO_LANG=$(normalize_lang_code "$PRIMARY_AUDIO_LANG")

        if [ -n "$PRIMARY_AUDIO_LANG" ] && [ "$PRIMARY_AUDIO_LANG" != "en" ]; then
            log "Step 1/5: Audio metadata language [${PRIMARY_AUDIO_LANG}]"
            log "Verdict: FOREIGN [${PRIMARY_AUDIO_LANG}]"
            PROCESSING_MODE="FOREIGN"
            DETECTED_FOREIGN_LANG="$PRIMARY_AUDIO_LANG"
        else
            log "Step 1/5: AI Language Profiler..."
            local DURATION
            DURATION=$(ffprobe -v error -show_entries format=duration \
                -of default=noprint_wrappers=1:nokey=1 "${VIDEO_FILE}" | awk '{print int($1)}')

            if [ -n "$DURATION" ] && [ "$DURATION" -gt 300 ]; then
                local FOREIGN_VOTES=0
                for PERCENT in 15 30 50 70 85; do
                    local ST=$(( DURATION * PERCENT / 100 ))
                    local TS="/tmp/sample_${PERCENT}.wav"
                    local JSON_FILE="/tmp/profile_${PERCENT}_$$.json"
                    ffmpeg -nostdin -ss "$ST" -i "${VIDEO_FILE}" \
                        -map 0:a:0 -t 30 -vn -acodec pcm_s16le -ar 16000 -ac 1 \
                        -y "$TS" -loglevel quiet
                    local DL
                    curl -s -X POST -F "audio_file=@${TS}" \
                        "${WHISPER_API_URL}?task=transcribe&output=json" -o "$JSON_FILE"
                    DL=$(python3 - "$JSON_FILE" <<'PYEOF'
import json, sys, unicodedata
path = sys.argv[1]
try:
    data = json.load(open(path, 'r', encoding='utf-8', errors='replace'))
except Exception:
    data = {}
lang = str(data.get('language') or '').strip().lower()
if lang:
    print(lang)
    raise SystemExit
text = str(data.get('text') or '')
letters = [c for c in text if unicodedata.category(c).startswith('L')]
non_ascii = [c for c in letters if ord(c) > 127]
if not letters or len(non_ascii) / max(len(letters), 1) < 0.25:
    print('en')
    raise SystemExit
hangul = sum(1 for c in non_ascii if '가' <= c <= '힯' or 'ᄀ' <= c <= 'ᇿ')
kana = sum(1 for c in non_ascii if '぀' <= c <= 'ヿ')
arabic = sum(1 for c in non_ascii if '؀' <= c <= 'ۿ')
cjk = sum(1 for c in non_ascii if '一' <= c <= '鿿')
if hangul >= max(kana, arabic, cjk): print('ko')
elif kana >= max(hangul, arabic, cjk): print('ja')
elif arabic >= max(hangul, kana, cjk): print('ar')
elif cjk: print('zh')
else: print('en')
PYEOF
                    )
                    DL=$(normalize_lang_code "$DL")
                    rm -f "$TS" "$JSON_FILE"
                    if [ -n "$DL" ] && [ "$DL" != "en" ]; then
                        log "  -> ${PERCENT}%: [${DL}]"
                        FOREIGN_VOTES=$((FOREIGN_VOTES+1))
                        [ -z "$DETECTED_FOREIGN_LANG" ] && DETECTED_FOREIGN_LANG="$DL"
                    else
                        log "  -> ${PERCENT}%: [en]"
                    fi
                done
                if [ "$FOREIGN_VOTES" -gt 0 ]; then
                    log "Verdict: FOREIGN [${DETECTED_FOREIGN_LANG}]"; PROCESSING_MODE="FOREIGN"
                else
                    log "Verdict: ENGLISH"
                fi
            else
                log "Too short for profiling. Default: English."
            fi
        fi
    fi

    # --- EXTRACT AUDIO ---
    [ -f "$TEMP_OUTPUT_FILE" ] && rm -f "$TEMP_OUTPUT_FILE"
    rm -f "${TEMP_AUDIO_FILE}"
    log "Step 2/5: Extracting audio..."

    if [ "$PROCESSING_MODE" = "ENGLISH" ]; then
        ffmpeg -nostdin -i "${VIDEO_FILE}" -map 0:a:m:language:eng:0 \
            -vn -acodec pcm_s16le -ar 16000 -ac 1 -y "${TEMP_AUDIO_FILE}" -loglevel quiet
        [ ! -s "${TEMP_AUDIO_FILE}" ] && \
            ffmpeg -nostdin -i "${VIDEO_FILE}" -map 0:a:m:language:en:0 \
                -vn -acodec pcm_s16le -ar 16000 -ac 1 -y "${TEMP_AUDIO_FILE}" -loglevel quiet
        [ ! -s "${TEMP_AUDIO_FILE}" ] && \
            ffmpeg -nostdin -i "${VIDEO_FILE}" -map 0:a:0 \
                -vn -acodec pcm_s16le -ar 16000 -ac 1 -y "${TEMP_AUDIO_FILE}" -loglevel quiet
    elif [ "$PROCESSING_MODE" = "BILINGUAL_VIP" ]; then
        for _bil_lang in kor jpn zho cmn yue ara fra deu spa ita por rus; do
            ffmpeg -nostdin -i "${VIDEO_FILE}" \
                -map "0:a:m:language:${_bil_lang}:0" -vn -acodec pcm_s16le -ar 16000 -ac 1 \
                -y "${TEMP_AUDIO_FILE}" -loglevel quiet 2>/dev/null
            [ -s "${TEMP_AUDIO_FILE}" ] && { log "  Audio: found [${_bil_lang}] track."; break; }
        done
        [ ! -s "${TEMP_AUDIO_FILE}" ] && \
            ffmpeg -nostdin -i "${VIDEO_FILE}" -map 0:a:0 \
                -vn -acodec pcm_s16le -ar 16000 -ac 1 -y "${TEMP_AUDIO_FILE}" -loglevel quiet
    else
        ffmpeg -nostdin -i "${VIDEO_FILE}" -map 0:a:0 \
            -vn -acodec pcm_s16le -ar 16000 -ac 1 -y "${TEMP_AUDIO_FILE}" -loglevel quiet
    fi

    if [ ! -s "${TEMP_AUDIO_FILE}" ]; then
        log "ERROR: No audio extracted."
        write_failed_marker "$FAILED_MARKER_FILE" "$FAIL_EMPTY_AUDIO"
        quarantine_video "$VIDEO_FILE" "$FAIL_EMPTY_AUDIO"
        return
    fi

    local AUDIO_SIZE_MB=$(( $(stat -c%s "$TEMP_AUDIO_FILE") / 1024 / 1024 ))
    if [ "$AUDIO_SIZE_MB" -gt "$MAX_FILE_SIZE_MB" ]; then
        log "SKIP: Audio too large (${AUDIO_SIZE_MB}MB)."
        SKIPPED_SIZE=$((SKIPPED_SIZE+1))
        rm -f "$TEMP_AUDIO_FILE"
        return
    fi

    # --- API CALL ---
    local ENCODED_PROMPT=""
    if [ -n "$INITIAL_PROMPT" ]; then
        ENCODED_PROMPT=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" \
            "$INITIAL_PROMPT" 2>/dev/null \
            || printf '%s' "$INITIAL_PROMPT" | sed 's/ /%20/g; s/!/%21/g; s/,/%2C/g')
    fi

    local API_URL_PARAMS
    case "$PROCESSING_MODE" in
        ENGLISH)
            API_URL_PARAMS="${WHISPER_API_URL}?task=transcribe&output=${OUTPUT_FORMAT}&language=en"
            [ -n "$ENCODED_PROMPT" ] && API_URL_PARAMS="${API_URL_PARAMS}&initial_prompt=${ENCODED_PROMPT}"
            ;;
        FOREIGN)
            API_URL_PARAMS="${WHISPER_API_URL}?task=transcribe&output=${OUTPUT_FORMAT}&language=${DETECTED_FOREIGN_LANG}"
            ;;
        BILINGUAL_VIP)
            API_URL_PARAMS="${WHISPER_API_URL}?task=transcribe&output=${OUTPUT_FORMAT}"
            ;;
    esac

    log "Step 3/5: Uploading audio (${AUDIO_SIZE_MB}MB)..."
    local HTTP_STATUS
    HTTP_STATUS=$(curl -s -w "%{http_code}" -o "${TEMP_OUTPUT_FILE}" \
        --connect-timeout 60 --max-time 7200 \
        -X POST -F "audio_file=@${TEMP_AUDIO_FILE}" "${API_URL_PARAMS}")
    local CURL_EXIT=$?
    # Keep audio for BILINGUAL_VIP — may need it for translate pass
    [ "$PROCESSING_MODE" != "BILINGUAL_VIP" ] && rm -f "$TEMP_AUDIO_FILE"

    if [ $CURL_EXIT -ne 0 ]; then
        log "ERROR: Curl exit $CURL_EXIT."
        rm -f "$TEMP_OUTPUT_FILE" 2>/dev/null
        write_failed_marker "$FAILED_MARKER_FILE" "$FAIL_CURL_TIMEOUT"
        return
    fi
    if [ "$HTTP_STATUS" != "200" ]; then
        log "ERROR: API HTTP $HTTP_STATUS"
        rm -f "$TEMP_OUTPUT_FILE" 2>/dev/null
        write_failed_marker "$FAILED_MARKER_FILE" "$FAIL_API_ERROR"
        return
    fi
    if [ ! -s "$TEMP_OUTPUT_FILE" ] || ! grep -qF -- '-->' "$TEMP_OUTPUT_FILE"; then
        log "ERROR: Invalid SRT output."
        rm -f "$TEMP_OUTPUT_FILE"
        write_failed_marker "$FAILED_MARKER_FILE" "$FAIL_INVALID_OUTPUT"
        return
    fi

    # --- STEP 4: POST-PROCESSING ---
    log "Step 4/5: Post-processing..."
    local REM
    REM=$(clean_hallucinations "$TEMP_OUTPUT_FILE"); REM="${REM:-0}"
    [ "$REM" -gt 0 ] && { log "  Hallucinations: -${REM}"; HALLUCINATIONS_CLEANED=$((HALLUCINATIONS_CLEANED + REM)); }
    detect_music_cues "$TEMP_OUTPUT_FILE" "strip" >/dev/null
    normalize_timestamps "$TEMP_OUTPUT_FILE" >/dev/null

    # --- STEP 5: VALIDATE + FINALIZE ---
    log "Step 5/5: Validate & finalize..."
    if ! validate_srt "$TEMP_OUTPUT_FILE"; then
        log "ERROR: Validation failed."
        rm -f "$TEMP_OUTPUT_FILE"
        write_failed_marker "$FAILED_MARKER_FILE" "$FAIL_VALIDATION"
        quarantine_video "$VIDEO_FILE" "$FAIL_VALIDATION"
        return
    fi

    apply_watermark "$TEMP_OUTPUT_FILE"
    verify_encoding "$TEMP_OUTPUT_FILE"

    [ "$PROCESSING_MODE" = "FOREIGN" ] && [ -n "$DETECTED_FOREIGN_LANG" ] && \
        FINAL_OUTPUT_FILE="${DIR_PATH}/${BASENAME_NO_EXT}.${DETECTED_FOREIGN_LANG}.srt"

    mv "${TEMP_OUTPUT_FILE}" "${FINAL_OUTPUT_FILE}"
    log "SUCCESS: ${FINAL_OUTPUT_FILE}"

    # --- BILINGUAL VIP: detect non-Latin → rename + translate pass ---
    if [ "$PROCESSING_MODE" = "BILINGUAL_VIP" ]; then
        local FOREIGN_LANG_CODE
        FOREIGN_LANG_CODE=$(python3 - "${FINAL_OUTPUT_FILE}" <<'PYEOF'
import re, sys, unicodedata

def detect_lang(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except Exception:
        return ''
    text = re.sub(r'\d+\n[\d:,]+ --> [\d:,]+\n', '', text)
    chars = [c for c in text if unicodedata.category(c).startswith('L')]
    if not chars:
        return ''
    non_ascii = [c for c in chars if ord(c) > 127]
    if not non_ascii or len(non_ascii) / len(chars) < 0.3:
        return ''
    hangul = sum(1 for c in non_ascii if '\uac00' <= c <= '\ud7af' or '\u1100' <= c <= '\u11ff')
    kana   = sum(1 for c in non_ascii if '\u3040' <= c <= '\u30ff' or '\u30a0' <= c <= '\u30ff')
    arabic = sum(1 for c in non_ascii if '\u0600' <= c <= '\u06ff')
    cjk    = sum(1 for c in non_ascii if '\u4e00' <= c <= '\u9fff')
    if hangul >= max(kana, arabic, cjk):
        print('ko')
    elif kana >= max(hangul, arabic, cjk):
        print('ja')
    elif arabic >= max(hangul, kana, cjk):
        print('ar')
    elif cjk:
        print('zh')

detect_lang(sys.argv[1])
PYEOF
        )

        if [ -n "$FOREIGN_LANG_CODE" ]; then
            local FOREIGN_SRT="${DIR_PATH}/${BASENAME_NO_EXT}.${FOREIGN_LANG_CODE}.srt"
            mv "${FINAL_OUTPUT_FILE}" "${FOREIGN_SRT}"
            log "  Non-Latin detected [${FOREIGN_LANG_CODE}] — renamed to $(basename "${FOREIGN_SRT}")"

            # Translate pass → .en.srt
            if [ -s "${TEMP_AUDIO_FILE}" ]; then
                log "  Translate pass [${FOREIGN_LANG_CODE}] → en..."
                local TRANSLATE_TMP="${DIR_PATH}/${BASENAME_NO_EXT}.translate_tmp.srt"
                local TRANSLATE_HTTP
                TRANSLATE_HTTP=$(curl -s -w "%{http_code}" -o "${TRANSLATE_TMP}" \
                    --connect-timeout 60 --max-time 7200 \
                    -X POST -F "audio_file=@${TEMP_AUDIO_FILE}" \
                    "${WHISPER_API_URL}?task=translate&output=${OUTPUT_FORMAT}&language=${FOREIGN_LANG_CODE}")
                if [ "$TRANSLATE_HTTP" = "200" ] && [ -s "${TRANSLATE_TMP}" ] && grep -qF -- '-->' "${TRANSLATE_TMP}"; then
                    clean_hallucinations "${TRANSLATE_TMP}" >/dev/null
                    normalize_timestamps "${TRANSLATE_TMP}" >/dev/null
                    apply_watermark "${TRANSLATE_TMP}"
                    verify_encoding "${TRANSLATE_TMP}"
                    mv "${TRANSLATE_TMP}" "${FINAL_OUTPUT_FILE}"
                    log "  SUCCESS: English translation → $(basename "${FINAL_OUTPUT_FILE}")"
                else
                    log "  WARNING: Translate pass failed (HTTP ${TRANSLATE_HTTP}) — .en.srt not created."
                    rm -f "${TRANSLATE_TMP}" 2>/dev/null
                fi
            fi
        fi
        rm -f "${TEMP_AUDIO_FILE}" 2>/dev/null
    fi

    # Confidence check (non-blocking)
    local CONF CONF_LANG
    CONF_LANG="en"
    [ "$PROCESSING_MODE" = "FOREIGN" ] && [ -n "$DETECTED_FOREIGN_LANG" ] && CONF_LANG="$DETECTED_FOREIGN_LANG"
    [ "$PROCESSING_MODE" = "BILINGUAL_VIP" ] && CONF_LANG="auto"
    CONF=$(score_confidence "$VIDEO_FILE" "$FINAL_OUTPUT_FILE" "$CONF_LANG" | awk '/^[0-9]+$/ { v=$0 } END { print v }'); CONF="${CONF:-50}"
    [ "$CONF" -lt "$CONFIDENCE_THRESHOLD" ] && { log "WARNING: Low confidence ${CONF}/100"; quarantine_video "$VIDEO_FILE" "LOW_CONFIDENCE_${CONF}"; }

    case "$PROCESSING_MODE" in
        ENGLISH)       ENGLISH_PROCESSED=$((ENGLISH_PROCESSED+1)) ;;
        FOREIGN)       FOREIGN_PROCESSED=$((FOREIGN_PROCESSED+1)) ;;
        BILINGUAL_VIP) VIP_PROCESSED=$((VIP_PROCESSED+1)) ;;
    esac
    SESSION_PROCESSING_SECONDS=$(( SESSION_PROCESSING_SECONDS + $(date +%s) - START_JOB_TIME ))
    sleep 2
}

# ==============================================================================
# MAIN
# ==============================================================================
log "========================================================="
log "Transcription Backfill v0.8.14 (containerized)"
log "Window: $(time_window_label) ($(time_window_hours)h); max runtime: ${MAX_RUNTIME_MINUTES:-0}m; retention: ${LOG_RETENTION_DAYS}d; RUN_NOW=${RUN_NOW}"
log "========================================================="
check_dependencies curl ffmpeg ffprobe python3

# Wait for PlexMind to finish if it's holding the GPU
PLEXMIND_SENTINEL="/tmp/plexmind.running"
if [ -f "$PLEXMIND_SENTINEL" ]; then
    log "PlexMind is running — waiting before starting Whisper..."
    while [ -f "$PLEXMIND_SENTINEL" ]; do
        sleep 30
        check_run_limits
    done
    log "PlexMind finished — proceeding."
fi

start_docker_container "Whisper" "${WHISPER_CONTAINER_NAME:-}" whisper-asr-webservice plexmind-whisper whisper
wait_for_whisper_api

ALL_MEDIA_DIRS=("${MOVIE_DIR}" "${TV_DIR}")

log "Checking for partial files from interrupted runs..."
resume_partial "${MOVIE_DIR}" "${TV_DIR}" >/dev/null

check_run_limits
calculate_pending_jobs

while IFS= read -r -d '' VIDEO_FILE; do
    check_run_limits
    TOTAL_SCANNED=$((TOTAL_SCANNED+1))
    process_video "$VIDEO_FILE"
done < <(find "${ALL_MEDIA_DIRS[@]}" -type f \( -iname "*.mkv" -o -iname "*.mp4" -o -iname "*.avi" -o -iname "*.mov" -o -iname "*.wmv" -o -iname "*.m4v" \) -print0 2>/dev/null)

exit 0
