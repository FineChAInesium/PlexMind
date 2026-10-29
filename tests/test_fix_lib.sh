#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT
# `! cmd` is exempt from `set -e`, so negative assertions must exit explicitly.
fail() { echo "FAIL: $*" >&2; exit 1; }

mkdir -p "$TEMP_ROOT/data"
DATA_DIR="$TEMP_ROOT/data"
LOG_FILE="$TEMP_ROOT/lib.log"
source "$ROOT_DIR/scripts/lib.sh"

# --- A held lock exits 75 before any work; a free one is taken ---
if command -v flock >/dev/null 2>&1; then
    LOCK="$TEMP_ROOT/data/plexmind_media_mutation.lock"
    try_lock() {
        LOG_FILE="$TEMP_ROOT/lock.log" bash -c 'source "$1"; acquire_lock "$2"; echo acquired' _ "$ROOT_DIR/scripts/lib.sh" "$LOCK"
    }
    exec {held_fd}>"$LOCK"
    flock -n "$held_fd" || fail "could not take the test lock"
    rc=0
    try_lock > "$TEMP_ROOT/lock.out" 2>&1 || rc=$?
    [ "$rc" -eq 75 ] || fail "a held lock exited $rc instead of 75"
    if grep -q acquired "$TEMP_ROOT/lock.out"; then fail "acquire_lock returned although the lock is held"; fi
    grep -qF "Another job already holds ${LOCK}" "$TEMP_ROOT/lock.log" || fail "lock contention was not logged"
    exec {held_fd}>&-
    rc=0
    try_lock > "$TEMP_ROOT/lock.out" 2>&1 || rc=$?
    [ "$rc" -eq 0 ] || fail "a free lock exited $rc"
    grep -q acquired "$TEMP_ROOT/lock.out" || fail "a free lock was not acquired"
    [ -s "$TEMP_ROOT/data/plexmind_media_mutation.pid" ] || fail "lock holder pid was not written"
fi

# --- Failure markers count every attempt until the retry budget is spent ---
MAX_RETRIES_SOFT=3
MARKER="$TEMP_ROOT/Film.ai.failed"
write_failed_marker "$MARKER" "$FAIL_API_ERROR"
for attempt in 2 3; do
    retry_failed "$MARKER" > "$TEMP_ROOT/retry.log" || fail "transient failure was not retried for attempt $attempt"
    [ -f "$MARKER" ] || fail "retry removed the failure marker before attempt $attempt"
    grep -qF "attempt ${attempt}/3" "$TEMP_ROOT/retry.log" || fail "retry did not log attempt ${attempt}/3"
    write_failed_marker "$MARKER" "$FAIL_CURL_TIMEOUT"
    [ "$(sed -n 2p "$MARKER")" = "$attempt" ] || fail "failure count is $(sed -n 2p "$MARKER") after attempt $attempt"
done
if retry_failed "$MARKER" > "$TEMP_ROOT/retry.log"; then fail "exhausted transient failure was retried again"; fi
grep -qF 'exhausted 3 retries' "$TEMP_ROOT/retry.log" || fail "exhausted retries were not logged"
[ "$(head -n 1 "$MARKER")" = "$FAIL_CURL_TIMEOUT" ] || fail "marker lost the latest failure reason"

write_failed_marker "$TEMP_ROOT/Bad.ai.failed" "$FAIL_VALIDATION"
if retry_failed "$TEMP_ROOT/Bad.ai.failed" > /dev/null; then fail "a validation failure was retried"; fi

printf '%s\n%s\n' "$FAIL_API_ERROR" 'x[$(touch${IFS}'"$TEMP_ROOT"'/pwned)]' > "$TEMP_ROOT/Odd.ai.failed"
write_failed_marker "$TEMP_ROOT/Odd.ai.failed" "$FAIL_API_ERROR"
[ ! -e "$TEMP_ROOT/pwned" ] || fail "marker contents were evaluated as shell code"
[ "$(sed -n 2p "$TEMP_ROOT/Odd.ai.failed")" = 1 ] || fail "malformed failure count was not reset"
printf '%s\n08\n' "$FAIL_API_ERROR" > "$TEMP_ROOT/Padded.ai.failed"
write_failed_marker "$TEMP_ROOT/Padded.ai.failed" "$FAIL_API_ERROR"
[ "$(sed -n 2p "$TEMP_ROOT/Padded.ai.failed")" = 9 ] || fail "zero-padded failure count was misread"

# --- Media language tags become codes Whisper accepts ---
for pair in swe:sv SWE:sv sv-SE:sv hin:hi cze:cs ces:cs nob:no fil:tl gre:el fre:fr yue:zh haw:haw en:en pt-BR:pt; do
    tag="${pair%%:*}"
    [ "$(normalize_lang_code "$tag")" = "${pair#*:}" ] || fail "$tag normalized to '$(normalize_lang_code "$tag")' instead of ${pair#*:}"
done
for tag in xyz tlh mis zxx "../../evil" "engl" 'en$(id)'; do
    [ -z "$(normalize_lang_code "$tag")" ] || fail "tag Whisper would reject was passed through: $tag"
done

# --- Sidecar start/stop log Docker's reason and never hide a failed stop ---
CALLS_FILE="$TEMP_ROOT/calls"
curl() {
    printf '%s\n' "$*" >> "$CALLS_FILE"
    local body="" code=500
    case "$*" in
        */start*) body="${FAKE_START_BODY:-}"; code="${FAKE_START_STATUS:-204}" ;;
        */stop*) body="${FAKE_STOP_BODY:-}"; code="${FAKE_STOP_STATUS:-204}" ;;
    esac
    # Like `curl -w '\n%{http_code}'`: the body, a newline, then the status.
    printf '%s\n%s' "$body" "$code"
    [ "$code" != 000 ] || return 7
}
DOCKER_BROKER_URL="http://docker-broker:9020"
PLEXMIND_BROKER_TOKEN="test-broker-token"
SIDECAR_OWNED_CONTAINERS=""
if command -v jq >/dev/null 2>&1; then HAVE_JQ=1; else HAVE_JQ=0; fi
no_misleading_stop_line() {
    if grep -qF 'no job-owned sidecar' "$LOG_FILE"; then fail "$1 was logged as nothing to stop"; fi
}

: > "$LOG_FILE"
FAKE_START_STATUS=500
FAKE_START_BODY='{"message":"could not select device driver \"nvidia\""}'
if start_docker_container "Whisper" "gpu-sidecar" > /dev/null; then fail "a failed sidecar start was reported as started"; fi
grep -qF 'Docker start request for gpu-sidecar returned HTTP 500' "$LOG_FILE" || fail "start failure status was not logged"
if [ "$HAVE_JQ" = 1 ]; then
    grep -qF 'HTTP 500: could not select device driver "nvidia".' "$LOG_FILE" || fail "Docker's start error was not logged"
fi
grep -qF -- '-w \n%{http_code}' "$CALLS_FILE" || fail "broker response body was not captured with the status"
case " $SIDECAR_OWNED_CONTAINERS " in *" gpu-sidecar "*) fail "a failed start marked the sidecar as job-owned" ;; esac

FAKE_START_STATUS=204
FAKE_START_BODY=""
start_docker_container "Whisper" "gpu-sidecar" > /dev/null || fail "sidecar start failed"

: > "$LOG_FILE"
FAKE_STOP_STATUS=502
FAKE_STOP_BODY='{"detail":"timed out"}'
stop_docker_container "Whisper" "gpu-sidecar" > /dev/null || fail "a failed stop failed the caller"
grep -qF 'ERROR: Whisper: could not stop job-owned container gpu-sidecar (HTTP 502' "$LOG_FILE" || fail "failed stop was not reported"
if [ "$HAVE_JQ" = 1 ]; then
    grep -qF '(HTTP 502: timed out)' "$LOG_FILE" || fail "broker stop error was not logged"
fi
no_misleading_stop_line "a failed stop"

: > "$LOG_FILE"
FAKE_STOP_STATUS=000
FAKE_STOP_BODY=""
stop_docker_container "Whisper" "gpu-sidecar" > /dev/null
grep -qF '(HTTP 000: Docker broker unreachable)' "$LOG_FILE" || fail "unreachable broker was not reported"
no_misleading_stop_line "an unreachable broker"

: > "$LOG_FILE"
FAKE_STOP_STATUS=404
FAKE_STOP_BODY='{"message":"No such container: gpu-sidecar"}'
stop_docker_container "Whisper" "gpu-sidecar" > /dev/null
grep -qF 'job-owned container gpu-sidecar no longer exists' "$LOG_FILE" || fail "vanished sidecar was not reported"
no_misleading_stop_line "a vanished sidecar"

: > "$LOG_FILE"
FAKE_STOP_STATUS=204
FAKE_STOP_BODY=""
stop_docker_container "Whisper" "gpu-sidecar" > /dev/null
grep -qF 'Whisper: stopped container gpu-sidecar.' "$LOG_FILE" || fail "successful stop was not logged"

echo "lib fix tests passed"
