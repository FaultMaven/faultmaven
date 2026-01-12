#!/bin/bash

# Configuration
APP_NAME="FaultMaven"
PID_FILE=".faultmaven.pid"
LOG_FILE="/tmp/faultmaven-live.log"
DEFAULT_PORT=8000
DEFAULT_HOST="0.0.0.0"

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

usage() {
    echo -e "${YELLOW}Usage: $0 {start|stop|restart|status|test} [options]${NC}"
    echo ""
    echo "Commands:"
    echo "  start      Start FaultMaven services"
    echo "  stop       Stop services"
    echo "  restart    Restart services"
    echo "  status     Show service status"
    echo "  test       Run tests (delegates to scripts/tests.py)"
    echo ""
    echo "Service options:"
    echo "  -d, --daemon    Run in background"
    echo "  --port PORT     Bind port (default: 8000)"
    echo "  --host HOST     Bind host (default: 0.0.0.0)"
    echo ""
    echo "Test options (see 'scripts/tests.py --help' for full list):"
    echo "  --unit           Run unit tests only"
    echo "  --integration    Run integration tests only"
    echo "  -k, --keyword    Filter tests by keyword"
    echo "  -m, --marker     Run tests with specific marker"
    echo "  --dir            Run tests in specific directory"
    echo "  --coverage       Run with coverage report"
    echo "  --parallel       Run in parallel"
    echo "  --fail-fast      Stop after first failure"
    echo "  -v, --verbose    Verbose output"
    exit 1
}

# Fail-safe check for virtual environment
ensure_venv() {
    if [[ -z "$VIRTUAL_ENV" ]]; then
        if [[ -d ".venv" ]]; then
            echo -e "${YELLOW}Automatically activating virtual environment...${NC}"
            source .venv/bin/activate
        else
            echo -e "${RED}❌ Error: Virtual environment (.venv) not found and not active.${NC}"
            echo -e "${YELLOW}Please create it first: python -m venv .venv${NC}"
            exit 1
        fi
    fi
}

check_env() {
    if [ ! -d ".venv" ]; then
        echo -e "${RED}❌ Virtual environment not found. Run 'python -m venv .venv' first.${NC}"
        exit 1
    fi
    if [ ! -f ".env" ]; then
        echo -e "${YELLOW}⚠️  .env file not found. Copying from .env.example...${NC}"
        if [ -f ".env.example" ]; then
            cp .env.example .env
        else
            echo -e "${RED}❌ .env.example not found. Please create .env file.${NC}"
            exit 1
        fi
    fi
}

start_app() {
    local daemon=$1
    check_env

    # 1. Extract HOST and PORT from .env if present (without sourcing entire file)
    # Use grep to safely extract only these values, avoiding JSON arrays and complex values
    ENV_HOST=$(grep -E '^HOST=' .env 2>/dev/null | cut -d '=' -f2- | tr -d '"' | tr -d "'" | head -1)
    ENV_PORT=$(grep -E '^PORT=' .env 2>/dev/null | cut -d '=' -f2- | tr -d '"' | tr -d "'" | head -1)

    # 2. CLI arguments override .env, which override defaults
    HOST=${CLI_HOST:-${ENV_HOST:-$DEFAULT_HOST}}
    PORT=${CLI_PORT:-${ENV_PORT:-$DEFAULT_PORT}}

    # Export for use by Python application (these will override .env values)
    export HOST
    export PORT

    # 3. Check if port is already in use
    if command -v lsof >/dev/null 2>&1 && lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null 2>&1; then
        echo -e "${RED}❌ Port $PORT is already in use by another process.${NC}"
        exit 1
    fi

    # 4. Check if FaultMaven is already running
    PID=$(cat "$PID_FILE" 2>/dev/null || pgrep -f "faultmaven.main" 2>/dev/null | head -1)
    if [ ! -z "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        echo -e "${YELLOW}⚠️  $APP_NAME is already running (PID: $PID).${NC}"
        exit 1
    fi

    # Activate virtual environment
    source .venv/bin/activate

    echo -e "${GREEN}🚀 Starting $APP_NAME on $HOST:$PORT...${NC}"
    
    if [ "$daemon" = true ]; then
        # Daemon mode: run in background with output redirected
        nohup python -m faultmaven.main > "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        echo -e "${GREEN}✅ Started in background (PID: $!, Log: $LOG_FILE).${NC}"
        echo -e "${GREEN}👀 View logs: tail -f $LOG_FILE${NC}"
    else
        # Foreground mode: run in background but wait for it (allows signal trapping)
        echo -e "${GREEN}🔧 Running in foreground. Use Ctrl+C to stop.${NC}"
        # Trap signals for graceful shutdown
        trap "echo -e '\n${YELLOW}🛑 Shutting down...${NC}'; kill $PID 2>/dev/null; rm -f '$PID_FILE'; exit" SIGINT SIGTERM
        # Run in background but capture PID immediately
        python -m faultmaven.main &
        PID=$!
        echo $PID > "$PID_FILE"
        # Wait for the process (foreground behavior)
        wait $PID
        # Cleanup on exit
        rm -f "$PID_FILE"
    fi
}

stop_app() {
    # Try PID file first
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE" 2>/dev/null)
        if [ ! -z "$PID" ] && kill -0 "$PID" 2>/dev/null; then
            echo -e "${YELLOW}🛑 Stopping $APP_NAME (PID: $PID)...${NC}"
            kill -TERM "$PID" 2>/dev/null

            # Wait for graceful shutdown
            for i in {1..5}; do
                if ! kill -0 "$PID" 2>/dev/null; then
                    echo -e "${GREEN}✅ Stopped gracefully.${NC}"
                    rm -f "$PID_FILE"
                    return
                fi
                sleep 1
            done

            echo -e "${RED}❌ Force killing...${NC}"
            kill -9 "$PID" 2>/dev/null
            rm -f "$PID_FILE"
            return
        fi
    fi

    # Fallback: search by process name
    PID=$(pgrep -f "faultmaven.main" 2>/dev/null | head -1)
    if [ -z "$PID" ]; then
        echo -e "${YELLOW}ℹ️  $APP_NAME is not running.${NC}"
        rm -f "$PID_FILE"
        return
    fi

    echo -e "${YELLOW}🛑 Stopping $APP_NAME (PID: $PID)...${NC}"
    kill -TERM "$PID" 2>/dev/null

    # Wait for graceful shutdown
    for i in {1..5}; do
        if ! kill -0 "$PID" 2>/dev/null; then
            echo -e "${GREEN}✅ Stopped gracefully.${NC}"
            rm -f "$PID_FILE"
            return
        fi
        sleep 1
    done

    echo -e "${RED}❌ Force killing...${NC}"
    kill -9 "$PID" 2>/dev/null
    rm -f "$PID_FILE"
}

status_app() {
    # Check PID file first
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE" 2>/dev/null)
        if [ ! -z "$PID" ] && kill -0 "$PID" 2>/dev/null; then
            echo -e "${GREEN}✅ $APP_NAME is running (PID: $PID).${NC}"
            return
        fi
    fi

    # Fallback: search by process name
    PID=$(pgrep -f "faultmaven.main" 2>/dev/null | head -1)
    if [ ! -z "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        echo -e "${GREEN}✅ $APP_NAME is running (PID: $PID).${NC}"
        echo -e "${YELLOW}⚠️  PID file missing or stale.${NC}"
    else
        echo -e "${RED}❌ $APP_NAME is not running.${NC}"
        rm -f "$PID_FILE"
    fi
}

# Main Execution
if [[ $# -lt 1 ]]; then
    usage
fi

# Ensure virtual environment is active (fail-safe check)
ensure_venv

COMMAND=$1
shift

case "$COMMAND" in
    start|stop|restart|status)
        # Service management commands
        DAEMON=false
        CLI_HOST=""
        CLI_PORT=""

        while [[ "$#" -gt 0 ]]; do
            case $1 in
                -d|--daemon) DAEMON=true ;;
                --port)
                    if [ -z "$2" ] || [[ "$2" =~ ^- ]]; then
                        echo -e "${RED}❌ --port requires a port number${NC}"
                        usage
                    fi
                    CLI_PORT="$2"
                    shift
                    ;;
                --host)
                    if [ -z "$2" ] || [[ "$2" =~ ^- ]]; then
                        echo -e "${RED}❌ --host requires a host address${NC}"
                        usage
                    fi
                    CLI_HOST="$2"
                    shift
                    ;;
                *) usage ;;
            esac
            shift
        done

        case "$COMMAND" in
            start)   start_app $DAEMON ;;
            stop)    stop_app ;;
            restart) stop_app; sleep 2; start_app $DAEMON ;;
            status)  status_app ;;
        esac
        ;;
    test)
        # Test command: delegate to scripts/tests.py
        echo -e "${GREEN}🧪 Running FaultMaven Test Suite...${NC}"
        if [ ! -f "scripts/tests.py" ]; then
            echo -e "${RED}❌ scripts/tests.py not found.${NC}"
            exit 1
        fi
        # Pass all remaining arguments to scripts/tests.py
        python3 scripts/tests.py run "$@"
        ;;
    *)
        usage
        ;;
esac
