#!/bin/bash
# FaultMaven Local Deployment CLI (Docker-based)
# Manages FaultMaven containerized local deployment
#
# This script is for LOCAL DEPLOYMENT where users install and manage the application themselves.
# Local deployment can be used for both development and production workloads.
# For cloud/SaaS deployment, see faultmaven-enterprise-infra repository.
#
# IMAGES: By default both services run from PRE-BUILT images on the GitHub
# Container Registry (GHCR) — no build toolchain, no Docker Hub rate limits:
#   - api:       ghcr.io/faultmaven/faultmaven           (bundles model + KB; runs offline)
#   - dashboard: ghcr.io/faultmaven/faultmaven-dashboard
# Tags are pinnable via FM_IMAGE_TAG / FM_DASHBOARD_IMAGE_TAG in .env (default :latest).
# ChromaDB and Redis run in-process within the API container, NOT as separate containers.
#
# BUILD FROM SOURCE (contributors): `start --build` builds the API from this repo;
# `start --build-dashboard` also builds the Dashboard from ../faultmaven-dashboard.
# These layer docker-compose.build.yml / docker-compose.dashboard-build.yml on top.
# `start --pull` refreshes the pre-built images before starting.

set -e

# Ensure script is run from the correct directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
CLI_VERSION="2.0.0"
MIN_RAM_GB=4
REQUIRED_COMMANDS=("docker" "curl")

# CLI options
NO_COLOR=false
TAIL_LINES=""
YES_FLAG=false
INTERACTIVE=true
DEMO_MODE=false
BUILD_MODE=false        # --build: build the API from this repo's source
BUILD_DASHBOARD=false   # --build-dashboard: also build the Dashboard from ../faultmaven-dashboard
PULL_MODE=false         # --pull: refresh pre-built images from the registry before starting

# Sibling dashboard source repo (only needed for --build-dashboard)
DASHBOARD_SRC_DIR="../faultmaven-dashboard"

# Detect non-interactive mode
if [ ! -t 0 ]; then
    INTERACTIVE=false
fi

# Health check endpoints: "port:service_name:path"
HEALTH_CHECK_SERVICES=(
    "8090:API:/health"
    "3333:Dashboard:/"
)
DEFAULT_PORT=8090

#######################################
# Utility Functions
#######################################

print_header() {
    echo -e "${BLUE}╔════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║${NC}    FaultMaven Container Manager      ${BLUE}║${NC}"
    echo -e "${BLUE}╚════════════════════════════════════════╝${NC}"
    echo ""
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

# Prompt helper for interactive/non-interactive mode
confirm_prompt() {
    local prompt="$1"
    local default="${2:-n}"
    local context="${3:-}"

    if [ "$YES_FLAG" = true ]; then
        return 0
    fi

    if [ "$INTERACTIVE" = false ]; then
        if [ "$default" = "y" ]; then
            return 0
        else
            if [ -n "$context" ]; then
                echo "Non-interactive mode: '$context' requires --yes to confirm"
            else
                echo "Non-interactive mode: use --yes to confirm destructive operations"
            fi
            return 1
        fi
    fi

    read -p "$prompt " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        return 0
    elif [[ $REPLY =~ ^[Nn]$ ]]; then
        return 1
    else
        [ "$default" = "y" ] && return 0 || return 1
    fi
}

#######################################
# Compose file assembly
#######################################

# Assemble the `-f <file>` arguments for docker compose based on build flags.
# Result is placed in the COMPOSE_FILES array (caller expands "${COMPOSE_FILES[@]}").
#   default            → docker-compose.yml only (pull pre-built GHCR images)
#   --build            → + docker-compose.build.yml (build API from this repo)
#   --build-dashboard  → + docker-compose.dashboard-build.yml (build Dashboard from source)
COMPOSE_FILES=()
compose_files_args() {
    COMPOSE_FILES=(-f docker-compose.yml)
    if [ "$BUILD_MODE" = true ]; then
        COMPOSE_FILES+=(-f docker-compose.build.yml)
    fi
    if [ "$BUILD_DASHBOARD" = true ]; then
        COMPOSE_FILES+=(-f docker-compose.dashboard-build.yml)
    fi
}

# Guard: --build-dashboard needs the dashboard source checked out as a sibling.
check_dashboard_source() {
    if [ "$BUILD_DASHBOARD" = true ] && [ ! -d "$DASHBOARD_SRC_DIR" ]; then
        print_error "--build-dashboard requires the Dashboard source at $DASHBOARD_SRC_DIR"
        echo ""
        echo "The Dashboard lives in a separate repository. Clone it next to this one:"
        echo "  git clone https://github.com/FaultMaven/faultmaven-dashboard.git $DASHBOARD_SRC_DIR"
        echo ""
        echo "Or drop --build-dashboard to use the pre-built Dashboard image from GHCR."
        exit 1
    fi
}

#######################################
# Pre-flight Checks
#######################################

check_dependencies() {
    print_info "Checking dependencies..."

    local all_present=true
    local missing_cmds=()

    for cmd in "${REQUIRED_COMMANDS[@]}"; do
        if ! command -v "$cmd" &> /dev/null; then
            print_error "$cmd is not installed"
            missing_cmds+=("$cmd")
            all_present=false
        else
            print_success "$cmd found"
        fi
    done

    if [ "$all_present" = false ]; then
        echo ""
        print_error "Missing required dependencies: ${missing_cmds[*]}"
        echo ""
        echo "Install instructions:"
        for cmd in "${missing_cmds[@]}"; do
            case "$cmd" in
                docker)
                    echo "  docker: https://docs.docker.com/get-docker/"
                    ;;
                curl)
                    echo "  curl:   sudo apt install curl (Debian/Ubuntu)"
                    echo "          brew install curl     (macOS)"
                    ;;
            esac
        done
        exit 1
    fi
}

check_docker_running() {
    if ! docker info &> /dev/null; then
        print_error "Docker daemon is not running"
        echo "Please start Docker Desktop or the Docker service"
        exit 1
    fi
    print_success "Docker daemon is running"
}

check_resources() {
    print_info "Checking system resources..."

    # Check available RAM
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        local total_ram=$(free -g | awk '/^Mem:/{print $2}')
        local available_ram=$(free -g | awk '/^Mem:/{print $7}')
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        local total_ram_bytes=$(sysctl -n hw.memsize)
        local total_ram=$((total_ram_bytes / 1024 / 1024 / 1024))
        local available_ram=$total_ram
    else
        print_warning "Cannot determine RAM on this OS, skipping check"
        return
    fi

    if [ "$available_ram" -lt "$MIN_RAM_GB" ]; then
        print_warning "Only ${available_ram}GB RAM available (${MIN_RAM_GB}GB minimum recommended)"
        print_warning "FaultMaven may run slowly or fail to start"
        echo ""
        if ! confirm_prompt "Continue anyway? (y/N)"; then
            print_info "Startup cancelled"
            exit 1
        fi
    else
        print_success "Sufficient RAM available (${available_ram}GB)"
    fi
}

# Read a single KEY's value from .env WITHOUT executing the file. A .env is NOT a
# shell script: values may legitimately contain spaces, braces, or JSON (e.g.
# LLM_PROVIDER_TIMEOUT_OVERRIDES={"fireworks": 180}), which `source` would try to
# run. docker compose and pydantic parse these fine; we must not `source` them.
# Last assignment wins; a single layer of surrounding quotes is stripped.
read_env_var() {
    local key="$1" line val
    line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" .env 2>/dev/null | tail -n1)
    [ -z "$line" ] && return 0
    val="${line#*=}"
    # strip one matching pair of surrounding quotes, plus trailing whitespace
    val="${val%"${val##*[![:space:]]}"}"
    val="${val%\"}"; val="${val#\"}"
    val="${val%\'}"; val="${val#\'}"
    printf '%s' "$val"
}

check_env_file() {
    if [ ! -f .env ]; then
        print_warning ".env file not found"
        print_info "Creating .env from .env.example..."

        if [ -f .env.example ]; then
            cp .env.example .env
            print_success ".env file created"
            echo ""
            print_error "ACTION REQUIRED: Configure .env file before starting"
            echo ""
            echo "Edit .env and set these REQUIRED variables:"
            echo "  1. OPENAI_API_KEY=sk-... (or another LLM provider)"
            echo "  2. CHAT_PROVIDER=openai (or groq, anthropic, etc.)"
            echo ""
            echo "Then run: ./faultmaven.sh start"
            exit 1
        else
            print_error ".env.example not found"
            exit 1
        fi
    fi

    # Check for at least one LLM provider configured (read values safely, do NOT
    # source .env — see read_env_var above).
    # Report the provider the app will ACTUALLY use (CHAT_PROVIDER) — not just the
    # first key we happen to find. Values are read safely (no `source` — see
    # read_env_var). Recognized providers in STABLE order: "id|Display|CRED_VAR";
    # 'local' authenticates via LOCAL_LLM_URL rather than an API key.
    local provider_specs=(
        "openai|OpenAI|OPENAI_API_KEY"
        "anthropic|Anthropic|ANTHROPIC_API_KEY"
        "gemini|Gemini|GEMINI_API_KEY"
        "fireworks|Fireworks|FIREWORKS_API_KEY"
        "groq|Groq|GROQ_API_KEY"
        "huggingface|HuggingFace|HUGGINGFACE_API_KEY"
        "cohere|Cohere|COHERE_API_KEY"
        "openrouter|OpenRouter|OPENROUTER_API_KEY"
        "local|Local (Ollama/vLLM)|LOCAL_LLM_URL"
    )

    local chat_provider
    chat_provider="$(read_env_var CHAT_PROVIDER | tr '[:upper:]' '[:lower:]')"

    # First provider (stable order) with its credential set — the start gate and
    # the fallback when CHAT_PROVIDER is unset/unusable.
    local first_name="" spec pid pname pcred cred
    for spec in "${provider_specs[@]}"; do
        IFS='|' read -r pid pname pcred <<< "$spec"
        cred="$(read_env_var "$pcred")"
        if [ -n "$cred" ] && [[ ! "$cred" =~ ^your- ]]; then
            first_name="$pname"
            break
        fi
    done

    # Resolve CHAT_PROVIDER → display name + whether its credential is set.
    local chat_name="" chat_known=false chat_ok=false
    if [ -n "$chat_provider" ]; then
        for spec in "${provider_specs[@]}"; do
            IFS='|' read -r pid pname pcred <<< "$spec"
            if [ "$pid" = "$chat_provider" ]; then
                chat_known=true; chat_name="$pname"
                cred="$(read_env_var "$pcred")"
                [ -n "$cred" ] && [[ ! "$cred" =~ ^your- ]] && chat_ok=true
                break
            fi
        done
    fi

    # Start gate: refuse only when NOTHING usable is configured.
    if [ -z "$first_name" ] && [ "$chat_ok" = false ]; then
        print_error "No LLM provider configured"
        echo ""
        echo "Edit .env and set CHAT_PROVIDER plus its credential, e.g.:"
        echo "  CHAT_PROVIDER=openai   + OPENAI_API_KEY=sk-..."
        echo "  CHAT_PROVIDER=gemini   + GEMINI_API_KEY=..."
        echo "  CHAT_PROVIDER=groq     + GROQ_API_KEY=gsk-..."
        echo "  CHAT_PROVIDER=local    + LOCAL_LLM_URL=http://localhost:11434  (no key)"
        echo ""
        exit 1
    fi

    # Confirm the file is valid BEFORE naming the specific provider, so the
    # sequence reads: "✓ .env file configured" → "ℹ LLM Provider: …".
    print_success ".env file configured"

    # Report tied to CHAT_PROVIDER (the provider the app selects at runtime).
    if [ "$chat_known" = true ]; then
        print_info "LLM Provider: $chat_name (CHAT_PROVIDER)"
        if [ "$chat_ok" = false ]; then
            print_warning "CHAT_PROVIDER=$chat_provider but its credential is not set in .env"
            [ -n "$first_name" ] && print_warning "Another provider IS configured ($first_name) — the app may fall back to it"
        fi
    elif [ -n "$chat_provider" ]; then
        print_warning "CHAT_PROVIDER='$chat_provider' is not a recognized provider"
        print_info "LLM Provider: $first_name (first configured — check CHAT_PROVIDER)"
    else
        print_info "LLM Provider: $first_name (CHAT_PROVIDER unset — app uses its default)"
    fi
}

#######################################
# Core Commands
#######################################

# Returns 0 if any of THIS project's compose containers are currently running.
fm_containers_running() {
    docker compose ps --format json 2>/dev/null | grep -q '"State":"running"'
}

# Identify the process(es) holding a port. Populates:
#   HOLDER_PIDS        — space-separated PID list (may be empty if root-owned)
#   HOLDER_IS_DOCKER   — true if a holder looks like Docker (docker-proxy/dockerd),
#                        or if the port is held but no PID is visible (privileged
#                        listener — most often Docker's published port).
describe_port_holder() {
    local port="$1"
    HOLDER_PIDS=""
    HOLDER_IS_DOCKER=false

    # ss first (no privileges needed for our own processes); fall back to lsof.
    HOLDER_PIDS=$(ss -tlnp 2>/dev/null | grep ":$port " | grep -oP 'pid=\K[0-9]+' | sort -u | tr '\n' ' ')
    if [ -z "$HOLDER_PIDS" ] && command -v lsof >/dev/null 2>&1; then
        HOLDER_PIDS=$(lsof -ti :"$port" -sTCP:LISTEN 2>/dev/null | sort -u | tr '\n' ' ')
    fi

    if [ -z "$HOLDER_PIDS" ]; then
        # Port is in use but we can't see the owning PID — typically a root-owned
        # listener such as Docker's published port.
        HOLDER_IS_DOCKER=true
        return
    fi

    local p comm
    for p in $HOLDER_PIDS; do
        comm=$(ps -p "$p" -o comm= 2>/dev/null)
        if [[ "$comm" == *docker* ]]; then
            HOLDER_IS_DOCKER=true
        fi
    done
}

# Returns 0 if both required ports are free; 1 (with guidance) if either is held
# by something OTHER than this project's own containers. Identifies each holder
# (process vs Docker) so the user knows whether a process-based FaultMaven
# (./scripts/faultmaven-dev.sh) or a stray container is in the way.
check_docker_ports() {
    local ports_to_check=(8090 3333)
    local any_conflict=false
    local saw_docker=false
    local saw_process=false

    for port in "${ports_to_check[@]}"; do
        # Is anything listening on this port?
        if ! ss -tln 2>/dev/null | grep -q ":$port " \
           && ! { command -v lsof >/dev/null 2>&1 && lsof -Pi :"$port" -sTCP:LISTEN -t >/dev/null 2>&1; }; then
            continue
        fi

        if [ "$any_conflict" = false ]; then
            print_error "Required port(s) already in use — cannot start:"
            any_conflict=true
        fi

        describe_port_holder "$port"
        if [ "$HOLDER_IS_DOCKER" = true ]; then
            saw_docker=true
            echo "  • Port $port — held by a Docker container / privileged listener${HOLDER_PIDS:+ (PID(s): $HOLDER_PIDS)}"
        else
            saw_process=true
            echo "  • Port $port — held by process (PID(s): $HOLDER_PIDS)"
        fi
        # Show holder details (mirrors faultmaven-dev.sh) so the user can identify it
        local p
        for p in $HOLDER_PIDS; do
            ps -p "$p" -o pid,ppid,comm,args 2>/dev/null | tail -n +1 | sed 's/^/      /'
        done
    done

    if [ "$any_conflict" = true ]; then
        echo ""
        echo "Resolve before starting:"
        if [ "$saw_process" = true ]; then
            echo "  • A process-based FaultMaven on these ports? Stop it: ./scripts/faultmaven-dev.sh stop"
        fi
        if [ "$saw_docker" = true ]; then
            echo "  • A stray/previous FaultMaven container? Stop it: ./faultmaven.sh stop  (or: ./faultmaven.sh kill)"
        fi
        echo "  • Inspect manually: ss -tlnp | grep -E ':8090 |:3333 '   (or: lsof -i :8090)"
        return 1
    fi
    return 0
}

wait_for_containers_ready() {
    local max_wait=120   # first boot also runs DB migrations + KB seeding
    local elapsed=0
    local check_interval=2
    local total_count=${#HEALTH_CHECK_SERVICES[@]}

    print_info "Waiting for services to become healthy (up to ${max_wait}s; first boot runs migrations + KB seeding)..."

    while [ $elapsed -lt $max_wait ]; do
        local healthy_count=0
        local status_line=""

        for service in "${HEALTH_CHECK_SERVICES[@]}"; do
            local port="${service%%:*}"
            local rest="${service#*:}"
            local name="${rest%%:*}"
            local path="${rest#*:}"

            if curl -sf "http://localhost:$port$path" > /dev/null 2>&1; then
                healthy_count=$((healthy_count + 1))
                status_line+="  ${name} ✓"
            else
                status_line+="  ${name} …"
            fi
        done

        # In-place progress: elapsed / budget + per-service state.
        printf '\r  ⏳ %3ds / %ds —%s        ' "$elapsed" "$max_wait" "$status_line"

        if [ "$healthy_count" -eq "$total_count" ]; then
            # All responding — confirm stability with one more check after a pause.
            sleep 2
            local still_healthy=0
            for service in "${HEALTH_CHECK_SERVICES[@]}"; do
                local port="${service%%:*}"
                local rest="${service#*:}"
                local path="${rest#*:}"
                curl -sf "http://localhost:$port$path" > /dev/null 2>&1 && still_healthy=$((still_healthy + 1))
            done
            if [ "$still_healthy" -eq "$total_count" ]; then
                printf '\r  ✓ all services healthy in ~%ds%-30s\n' "$elapsed" ""
                return 0
            fi
        fi

        sleep $check_interval
        elapsed=$((elapsed + check_interval))
    done

    echo ""
    return 1
}

cmd_start() {
    print_header
    local start_time=$(date +%s)
    echo "Starting FaultMaven services..."
    echo ""

    check_dependencies
    check_docker_running
    check_resources
    check_env_file
    check_dashboard_source

    # If THIS stack is already running, don't mistake our own published ports for
    # a conflict — report "already running" (parity with faultmaven-dev.sh).
    if fm_containers_running; then
        print_warning "FaultMaven containers are already running"
        echo ""
        echo "  • Health:  ./faultmaven.sh health"
        echo "  • Logs:    ./faultmaven.sh logs"
        echo "  • Apply config/image changes: ./faultmaven.sh restart"
        echo "  • Access:  Dashboard http://localhost:3333  |  API http://localhost:8090"
        exit 0
    fi

    # Check if ports are available before starting (held by a process-based
    # FaultMaven, a stray container, or anything else).
    if ! check_docker_ports; then
        exit 1
    fi

    echo ""

    # Assemble compose files + flags from build/demo/pull options
    compose_files_args
    local profile_args=()
    if [ "$DEMO_MODE" = true ]; then
        profile_args=(--profile demo)
        print_info "Demo mode enabled: Will seed sample data"
    fi

    local building=false
    if [ "$BUILD_MODE" = true ] || [ "$BUILD_DASHBOARD" = true ]; then
        building=true
    fi

    if [ "$building" = true ]; then
        print_info "Building from source (API: $BUILD_MODE, Dashboard: $BUILD_DASHBOARD)..."
        print_info "First build compiles dependencies — typically several minutes. Live progress below."
    else
        print_info "Starting from pre-built GHCR images..."
        # Refresh images first only when explicitly asked (pure pull mode)
        if [ "$PULL_MODE" = true ]; then
            print_info "Refreshing pre-built images from registry..."
            docker compose "${COMPOSE_FILES[@]}" "${profile_args[@]}" pull || \
                print_warning "Image refresh failed; continuing with locally cached images"
        fi
        # The API image is ~5GB. First run downloads it; cached restarts skip this.
        if docker image inspect "ghcr.io/faultmaven/faultmaven:${FM_IMAGE_TAG:-latest}" >/dev/null 2>&1; then
            print_info "API image present — starting containers (~30–60s)."
        else
            print_info "First run downloads the API image (~5GB) — typically 2–5 min on a fast link. Live progress below."
        fi
    fi
    echo ""

    local up_args=(up -d)
    [ "$building" = true ] && up_args+=(--build)

    # Stream compose output LIVE so the user sees docker's own pull/build/create
    # progress (the previous version captured it, leaving a long silent wait),
    # while still capturing it via tee for error analysis on failure.
    local compose_output compose_log compose_rc
    compose_log="$(mktemp)"
    docker compose "${COMPOSE_FILES[@]}" "${profile_args[@]}" "${up_args[@]}" 2>&1 | tee "$compose_log"
    compose_rc=${PIPESTATUS[0]}
    compose_output="$(cat "$compose_log")"
    rm -f "$compose_log"

    if [ "$compose_rc" -ne 0 ]; then
        echo ""
        print_error "Failed to start Docker containers"
        echo ""
        echo "Docker Compose output:"
        echo "$compose_output" | head -20
        echo ""

        # Check for common errors
        if echo "$compose_output" | grep -qi "port.*already.*allocated\|address already in use"; then
            print_error "Port conflict detected"
            echo "A container or process is already using the required ports."
            echo "Run: ./faultmaven.sh stop  # Stop existing containers"
        elif echo "$compose_output" | grep -qi "Cannot connect to the Docker daemon"; then
            print_error "Cannot connect to Docker daemon"
            echo "Make sure Docker is running: sudo systemctl start docker"
        elif echo "$compose_output" | grep -qiE "manifest.*not found|pull access denied|not found: manifest|failed to resolve|no such host|unauthorized"; then
            print_error "Could not pull a pre-built image from the registry"
            echo "The tagged image may not exist, or the registry is unreachable."
            echo "  • Check connectivity to ghcr.io"
            echo "  • Verify FM_IMAGE_TAG / FM_DASHBOARD_IMAGE_TAG in .env are valid tags"
            echo "  • Or build from source instead: ./faultmaven.sh start --build"
        else
            echo "Check container logs for details:"
            echo "  ./faultmaven.sh logs"
        fi
        exit 1
    fi

    # Verify containers actually started
    sleep 2
    local running_containers=0
    if docker compose ps --format json 2>/dev/null | grep -q '"State":"running"'; then
        running_containers=$(docker compose ps --format json 2>/dev/null | grep -c '"State":"running"' || echo "0")
    fi

    if [ "$running_containers" -eq "0" ]; then
        echo ""
        print_error "Containers failed to start"
        echo ""
        echo "Container status:"
        docker compose ps
        echo ""
        echo "Recent logs:"
        docker compose logs --tail 20
        exit 1
    fi

    # Wait for services to become ready and stable
    if wait_for_containers_ready; then
        local end_time=$(date +%s)
        local duration=$((end_time - start_time))
        echo ""
        print_success "FaultMaven services started successfully! (${duration}s)"
        echo ""
        echo "Next steps:"
        echo "  1. Check health:  ./faultmaven.sh health"
        echo "  2. View logs:     ./faultmaven.sh logs"
        echo "  3. Access services:"
        echo "     - Dashboard: http://localhost:3333"
        echo "     - API:       http://localhost:8090"
        echo "     - API Docs:  http://localhost:8090/docs"
        echo ""
    else
        echo ""
        print_error "Services started but did not become healthy within timeout"
        echo ""
        echo "Container status:"
        docker compose ps
        echo ""
        echo "Recent logs:"
        docker compose logs --tail 30
        echo ""
        print_info "Run './faultmaven.sh logs' for full logs or './faultmaven.sh health' to check health"
        exit 1
    fi
}

cmd_stop() {
    print_header
    echo "Stopping FaultMaven services..."
    echo ""

    # Use --remove-orphans to clean up containers from old compose file versions
    if docker compose down --remove-orphans; then
        print_success "Services stopped"
        echo ""
        print_info "Data preserved in ./data directory"
        print_info "Run './faultmaven.sh start' to restart"
    else
        print_error "Failed to stop services"
        exit 1
    fi
}

cmd_health() {
    print_header
    echo "Running comprehensive health checks..."
    echo ""

    # Container Status
    echo "Container Status:"
    echo "-----------------"
    local container_output
    container_output=$(docker compose ps 2>/dev/null || true)
    echo "$container_output"

    # Check if any containers are running
    local running_containers=0
    if docker compose ps --format json 2>/dev/null | grep -q '"State":"running"'; then
        running_containers=$(docker compose ps --format json 2>/dev/null | grep -c '"State":"running"' || echo "0")
    fi

    echo ""
    if [ "$running_containers" -eq "0" ]; then
        print_warning "No containers are running"
        echo ""
        print_info "Start services with: ./faultmaven.sh start"
        print_info ""
        print_info "If you're running services locally (not in Docker), use:"
        echo "  ./scripts/faultmaven-dev.sh health  # Check local process health"
        echo ""
        exit 1
    fi

    echo "HTTP Health Checks:"
    echo "-------------------"
    local failed=0

    for service in "${HEALTH_CHECK_SERVICES[@]}"; do
        local port="${service%%:*}"
        local rest="${service#*:}"
        local name="${rest%%:*}"
        local path="${rest#*:}"

        echo -n "$name (port $port)... "
        if curl -sf "http://localhost:$port$path" > /dev/null 2>&1; then
            print_success "OK"
        else
            print_error "NOT RESPONDING"
            ((failed++))
        fi
    done

    echo ""

    # Check data directory
    if [ -d ./data ]; then
        local db_size=$(du -sh ./data 2>/dev/null | cut -f1)
        print_info "Data directory size: $db_size"
    fi

    echo ""
    if [ $failed -eq 0 ]; then
        print_success "All health checks passed!"
        echo ""
        echo "Access points:"
        echo "  • Dashboard: http://localhost:3333"
        echo "  • API:       http://localhost:8090"
        echo "  • API Docs:  http://localhost:8090/docs"
    else
        print_error "$failed service(s) not responding"
        echo ""
        echo "Troubleshooting:"
        echo "  • Check logs: ./faultmaven.sh logs"
        echo "  • Restart:    ./faultmaven.sh restart"
        exit 1
    fi
}

cmd_logs() {
    local service="$1"
    local tail_opt=""

    if [ -n "$TAIL_LINES" ]; then
        tail_opt="--tail $TAIL_LINES"
    fi

    # Check if containers are running
    local running_containers=0
    if docker compose ps --format json 2>/dev/null | grep -q '"State":"running"'; then
        running_containers=$(docker compose ps --format json 2>/dev/null | grep -c '"State":"running"' || echo "0")
    fi

    if [ "$running_containers" -gt "0" ]; then
        print_success "FaultMaven containers are RUNNING ($running_containers services)"
        if [ -z "$service" ]; then
            if [ -n "$TAIL_LINES" ]; then
                echo "Showing last $TAIL_LINES lines from all services (Ctrl+C to exit)..."
            else
                echo "Streaming logs from all services (Ctrl+C to exit)..."
            fi
            echo ""
            docker compose logs -f $tail_opt
        else
            if [ -n "$TAIL_LINES" ]; then
                echo "Showing last $TAIL_LINES lines from $service (Ctrl+C to exit)..."
            else
                echo "Streaming logs from $service (Ctrl+C to exit)..."
            fi
            echo ""
            docker compose logs -f $tail_opt "$service"
        fi
    else
        print_warning "FaultMaven containers are NOT RUNNING"
        if [ -z "$service" ]; then
            echo "Showing logs from stopped containers..."
            echo ""
            docker compose logs $tail_opt
        else
            echo "Showing logs from stopped $service..."
            echo ""
            docker compose logs $tail_opt "$service"
        fi
    fi
}

cmd_restart() {
    print_header
    local service="$1"

    # Check if containers are running — if not, delegate to start
    local running_containers=0
    if docker compose ps --format json 2>/dev/null | grep -q '"State":"running"'; then
        running_containers=$(docker compose ps --format json 2>/dev/null | grep -c '"State":"running"' || echo "0")
    fi

    if [ "$running_containers" -eq "0" ]; then
        print_warning "No containers running — starting services instead"
        echo ""
        cmd_start
        return
    fi

    if [ -z "$service" ]; then
        echo "Restarting all FaultMaven services..."
        echo ""

        if docker compose restart; then
            print_success "All services restarted"
            echo ""
            print_info "Run './faultmaven.sh health' to verify health"
        else
            print_error "Failed to restart services"
            exit 1
        fi
    else
        echo "Restarting $service..."
        echo ""

        if docker compose restart "$service"; then
            print_success "$service restarted"
            echo ""
            print_info "Run './faultmaven.sh health' to verify health"
        else
            print_error "Failed to restart $service"
            exit 1
        fi
    fi
}

cmd_kill() {
    print_header
    echo "Force-killing all FaultMaven containers..."
    echo ""

    # Kill and remove containers from main compose file (api + dashboard only)
    # Use --remove-orphans to clean up containers from old compose file versions
    # (e.g., chromadb, redis which should not exist in local version)
    docker compose kill 2>/dev/null || true
    docker compose rm -f --remove-orphans 2>/dev/null || true

    # Kill any remaining containers with faultmaven prefix (catch-all for orphaned containers)
    # This handles containers from old compose file versions (e.g., chromadb, redis)
    # Note: Local version uses in-process storage, so these containers shouldn't exist
    local remaining_containers
    remaining_containers=$(docker ps -a --filter "name=faultmaven" --format "{{.Names}}" 2>/dev/null || true)
    if [ -n "$remaining_containers" ]; then
        # Use process substitution to avoid subshell issues
        while IFS= read -r container || [ -n "$container" ]; do
            [ -z "$container" ] && continue
            print_info "Removing orphaned container: $container"
            docker kill "$container" 2>/dev/null || true
            docker rm -f "$container" 2>/dev/null || true
        done < <(echo "$remaining_containers")
    fi

    print_success "All FaultMaven containers killed and removed"
    echo ""
    print_info "Data preserved in ./data directory"
    print_info "Run './faultmaven.sh start' to restart"
}

cmd_clean() {
    print_header
    local wipe_data=false

    # Check arguments
    for arg in "$@"; do
        if [ "$arg" == "--wipe-data" ]; then
            wipe_data=true
        fi
    done

    if [ "$wipe_data" = true ]; then
        print_warning "This will PERMANENTLY DELETE all data including:"
        echo "  - All cases and troubleshooting sessions"
        echo "  - All uploaded evidence files"
        echo "  - All knowledge base documents"
        echo "  - SQLite database"
        echo ""
        echo "Docker images and containers will be preserved"
        echo ""

        if [ "$YES_FLAG" = true ]; then
            print_warning "Proceeding with --yes flag..."
        elif [ "$INTERACTIVE" = false ]; then
            echo "Non-interactive mode: 'clean --wipe-data' requires --yes to confirm"
            exit 1
        else
            read -p "Are you sure? Type 'DELETE' to confirm: " confirm
            if [ "$confirm" != "DELETE" ]; then
                print_info "Clean cancelled"
                return
            fi
        fi
    else
        print_info "Cleaning Docker resources (services stopped)..."
        echo "  (Data directory is protected. Use '--wipe-data' to delete it)"
        echo ""
    fi

    echo ""
    print_info "Stopping services..."
    docker compose down -v --remove-orphans

    if [ "$wipe_data" = true ]; then
        if [ -d ./data ]; then
            print_info "Removing data directory..."
            rm -rf ./data
            print_success "FaultMaven data has been deleted"
        fi
    else
        print_info "Data preserved in ./data directory"
    fi

    echo ""
    print_info "Docker images preserved - restart will be fast"
    print_info "Run './faultmaven.sh start' to start fresh"
}

cmd_prune() {
    print_header
    print_warning "This will remove:"
    echo "  - All stopped FaultMaven containers"
    echo "  - All FaultMaven images"
    echo "  - Unused Docker networks"
    echo ""
    echo "Data in ./data directory will be preserved"
    echo ""

    if ! confirm_prompt "Continue? (y/N)" "n" "prune"; then
        print_info "Prune cancelled"
        return
    fi

    echo ""

    # Stop and remove containers (including orphans from old compose files)
    print_info "Stopping and removing containers..."
    docker compose down --remove-orphans 2>/dev/null || true

    # Remove FaultMaven images
    print_info "Removing FaultMaven images..."
    docker compose down --rmi all 2>/dev/null || true

    # Remove unused networks
    print_info "Removing unused networks..."
    docker network prune -f 2>/dev/null || true

    print_success "Docker cleanup complete"
    echo ""
    print_info "Data preserved in ./data directory"
    print_info "Run './faultmaven.sh start' to rebuild and restart"
}

cmd_build() {
    print_header

    # `build` always builds the API from source; add the Dashboard with --build-dashboard.
    BUILD_MODE=true
    check_dashboard_source
    compose_files_args

    if [ "$BUILD_DASHBOARD" = true ]; then
        echo "Building FaultMaven API + Dashboard images from source..."
    else
        echo "Building FaultMaven API image from source..."
        echo "  (Dashboard uses the pre-built GHCR image; add --build-dashboard to build it too)"
    fi
    echo ""

    if docker compose "${COMPOSE_FILES[@]}" build; then
        echo ""
        print_success "Images built successfully"
        echo ""
        echo "Next steps:"
        echo "  ./faultmaven.sh start --build    # Start with newly built images"
    else
        echo ""
        print_error "Build failed"
        exit 1
    fi
}

cmd_ps() {
    print_header
    echo "Running containers:"
    echo ""
    docker compose ps
}

cmd_version() {
    echo "FaultMaven CLI v${CLI_VERSION}"
    echo ""
    echo "Repository: https://github.com/FaultMaven/faultmaven"
}

cmd_list_users() {
    # Check if API service is running
    if ! docker compose ps --services --filter "status=running" | grep -q "api"; then
        print_header
        print_error "API service is not running. Run './faultmaven.sh start' first."
        exit 1
    fi

    local port=$DEFAULT_PORT

    print_header
    print_info "Listing all user accounts..."
    echo ""

    # Call the API endpoint
    response=$(curl -s "http://localhost:$port/api/v1/auth/dev-list-users")

    # Parse and format the output
    echo "$response" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print('=' * 100)
    print(f\"Found {data['total']} user(s):\n\")
    if data['users']:
        print(f\"{'#':<4} {'USERNAME':<20} {'EMAIL':<30} {'ROLES':<20} {'USER_ID'}\")
        print('-' * 100)
        for idx, user in enumerate(data['users'], 1):
            roles_str = ', '.join(user['roles']) if user['roles'] else 'none'
            admin_indicator = '👑 ' if 'admin' in user['roles'] else '   '
            print(f\"{admin_indicator}{idx:<4} {user['username']:<20} {user['email']:<30} {roles_str:<20} {user['user_id'][:36]}\")
        print('\n' + '=' * 100)
        admin_count = sum(1 for u in data['users'] if 'admin' in u['roles'])
        print(f\"Total: {data['total']} user(s) | Admins: {admin_count} | Regular: {data['total'] - admin_count}\")
        print('=' * 100)
except Exception as e:
    print(f'❌ Error parsing response: {e}')
    sys.exit(1)
"
}

cmd_delete_user() {
    # Check if API service is running
    if ! docker compose ps --services --filter "status=running" | grep -q "api"; then
        print_header
        print_error "API service is not running. Run './faultmaven.sh start' first."
        exit 1
    fi

    local port=$DEFAULT_PORT

    print_header

    # Prompt for username
    local username="$1"
    if [ -z "$username" ]; then
        read -p "Username to delete: " username
    fi

    if [ -z "$username" ]; then
        print_error "Username is required"
        exit 1
    fi

    echo ""
    print_warning "This will PERMANENTLY DELETE user: $username"
    echo ""
    read -p "Are you sure? Type 'DELETE' to confirm: " confirm

    if [ "$confirm" != "DELETE" ]; then
        print_info "Cancelled"
        exit 0
    fi

    echo ""

    # Call the API endpoint
    response=$(curl -s -w "\n%{http_code}" -X DELETE "http://localhost:$port/api/v1/auth/dev-delete-user/$username")
    http_code=$(echo "$response" | tail -n1)
    body=$(echo "$response" | sed '$d')

    if [ "$http_code" -eq 200 ]; then
        echo "$body" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(f\"✓ {data['message']}\")
    print(f\"  User ID: {data['user_id']}\")
except Exception as e:
    print('✓ User deleted successfully')
"
        echo ""
        print_success "User deleted successfully"
    else
        echo "$body" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(f\"❌ {data.get('detail', 'Failed to delete user')}\")
except:
    print('❌ Failed to delete user')
"
        echo ""
        print_error "Failed to delete user"
        exit 1
    fi
}

cmd_create_user() {
    # Check if API service is running
    if ! docker compose ps --services --filter "status=running" | grep -q "api"; then
        print_header
        print_error "API service is not running. Run './faultmaven.sh start' first."
        exit 1
    fi

    # Check if API is healthy
    if ! curl -sf "http://localhost:8090/health" > /dev/null 2>&1; then
        print_header
        print_error "API service is not responding. Wait for it to become healthy."
        exit 1
    fi

    print_header
    echo "Create New User Account"
    echo ""
    echo "This will create a user account via the API endpoint."
    echo "You'll be prompted for username, email (optional), and role."
    echo ""

    # Interactive prompts
    read -p "Username (required): " username
    if [ -z "$username" ]; then
        print_error "Username is required"
        exit 1
    fi

    read -p "Email (optional, will auto-generate if empty): " email
    read -p "Display Name (optional, will auto-generate if empty): " display_name
    read -p "Role (user/admin) [default: user]: " role_input

    role_input=${role_input:-user}
    role_input=$(echo "$role_input" | tr '[:upper:]' '[:lower:]')

    if [ "$role_input" != "admin" ] && [ "$role_input" != "user" ]; then
        print_warning "Invalid role '$role_input', defaulting to 'user'"
        role_input="user"
    fi

    echo ""
    echo "Creating user with:"
    echo "  Username: $username"
    echo "  Email: ${email:-'(auto-generated)'}"
    echo "  Display Name: ${display_name:-'(auto-generated)'}"
    echo "  Role: $role_input"
    echo ""

    read -p "Create this user? (yes/no): " confirm
    confirm=$(echo "$confirm" | tr '[:upper:]' '[:lower:]')
    if [ "$confirm" != "yes" ] && [ "$confirm" != "y" ]; then
        print_info "Cancelled"
        exit 0
    fi

    echo ""
    print_info "Creating user via API..."

    # Build JSON payload
    json_payload="{"
    json_payload+="\"username\": \"$username\""
    if [ -n "$email" ]; then
        json_payload+=", \"email\": \"$email\""
    fi
    if [ -n "$display_name" ]; then
        json_payload+=", \"display_name\": \"$display_name\""
    fi
    json_payload+="}"

    # Call dev-register endpoint
    response=$(curl -s -w "\n%{http_code}" -X POST "http://localhost:8090/api/v1/auth/dev-register" \
        -H "Content-Type: application/json" \
        -d "$json_payload" 2>&1)

    http_code=$(echo "$response" | tail -n1)
    body=$(echo "$response" | sed '$d')

    if [ "$http_code" = "201" ]; then
        echo ""
        print_success "User '$username' created successfully!"
        echo ""
        print_info "You can now log in at http://localhost:3333"
        echo ""
        echo "Note: If you specified 'admin' role, you'll need to promote the user:"
        echo "  docker compose exec api python scripts/auth/promote_to_admin.py $username"
    elif [ "$http_code" = "409" ]; then
        echo ""
        print_error "User '$username' already exists"
        echo ""
        print_info "Use './faultmaven.sh create-user' with a different username, or log in directly."
        exit 1
    else
        echo ""
        print_error "Failed to create user (HTTP $http_code)"
        echo ""
        echo "Response:"
        echo "$body" | head -20
        exit 1
    fi
}

cmd_help() {
    print_header
    echo "Usage: ./faultmaven.sh [command] [options]"
    echo ""
    echo "Service Management:"
    echo "  start [options]             Start all FaultMaven services (pulls pre-built GHCR images)"
    echo "  stop                        Stop all services (preserves data)"
    echo "  restart [service]           Restart all or specific service"
    echo "  health                      Run comprehensive health checks"
    echo "  logs [service] [--tail N]   Stream logs from services"
    echo "  ps                          Show running containers"
    echo ""
    echo "User Management:"
    echo "  create-user                 Create a new user account"
    echo "  list-users                  List all user accounts"
    echo "  delete-user [name]          Delete a user account"
    echo ""
    echo "Build Commands:"
    echo "  build [--build-dashboard]   Build image(s) from source (API always; Dashboard if flagged)"
    echo ""
    echo "Cleanup Commands:"
    echo "  clean                       Delete data only (preserves images)"
    echo "  kill                        Force-kill all containers"
    echo "  prune                       Remove containers + images"
    echo ""
    echo "Information:"
    echo "  version                     Show CLI version"
    echo "  help                        Show this help message"
    echo ""
    echo "Options:"
    echo "  --demo                      Start with demo data (sample runbooks)"
    echo "  --pull                      Refresh pre-built images from the registry before starting"
    echo "  --build                     Build the API from THIS repo's source instead of pulling"
    echo "  --build-dashboard           Also build the Dashboard from ../faultmaven-dashboard"
    echo "  --no-color                  Disable colored output"
    echo "  --yes, -y                   Auto-confirm destructive operations"
    echo "  --tail N                    Limit log output to last N lines"
    echo ""
    echo "Image source (default = pre-built images from GHCR):"
    echo "  Pin tags in .env:  FM_IMAGE_TAG, FM_DASHBOARD_IMAGE_TAG  (default: latest)"
    echo "  ghcr.io/faultmaven/faultmaven, ghcr.io/faultmaven/faultmaven-dashboard"
    echo ""
    echo "Examples:"
    echo "  ./faultmaven.sh start                 # Start from pre-built GHCR images (fast)"
    echo "  ./faultmaven.sh start --pull          # Refresh images, then start"
    echo "  ./faultmaven.sh start --demo          # Start with demo data"
    echo "  ./faultmaven.sh start --build         # Build the API from source, then start"
    echo "  ./faultmaven.sh start --build --build-dashboard  # Build both from source"
    echo "  ./faultmaven.sh create-user           # Create user account"
    echo "  ./faultmaven.sh list-users            # List all users"
    echo "  ./faultmaven.sh delete-user bob       # Delete user 'bob'"
    echo "  ./faultmaven.sh health                # Run health checks"
    echo "  ./faultmaven.sh logs api              # View API logs"
    echo "  ./faultmaven.sh logs --tail 100       # View last 100 lines"
    echo "  ./faultmaven.sh restart dashboard     # Restart dashboard only"
    echo ""
    echo "Alternative: Process-based Local Deployment (no Docker):"
    echo "  ./scripts/faultmaven-dev.sh start  # Start API as local Python process"
    echo "  ./scripts/faultmaven-dev.sh health # Check process health"
    echo ""
    echo "Note: The 'status' command has been replaced with 'health' for consistency."
    echo ""
}

#######################################
# Main
#######################################

# Parse flags and extract command + args
COMMAND=""
ARGS=()
SKIP_NEXT=false

for ((i=1; i<=$#; i++)); do
    if [ "$SKIP_NEXT" = true ]; then
        SKIP_NEXT=false
        continue
    fi

    arg="${!i}"
    next_i=$((i + 1))
    next_arg="${!next_i:-}"

    case $arg in
        --demo)
            DEMO_MODE=true
            ;;
        --build)
            BUILD_MODE=true
            ;;
        --build-dashboard)
            BUILD_DASHBOARD=true
            ;;
        --pull)
            PULL_MODE=true
            ;;
        --no-color)
            NO_COLOR=true
            RED=''
            GREEN=''
            YELLOW=''
            BLUE=''
            NC=''
            ;;
        --yes|-y)
            YES_FLAG=true
            ;;
        --tail)
            if [ -n "$next_arg" ] && [[ "$next_arg" =~ ^[0-9]+$ ]]; then
                TAIL_LINES="$next_arg"
                SKIP_NEXT=true
            else
                echo "Error: --tail requires a numeric argument"
                exit 1
            fi
            ;;
        --tail=*)
            TAIL_LINES="${arg#*=}"
            if ! [[ "$TAIL_LINES" =~ ^[0-9]+$ ]]; then
                echo "Error: --tail requires a numeric argument"
                exit 1
            fi
            ;;
        -h|--help)
            if [ -z "$COMMAND" ]; then
                COMMAND="help"
            fi
            ;;
        -*)
            echo "Warning: Unknown flag '$arg' (ignored)"
            ;;
        *)
            if [ -z "$COMMAND" ]; then
                COMMAND="$arg"
            else
                ARGS+=("$arg")
            fi
            ;;
    esac
done

# Execute command
case "${COMMAND:-}" in
    start)
        cmd_start
        ;;
    stop)
        cmd_stop
        ;;
    restart)
        cmd_restart "${ARGS[0]:-}"
        ;;
    health)
        cmd_health
        ;;
    status)
        # Redirect old status command to health
        print_warning "'status' command is deprecated. Use 'health' instead."
        echo ""
        cmd_health
        ;;
    logs)
        cmd_logs "${ARGS[0]:-}"
        ;;
    kill)
        cmd_kill
        ;;
    clean)
        shift
        cmd_clean "$@"
        ;;
    prune)
        cmd_prune
        ;;
    build)
        cmd_build
        ;;
    create-user)
        cmd_create_user
        ;;
    list-users)
        cmd_list_users
        ;;
    delete-user)
        cmd_delete_user "${ARGS[0]:-}"
        ;;
    ps)
        cmd_ps
        ;;
    version|--version|-v)
        cmd_version
        ;;
    help|--help|-h|"")
        cmd_help
        ;;
    *)
        print_error "Unknown command: ${COMMAND}"
        echo ""
        cmd_help
        exit 1
        ;;
esac
