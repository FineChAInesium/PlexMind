#!/bin/bash
# ==============================================================================

umask 077
# common_lib.sh — Shared Infrastructure for Transcription/Translation Pipeline
# Version: 0.8.22 — PlexMind release line
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
#   QUARANTINE_DIR      — quarantine log plus moved-aside subtitles (pgs/,
#                         partials/, encoding-backups/)
#   REPORT_DIR          — where generate_report writes output
# ==============================================================================

# --- Defaults for optional config ---
ENABLE_WATERMARK="${ENABLE_WATERMARK:-true}"
# Not ${WATERMARK_TEXT:-...}: the first unquoted } in the default would end the
# expansion and append "<i>...</i>}" to every configured value.
WATERMARK_TEXT="${WATERMARK_TEXT:-}"
[ -n "$WATERMARK_TEXT" ] || WATERMARK_TEXT='{\an8}<i>Brought to you by PlexMind</i>'
WATERMARK_SEARCH="${WATERMARK_SEARCH:-PlexMind}"
MIN_CUE_COUNT="${MIN_CUE_COUNT:-5}"
MAX_AVG_CUE_DURATION="${MAX_AVG_CUE_DURATION:-15}"
MAX_AVG_CUE_CHARS="${MAX_AVG_CUE_CHARS:-150}"
HALLUCINATION_REPEAT_THRESHOLD="${HALLUCINATION_REPEAT_THRESHOLD:-3}"
MAX_RETRIES_SOFT="${MAX_RETRIES_SOFT:-3}"
RUN_NOW="${RUN_NOW:-0}"
MAX_RUNTIME_MINUTES="${MAX_RUNTIME_MINUTES:-0}"
SCRIPT_STARTED_AT="${SCRIPT_STARTED_AT:-$(date +%s)}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-7}"
SUBTITLE_FILE_MODE="${SUBTITLE_FILE_MODE:-0644}"
SUBTITLE_UID="${SUBTITLE_UID:-}"
SUBTITLE_GID="${SUBTITLE_GID:-}"

DATA_DIR="${DATA_DIR:-/app/data}"
QUARANTINE_DIR="${QUARANTINE_DIR:-${DATA_DIR}/quarantine}"
REPORT_DIR="${REPORT_DIR:-${DATA_DIR}/reports}"

# Media directories (container paths)
MOVIE_DIR="${MOVIE_DIR:-/media/movies}"
TV_DIR="${TV_DIR:-/media/tv}"
SIDECAR_OWNED_CONTAINERS="${SIDECAR_OWNED_CONTAINERS:-}"

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

# Read the numeric lifetime-stat files as data. Never source files from the
# shared data volume: the API and worker containers can also write there.
load_numeric_stats() {
    local stats_file="$1" raw key value
    [ -f "$stats_file" ] || return 0
    while IFS= read -r raw || [ -n "$raw" ]; do
        raw="${raw%$'\r'}"
        [[ "$raw" =~ ^[[:space:]]*(#|$) ]] && continue
        if [[ "$raw" =~ ^(LIFETIME_[A-Z0-9_]+)=([0-9]+)$ ]]; then
            key="${BASH_REMATCH[1]}"
            value="${BASH_REMATCH[2]}"
            printf -v "$key" '%s' "$value"
        else
            log "WARNING: Ignoring invalid stats record in ${stats_file##*/}."
        fi
    done < "$stats_file"
}

finalize_subtitle_permissions() {
    local subtitle_file="$1"
    chmod "$SUBTITLE_FILE_MODE" "$subtitle_file" || {
        log "ERROR: Could not set subtitle mode ${SUBTITLE_FILE_MODE}: $subtitle_file"
        return 1
    }
    if [ -n "$SUBTITLE_UID" ] || [ -n "$SUBTITLE_GID" ]; then
        if { [ -n "$SUBTITLE_UID" ] && ! [[ "$SUBTITLE_UID" =~ ^[0-9]+$ ]]; } || \
           { [ -n "$SUBTITLE_GID" ] && ! [[ "$SUBTITLE_GID" =~ ^[0-9]+$ ]]; }; then
            log "ERROR: SUBTITLE_UID and SUBTITLE_GID must be numeric when configured."
            return 1
        fi
        local owner="$SUBTITLE_UID"
        [ -n "$SUBTITLE_GID" ] && owner="${owner}:${SUBTITLE_GID}"
        chown "$owner" "$subtitle_file" || {
            log "ERROR: Could not set configured subtitle ownership: $subtitle_file"
            return 1
        }
    fi
}

# Path of a Python helper shipped next to lib.sh.
script_helper() {
    printf '%s/%s' "${SCRIPT_DIR:-$(dirname "${BASH_SOURCE[0]}")}" "$1"
}

# Map a container (ISO 639-2) or Whisper language tag to the ISO 639-1 code
# used in subtitle names and Whisper requests. Whisper rejects other
# three-letter codes, so an unmapped one comes back empty and the caller
# detects the language instead. Anything else also comes back empty, so
# untrusted media metadata never reaches a path or URL.
normalize_lang_code() {
    local code
    code=$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]' | cut -d'-' -f1)
    case "$code" in
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
        ara) echo "ar" ;;
        ben) echo "bn" ;;
        bul) echo "bg" ;;
        cat) echo "ca" ;;
        ces|cze) echo "cs" ;;
        dan) echo "da" ;;
        ell|gre) echo "el" ;;
        fas|per) echo "fa" ;;
        fin) echo "fi" ;;
        heb) echo "he" ;;
        hin) echo "hi" ;;
        hrv) echo "hr" ;;
        hun) echo "hu" ;;
        ind) echo "id" ;;
        mal) echo "ml" ;;
        msa|may) echo "ms" ;;
        nld|dut) echo "nl" ;;
        nor|nob) echo "no" ;;
        pol) echo "pl" ;;
        ron|rum) echo "ro" ;;
        slk|slo) echo "sk" ;;
        srp) echo "sr" ;;
        swe) echo "sv" ;;
        tam) echo "ta" ;;
        tel) echo "te" ;;
        tgl|fil) echo "tl" ;;
        tha) echo "th" ;;
        tur) echo "tr" ;;
        ukr) echo "uk" ;;
        urd) echo "ur" ;;
        vie) echo "vi" ;;
        und|unk|unknown|none) echo "" ;;
        *)
            # haw is Whisper's only other three-letter code (yue maps to zh above).
            if [[ "$code" =~ ^[a-z]{2}$ ]] || [ "$code" = "haw" ]; then echo "$code"; else echo ""; fi
            ;;
    esac
}

prepare_log_file() {
    [ -n "${LOG_FILE:-}" ] || return 0

    local original_log="$LOG_FILE"
    local log_dir base archive_dir today_file legacy_file
    log_dir=$(dirname "$original_log")
    base=$(basename "$original_log" .log)
    archive_dir="${LOG_ARCHIVE_DIR:-${log_dir}/logs}"
    today_file="${archive_dir}/${base}-$(date '+%Y-%m-%d').log"

    mkdir -p "$archive_dir"
    if [ -f "$original_log" ] && [ ! -L "$original_log" ] && [ "$original_log" != "$today_file" ]; then
        legacy_file="${archive_dir}/${base}-$(date -r "$original_log" '+%Y-%m-%d_%H%M%S').legacy.log"
        mv "$original_log" "$legacy_file"
    fi

    touch "$today_file"
    ln -sfn "$today_file" "$original_log"
    LOG_FILE="$today_file"

    find "$archive_dir" -type f -name "${base}-*.log" -mtime +"$LOG_RETENTION_DAYS" -delete 2>/dev/null || true
}

# Acquire a lock file. Call early in the sourcing script. A held lock exits 75
# (EX_TEMPFAIL) before any work is done, so a launcher can tell "busy, retry
# later" apart from a failed run.
# Usage: acquire_lock "/tmp/my_script.lock"
acquire_lock() {
    local LOCK="$1"
    local lock_fd
    mkdir -p "$(dirname "$LOCK")"
    exec {lock_fd}>"$LOCK"
    # The file stays empty. umask 077 would make it unreadable to the API's
    # scheduler (another user), which probes the shared locks before a launch.
    chmod 644 "$LOCK" 2>/dev/null || true
    if ! flock -n "$lock_fd"; then
        log "Another job already holds ${LOCK}. Exiting without doing any work."
        exit 75
    fi
    PLEXMIND_LOCK_FDS="${PLEXMIND_LOCK_FDS:-} ${lock_fd}"
    echo "$$" > "${LOCK%.lock}.pid"
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


# Reason for a failed broker call, given curl's "<body>\n<status>" output:
# Docker's .message, the broker's own .detail, or that nothing answered.
broker_error_detail() {
    local response="$1" status="${1##*$'\n'}"
    if [ -z "$status" ] || [ "$status" = "000" ]; then
        printf 'Docker broker unreachable'
    elif [ "$response" != "$status" ]; then
        printf '%s' "${response%$'\n'*}" | jq -r '.message // .detail // empty' 2>/dev/null | head -n 1 | cut -c1-200 || true
    fi
}


stop_docker_container() {
    local service_name="$1"
    shift

    [ "${STOP_SIDECAR_CONTAINERS:-1}" = "1" ] || return 0

    local broker="${DOCKER_BROKER_URL:-}"
    if [ -z "$broker" ] || [ -z "${PLEXMIND_BROKER_TOKEN:-}" ]; then
        log "${service_name}: authenticated Docker broker is not configured; leaving sidecar container running."
        return 0
    fi
    if ! command -v curl >/dev/null 2>&1; then
        log "${service_name}: curl not available; cannot stop sidecar container."
        return 0
    fi

    local timeout="${CONTAINER_STOP_TIMEOUT_SECONDS:-30}"
    if [[ "$timeout" =~ ^[0-9]{1,3}$ ]]; then timeout=$((10#$timeout)); else timeout=30; fi
    local name response status detail
    for name in "$@"; do
        [ -n "${name:-}" ] || continue
        case " ${SIDECAR_OWNED_CONTAINERS} " in
            *" ${name} "*) ;;
            *) continue ;;
        esac
        # Bounded: transcribe.sh ignores INT/TERM around this request, so a
        # hung broker must not hold the job (and its locks) forever. Docker
        # waits up to t seconds and the broker up to t+15 for Docker.
        response=$(curl -sS --connect-timeout 10 --max-time $((timeout + 30)) -w '\n%{http_code}' -H "X-Broker-Token: ${PLEXMIND_BROKER_TOKEN}" -X POST "${broker%/}/containers/${name}/stop?t=${timeout}" 2>/dev/null || true)
        status="${response##*$'\n'}"
        case "$status" in
            204) log "${service_name}: stopped container ${name}."; return 0 ;;
            304) log "${service_name}: container ${name} already stopped."; return 0 ;;
            404) log "${service_name}: job-owned container ${name} no longer exists."; return 0 ;;
            *)
                # Runs from EXIT traps: report it, but do not fail the caller.
                detail=$(broker_error_detail "$response")
                log "ERROR: ${service_name}: could not stop job-owned container ${name} (HTTP ${status:-unknown}${detail:+: ${detail}}); it may still be running."
                return 0
                ;;
        esac
    done

    log "${service_name}: no job-owned sidecar container to stop; leaving shared service unchanged."
}


start_docker_container() {
    local service_name="$1"
    shift

    [ "${START_SIDECAR_CONTAINERS:-1}" = "1" ] || return 0

    local broker="${DOCKER_BROKER_URL:-}"
    if [ -z "$broker" ] || [ -z "${PLEXMIND_BROKER_TOKEN:-}" ]; then
        log "${service_name}: authenticated Docker broker is not configured; cannot start sidecar container."
        return 0
    fi
    if ! command -v curl >/dev/null 2>&1; then
        log "${service_name}: curl not available; cannot start sidecar container."
        return 0
    fi

    local name response status detail
    for name in "$@"; do
        [ -n "${name:-}" ] || continue
        response=$(curl -sS --connect-timeout 10 --max-time 60 -w '\n%{http_code}' -H "X-Broker-Token: ${PLEXMIND_BROKER_TOKEN}" -X POST "${broker%/}/containers/${name}/start" 2>/dev/null || true)
        status="${response##*$'\n'}"
        case "$status" in
            204)
                SIDECAR_OWNED_CONTAINERS="${SIDECAR_OWNED_CONTAINERS:+${SIDECAR_OWNED_CONTAINERS} }${name}"
                log "${service_name}: started container ${name}; this job owns the transition."
                return 0
                ;;
            304) log "${service_name}: container ${name} already running."; return 0 ;;
            404) ;;
            *)
                detail=$(broker_error_detail "$response")
                log "${service_name}: Docker start request for ${name} returned HTTP ${status:-unknown}${detail:+: ${detail}}."
                ;;
        esac
    done

    log "ERROR: ${service_name}: no configured sidecar container could be started."
    return 1
}


validate_media_directories() {
    local root entries total=0
    if [ "$MOVIE_DIR" = "$TV_DIR" ]; then
        log "ERROR: MOVIE_DIR and TV_DIR resolve to the same container path: ${MOVIE_DIR}"
        return 1
    fi
    for root in "$MOVIE_DIR" "$TV_DIR"; do
        if [ ! -d "$root" ]; then
            log "ERROR: configured media root is not a directory inside this container: ${root}"
            return 1
        fi
        if [ ! -r "$root" ] || [ ! -x "$root" ]; then
            log "ERROR: configured media root is not readable/searchable: ${root}"
            return 1
        fi
        entries=$(find "$root" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null | wc -l)
        total=$((total + entries))
        log "MEDIA ROOT: ${root} is accessible; nonempty=${entries}."
    done
    if [ "$total" -eq 0 ] && [ "${ALLOW_EMPTY_MEDIA_ROOTS:-0}" != "1" ]; then
        log "ERROR: all configured media roots are empty; set ALLOW_EMPTY_MEDIA_ROOTS=1 only for an intentional empty library."
        return 1
    fi
    return 0
}

wait_for_whisper_api() {
    local MAX="${1:-30}"
    log "Waiting for Whisper API at ${WHISPER_API_URL}..."
    local RETRY=0
    while [ $RETRY -lt "$MAX" ]; do
        local STATUS
        STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 10 --max-time 30 "${WHISPER_API_URL}" 2>/dev/null)
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

check_runtime() {
    [ "${MAX_RUNTIME_MINUTES:-0}" -gt 0 ] 2>/dev/null || return 0
    local now elapsed limit
    # translate.sh checks once per file and language; EPOCHSECONDS (bash 5)
    # saves a date fork per check.
    now="${EPOCHSECONDS:-$(date +%s)}"
    elapsed=$((now - SCRIPT_STARTED_AT))
    limit=$((MAX_RUNTIME_MINUTES * 60))
    if [ "$elapsed" -ge "$limit" ]; then
        log "Max runtime reached (${MAX_RUNTIME_MINUTES}m). Exiting cleanly before starting more work."
        exit 0
    fi
}

# ==============================================================================
# FAILURE MARKERS & RETRY
# ==============================================================================

# Sets MARKER_REASON and MARKER_COUNT (failures so far) from a failure marker
# without forking; a missing or malformed count reads as 0.
read_failed_marker() {
    local reason="" count=""
    { IFS= read -r reason; IFS= read -r count; } 2>/dev/null < "$1" || true
    MARKER_REASON="${reason//[[:space:]]/}"
    count="${count//[[:space:]]/}"
    # Arithmetic would evaluate anything else in the marker as an expression.
    [[ "$count" =~ ^[0-9]+$ ]] || count=0
    MARKER_COUNT=$((10#$count))
}

write_failed_marker() {
    local MARKER_FILE="$1"
    local REASON="$2"
    local MARKER_REASON MARKER_COUNT=0

    [ -f "$MARKER_FILE" ] && read_failed_marker "$MARKER_FILE"
    printf '%s\n%s\n' "$REASON" "$((MARKER_COUNT + 1))" > "$MARKER_FILE"
}

# True for a transient failure (curl timeout, API error) with retries left
# before MAX_RETRIES_SOFT. Sets MARKER_REASON and MARKER_COUNT.
failed_marker_retryable() {
    read_failed_marker "$1"
    case "$MARKER_REASON" in
        "$FAIL_CURL_TIMEOUT"|"$FAIL_API_ERROR") [ "$MARKER_COUNT" -lt "$MAX_RETRIES_SOFT" ] ;;
        *) return 1 ;;
    esac
}

# The marker stays in place during the retry, so a repeat failure counts up
# towards MAX_RETRIES_SOFT; the caller removes it once the video succeeds.
retry_failed() {
    local MARKER_FILE="$1"
    local MARKER_REASON MARKER_COUNT
    [ ! -f "$MARKER_FILE" ] && return 1

    if failed_marker_retryable "$MARKER_FILE"; then
        log "  RETRY: Transient failure (${MARKER_REASON}), attempt $((MARKER_COUNT + 1))/${MAX_RETRIES_SOFT}."
        return 0
    fi
    case "$MARKER_REASON" in
        "$FAIL_CURL_TIMEOUT"|"$FAIL_API_ERROR")
            log "  RETRY: ${MARKER_REASON} exhausted ${MAX_RETRIES_SOFT} retries. Permanent skip." ;;
    esac
    return 1
}

# ==============================================================================
# VALIDATE SRT
# ==============================================================================

validate_srt() {
    local SRT_FILE="$1"
    local VALIDATION_MODE="${2:-quality}"
    local RESULT STATUS REASON CUE_COUNT AVG_DUR AVG_CHARS
    RESULT=$(python3 - "$SRT_FILE" "$MIN_CUE_COUNT" "$MAX_AVG_CUE_DURATION" "$MAX_AVG_CUE_CHARS" "$VALIDATION_MODE" <<'PYEOF'
import os, re, sys
path, min_cues, max_avg_duration, max_avg_chars, mode = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
try:
    size = os.path.getsize(path)
    text = open(path, encoding="utf-8", errors="replace").read().replace("\r\n", "\n").replace("\r", "\n")
except OSError:
    print("fail|unreadable|0|0|0"); raise SystemExit
if size < 100:
    print(f"fail|suspiciously_small:{size}|0|0|0"); raise SystemExit
stamp = re.compile(r"^\s*(\d{2}):(\d{2}):(\d{2}),([0-9]{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2}),([0-9]{3})(?:\s+.*)?\s*$")
durations=[]
for line in text.splitlines():
    match=stamp.match(line)
    if not match: continue
    values=list(map(int, match.groups()))
    start=values[0]*3600+values[1]*60+values[2]+values[3]/1000
    end=values[4]*3600+values[5]*60+values[6]+values[7]/1000
    if end <= start:
        print(f"fail|non_positive_timestamp|{len(durations)+1}|0|0"); raise SystemExit
    durations.append(end-start)
cue_count=len(durations)
if mode == "structure":
    if cue_count == 0:
        print("fail|no_valid_timestamps|0|0|0"); raise SystemExit
    print(f"ok|ok|{cue_count}|{sum(durations)/cue_count:.1f}|0"); raise SystemExit
if cue_count < min_cues:
    print(f"fail|too_few_cues:{cue_count}:{min_cues}|{cue_count}|0|0"); raise SystemExit
blocks=[block for block in re.split(r"\n{2,}", text.strip()) if any(stamp.match(line) for line in block.splitlines())]
dialogue=[]
for block in blocks:
    lines=block.splitlines()
    timestamp_index=next((i for i,line in enumerate(lines) if stamp.match(line)), None)
    if timestamp_index is not None:
        dialogue.extend(line for line in lines[timestamp_index+1:] if line.strip())
avg_duration=sum(durations)/cue_count
avg_chars=sum(map(len, dialogue))/len(dialogue) if dialogue else 0
if avg_duration > max_avg_duration:
    print(f"fail|avg_duration:{avg_duration:.1f}:{max_avg_duration:g}|{cue_count}|{avg_duration:.1f}|{avg_chars:.0f}"); raise SystemExit
if avg_chars > max_avg_chars:
    print(f"fail|avg_text:{avg_chars:.0f}:{max_avg_chars:g}|{cue_count}|{avg_duration:.1f}|{avg_chars:.0f}"); raise SystemExit
print(f"ok|ok|{cue_count}|{avg_duration:.1f}|{avg_chars:.0f}")
PYEOF
    )
    IFS='|' read -r STATUS REASON CUE_COUNT AVG_DUR AVG_CHARS <<< "$RESULT"
    if [ "$STATUS" != "ok" ]; then
        log "  VALIDATE: FAIL — ${REASON:-unknown} (cues ${CUE_COUNT:-0}, avg duration ${AVG_DUR:-0}s, avg text ${AVG_CHARS:-0} chars)."
        return 1
    fi
    log "  VALIDATE: OK — ${CUE_COUNT} cues, avg duration ${AVG_DUR}s, avg text ${AVG_CHARS} chars."
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


format_srt_readability() {
    local SRT_FILE="$1"
    local OUT_FILE="${SRT_FILE}.readable"

    local CHANGES
    CHANGES=$(python3 - "$SRT_FILE" "$OUT_FILE" \
        "${SUBTITLE_MAX_LINE_CHARS:-42}" \
        "${SUBTITLE_MAX_LINES:-2}" \
        "${SUBTITLE_MAX_CPS:-20}" \
        "${SUBTITLE_MIN_CUE_SECONDS:-1.0}" \
        "${SUBTITLE_MAX_CUE_SECONDS:-7.0}" \
        "${SUBTITLE_MIN_GAP_SECONDS:-0.04}" <<'PYEOF'
import math
import re
import sys

srt_path, out_path = sys.argv[1], sys.argv[2]
max_line = int(float(sys.argv[3]))
max_lines = int(float(sys.argv[4]))
max_cps = float(sys.argv[5])
min_dur = float(sys.argv[6])
max_dur = float(sys.argv[7])
min_gap = float(sys.argv[8])


def ts_to_sec(ts):
    ts = ts.strip().replace(',', '.')
    h, m, s = ts.split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)


def sec_to_ts(value):
    value = max(0.0, value)
    h = int(value // 3600)
    m = int((value % 3600) // 60)
    s = value % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace('.', ',')


def parse_srt(content):
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    cues = []
    for block in re.split(r'\n{2,}', content.strip()):
        lines = block.strip().splitlines()
        ts_i = next((i for i, line in enumerate(lines) if ' --> ' in line), None)
        if ts_i is None:
            continue
        try:
            start_raw, end_raw = [part.strip() for part in lines[ts_i].split(' --> ', 1)]
            start = ts_to_sec(start_raw)
            end = ts_to_sec(end_raw.split()[0])
        except Exception:
            continue
        text_lines = [line.strip() for line in lines[ts_i + 1:] if line.strip()]
        text = ' '.join(text_lines)
        if text:
            cues.append({'start': start, 'end': max(end, start + 0.001), 'text': text, 'original_lines': text_lines})
    return cues


def wrap_text(text):
    words = text.split()
    if not words:
        return []
    lines = []
    current = ''
    for word in words:
        if len(word) > max_line:
            if current:
                lines.append(current)
                current = ''
            while len(word) > max_line:
                lines.append(word[:max_line])
                word = word[max_line:]
            current = word
        elif not current:
            current = word
        elif len(current) + 1 + len(word) <= max_line:
            current += ' ' + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def line_groups(lines):
    return [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]


with open(srt_path, 'r', encoding='utf-8', errors='replace') as fh:
    original = fh.read()

source = parse_srt(original)
expanded = []
changes = 0
for cue in source:
    original_lines = cue.get('original_lines') or [line.strip() for line in cue['text'].split('\n') if line.strip()]
    original_chars = sum(len(line) for line in original_lines)
    original_cps = original_chars / max(0.001, cue['end'] - cue['start'])
    original_overloaded = (
        len(original_lines) > max_lines
        or any(len(line) > max_line for line in original_lines)
        or original_chars > max_line * max_lines
    )
    wrapped = wrap_text(cue['text'])
    wrapped_overloaded = len(wrapped) > max_lines
    if original_cps > max_cps and original_chars <= max_line * max_lines:
        expanded.append({**cue, 'lines': original_lines})
        continue
    if not original_overloaded:
        expanded.append({**cue, 'lines': original_lines})
        continue
    if len(wrapped) != len(original_lines) or any(len(line) > max_line for line in wrapped) or wrapped_overloaded:
        changes += 1
    groups = line_groups(wrapped) or [['']]
    if len(groups) == 1:
        expanded.append({**cue, 'lines': groups[0]})
        continue

    duration = max(0.001, cue['end'] - cue['start'])
    weights = [max(1, sum(len(line) for line in group)) for group in groups]
    total = sum(weights)
    cursor = cue['start']
    for idx, group in enumerate(groups):
        if idx == len(groups) - 1:
            end = cue['end']
        else:
            end = cursor + duration * (weights[idx] / total)
            end = min(end, cue['end'] - min_gap * (len(groups) - idx - 1))
        if end <= cursor:
            end = min(cue['end'], cursor + 0.05)
        expanded.append({'start': cursor, 'end': end, 'text': ' '.join(group), 'lines': group})
        cursor = min(cue['end'], end + min_gap)

# Keep timing stable. This pass is intentionally conservative: it fixes
# visually overloaded captions without trying to retime rapid-fire dialogue.
for cue in expanded:
    if cue['end'] <= cue['start']:
        cue['end'] = cue['start'] + 0.05
        changes += 1

out_lines = []
for number, cue in enumerate(expanded, 1):
    out_lines.append(str(number))
    out_lines.append(f"{sec_to_ts(cue['start'])} --> {sec_to_ts(cue['end'])}")
    out_lines.extend(cue['lines'][:max_lines])
    out_lines.append('')

with open(out_path, 'w', encoding='utf-8') as fh:
    fh.write('\n'.join(out_lines).rstrip() + '\n')
print(changes)
PYEOF
)

    if [ $? -ne 0 ] || [ ! -s "$OUT_FILE" ]; then
        log "  READABILITY: formatter failed. Keeping original."
        rm -f "$OUT_FILE"
        echo "0"
        return
    fi
    mv "$OUT_FILE" "$SRT_FILE"
    echo "${CHANGES:-0}"
}

# ==============================================================================
# WATERMARK
# ==============================================================================

# Replace FILE with NEW_FILE, keeping FILE's mode and, where the worker is
# permitted, its ownership. BusyBox chmod/chown have no --reference.
replace_keeping_metadata() {
    local new_file="$1" file="$2" mode owner
    mode=$(stat -c %a "$file") && owner=$(stat -c %u:%g "$file") || return 1
    chown "$owner" "$new_file" 2>/dev/null || true
    chmod "$mode" "$new_file" && mv -f "$new_file" "$file"
}

# Prepend the watermark cue. WATERMARK_TMP_FILE names the temp file while it
# exists so callers can remove it from an EXIT trap.
prepend_watermark() {
    local SUB_FILE="$1"
    WATERMARK_TMP_FILE="${SUB_FILE}.tmp"
    if { printf '0\n00:00:00,000 --> 00:00:05,000\n%s\n\n' "${WATERMARK_TEXT}" && cat "$SUB_FILE"; } > "$WATERMARK_TMP_FILE" \
        && replace_keeping_metadata "$WATERMARK_TMP_FILE" "$SUB_FILE"; then
        WATERMARK_TMP_FILE=""
        return 0
    fi
    rm -f "$WATERMARK_TMP_FILE"
    WATERMARK_TMP_FILE=""
    log "  WATERMARK: Could not watermark ${SUB_FILE##*/}. Keeping original."
    return 1
}

apply_watermark() {
    local SUB_FILE="$1"
    if [ "$ENABLE_WATERMARK" = true ] && [ -n "$WATERMARK_TEXT" ]; then
        # Not `head | grep -q`: under pipefail grep's early exit fails head
        # with SIGPIPE. The quoted pattern matches literally.
        case "$(head -n 15 "$SUB_FILE")" in
            *"$WATERMARK_SEARCH"*) ;;
            *) prepend_watermark "$SUB_FILE" || return 1 ;;
        esac
    fi
}

# ==============================================================================
# ERROR RECOVERY & RESILIENCE
# ==============================================================================

health_check_api() {
    local API_URL="${1:-$WHISPER_API_URL}"
    local MAX_WAIT="${2:-120}"

    local STATUS
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 30 "$API_URL" 2>/dev/null)

    if [ "$STATUS" -eq 200 ] || [ "$STATUS" -eq 405 ] || [ "$STATUS" -eq 422 ]; then
        return 0
    fi

    log "HEALTH CHECK: API unresponsive (HTTP ${STATUS}). Waiting up to ${MAX_WAIT}s..."

    local ELAPSED=0
    while [ $ELAPSED -lt "$MAX_WAIT" ]; do
        sleep 10
        ELAPSED=$((ELAPSED + 10))
        STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 30 "$API_URL" 2>/dev/null)
        if [ "$STATUS" -eq 200 ] || [ "$STATUS" -eq 405 ] || [ "$STATUS" -eq 422 ]; then
            log "HEALTH CHECK: API recovered after ${ELAPSED}s."
            return 0
        fi
    done

    log "HEALTH CHECK: FATAL — API unrecoverable after ${MAX_WAIT}s."
    return 1
}

# Move FILE to QUARANTINE_DIR/CATEGORY, keeping its path relative to ROOT.
quarantine_media_file() {
    local file="$1" root="$2" category="$3" target
    target="${QUARANTINE_DIR}/${category}/${file#"${root%/}"/}"
    [ -e "$target" ] && target="${target}.$(date +%s)"
    if mkdir -p "$(dirname "$target")" && mv -f "$file" "$target"; then
        log "  QUARANTINE: ${file} -> ${target}"
        return 0
    fi
    log "ERROR: Could not quarantine ${file}."
    return 1
}

# transcribe.sh writes Title.ai.srt and renames it to Title.ai-<lang>.srt only
# once it is validated, watermarked and encoding-checked. Only that finished
# form is promoted (to Title.<lang>.srt, with the final subtitle permissions);
# every other leftover of an interrupted run is quarantined.
resume_partial() {
    local RECOVERED=0
    local QUARANTINED=0
    local DIR TEMP_FILE DIR_PATH BASENAME FINAL_PATH

    for DIR in "$@"; do
        while IFS= read -r -d '' TEMP_FILE; do
            DIR_PATH="${TEMP_FILE%/*}"
            BASENAME="${TEMP_FILE##*/}"

            if [[ "$BASENAME" =~ ^(.+)\.ai-([A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?)\.srt$ ]]; then
                FINAL_PATH="${DIR_PATH}/${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.srt"
                log "RESUME: Found finished transcript: ${BASENAME}"
                if [ -e "$FINAL_PATH" ]; then
                    log "  RESUME: ${FINAL_PATH##*/} already exists."
                elif ! validate_srt "$TEMP_FILE"; then
                    log "  RESUME: Failed validation."
                elif finalize_subtitle_permissions "$TEMP_FILE" && mv "$TEMP_FILE" "$FINAL_PATH"; then
                    log "  RESUME: Promoted to ${FINAL_PATH##*/}"
                    RECOVERED=$((RECOVERED + 1))
                    continue
                else
                    log "  RESUME: Could not finalize ${BASENAME}; keeping it for the next run."
                    continue
                fi
            elif [[ "$BASENAME" =~ \.(ai|translate_tmp)\.srt(\.(tmp|cleaned|readable|normalized|musicclean|enc_tmp))?$ ]]; then
                log "RESUME: Found interrupted transcript: ${BASENAME}"
            else
                continue
            fi
            quarantine_media_file "$TEMP_FILE" "$DIR" partials && QUARANTINED=$((QUARANTINED + 1))
        done < <(find "$DIR" -type f \( -name "*.ai.srt*" -o -name "*.ai-*.srt" -o -name "*.translate_tmp.srt*" \) -print0 2>/dev/null)
    done

    log "RESUME: Recovered ${RECOVERED}, quarantined ${QUARANTINED} partial file(s)."
    echo "$RECOVERED"
}

quarantine_video() {
    local VIDEO_FILE="$1"
    local REASON="${2:-unknown}"

    mkdir -p "$QUARANTINE_DIR"

    local QUARANTINE_LOG="${QUARANTINE_DIR}/quarantine.log"
    local BASENAME="${VIDEO_FILE##*/}"
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
    local CHECK_LANGUAGE="${3:-en}"
    local OFFSET="${4:-300}"
    local SAMPLE_DURATION=30
    local THRESHOLD="${CONFIDENCE_THRESHOLD:-30}"

    local API_URL="${WHISPER_API_URL}?task=transcribe&output=srt"
    if [ -n "$CHECK_LANGUAGE" ] && [ "$CHECK_LANGUAGE" != "auto" ]; then
        API_URL="${API_URL}&language=${CHECK_LANGUAGE}"
    fi

    run_confidence_sample() {
        local SAMPLE_OFFSET="$1"
        local TEMP_SAMPLE="/tmp/confidence_sample_$$_${SAMPLE_OFFSET}.wav"
        local TEMP_SRT="/tmp/confidence_check_$$_${SAMPLE_OFFSET}.srt"

        ffmpeg -nostdin -ss "$SAMPLE_OFFSET" -i "$VIDEO_FILE" \
            -map 0:a:0 -t "$SAMPLE_DURATION" -vn -acodec pcm_s16le -ar 16000 -ac 1 \
            -y "$TEMP_SAMPLE" -loglevel quiet 2>/dev/null

        if [ ! -s "$TEMP_SAMPLE" ]; then
            rm -f "$TEMP_SAMPLE" "$TEMP_SRT"
            echo "50"
            return
        fi

        curl -s --fail --connect-timeout 30 --max-time 300 \
            -X POST -F "audio_file=@${TEMP_SAMPLE}" \
            "$API_URL" -o "$TEMP_SRT" 2>/dev/null

        rm -f "$TEMP_SAMPLE"

        if [ ! -s "$TEMP_SRT" ] || ! grep -qF -- '-->' "$TEMP_SRT"; then
            rm -f "$TEMP_SRT"
            echo "50"
            return
        fi

        local SAMPLE_SCORE
        SAMPLE_SCORE=$(python3 - "$SRT_FILE" "$TEMP_SRT" "$SAMPLE_OFFSET" "$SAMPLE_DURATION" <<'PYEOF'
import sys, re

CJK_RE = re.compile(r'[\u3040-\u30ff\u3400-\u9fff]')


def normalize_text(text):
    text = re.sub(r'\{\\an8[^}]*\}', ' ', text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = text.lower()
    text = re.sub(r'[^\w\s\u3040-\u30ff\u3400-\u9fff]', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def text_units(text):
    text = normalize_text(text)
    compact = re.sub(r'\s+', '', text)
    cjk = ''.join(ch for ch in compact if CJK_RE.match(ch))
    if len(cjk) >= 8:
        units = set(cjk)
        for n in (2, 3):
            units.update(cjk[i:i+n] for i in range(0, max(0, len(cjk) - n + 1)))
        return units
    return set(text.split())


def extract_text(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()
    texts = []
    for block in re.split(r'\n{2,}', content.strip()):
        lines = block.strip().splitlines()
        ts_i = next((i for i, l in enumerate(lines) if ' --> ' in l), None)
        if ts_i is None:
            continue
        text = ' '.join(lines[ts_i+1:]).strip()
        if text:
            texts.append(text)
    return text_units(' '.join(texts))


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
        if ts_i is None:
            continue
        try:
            parts = lines[ts_i].split(' --> ')
            cue_start = ts_to_sec(parts[0])
        except Exception:
            continue
        if start_sec <= cue_start <= end_sec:
            text = ' '.join(lines[ts_i+1:]).strip()
            if text:
                texts.append(text)
    return text_units(' '.join(texts))


offset = float(sys.argv[3])
duration = float(sys.argv[4])

original_units = extract_text_in_range(sys.argv[1], offset, duration)
verify_units = extract_text(sys.argv[2])

if not original_units or not verify_units:
    print(50)
    sys.exit(0)

union = original_units | verify_units
intersection = original_units & verify_units
score = int((len(intersection) / len(union)) * 100) if union else 50
print(score)
PYEOF
        )

        rm -f "$TEMP_SRT"
        echo "${SAMPLE_SCORE:-50}"
    }

    local BEST_SCORE
    BEST_SCORE=$(run_confidence_sample "$OFFSET")
    BEST_SCORE="${BEST_SCORE:-50}"

    if [ "$BEST_SCORE" -lt "$THRESHOLD" ] 2>/dev/null; then
        local SAMPLE_OFFSET SAMPLE_SCORE
        for SAMPLE_OFFSET in 60 180 900 1200; do
            [ "$SAMPLE_OFFSET" = "$OFFSET" ] && continue
            SAMPLE_SCORE=$(run_confidence_sample "$SAMPLE_OFFSET")
            SAMPLE_SCORE="${SAMPLE_SCORE:-50}"
            if [ "$SAMPLE_SCORE" -gt "$BEST_SCORE" ] 2>/dev/null; then
                BEST_SCORE="$SAMPLE_SCORE"
            fi
            [ "$BEST_SCORE" -ge "$THRESHOLD" ] 2>/dev/null && break
        done
    fi

    log "  CONFIDENCE: Score ${BEST_SCORE}/100"
    echo "$BEST_SCORE"
}

# ==============================================================================
# LIBRARY MANAGEMENT
# ==============================================================================

# Convert a subtitle to UTF-8 when needed (see srt_encoding.py: lossless only,
# original backed up, atomic replace). Returns 1 when the file was converted.
verify_encoding() {
    local SRT_FILE="$1"
    local RESULT STATUS DETAIL

    if [ ! -f "$SRT_FILE" ]; then return 0; fi

    RESULT=$(python3 "$(script_helper srt_encoding.py)" repair \
        --backup-dir "${QUARANTINE_DIR}/encoding-backups" "$SRT_FILE") || true
    IFS='|' read -r STATUS DETAIL <<< "$RESULT"
    case "$STATUS" in
        ok) return 0 ;;
        converted)
            log "  ENCODING: ${SRT_FILE##*/} converted to UTF-8 ${DETAIL}."
            return 1
            ;;
        *)
            log "  ENCODING: ${SRT_FILE##*/} ${DETAIL:-could not be checked}. Keeping original."
            return 0
            ;;
    esac
}

# Run pgs_cleanup.py, which decides whether an SRT exactly replaces an image
# subtitle (same title, language and forced/SDH kind).
pgs_cleanup_helper() {
    local args=()
    [ "${PGS_CLEANUP_ALLOW_UNKNOWN:-0}" = "1" ] && args+=( --allow-unknown )
    python3 "$(script_helper pgs_cleanup.py)" "${args[@]}" "$@"
}

# Moves replaced image subtitles to QUARANTINE_DIR/pgs. PGS_DELETED_COUNT
# (name kept for callers) is the number of files moved.
cleanup_pgs() {
    local OUTPUT LINE RC=0
    PGS_DELETED_COUNT=0

    OUTPUT=$(pgs_cleanup_helper --quarantine-dir "${QUARANTINE_DIR}/pgs" "$@" 2>&1) || RC=$?
    while IFS= read -r LINE; do
        case "$LINE" in
            TOTAL:*) PGS_DELETED_COUNT="${LINE#TOTAL:}" ;;
            ?*) log "$LINE" ;;
        esac
    done <<< "$OUTPUT"

    log "PGS CLEANUP: Quarantined ${PGS_DELETED_COUNT} image-based subtitle file(s) in ${QUARANTINE_DIR}/pgs."
    return "$RC"
}

# ---------------------------------------------------------------------------
# deduplicate_subs()
#
# Detects cases where multiple SRT files exist for the same title, language
# and track kind (e.g., one downloaded + one from Whisper). Keeps the one with
# more cues and moves the inferior one to DATA_DIR/quarantine/dedup. Forced
# and SDH tracks are never treated as duplicates of a regular track.
#
# Arguments: $1 = directory to scan (recursive)
# ---------------------------------------------------------------------------
deduplicate_subs() {
    local SCAN_DIR="$1"

    python3 "$(script_helper dedup_subs.py)" "$SCAN_DIR" 2>&1 | while IFS= read -r line; do log "$line"; done
    local rc=${PIPESTATUS[0]}

    log "DEDUP: Scan of ${SCAN_DIR} complete."
    return "$rc"
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
            local VDIR="${VIDEO%/*}"
            local VBASE="${VIDEO##*/}"
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
            local SDIR="${SRT%/*}"
            local SBASE="${SRT##*/}"
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
        while IFS='|' read -r -d '' REASON CUES SIZE SRT; do
            echo "  [INVALID reason:${REASON} cues:${CUES} size:${SIZE}b] ${SRT}"
            INVALID_SRTS=$((INVALID_SRTS + 1))
        done < <(find "${DIRS[@]}" -type f -iname "*.srt" -print0 2>/dev/null | python3 -c '
import os, re, sys
stamp = re.compile(rb"^\s*(\d{2}):(\d{2}):(\d{2}),([0-9]{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2}),([0-9]{3})(?:\s+.*)?\s*$")
for raw_path in sys.stdin.buffer.read().split(b"\0"):
    if not raw_path:
        continue
    path = os.fsdecode(raw_path)
    reason = None
    cues = 0
    try:
        size = os.path.getsize(path)
        data = open(path, "rb").read().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        for line in data.splitlines():
            match = stamp.match(line)
            if not match:
                continue
            values = list(map(int, match.groups()))
            start = values[0]*3600 + values[1]*60 + values[2] + values[3]/1000
            end = values[4]*3600 + values[5]*60 + values[6] + values[7]/1000
            cues += 1
            if end <= start:
                reason = "non_positive_timestamp"
                break
        if cues == 0 and reason is None:
            reason = "no_valid_timestamps"
    except OSError:
        size, reason = 0, "unreadable"
    if reason:
        record = f"{reason}|{cues}|{size}|{path}".encode("utf-8", "replace") + b"\0"
        sys.stdout.buffer.write(record)
')
        echo ""
        echo "  Total: ${INVALID_SRTS} invalid SRT files"
        echo ""

        echo "## PGS/IMAGE SUBS WITH TEXT REPLACEMENTS AVAILABLE"
        echo ""
        local PGS
        while IFS= read -r -d '' PGS; do
            echo "  [CLEANABLE] ${PGS}"
            PGS_CLEANABLE=$((PGS_CLEANABLE + 1))
        done < <(pgs_cleanup_helper --list "${DIRS[@]}" 2>/dev/null)
        echo ""
        echo "  Total: ${PGS_CLEANABLE} PGS files replaceable"
        echo ""

        echo "## ENCODING ISSUES (sampled)"
        echo ""
        local ENC_CHECKED=0 ENC
        while IFS='|' read -r -d '' ENC SRT; do
            if [ "$ENC" = "checked" ]; then
                ENC_CHECKED="$SRT"
                continue
            fi
            echo "  [${ENC}] ${SRT}"
            ENCODING_ISSUES=$((ENCODING_ISSUES + 1))
        done < <(find "${DIRS[@]}" -type f -iname "*.srt" -print0 2>/dev/null \
            | python3 "$(script_helper srt_encoding.py)" check --limit 200)
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
            LIFETIME_SCANNED=0 LIFETIME_ENGLISH_PROCESSED=0 LIFETIME_BILINGUAL_PROCESSED=0
            LIFETIME_FOREIGN_PROCESSED=0 LIFETIME_SKIPPED_EXISTING=0 LIFETIME_SKIPPED_FAILED=0
            LIFETIME_SKIPPED_SIZE=0 LIFETIME_HALLUCINATIONS_CLEANED=0 LIFETIME_PROCESSING_SECONDS=0
            load_numeric_stats "$TRANS_STATS"
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
            LIFETIME_SCANNED=0 LIFETIME_PROCESSED=0 LIFETIME_SKIPPED_EXISTING=0
            LIFETIME_SKIPPED_FAILED=0 LIFETIME_PROCESSING_SECONDS=0
            load_numeric_stats "$TRANSL_STATS"
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
            local VD="${V%/*}"
            local VN="${V##*/}"
            VN="${VN%.*}"
            shopt -s nullglob nocaseglob
            local V_SUBS=( "${VD}/${VN}".*.srt )
            shopt -u nullglob nocaseglob
            if [ ${#V_SUBS[@]} -gt 0 ]; then
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
