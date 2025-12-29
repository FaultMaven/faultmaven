"""Load testing scripts for FaultMaven.

This package contains Locust-based load testing scripts for
stress testing the FaultMaven API.

Usage:
    # Run with UI
    locust -f tests/load/locustfile.py --host=http://localhost:8000

    # Headless mode
    locust -f tests/load/locustfile.py \
           --host=http://localhost:8000 \
           --users 50 \
           --spawn-rate 10 \
           --run-time 60s \
           --headless

    # Using the runner script
    ./scripts/run_load_tests.sh local
"""
