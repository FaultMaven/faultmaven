#!/bin/bash

# Simple FaultMaven runner script
# This script activates the virtual environment and runs FaultMaven
# All configuration is loaded from .env file automatically
# 
# The system will automatically initialize available LLM providers
# based on API keys found in .env (fireworks, openai, local, etc.)

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
python -m faultmaven.main