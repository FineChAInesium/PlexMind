#!/bin/bash
set -eu

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT

mkdir -p "$TEMP_ROOT/movies" "$TEMP_ROOT/tv"
touch "$TEMP_ROOT/movies/example.mkv"

MOVIE_DIR="$TEMP_ROOT/movies"
TV_DIR="$TEMP_ROOT/tv"
LOG_FILE=""
source "$ROOT_DIR/scripts/lib.sh"
validate_media_directories

MOVIE_DIR="$TEMP_ROOT/missing"
if validate_media_directories; then
    echo "missing media root was incorrectly accepted" >&2
    exit 1
fi

MOVIE_DIR="$TEMP_ROOT/movies"
TV_DIR="$TEMP_ROOT/movies"
if validate_media_directories; then
    echo "identical media roots were incorrectly accepted" >&2
    exit 1
fi

echo "media validation tests passed"
