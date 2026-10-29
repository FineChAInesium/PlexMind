#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT
# `! cmd` is exempt from `set -e`, so negative assertions must exit explicitly.
fail() { echo "FAIL: $*" >&2; cat "$TEMP_ROOT/run.log" >&2 2>/dev/null || true; exit 1; }

srt_cues() {  # srt_cues N -> N numbered cues that pass validate_srt
    local n="$1" i
    for i in $(seq 1 "$n"); do printf '%d\n00:00:%02d,000 --> 00:00:%02d,500\nLine number %d is here.\n\n' "$i" "$i" "$i" "$i"; done
}

MOVIES="$TEMP_ROOT/movies"
SEASON="$TEMP_ROOT/tv/Show/Season 01"
mkdir -p "$MOVIES/Film" "$SEASON" "$TEMP_ROOT/data" "$TEMP_ROOT/bin"
for i in 1 2 3 4 5; do srt_cues 5 > "$MOVIES/Film/Clean$i.en.srt"; done
srt_cues 5 > "$SEASON/Show S01E01.en.srt"
{ printf '\357\273\277'; srt_cues 5; } > "$MOVIES/Film/Bom.en.srt"
# A '|' in the name must survive the problem|path records.
{ printf '\357\273\277'; srt_cues 5; } > "$SEASON/Pipe | Show.en.srt"
{ printf '\357\273\277'; srt_cues 5; printf 'Broken \377 byte\n'; } > "$MOVIES/Film/Bad.en.srt"
cp "$MOVIES/Film/Bad.en.srt" "$TEMP_ROOT/bad-before.srt"

# Records each python3 start by sub-command; PY_FAIL_CHECK makes the check die.
REAL_PYTHON="$(command -v python3)"
cat > "$TEMP_ROOT/bin/python3" <<'EOF'
#!/bin/sh
printf '%s\n' "${2:-}" >> "$PY_CALLS"
if [ "${2:-}" = check ] && [ -n "${PY_FAIL_CHECK:-}" ]; then exit 3; fi
exec "$REAL_PYTHON" "$@"
EOF
chmod +x "$TEMP_ROOT/bin/python3"

run_maintenance() {  # run_maintenance MODE [VAR=value...]; sets rc
    rc=0
    : > "$TEMP_ROOT/python-calls"
    env "${@:2}" PATH="$TEMP_ROOT/bin:$PATH" REAL_PYTHON="$REAL_PYTHON" PY_CALLS="$TEMP_ROOT/python-calls" \
        MOVIE_DIR="$MOVIES" TV_DIR="$TEMP_ROOT/tv" DATA_DIR="$TEMP_ROOT/data" \
        LOG_FILE="$TEMP_ROOT/data/maintenance.log" \
        bash "$ROOT_DIR/scripts/maintenance.sh" "$1" > "$TEMP_ROOT/run.log" 2>&1 || rc=$?
}

# --- One check for the library, a repair only for files that are not UTF-8 ---
run_maintenance encoding
[ "$rc" -eq 0 ] || fail "maintenance.sh encoding exited $rc"
grep -qF 'Encoding fix complete. Converted: 2 files.' "$TEMP_ROOT/run.log" || fail "converted count is wrong"
[ "$(head -c 1 "$MOVIES/Film/Bom.en.srt")" = 1 ] || fail "UTF-8 BOM was not stripped"
[ "$(head -c 1 "$SEASON/Pipe | Show.en.srt")" = 1 ] || fail "subtitle with '|' in its name was not repaired"
cmp -s "$MOVIES/Film/Bad.en.srt" "$TEMP_ROOT/bad-before.srt" || fail "undecodable subtitle was rewritten"
grep -q 'Bad.en.srt does not decode cleanly' "$TEMP_ROOT/run.log" || fail "refused conversion was not logged"
[ "$(grep -cx check "$TEMP_ROOT/python-calls")" -eq 1 ] || fail "library was not classified by a single check"
[ "$(grep -cx repair "$TEMP_ROOT/python-calls")" -eq 3 ] || fail "repair did not run exactly for the 3 non-UTF-8 files: $(sort "$TEMP_ROOT/python-calls" | uniq -c)"

# --- A check that dies is an error, not "Converted: 0" ---
run_maintenance encoding PY_FAIL_CHECK=1
[ "$rc" -eq 2 ] || fail "failed encoding check exited $rc instead of 2"
grep -qF 'ERROR: Encoding scan failed.' "$TEMP_ROOT/run.log" || fail "failed encoding check was not reported"
if grep -q 'Converted:' "$TEMP_ROOT/run.log"; then fail "failed encoding check was reported as complete"; fi
if grep -qx repair "$TEMP_ROOT/python-calls"; then fail "repair ran without a finished check"; fi

run_maintenance all PY_FAIL_CHECK=1
[ "$rc" -eq 2 ] || fail "full maintenance exited $rc after the encoding check failed"
grep -qF 'ERROR: Encoding scan failed.' "$TEMP_ROOT/run.log" || fail "full maintenance did not report the failed encoding check"
if grep -q 'Phase 2' "$TEMP_ROOT/run.log"; then fail "full maintenance went on after the encoding check failed"; fi

# --- ordering sweeps the library's translations under the lock maintenance.sh holds ---
printf '2\n00:00:05,000 --> 00:00:06,000\nSegunda.\n\n1\n00:00:01,000 --> 00:00:02,000\nPrimera.\n' > "$MOVIES/Film/Film.es-MX.srt"
run_maintenance ordering
[ "$rc" -eq 0 ] || fail "maintenance.sh ordering exited $rc"
[ "$(sed -n 2p "$MOVIES/Film/Film.es-MX.srt")" = '00:00:01,000 --> 00:00:02,000' ] || fail "out-of-order translation was not reordered"
grep -qF 'FIXED (2 order issues): Film.es-MX.srt' "$TEMP_ROOT/run.log" || fail "ordering fix was not logged"
if grep -q 'Another media mutation is running' "$TEMP_ROOT/run.log"; then fail "the fixer tried to take the lock maintenance.sh holds"; fi
[ -n "$(find "$TEMP_ROOT/data/quarantine/srt-ordering-backups" -type f -name '*-Film.es-MX.srt' -print -quit 2>/dev/null)" ] \
    || fail "reordered translation was not backed up"

echo "maintenance encoding tests passed"
