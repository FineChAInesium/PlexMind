#!/usr/bin/env bash
# ==============================================================================
# PlexMind Suite setup
#
#   ./setup.sh                 sign in with Plex, find your libraries, pick and
#                              download a model that fits your GPU, start PlexMind
#   ./setup.sh --check         read-only preflight: tools, GPU, the model it would pick
#   ./setup.sh --model DIR     only download the model that fits this GPU into DIR
#                              (for Unraid, where the containers come from templates)
#
# Nothing is typed in but answers to a few questions: no tokens, no keys.
# Re-running keeps your .env and your Plex sign-in.
# ==============================================================================
set -euo pipefail
umask 077

BOLD='\033[1m'; DIM='\033[2m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }
ask()   { local reply; read -rp "  $1 [$2]: " reply </dev/tty; echo "${reply:-$2}"; }

DATA_DIR="${DATA_DIR:-./data}"
PLEX_TV="https://plex.tv"
APP_UID=1000

banner() {
    echo -e "${BOLD}"
    cat << 'EOF'
    ____  __           __  ____           __
   / __ \/ /__  _  __ /  |/  (_)___  ____/ /
  / /_/ / / _ \| |/_// /|_/ / / __ \/ __  /
 / ____/ /  __/>  < / /  / / / / / / /_/ /
/_/   /_/\___/_/|_|/_/  /_/_/_/ /_/\__,_/
EOF
    echo -e "${NC}${DIM}Recommendations, subtitles and translation for Plex, on your own GPU${NC}\n"
}

# ==============================================================================
# Tools and hardware
# ==============================================================================

check_dependencies() {
    local missing=()
    for cmd in docker curl jq sha256sum; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done
    if docker compose version &>/dev/null; then
        COMPOSE_CMD="docker compose"
    elif command -v docker-compose &>/dev/null; then
        COMPOSE_CMD="docker-compose"
    else
        COMPOSE_CMD=""
        [[ "${1:-}" == need-compose ]] && missing+=("docker-compose")
    fi
    if [[ ${#missing[@]} -gt 0 ]]; then
        error "Missing required tools: ${missing[*]}"
        exit 1
    fi
    ok "Tools: docker, curl, jq${COMPOSE_CMD:+, ${COMPOSE_CMD}}"
}

detect_gpu() {
    HAS_NVIDIA=false; GPU_NAME=""; GPU_VRAM_MB=0
    if command -v nvidia-smi &>/dev/null; then
        GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | xargs || true)
        GPU_VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | xargs || echo 0)
        [[ -n "$GPU_NAME" && "${GPU_VRAM_MB:-0}" -gt 0 ]] && HAS_NVIDIA=true
    fi
    if [[ "$HAS_NVIDIA" == true ]]; then
        ok "GPU: ${GPU_NAME} ($(( GPU_VRAM_MB / 1024 )) GB)"
        WHISPER_DEVICE=cuda
    else
        warn "No NVIDIA GPU found. Transcription runs on the CPU (slowly); translation and recommendations need a GPU with 8 GB or more."
        WHISPER_DEVICE=cpu
    fi
    if   (( GPU_VRAM_MB >= 10000 )); then WHISPER_MODEL=turbo
    elif (( GPU_VRAM_MB >= 5000 ));  then WHISPER_MODEL=medium
    elif (( GPU_VRAM_MB >= 2000 ));  then WHISPER_MODEL=small
    else WHISPER_MODEL=base; fi
}

# The language model for recommendations and translation, by GPU memory.
# Sizes and SHA-256 checksums are from Hugging Face; the download is checked.
pick_model() {
    MODEL_NAME=""
    if (( GPU_VRAM_MB >= 11500 )); then
        MODEL_NAME=gemma-4-12b-it-Q5_K_M; MODEL_REPO=unsloth/gemma-4-12b-it-GGUF
        MODEL_SIZE=8413576000; MODEL_SHA=32c554c7d1338a23837ae39b3db482213a4a642f96df3b2619f516a2375d16cd
    elif (( GPU_VRAM_MB >= 9500 )); then
        MODEL_NAME=Qwen3.5-9B-Q5_K_M; MODEL_REPO=unsloth/Qwen3.5-9B-GGUF
        MODEL_SIZE=6577841376; MODEL_SHA=dc2a39aef291f91a9116ad214058da0d86eb648743a124bd8c333787c4b9c91c
    elif (( GPU_VRAM_MB >= 7500 )); then
        MODEL_NAME=Qwen3-8B-Q4_K_M; MODEL_REPO=Qwen/Qwen3-8B-GGUF
        MODEL_SIZE=5027783488; MODEL_SHA=d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785
    fi
    if [[ -n "$MODEL_NAME" ]]; then
        ok "Model for this GPU: ${MODEL_NAME} ($(( MODEL_SIZE / 1000000000 )).$(( MODEL_SIZE / 100000000 % 10 )) GB download)"
    else
        warn "This GPU is too small for the bundled model choices; put a GGUF in the models folder yourself."
    fi
}

download_model() {
    local dir="$1"
    [[ -n "$MODEL_NAME" ]] || return 0
    local file="${dir}/${MODEL_NAME}.gguf"
    mkdir -p "$dir"
    if [[ -f "$file" ]]; then
        ok "Model already there: ${file}"
        return 0
    fi
    local answer
    answer=$(ask "Download ${MODEL_NAME} from Hugging Face now? (y/n)" "y")
    if [[ ! "$answer" =~ ^[Yy] ]]; then
        warn "Skipped. Put ${MODEL_NAME}.gguf (or any GGUF) in ${dir} before using recommendations or translation."
        return 0
    fi
    local free_kb
    free_kb=$(df -Pk "$dir" | awk 'NR==2 {print $4}')
    if (( free_kb * 1024 < MODEL_SIZE + 1000000000 )); then
        error "Not enough free space in ${dir} for ${MODEL_NAME}."
        return 1
    fi
    info "Downloading to ${file} (resumes if interrupted)..."
    curl -fL --retry 3 -C - --progress-bar -o "${file}.part" \
        "https://huggingface.co/${MODEL_REPO}/resolve/main/${MODEL_NAME}.gguf"
    info "Checking the download..."
    if [[ "$(sha256sum "${file}.part" | cut -d' ' -f1)" != "$MODEL_SHA" ]]; then
        error "Checksum mismatch: the download is damaged. Removing it; run setup again to retry."
        rm -f "${file}.part"
        return 1
    fi
    chmod 644 "${file}.part"
    mv "${file}.part" "$file"
    ok "Model ready: ${file}"
}

# ==============================================================================
# Sign in with Plex (the same PIN flow the dashboard uses)
# ==============================================================================

client_id() {
    local file="${DATA_DIR}/plex_client_id"
    if [[ ! -s "$file" ]]; then
        echo "plexmind-$(cat /proc/sys/kernel/random/uuid)" > "$file"
    fi
    cat "$file"
}

plex_tv() {  # METHOD PATH [TOKEN] -> JSON
    local token="${3:-}"
    curl -fsS -X "$1" "${PLEX_TV}$2" -H 'Accept: application/json' \
        -H 'X-Plex-Product: PlexMind' -H "X-Plex-Client-Identifier: ${CLIENT_ID}" \
        ${token:+-H "X-Plex-Token: ${token}"}
}

plex_sign_in() {
    local saved="${DATA_DIR}/plex_auth.json"
    if [[ -s "$saved" ]] && jq -e '.token and .url' "$saved" >/dev/null 2>&1; then
        PLEX_URL=$(jq -r .url "$saved"); PLEX_TOKEN=$(jq -r .token "$saved")
        if curl -fsS -m 5 -H "X-Plex-Token: ${PLEX_TOKEN}" "${PLEX_URL}/identity" >/dev/null 2>&1; then
            ok "Already connected to Plex: $(jq -r '.server_name // .url' "$saved")"
            return 0
        fi
        warn "The saved Plex sign-in no longer works; signing in again."
    fi

    CLIENT_ID=$(client_id)
    local pin pin_id code url token=""
    pin=$(plex_tv POST "/api/v2/pins?strong=true")
    pin_id=$(jq -r .id <<<"$pin"); code=$(jq -r .code <<<"$pin")
    url="https://app.plex.tv/auth#?clientID=${CLIENT_ID}&code=${code}&context%5Bdevice%5D%5Bproduct%5D=PlexMind"
    echo -e "\n  ${BOLD}Open this link and sign in with the Plex account that owns your server:${NC}\n"
    echo -e "  ${CYAN}${url}${NC}\n"
    info "Waiting for you to approve PlexMind (up to 10 minutes)..."
    for _ in $(seq 1 300); do
        token=$(plex_tv GET "/api/v2/pins/${pin_id}" 2>/dev/null | jq -r '.authToken // empty' || true)
        [[ -n "$token" ]] && break
        sleep 2
    done
    [[ -n "$token" ]] || { error "Timed out waiting for Plex."; exit 1; }

    local account servers count choice=0
    account=$(plex_tv GET /api/v2/user "$token")
    servers=$(plex_tv GET "/api/v2/resources?includeHttps=1&includeRelay=0" "$token" \
        | jq '[.[] | select((.provides // "" | split(",") | index("server")) and .owned)]')
    count=$(jq length <<<"$servers")
    if (( count == 0 )); then
        error "$(jq -r .username <<<"$account") does not own a Plex server."
        exit 1
    elif (( count > 1 )); then
        echo "  This account owns ${count} servers:"
        jq -r 'to_entries[] | "    \(.key + 1). \(.value.name)"' <<<"$servers"
        choice=$(( $(ask "Which one?" 1) - 1 ))
    fi
    local server name
    server=$(jq ".[${choice}]" <<<"$servers")
    name=$(jq -r .name <<<"$server")

    # Plain http on the LAN first: it works from a Docker bridge without plex.direct DNS.
    PLEX_URL=""
    local candidate
    for candidate in $(jq -r '
            [.connections[] | select(.relay | not)] as $c
            | ([$c[] | select(.local) | "http://\(.address):\(.port)"]
               + [$c[] | select(.local) | .uri] + [$c[] | select(.local | not) | .uri])[]' <<<"$server"); do
        if curl -fsS -m 4 -H "X-Plex-Token: ${token}" "${candidate}/identity" >/dev/null 2>&1; then
            PLEX_URL="$candidate"; break
        fi
    done
    [[ -n "$PLEX_URL" ]] || { error "Could not reach ${name} from this machine."; exit 1; }
    PLEX_TOKEN="$token"

    jq -n --arg url "$PLEX_URL" --arg token "$token" --arg name "$name" \
          --arg id "$(jq -r .id <<<"$account")" --arg user "$(jq -r .username <<<"$account")" \
          '{url: $url, token: $token, account_id: $id, username: $user, server_name: $name, saved_at: now | floor}' \
          > "${saved}.tmp"
    chmod 600 "${saved}.tmp"
    mv "${saved}.tmp" "$saved"
    ok "Connected to ${name} at ${PLEX_URL} as $(jq -r .username <<<"$account")"
}

# ==============================================================================
# Media folders, from Plex's own libraries
# ==============================================================================

# Plex in Docker sees its own paths (/movies); turn them into host paths.
host_path() {
    local path="$1" container mounts best="" best_len=0 src dst
    container=$(docker ps --format '{{.ID}} {{.Image}}' 2>/dev/null \
        | grep -iE 'pms-docker|linuxserver/plex|hotio/plex|binhex/arch-plex|/plex(:|@| |$)' | head -1 | cut -d' ' -f1 || true)
    if [[ -n "$container" ]]; then
        mounts=$(docker inspect -f '{{range .Mounts}}{{.Source}}|{{.Destination}}{{"\n"}}{{end}}' "$container")
        while IFS='|' read -r src dst; do
            [[ -z "$dst" ]] && continue
            if [[ "$path" == "$dst" || "$path" == "$dst"/* ]] && (( ${#dst} > best_len )); then
                best="${src}${path#"$dst"}"; best_len=${#dst}
            fi
        done <<<"$mounts"
    fi
    echo "${best:-$path}"
}

library_folder() {  # TYPE (movie|show) -> host folder of the first such library
    local sections paths first
    sections=$(curl -fsS -m 10 -H 'Accept: application/json' -H "X-Plex-Token: ${PLEX_TOKEN}" "${PLEX_URL}/library/sections" || echo '{}')
    paths=$(jq -r --arg t "$1" '.MediaContainer.Directory[]? | select(.type == $t) | .Location[]?.path' <<<"$sections")
    first=$(head -1 <<<"$paths")
    if (( $(grep -c . <<<"$paths" || true) > 1 )); then
        warn "More than one $1 folder in Plex; PlexMind uses one per type: $(paste -sd, <<<"$paths")" >&2
    fi
    [[ -n "$first" ]] && host_path "$first"
}

choose_folder() {  # LABEL TYPE ENVKEY FALLBACK
    local current found
    current=$(env_value "$3")
    found=$(library_folder "$2" || true)
    local default="${current:-${found:-$4}}"
    local answer
    while true; do
        answer=$(ask "$1 folder" "$default")
        [[ -d "$answer" ]] && break
        warn "No such folder on this machine: ${answer}" >&2
    done
    echo "$answer"
}

# ==============================================================================
# .env: only what differs per server; everything else has a default in
# docker-compose.yml (see docs/CONFIGURATION.md).
# ==============================================================================

env_value() { [[ -f .env ]] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }

set_env() {
    touch .env
    if grep -qE "^$1=" .env; then
        local escaped
        escaped=$(printf '%s' "$2" | sed 's/[&|\\]/\\&/g')
        sed -i "s|^$1=.*|$1=${escaped}|" .env
    else
        printf '%s=%s\n' "$1" "$2" >> .env
    fi
}

write_env() {
    if [[ ! -f .env ]]; then
        cat > .env << EOF
# PlexMind settings for this server, written by setup.sh.
# Everything not listed here has a default; see docs/CONFIGURATION.md.
# Plex: signed in with Plex (data/plex_auth.json). API key: data/secrets.json.
EOF
    fi
    # An old placeholder token would override the Plex sign-in.
    sed -i '/^PLEX_TOKEN=YOUR_PLEX_TOKEN_HERE$/d' .env
    set_env MOVIES_HOST_PATH "$MOVIES_HOST_PATH"
    set_env TV_HOST_PATH "$TV_HOST_PATH"
    set_env TARGET_LANGUAGES "$TARGET_LANGS"
    set_env LLAMA_CPP_MODELS_DIR "$MODELS_DIR"
    [[ -n "$MODEL_NAME" ]] && set_env LLAMA_CPP_MODEL_ALIAS "$MODEL_NAME"
    set_env WHISPER_MODEL "$WHISPER_MODEL"
    set_env WHISPER_DEVICE "$WHISPER_DEVICE"
    set_env SUBTITLE_UID "$(stat -c '%u' "$MOVIES_HOST_PATH")"
    set_env SUBTITLE_GID "$(stat -c '%g' "$MOVIES_HOST_PATH")"
    set_env TZ "$(cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value 2>/dev/null || echo UTC)"
    [[ -d /var/local/emhttp ]] && set_env UNRAID_EMHTTP_DIR /var/local/emhttp
    chmod 600 .env
    ok "Settings saved to .env ($(grep -c '^[A-Z]' .env) lines)"
}

start_services() {
    chown -R "${APP_UID}:${APP_UID}" "$DATA_DIR" 2>/dev/null || warn "Could not hand ${DATA_DIR} to UID ${APP_UID}; make sure it can write there."
    info "Starting PlexMind..."
    ${COMPOSE_CMD} up -d plexmind
    # Whisper is created now and started by PlexMind only while transcribing.
    ${COMPOSE_CMD} --profile whisper create whisper
}

print_summary() {
    local host
    host=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo -e "\n${BOLD}${GREEN}PlexMind is running.${NC}\n"
    echo -e "  Open ${BOLD}http://${host:-localhost}:8000${NC} and sign in with Plex."
    echo -e "  ${DIM}Logs: ${COMPOSE_CMD} logs -f plexmind    Stop: ${COMPOSE_CMD} down${NC}"
    echo -e "  ${DIM}API key for scripts: ${DATA_DIR}/secrets.json (created on first start)${NC}\n"
}

# ==============================================================================

main() {
    local mode="${1:-install}"
    case "$mode" in
        install|--check) ;;
        --model)
            [[ -n "${2:-}" ]] || { error "Usage: $0 --model DIR"; return 2; }
            check_dependencies; detect_gpu; pick_model; download_model "$2"
            [[ -n "$MODEL_NAME" ]] && echo -e "\n  Set the model to ${BOLD}${MODEL_NAME}${NC} in PlexMind (Dashboard → Model Advisor)."
            return 0 ;;
        *) error "Usage: $0 [--check | --model DIR]"; return 2 ;;
    esac
    banner
    if [[ "$mode" == "--check" ]]; then
        check_dependencies
        detect_gpu
        pick_model
        ok "Read-only setup preflight passed. No configuration or services were changed."
        return 0
    fi
    check_dependencies need-compose
    detect_gpu
    pick_model
    mkdir -p "$DATA_DIR"
    echo -e "\n${BOLD}1. Plex${NC}"
    plex_sign_in
    echo -e "\n${BOLD}2. Your media${NC}"
    MOVIES_HOST_PATH=$(choose_folder Movies movie MOVIES_HOST_PATH /mnt/user/media/Movies)
    TV_HOST_PATH=$(choose_folder "TV shows" show TV_HOST_PATH "/mnt/user/media/TV Shows")
    TARGET_LANGS=$(ask "Translate subtitles into (comma-separated codes, e.g. zh,es-MX,fr)" "$(env_value TARGET_LANGUAGES | grep . || echo zh,es-MX)")
    echo -e "\n${BOLD}3. AI model${NC}"
    local default_models=./models
    [[ -d /mnt/user/appdata ]] && default_models=/mnt/user/appdata/llama-cpp/models
    MODELS_DIR=$(ask "Models folder" "$(env_value LLAMA_CPP_MODELS_DIR | grep . || echo "$default_models")")
    download_model "$MODELS_DIR"
    echo
    write_env
    start_services
    print_summary
}

main "$@"
