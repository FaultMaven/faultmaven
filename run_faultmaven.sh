#!/bin/bash

# Enhanced FaultMaven runner script with process management
# This script activates the virtual environment and runs FaultMaven
# All configuration is loaded from .env file automatically
#
# The system will automatically initialize available LLM providers
# based on API keys found in .env (fireworks, openai, local, etc.)
#
# Features:
# - Detects if FaultMaven is already running
# - Offers to restart running instances
# - Prevents port conflicts

echo "🚀 Starting FaultMaven..."
echo "=========================="

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo "❌ Virtual environment not found. Please run:"
    echo "   python -m venv .venv"
    echo "   source .venv/bin/activate"
    echo "   pip install -r requirements.txt"
    exit 1
fi

# Check if .env file exists
if [ ! -f ".env" ]; then
    echo "❌ .env file not found. Please create one based on .env.example"
    exit 1
fi

# Check if FaultMaven is already running
EXISTING_PID=$(pgrep -f "faultmaven.main" 2>/dev/null)
if [ ! -z "$EXISTING_PID" ]; then
    echo "⚠️  FaultMaven is already running (PID: $EXISTING_PID)"

    # Check if running interactively (not in background)
    if [ -t 0 ]; then
        echo "🔄 Do you want to restart it? [y/N]: "
        read -r response
        case $response in
            [yY][eE][sS]|[yY])
                echo "🛑 Stopping existing FaultMaven instance..."
                pkill -f "faultmaven.main"
                sleep 3

                # Verify it stopped
                if pgrep -f "faultmaven.main" > /dev/null; then
                    echo "❌ Failed to stop existing instance. Force killing..."
                    pkill -9 -f "faultmaven.main"
                    sleep 2
                fi
                echo "✅ Existing instance stopped"
                ;;
            *)
                echo "ℹ️  Keeping existing instance running"
                echo "📄 Logs: tail -f /tmp/faultmaven-live.log"
                echo "🛑 To stop: pkill -f 'faultmaven.main'"
                exit 0
                ;;
        esac
    else
        # Running non-interactively (e.g., from automation)
        echo "🔄 Non-interactive mode: automatically restarting..."
        echo "🛑 Stopping existing FaultMaven instance..."
        pkill -f "faultmaven.main"
        sleep 3

        # Verify it stopped
        if pgrep -f "faultmaven.main" > /dev/null; then
            echo "❌ Failed to stop existing instance. Force killing..."
            pkill -9 -f "faultmaven.main"
            sleep 2
        fi
        echo "✅ Existing instance stopped"
    fi
fi

# Double-check port availability
if command -v netstat >/dev/null 2>&1; then
    if netstat -ln 2>/dev/null | grep -q ":8000 "; then
        echo "⚠️  Port 8000 is still in use. Waiting for it to be released..."
        sleep 2
        if netstat -ln 2>/dev/null | grep -q ":8000 "; then
            echo "❌ Port 8000 is still occupied. Please check manually:"
            if command -v lsof >/dev/null 2>&1; then
                lsof -i:8000
            fi
            exit 1
        fi
    fi
fi

# Activate virtual environment
source .venv/bin/activate

# Load environment variables from .env file
echo "📁 Loading configuration from .env file..."
export $(cat .env | grep -v '^#' | grep -v '^$' | xargs)
echo "✅ Environment variables loaded"

# Run FaultMaven
echo "📁 Configuration loaded from .env file"
echo "🏃 Running FaultMaven on http://localhost:8000"
echo "📜 Provider initialization logs will be shown"
echo "⚠️  All warnings and errors will be displayed"
echo "🔧 Use Ctrl+C to stop"
echo ""
python -m faultmaven.main > /tmp/faultmaven-live.log 2>&1 &
echo "🎯 FaultMaven started in background, PID: $!"
echo "📄 Logs are being written to: /tmp/faultmaven-live.log"
echo "👀 To view live logs: tail -f /tmp/faultmaven-live.log"
echo "🛑 To stop: pkill -f 'faultmaven.main'"