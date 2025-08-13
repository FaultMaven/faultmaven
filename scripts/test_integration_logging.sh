#!/bin/bash

# FaultMaven Integration Logging Tests Runner
# This script provides multiple options for running integration logging tests

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

# Default values
SKIP_SERVICES=true
VERBOSE=false
COVERAGE=false
SPECIFIC_TEST=""

# Help function
show_help() {
    echo "FaultMaven Integration Logging Tests Runner"
    echo ""
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -h, --help              Show this help message"
    echo "  -s, --with-services     Run tests with live services (default: skip services)"
    echo "  -v, --verbose           Enable verbose output"
    echo "  -c, --coverage          Enable coverage reporting"
    echo "  -t, --test TEST_NAME    Run specific test by name"
    echo ""
    echo "Examples:"
    echo "  $0                      # Run all tests without services"
    echo "  $0 -s                   # Run all tests with live services"  
    echo "  $0 -v -c               # Run with verbose output and coverage"
    echo "  $0 -t test_error_propagation_logging  # Run specific test"
    echo ""
    echo "Environment Variables:"
    echo "  SKIP_SERVICE_CHECKS     Set to 'false' to require live services"
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            show_help
            exit 0
            ;;
        -s|--with-services)
            SKIP_SERVICES=false
            shift
            ;;
        -v|--verbose)
            VERBOSE=true
            shift
            ;;
        -c|--coverage)
            COVERAGE=true
            shift
            ;;
        -t|--test)
            SPECIFIC_TEST="$2"
            shift 2
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            show_help
            exit 1
            ;;
    esac
done

# Check if we're in the correct directory
if [[ ! -f "faultmaven/__init__.py" ]]; then
    echo -e "${RED}Error: Must run from FaultMaven project root${NC}"
    exit 1
fi

# Activate virtual environment if it exists
if [[ -f ".venv/bin/activate" ]]; then
    echo -e "${YELLOW}Activating virtual environment...${NC}"
    source .venv/bin/activate
else
    echo -e "${YELLOW}Warning: No virtual environment found${NC}"
fi

# Set environment variables
export SKIP_SERVICE_CHECKS=$SKIP_SERVICES

# Build pytest command
PYTEST_CMD="python -m pytest tests/integration/logging/"

if [[ "$VERBOSE" == true ]]; then
    PYTEST_CMD="$PYTEST_CMD -v"
fi

if [[ "$COVERAGE" == true ]]; then
    PYTEST_CMD="$PYTEST_CMD --cov=faultmaven.infrastructure.logging --cov-report=term-missing"
fi

if [[ -n "$SPECIFIC_TEST" ]]; then
    PYTEST_CMD="$PYTEST_CMD -k $SPECIFIC_TEST"
fi

# Check for required services if not skipping
if [[ "$SKIP_SERVICES" == false ]]; then
    echo -e "${YELLOW}Checking for required services...${NC}"
    
    # Check for backend API
    if ! curl -s http://localhost:8000/health >/dev/null 2>&1; then
        echo -e "${RED}Error: Backend API not running at http://localhost:8000${NC}"
        echo -e "${YELLOW}Start services with: docker-compose up -d${NC}"
        exit 1
    fi
    
    # Check for Redis
    if ! redis-cli -h localhost -p 6379 ping >/dev/null 2>&1; then
        echo -e "${RED}Error: Redis not running at localhost:6379${NC}"
        echo -e "${YELLOW}Start services with: docker-compose up -d${NC}"
        exit 1
    fi
    
    echo -e "${GREEN}All services are available${NC}"
else
    echo -e "${YELLOW}Skipping service checks (SKIP_SERVICE_CHECKS=$SKIP_SERVICES)${NC}"
fi

# Print command being executed
echo -e "${YELLOW}Running: $PYTEST_CMD${NC}"
echo ""

# Run tests
if eval $PYTEST_CMD; then
    echo ""
    echo -e "${GREEN}✅ Integration logging tests completed successfully!${NC}"
    exit 0
else
    echo ""
    echo -e "${RED}❌ Integration logging tests failed${NC}"
    exit 1
fi