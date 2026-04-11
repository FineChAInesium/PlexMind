#!/bin/bash
# ==============================================================================
# common_lib.sh — Shared Infrastructure for Transcription/Translation Pipeline
# Version: 2.0 — Containerized (PlexMind Suite)
#
# Source this file from any pipeline script:
#   source /app/lib.sh || { echo "FATAL: Cannot load lib.sh"; exit 1; }
#
# REQUIRES the sourcing script to set these variables before sourcing:
#   LOG_FILE            — path to log file (or "" for stdout-only)
#   WHISPER_API_URL     — e.g. http://whisper:9000/asr
#
# OPTIONAL variables (defaults provided):
#   ENABLE_WATERMARK    — true/false (default: true)
#   WATERMARK_TEXT      — subtitle watermark string
#   WATERMARK_SEARCH    — grep search string for dupe detection
#   MIN_CUE_COUNT       MAX_AVG_CUE_DURATION   MAX_AVG_CUE_CHARS
#   HALLUCINATION_REPEAT_THRESHOLD
#   MAX_RETRIES_SOFT
#   START_HOUR / END_HOUR
#   QUARANTINE_DIR      — where to log quarantined files
#   REPORT_DIR          — where generate_report writes output
# ==============================================================================

# --- Defaults for optional config ---
ENABLE_WATERMARK="${ENABLE_WATERMARK:-true}"
WATERMARK_TEXT="${WATERMARK_TEXT:-{\\an8}<i>Brought to you by PlexMind</i>}"
WATERMARK_SEARCH="${WATERMARK_SEARCH:-PlexMind}"
MIN_CUE_COUNT="${MIN_CUE_COUNT:-5}"
MAX_AVG_CUE_DURATION="${MAX_AVG_CUE_DURATION:-15}"
MAX_AVG_CUE_CHARS="${MAX_AVG_CUE_CHARS:-150}"
HALLUCINATION_REPEAT_THRESHOLD="${HALLUCINATION_REPEAT_THRESHOLD:-3}"
MAX_RETRIES_SOFT="${MAX_RETRIES_SOFT:-3}"
START_HOUR="${START_HOUR:-0}"
END_HOUR="${END_HOUR:-0}"

QUARANTINE_DIR="${QUARANTINE_DIR:-/app/data/quarantine}"
REPORT_DIR="${REPORT_DIR:-/app/data/reports}"

# Media directories (container paths)
MOVIE_DIR="${MOVIE_DIR:-/media/movies}"
TV_DIR="${TV_DIR:-/media/tv}"

# Failure reason codes
FAIL_CURL_TIMEOUT="CURL_TIMEOUT"
FAIL_API_ERROR="API_ERROR"
FAIL_EMPTY_AUDIO="EMPTY_AUDIO"
FAIL_INVALID_OUTPUT="INVALID_OUTPUT"
FAIL_VALIDATION="VALIDATION_FAILED"

# ==============================================================================
# CORE UTILITIES
# ==============================================================================

log() {
    local MSG="$(date '+%Y-%m-%d %H:%M:%S') - $1"
    echo "$MSG"
    if [ -n "${LOG_FILE:-}" ] && [ -n "$LOG_FILE" ]; then
        echo "$MSG" >> "$LOG_FILE"
    fi
}

# Acquire a lock file. Call early in the sourcing script.
# Usage: acquire_lock "/tmp/my_script.lock"
acquire_lock() {
    local LOCK="$1"
    exec 200>"$LOCK"
    if ! flock -n 200; then
        log "Another instance is already running (lock: $LOCK). Exiting."
        exit 1
    fi
}

check_dependencies() {
    local required=("$@")
    local missing=()
    for cmd in "${required[@]}"; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if [ ${#missing[@]} -gt 0 ]; then
        log "ERROR: Missing dependencies: ${missing[*]}"
        exit 1
    fi
    log "All dependencies available: ${required[*]}"
}

wait_for_whisper_api() {
    local MAX="${1:-30}"
    log "Waiting for Whisper API at ${WHISPER_API_URL}..."
    local RETRY=0
    while [ $RETRY -lt "$MAX" ]; do
        local STATUS
        STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 10 "${WHISPER_API_URL}" 2>/dev/null)
        if [ "$STATUS" -eq 200 ] || [ "$STATUS" -eq 405 ] || [ "$STATUS" -eq 422 ]; then
            log "Whisper API online (HTTP ${STATUS})."
            return 0
        fi
        log "API not ready (HTTP ${STATUS}), retrying... (${RETRY}/${MAX})"
        sleep 10
        RETRY=$((RETRY+1))
    done
    log "ERROR: Whisper API unavailable after ${MAX} retries."
    exit 1
}

check_time() {
    local CURRENT_HOUR
    CURRENT_HOUR=$(date +%H)
    if [ "$START_HOUR" -ne "$END_HOUR" ]; then
        if [ "$START_HOUR" -lt "$END_HOUR" ]; then
            if [ "$CURRENT_HOUR" -ge "$END_HOUR" ] || [ "$CURRENT_HOUR" -lt "$START_HOUR" ]; then
                log "Outside allowed window (${START_HOUR}:00–${END_HOUR}:00). Exiting."
                exit 0
            fi
        else
            if [ "$CURRENT_HOUR" -ge "$END_HOUR" ] && [ "$CURRENT_HOUR" -lt "$START_HOUR" ]; then
                log "Outside allowed window. Exiting."
                exit 0
            fi
        fi
    fi
}

# ==============================================================================
# FAILURE MARKERS & RETRY
# ==============================================================================

write_failed_marker() {
    local MARKER_FILE="$1"
    local REASON="$2"

    local EXISTING_COUNT=0
    if [ -f "$MARKER_FILE" ]; then
        EXISTING_COUNT=$(sed -n '2p' "$MARKER_FILE" 2>/dev/null | tr -d '[:space:]')
        EXISTING_COUNT="${EXISTING_COUNT:-0}"
    fi

    printf '%s\n%s\n' "$REASON" "$((EXISTING_COUNT + 1))" > "$MARKER_FILE"
}

retry_failed() {
    local MARKER_FILE="$1"
    [ ! -f "$MARKER_FILE" ] && return 1

    local REASON
    REASON=$(head -n1 "$MARKER_FILE" 2>/dev/null | tr -d '[:space:]')
    local RETRY_COUNT
    RETRY_COUNT=$(sed -n '2p' "$MARKER_FILE" 2>/dev/null | tr -d '[:space:]')
    RETRY_COUNT="${RETRY_COUNT:-0}"

    case "$REASON" in
        "$FAIL_CURL_TIMEOUT"|"$FAIL_API_ERROR")
            if [ "$RETRY_COUNT" -lt "$MAX_RETRIES_SOFT" ]; then
                log "  RETRY: Transient failure (${REASON}), attempt $((RETRY_COUNT + 1))/${MAX_RETRIES_SOFT}."
                rm -f "$MARKER_FILE"
                return 0
            else
                log "  RETRY: ${REASON} exhausted ${MAX_RETRIES_SOFT} retries. Permanent skip."
                return 1
            fi
            ;;
        *) return 1 ;;
    esac
}

# ==============================================================================
# VALIDATE SRT
# ==============================================================================

validate_srt() {
    local SRT_FILE="$1"

    local FILE_BYTES
    FILE_BYTES=$(stat -c%s "$SRT_FILE" 2>/dev/null || echo 0)
    if [ "$FILE_BYTES" -lt 100 ]; then
        log "  VALIDATE: FAIL — file is suspiciously small (${FILE_BYTES} bytes)."
        return 1
    fi

    local CUE_COUNT
    CUE_COUNT=$(grep -c ' --> ' "$SRT_FILE" 2>/dev/null || echo 0)
    if [ "$CUE_COUNT" -lt "$MIN_CUE_COUNT" ]; then
        log "  VALIDATE: FAIL — only ${CUE_COUNT} cues (minimum: ${MIN_CUE_COUNT})."
        return 1
    fi

    local AVG_DUR
    AVG_DUR=$(grep ' --> ' "$SRT_FILE" | awk '
        function ts_to_sec(ts) {
            gsub(",", ".", ts)
            split(ts, p, ":")
            return p[1]*3600 + p[2]*60 + p[3]
        }
        {
            split($0, parts, " --> ")
            dur = ts_to_sec(parts[2]) - ts_to_sec(parts[1])
            if (dur > 0) { total += dur; count++ }
        }
        END { if (count > 0) printf "%.1f", total/count; else print "0" }
    ')
    AVG_DUR="${AVG_DUR:-0}"

    if [ "$(awk -v a="$AVG_DUR" -v m="$MAX_AVG_CUE_DURATION" 'BEGIN{print(a>m)?"1":"0"}')" = "1" ]; then
        log "  VALIDATE: FAIL — avg cue duration ${AVG_DUR}s exceeds ${MAX_AVG_CUE_DURATION}s."
        return 1
    fi

    local AVG_CHARS
    AVG_CHARS=$(grep -v ' --> ' "$SRT_FILE" | grep -v '^[0-9]*$' | grep -v '^$' | \
        awk '{ t+=length; c++ } END { if(c>0) printf "%.0f",t/c; else print "0" }')
    AVG_CHARS="${AVG_CHARS:-0}"

    if [ "$(awk -v a="$AVG_CHARS" -v m="$MAX_AVG_CUE_CHARS" 'BEGIN{print(a>m)?"1":"0"}')" = "1" ]; then
        log "  VALIDATE: FAIL — avg cue text ${AVG_CHARS} chars exceeds ${MAX_AVG_CUE_CHARS}."
        return 1
    fi

    log "  VALIDATE: OK — ${CUE_COUNT} cues, avg duration ${AVG_DUR}s, avg text ${AVG_CHARS} chars."
    return 0
}

# ==============================================================================
# CLEAN HALLUCINATIONS
# ==============================================================================

clean_hallucinations() {
    local SRT_FILE="$1"
    local CLEANED_FILE="${SRT_FILE}.cleaned"

    python3 - "$SRT_FILE" "$CLEANED_FILE" "$HALLUCINATION_REPEAT_THRESHOLD" <<'PYEOF'
import sys, re

srt_path   = sys.argv[1]
out_path   = sys.argv[2]
threshold  = int(sys.argv[3])

def parse_srt(content):
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    cues = []
    for block in re.split(r'\n{2,}', content.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 2: continue
        ts_i = next((i for i, l in enumerate(lines) if ' --> ' in l), None)
        if ts_i is None: continue
        try:
            parts = lines[ts_i].split(' --> ')
            start, end = parts[0].strip(), parts[1].strip()
        except Exception: continue
        text = '\n'.join(lines[ts_i+1:]).strip()
        if text:
            cues.append({'start': start, 'end': end, 'text': text})
    return cues

with open(srt_path, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()

cues = parse_srt(content)
if not cues:
    print(0); sys.exit(0)

output, removed, i = [], 0, 0
while i < len(cues):
    ct = cues[i]['text'].strip().lower()
    j = i + 1
    while j < len(cues) and cues[j]['text'].strip().lower() == ct: j += 1
    run = j - i
    if run >= threshold:
        output.append({'start': cues[i]['start'], 'end': cues[j-1]['end'], 'text': cues[i]['text']})
        removed += (run - 1)
    else:
        output.extend(cues[i:j])
    i = j

lines = []
for idx, cue in enumerate(output, 1):
    lines += [str(idx), f"{cue['start']} --> {cue['end']}", cue['text'], '']

with open(out_path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))
print(removed)
PYEOF

    if [ $? -ne 0 ] || [ ! -s "$CLEANED_FILE" ]; then
        log "  CLEAN: Hallucination cleaner failed. Keeping original."
        rm -f "$CLEANED_FILE"
        echo "0"
        return
    fi
    mv "$CLEANED_FILE" "$SRT_FILE"
}

# ==============================================================================
# WATERMARK
# ==============================================================================

apply_watermark() {
    local SUB_FILE="$1"
    if [ "$ENABLE_WATERMARK" = true ] && [ -n "$WATERMARK_TEXT" ]; then
        if ! head -n 15 "$SUB_FILE" | grep -qF "$WATERMARK_SEARCH"; then
            printf '0\n00:00:00,000 --> 00:00:05,000\n%s\n\n' "${WATERMARK_TEXT}" \
                | cat - "$SUB_FILE" > "${SUB_FILE}.tmp"
            mv "${SUB_FILE}.tmp" "$SUB_FILE"
        fi
    fi
}

# ==============================================================================
# ERROR RECOVERY & RESILIENCE
# ==============================================================================

health_check_api() {
    local API_URL="${1:-$WHISPER_API_URL}"
    local MAX_WAIT="${2:-120}"

    local STATUS
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$API_URL" 2>/dev/null)

    if [ "$STATUS" -eq 200 ] || [ "$STATUS" -eq 405 ] || [ "$STATUS" -eq 422 ]; then
        return 0
    fi

    log "HEALTH CHECK: API unresponsive (HTTP ${STATUS}). Waiting up to ${MAX_WAIT}s..."

    local ELAPSED=0
    while [ $ELAPSED -lt "$MAX_WAIT" ]; do
        sleep 10
        ELAPSED=$((ELAPSED + 10))
        STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$API_URL" 2>/dev/null)
        if [ "$STATUS" -eq 200 ] || [ "$STATUS" -eq 405 ] || [ "$STATUS" -eq 422 ]; then
            log "HEALTH CHECK: API recovered after ${ELAPSED}s."
            return 0
        fi
    done

    log "HEALTH CHECK: FATAL — API unrecoverable after ${MAX_WAIT}s."
    return 1
}

resume_partial() {
    local RECOVERED=0
    local DELETED=0

    for DIR in "$@"; do
        while IFS= read -r -d '' TEMP_FILE; do
            local DIR_PATH
            DIR_PATH=$(dirname "$TEMP_FILE")
            local BASENAME
            BASENAME=$(basename "$TEMP_FILE")
            local BASE_NO_AI="${BASENAME%.ai.srt}"

            log "RESUME: Found partial file: ${BASENAME}"

            if validate_srt "$TEMP_FILE" 2>/dev/null; then
                local FINAL_NAME
                if [[ "$BASE_NO_AI" =~ \.([a-z]{2,3})$ ]]; then
                    FINAL_NAME="${BASE_NO_AI}.srt"
                else
                    FINAL_NAME="${BASE_NO_AI}.en.srt"
                fi

                local FINAL_PATH="${DIR_PATH}/${FINAL_NAME}"

                if [ -f "$FINAL_PATH" ]; then
                    log "  RESUME: Final file already exists. Deleting partial."
                    rm -f "$TEMP_FILE"
                    DELETED=$((DELETED + 1))
                else
                    apply_watermark "$TEMP_FILE"
                    mv "$TEMP_FILE" "$FINAL_PATH"
                    log "  RESUME: Promoted to ${FINAL_NAME}"
                    RECOVERED=$((RECOVERED + 1))
                fi
            else
                log "  RESUME: Failed validation. Deleting."
                rm -f "$TEMP_FILE"
                DELETED=$((DELETED + 1))
            fi
        done < <(find "$DIR" -type f -name "*.ai.srt" -print0 2>/dev/null)
    done

    log "RESUME: Recovered ${RECOVERED}, deleted ${DELETED} partial files."
    echo "$RECOVERED"
}

quarantine_video() {
    local VIDEO_FILE="$1"
    local REASON="${2:-unknown}"

    mkdir -p "$QUARANTINE_DIR"

    local QUARANTINE_LOG="${QUARANTINE_DIR}/quarantine.log"
    local BASENAME
    BASENAME=$(basename "$VIDEO_FILE")
    local FILE_SIZE="unknown"
    local CODEC_INFO="unknown"
    local DURATION="unknown"

    if [ -f "$VIDEO_FILE" ]; then
        FILE_SIZE=$(stat -c%s "$VIDEO_FILE" 2>/dev/null | awk '{printf "%.0fMB", $1/1024/1024}')

        CODEC_INFO=$(ffprobe \
            -v error -select_streams a:0 \
            -show_entries stream=codec_name,channels,sample_rate \
            -of csv=p=0 "$VIDEO_FILE" 2>/dev/null || echo "probe_failed")

        DURATION=$(ffprobe \
            -v error -show_entries format=duration \
            -of default=noprint_wrappers=1:nokey=1 \
            "$VIDEO_FILE" 2>/dev/null | awk '{m=int($1/60); s=int($1%60); printf "%dm%ds", m, s}')
    fi

    local ENTRY="$(date '+%Y-%m-%d %H:%M:%S')|${REASON}|${FILE_SIZE}|${DURATION}|${CODEC_INFO}|${VIDEO_FILE}"
    echo "$ENTRY" >> "$QUARANTINE_LOG"

    log "QUARANTINE: ${BASENAME} — ${REASON} (${FILE_SIZE}, ${DURATION}, codec: ${CODEC_INFO})"
}

# ==============================================================================
# QUALITY & POST-PROCESSING
# ==============================================================================

normalize_timestamps() {
    local SRT_FILE="$1"
    local OUT_FILE="${SRT_FILE}.normalized"

    local FIXES
    FIXES=$(python3 - "$SRT_FILE" "$OUT_FILE" <<'PYEOF'
import sys, re

MIN_DURATION = 0.5  # seconds

def ts_to_sec(ts):
    ts = ts.strip().replace(',', '.')
    h, m, s = ts.split(':')
    return float(h)*3600 + float(m)*60 + float(s)

def sec_to_ts(s):
    if s < 0: s = 0
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}".replace('.', ',')

def parse_srt(content):
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    cues = []
    for block in re.split(r'\n{2,}', content.strip()):
        lines = block.strip().splitlines()
        ts_i = next((i for i, l in enumerate(lines) if ' --> ' in l), None)
        if ts_i is None: continue
        try:
            parts = lines[ts_i].split(' --> ')
            start = ts_to_sec(parts[0])
            end = ts_to_sec(parts[1])
        except Exception: continue
        text = '\n'.join(lines[ts_i+1:]).strip()
        if text:
            cues.append({'start': start, 'end': end, 'text': text})
    return cues

with open(sys.argv[1], 'r', encoding='utf-8', errors='replace') as f:
    cues = parse_srt(f.read())

fixes = 0

# Pass 1: remove negative duration cues
clean = []
for c in cues:
    if c['end'] <= c['start']:
        if c['end'] == c['start']:
            c['end'] = c['start'] + MIN_DURATION
            fixes += 1
            clean.append(c)
        else:
            fixes += 1  # removed
    else:
        clean.append(c)

# Pass 2: enforce minimum duration
for c in clean:
    dur = c['end'] - c['start']
    if 0 < dur < MIN_DURATION:
        c['end'] = c['start'] + MIN_DURATION
        fixes += 1

# Pass 3: fix overlaps
for i in range(len(clean) - 1):
    if clean[i]['end'] > clean[i+1]['start']:
        clean[i]['end'] = clean[i+1]['start'] - 0.001
        if clean[i]['end'] <= clean[i]['start']:
            clean[i]['end'] = clean[i]['start'] + MIN_DURATION
        fixes += 1

# Write
lines = []
for idx, c in enumerate(clean, 1):
    lines += [str(idx), f"{sec_to_ts(c['start'])} --> {sec_to_ts(c['end'])}", c['text'], '']

with open(sys.argv[2], 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))

print(fixes)
PYEOF
    )

    if [ $? -ne 0 ] || [ ! -s "$OUT_FILE" ]; then
        log "  NORMALIZE: Failed. Keeping original."
        rm -f "$OUT_FILE"
        echo "0"
        return
    fi

    FIXES="${FIXES:-0}"
    if [ "$FIXES" -gt 0 ]; then
        mv "$OUT_FILE" "$SRT_FILE"
        log "  NORMALIZE: Applied ${FIXES} timestamp fix(es)."
    else
        rm -f "$OUT_FILE"
        log "  NORMALIZE: No timestamp issues found."
    fi
    echo "$FIXES"
}

detect_music_cues() {
    local SRT_FILE="$1"
    local MODE="${2:-strip}"
    local OUT_FILE="${SRT_FILE}.musicclean"

    local FOUND
    FOUND=$(python3 - "$SRT_FILE" "$OUT_FILE" "$MODE" <<'PYEOF'
import sys, re

srt_path = sys.argv[1]
out_path = sys.argv[2]
mode     = sys.argv[3]

MUSIC_PATTERNS = [
    r'^[\s♪♫🎵🎶\.\-]+$',
    r'^\[?\(?\s*music\s*\)?\]?\.?$',
    r'^\[?\(?\s*singing\s*\)?\]?\.?$',
    r'^\[?\(?\s*humming\s*\)?\]?\.?$',
    r'^\[?\(?\s*applause\s*\)?\]?\.?$',
    r'^\[?\(?\s*laughter\s*\)?\]?\.?$',
]

def is_music(text):
    t = text.strip()
    if len(t) <= 2 and not t.isalnum():
        return True
    for pat in MUSIC_PATTERNS:
        if re.match(pat, t, re.IGNORECASE):
            return True
    return False

def parse_srt(content):
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    cues = []
    for block in re.split(r'\n{2,}', content.strip()):
        lines = block.strip().splitlines()
        ts_i = next((i for i, l in enumerate(lines) if ' --> ' in l), None)
        if ts_i is None: continue
        try:
            parts = lines[ts_i].split(' --> ')
            start, end = parts[0].strip(), parts[1].strip()
        except Exception: continue
        text = '\n'.join(lines[ts_i+1:]).strip()
        if text:
            cues.append({'start': start, 'end': end, 'text': text})
    return cues

with open(srt_path, 'r', encoding='utf-8', errors='replace') as f:
    cues = parse_srt(f.read())

found = 0
output = []
for c in cues:
    if is_music(c['text']):
        found += 1
        if mode == 'tag':
            c['text'] = '[♪] ' + c['text']
            output.append(c)
    else:
        output.append(c)

lines = []
for idx, c in enumerate(output, 1):
    lines += [str(idx), f"{c['start']} --> {c['end']}", c['text'], '']

with open(out_path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))

print(found)
PYEOF
    )

    if [ $? -ne 0 ] || [ ! -s "$OUT_FILE" ]; then
        log "  MUSIC: Detection failed. Keeping original."
        rm -f "$OUT_FILE"
        echo "0"
        return
    fi

    FOUND="${FOUND:-0}"
    if [ "$FOUND" -gt 0 ]; then
        mv "$OUT_FILE" "$SRT_FILE"
        log "  MUSIC: ${MODE^} ${FOUND} music/noise cue(s)."
    else
        rm -f "$OUT_FILE"
        log "  MUSIC: No music cues detected."
    fi
    echo "$FOUND"
}

score_confidence() {
    local VIDEO_FILE="$1"
    local SRT_FILE="$2"
    local OFFSET="${3:-120}"
    local SAMPLE_DURATION=30

    local TEMP_SAMPLE="/tmp/confidence_sample_$$.wav"
    local TEMP_SRT="/tmp/confidence_check_$$.srt"

    # Direct ffmpeg call (container has ffmpeg installed)
    ffmpeg -nostdin -ss "$OFFSET" -i "$VIDEO_FILE" \
        -map 0:a:0 -t "$SAMPLE_DURATION" -vn -acodec pcm_s16le -ar 16000 -ac 1 \
        -y "$TEMP_SAMPLE" -loglevel quiet 2>/dev/null

    if [ ! -s "$TEMP_SAMPLE" ]; then
        log "  CONFIDENCE: Could not extract audio sample."
        echo "50"
        return
    fi

    local API_URL="${WHISPER_API_URL}?task=transcribe&output=srt"
    curl -s --fail --connect-timeout 30 --max-time 300 \
        -X POST -F "audio_file=@${TEMP_SAMPLE}" \
        "$API_URL" -o "$TEMP_SRT" 2>/dev/null

    rm -f "$TEMP_SAMPLE"

    if [ ! -s "$TEMP_SRT" ] || ! grep -qF -- '-->' "$TEMP_SRT"; then
        log "  CONFIDENCE: Verification pass failed."
        rm -f "$TEMP_SRT"
        echo "50"
        return
    fi

    local SCORE
    SCORE=$(python3 - "$SRT_FILE" "$TEMP_SRT" "$OFFSET" "$SAMPLE_DURATION" <<'PYEOF'
import sys, re

def extract_text(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()
    texts = []
    for block in re.split(r'\n{2,}', content.strip()):
        lines = block.strip().splitlines()
        ts_i = next((i for i, l in enumerate(lines) if ' --> ' in l), None)
        if ts_i is None: continue
        text = ' '.join(lines[ts_i+1:]).strip().lower()
        text = re.sub(r'[^\w\s]', '', text)
        if text: texts.append(text)
    return ' '.join(texts).split()

def ts_to_sec(ts):
    ts = ts.strip().replace(',', '.')
    h, m, s = ts.split(':')
    return float(h)*3600 + float(m)*60 + float(s)

def extract_text_in_range(path, start_sec, duration):
    end_sec = start_sec + duration
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()
    texts = []
    for block in re.split(r'\n{2,}', content.strip()):
        lines = block.strip().splitlines()
        ts_i = next((i for i, l in enumerate(lines) if ' --> ' in l), None)
        if ts_i is None: continue
        try:
            parts = lines[ts_i].split(' --> ')
            cue_start = ts_to_sec(parts[0])
        except: continue
        if start_sec <= cue_start <= end_sec:
            text = ' '.join(lines[ts_i+1:]).strip().lower()
            text = re.sub(r'[^\w\s]', '', text)
            if text: texts.append(text)
    return ' '.join(texts).split()

offset = float(sys.argv[3])
duration = float(sys.argv[4])

original_words = set(extract_text_in_range(sys.argv[1], offset, duration))
verify_words = set(extract_text(sys.argv[2]))

if not original_words or not verify_words:
    print(50)
    sys.exit(0)

union = original_words | verify_words
intersection = original_words & verify_words
score = int((len(intersection) / len(union)) * 100) if union else 50
print(score)
PYEOF
    )

    rm -f "$TEMP_SRT"
    SCORE="${SCORE:-50}"
    log "  CONFIDENCE: Score ${SCORE}/100"
    echo "$SCORE"
}

# ==============================================================================
# LIBRARY MANAGEMENT
# ==============================================================================

verify_encoding() {
    local SRT_FILE="$1"

    if [ ! -f "$SRT_FILE" ]; then return 0; fi

    local DETECTED
    DETECTED=$(file -bi "$SRT_FILE" 2>/dev/null | grep -oP 'charset=\K[^\s;]+')
    DETECTED="${DETECTED:-unknown}"

    local HAS_BOM=false
    if [ "$(xxd -l 3 -p "$SRT_FILE" 2>/dev/null)" = "efbbbf" ]; then
        HAS_BOM=true
    fi

    if [ "$DETECTED" = "utf-8" ] && [ "$HAS_BOM" = false ]; then
        return 0
    fi

    log "  ENCODING: ${SRT_FILE##*/} is ${DETECTED}$([ "$HAS_BOM" = true ] && echo ' with BOM'). Converting to UTF-8..."

    local TEMP="${SRT_FILE}.enc_tmp"

    if [ "$HAS_BOM" = true ]; then
        tail -c +4 "$SRT_FILE" | iconv -f "${DETECTED}" -t UTF-8 -c > "$TEMP" 2>/dev/null
    else
        iconv -f "${DETECTED}" -t UTF-8 -c "$SRT_FILE" > "$TEMP" 2>/dev/null
    fi

    if [ -s "$TEMP" ]; then
        mv "$TEMP" "$SRT_FILE"
        return 1
    else
        log "  ENCODING: Conversion failed. Keeping original."
        rm -f "$TEMP"
        return 0
    fi
}

cleanup_pgs() {
    local DELETED=0

    for DIR in "$@"; do
        while IFS= read -r -d '' PGS_FILE; do
            local PGS_DIR
            PGS_DIR=$(dirname "$PGS_FILE")
            local PGS_BASE
            PGS_BASE=$(basename "$PGS_FILE")
            local BASE_NO_EXT="${PGS_BASE%.sup}"

            local VIDEO_STEM="$BASE_NO_EXT"
            if [[ "$BASE_NO_EXT" =~ ^(.+)\.([a-z]{2,3}(-[a-z]{2,4})?)$ ]]; then
                VIDEO_STEM="${BASH_REMATCH[1]}"
            fi

            shopt -s nullglob nocaseglob
            local SRT_FILES=( "${PGS_DIR}/${VIDEO_STEM}".*.srt )
            shopt -u nullglob nocaseglob

            if [ ${#SRT_FILES[@]} -gt 0 ]; then
                log "PGS CLEANUP: Deleting ${PGS_BASE} (${#SRT_FILES[@]} SRT file(s) exist)"
                rm -f "$PGS_FILE"
                DELETED=$((DELETED + 1))
            fi
        done < <(find "$DIR" -type f -iname "*.sup" -print0 2>/dev/null)

        while IFS= read -r -d '' SUB_FILE; do
            local SUB_DIR
            SUB_DIR=$(dirname "$SUB_FILE")
            local SUB_BASE
            SUB_BASE=$(basename "$SUB_FILE")
            local IDX_FILE="${SUB_DIR}/${SUB_BASE%.sub}.idx"

            if [ ! -f "$IDX_FILE" ]; then continue; fi

            local BASE_NO_EXT="${SUB_BASE%.sub}"
            local VIDEO_STEM="$BASE_NO_EXT"
            if [[ "$BASE_NO_EXT" =~ ^(.+)\.([a-z]{2,3}(-[a-z]{2,4})?)$ ]]; then
                VIDEO_STEM="${BASH_REMATCH[1]}"
            fi

            shopt -s nullglob nocaseglob
            local SRT_FILES=( "${SUB_DIR}/${VIDEO_STEM}".*.srt )
            shopt -u nullglob nocaseglob

            if [ ${#SRT_FILES[@]} -gt 0 ]; then
                log "PGS CLEANUP: Deleting ${SUB_BASE} + .idx (${#SRT_FILES[@]} SRT file(s) exist)"
                rm -f "$SUB_FILE" "$IDX_FILE"
                DELETED=$((DELETED + 2))
            fi
        done < <(find "$DIR" -type f -iname "*.sub" -print0 2>/dev/null)
    done

    log "PGS CLEANUP: Deleted ${DELETED} image-based subtitle file(s)."
    echo "$DELETED"
}

# ---------------------------------------------------------------------------
# deduplicate_subs()
#
# Detects cases where multiple SRT files exist for the same language
# (e.g., one downloaded + one from Whisper). Keeps the one with more cues
# and better timing. Deletes or renames the inferior one.
#
# Arguments: $1 = directory to scan (recursive)
# Returns:   number of duplicates removed (printed to stdout)
# ---------------------------------------------------------------------------
deduplicate_subs() {
    local SCAN_DIR="$1"

    python3 - "$SCAN_DIR" <<'PYEOF'
import os, sys, re
from collections import defaultdict

scan_dir = sys.argv[1]
removed = 0

groups = defaultdict(list)

for root, dirs, files in os.walk(scan_dir):
    srt_files = [f for f in files if f.lower().endswith('.srt')]
    for srt in srt_files:
        path = os.path.join(root, srt)
        name = srt
        for suffix in ['.srt']:
            name = name[:-len(suffix)] if name.lower().endswith(suffix) else name

        parts = name.split('.')
        lang = None
        base_parts = []
        tags = []
        for p in parts:
            pl = p.lower()
            if pl in ('hi', 'sdh', 'cc', 'forced', 'ai'):
                tags.append(pl)
            elif len(pl) >= 2 and len(pl) <= 3 and pl.isalpha() and not base_parts:
                base_parts.append(p)
            elif len(pl) >= 2 and len(pl) <= 5 and re.match(r'^[a-z]{2,3}(-[a-z]{2,4})?$', pl):
                lang = pl
            else:
                base_parts.append(p)

        if lang is None:
            lang = 'unknown'

        base_name = '.'.join(base_parts)
        key = (root, base_name, lang)
        groups[key].append(path)

for key, paths in groups.items():
    if len(paths) <= 1:
        continue

    scored = []
    for p in paths:
        try:
            with open(p, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            cues = len(re.findall(r' --> ', content))
            size = os.path.getsize(p)
            scored.append((cues, size, p))
        except Exception:
            scored.append((0, 0, p))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    best = scored[0][2]
    for cues, size, path in scored[1:]:
        try:
            os.remove(path)
            removed += 1
            print(f"DEDUP: Removed {os.path.basename(path)} (kept {os.path.basename(best)}, {scored[0][0]} vs {cues} cues)")
        except Exception as e:
            print(f"DEDUP: Failed to remove {path}: {e}")

print(f"TOTAL:{removed}")
PYEOF

    log "DEDUP: Scan of ${SCAN_DIR} complete."
}

audit_library() {
    local REPORT_FILE="$1"
    shift
    local DIRS=("$@")

    mkdir -p "$(dirname "$REPORT_FILE")"

    local NO_SUBS=0
    local NO_TRANSLATIONS=0
    local ORPHANED_MARKERS=0
    local INVALID_SRTS=0
    local PGS_CLEANABLE=0
    local ENCODING_ISSUES=0
    local TOTAL_VIDEOS=0
    local TOTAL_SRTS=0

    {
        echo "============================================================"
        echo "LIBRARY AUDIT REPORT"
        echo "Generated: $(date '+%Y-%m-%d %H:%M:%S')"
        echo "Directories: ${DIRS[*]}"
        echo "============================================================"
        echo ""

        echo "## VIDEOS WITH NO SUBTITLES"
        echo ""
        while IFS= read -r -d '' VIDEO; do
            TOTAL_VIDEOS=$((TOTAL_VIDEOS + 1))
            local VDIR
            VDIR=$(dirname "$VIDEO")
            local VBASE
            VBASE=$(basename "$VIDEO")
            local VNAME="${VBASE%.*}"

            shopt -s nullglob nocaseglob
            local SUBS=( "${VDIR}/${VNAME}".*.srt )
            shopt -u nullglob nocaseglob

            if [ ${#SUBS[@]} -eq 0 ]; then
                echo "  [NO SUBS] ${VIDEO}"
                NO_SUBS=$((NO_SUBS + 1))
            fi
        done < <(find "${DIRS[@]}" -type f \( -iname "*.mkv" -o -iname "*.mp4" -o -iname "*.avi" -o -iname "*.mov" -o -iname "*.wmv" -o -iname "*.m4v" \) -print0 2>/dev/null)
        echo ""
        echo "  Total: ${NO_SUBS} videos without subtitles"
        echo ""

        echo "## VIDEOS MISSING TRANSLATIONS"
        echo ""
        local TARGET_LANGS=("zh" "es-MX")
        while IFS= read -r -d '' SRT; do
            TOTAL_SRTS=$((TOTAL_SRTS + 1))
            local SDIR
            SDIR=$(dirname "$SRT")
            local SBASE
            SBASE=$(basename "$SRT")
            if [[ ! "$SBASE" =~ \.en(\.hi|\.sdh)?\.srt$ ]] && [[ ! "$SBASE" =~ \.(hi|sdh)\.en\.srt$ ]]; then
                continue
            fi
            local SNAME
            SNAME=$(echo "$SBASE" | sed -E 's/\.en(\.hi|\.sdh)?\.srt$//I' | sed -E 's/\.(hi|sdh)\.en\.srt$//I')

            local MISSING_LANGS=()
            for TL in "${TARGET_LANGS[@]}"; do
                shopt -s nullglob nocaseglob
                local TL_FILES=( "${SDIR}/${SNAME}"*.${TL}.srt )
                shopt -u nullglob nocaseglob
                if [ ${#TL_FILES[@]} -eq 0 ]; then
                    MISSING_LANGS+=("$TL")
                fi
            done

            if [ ${#MISSING_LANGS[@]} -gt 0 ]; then
                echo "  [MISSING ${MISSING_LANGS[*]}] ${SRT}"
                NO_TRANSLATIONS=$((NO_TRANSLATIONS + 1))
            fi
        done < <(find "${DIRS[@]}" -type f -iname "*.srt" -print0 2>/dev/null)
        echo ""
        echo "  Total: ${NO_TRANSLATIONS} files missing translations"
        echo ""

        echo "## ORPHANED FAILURE MARKERS (>30 days)"
        echo ""
        while IFS= read -r -d '' MARKER; do
            local REASON
            REASON=$(head -n1 "$MARKER" 2>/dev/null | tr -d '[:space:]')
            local RETRIES
            RETRIES=$(sed -n '2p' "$MARKER" 2>/dev/null | tr -d '[:space:]')
            echo "  [${REASON:-LEGACY}|retries:${RETRIES:-0}] ${MARKER}"
            ORPHANED_MARKERS=$((ORPHANED_MARKERS + 1))
        done < <(find "${DIRS[@]}" -type f -name "*.ai.failed" -mtime +30 -print0 2>/dev/null)
        echo ""
        echo "  Total: ${ORPHANED_MARKERS} orphaned markers"
        echo ""

        echo "## INVALID SRT FILES"
        echo ""
        while IFS= read -r -d '' SRT; do
            if ! validate_srt "$SRT" >/dev/null 2>&1; then
                local CUES
                CUES=$(grep -c ' --> ' "$SRT" 2>/dev/null || echo 0)
                local SIZE
                SIZE=$(stat -c%s "$SRT" 2>/dev/null || echo 0)
                echo "  [INVALID cues:${CUES} size:${SIZE}b] ${SRT}"
                INVALID_SRTS=$((INVALID_SRTS + 1))
            fi
        done < <(find "${DIRS[@]}" -type f -iname "*.srt" -print0 2>/dev/null)
        echo ""
        echo "  Total: ${INVALID_SRTS} invalid SRT files"
        echo ""

        echo "## PGS/IMAGE SUBS WITH TEXT REPLACEMENTS AVAILABLE"
        echo ""
        for DIR in "${DIRS[@]}"; do
            while IFS= read -r -d '' PGS; do
                local PDIR
                PDIR=$(dirname "$PGS")
                local PBASE
                PBASE=$(basename "$PGS")
                local PNAME="${PBASE%.sup}"
                local VSTEM="$PNAME"
                if [[ "$PNAME" =~ ^(.+)\.([a-z]{2,3}(-[a-z]{2,4})?)$ ]]; then
                    VSTEM="${BASH_REMATCH[1]}"
                fi
                shopt -s nullglob nocaseglob
                local SRTS=( "${PDIR}/${VSTEM}".*.srt )
                shopt -u nullglob nocaseglob
                if [ ${#SRTS[@]} -gt 0 ]; then
                    echo "  [CLEANABLE] ${PGS}"
                    PGS_CLEANABLE=$((PGS_CLEANABLE + 1))
                fi
            done < <(find "$DIR" -type f -iname "*.sup" -print0 2>/dev/null)
        done
        echo ""
        echo "  Total: ${PGS_CLEANABLE} PGS files replaceable"
        echo ""

        echo "## ENCODING ISSUES (sampled)"
        echo ""
        local ENC_CHECKED=0
        while IFS= read -r -d '' SRT; do
            [ $ENC_CHECKED -ge 200 ] && break
            ENC_CHECKED=$((ENC_CHECKED + 1))
            local ENC
            ENC=$(file -bi "$SRT" 2>/dev/null | grep -oP 'charset=\K[^\s;]+')
            local BOM=""
            if [ "$(xxd -l 3 -p "$SRT" 2>/dev/null)" = "efbbbf" ]; then
                BOM=" +BOM"
            fi
            if [ "$ENC" != "utf-8" ] || [ -n "$BOM" ]; then
                echo "  [${ENC}${BOM}] ${SRT}"
                ENCODING_ISSUES=$((ENCODING_ISSUES + 1))
            fi
        done < <(find "${DIRS[@]}" -type f -iname "*.srt" -print0 2>/dev/null)
        echo ""
        echo "  Total: ${ENCODING_ISSUES} encoding issues (of ${ENC_CHECKED} checked)"
        echo ""

        echo "============================================================"
        echo "SUMMARY"
        echo "============================================================"
        echo "Total videos scanned:        ${TOTAL_VIDEOS}"
        echo "Total SRT files found:       ${TOTAL_SRTS}"
        echo "Videos without subtitles:    ${NO_SUBS}"
        echo "Missing translations:        ${NO_TRANSLATIONS}"
        echo "Orphaned failure markers:    ${ORPHANED_MARKERS}"
        echo "Invalid SRT files:           ${INVALID_SRTS}"
        echo "Cleanable PGS files:         ${PGS_CLEANABLE}"
        echo "Encoding issues:             ${ENCODING_ISSUES}"
        echo "============================================================"

    } > "$REPORT_FILE"

    log "AUDIT: Report written to ${REPORT_FILE}"
    log "AUDIT: ${NO_SUBS} no-subs, ${NO_TRANSLATIONS} no-translations, ${INVALID_SRTS} invalid, ${PGS_CLEANABLE} PGS cleanable"
}

generate_report() {
    local REPORT_FILE="${1:-${REPORT_DIR}/report_$(date '+%Y-%m-%d').md}"
    mkdir -p "$(dirname "$REPORT_FILE")"

    local TRANS_STATS="/app/data/lifetime_stats.env"
    local TRANSL_STATS="/app/data/translation_stats.env"
    local QUARANTINE_LOG="${QUARANTINE_DIR}/quarantine.log"

    {
        echo "# Pipeline Dashboard Report"
        echo "*Generated: $(date '+%Y-%m-%d %H:%M:%S')*"
        echo ""

        echo "## Transcription Lifetime Stats"
        echo ""
        if [ -f "$TRANS_STATS" ]; then
            source "$TRANS_STATS"
            local TOTAL_PROC=$(( ${LIFETIME_ENGLISH_PROCESSED:-0} + ${LIFETIME_BILINGUAL_PROCESSED:-0} + ${LIFETIME_FOREIGN_PROCESSED:-0} ))
            local HOURS=$(( ${LIFETIME_PROCESSING_SECONDS:-0} / 3600 ))
            local MINS=$(( (${LIFETIME_PROCESSING_SECONDS:-0} % 3600) / 60 ))
            local AVG_SEC=0
            [ "$TOTAL_PROC" -gt 0 ] && AVG_SEC=$(( ${LIFETIME_PROCESSING_SECONDS:-0} / TOTAL_PROC ))

            echo "| Metric | Value |"
            echo "|--------|-------|"
            echo "| Videos Scanned | ${LIFETIME_SCANNED:-0} |"
            echo "| English Processed | ${LIFETIME_ENGLISH_PROCESSED:-0} |"
            echo "| Bilingual Processed | ${LIFETIME_BILINGUAL_PROCESSED:-0} |"
            echo "| Foreign Processed | ${LIFETIME_FOREIGN_PROCESSED:-0} |"
            echo "| **Total Processed** | **${TOTAL_PROC}** |"
            echo "| Skipped (Existing) | ${LIFETIME_SKIPPED_EXISTING:-0} |"
            echo "| Skipped (Failed) | ${LIFETIME_SKIPPED_FAILED:-0} |"
            echo "| Skipped (Too Large) | ${LIFETIME_SKIPPED_SIZE:-0} |"
            echo "| Hallucinations Cleaned | ${LIFETIME_HALLUCINATIONS_CLEANED:-0} |"
            echo "| Total Processing Time | ${HOURS}h ${MINS}m |"
            echo "| Avg Time per Video | ${AVG_SEC}s |"
        else
            echo "*No transcription stats file found.*"
        fi
        echo ""

        echo "## Translation Lifetime Stats"
        echo ""
        if [ -f "$TRANSL_STATS" ]; then
            source "$TRANSL_STATS"
            local TL_HOURS=$(( ${LIFETIME_PROCESSING_SECONDS:-0} / 3600 ))
            local TL_MINS=$(( (${LIFETIME_PROCESSING_SECONDS:-0} % 3600) / 60 ))
            local TL_AVG=0
            [ "${LIFETIME_PROCESSED:-0}" -gt 0 ] && TL_AVG=$(( ${LIFETIME_PROCESSING_SECONDS:-0} / ${LIFETIME_PROCESSED} ))

            echo "| Metric | Value |"
            echo "|--------|-------|"
            echo "| Source Files Scanned | ${LIFETIME_SCANNED:-0} |"
            echo "| Translations Completed | ${LIFETIME_PROCESSED:-0} |"
            echo "| Skipped (Existing) | ${LIFETIME_SKIPPED_EXISTING:-0} |"
            echo "| Skipped (Failed) | ${LIFETIME_SKIPPED_FAILED:-0} |"
            echo "| Total Processing Time | ${TL_HOURS}h ${TL_MINS}m |"
            echo "| Avg Time per Translation | ${TL_AVG}s |"
        else
            echo "*No translation stats file found.*"
        fi
        echo ""

        echo "## Quarantined Files"
        echo ""
        if [ -f "$QUARANTINE_LOG" ]; then
            local Q_COUNT
            Q_COUNT=$(wc -l < "$QUARANTINE_LOG")
            echo "Total quarantined: ${Q_COUNT}"
            echo ""
            echo "**Recent entries (last 20):**"
            echo ""
            echo '```'
            tail -n 20 "$QUARANTINE_LOG"
            echo '```'
        else
            echo "*No quarantined files.*"
        fi
        echo ""

        echo "## Library Coverage"
        echo ""

        local V_TOTAL=0
        local V_WITH_SUBS=0

        while IFS= read -r -d '' V; do
            V_TOTAL=$((V_TOTAL + 1))
            local VD
            VD=$(dirname "$V")
            local VN
            VN=$(basename "$V")
            VN="${VN%.*}"
            shopt -s nullglob nocaseglob
            local S=( "${VD}/${VN}".*.srt )
            shopt -u nullglob nocaseglob
            if [ ${#S[@]} -gt 0 ]; then
                V_WITH_SUBS=$((V_WITH_SUBS + 1))
            fi
        done < <(find "${MOVIE_DIR}" "${TV_DIR}" -type f \( -iname "*.mkv" -o -iname "*.mp4" -o -iname "*.avi" -o -iname "*.mov" -o -iname "*.wmv" -o -iname "*.m4v" \) -print0 2>/dev/null)

        local COVERAGE=0
        [ "$V_TOTAL" -gt 0 ] && COVERAGE=$(( V_WITH_SUBS * 100 / V_TOTAL ))

        echo "| Metric | Value |"
        echo "|--------|-------|"
        echo "| Total Videos | ${V_TOTAL} |"
        echo "| Videos with Subtitles | ${V_WITH_SUBS} |"
        echo "| **Coverage** | **${COVERAGE}%** |"
        echo ""

    } > "$REPORT_FILE"

    log "REPORT: Dashboard written to ${REPORT_FILE}"
}

# ==============================================================================
log "Loaded common_lib.sh v2.0 (containerized)"
