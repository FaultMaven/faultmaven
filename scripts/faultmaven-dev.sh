#!/bin/bash
# FaultMaven Local Development Script
# Manages FaultMaven API as a local Python process (no Docker)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# Configuration
APP_NAME="FaultMaven"
PID_FILE=".faultmaven-dev.pid"
LOG_FILE="/tmp/faultmaven-dev.log"
DEFAULT_PORT=8000
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
    echo -e "${BLUE}║${NC}  FaultMaven Local Development         ${BLUE}║${NC}"
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

start_app() {
    print_header
    ensure_venv
    check_env

    if is_running; then
        print_warning "FaultMaven is already running (PID $(cat $PID_FILE))"
        return 0
    fi

    # Extract port from .env
    local port=$(grep -E '^PORT=' .env 2>/dev/null | cut -d '=' -f2 | tr -d '"' | tr -d "'")
    port=${port:-$DEFAULT_PORT}

    print_info "Starting FaultMaven API on port $port..."
    echo ""

    # Start uvicorn
    uvicorn faultmaven.main:app --host 0.0.0.0 --port "$port" > "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"

    # Wait for startup
    sleep 3

    if is_running; then
        print_success "FaultMaven started (PID $pid)"
        echo ""
        print_info "Access points:"
        echo "  • API:      http://localhost:$port"
        echo "  • API Docs: http://localhost:$port/docs"
        echo "  • Logs:     tail -f $LOG_FILE"
    else
        print_error "Failed to start FaultMaven"
        echo "Check logs: cat $LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
}

stop_app() {
    print_header

    if ! is_running; then
        print_info "FaultMaven is not running"
        return 0
    fi

    local pid=$(cat "$PID_FILE")
    print_info "Stopping FaultMaven (PID $pid)..."

    kill "$pid" 2>/dev/null || true
    sleep 2

    if ps -p "$pid" > /dev/null 2>&1; then
        print_warning "Process didn't stop gracefully, forcing..."
        kill -9 "$pid" 2>/dev/null || true
    fi

    rm -f "$PID_FILE"
    print_success "FaultMaven stopped"
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

    print_info "Streaming logs (Ctrl+C to exit)..."
    echo ""
    tail -f "$LOG_FILE"
}

run_tests() {
    print_header
    ensure_venv

    print_info "Running tests via scripts/tests.py..."
    echo ""

    # Pass all arguments to tests.py
    python scripts/tests.py "$@"
}

usage() {
    print_header
    echo "Usage: $0 <command> [options]"
    echo ""
    echo "Commands:"
    echo "  start       Start FaultMaven API as local process"
    echo "  stop        Stop the API"
    echo "  restart     Restart the API"
    echo "  status      Show service status"
    echo "  health      Run comprehensive health checks"
    echo "  logs        Stream application logs"
    echo "  test        Run tests (delegates to scripts/tests.py)"
    echo ""
    echo "Examples:"
    echo "  $0 start              # Start API"
    echo "  $0 health             # Check health"
    echo "  $0 logs               # View logs"
    echo "  $0 test --unit        # Run unit tests"
    echo ""
    echo "For Docker deployment:"
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
