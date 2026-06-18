"""Tests for scripts/check_env_example_sync.py.

The script enforces that .env.example documented defaults stay in sync with the
settings.py Field defaults AND the registry default_model (the three-way guard).
These tests cover the parser, normalize(), the in-sync invariant on the real
files, and that drift / unknown vars are actually caught.
"""

import enum
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check_env_example_sync.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("check_env_example_sync", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_env_example_is_in_sync(mod):
    """The committed .env.example must match settings.py + registry (the CI guard)."""
    errors, checked, reg_checked, _skipped = mod.run_checks()
    assert errors == [], errors
    assert checked > 0
    assert reg_checked > 0  # registry leg actually ran


def test_normalize(mod):
    class Color(enum.Enum):
        RED = "red"

    assert mod.normalize(Color.RED) == "red"
    assert mod.normalize(True) == "true"
    assert mod.normalize(False) == "false"
    assert mod.normalize(8090) == "8090"


def test_parse_only_commented_lines_and_strips_inline(mod, tmp_path):
    p = tmp_path / ".env.example"
    p.write_text("# LOG_LEVEL=INFO   # inline note\nACTIVE_VAR=1\n# PORT=8090\n")
    parsed = {key: val for _ln, key, val in mod.parse_env_example(p)}
    # active (uncommented) lines are user overrides → not captured;
    # inline comment is stripped from the commented default.
    assert parsed == {"LOG_LEVEL": "INFO", "PORT": "8090"}


def test_detects_value_drift(mod, tmp_path):
    p = tmp_path / ".env.example"
    p.write_text("# LOG_LEVEL=DEBUG\n")  # settings default is INFO
    errors, *_ = mod.run_checks(p)
    assert any("LOG_LEVEL" in e for e in errors), errors


def test_flags_unknown_variable(mod, tmp_path):
    p = tmp_path / ".env.example"
    p.write_text("# NOT_A_REAL_SETTING=x\n")
    errors, *_ = mod.run_checks(p)
    assert any("NOT_A_REAL_SETTING" in e for e in errors), errors


def test_allowlisted_var_not_flagged(mod, tmp_path):
    p = tmp_path / ".env.example"
    p.write_text("# FM_IMAGE_TAG=latest\n")  # compose-only, allow-listed
    errors, *_ = mod.run_checks(p)
    assert errors == []
