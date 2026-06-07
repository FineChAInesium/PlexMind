#!/usr/bin/env bash
# Verify the live PlexMind API/container is aligned with the llama.cpp deployment.
set -u

API_BASE="${API_BASE:-http://127.0.0.1:8000}"
RECOMMENDATION_USER="${RECOMMENDATION_USER:-admin}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"
FAILURES=0

log() { printf '%s\n' "$*"; }
pass() { log "ok: $*"; }
fail() { log "FAIL: $*"; FAILURES=$((FAILURES + 1)); }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { fail "missing command: $1"; return 1; }
}

json_get() {
  python3 - "$1" "$2" <<'PY'
import json, sys
path = sys.argv[1].split('.')
data = json.loads(sys.argv[2])
for part in path:
    if isinstance(data, dict):
        data = data.get(part)
    else:
        data = None
        break
if isinstance(data, bool):
    print(str(data).lower())
elif data is not None:
    print(data)
PY
}

http_get() {
  curl -sS --max-time "${HTTP_TIMEOUT:-20}" "$@"
}

api_key() {
  if [ -n "${PLEXMIND_API_KEY:-}" ]; then
    printf '%s' "$PLEXMIND_API_KEY"
    return 0
  fi
  if [ -f "$ENV_FILE" ]; then
    awk -F= '/^PLEXMIND_API_KEY=/{print substr($0, index($0, "=") + 1); exit}' "$ENV_FILE"
  fi
}

need_cmd curl
need_cmd python3

log "PlexMind live verification"
log "API: ${API_BASE}"
if command -v git >/dev/null 2>&1 && git -C "$ROOT_DIR" rev-parse --short HEAD >/dev/null 2>&1; then
  log "local_commit: $(git -C "$ROOT_DIR" rev-parse --short HEAD)"
fi

health="$(http_get "${API_BASE}/health" 2>/dev/null || true)"
if [ -z "$health" ]; then
  fail "/health returned no response"
else
  status="$(json_get status "$health" 2>/dev/null || true)"
  llm="$(json_get llm "$health" 2>/dev/null || true)"
  llm_ready="$(json_get llm_ready "$health" 2>/dev/null || true)"
  [ "$status" = "ok" ] && pass "/health status ok" || fail "/health status is ${status:-missing}"
  [ "$llm_ready" = "true" ] && pass "LLM ready (${llm:-unknown})" || fail "LLM not ready (${llm:-unknown})"
  case "$llm" in
    qwen3-4b-q4_k_m) pass "expected llama.cpp model label" ;;
    *) fail "unexpected LLM label: ${llm:-missing}" ;;
  esac
fi

scheduler="$(http_get "${API_BASE}/api/scheduler/status" 2>/dev/null || true)"
if [ -z "$scheduler" ]; then
  fail "/api/scheduler/status returned no response"
else
  vendor="$(json_get gpu_vendor "$scheduler" 2>/dev/null || true)"
  util="$(json_get gpu_utilization_pct "$scheduler" 2>/dev/null || true)"
  [ -n "$vendor" ] && pass "GPU detected (${vendor}, ${util:-unknown}% utilization)" || fail "GPU vendor missing from scheduler status"
fi

key="$(api_key)"
if [ -n "$key" ]; then
  translate_status="$(curl -sS --max-time 20 -H "X-API-Key: ${key}" "${API_BASE}/api/scripts/translate/status" 2>/dev/null || true)"
  if [ -n "$translate_status" ]; then
    mode="$(json_get mode "$translate_status" 2>/dev/null || true)"
    available="$(json_get script_available "$translate_status" 2>/dev/null || true)"
    [ "$available" = "true" ] && pass "translation script available (${mode:-unknown} mode)" || fail "translation script unavailable"
  else
    fail "translation status returned no response"
  fi

  if [ "${SKIP_RECOMMENDATION_SMOKE:-0}" != "1" ]; then
    rec_file="$(mktemp)"
    rec_code="$(curl -sS --max-time "${REC_TIMEOUT:-180}" -o "$rec_file" -w '%{http_code}' -H "X-API-Key: ${key}" "${API_BASE}/api/users/${RECOMMENDATION_USER}/recommendations?force=true" 2>/dev/null || true)"
    if [ "$rec_code" = "200" ] && python3 - "$rec_file" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as fh:
    data = json.load(fh)
raise SystemExit(0 if isinstance(data, list) and len(data) > 0 else 1)
PY
    then
      count="$(python3 - "$rec_file" <<'PY'
import json, sys
print(len(json.load(open(sys.argv[1], encoding='utf-8'))))
PY
)"
      pass "recommendation smoke returned ${count} items"
    else
      fail "recommendation smoke failed with HTTP ${rec_code:-none}"
    fi
    rm -f "$rec_file"
  fi
else
  log "skip: protected endpoint checks need PLEXMIND_API_KEY or ${ENV_FILE}"
fi

page="$(http_get "${API_BASE}/" 2>/dev/null || true)"
if printf '%s' "$page" | grep -q 'LLM (llama.cpp)' && printf '%s' "$page" | grep -q 'qwen3-4b-q4_k_m'; then
  pass "dashboard labels show llama.cpp/qwen3-4b"
else
  fail "dashboard labels do not show expected llama.cpp/qwen3-4b text"
fi
if printf '%s' "$page" | grep -qE 'Ollama|qwen3\.5'; then
  fail "dashboard still contains stale Ollama/qwen3.5 text"
else
  pass "dashboard has no stale Ollama/qwen3.5 text"
fi

if [ "$FAILURES" -eq 0 ]; then
  log "PASS: live verification succeeded"
  exit 0
fi
log "FAIL: ${FAILURES} check(s) failed"
exit 1
