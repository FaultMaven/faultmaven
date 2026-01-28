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

is_running() {
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if ps -p "$pid" > /dev/null 2>&1; then
            return 0
        else
            rm -f "$PID_FILE"
            return 1
        fi
    fi
    return 1
}

check_port_available() {
    local port=$1
    if lsof -Pi :$port -sTCP:LISTEN -t >/dev/null 2>&1; then
        local pids=$(lsof -ti :$port | tr '\n' ' ')
        print_error "Port $port is already in use by process(es): $pids"
        echo ""
        echo "To resolve this:"
        echo "  1. Check what's using the port: lsof -i :$port"
        echo "  2. If it's a previous FaultMaven instance, stop it:"
        echo "     ./scripts/faultmaven-dev.sh stop"
        echo "  3. Or manually stop the process(es): kill $pids"
        return 1
    fi
    return 0
}

wait_for_service() {
    local port=$1
    local max_wait=30
    local elapsed=0
    local check_interval=1

    print_info "Waiting for service to become ready..."
    
    while [ $elapsed -lt $max_wait ]; do
        # Check if process is still running
        if ! is_running; then
            return 1
        fi

        # Check if service is responding
        if curl -sf "http://localhost:$port/health" > /dev/null 2>&1; then
            # Service is responding, wait a bit more to ensure stability
            sleep 2
            # Verify it's still responding after the stability check
            if curl -sf "http://localhost:$port/health" > /dev/null 2>&1; then
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

start_app() {
    print_header
    ensure_venv
    check_env

    if is_running; then
        local existing_pid=$(cat "$PID_FILE")
        print_warning "FaultMaven is already running (PID $existing_pid)"
        return 0
    fi

    # Extract port from .env
    local port=$(grep -E '^PORT=' .env 2>/dev/null | cut -d '=' -f2 | tr -d '"' | tr -d "'")
    port=${port:-$DEFAULT_PORT}

    # Check if port is available
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
    if wait_for_service "$port"; then
        print_success "FaultMaven started and ready (PID $pid)"
        echo ""
        print_info "Access points:"
        echo "  • API:      http://localhost:$port"
        echo "  • API Docs: http://localhost:$port/docs"
        echo "  • Logs:     tail -f $LOG_FILE"
    else
        print_error "Failed to start FaultMaven - service did not become ready"
        echo ""
        echo "Possible issues:"
        echo "  • Port conflict (check if another service is using port $port)"
        echo "  • Startup error (check logs below)"
        echo ""
        echo "Last 20 lines of logs:"
        tail -20 "$LOG_FILE" 2>/dev/null || echo "  (log file not found)"
        echo ""
        # Clean up failed process
        if is_running; then
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
    
    local port_pids=$(lsof -ti :$port 2>/dev/null || true)
    if [ -n "$port_pids" ]; then
        print_warning "Found process(es) still using port $port: $port_pids"
        print_info "Stopping process(es) on port $port..."
        
        # Kill all processes using the port
        echo "$port_pids" | while read -r pid; do
            [ -z "$pid" ] && continue
            kill "$pid" 2>/dev/null || true
        done
        
        sleep 2
        
        # Force kill if still running
        port_pids=$(lsof -ti :$port 2>/dev/null || true)
        if [ -n "$port_pids" ]; then
            echo "$port_pids" | while read -r pid; do
                [ -z "$pid" ] && continue
                kill -9 "$pid" 2>/dev/null || true
            done
        fi
        
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
    sleep 1
    start_app
}

status_app() {
    print_header

    if is_running; then
        local pid=$(cat "$PID_FILE")
        print_success "FaultMaven is running (PID $pid)"

        # Extract port
        local port=$(grep -E '^PORT=' .env 2>/dev/null | cut -d '=' -f2 | tr -d '"' | tr -d "'")
        port=${port:-$DEFAULT_PORT}

        echo ""
        print_info "Testing health endpoint..."

        if curl -sf "http://localhost:$port/health" > /dev/null 2>&1; then
            print_success "Health check passed"
        else
            print_error "Health check failed"
        fi
    else
        print_warning "FaultMaven is not running"
    fi
}

health_check() {
    print_header
    echo "Running health checks..."
    echo ""

    local port=$(grep -E '^PORT=' .env 2>/dev/null | cut -d '=' -f2 | tr -d '"' | tr -d "'")
    port=${port:-$DEFAULT_PORT}

    local failed=0

    # Check if process is running
    echo -n "Checking process... "
    if is_running; then
        print_success "Running (PID $(cat $PID_FILE))"
    else
        print_error "Not running"
        ((failed++))
        echo ""
        echo "Start it with: $0 start"
        exit 1
    fi

    # Check if port is listening
    echo -n "Checking port $port... "
    if lsof -Pi :$port -sTCP:LISTEN -t >/dev/null 2>&1; then
        print_success "Listening"
    else
        print_error "Not listening"
        ((failed++))
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
        if [ "$role_input" = "admin" ]; then
            echo "Note: To grant admin role, run:"
            echo "  python scripts/auth/promote_to_admin.py $username"
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
    echo "  status      Show service status"
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
    status)
        status_app
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
