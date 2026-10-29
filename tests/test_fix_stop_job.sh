#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
JOB_PID_FILE="$TEMP_ROOT/translation_backfill.pid"
CHILD_PID_FILE="$TEMP_ROOT/child.pid"
export JOB_PID_FILE CHILD_PID_FILE
job_pid="" child_pid=""

cleanup_test() {
    local pid
    # Never leave a fake job or its sleep running after a failed assertion.
    for pid in "$job_pid" "$child_pid"; do
        [ -z "$pid" ] || kill -KILL "$pid" 2>/dev/null || true
    done
    rm -rf "$TEMP_ROOT"
}
trap cleanup_test EXIT
# `! cmd` is exempt from `set -e`, so negative assertions must exit explicitly.
fail() { echo "FAIL: $*" >&2; exit 1; }

command -v setsid >/dev/null || { echo "setsid not found; skipping stop-job tests"; exit 0; }

# stop-job.sh hard-codes /tmp; run a copy pointed at the temp dir so a real
# job's PID file is never touched.
sed "s#/tmp/#$TEMP_ROOT/#g" "$ROOT_DIR/scripts/stop-job.sh" > "$TEMP_ROOT/stop-job.sh"

# A bash job with an EXIT trap waiting on a foreground child, like translate.sh
# waiting on curl to llama.cpp.
cat > "$TEMP_ROOT/job.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
trap 'rm -f "$JOB_PID_FILE"' EXIT
echo "$$" > "$JOB_PID_FILE"
sh -c 'echo "$$" > "$1"; exec sleep 30' sh "$CHILD_PID_FILE"
EOF

# A zombie still answers kill -0, so read the process state from /proc.
alive() {
    local state
    state=$(cut -d' ' -f3 "/proc/$1/stat" 2>/dev/null) || return 1
    [ "$state" != Z ]
}
gone() { ! alive "$1"; }
pgrp() { cut -d' ' -f5 "/proc/$1/stat"; }

wait_until() {
    local tries=0
    until "$@"; do
        tries=$((tries + 1))
        [ "$tries" -lt 50 ] || return 1
        sleep 0.1
    done
}

start_job() {
    rm -f "$JOB_PID_FILE" "$CHILD_PID_FILE"
    "$@" bash "$TEMP_ROOT/job.sh" </dev/null >/dev/null 2>&1 &
    job_bg=$!
    wait_until [ -s "$CHILD_PID_FILE" ] || fail "fake job did not start"
    job_pid=$(cat "$JOB_PID_FILE")
    child_pid=$(cat "$CHILD_PID_FILE")
}

# setsid gives stop-job.sh its own session, as docker exec does.
run_stop() {
    rc=0
    out=$(setsid sh "$TEMP_ROOT/stop-job.sh" translate 2>&1) || rc=$?
}

# Control API (start_new_session) and docker exec jobs lead their own group.
start_job setsid
[ "$(pgrp "$job_pid")" = "$job_pid" ] || fail "setsid did not give the fake job its own process group"
run_stop
[ "$rc" -eq 0 ] || fail "stop-job.sh exited $rc: $out"
wait_until gone "$child_pid" || fail "stop-job.sh left the job's running child (and its locks) behind"
child_pid=""
wait_until gone "$job_pid" || fail "stop-job.sh did not stop the job"
wait "$job_bg" || true
job_pid=""
[ ! -e "$JOB_PID_FILE" ] || fail "the job's EXIT trap did not run"

# A job started under a wrapper shell has no group of its own; the group
# signal fails and stop-job.sh must still signal the job's PID.
start_job
[ "$(pgrp "$job_pid")" != "$job_pid" ] || fail "fake job unexpectedly leads a process group"
run_stop
[ "$rc" -eq 0 ] || fail "stop-job.sh exited $rc for a job without its own group: $out"
wait_until gone "$job_pid" || fail "stop-job.sh did not fall back to signalling the job's PID"
wait "$job_bg" || true
job_pid=""

# PID 0 would make kill signal stop-job.sh's own process group.
echo 0 > "$JOB_PID_FILE"
run_stop
[ "$rc" -eq 1 ] || fail "stop-job.sh accepted PID 0 (exit $rc)"
[ ! -e "$JOB_PID_FILE" ] || fail "stop-job.sh kept an invalid PID file"

echo "stop-job tests passed"
