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
    echo -e "${YELLOW}Usage: $0 {start|stop|restart|status} [-d|--daemon] [--port PORT] [--host HOST]${NC}"
    exit 1
}

check_env() {
    # Check if virtual environment exists
    if [ ! -d ".venv" ]; then
        echo -e "${RED}❌ Virtual environment not found.${NC}"
        exit 1
    fi
    # Check if .env file exists
    if [ ! -f ".env" ]; then
        echo -e "${RED}❌ .env file not found.${NC}"
        exit 1
    fi
}

start_app() {
    local daemon=$1
    local host=${HOST:-$DEFAULT_HOST}
    local port=${PORT:-$DEFAULT_PORT}

    check_env

    # Detect if FaultMaven is already running via PID file or pgrep
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        echo -e "${YELLOW}⚠️  $APP_NAME is already running (PID: $(cat $PID_FILE)).${NC}"
        exit 1
    fi

    # Check if already running via process name
    EXISTING_PID=$(pgrep -f "faultmaven.main" 2>/dev/null | head -1)
    if [ ! -z "$EXISTING_PID" ]; then
        echo -e "${YELLOW}⚠️  $APP_NAME is already running (PID: $EXISTING_PID).${NC}"
        exit 1
    fi

    # Activate virtual environment
    source .venv/bin/activate

    # Export HOST and PORT before loading .env (so they can override .env values)
    export HOST="$host"
    export PORT="$port"
    
    # Load environment variables from .env file (HOST and PORT may override .env)
    export $(grep -v '^#' .env | grep -v '^$' | xargs)

    echo -e "${GREEN}🚀 Starting $APP_NAME on $HOST:$PORT...${NC}"
    
    if [ "$daemon" = true ]; then
        # Daemon mode: run in background with output redirected
        nohup python -m faultmaven.main > "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        echo -e "${GREEN}✅ Started in background (PID: $!, Log: $LOG_FILE).${NC}"
        echo -e "${GREEN}👀 View logs: tail -f $LOG_FILE${NC}"
    else
        # Foreground mode: run directly (not in background)
        echo -e "${GREEN}🔧 Running in foreground. Use Ctrl+C to stop.${NC}"
        # Trap signals for graceful shutdown
        trap "echo -e '\n${YELLOW}🛑 Shutting down...${NC}'; rm -f '$PID_FILE'; exit" SIGINT SIGTERM
        echo $$ > "$PID_FILE"
        # Run directly (not with &)
        python -m faultmaven.main
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

# Parse Arguments
ACTION=$1
shift
DAEMON=false

while [[ "$#" -gt 0 ]]; do
    case $1 in
        -d|--daemon) DAEMON=true ;;
        --port) export PORT="$2"; shift ;;
        --host) export HOST="$2"; shift ;;
        *) usage ;;
    esac
    shift
done

case "$ACTION" in
    start)   start_app $DAEMON ;;
    stop)    stop_app ;;
    restart) stop_app; sleep 2; start_app $DAEMON ;;
    status)  status_app ;;
    *)       usage ;;
esac
