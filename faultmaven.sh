#!/bin/bash
# FaultMaven Docker Compose CLI
# Manages FaultMaven containerized deployment

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

# Detect non-interactive mode
if [ ! -t 0 ]; then
    INTERACTIVE=false
fi

# Health check endpoints: "port:service_name"
HEALTH_CHECK_SERVICES=(
    "8000:API"
    "3000:Dashboard"
)

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

    # Source .env to check values
    set -a
    source .env
    set +a

    # Check for at least one LLM provider configured
    local has_llm=false
    if [ -n "${OPENAI_API_KEY:-}" ] && [ "${OPENAI_API_KEY}" != "your-openai-api-key" ]; then
        has_llm=true
        print_info "LLM Provider: OpenAI"
    elif [ -n "${ANTHROPIC_API_KEY:-}" ] && [ "${ANTHROPIC_API_KEY}" != "your-anthropic-api-key" ]; then
        has_llm=true
        print_info "LLM Provider: Anthropic"
    elif [ -n "${GROQ_API_KEY:-}" ]; then
        has_llm=true
        print_info "LLM Provider: Groq"
    fi

    if [ "$has_llm" = false ]; then
        print_error "No LLM API key configured"
        echo ""
        echo "Edit .env and configure AT LEAST ONE provider:"
        echo "  OPENAI_API_KEY=sk-...              # OpenAI GPT"
        echo "  ANTHROPIC_API_KEY=sk-ant-...       # Anthropic Claude"
        echo "  GROQ_API_KEY=gsk-...               # Groq (FREE tier, ultra-fast!)"
        echo ""
        exit 1
    fi

    print_success ".env file configured"
}

#######################################
# Core Commands
#######################################

cmd_start() {
    print_header
    local start_time=$(date +%s)
    echo "Starting FaultMaven services..."
    echo ""

    check_dependencies
    check_docker_running
    check_resources
    check_env_file

    echo ""
    print_info "Starting containers with Docker Compose..."
    echo ""

    local compose_cmd="docker compose up -d"
    if [ "$DEMO_MODE" = true ]; then
        compose_cmd="docker compose --profile demo up -d"
        print_info "Demo mode enabled: Will seed sample data"
    fi

    if $compose_cmd; then
        echo ""
        print_info "Waiting for services to become healthy (up to 60 seconds)..."
        echo ""

        sleep 5  # Give containers time to start

        local max_wait=60
        local elapsed=0
        local all_healthy=false

        while [ $elapsed -lt $max_wait ]; do
            local healthy_count=0
            local total_count=${#HEALTH_CHECK_SERVICES[@]}

            for service in "${HEALTH_CHECK_SERVICES[@]}"; do
                local port="${service%%:*}"

                if curl -sf "http://localhost:$port/health" > /dev/null 2>&1; then
                    ((healthy_count++))
                fi
            done

            if [ "$healthy_count" -eq "$total_count" ]; then
                all_healthy=true
                break
            fi

            echo -n "."
            sleep 5
            elapsed=$((elapsed + 5))
        done

        echo ""

        if [ "$all_healthy" = true ]; then
            local end_time=$(date +%s)
            local duration=$((end_time - start_time))
            echo ""
            print_success "FaultMaven services started successfully! (${duration}s)"
            echo ""
            echo "Next steps:"
            echo "  1. Check status:  ./faultmaven.sh status"
            echo "  2. View logs:     ./faultmaven.sh logs"
            echo "  3. Access services:"
            echo "     - Dashboard: http://localhost:3000"
            echo "     - API:       http://localhost:8000"
            echo "     - API Docs:  http://localhost:8000/docs"
            echo ""
        else
            echo ""
            print_warning "Services started but some may still be initializing"
            echo ""
            echo "Run './faultmaven.sh status' in 30 seconds to verify all services are up."
            echo ""
        fi
    else
        echo ""
        print_error "Failed to start services"
        echo "Run './faultmaven.sh logs' to see error details"
        exit 1
    fi
}

cmd_stop() {
    print_header
    echo "Stopping FaultMaven services..."
    echo ""

    if docker compose down; then
        print_success "Services stopped"
        echo ""
        print_info "Data preserved in ./data directory"
        print_info "Run './faultmaven.sh start' to restart"
    else
        print_error "Failed to stop services"
        exit 1
    fi
}

cmd_status() {
    print_header
    echo "Service Status:"
    echo ""

    docker compose ps

    echo ""
    echo "Health Checks:"
    echo ""

    for service in "${HEALTH_CHECK_SERVICES[@]}"; do
        local port="${service%%:*}"
        local name="${service#*:}"

        if curl -sf "http://localhost:$port/health" > /dev/null 2>&1; then
            print_success "$name (port $port)"
        else
            print_error "$name (port $port) - Not responding"
        fi
    done

    echo ""

    # Check data directory
    if [ -d ./data ]; then
        local db_size=$(du -sh ./data 2>/dev/null | cut -f1)
        print_info "Data directory size: $db_size"
    fi
}

cmd_logs() {
    local service="$1"
    local tail_opt=""

    if [ -n "$TAIL_LINES" ]; then
        tail_opt="--tail $TAIL_LINES"
    fi

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
}

cmd_restart() {
    print_header
    local service="$1"

    if [ -z "$service" ]; then
        echo "Restarting all FaultMaven services..."
        echo ""

        if docker compose restart; then
            print_success "All services restarted"
            echo ""
            print_info "Run './faultmaven.sh status' to verify health"
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
            print_info "Run './faultmaven.sh status' to verify health"
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

    # Kill and remove containers
    docker compose kill 2>/dev/null || true
    docker compose rm -f 2>/dev/null || true

    print_success "All FaultMaven containers killed and removed"
    echo ""
    print_info "Data preserved in ./data directory"
    print_info "Run './faultmaven.sh start' to restart"
}

cmd_clean() {
    print_header
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
        echo "Non-interactive mode: 'clean' requires --yes to confirm"
        exit 1
    else
        read -p "Are you sure? Type 'DELETE' to confirm: " confirm
        if [ "$confirm" != "DELETE" ]; then
            print_info "Clean cancelled"
            return
        fi
    fi

    echo ""
    print_info "Stopping services..."
    docker compose down -v

    if [ -d ./data ]; then
        print_info "Removing data directory..."
        rm -rf ./data
    fi

    print_success "FaultMaven data has been deleted"
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

    # Stop and remove containers
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
    echo "Building FaultMaven images from source..."
    echo ""

    if docker compose build; then
        echo ""
        print_success "Images built successfully"
        echo ""
        echo "Next steps:"
        echo "  ./faultmaven.sh start    # Start with newly built images"
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

cmd_help() {
    print_header
    echo "Usage: ./faultmaven.sh [command] [options]"
    echo ""
    echo "Service Management:"
    echo "  start [--demo]              Start all FaultMaven services"
    echo "  stop                        Stop all services (preserves data)"
    echo "  restart [service]           Restart all or specific service"
    echo "  status                      Show service status and health checks"
    echo "  logs [service] [--tail N]   Stream logs from services"
    echo "  ps                          Show running containers"
    echo ""
    echo "Development Commands:"
    echo "  build                       Build Docker images from source"
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
    echo "  --no-color                  Disable colored output"
    echo "  --yes, -y                   Auto-confirm destructive operations"
    echo "  --tail N                    Limit log output to last N lines"
    echo ""
    echo "Examples:"
    echo "  ./faultmaven.sh start              # Start services"
    echo "  ./faultmaven.sh start --demo       # Start with demo data"
    echo "  ./faultmaven.sh status             # Check service health"
    echo "  ./faultmaven.sh logs api           # View API logs"
    echo "  ./faultmaven.sh logs --tail 100    # View last 100 lines"
    echo "  ./faultmaven.sh restart dashboard  # Restart dashboard only"
    echo ""
    echo "For local development (without Docker):"
    echo "  ./scripts/faultmaven-dev.sh start  # Start API as local process"
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
    status)
        cmd_status
        ;;
    logs)
        cmd_logs "${ARGS[0]:-}"
        ;;
    kill)
        cmd_kill
        ;;
    clean)
        cmd_clean
        ;;
    prune)
        cmd_prune
        ;;
    build)
        cmd_build
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
