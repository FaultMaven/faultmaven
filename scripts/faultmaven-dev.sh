#!/bin/bash
# FaultMaven Local Deployment CLI (Process-based)
# Manages FaultMaven API as a local Python process (no Docker)
#
# This script is for LOCAL DEPLOYMENT where users install and manage the application themselves.
# Local deployment can be used for both development and production workloads.
# For cloud/SaaS deployment, see faultmaven-enterprise-infra repository.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# Configuration
APP_NAME="FaultMaven"
PID_FILE=".faultmaven-dev.pid"
LOG_FILE="/tmp/faultmaven-dev.log"
DEFAULT_PORT=8090
DEFAULT_HOST="0.0.0.0"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

#######################################
# Utility Functions
#######################################

print_header() {
    echo -e "${BLUE}╔════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║${NC}  FaultMaven Local Deployment         ${BLUE}║${NC}"
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

ensure_venv() {
    if [[ -z "$VIRTUAL_ENV" ]]; then
        if [[ -d ".venv" ]]; then
            print_info "Activating virtual environment..."
            source .venv/bin/activate
        else
            print_error "Virtual environment (.venv) not found"
            echo "Create it first: python -m venv .venv"
            exit 1
        fi
    fi
}

check_env() {
    if [ ! -f ".env" ]; then
        print_warning ".env file not found"
        if [ -f ".env.example" ]; then
            cp .env.example .env
            print_success ".env created from .env.example"
            print_warning "Please edit .env and configure your LLM API keys"
        else
            print_error ".env.example not found"
            exit 1
        fi
    fi
}

#######################################
# Process Management
#######################################

# Returns:
#   0 — managed process is running and listening on the expected port
#   1 — nothing is running and nothing else holds the port
#   2 — port is held by an unmanaged process (no PID file, or PID file was
#       stale and the port has since been claimed by something this script
#       did not start: stale uvicorn, Docker, manual run). UNMANAGED_PORT_HOLDER
#       is set to the holder's PID so callers can report it cleanly instead
#       of telling the user "not running" while the port is occupied.
is_running() {
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if ps -p "$pid" > /dev/null 2>&1; then
            # Verify the process actually owns the port
            local port=$(grep -E '^PORT=' .env 2>/dev/null | cut -d '=' -f2 | tr -d '"' | tr -d "'")
            port=${port:-$DEFAULT_PORT}

            # Check if this specific PID is listening on the expected port.
            # Use ss because it doesn't require sudo and works reliably.
            # Retry once after a brief pause to handle transient failures
            # (e.g., ss reading /proc while kernel socket state is updating).
            local attempt
            for attempt in 1 2; do
                if ss -tlnp 2>/dev/null | grep ":$port " | grep -q "pid=$pid"; then
                    return 0
                fi
                [ "$attempt" -eq 1 ] && sleep 1
            done

            # Process exists but not listening on expected port after retries.
            # Do NOT delete the PID file — the process is still alive and may
            # reclaim the port (or the next check may succeed). Only stop_app
            # and start_app failure paths should remove the PID file.
            print_warning "Process $pid exists but not detected on port $port"
            return 1
        else
            # Process is dead — PID file is stale; clean up and fall through
            # to the unmanaged-port check below (something else may have
            # claimed the port after the crash).
            rm -f "$PID_FILE"
        fi
    fi

    # No managed process. Before reporting "not running", check whether the
    # port is held by some other process — without this, health/start would
    # disagree (health says "not running"; start says "port in use") and
    # mislead the user about the actual state.
    local check_port=$(grep -E '^PORT=' .env 2>/dev/null | cut -d '=' -f2 | tr -d '"' | tr -d "'")
    check_port=${check_port:-$DEFAULT_PORT}
    UNMANAGED_PORT_HOLDER=$(ss -tlnp 2>/dev/null | grep ":$check_port " | grep -oP 'pid=\K[0-9]+' | head -1)
    if [ -n "$UNMANAGED_PORT_HOLDER" ]; then
        return 2
    fi
    UNMANAGED_PORT_HOLDER=""
    return 1
}

check_port_available() {
    local port=$1

    # Use ss to check port (works without sudo)
    if ss -tlnp 2>/dev/null | grep -q ":$port "; then
        print_error "Port $port is already in use"
        echo ""

        # Try to identify what's using the port
        echo "Diagnosing port conflict..."
        echo ""

        # Check if it's Docker (requires sudo for lsof, but we can detect docker-proxy)
        local docker_check=$(sudo lsof -i :$port 2>/dev/null | grep docker-proxy | wc -l)

        if [ "$docker_check" -gt 0 ]; then
            print_error "Port $port is in use by Docker containers (docker-proxy)"
            echo ""
            echo "You have two options:"
            echo "  1. Stop Docker deployment:"
            echo "     ./faultmaven.sh stop"
            echo ""
            echo "  2. Use a different port for process-based deployment:"
            echo "     - Edit .env and change PORT=$port to PORT=$((port+1))"
            echo "     - Then run: $0 start"
        else
            echo "Port $port is in use. Details:"
            ss -tlnp 2>/dev/null | grep ":$port " || true
            echo ""
            echo "To resolve this:"
            echo "  1. Check what's using the port: ss -tlnp | grep :$port"
            echo "  2. If it's a previous FaultMaven instance: $0 stop"
            echo "  3. Or use a different port in .env: PORT=$((port+1))"
        fi
        return 1
    fi
    return 0
}

wait_for_service() {
    local port=$1
    local pid=$2
    local max_wait=60
    local elapsed=0
    local check_interval=1

    print_info "Waiting for service to become ready..."

    while [ $elapsed -lt $max_wait ]; do
        # Check if process is still running
        if ! ps -p "$pid" > /dev/null 2>&1; then
            echo ""
            print_error "Process terminated unexpectedly"
            return 1
        fi

        # Check if service is responding on HTTP
        if curl -sf "http://localhost:$port/health" > /dev/null 2>&1; then
            # Service is responding, verify port ownership
            if ss -tlnp 2>/dev/null | grep ":$port " | grep -q "pid=$pid,"; then
                # Wait a bit more to ensure stability
                sleep 2
                # Final verification
                if curl -sf "http://localhost:$port/health" > /dev/null 2>&1; then
                    echo ""
                    return 0
                fi
            fi
        fi

        echo -n "."
        sleep $check_interval
        elapsed=$((elapsed + check_interval))
    done

    echo ""
    return 1
}

start_app() {
    print_header
    ensure_venv
    check_env

    local running_state=0
    is_running || running_state=$?

    # Extract port from .env (used by all branches below)
    local port=$(grep -E '^PORT=' .env 2>/dev/null | cut -d '=' -f2 | tr -d '"' | tr -d "'")
    port=${port:-$DEFAULT_PORT}

    if [ "$running_state" -eq 0 ]; then
        local existing_pid=$(cat "$PID_FILE")
        print_warning "FaultMaven is already running (PID $existing_pid)"

        echo ""
        print_info "Access points:"
        echo "  • API:      http://localhost:$port"
        echo "  • API Docs: http://localhost:$port/docs"
        echo "  • Logs:     tail -f $LOG_FILE"
        return 0
    elif [ "$running_state" -eq 2 ]; then
        # Port held but no PID file. Surface this clearly instead of falling
        # through to check_port_available, which would just say "Port in use"
        # without distinguishing it from a fresh conflict.
        print_warning "FaultMaven appears to be running but unmanaged — port $port held by PID $UNMANAGED_PORT_HOLDER (not started via $0)"
        echo ""
        echo "Process details:"
        ps -p "$UNMANAGED_PORT_HOLDER" -o pid,ppid,comm,args 2>/dev/null || true
        echo ""
        echo "Resolve before starting:"
        echo "  • If it's a previous FaultMaven instance: $0 stop"
        echo "  • If it's Docker: ./faultmaven.sh stop"
        echo "  • Or use a different port in .env: PORT=$((port+1))"
        exit 1
    fi

    # Not running — verify port is free before starting (covers a race where
    # something grabs the port between is_running and the bind).
    if ! check_port_available "$port"; then
        exit 1
    fi

    print_info "Starting FaultMaven API on port $port..."
    echo ""

    # Start uvicorn
    uvicorn faultmaven.main:app --host 0.0.0.0 --port "$port" > "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"

    # Wait for service to become ready and stable
    if wait_for_service "$port" "$pid"; then
        # Double-check port ownership after successful start
        if ss -tlnp 2>/dev/null | grep ":$port " | grep -q "pid=$pid,"; then
            print_success "FaultMaven started and ready (PID $pid)"
            echo ""
            print_info "Access points:"
            echo "  • API:      http://localhost:$port"
            echo "  • API Docs: http://localhost:$port/docs"
            echo "  • Logs:     tail -f $LOG_FILE"
        else
            print_error "Service started but not owned by our process (port conflict)"
            echo ""
            echo "Another service may have claimed the port during startup."
            kill "$pid" 2>/dev/null || true
            rm -f "$PID_FILE"
            exit 1
        fi
    else
        print_error "Failed to start FaultMaven - service did not become ready"
        echo ""

        # Check logs for specific error patterns
        if grep -q "address already in use" "$LOG_FILE" 2>/dev/null; then
            print_error "Port $port is already in use by another service"
            echo ""
            echo "Check what's using the port:"
            echo "  ss -tlnp | grep :$port"
            echo "  sudo lsof -i :$port"
        elif grep -q "error while attempting to bind" "$LOG_FILE" 2>/dev/null; then
            print_error "Failed to bind to port $port"
            tail -10 "$LOG_FILE" 2>/dev/null | grep -i "error" || true
        else
            echo "Possible issues:"
            echo "  • Port conflict (check if another service is using port $port)"
            echo "  • Startup error (check logs below)"
        fi

        echo ""
        echo "Last 20 lines of logs:"
        tail -20 "$LOG_FILE" 2>/dev/null || echo "  (log file not found)"
        echo ""

        # Clean up failed process
        if ps -p "$pid" > /dev/null 2>&1; then
            kill "$pid" 2>/dev/null || true
        fi
        rm -f "$PID_FILE"
        exit 1
    fi
}

stop_app() {
    print_header

    local stopped_any=false

    # Stop process tracked by PID file
    if is_running; then
        local pid=$(cat "$PID_FILE")
        print_info "Stopping FaultMaven (PID $pid)..."

        # Kill the main process and its children
        kill "$pid" 2>/dev/null || true
        sleep 2

        if ps -p "$pid" > /dev/null 2>&1; then
            print_warning "Process didn't stop gracefully, forcing..."
            kill -9 "$pid" 2>/dev/null || true
        fi

        # Kill any child processes (workers) that might still be running
        pkill -P "$pid" 2>/dev/null || true
        sleep 1

        rm -f "$PID_FILE"
        stopped_any=true
    fi

    # Also check for processes using the port (in case PID file is missing)
    local port=$(grep -E '^PORT=' .env 2>/dev/null | cut -d '=' -f2 | tr -d '"' | tr -d "'")
    port=${port:-$DEFAULT_PORT}

    # Extract PIDs from ss output (more reliable than lsof without sudo)
    local port_pids=$(ss -tlnp 2>/dev/null | grep ":$port " | grep -oP 'pid=\K[0-9]+' | sort -u | tr '\n' ' ')

    if [ -n "$port_pids" ]; then
        print_warning "Found process(es) still using port $port: $port_pids"
        print_info "Stopping process(es) on port $port..."

        # Kill all processes using the port
        for pid in $port_pids; do
            [ -z "$pid" ] && continue
            if ps -p "$pid" > /dev/null 2>&1; then
                kill "$pid" 2>/dev/null || true
            fi
        done

        sleep 2

        # Force kill if still running
        for pid in $port_pids; do
            [ -z "$pid" ] && continue
            if ps -p "$pid" > /dev/null 2>&1; then
                kill -9 "$pid" 2>/dev/null || true
            fi
        done

        stopped_any=true
    fi

    if [ "$stopped_any" = true ]; then
        print_success "FaultMaven stopped"
    else
        print_info "FaultMaven is not running"
    fi
}

restart_app() {
    print_header
    print_info "Restarting FaultMaven..."
    echo ""

    stop_app
    sleep 3
    start_app
}

health_check() {
    print_header
    echo "Running comprehensive health checks..."
    echo ""

    local port=$(grep -E '^PORT=' .env 2>/dev/null | cut -d '=' -f2 | tr -d '"' | tr -d "'")
    port=${port:-$DEFAULT_PORT}

    local failed=0

    # Check if process is running
    echo -n "Checking process... "
    local running_state=0
    is_running || running_state=$?

    if [ "$running_state" -eq 0 ]; then
        local pid=$(cat "$PID_FILE")
        print_success "Running (PID $pid)"
    elif [ "$running_state" -eq 2 ]; then
        print_warning "Running but unmanaged (port $port held by PID $UNMANAGED_PORT_HOLDER)"
        echo ""
        echo "This script didn't start the process holding the port. Likely causes:"
        echo "  • Stale uvicorn from a previous crash"
        echo "  • Docker-based FaultMaven (./faultmaven.sh)"
        echo "  • A manually-started instance"
        echo ""
        echo "Process details:"
        ps -p "$UNMANAGED_PORT_HOLDER" -o pid,ppid,comm,args 2>/dev/null || true
        echo ""
        echo "Reclaim with: $0 stop"
        exit 1
    else
        print_error "Not running"
        echo ""
        echo "Start it with: $0 start"
        exit 1
    fi

    # Check if port is listening (owned by our PID)
    local pid=$(cat "$PID_FILE")
    echo -n "Checking port $port ownership... "
    if ss -tlnp 2>/dev/null | grep ":$port " | grep -q "pid=$pid"; then
        print_success "Listening (owned by PID $pid)"
    else
        print_error "Process running but not listening on port $port"
        ((failed++))
        echo ""
        echo "This usually means:"
        echo "  • Port conflict during startup"
        echo "  • Service failed to bind"
        echo ""
        echo "Check logs: tail -f $LOG_FILE"
    fi

    echo ""
    echo "HTTP Endpoints:"
    echo "---------------"

    # Check health endpoint
    echo -n "API Health... "
    if curl -sf "http://localhost:$port/health" > /dev/null 2>&1; then
        print_success "OK (HTTP 200)"
    else
        print_error "FAILED"
        ((failed++))
    fi

    # Check docs endpoint
    echo -n "API Docs... "
    if curl -sf "http://localhost:$port/docs" > /dev/null 2>&1; then
        print_success "OK (HTTP 200)"
    else
        print_error "FAILED"
        ((failed++))
    fi

    # Check OpenAPI endpoint
    echo -n "OpenAPI Spec... "
    if curl -sf "http://localhost:$port/openapi.json" > /dev/null 2>&1; then
        print_success "OK (HTTP 200)"
    else
        print_error "FAILED"
        ((failed++))
    fi

    echo ""
    if [ $failed -eq 0 ]; then
        print_success "All health checks passed!"
        echo ""
        echo "Access points:"
        echo "  • API:      http://localhost:$port"
        echo "  • API Docs: http://localhost:$port/docs"
    else
        print_error "$failed check(s) failed"
        echo ""
        echo "Troubleshooting:"
        echo "  • Check logs: tail -f $LOG_FILE"
        echo "  • Restart:    $0 restart"
        exit 1
    fi
}

logs_app() {
    if [ ! -f "$LOG_FILE" ]; then
        print_error "Log file not found: $LOG_FILE"
        exit 1
    fi

    if is_running; then
        local pid=$(cat "$PID_FILE")
        print_success "FaultMaven is RUNNING (PID $pid)"
        print_info "Streaming live logs (Ctrl+C to exit)..."
        echo ""
        tail -f "$LOG_FILE"
    else
        print_warning "FaultMaven is NOT RUNNING"
        print_info "Showing last 50 lines of static logs..."
        echo ""
        tail -n 50 "$LOG_FILE"
    fi
}

run_tests() {
    print_header
    ensure_venv

    print_info "Running tests via scripts/tests.py..."
    echo ""

    # Pass all arguments to tests.py
    python scripts/tests.py "$@"
}

list_users() {
    # Check if API is running
    if ! is_running; then
        print_header
        print_error "FaultMaven API is not running. Run '$0 start' first."
        exit 1
    fi

    # Extract port
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

delete_user() {
    # Check if API is running
    if ! is_running; then
        print_header
        print_error "FaultMaven API is not running. Run '$0 start' first."
        exit 1
    fi

    # Extract port
    local port=$(grep -E '^PORT=' .env 2>/dev/null | cut -d '=' -f2 | tr -d '"' | tr -d "'")
    port=${port:-$DEFAULT_PORT}

    print_header

    # Prompt for username
    if [ -z "$1" ]; then
        read -p "Username to delete: " username
    else
        username="$1"
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

create_user() {
    # Check if API is running
    if ! is_running; then
        print_header
        print_error "FaultMaven API is not running. Run '$0 start' first."
        exit 1
    fi

    # Extract port
    local port=$(grep -E '^PORT=' .env 2>/dev/null | cut -d '=' -f2 | tr -d '"' | tr -d "'")
    port=${port:-$DEFAULT_PORT}

    # Check if API is healthy
    if ! curl -sf "http://localhost:$port/health" > /dev/null 2>&1; then
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
    read -p "Role (user/admin/platform_admin) [default: user]: " role_input

    role_input=${role_input:-user}
    role_input=$(echo "$role_input" | tr '[:upper:]' '[:lower:]')

    if [ "$role_input" != "admin" ] && [ "$role_input" != "user" ] && [ "$role_input" != "platform_admin" ]; then
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
    response=$(curl -s -w "\n%{http_code}" -X POST "http://localhost:$port/api/v1/auth/dev-register" \
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
        # dev-register always creates the account with the baseline 'user' role.
        # The two elevated roles are separate axes (ADR-012 D9) and are granted
        # by different paths, so name the right one for what was asked.
        if [ "$role_input" = "admin" ]; then
            echo "Note: dev-register created '$username' with the 'user' role."
            echo "  To grant the ORGANIZATION-scoped admin role (tenant-bounded):"
            echo "    POST /api/v1/admin/users/{user_id}/roles  with  {\"role\": \"admin\"}"
        elif [ "$role_input" = "platform_admin" ]; then
            echo "Note: dev-register created '$username' with the 'user' role."
            echo "  To grant the DEPLOYMENT operator role (cross-tenant reach), run:"
            echo "    fm-promote-platform-admin $username"
        fi
    elif [ "$http_code" = "409" ]; then
        echo ""
        print_error "User '$username' already exists"
        echo ""
        print_info "Use '$0 create-user' with a different username, or log in directly."
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

usage() {
    print_header
    echo "Usage: $0 <command> [options]"
    echo ""
    echo "Service Management:"
    echo "  start       Start FaultMaven API as local process"
    echo "  stop        Stop the API"
    echo "  restart     Restart the API"
    echo "  health      Run comprehensive health checks"
    echo "  logs        Stream application logs"
    echo ""
    echo "User Management:"
    echo "  create-user          Create a new user account"
    echo "  list-users           List all user accounts"
    echo "  delete-user [name]   Delete a user account"
    echo ""
    echo "Development:"
    echo "  test        Run tests (delegates to scripts/tests.py)"
    echo ""
    echo "Examples:"
    echo "  $0 start              # Start API"
    echo "  $0 create-user        # Create user account"
    echo "  $0 list-users         # List all users"
    echo "  $0 delete-user bob    # Delete user 'bob'"
    echo "  $0 health             # Check health"
    echo "  $0 logs               # View logs"
    echo "  $0 test --unit        # Run unit tests"
    echo ""
    echo "Alternative: Docker-based Local Deployment:"
    echo "  ./faultmaven.sh start # Use main script for containers"
    echo ""
}

#######################################
# Main
#######################################

case "${1:-}" in
    start)
        start_app
        ;;
    stop)
        stop_app
        ;;
    restart)
        restart_app
        ;;
    health)
        health_check
        ;;
    logs)
        logs_app
        ;;
    create-user)
        create_user
        ;;
    list-users)
        list_users
        ;;
    delete-user)
        delete_user "$2"
        ;;
    test)
        shift
        run_tests "$@"
        ;;
    status)
        # Redirect old status command to health
        print_warning "'status' command is deprecated. Use 'health' instead."
        echo ""
        health_check
        ;;
    help|--help|-h|"")
        usage
        ;;
    *)
        print_error "Unknown command: $1"
        echo ""
        usage
        exit 1
        ;;
esac
