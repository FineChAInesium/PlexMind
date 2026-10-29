#!/bin/sh
set -eu

case "${1:-}" in
  transcribe|transcription) pid_file=/tmp/transcription_backfill.pid ;;
  translate|translation) pid_file=/tmp/translation_backfill.pid ;;
  *) echo "Usage: $0 {transcribe|translate}" >&2; exit 2 ;;
esac

if [ ! -f "$pid_file" ]; then
  echo "No running job found for $1"
  exit 0
fi

pid=$(cat "$pid_file" 2>/dev/null || true)
case "$pid" in
  ''|0*|*[!0-9]*) echo "Invalid PID file: $pid_file" >&2; rm -f "$pid_file"; exit 1 ;;
esac

if kill -0 "$pid" 2>/dev/null; then
  # Signal the job's process group (control API and docker exec jobs lead one)
  # so the running curl/ffmpeg child, which holds the job's locks, exits too.
  # "kill -TERM -1" would signal every process, so PID 1 gets a plain kill.
  if [ "$pid" -eq 1 ] || ! kill -TERM "-$pid" 2>/dev/null; then
    kill -TERM "$pid"
  fi
  echo "Stop requested for $1 job (pid $pid)"
else
  echo "Stale PID file removed for $1"
  rm -f "$pid_file"
fi
