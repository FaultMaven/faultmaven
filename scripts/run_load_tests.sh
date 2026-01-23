#!/bin/bash
# Run load tests against FaultMaven API
#
# Usage:
#   ./scripts/run_load_tests.sh [environment]
#
# Arguments:
#   environment - local (default), staging
#
# Examples:
#   ./scripts/run_load_tests.sh           # Run against localhost
#   ./scripts/run_load_tests.sh local     # Run against localhost
#   ./scripts/run_load_tests.sh staging   # Run against staging

set -e

ENVIRONMENT=${1:-local}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
RESULTS_DIR="$PROJECT_ROOT/benchmark_results/$(date +%Y%m%d_%H%M%S)"

# Configuration per environment
case $ENVIRONMENT in
    local)
        HOST="http://localhost:8090"
        USERS=10
        SPAWN_RATE=2
        RUN_TIME="60s"
        ;;
    staging)
        HOST="${STAGING_HOST:-https://staging.faultmaven.com}"
        USERS=50
        SPAWN_RATE=10
        RUN_TIME="300s"
        ;;
    production)
        echo "Production load testing disabled by default"
        echo "Edit this script to enable (USE WITH CAUTION)"
        exit 1
        ;;
    *)
        echo "Unknown environment: $ENVIRONMENT"
        echo "Usage: $0 [local|staging]"
        exit 1
        ;;
esac

echo "========================================"
echo "FaultMaven Load Test"
echo "========================================"
echo ""
echo "Environment: $ENVIRONMENT"
echo "Host:        $HOST"
echo "Users:       $USERS"
echo "Spawn Rate:  $SPAWN_RATE users/sec"
echo "Duration:    $RUN_TIME"
echo ""

# Create results directory
mkdir -p "$RESULTS_DIR"
echo "Results will be saved to: $RESULTS_DIR"
echo ""

# Check if locust is installed
if ! command -v locust &> /dev/null; then
    echo "Error: locust is not installed"
    echo "Install with: pip install locust"
    exit 1
fi

# Check if locustfile exists
LOCUSTFILE="$PROJECT_ROOT/tests/load/locustfile.py"
if [[ ! -f "$LOCUSTFILE" ]]; then
    echo "Error: Locust file not found at $LOCUSTFILE"
    exit 1
fi

echo "Starting load test..."
echo ""

# Run locust in headless mode
locust -f "$LOCUSTFILE" \
    --host="$HOST" \
    --users="$USERS" \
    --spawn-rate="$SPAWN_RATE" \
    --run-time="$RUN_TIME" \
    --headless \
    --html="$RESULTS_DIR/report.html" \
    --csv="$RESULTS_DIR/results" \
    --only-summary \
    2>&1 | tee "$RESULTS_DIR/output.log"

EXIT_CODE=${PIPESTATUS[0]}

echo ""
echo "========================================"
echo "Load Test Complete"
echo "========================================"
echo ""
echo "Results saved to:"
echo "  - HTML Report: $RESULTS_DIR/report.html"
echo "  - CSV Stats:   $RESULTS_DIR/results_stats.csv"
echo "  - Output Log:  $RESULTS_DIR/output.log"
echo ""

if [[ $EXIT_CODE -eq 0 ]]; then
    echo "Load test completed successfully"
else
    echo "Load test failed with exit code: $EXIT_CODE"
fi

exit $EXIT_CODE
