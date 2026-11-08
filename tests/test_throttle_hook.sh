#!/bin/bash
# throttle_if_needed: off without limits, rate-limited, and its messages logged.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

mkdir -p "$TEMP_ROOT/data" "$TEMP_ROOT/helpers"
DATA_DIR="$TEMP_ROOT/data"
LOG_FILE="$TEMP_ROOT/lib.log"
source "$ROOT_DIR/scripts/lib.sh"

# A stand-in for resource_guard.py that records each call.
CALLS="$TEMP_ROOT/calls"
export CALLS
cat > "$TEMP_ROOT/helpers/resource_guard.py" <<'EOF'
import os
open(os.environ["CALLS"], "a").write("call\n")
print("THROTTLE: holding: GPU 85C (limit 80C)", flush=True)
EOF
SCRIPT_DIR="$TEMP_ROOT/helpers"
calls() { [ -f "$CALLS" ] && wc -l < "$CALLS" || echo 0; }

throttle_if_needed
[ "$(calls)" -eq 0 ] || fail "the guard ran with no limits set"

THROTTLE_GPU_TEMP=80
THROTTLE_CHECK_SECONDS=300
throttle_if_needed
[ "$(calls)" -eq 1 ] || fail "the guard did not run with a limit set"
grep -qF 'THROTTLE: holding: GPU 85C (limit 80C)' "$LOG_FILE" || fail "the guard's message was not logged"

throttle_if_needed
[ "$(calls)" -eq 1 ] || fail "the guard ran again inside THROTTLE_CHECK_SECONDS"

THROTTLE_NEXT_CHECK=0
throttle_if_needed
[ "$(calls)" -eq 2 ] || fail "the guard did not run once the interval had passed"

echo "throttle hook tests passed"
