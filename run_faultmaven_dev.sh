#!/bin/bash

# FaultMaven development runner
# This script runs FaultMaven in development mode with full transparency
# All warnings, errors, and debug information will be displayed

echo "🚀 Starting FaultMaven (development mode)..."
echo "============================================="

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo "❌ Virtual environment not found. Please run ./run_faultmaven.sh first"
    exit 1
fi

# Activate virtual environment
source .venv/bin/activate

echo "📁 Configuration loaded from .env file"
echo "🏃 Running FaultMaven on http://localhost:8000"
echo "📜 All warnings and errors will be shown for transparency"
echo "🔧 Use Ctrl+C to stop"
echo ""

# Run FaultMaven with full transparency
python -m faultmaven.main