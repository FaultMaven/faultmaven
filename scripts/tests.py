#!/usr/bin/env python3
"""CI/CD-ready test runner for FaultMaven.

This script provides a unified interface for running tests with support for:
- Multiple test categories (unit, integration, benchmark, load)
- CI/CD pipeline stages (fast, full, nightly)
- Coverage reporting
- Parallel execution
- Background execution for local development

Usage:
    # Run all fast tests (default for CI)
    python scripts/tests.py

    # Run specific categories
    python scripts/tests.py --unit
    python scripts/tests.py --integration
    python scripts/tests.py --benchmarks
    python scripts/tests.py --performance

    # CI/CD pipeline modes
    python scripts/tests.py --ci              # Fast tests only (unit + quick integration)
    python scripts/tests.py --ci-full         # Full test suite
    python scripts/tests.py --ci-nightly      # Full suite + benchmarks + slow tests

    # Run tests in background
    python scripts/tests.py --daemon

    # Check status of background tests
    python scripts/tests.py status

    # Stop background tests
    python scripts/tests.py stop
"""
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# Configuration
BASE_DIR = Path(__file__).parent.parent
METADATA_DIR = BASE_DIR / ".faultmaven"
PID_FILE = METADATA_DIR / "run_tests.pid"
LOG_FILE = METADATA_DIR / "tests.log"

# Test categories and their paths
TEST_CATEGORIES = {
    "unit": "tests/unit",
    "integration": "tests/integration",
    "infrastructure": "tests/infrastructure",
    "benchmarks": "tests/benchmarks",
    "performance": "tests/performance",
    "health": "tests/health",
}

# CI/CD pipeline configurations
CI_MODES = {
    "ci": {
        "description": "Fast tests for CI (unit tests only)",
        "paths": ["tests/unit"],
        "markers": "not slow and not enterprise",
        "parallel": True,
    },
    "ci-full": {
        "description": "Full test suite for CI",
        "paths": ["tests/unit", "tests/integration", "tests/infrastructure"],
        "markers": "not slow and not enterprise",
        "parallel": True,
    },
    "ci-nightly": {
        "description": "Complete test suite including slow tests",
        "paths": ["tests/"],
        "markers": "not enterprise",
        "parallel": False,  # Benchmarks may need sequential execution
        "env": {"RUN_PERFORMANCE_TESTS": "true"},
    },
    "ci-enterprise": {
        "description": "Enterprise tests requiring Redis, PostgreSQL",
        "paths": ["tests/"],
        "markers": "enterprise",
        "parallel": False,
    },
}


def ensure_metadata_dir():
    """Ensure the .faultmaven directory exists for PID and log files."""
    METADATA_DIR.mkdir(exist_ok=True)


def get_pytest_path() -> str:
    """Get the path to pytest from the virtual environment."""
    venv_pytest = BASE_DIR / ".venv" / "bin" / "pytest"
    return str(venv_pytest) if venv_pytest.exists() else "pytest"


def stop_tests():
    """Gracefully terminate a background test process."""
    if not PID_FILE.exists():
        print("No background tests are currently running.")
        return 0

    try:
        pid = int(PID_FILE.read_text().strip())
        print(f"Stopping test process (PID: {pid})...")
        os.kill(pid, signal.SIGTERM)

        # Give it a few seconds to clean up
        for _ in range(5):
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except OSError:
                print("Tests stopped successfully.")
                PID_FILE.unlink(missing_ok=True)
                return 0

        print("Process did not exit, force killing...")
        os.kill(pid, signal.SIGKILL)
        PID_FILE.unlink(missing_ok=True)
        return 0
    except (ValueError, ProcessLookupError, OSError):
        print("Could not find process. Cleaning up stale PID file.")
        PID_FILE.unlink(missing_ok=True)
        return 0


def status_tests():
    """Check status of running tests."""
    if not PID_FILE.exists():
        # Fallback: check if pytest process is running
        try:
            result = subprocess.run(
                ["pgrep", "-f", "pytest"], capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0 and result.stdout.strip():
                pid = result.stdout.strip().split("\n")[0]
                print(f"Tests are running (PID: {pid}, but PID file missing)")
                return 0
        except (
            subprocess.TimeoutExpired,
            FileNotFoundError,
            subprocess.SubprocessError,
        ):
            pass
        print("No tests currently running.")
        return 1

    try:
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, 0)  # Check if process exists
            print(f"Tests are running (PID: {pid})")
            if LOG_FILE.exists():
                print(f"Log file: {LOG_FILE}")
            return 0
        except OSError:
            print("PID file exists but process is not running. Cleaning up...")
            PID_FILE.unlink(missing_ok=True)
            return 1
    except (ValueError, OSError):
        print("Invalid PID file. Cleaning up...")
        PID_FILE.unlink(missing_ok=True)
        return 1


def build_pytest_command(args, unknown_args: list[str]) -> list[str]:
    """Build the pytest command based on arguments."""
    pytest_cmd = [get_pytest_path()]

    # Determine test paths
    test_paths: list[str] = []

    # Check for CI mode first
    ci_mode = None
    if args.ci:
        ci_mode = "ci"
    elif args.ci_full:
        ci_mode = "ci-full"
    elif args.ci_nightly:
        ci_mode = "ci-nightly"
    elif args.ci_enterprise:
        ci_mode = "ci-enterprise"

    if ci_mode:
        config = CI_MODES[ci_mode]
        test_paths = config["paths"]
        if config.get("markers"):
            pytest_cmd.extend(["-m", config["markers"]])
        if config.get("parallel") and not getattr(args, "no_parallel", False):
            try:
                import xdist  # noqa: F401

                pytest_cmd.extend(["-n", "auto"])
            except ImportError:
                print(
                    "Warning: pytest-xdist not installed. Install with: pip install pytest-xdist"
                )
                print("         Running tests sequentially instead.")
        # Set environment variables
        if config.get("env"):
            for key, value in config["env"].items():
                os.environ[key] = value
    else:
        # Individual category selection
        if args.unit:
            test_paths.append(TEST_CATEGORIES["unit"])
        if args.integration:
            test_paths.append(TEST_CATEGORIES["integration"])
        if args.infrastructure:
            test_paths.append(TEST_CATEGORIES["infrastructure"])
        if args.benchmarks:
            test_paths.append(TEST_CATEGORIES["benchmarks"])
            pytest_cmd.extend(["-m", "benchmark"])
            os.environ["RUN_PERFORMANCE_TESTS"] = "true"
        if args.performance:
            test_paths.append(TEST_CATEGORIES["performance"])
            os.environ["RUN_PERFORMANCE_TESTS"] = "true"
        if args.health:
            test_paths.append(TEST_CATEGORIES["health"])

        if args.directory:
            test_paths.append(args.directory)

    # Default to all tests if nothing specified
    if not test_paths:
        test_paths = ["tests/"]

    pytest_cmd.extend(test_paths)

    # Filtering options
    if args.keyword:
        pytest_cmd.extend(["-k", args.keyword])
    if args.marker:
        pytest_cmd.extend(["-m", args.marker])

    # Behavior options
    if args.fail_fast:
        pytest_cmd.append("-x")
    if args.verbose:
        pytest_cmd.append("-v")
    if args.quiet:
        pytest_cmd.append("-q")

    # Coverage options
    if args.coverage:
        pytest_cmd.extend(
            [
                "--cov=faultmaven",
                "--cov-report=term-missing",
                "--cov-report=xml:coverage.xml",
                "--cov-report=html:htmlcov",
            ]
        )
        if args.coverage_fail_under:
            pytest_cmd.append(f"--cov-fail-under={args.coverage_fail_under}")

    # Parallel execution (if not in CI mode that already handled it)
    if getattr(args, "parallel", False) and not ci_mode:
        try:
            import xdist  # noqa: F401

            jobs = getattr(args, "jobs", None)
            pytest_cmd.extend(["-n", str(jobs) if jobs else "auto"])
        except ImportError:
            print(
                "Warning: pytest-xdist not installed. Install with: pip install pytest-xdist"
            )
            print("         Running tests sequentially instead.")

    # JUnit XML output for CI
    if args.junit_xml:
        pytest_cmd.extend(["--junit-xml", args.junit_xml])

    # Pass through any additional pytest arguments
    pytest_cmd.extend(unknown_args)

    return pytest_cmd


def run_tests(args, unknown_args: list[str]) -> int:
    """Build and execute the pytest command."""
    ensure_metadata_dir()

    # Ensure SKIP_SERVICE_CHECKS is set for tests
    os.environ.setdefault("SKIP_SERVICE_CHECKS", "true")

    pytest_cmd = build_pytest_command(args, unknown_args)

    print(f"Executing: {' '.join(pytest_cmd)}")

    if args.daemon:
        # Daemon mode: run in background
        with open(LOG_FILE, "w") as log:
            proc = subprocess.Popen(
                pytest_cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=str(BASE_DIR),
            )
            PID_FILE.write_text(str(proc.pid))
            print(f"Tests running in background (PID: {proc.pid})")
            print(f"View logs: tail -f {LOG_FILE}")
        return 0
    else:
        # Foreground execution
        try:
            result = subprocess.run(pytest_cmd, cwd=str(BASE_DIR))
            return result.returncode
        except KeyboardInterrupt:
            print("\nTests interrupted by user.")
            return 130


def run_load_tests(args) -> int:
    """Run Locust load tests."""
    locustfile = BASE_DIR / "tests" / "load" / "locustfile.py"
    if not locustfile.exists():
        print(f"Error: Locust file not found at {locustfile}")
        return 1

    host = args.host or "http://localhost:8090"
    cmd = ["locust", "-f", str(locustfile), "--host", host]

    if args.headless:
        cmd.extend(
            [
                "--users",
                str(args.users or 10),
                "--spawn-rate",
                str(args.spawn_rate or 2),
                "--run-time",
                args.run_time or "60s",
                "--headless",
            ]
        )
        if args.csv:
            cmd.extend(["--csv", args.csv])

    print(f"Running load tests: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, cwd=str(BASE_DIR))
        return result.returncode
    except FileNotFoundError:
        print("Error: Locust not installed. Install with: pip install locust")
        return 1


def main():
    parser = argparse.ArgumentParser(
        description="FaultMaven Test Runner - CI/CD Ready",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                      Run default tests (unit + integration)
  %(prog)s --ci                 Run fast CI tests (unit only)
  %(prog)s --ci-full            Run full CI test suite
  %(prog)s --ci-nightly         Run nightly tests (includes slow/benchmarks)
  %(prog)s --unit --coverage    Run unit tests with coverage
  %(prog)s --integration -v     Run integration tests verbosely
  %(prog)s --benchmarks         Run performance benchmarks
  %(prog)s load --headless      Run load tests in headless mode
  %(prog)s status               Check if background tests are running
  %(prog)s stop                 Stop background tests
        """,
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # run command (default)
    run_parser = subparsers.add_parser("run", help="Run tests (default)")
    _add_test_arguments(run_parser)

    # status command
    subparsers.add_parser("status", help="Check background test status")

    # stop command
    subparsers.add_parser("stop", help="Stop background tests")

    # load command
    load_parser = subparsers.add_parser("load", help="Run load tests with Locust")
    load_parser.add_argument("--host", help="Target host URL")
    load_parser.add_argument(
        "--headless", action="store_true", help="Run without web UI"
    )
    load_parser.add_argument("--users", type=int, help="Number of users (default: 10)")
    load_parser.add_argument(
        "--spawn-rate", type=int, help="Users per second (default: 2)"
    )
    load_parser.add_argument("--run-time", help="Test duration (default: 60s)")
    load_parser.add_argument("--csv", help="CSV output prefix")

    # Add arguments to main parser as well for convenience
    _add_test_arguments(parser)

    args, unknown = parser.parse_known_args()

    # Handle commands
    if args.command == "status":
        return status_tests()
    elif args.command == "stop":
        return stop_tests()
    elif args.command == "load":
        return run_load_tests(args)
    else:
        # Default: run tests
        return run_tests(args, unknown)


def _add_test_arguments(parser):
    """Add common test arguments to a parser."""
    # CI/CD modes
    ci_group = parser.add_argument_group("CI/CD Pipeline Modes")
    ci_exclusive = ci_group.add_mutually_exclusive_group()
    ci_exclusive.add_argument(
        "--ci",
        action="store_true",
        help="Fast CI mode: unit tests only, parallel execution",
    )
    ci_exclusive.add_argument(
        "--ci-full",
        action="store_true",
        help="Full CI mode: unit + integration + infrastructure",
    )
    ci_exclusive.add_argument(
        "--ci-nightly",
        action="store_true",
        help="Nightly mode: all tests including benchmarks",
    )
    ci_exclusive.add_argument(
        "--ci-enterprise",
        action="store_true",
        help="Enterprise mode: tests requiring Redis, PostgreSQL",
    )

    # Test categories
    cat_group = parser.add_argument_group("Test Categories")
    cat_group.add_argument("--unit", action="store_true", help="Run unit tests")
    cat_group.add_argument(
        "--integration", action="store_true", help="Run integration tests"
    )
    cat_group.add_argument(
        "--infrastructure", action="store_true", help="Run infrastructure tests"
    )
    cat_group.add_argument(
        "--benchmarks", action="store_true", help="Run performance benchmarks"
    )
    cat_group.add_argument(
        "--performance", action="store_true", help="Run performance overhead tests"
    )
    cat_group.add_argument(
        "--health", action="store_true", help="Run health/smoke tests"
    )
    cat_group.add_argument(
        "--dir", "--directory", dest="directory", help="Run tests in specific directory"
    )

    # Filtering
    filter_group = parser.add_argument_group("Test Filtering")
    filter_group.add_argument(
        "-k", "--keyword", help="Filter tests by keyword expression"
    )
    filter_group.add_argument("-m", "--marker", help="Run tests with specific marker")

    # Execution options
    exec_group = parser.add_argument_group("Execution Options")
    exec_group.add_argument(
        "-x", "--fail-fast", action="store_true", help="Stop after first failure"
    )
    exec_group.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose output"
    )
    exec_group.add_argument("-q", "--quiet", action="store_true", help="Quiet output")
    exec_group.add_argument(
        "-d",
        "--daemon",
        action="store_true",
        help="Run in background (for CI/long test suites, not typical local dev)",
    )
    exec_group.add_argument(
        "-n",
        "--parallel",
        action="store_true",
        help="Run tests in parallel (requires pytest-xdist)",
    )
    exec_group.add_argument(
        "-j", "--jobs", type=int, help="Number of parallel workers (default: auto)"
    )
    exec_group.add_argument(
        "--no-parallel",
        action="store_true",
        help="Disable parallel execution even in CI modes",
    )

    # Coverage
    cov_group = parser.add_argument_group("Coverage Options")
    cov_group.add_argument(
        "--coverage", action="store_true", help="Generate coverage report"
    )
    cov_group.add_argument(
        "--coverage-fail-under", type=int, help="Fail if coverage is below threshold"
    )

    # CI output
    ci_output = parser.add_argument_group("CI Output")
    ci_output.add_argument("--junit-xml", help="Generate JUnit XML report")


if __name__ == "__main__":
    sys.exit(main())
