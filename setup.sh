#!/usr/bin/env bash
# ==============================================================================
# PlexMind Suite — One-Button Setup
#
# Detects server hardware (GPU, VRAM, CPU, RAM), selects optimal AI models,
# generates .env config, pulls Docker images, and starts all services.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/.../setup.sh | bash
#   — or —
#   git clone ... && cd plexmind-suite && ./setup.sh
# ==============================================================================
set -euo pipefail

BOLD='\033[1m'
DIM='\033[2m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

banner() {
    echo -e "${BOLD}"
    cat << 'EOF'
    ____  __           __  ____           __
   / __ \/ /__  _  __ /  |/  (_)___  ____/ /
  / /_/ / / _ \| |/_// /|_/ / / __ \/ __  /
 / ____/ /  __/>  < / /  / / / / / / /_/ /
/_/   /_/\___/_/|_|/_/  /_/_/_/ /_/\__,_/
           S  U  I  T  E
EOF
    echo -e "${NC}"
    echo -e "${DIM}AI-powered transcription, translation, and recommendations for Plex${NC}"
    echo ""
}

# ==============================================================================
# DEPENDENCY CHECKS
# ==============================================================================

check_dependencies() {
    local missing=()
    for cmd in docker curl jq; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done

    # Check docker compose (v2 plugin or standalone)
    if docker compose version &>/dev/null; then
        COMPOSE_CMD="docker compose"
    elif command -v docker-compose &>/dev/null; then
        COMPOSE_CMD="docker-compose"
    else
        missing+=("docker-compose")
    fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        error "Missing required tools: ${missing[*]}"
        echo "  Install them and re-run this script."
        exit 1
    fi
    ok "Dependencies: docker, curl, jq, ${COMPOSE_CMD}"
}

# ==============================================================================
# HARDWARE DETECTION
# ==============================================================================

detect_cpu() {
    CPU_MODEL=$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | xargs 2>/dev/null || echo "Unknown")
    CPU_CORES=$(nproc 2>/dev/null || echo 1)
    RAM_MB=$(awk '/MemTotal/ {printf "%.0f", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
    RAM_GB=$(( RAM_MB / 1024 ))

    info "CPU:  ${CPU_MODEL} (${CPU_CORES} cores)"
    info "RAM:  ${RAM_GB}GB"
}

detect_gpu() {
    HAS_NVIDIA=false
    GPU_NAME=""
    GPU_VRAM_MB=0
    GPU_COMPUTE=""

    if command -v nvidia-smi &>/dev/null; then
        GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | xargs)
        GPU_VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | xargs)
        GPU_COMPUTE=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | xargs)

        if [[ -n "$GPU_NAME" && "$GPU_VRAM_MB" -gt 0 ]]; then
            HAS_NVIDIA=true
            GPU_VRAM_GB=$(( GPU_VRAM_MB / 1024 ))
            ok "GPU:  ${GPU_NAME} (${GPU_VRAM_GB}GB VRAM, Compute ${GPU_COMPUTE})"
        fi
    fi

    if [[ "$HAS_NVIDIA" == false ]]; then
        warn "No NVIDIA GPU detected. Running in CPU-only mode."
        warn "Transcription and recommendations will be slower."
        GPU_VRAM_GB=0
    fi
}

# ==============================================================================
# MODEL SELECTION
# ==============================================================================

select_models() {
    info "Selecting optimal models for your hardware..."
    echo ""

    # --- llama.cpp model (recommendations + translation) ---
    LLAMA_CPP_MODEL_ALIAS="qwen3-4b-q4_k_m"
    LLAMA_CPP_MODEL_PATH="${LLAMA_CPP_MODEL_PATH:-/mnt/cache/appdata/llama-cpp/models/Qwen3-4B-Q4_K_M.gguf}"
    LLM_TIER="Local GGUF"

    # --- Whisper Model (transcription) ---
    if [[ $GPU_VRAM_GB -ge 10 ]]; then
        WHISPER_MODEL="turbo"
        WHISPER_TIER="Best"
    elif [[ $GPU_VRAM_GB -ge 5 ]]; then
        WHISPER_MODEL="medium"
        WHISPER_TIER="Balanced"
    elif [[ $GPU_VRAM_GB -ge 2 ]]; then
        WHISPER_MODEL="small"
        WHISPER_TIER="Fast"
    elif [[ $GPU_VRAM_GB -ge 1 ]]; then
        WHISPER_MODEL="base"
        WHISPER_TIER="Basic"
    else
        WHISPER_MODEL="tiny"
        WHISPER_TIER="CPU"
    fi

    # Whisper Docker image tag
    if [[ "$HAS_NVIDIA" == true ]]; then
        WHISPER_IMAGE="onerahmet/openai-whisper-asr-webservice:latest-gpu"
        WHISPER_DEVICE="cuda"
    else
        WHISPER_IMAGE="onerahmet/openai-whisper-asr-webservice:latest"
        WHISPER_DEVICE="cpu"
    fi

    echo -e "  ${BOLD}Model Selection${NC}"
    echo -e "  ────────────────────────────────────────"
    echo -e "  LLM (recs + translation):  ${GREEN}${LLAMA_CPP_MODEL_ALIAS}${NC} [${LLM_TIER}]"
    echo -e "  Whisper (transcription):   ${GREEN}${WHISPER_MODEL}${NC} [${WHISPER_TIER}]"
    echo -e "  Whisper device:            ${WHISPER_DEVICE}"
    echo ""
}

# ==============================================================================
# CONFIGURATION
# ==============================================================================

prompt_config() {
    echo -e "${BOLD}Configuration${NC}"
    echo -e "────────────────────────────────────────"

    # Plex
    local default_plex_url="http://localhost:32400"
    read -rp "  Plex URL [${default_plex_url}]: " PLEX_URL
    PLEX_URL="${PLEX_URL:-$default_plex_url}"

    read -rp "  Plex Token (find at plex.tv/claim): " PLEX_TOKEN
    if [[ -z "$PLEX_TOKEN" ]]; then
        warn "No Plex token provided. You can set PLEX_TOKEN in .env later."
        PLEX_TOKEN="YOUR_PLEX_TOKEN_HERE"
    fi

    # TMDB
    read -rp "  TMDB API Key (free at themoviedb.org): " TMDB_API_KEY
    TMDB_API_KEY="${TMDB_API_KEY:-}"

    # Media paths
    local default_movies="/mnt/data/media/Movies"
    local default_tv="/mnt/data/media/TV Shows"
    read -rp "  Movies directory [${default_movies}]: " MOVIES_DIR
    MOVIES_DIR="${MOVIES_DIR:-$default_movies}"
    read -rp "  TV Shows directory [${default_tv}]: " TV_DIR
    TV_DIR="${TV_DIR:-$default_tv}"

    # Translation languages
    echo ""
    echo -e "  ${DIM}Translation target languages (comma-separated ISO codes)${NC}"
    echo -e "  ${DIM}Examples: zh (Chinese), es-MX (Mexican Spanish), fr (French), ja (Japanese)${NC}"
    read -rp "  Languages [zh,es-MX]: " TARGET_LANGS
    TARGET_LANGS="${TARGET_LANGS:-zh,es-MX}"

    # Watermark
    read -rp "  Subtitle watermark text [Brought to you by PlexMind]: " WATERMARK_TEXT
    WATERMARK_TEXT="${WATERMARK_TEXT:-Brought to you by PlexMind}"

    echo ""
}

generate_env() {
    info "Generating .env configuration..."

    cat > .env << ENVEOF
# ==============================================================================
# PlexMind Suite Configuration
# Generated by setup.sh on $(date -u '+%Y-%m-%d %H:%M:%S UTC')
# Hardware: ${CPU_MODEL} | ${RAM_GB}GB RAM | ${GPU_NAME:-CPU only} ${GPU_VRAM_GB}GB VRAM
# ==============================================================================

# --- Plex ---
PLEX_URL=${PLEX_URL}
PLEX_TOKEN=${PLEX_TOKEN}

# --- Media Paths ---
MOVIES_DIR=${MOVIES_DIR}
TV_DIR=${TV_DIR}

# --- LLM (llama.cpp) ---
LLAMA_CPP_URL=http://llama-cpp:8080
LLAMA_CPP_API_URL=http://llama-cpp:8080/v1/chat/completions
LLAMA_CPP_MODEL_ALIAS=${LLAMA_CPP_MODEL_ALIAS}
LLAMA_CPP_MODEL_PATH=${LLAMA_CPP_MODEL_PATH}
LLAMA_CPP_HOST_PORT=11435
LLAMA_CPP_MAX_TOKENS=768

# --- Whisper (Transcription) ---
WHISPER_IMAGE=${WHISPER_IMAGE}
WHISPER_MODEL=${WHISPER_MODEL}
WHISPER_DEVICE=${WHISPER_DEVICE}
WHISPER_MEM_LIMIT=12g
WHISPER_HOST_PORT=9001
TRANSCRIBE_AUDIO_EXT=mp3
TRANSCRIBE_AUDIO_CODEC=libmp3lame
TRANSCRIBE_AUDIO_BITRATE=64k
WHISPER_UPLOAD_SPLIT_MB=50
WHISPER_SEGMENT_SECONDS=600

# --- API Keys ---
TMDB_API_KEY=${TMDB_API_KEY}
TVDB_API_KEY=
OMDB_API_KEY=

# --- Translation ---
TARGET_LANGUAGES=${TARGET_LANGS}
WATERMARK_TEXT=${WATERMARK_TEXT}

# --- PlexMind Recommendations ---
MAX_RECOMMENDATIONS=10
CANDIDATE_POOL_SIZE=40
MIN_HISTORY_ITEMS=3
SUPPRESSION_DAYS=60
CACHE_TTL_SECONDS=3600

# --- GPU Management ---
GPU_THRESHOLD_PCT=30
GPU_BACKOFF_MINUTES=30

# --- Script logs ---
# Dated script logs under /app/data/logs are retained for 7 days
LOG_RETENTION_DAYS=7
# Optional per-run cap. Use 0 for no cap, or pass via docker exec/cron.
MAX_RUNTIME_MINUTES=0

# --- Scheduling ---
# Transcription: runs daily at 5am, stops at noon
TRANSCRIBE_START_HOUR=5
TRANSCRIBE_END_HOUR=12
# Translation: runs nightly at 11pm, stops at 3am
TRANSLATE_START_HOUR=23
TRANSLATE_END_HOUR=3
# Recommendations: 1st of month at 3am UTC
REC_CRON=0 3 1 * *

# --- Paths (internal, don't change) ---
FEEDBACK_FILE=data/feedback.json
SHOWN_RECS_FILE=data/shown_recs.json
WATCHLIST_TRACK_FILE=data/watchlist_track.json
ENVEOF

    ok "Configuration written to .env"
}

# ==============================================================================
# DOCKER SETUP
# ==============================================================================

prepare_models() {
    info "Checking llama.cpp model configuration..."

    if [[ -f "${LLAMA_CPP_MODEL_PATH}" ]]; then
        ok "llama.cpp model found: ${LLAMA_CPP_MODEL_PATH}"
    else
        warn "llama.cpp model file not found: ${LLAMA_CPP_MODEL_PATH}"
        warn "Place a GGUF model there or edit LLAMA_CPP_MODEL_PATH in .env before using recommendations or translation."
    fi

    ok "Whisper model (${WHISPER_MODEL}) will download on first transcription"
}

start_services() {
    info "Starting all services..."
    ${COMPOSE_CMD} up -d 2>&1
    echo ""

    # Wait for services to be healthy
    sleep 5

    echo -e "${BOLD}Service Status${NC}"
    echo -e "────────────────────────────────────────"
    ${COMPOSE_CMD} ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || ${COMPOSE_CMD} ps
    echo ""
}

# ==============================================================================
# SUMMARY
# ==============================================================================

print_summary() {
    echo -e "${BOLD}${GREEN}"
    echo "============================================================"
    echo "  PlexMind Suite is running!"
    echo "============================================================"
    echo -e "${NC}"
    echo -e "  ${BOLD}Services:${NC}"
    echo -e "    Recommendations API:  http://localhost:8000/docs"
    echo -e "    Whisper ASR:          http://localhost:${WHISPER_HOST_PORT:-9001}"
    echo -e "    llama.cpp LLM:        http://localhost:${LLAMA_CPP_HOST_PORT:-11435}"
    echo ""
    echo -e "  ${BOLD}Commands:${NC}"
    echo -e "    Start all:            ${COMPOSE_CMD} up -d"
    echo -e "    Stop all:             ${COMPOSE_CMD} down"
    echo -e "    View logs:            ${COMPOSE_CMD} logs -f"
    echo -e "    Run transcription:    ${COMPOSE_CMD} exec scripts transcribe.sh"
    echo -e "    Run translation:      ${COMPOSE_CMD} exec scripts translate.sh"
    echo -e "    Run recommendations:  curl -X POST http://localhost:8000/api/run-all"
    echo -e "    Library maintenance:  ${COMPOSE_CMD} exec scripts maintenance.sh all"
    echo ""
    echo -e "  ${BOLD}Configuration:${NC} .env"
    echo -e "  ${BOLD}Data:${NC}          ./data/"
    echo ""
    echo -e "  ${DIM}Edit .env and restart to change settings.${NC}"
    echo -e "  ${DIM}For help: https://github.com/FineChAInesium/PlexMind${NC}"
    echo ""
}

# ==============================================================================
# MAIN
# ==============================================================================

main() {
    banner
    check_dependencies
    echo ""

    echo -e "${BOLD}Hardware Detection${NC}"
    echo -e "────────────────────────────────────────"
    detect_cpu
    detect_gpu
    echo ""

    select_models

    prompt_config
    generate_env

    echo ""
    prepare_models
    echo ""

    start_services
    print_summary
}

main "$@"
