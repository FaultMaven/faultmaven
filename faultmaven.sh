#!/bin/bash
# FaultMaven Local Deployment CLI (Docker-based)
# Manages FaultMaven containerized local deployment
#
# This script is for LOCAL DEPLOYMENT where users install and manage the application themselves.
# Local deployment can be used for both development and production workloads.
# For cloud/SaaS deployment, see faultmaven-enterprise-infra repository.
#
# IMPORTANT: This script uses docker-compose.yml which defines ONLY:
#   - api: FaultMaven API server (uses in-process storage: in-memory sessions/vectors, SQLite)
#   - dashboard: FaultMaven Dashboard frontend
#
# ChromaDB and Redis run as in-process services within the API container,
# NOT as separate containers. For external services, use docker-compose.dev.yml separately.

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

check_docker_ports() {
    # Check if required ports are available
    local ports_to_check=(8000 3000)
    local ports_in_use=()

    for port in "${ports_to_check[@]}"; do
        # Use ss first (works without special privileges), fallback to lsof
        if ss -tlnp 2>/dev/null | grep -q ":$port "; then
            # Extract PIDs from ss output
            local pids=$(ss -tlnp 2>/dev/null | grep ":$port " | grep -oP 'pid=\K[0-9]+' | sort -u | tr '\n' ' ' || echo "unknown")
            ports_in_use+=("$port (PID(s): ${pids:-unknown})")
        elif command -v lsof >/dev/null 2>&1 && lsof -Pi :$port -sTCP:LISTEN -t >/dev/null 2>&1; then
            # Fallback to lsof if ss didn't find anything
            local pids=$(lsof -ti :$port 2>/dev/null | tr '\n' ' ' || echo "unknown")
            ports_in_use+=("$port (PID(s): ${pids:-unknown})")
        fi
    done

    if [ ${#ports_in_use[@]} -gt 0 ]; then
        print_error "Required ports are already in use:"
        for port_info in "${ports_in_use[@]}"; do
            echo "  • Port $port_info"
        done
        echo ""
        echo "To resolve this:"
        echo "  1. Check what's using the ports:"
        for port in "${ports_to_check[@]}"; do
            echo "     ss -tlnp | grep :$port"
            echo "     (or) lsof -i :$port"
        done
        echo "  2. Stop existing FaultMaven containers: ./faultmaven.sh stop"
        echo "  3. Or stop local processes using those ports:"
        echo "     ./scripts/faultmaven-dev.sh stop"
        return 1
    fi
    return 0
}

wait_for_containers_ready() {
    local max_wait=60
    local elapsed=0
    local check_interval=2

    print_info "Waiting for services to become healthy (up to ${max_wait}s)..."
    echo ""

    while [ $elapsed -lt $max_wait ]; do
        local healthy_count=0
        local total_count=${#HEALTH_CHECK_SERVICES[@]}

        for service in "${HEALTH_CHECK_SERVICES[@]}"; do
            local port="${service%%:*}"
            local name="${service#*:}"

            if curl -sf "http://localhost:$port/health" > /dev/null 2>&1; then
                ((healthy_count++))
            fi
        done

        if [ "$healthy_count" -eq "$total_count" ]; then
            # All services are responding, wait a bit more to ensure stability
            sleep 2
            # Verify they're still responding
            local still_healthy=0
            for service in "${HEALTH_CHECK_SERVICES[@]}"; do
                local port="${service%%:*}"
                if curl -sf "http://localhost:$port/health" > /dev/null 2>&1; then
                    ((still_healthy++))
                fi
            done
            if [ "$still_healthy" -eq "$total_count" ]; then
                return 0
            fi
        fi

        echo -n "."
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

    # Check if ports are available before starting
    if ! check_docker_ports; then
        exit 1
    fi

    echo ""
    print_info "Starting containers with Docker Compose..."
    echo ""

    local compose_cmd="docker compose up -d"
    if [ "$DEMO_MODE" = true ]; then
        compose_cmd="docker compose --profile demo up -d"
        print_info "Demo mode enabled: Will seed sample data"
    fi

    # Capture compose output for error analysis
    local compose_output
    if ! compose_output=$($compose_cmd 2>&1); then
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
        local name="${service#*:}"

        echo -n "$name (port $port)... "
        if curl -sf "http://localhost:$port/health" > /dev/null 2>&1; then
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

    # Also kill containers from dev compose file if it exists (for cleanup of old containers)
    if [ -f docker-compose.dev.yml ]; then
        docker compose -f docker-compose.dev.yml kill 2>/dev/null || true
        docker compose -f docker-compose.dev.yml rm -f 2>/dev/null || true
    fi

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

cmd_list_users() {
    # Check if API service is running
    if ! docker compose ps --services --filter "status=running" | grep -q "api"; then
        print_header
        print_error "API service is not running. Run './faultmaven.sh start' first."
        exit 1
    fi

    # Extract port from environment
    local port=$(grep -E '^PORT=' .env 2>/dev/null | cut -d '=' -f2 | tr -d '"' | tr -d "'")
    port=${port:-$DEFAULT_PORT}

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

    # Extract port from environment
    local port=$(grep -E '^PORT=' .env 2>/dev/null | cut -d '=' -f2 | tr -d '"' | tr -d "'")
    port=${port:-$DEFAULT_PORT}

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
    echo "  start [--demo]              Start all FaultMaven services"
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
    echo "  ./faultmaven.sh create-user        # Create user account"
    echo "  ./faultmaven.sh list-users         # List all users"
    echo "  ./faultmaven.sh delete-user bob    # Delete user 'bob'"
    echo "  ./faultmaven.sh health             # Run health checks"
    echo "  ./faultmaven.sh logs api           # View API logs"
    echo "  ./faultmaven.sh logs --tail 100    # View last 100 lines"
    echo "  ./faultmaven.sh restart dashboard  # Restart dashboard only"
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
