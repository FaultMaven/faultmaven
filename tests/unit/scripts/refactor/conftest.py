"""Shared fixtures for scripts/refactor/ tool tests.

Every test under this package builds a small throwaway git repository under
``tmp_path`` and runs a tool against it. None of them import anything from
``faultmaven`` — the modules a tool is pointed at are synthetic fixtures
written by the test itself, so these tests do not depend on this package's
internals moving around underneath them.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "scripts" / "refactor"


def load_module(name: str):
    """Load a scripts/refactor/<name>.py module the way test_check_contract_version.py does."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def init_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    return path


def commit_all(path: Path, message: str = "commit") -> None:
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=path, check=True)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A freshly `git init`-ed directory, ready for commits."""
    return init_repo(tmp_path)


@pytest.fixture(scope="session")
def python_exe() -> str:
    return sys.executable
