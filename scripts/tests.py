#!/usr/bin/env python3
"""Improved Test runner script for FaultMaven backend."""
import argparse
import subprocess
import sys
import os
import signal
import time
from pathlib import Path

# Configuration: Using a dedicated hidden directory for process metadata
# This makes the root cleaner and the PID file less fragile.
BASE_DIR = Path(__file__).parent.parent  # Go up from scripts/ to project root
METADATA_DIR = BASE_DIR / ".faultmaven"
PID_FILE = METADATA_DIR / "run_tests.pid"
LOG_FILE = METADATA_DIR / "tests.log"

def ensure_metadata_dir():
    """Ensure the .faultmaven directory exists for PID and log files."""
    METADATA_DIR.mkdir(exist_ok=True)

def get_pytest_path():
    """Ensure we use the pytest from the virtual environment."""
    venv_pytest = BASE_DIR / ".venv" / "bin" / "pytest"
    return str(venv_pytest) if venv_pytest.exists() else "pytest"

def stop_tests():
    """Gracefully terminate a background test process."""
    if not PID_FILE.exists():
        print("ℹ️  No background tests are currently running.")
        return

    try:
        pid = int(PID_FILE.read_text().strip())
        print(f"🛑 Stopping test process (PID: {pid})...")
        os.kill(pid, signal.SIGTERM)
        
        # Give it a few seconds to clean up
        for _ in range(5):
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except OSError:
                print("✅ Tests stopped successfully.")
                PID_FILE.unlink(missing_ok=True)
                return
        
        print("⚠️  Process did not exit, force killing...")
        os.kill(pid, signal.SIGKILL)
        PID_FILE.unlink(missing_ok=True)
    except (ValueError, ProcessLookupError, OSError):
        print("⚠️  Could not find process. Cleaning up stale PID file.")
        PID_FILE.unlink(missing_ok=True)

def status_tests():
    """Check status of running tests."""
    if not PID_FILE.exists():
        # Fallback: check if pytest process is running (PID file missing)
        try:
            result = subprocess.run(
                ["pgrep", "-f", "pytest"],
                capture_output=True,
                text=True,
                timeout=2
            )
            if result.returncode == 0 and result.stdout.strip():
                pid = result.stdout.strip().split('\n')[0]
                print(f"✅ Tests are running (PID: {pid}, but PID file missing)")
                return
        except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
            pass
        print("❌ No tests currently running.")
        return
    
    try:
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, 0)  # Check if process exists
            print(f"✅ Tests are running (PID: {pid})")
        except OSError:
            print("❌ PID file exists but process is not running. Cleaning up...")
            PID_FILE.unlink(missing_ok=True)
    except (ValueError, OSError):
        print("❌ Invalid PID file. Cleaning up...")
        PID_FILE.unlink(missing_ok=True)

def run_tests(args, unknown_args):
    """Build and execute the pytest command."""
    if args.action == "stop":
        stop_tests()
        return

    ensure_metadata_dir()
    pytest_cmd = [get_pytest_path()]

    # 1. Scope filtering
    if args.unit:
        pytest_cmd.append("tests/unit")
    elif args.integration:
        pytest_cmd.append("tests/integration")
    elif args.directory:
        pytest_cmd.append(args.directory)
    else:
        pytest_cmd.append("tests/")

    # 2. Logic flags
    if args.keyword:
        pytest_cmd.extend(["-k", args.keyword])
    if args.marker:
        pytest_cmd.extend(["-m", args.marker])
    if args.fail_fast:
        pytest_cmd.append("-x")
    if args.verbose:
        pytest_cmd.append("-v")
    
    # 3. Advanced Features
    if args.coverage:
        pytest_cmd.extend([
            "--cov=faultmaven",
            "--cov-report=term-missing",
            "--cov-report=xml"
        ])
    
    if args.parallel:
        # Explicit dependency check for pytest-xdist
        try:
            import xdist  # noqa: F401
            pytest_cmd.extend(["-n", "auto"])
        except ImportError:
            print("❌ Error: 'pytest-xdist' is required for parallel execution.")
            print("Please install it via: pip install pytest-xdist")
            sys.exit(1)

    # 4. Pass through any other raw pytest arguments
    pytest_cmd.extend(unknown_args)

    print(f"🚀 Executing: {' '.join(pytest_cmd)}")

    if args.daemon:
        # Daemon mode: intended for local dev convenience, not CI
        with open(LOG_FILE, "w") as log:
            proc = subprocess.Popen(
                pytest_cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True
            )
            PID_FILE.write_text(str(proc.pid))
            print(f"✅ Tests running in background (PID: {proc.pid})")
            print(f"👀 View logs: tail -f {LOG_FILE}")
    else:
        # Foreground execution
        try:
            result = subprocess.run(pytest_cmd)
            sys.exit(result.returncode)
        except KeyboardInterrupt:
            print("\n🛑 Tests interrupted by user.")
            sys.exit(1)

def main():
    # Clearer help output for default action
    parser = argparse.ArgumentParser(
        description="FaultMaven Test Runner (Default action: run)",
        add_help=False
    )
    
    # Positional action
    parser.add_argument(
        "action",
        choices=["run", "stop", "status"],
        default="run",
        nargs="?",
        help="Action to perform (default: run)"
    )
    
    # Grouping for better help UI
    scope = parser.add_argument_group("Test Scope")
    scope.add_argument(
        "--unit",
        action="store_true",
        help="Run only unit tests"
    )
    scope.add_argument(
        "--integration",
        action="store_true",
        help="Run only integration tests"
    )
    scope.add_argument(
        "--dir", "--directory",
        type=str,
        dest="directory",
        help="Run tests in a specific directory"
    )

    filters = parser.add_argument_group("Filtering & Logic")
    filters.add_argument(
        "-k", "--keyword",
        help="Only run tests matching keyword"
    )
    filters.add_argument(
        "-m", "--marker",
        help="Only run tests with specific marker"
    )
    filters.add_argument(
        "--fail-fast",
        action="store_true",
        dest="fail_fast",
        help="Stop after first failure"
    )

    output = parser.add_argument_group("Output & Reporting")
    output.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output"
    )
    output.add_argument(
        "--coverage",
        action="store_true",
        help="Generate coverage report"
    )
    output.add_argument(
        "--parallel",
        action="store_true",
        help="Run tests in parallel"
    )
    output.add_argument(
        "-d", "--daemon",
        action="store_true",
        help="Run in background"
    )

    # Capture defined args and pass others to pytest directly
    args, unknown = parser.parse_known_args()
    
    # Handle status action
    if args.action == "status":
        status_tests()
        return
    
    run_tests(args, unknown)

if __name__ == "__main__":
    main()
