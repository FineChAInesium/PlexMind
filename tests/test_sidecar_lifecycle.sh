#!/bin/bash
set -eu

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
CALLS_FILE="$TEMP_ROOT/calls"

cleanup_test() {
    rm -rf "$TEMP_ROOT"
}
trap cleanup_test EXIT

curl() {
    printf '%s\n' "$*" >> "$CALLS_FILE"
    case "$*" in
        */start*) printf '%s' "${FAKE_START_STATUS:-204}" ;;
        */stop*) printf '%s' "204" ;;
        *) printf '%s' "500" ;;
    esac
}

DOCKER_BROKER_URL="http://docker-broker:9020"
PLEXMIND_BROKER_TOKEN="test-broker-token"
LOG_FILE=""
source "$ROOT_DIR/scripts/lib.sh"

start_docker_container "Test" "dedicated-sidecar"
case " $SIDECAR_OWNED_CONTAINERS " in *" dedicated-sidecar "*) ;; *) exit 1 ;; esac
stop_docker_container "Test" "dedicated-sidecar"
grep -q '/dedicated-sidecar/start' "$CALLS_FILE"
grep -q '/dedicated-sidecar/stop' "$CALLS_FILE"

: > "$CALLS_FILE"
SIDECAR_OWNED_CONTAINERS=""
FAKE_START_STATUS=304
start_docker_container "Shared" "shared-sidecar"
stop_docker_container "Shared" "shared-sidecar"
grep -q '/shared-sidecar/start' "$CALLS_FILE"
if grep -q '/shared-sidecar/stop' "$CALLS_FILE"; then
    echo "an already-running shared sidecar was stopped" >&2
    exit 1
fi

echo "sidecar lifecycle tests passed"
